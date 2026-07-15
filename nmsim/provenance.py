"""Run-level provenance and immutable output-directory management.

This module deliberately has no dependency on a particular LLM provider or on
the simulation loop.  Callers create a :class:`RunManager` before provider
construction, pass its ``events`` logger into the simulation, and finish (or
fail) it after all outputs have been written.

Scientific outputs live in ``<out_root>/runs/<run_id>``.  Compatibility links
may point legacy flat paths at the newest run, but an existing regular file is
never replaced.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import threading
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urlsplit, urlunsplit
import uuid

from .fingerprint import scientific_compatibility_metadata


MANIFEST_SCHEMA_VERSION = "1.0"
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SECRET_FIELD_RE = re.compile(
    r"(?:^|_)(?:api_?key|access_?token|auth_?token|secret|password|passwd)(?:$|_)",
    re.IGNORECASE,
)
_DEPENDENCIES = ("numpy", "matplotlib", "anthropic", "openai", "httpx")


def utc_now() -> str:
    """Return a sortable, timezone-explicit UTC timestamp."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return f"{stamp}-{uuid.uuid4().hex[:12]}"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def _redact_url_credentials(value: str) -> str:
    """Remove RFC-style userinfo without changing ordinary URLs."""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    if not parsed.scheme or not parsed.netloc or "@" not in parsed.netloc:
        return value
    host = parsed.netloc.rsplit("@", 1)[1]
    return urlunsplit((parsed.scheme, f"<redacted>@{host}", parsed.path,
                       parsed.query, parsed.fragment))


def redact_secrets(value: Any, field_name: str = "") -> Any:
    """Recursively redact credentials while retaining the complete key shape."""
    if field_name and _SECRET_FIELD_RE.search(field_name):
        return "<redacted>"
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, Mapping):
        return {str(k): redact_secrets(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact_secrets(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str) and (field_name.endswith("url") or field_name.endswith("uri")):
        return _redact_url_credentials(value)
    return _jsonable(value)


def _stable_json_hash(value: Any) -> str:
    encoded = json.dumps(_jsonable(value), sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return sha256_bytes(encoded)


def _run_git(repo_root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=10,
    )


def collect_git_state(repo_root: Path) -> dict:
    """Capture repository identity without mutating it.

    ``diff_hash`` covers the HEAD diff plus the porcelain status (and therefore
    names of untracked files).  Untracked file *contents* are intentionally not
    read: historical result trees can be very large.
    """
    try:
        commit_result = _run_git(repo_root, "rev-parse", "HEAD")
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return {"commit": None, "dirty": None, "diff_hash": None,
                "diff_hash_method": None, "error": f"{type(exc).__name__}: {exc}"}
    if commit_result.returncode != 0:
        error = commit_result.stderr.decode("utf-8", "replace").strip()
        return {"commit": None, "dirty": None, "diff_hash": None,
                "diff_hash_method": None,
                "error": error or f"git rev-parse exited {commit_result.returncode}"}

    commit = commit_result.stdout.decode("ascii", "replace").strip()
    try:
        status_result = _run_git(repo_root, "status", "--porcelain=v1",
                                 "--untracked-files=all")
        diff_result = _run_git(repo_root, "diff", "--binary", "HEAD", "--")
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return {"commit": commit, "dirty": None, "diff_hash": None,
                "diff_hash_method": None, "error": f"{type(exc).__name__}: {exc}"}
    if status_result.returncode != 0 or diff_result.returncode != 0:
        errors = b"\n".join((status_result.stderr, diff_result.stderr))
        return {"commit": commit, "dirty": None, "diff_hash": None,
                "diff_hash_method": None,
                "error": errors.decode("utf-8", "replace").strip() or "git inspection failed"}

    status = status_result.stdout
    dirty = bool(status.strip())
    diff_hash = None
    if dirty:
        diff_hash = sha256_bytes(diff_result.stdout + b"\x00STATUS\x00" + status)
    return {
        "commit": commit,
        "dirty": dirty,
        "diff_hash": diff_hash,
        "diff_hash_method": "sha256(git-diff-binary-HEAD + porcelain-status)",
        "error": None,
    }


def _dependency_versions() -> dict:
    versions = {}
    for package in _DEPENDENCIES:
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    return versions


def _file_descriptor(path: Path, display_path: Optional[str] = None) -> dict:
    item = {"path": display_path or str(path), "exists": path.exists()}
    if not path.exists():
        item.update({"kind": None, "size_bytes": None, "sha256": None,
                     "error": "not found"})
        return item
    try:
        if path.is_file():
            item.update({"kind": "file", "size_bytes": path.stat().st_size,
                         "sha256": sha256_file(path), "error": None})
        elif path.is_dir():
            item.update({"kind": "directory", "size_bytes": None,
                         "sha256": None, "error": "directory hashing is not supported"})
        else:
            item.update({"kind": "other", "size_bytes": None,
                         "sha256": None, "error": "not a regular file"})
    except OSError as exc:
        item.update({"kind": None, "size_bytes": None, "sha256": None,
                     "error": f"{type(exc).__name__}: {exc}"})
    return item


def _normalise_input_paths(input_paths: Any) -> list:
    if input_paths is None:
        return []
    if isinstance(input_paths, Mapping):
        return [(str(label), Path(path)) for label, path in input_paths.items() if path]
    if isinstance(input_paths, (str, os.PathLike)):
        return [("input", Path(input_paths))]
    result = []
    for index, path in enumerate(input_paths):
        if path:
            result.append((f"input_{index}", Path(path)))
    return result


def _planned_population(cfg: Any, personas: list) -> dict:
    ids = [str(p.get("id")) for p in personas]
    if getattr(cfg, "population", None):
        cast = []
        for persona_id, count in cfg.population.items():
            n = int(count)
            if persona_id == "influencer_amplifier":
                n = min(n, 1)
            if n > 0:
                cast.extend([str(persona_id)] * n)
        cast = cast[: int(getattr(cfg, "max_llm_agents", len(cast)))]
    else:
        limit = min(int(getattr(cfg, "n_llm_agents", len(ids))),
                    int(getattr(cfg, "max_llm_agents", len(ids))), len(ids))
        cast = ids[:max(0, limit)]
    return {
        "requested": _jsonable(getattr(cfg, "population", None)),
        "planned_llm_by_persona": dict(Counter(cast)),
        "planned_llm_total": len(cast),
        "planned_noise_total": int(getattr(cfg, "n_noise_agents", 0)),
        "actual_llm_by_persona": None,
        "actual_llm_total": None,
        "actual_noise_total": None,
        "actual_agent_ids": None,
    }


def _prompt_metadata(repo_root: Path) -> tuple:
    from . import prompts

    source_path = Path(prompts.__file__).resolve()
    source_hash = sha256_file(source_path)
    systems = {}
    for persona in prompts.PERSONAS:
        system = prompts.build_system(persona)
        systems[str(persona["id"])] = sha256_bytes(system.encode("utf-8"))
    try:
        display_source = str(source_path.relative_to(repo_root))
    except ValueError:
        display_source = str(source_path)
    return (
        [redact_secrets(p) for p in prompts.PERSONAS],
        {
            "template_version": f"sha256:{source_hash}",
            "source_path": display_source,
            "source_sha256": source_hash,
            "system_prompt_sha256": systems,
        },
    )


class RunManifest(dict):
    """Dictionary-shaped manifest with crash-safe atomic persistence."""

    def __init__(self, path: Path, initial: Mapping[str, Any]):
        super().__init__(_jsonable(initial))
        self.path = Path(path)

    def write_atomic(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temp.open("w", encoding="utf-8") as fh:
                json.dump(self, fh, indent=2, ensure_ascii=False, sort_keys=True,
                          allow_nan=False)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(str(temp), str(self.path))
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass


class _FallbackEventLogger:
    """Tiny logger used only when ``nmsim.events`` is unavailable."""

    schema_version = "1.0"

    def __init__(self, run_id: str, public_path: Path, private_path: Path):
        self.run_id = run_id
        self.public_path = Path(public_path)
        self.private_path = Path(private_path)
        self._counter = 0
        self._lock = threading.Lock()
        for path, mode in ((self.public_path, 0o644), (self.private_path, 0o600)):
            path.touch(mode=mode, exist_ok=True)
            os.chmod(str(path), mode)

    def emit(self, event_type: str, round_i: Optional[int] = None,
             agent_id: Optional[str] = None, data: Optional[dict] = None,
             private_data: Optional[dict] = None, **extra: Any) -> str:
        with self._lock:
            self._counter += 1
            event = {
                "run_id": self.run_id,
                "round": round_i,
                "event_id": f"evt-{self._counter:09d}",
                "timestamp": utc_now(),
                "agent_id": agent_id,
                "schema_version": self.schema_version,
                "type": event_type,
                "data": _jsonable(data or {}),
            }
            event.update(_jsonable(extra))
            with self.public_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
                fh.flush()
            if private_data is not None:
                private_event = dict(event)
                combined = dict(event["data"])
                combined.update(_jsonable(private_data))
                private_event["data"] = combined
                with self.private_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(private_event, ensure_ascii=False,
                                        sort_keys=True) + "\n")
                    fh.flush()
            return str(event["event_id"])

    def emit_private(self, event_type: str, round_i: Optional[int] = None,
                     agent_id: Optional[str] = None,
                     data: Optional[dict] = None) -> str:
        with self._lock:
            self._counter += 1
            event = {
                "run_id": self.run_id,
                "round": round_i,
                "event_id": f"evt-{self._counter:09d}",
                "timestamp": utc_now(),
                "agent_id": agent_id,
                "schema_version": self.schema_version,
                "type": event_type,
                "data": _jsonable(data or {}),
            }
            with self.private_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
                fh.flush()
            return str(event["event_id"])


def _create_event_logger(run_id: str, run_dir: Path):
    public_path = run_dir / "events.jsonl"
    private_path = run_dir / "private_events.jsonl"
    try:
        from .events import EventLogger
    except ImportError:
        return _FallbackEventLogger(run_id, public_path, private_path)

    # Phase-1's canonical constructor.  ``run_dir`` is also accepted for a
    # compatible logger that chooses the standard filenames itself.
    try:
        return EventLogger(run_id=run_id, public_path=public_path,
                           private_path=private_path)
    except TypeError as first_error:
        try:
            return EventLogger(run_id=run_id, run_dir=run_dir)
        except TypeError:
            raise first_error


class RunManager:
    """Own one immutable run directory, manifest, and event logger."""

    @classmethod
    def create(cls, cfg: Any, out_root: Optional[os.PathLike] = None,
               scenario_id: Optional[str] = None, run_id: Optional[str] = None,
               worker_count: Optional[int] = None, batching: Any = None,
               input_paths: Any = None, repo_root: Optional[os.PathLike] = None) -> "RunManager":
        return cls(cfg=cfg, out_root=out_root, scenario_id=scenario_id,
                   run_id=run_id, worker_count=worker_count, batching=batching,
                   input_paths=input_paths, repo_root=repo_root)

    def __init__(self, cfg: Any, out_root: Optional[os.PathLike] = None,
                 scenario_id: Optional[str] = None, run_id: Optional[str] = None,
                 worker_count: Optional[int] = None, batching: Any = None,
                 input_paths: Any = None, repo_root: Optional[os.PathLike] = None):
        self._lock = threading.RLock()
        self.cfg = cfg
        self.repo_root = Path(repo_root or Path(__file__).resolve().parent.parent).resolve()
        self.out_root = Path(out_root or getattr(cfg, "out_dir", "outputs")).expanduser()
        if not self.out_root.is_absolute():
            self.out_root = (Path.cwd() / self.out_root).resolve()
        self.run_id = run_id or _new_run_id()
        if not _RUN_ID_RE.fullmatch(self.run_id) or self.run_id in (".", ".."):
            raise ValueError(f"invalid run_id: {self.run_id!r}")

        # Inspect Git before creating provenance files, so this run cannot make
        # an otherwise-clean worktree look dirty.
        git_state = collect_git_state(self.repo_root)
        scientific_compatibility = scientific_compatibility_metadata(
            self.repo_root, git_state=git_state
        )
        self.scientific_compatibility = scientific_compatibility
        config = redact_secrets(cfg)
        personas, prompt = _prompt_metadata(self.repo_root)
        scenario_hash = _stable_json_hash({
            "n_rounds": getattr(cfg, "n_rounds", None),
            "news_round": getattr(cfg, "news_round", None),
            "news_text": getattr(cfg, "news_text", None),
            "population": getattr(cfg, "population", None),
            "seed_fraction": getattr(cfg, "seed_fraction", None),
        })
        scenario = {
            "id": scenario_id or f"scenario-{scenario_hash[:16]}",
            "identifier_source": "explicit" if scenario_id else "derived",
            "definition_sha256": scenario_hash,
        }

        inputs = _normalise_input_paths(input_paths)
        reference_path = getattr(cfg, "reference_path", "")
        if reference_path and not any(label == "reference_path" for label, _ in inputs):
            inputs.append(("reference_path", Path(reference_path)))
        input_items = []
        for label, path in inputs:
            descriptor = _file_descriptor(path, str(path))
            descriptor["label"] = label
            input_items.append(descriptor)

        started_at = utc_now()
        requested_worker_count = worker_count
        if requested_worker_count is None:
            try:
                requested_worker_count = int(
                    os.environ.get("NMSIM_DRIVER_WORKERS")
                    or os.environ.get("NMSIM_WORKER_COUNT", "1")
                )
            except ValueError:
                requested_worker_count = 1
        requested_provider = os.environ.get("LLM_PROVIDER") or getattr(cfg, "provider", None)
        requested_model = os.environ.get("LLM_MODEL") or getattr(cfg, "model", None) or None

        initial = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "run_id": self.run_id,
            "scenario_id": scenario["id"],
            "created_at": started_at,
            "started_at": started_at,
            "ended_at": None,
            "status": "running",
            "failure_reason": None,
            "git": git_state,
            **scientific_compatibility,
            "scientific_compatibility": scientific_compatibility,
            "cross_commit_same_scientific_fingerprint": None,
            "config": config,
            "config_sha256": _stable_json_hash(config),
            "scenario": scenario,
            "rng": {"seed": getattr(cfg, "seed", None),
                    "scope": "local simulator RNG; real-provider sampling may remain nondeterministic"},
            "llm": {
                "provider": requested_provider,
                "resolved_provider": None,
                "model": requested_model,
                "resolved_model": None,
                "temperature": getattr(cfg, "temperature", None),
                "max_tokens": getattr(cfg, "max_tokens", None),
                "cache_enabled": bool(getattr(cfg, "cache_enabled", False)),
                "use_cheap_model": bool(getattr(cfg, "use_cheap_model", False)),
                "mode": None,
                "record_source": None,
            },
            "execution": {
                "worker_count": max(1, int(requested_worker_count)),
                "batching": _jsonable(batching) if batching is not None else {
                    "strategy": "provider complete_batch; simulation submits one LLM-agent batch per round"
                },
                "batch_sizes": [],
                "batch_count": 0,
            },
            "personas": {
                "definitions": personas,
                "population": _planned_population(cfg, personas),
            },
            "prompt": prompt,
            "inputs": input_items,
            "environment": {
                "python_version": platform.python_version(),
                "python_implementation": platform.python_implementation(),
                "python_executable": sys.executable,
                "platform": platform.platform(),
                "dependencies": _dependency_versions(),
            },
            "samples": {"expected": None, "completed": 0, "failed": 0, "honest_n": 0},
            "results": [],
            "compatibility": {"latest_link": None, "legacy_links": [], "skipped": []},
            "warnings": [],
        }

        self.runs_root = self.out_root / "runs"
        self.run_dir = self.runs_root / self.run_id
        self.out_root.mkdir(parents=True, exist_ok=True)
        self.runs_root.mkdir(parents=True, exist_ok=True)
        # This is the non-overwrite guarantee and is intentionally not retried
        # for a caller-supplied id.  Default ids already contain UUID entropy.
        self.run_dir.mkdir(parents=False, exist_ok=False)
        self.manifest_path = self.run_dir / "run_manifest.json"
        self.manifest = RunManifest(self.manifest_path, initial)
        self.manifest.write_atomic()
        self.events = _create_event_logger(self.run_id, self.run_dir)
        self.event_logger = self.events
        self.public_events_path = self.run_dir / "events.jsonl"
        self.private_events_path = self.run_dir / "private_events.jsonl"
        self._terminal = False
        self._emit("RunStarted", data={"scenario_id": scenario["id"]})

    @property
    def manifest_data(self) -> dict:
        return self.manifest

    def _write(self) -> None:
        self.manifest.write_atomic()

    def _emit(self, event_type: str, round: Optional[int] = None,
              agent_id: Optional[str] = None, data: Optional[dict] = None,
              private: bool = False) -> None:
        try:
            if private:
                self.events.emit_private(event_type, round_i=round,
                                         agent_id=agent_id, data=data or {})
            else:
                self.events.emit(event_type, round_i=round, agent_id=agent_id,
                                 data=data or {})
        except Exception as exc:
            # Do not lose the run manifest merely because event I/O failed.
            warning = f"event logging failed for {event_type}: {type(exc).__name__}: {exc}"
            self.manifest["warnings"].append(self._sanitize_text(warning))
            self._write()

    def _sanitize_text(self, value: Any) -> str:
        text = str(value)
        secret_values = []
        if is_dataclass(self.cfg):
            config = asdict(self.cfg)
        elif isinstance(self.cfg, Mapping):
            config = self.cfg
        else:
            config = vars(self.cfg) if hasattr(self.cfg, "__dict__") else {}
        for key, val in config.items():
            if _SECRET_FIELD_RE.search(str(key)) and val:
                secret_values.append(str(val))
        for key, val in os.environ.items():
            if _SECRET_FIELD_RE.search(key) and val:
                secret_values.append(val)
        for secret in sorted(set(secret_values), key=len, reverse=True):
            if secret and secret != "EMPTY":
                text = text.replace(secret, "<redacted>")
        return text[:8000]

    def register_llm_runtime(self, llm: Any = None, provider: Optional[str] = None,
                             model: Optional[str] = None, mode: Optional[str] = None,
                             record_source: Optional[os.PathLike] = None,
                             cache_enabled: Optional[bool] = None,
                             **details: Any) -> None:
        """Record the provider/model actually in use (including fallbacks)."""
        with self._lock:
            if llm is not None:
                provider = provider or getattr(llm, "kind", None)
                model = model or getattr(llm, "model", None)
                if cache_enabled is None and hasattr(llm, "enabled"):
                    cache_enabled = bool(llm.enabled)
                if hasattr(llm, "cross_commit_same_scientific_fingerprint"):
                    cross_commit = bool(
                        getattr(llm, "cross_commit_same_scientific_fingerprint")
                    )
                    self.manifest[
                        "cross_commit_same_scientific_fingerprint"
                    ] = cross_commit
                    self.manifest["replay_compatibility"] = {
                        "source_git_commit": getattr(llm, "source_git_commit", None),
                        "current_git_commit": getattr(llm, "current_git_commit", None),
                        "source_git_dirty": getattr(
                            llm, "source_compatibility_metadata", {}
                        ).get("git_dirty"),
                        "current_git_dirty": getattr(
                            llm, "compatibility_metadata", {}
                        ).get("git_dirty"),
                        "source_scientific_component_fingerprint": getattr(
                            llm, "source_compatibility_metadata", {}
                        ).get("scientific_component_fingerprint"),
                        "current_scientific_component_fingerprint": getattr(
                            llm, "compatibility_metadata", {}
                        ).get("scientific_component_fingerprint"),
                        "strict_compatibility_passed": True,
                        "cross_commit_same_scientific_fingerprint": cross_commit,
                    }
            if provider is not None:
                self.manifest["llm"]["resolved_provider"] = str(provider)
            if model is not None:
                self.manifest["llm"]["resolved_model"] = str(model)
            if mode is not None:
                self.manifest["llm"]["mode"] = str(mode)
            if record_source is not None:
                self.manifest["llm"]["record_source"] = str(record_source)
            if cache_enabled is not None:
                self.manifest["llm"]["cache_enabled"] = bool(cache_enabled)
            if details:
                self.manifest["llm"].setdefault("runtime", {}).update(redact_secrets(details))
            self._write()

    def record_batch(self, size: int, **metadata: Any) -> None:
        with self._lock:
            self.manifest["execution"]["batch_sizes"].append(int(size))
            self.manifest["execution"]["batch_count"] = len(
                self.manifest["execution"]["batch_sizes"])
            if metadata:
                self.manifest["execution"].setdefault("batch_metadata", []).append(
                    redact_secrets(metadata))
            self._write()

    def register_batch_sizes(self, sizes: Iterable[int]) -> None:
        with self._lock:
            self.manifest["execution"]["batch_sizes"] = [int(n) for n in sizes]
            self.manifest["execution"]["batch_count"] = len(
                self.manifest["execution"]["batch_sizes"])
            self._write()

    def set_population(self, agents: Iterable[Any]) -> None:
        agents = list(agents)
        llm_agents = [a for a in agents if bool(getattr(a, "is_llm", False))]
        noise_agents = [a for a in agents if not bool(getattr(a, "is_llm", False))]
        counts = Counter(str(getattr(a, "persona_id", "unknown")) for a in llm_agents)
        with self._lock:
            pop = self.manifest["personas"]["population"]
            pop["actual_llm_by_persona"] = dict(counts)
            pop["actual_llm_total"] = len(llm_agents)
            pop["actual_noise_total"] = len(noise_agents)
            pop["actual_agent_ids"] = [str(getattr(a, "name", "unknown")) for a in agents]
            self._write()

    def set_samples(self, expected: Optional[int] = None,
                    completed: Optional[int] = None, failed: Optional[int] = None,
                    honest_n: Optional[int] = None) -> None:
        with self._lock:
            samples = self.manifest["samples"]
            if expected is not None:
                samples["expected"] = int(expected)
            if completed is not None:
                samples["completed"] = int(completed)
            if failed is not None:
                samples["failed"] = int(failed)
            if honest_n is None and completed is not None:
                honest_n = max(0, int(completed) - int(samples.get("failed", 0)))
            if honest_n is not None:
                samples["honest_n"] = int(honest_n)
            self._write()

    def collect_artifacts(self, extra_paths: Any = None) -> list:
        """Hash all current run artifacts, excluding the self-referential manifest."""
        descriptors = []
        for path in sorted(self.run_dir.rglob("*")):
            if not path.is_file() or path == self.manifest_path or path.name.startswith(
                    f".{self.manifest_path.name}."):
                continue
            rel = str(path.relative_to(self.run_dir))
            descriptor = _file_descriptor(path, rel)
            descriptor["inside_run_directory"] = True
            descriptors.append(descriptor)
        for label, path in _normalise_input_paths(extra_paths):
            descriptor = _file_descriptor(path, str(path))
            descriptor["label"] = label
            descriptor["inside_run_directory"] = False
            descriptors.append(descriptor)
        with self._lock:
            self.manifest["results"] = descriptors
            self._write()
        return descriptors

    def register_result(self, path: os.PathLike, label: str = "result") -> dict:
        descriptor = _file_descriptor(Path(path), str(path))
        descriptor.update({"label": label,
                           "inside_run_directory": self.run_dir in Path(path).resolve().parents})
        with self._lock:
            retained = [r for r in self.manifest["results"] if r.get("path") != str(path)]
            retained.append(descriptor)
            self.manifest["results"] = retained
            self._write()
        return descriptor

    @staticmethod
    def _managed_symlink(path: Path, basename: Optional[str] = None) -> bool:
        if not path.is_symlink():
            return False
        target = os.readlink(str(path))
        if basename is None:
            return target.startswith("runs/") and len(Path(target).parts) == 2
        return target == f"latest/{basename}"

    def publish_legacy_links(self, filenames: Iterable[os.PathLike]) -> dict:
        """Publish a managed ``latest`` link and safe flat compatibility links.

        Existing regular files and unrelated symlinks are recorded as skipped
        and left byte-for-byte untouched.
        """
        with self._lock:
            compatibility = self.manifest["compatibility"]
            latest = self.out_root / "latest"
            latest_target = os.path.relpath(str(self.run_dir), str(self.out_root))
            if os.path.lexists(str(latest)) and not self._managed_symlink(latest):
                message = f"left existing non-managed path untouched: {latest}"
                compatibility["skipped"].append(message)
                self.manifest["warnings"].append(message)
                latest_ok = False
            else:
                temporary = self.out_root / f".latest.{uuid.uuid4().hex}.tmp"
                try:
                    os.symlink(latest_target, str(temporary))
                    os.replace(str(temporary), str(latest))
                    compatibility["latest_link"] = {
                        "path": str(latest), "target": latest_target,
                    }
                    latest_ok = True
                finally:
                    if os.path.lexists(str(temporary)):
                        temporary.unlink()

            if latest_ok:
                for filename in filenames:
                    basename = Path(filename).name
                    source = self.run_dir / basename
                    link = self.out_root / basename
                    if not source.is_file():
                        message = f"legacy link source does not exist: {source}"
                        compatibility["skipped"].append(message)
                        continue
                    if os.path.lexists(str(link)) and not self._managed_symlink(link, basename):
                        message = f"left existing non-managed path untouched: {link}"
                        compatibility["skipped"].append(message)
                        continue
                    temporary = self.out_root / f".{basename}.{uuid.uuid4().hex}.tmp"
                    try:
                        os.symlink(f"latest/{basename}", str(temporary))
                        os.replace(str(temporary), str(link))
                        compatibility["legacy_links"].append({
                            "path": str(link), "target": f"latest/{basename}",
                        })
                    finally:
                        if os.path.lexists(str(temporary)):
                            temporary.unlink()
            self._write()
            return _jsonable(compatibility)

    def finish(self, expected: Optional[int] = None,
               completed: Optional[int] = None, failed: Optional[int] = None,
               honest_n: Optional[int] = None, extra_results: Any = None) -> Path:
        with self._lock:
            if self._terminal:
                return self.manifest_path
            if any(v is not None for v in (expected, completed, failed, honest_n)):
                self.set_samples(expected=expected, completed=completed,
                                 failed=failed, honest_n=honest_n)
            self._emit("RunFinished", data={"samples": self.manifest["samples"]})
            self.manifest["status"] = "finished"
            self.manifest["ended_at"] = utc_now()
            self.manifest["failure_reason"] = None
            self.collect_artifacts(extra_results)
            self._terminal = True
            self._write()
            return self.manifest_path

    def fail(self, error: Any, expected: Optional[int] = None,
             completed: Optional[int] = None, failed: Optional[int] = None,
             honest_n: Optional[int] = None, extra_results: Any = None) -> Path:
        with self._lock:
            if self._terminal:
                return self.manifest_path
            reason = self._sanitize_text(
                error if isinstance(error, str) else f"{type(error).__name__}: {error}")
            if any(v is not None for v in (expected, completed, failed, honest_n)):
                self.set_samples(expected=expected, completed=completed,
                                 failed=failed, honest_n=honest_n)
            self._emit("RunFailed", data={"failure_reason": reason})
            self.manifest["status"] = "failed"
            self.manifest["ended_at"] = utc_now()
            self.manifest["failure_reason"] = reason
            self.collect_artifacts(extra_results)
            self._terminal = True
            self._write()
            return self.manifest_path


__all__ = [
    "MANIFEST_SCHEMA_VERSION",
    "RunManifest",
    "RunManager",
    "collect_git_state",
    "redact_secrets",
    "sha256_bytes",
    "sha256_file",
    "utc_now",
]
