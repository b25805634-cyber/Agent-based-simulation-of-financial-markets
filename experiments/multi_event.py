"""Managed 3-event x 2-arm x N-seed x K-repeat distribution driver.

The parent owns an explicit immutable plan and run-count lifecycle.  Every
market simulation remains an independent managed ``experiments.run_seed``
child.  Dry-run and mock paths never probe a socket; real OpenAI-compatible
access requires ``--live`` and the frozen protocol environment.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import threading
import time
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from experiments.driver_utils import (
    DriverJobResult,
    ManagedDriverCompletion,
    assess_run_seed_reuse,
    expected_run_seed_identity,
    set_driver_provenance,
)
from nmsim.managed_cli import (
    BootstrapCLIError,
    ManagedCLIError,
    RaisingArgumentParser,
    bootstrap_cli,
    fail_cli,
)
from nmsim.multi_event import (
    ATTEMPT_SERIES_SCHEMA_VERSION,
    MultiEventMaterial,
    build_attempt_run_id,
    build_attempt_series_id,
    canonical_multi_event_basename,
    load_multi_event_material,
    load_protocol,
)
from nmsim.provenance import sha256_file
from nmsim.fingerprint import scientific_compatibility_metadata
from nmsim.provider_attempts import safe_reported_model
from nmsim.result_reuse import (
    ChildRunIdentity,
    REPORTED_MODEL_GATE_REJECTED,
    ReuseDecision,
)
from nmsim.run_context import ManagedRunContext


COMMAND_IDENTITY = "experiments.multi_event"
REPO_ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_PATH = Path(__file__).with_name("multi_event_protocol.json")
CATALOG_PATH = (
    REPO_ROOT
    / "nmsim"
    / "reference_data"
    / "v1"
    / "catalog.json"
)
PLAN_NAME = "multi_event_plan.json"
SELECTION_NAME = "multi_event_selection.json"
ATTEMPT_LEDGER_NAME = "multi_event_attempts.jsonl"
PRIVATE_ATTEMPT_LEDGER_NAME = "multi_event_attempts.private.jsonl"
ATTEMPT_COORDINATION_LOCK_NAME = ".multi_event_attempts.lock"
CANONICAL_LIVE_OUT = REPO_ROOT / "results_multi_event"
LIVE_SOURCE_SNAPSHOT_REJECTED = "live_source_snapshot_rejected"
MAX_PRIVATE_CHARS = 32768


class AttemptCoordinationError(RuntimeError):
    """A technical-attempt series cannot be inspected or advanced safely."""


class _OutputRootAttemptLock:
    """One durable advisory owner for every attempt series in an output root.

    The stable lock file is deliberately never deleted.  ``flock`` ownership
    is released by the kernel when the last inherited descriptor closes, so a
    dead parent is recoverable while an orphaned live child continues to block
    a resumed parent until that exact child terminates.
    """

    def __init__(self, out_root: Path, *, parent_run_id: str) -> None:
        self.path = Path(out_root) / ATTEMPT_COORDINATION_LOCK_NAME
        self.parent_run_id = str(parent_run_id)
        self.fd: int | None = None

    @property
    def pass_fds(self) -> tuple[int, ...]:
        if self.fd is None:
            raise RuntimeError("attempt coordination lock is not held")
        return (self.fd,)

    def __enter__(self) -> "_OutputRootAttemptLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.path, flags, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise AttemptCoordinationError(
                    "multi-event attempt coordination path is not a regular file"
                )
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                try:
                    owner = os.pread(fd, 4096, 0).decode("utf-8", "replace").strip()
                except OSError:
                    owner = ""
                detail = " owner metadata unavailable"
                if owner:
                    try:
                        parsed = json.loads(owner)
                        detail = " owner_run_id={}".format(
                            parsed.get("parent_run_id", "unknown")
                        )
                    except (TypeError, ValueError, json.JSONDecodeError):
                        pass
                raise AttemptCoordinationError(
                    "multi-event attempt coordination lock is already held;{}; "
                    "refusing to inspect or advance any technical-attempt series".format(
                        detail
                    )
                ) from error
            metadata = (
                json.dumps(
                    {
                        "schema_version": "multi_event_attempt_lock_v1",
                        "parent_run_id": self.parent_run_id,
                        "pid": os.getpid(),
                        "acquired_unix_time": time.time(),
                    },
                    sort_keys=True,
                    allow_nan=False,
                )
                + "\n"
            ).encode("utf-8")
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            offset = 0
            while offset < len(metadata):
                written = os.write(fd, metadata[offset:])
                if written <= 0:
                    raise OSError("short write to attempt coordination lock")
                offset += written
            os.fsync(fd)
            os.chmod(self.path, 0o600)
            self.fd = fd
            return self
        except BaseException:
            os.close(fd)
            raise

    def close(self) -> None:
        if self.fd is None:
            return
        fd, self.fd = self.fd, None
        # Do not issue LOCK_UN: all pass_fds descendants share this open file
        # description, and an explicit unlock would release their ownership.
        # Closing only the parent's descriptor lets the kernel retain the lock
        # until the last orphaned child descriptor closes.
        os.close(fd)

    def __exit__(self, exc_type, exc, traceback) -> bool:
        self.close()
        return False


@dataclass(frozen=True)
class MultiEventJob:
    material: MultiEventMaterial
    arm: str
    seed: int
    repeat_idx: int
    slot: Mapping[str, Any]
    basename: str
    base_command: tuple[str, ...]

    @property
    def event_id(self) -> str:
        return self.material.event_id

    @property
    def cell(self) -> str:
        return f"{self.event_id}__{self.arm}"

    @property
    def key(self) -> tuple[str, str, int, int]:
        return (self.event_id, self.arm, self.seed, self.repeat_idx)

    @property
    def tag(self) -> str:
        return "{} {} s{} r{}".format(
            self.event_id, self.arm, self.seed, self.repeat_idx
        )


def build_argparser() -> RaisingArgumentParser:
    parser = RaisingArgumentParser(allow_abbrev=False)
    parser.add_argument("--version", action="version", version="experiments.multi_event 1.0")
    parser.add_argument("--protocol", default=str(PROTOCOL_PATH))
    parser.add_argument("--catalog", default=str(CATALOG_PATH))
    parser.add_argument("--provider", choices=("mock", "openai"), default="mock")
    parser.add_argument("--model", default=None)
    parser.add_argument("--n", type=int, default=None,
                        help="mock-only test override; first N frozen seeds")
    parser.add_argument("--k", type=int, default=None,
                        help="mock-only test override; first K frozen repeats")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--out", default="results_multi_event")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--run-id", default=None)
    return parser


def _relative_file(path: Path, root: Path) -> str:
    try:
        return str(path.resolve(strict=True).relative_to(root.resolve(strict=True)))
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as error:
        raise ValueError("selection path escapes its declared root") from error


def _load_materials(protocol_path: Path, catalog_path: Path) -> tuple[Mapping[str, Any], str, list[MultiEventMaterial]]:
    protocol, protocol_hash = load_protocol(protocol_path)
    repo_root = Path(__file__).resolve().parents[1]
    materials = []
    for item in protocol["design"]["events"]:
        materials.append(
            load_multi_event_material(
                event_id=item["event_id"],
                reference_csv=repo_root / item["reference_csv"],
                news_timeline_jsonl=repo_root / item["news_timeline"],
                protocol_path=protocol_path,
                catalog_path=catalog_path,
            )
        )
    return protocol, protocol_hash, materials


def _validate_cli(args, protocol: Mapping[str, Any]) -> tuple[list[int], list[int], str, bool, str | None]:
    design = protocol["design"]
    frozen_seeds = list(design["seeds"])
    frozen_repeats = list(design["repeat_indices"])
    if args.workers < 1:
        raise ValueError("--workers must be >= 1")
    n = len(frozen_seeds) if args.n is None else args.n
    k = len(frozen_repeats) if args.k is None else args.k
    if not 1 <= n <= len(frozen_seeds):
        raise ValueError("--n must select 1..8 frozen seeds")
    if not 1 <= k <= len(frozen_repeats):
        raise ValueError("--k must select 1..3 frozen repeats")
    if args.live and args.provider != "openai":
        raise ValueError("--live requires --provider openai")
    if args.provider == "openai" and not args.live and not args.dry_run:
        raise ValueError("real OpenAI-compatible execution requires --live")
    if args.live and args.dry_run:
        raise ValueError("--live and --dry-run are mutually exclusive")
    if args.provider == "openai" and (args.n is not None or args.k is not None):
        raise ValueError("live execution cannot override frozen N/K")
    if args.live and args.workers != protocol["acceptance_and_execution"]["workers"]:
        raise ValueError("live execution requires the frozen --workers 1")

    frozen_model = protocol["effective_config_freeze"]["model_request"]["model"]
    if args.model is not None and args.model != frozen_model:
        raise ValueError("--model differs from the frozen protocol")
    ambient_provider = os.environ.get("LLM_PROVIDER")
    if ambient_provider and ambient_provider.strip().lower() != args.provider:
        raise ValueError("LLM_PROVIDER conflicts with --provider")
    ambient_model = os.environ.get("LLM_MODEL")
    if args.provider == "mock":
        if args.model is not None or ambient_model:
            raise ValueError("mock execution rejects model overrides")
    else:
        if ambient_model and ambient_model != frozen_model:
            raise ValueError("LLM_MODEL differs from the frozen protocol")
        frozen_endpoint = protocol["effective_config_freeze"]["model_request"][
            "openai_base_url"
        ]
        ambient_endpoint = os.environ.get("OPENAI_BASE_URL")
        if ambient_endpoint and ambient_endpoint != frozen_endpoint:
            raise ValueError("OPENAI_BASE_URL differs from the frozen protocol")

    selected_seeds = frozen_seeds[:n]
    selected_repeats = frozen_repeats[:k]
    if args.dry_run:
        mode = "dry_run"
        adherence = (
            args.provider == "openai"
            and args.n is None
            and args.k is None
            and args.workers
            == protocol["acceptance_and_execution"]["workers"]
        )
        reason = None if adherence else "dry_run_or_execution_override"
    elif args.provider == "mock":
        mode = "mock"
        adherence = False
        reason = "offline_engineering_acceptance_not_preregistered_realism"
    else:
        mode = "openai_live"
        adherence = args.workers == protocol["acceptance_and_execution"]["workers"]
        reason = None if adherence else "execution_worker_override"
    return selected_seeds, selected_repeats, mode, adherence, reason


def _resolve_output_root(raw: str, *, mode: str) -> Path:
    """Resolve a driver root; live has exactly one non-rebindable location."""

    expanded = os.path.expanduser(str(raw))
    lexical = Path(os.path.abspath(expanded))
    if mode != "openai_live":
        return lexical.resolve()
    canonical = CANONICAL_LIVE_OUT
    if lexical != canonical:
        raise ValueError(
            "live execution requires the canonical repository results_multi_event root"
        )
    if os.path.lexists(canonical):
        if canonical.is_symlink():
            raise ValueError("canonical live output root must not be a symlink")
        if not canonical.is_dir():
            raise ValueError("canonical live output root must be a directory")
        if canonical.resolve(strict=True) != canonical:
            raise ValueError("canonical live output root was rebound")
    return canonical


def _output_root_policy(mode: str, out_root: Path) -> Mapping[str, Any]:
    live = mode == "openai_live"
    return {
        "schema_version": "multi_event_output_root_v1",
        "effective_root": str(out_root),
        "canonical_repo_relative_root": "results_multi_event",
        "live_canonical_root_enforced": live,
        "alternate_root_allowed": not live,
        "symlink_or_rebinding_allowed": False if live else None,
        "attempt_cap_scope": (
            "single_canonical_study_root" if live else "non_live_engineering_root"
        ),
    }


def _git_stdout(*arguments: str) -> str:
    process = subprocess.run(
        ["git", "-C", str(REPO_ROOT), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )
    if process.returncode != 0:
        raise ValueError("cannot establish the live source snapshot")
    return process.stdout.strip()


def _source_snapshot(*, mode: str, protocol_path: Path) -> Mapping[str, Any]:
    """Bind live to the clean Git snapshot that owns the frozen protocol."""

    if mode != "openai_live":
        return {
            "schema_version": "multi_event_source_snapshot_v1",
            "execution_mode": mode,
            "live_snapshot_enforced": False,
            "live_eligibility_claim": False,
            "policy": "explicit_non_live_no_preregistered_source_snapshot_claim",
            "head_commit": None,
            "protocol_last_change_commit": None,
            "scientific_component_fingerprint": None,
        }
    canonical_protocol = PROTOCOL_PATH.resolve(strict=True)
    if protocol_path.resolve(strict=True) != canonical_protocol:
        raise ValueError("live execution requires the canonical protocol path")
    head = _git_stdout("rev-parse", "HEAD")
    protocol_relative = canonical_protocol.relative_to(REPO_ROOT).as_posix()
    protocol_commit = _git_stdout(
        "log", "-1", "--format=%H", "--", protocol_relative
    )
    status = _git_stdout("status", "--porcelain=v1", "--untracked-files=all")
    if not head or not protocol_commit:
        raise ValueError("live source snapshot commits are unavailable")
    if status:
        raise ValueError("live execution requires a completely clean repository")
    if head != protocol_commit:
        raise ValueError(
            "live HEAD must equal the commit that last changed the canonical protocol"
        )
    compatibility = scientific_compatibility_metadata(
        REPO_ROOT, git_state={"commit": head, "dirty": False}
    )
    return {
        "schema_version": "multi_event_source_snapshot_v1",
        "execution_mode": mode,
        "live_snapshot_enforced": True,
        "live_eligibility_claim": True,
        "policy": "clean_head_equals_canonical_protocol_last_change_commit",
        "repository_clean": True,
        "head_commit": head,
        "protocol_last_change_commit": protocol_commit,
        "protocol_repo_relative_path": protocol_relative,
        "scientific_component_fingerprint": compatibility[
            "scientific_component_fingerprint"
        ],
    }


def _validate_expected_source_snapshot(
    expected: Mapping[tuple[str, str, int, int], Any],
    source_snapshot: Mapping[str, Any],
    *,
    mode: str,
) -> None:
    if mode != "openai_live":
        return
    head = source_snapshot["head_commit"]
    fingerprint = source_snapshot["scientific_component_fingerprint"]
    if any(
        identity.git_commit != head
        or identity.scientific_component_fingerprint != fingerprint
        for identity in expected.values()
    ):
        raise ValueError(
            "live expected-child identity differs from the frozen source snapshot"
        )


def _attempt_lifecycle(candidate: Path) -> str:
    """Classify only enough lifecycle state to prevent in-flight advancement."""

    if not os.path.lexists(candidate):
        return "absent"
    if candidate.is_symlink() or not candidate.is_dir():
        return "terminal_invalid"
    manifest_path = candidate / "run_manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        return "indeterminate"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "indeterminate"
    if not isinstance(manifest, Mapping):
        return "indeterminate"
    managed_context = manifest.get("managed_context")
    managed_state = (
        managed_context.get("state")
        if isinstance(managed_context, Mapping)
        else None
    )
    if manifest.get("status") == "running" or managed_state == "ACTIVE":
        return "active"
    if manifest.get("status") in {"finished", "failed"} or managed_state in {
        "FINISHED",
        "FAILED",
    }:
        return "terminal"
    return "indeterminate"


def build_multi_event_child_command(
    *,
    material: MultiEventMaterial,
    arm: str,
    seed: int,
    repeat_idx: int,
    provider: str,
    out_root: Path,
) -> tuple[str, ...]:
    command = [
        sys.executable,
        "-m",
        "experiments.run_seed",
        "--seed",
        str(seed),
        "--provider",
        provider,
        "--social",
        "on" if arm == "social_on" else "off",
        "--repeat-idx",
        str(repeat_idx),
        "--reference-csv",
        str(material.reference_csv),
        "--news-timeline-jsonl",
        str(material.news_timeline_jsonl),
        "--event-id",
        material.event_id,
        "--protocol",
        str(material.protocol_path),
        "--catalog",
        str(material.catalog_path),
        "--out",
        str(out_root),
    ]
    if provider == "openai":
        command.extend(
            ["--model", str(material.protocol["effective_config_freeze"]["model_request"]["model"])]
        )
    return tuple(command)


def _build_jobs(
    materials: Sequence[MultiEventMaterial],
    *,
    seeds: Sequence[int],
    repeats: Sequence[int],
    provider: str,
    out_root: Path,
) -> list[MultiEventJob]:
    from nmsim.multi_event import build_experiment_slot

    jobs = []
    canonical_positions = {
        material.event_id: index for index, material in enumerate(materials)
    }
    for repeat_position, repeat_idx in enumerate(repeats):
        for seed_position, seed in enumerate(seeds):
            rotation = (repeat_position + seed_position) % len(materials)
            event_order = [
                materials[(rotation + offset) % len(materials)]
                for offset in range(len(materials))
            ]
            for material in event_order:
                canonical_event_position = canonical_positions[material.event_id]
                on_first = (
                    repeat_position
                    + seed_position
                    + canonical_event_position
                ) % 2 == 1
                arms = (
                    ("social_on", "social_off")
                    if on_first
                    else ("social_off", "social_on")
                )
                for arm in arms:
                    slot = build_experiment_slot(
                        protocol_hash=material.protocol_hash,
                        event_id=material.event_id,
                        social_arm=arm,
                        seed=seed,
                        repeat_idx=repeat_idx,
                    )
                    jobs.append(
                        MultiEventJob(
                            material=material,
                            arm=arm,
                            seed=seed,
                            repeat_idx=repeat_idx,
                            slot=slot,
                            basename=canonical_multi_event_basename(slot),
                            base_command=build_multi_event_child_command(
                                material=material,
                                arm=arm,
                                seed=seed,
                                repeat_idx=repeat_idx,
                                provider=provider,
                                out_root=out_root,
                            ),
                        )
                    )
    return jobs


def _effective_endpoint(protocol: Mapping[str, Any]) -> tuple[str, int]:
    raw = str(
        os.environ.get("OPENAI_BASE_URL")
        or protocol["effective_config_freeze"]["model_request"]["openai_base_url"]
    )
    try:
        parsed = urlsplit(raw)
        if not parsed.hostname:
            raise ValueError
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except (TypeError, ValueError) as error:
        raise ValueError("effective OPENAI_BASE_URL has no socket endpoint") from error
    return parsed.hostname, int(port)


def _endpoint_up(address: tuple[str, int], timeout: float = 4.0) -> bool:
    try:
        with socket.create_connection(address, timeout=timeout):
            return True
    except OSError:
        return False


def _wait_for_endpoint(address: tuple[str, int], max_wait: int = 180) -> bool:
    waited = 0
    while not _endpoint_up(address):
        if waited >= max_wait:
            return False
        time.sleep(8)
        waited += 8
    return True


def _model_aliases(
    manifest: Mapping[str, Any], result: Mapping[str, Any], *, mode: str
) -> list[str]:
    completion = manifest.get("completion")
    attempts = (
        completion.get("application_provider_attempts")
        if isinstance(completion, Mapping)
        else None
    )
    if not isinstance(attempts, Mapping):
        raise ValueError("application provider-attempt evidence is missing")
    if attempts.get("reported_models_truncated") is not False:
        raise ValueError("reported model aliases are truncated")
    raw = attempts.get("reported_models")
    if not isinstance(raw, list) or any(
        safe_reported_model(alias) != alias for alias in raw
    ):
        raise ValueError("reported model aliases are malformed")
    aliases = sorted(set(raw))
    if mode == "mock" and aliases:
        raise ValueError("mock child unexpectedly reported a model alias")
    if mode == "openai_live" and len(aliases) != 1:
        raise ValueError("live child must report exactly one model alias")
    if result.get("reported_model_aliases") != aliases:
        raise ValueError("result reported-model aliases do not match manifest")
    return aliases


def _gate_reported_model(
    decision: ReuseDecision, *, mode: str
) -> ReuseDecision:
    if not decision.reusable or decision.manifest_path is None:
        return decision
    try:
        manifest = json.loads(decision.manifest_path.read_text(encoding="utf-8"))
        result = json.loads(
            (decision.manifest_path.parent / "experiment_result.json").read_text(
                encoding="utf-8"
            )
        )
        _model_aliases(manifest, result, mode=mode)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return replace(
            decision,
            reusable=False,
            reason_codes=(REPORTED_MODEL_GATE_REJECTED,),
            cross_commit_same_scientific_fingerprint=False,
        )
    return decision


def _gate_live_source_snapshot(
    decision: ReuseDecision,
    *,
    mode: str,
    source_snapshot: Mapping[str, Any],
) -> ReuseDecision:
    if mode != "openai_live" or not decision.reusable:
        return decision
    if decision.manifest_path is None:
        return replace(
            decision,
            reusable=False,
            reason_codes=(LIVE_SOURCE_SNAPSHOT_REJECTED,),
            cross_commit_same_scientific_fingerprint=False,
        )
    try:
        manifest = json.loads(
            decision.manifest_path.read_text(encoding="utf-8")
        )
        git = manifest.get("git")
        if not isinstance(git, Mapping):
            raise ValueError("missing child Git identity")
        child = ChildRunIdentity.from_manifest(decision.manifest_path)
        if (
            git.get("dirty") is not False
            or child.git_commit != source_snapshot["head_commit"]
            or child.scientific_component_fingerprint
            != source_snapshot["scientific_component_fingerprint"]
        ):
            raise ValueError("child source snapshot mismatch")
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return replace(
            decision,
            reusable=False,
            reason_codes=(LIVE_SOURCE_SNAPSHOT_REJECTED,),
            cross_commit_same_scientific_fingerprint=False,
        )
    return decision


def _bounded_private(manager: ManagedDriverCompletion, value: str) -> Mapping[str, Any]:
    sanitized = manager.context._manager._sanitize_text(value or "", max_length=None)
    payload: dict[str, Any] = {
        "text": sanitized[:MAX_PRIVATE_CHARS],
        "truncated": len(sanitized) > MAX_PRIVATE_CHARS,
    }
    if payload["truncated"]:
        payload["sha256"] = hashlib.sha256(sanitized.encode("utf-8")).hexdigest()
        payload["sanitized_characters"] = len(sanitized)
    return payload


def _write_jsonl_exclusive(
    path: Path, records: Sequence[Mapping[str, Any]], *, mode: int
) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            for record in records:
                handle.write(
                    json.dumps(
                        record,
                        ensure_ascii=False,
                        sort_keys=True,
                        allow_nan=False,
                    )
                )
                handle.write("\n")
    finally:
        if fd >= 0:
            os.close(fd)
    os.chmod(path, mode)


def _append_jsonl_durable(
    path: Path, record: Mapping[str, Any], *, mode: int
) -> None:
    encoded = (
        json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    )
    fd = os.open(path, os.O_WRONLY | os.O_APPEND, mode)
    try:
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            fd = -1
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if fd >= 0:
            os.close(fd)


def _attempt_series_id(job: MultiEventJob, expected: Any) -> str:
    return build_attempt_series_id(job.slot, expected)


def _attempt_run_id(
    job: MultiEventJob, attempt_idx: int, attempt_series_id: str
) -> str:
    return build_attempt_run_id(job.slot, attempt_series_id, attempt_idx)


def _candidate_record(
    job: MultiEventJob,
    decision: ReuseDecision,
    *,
    out_root: Path,
    mode: str,
    attempt_run_ids: Sequence[str],
) -> Mapping[str, Any]:
    if not decision.reusable or decision.manifest_path is None:
        raise ValueError("accepted record requires a reusable child")
    child = ChildRunIdentity.from_manifest(decision.manifest_path)
    manifest = json.loads(decision.manifest_path.read_text(encoding="utf-8"))
    result_path = decision.manifest_path.parent / "experiment_result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    aliases = _model_aliases(manifest, result, mode=mode)
    attempts = list(attempt_run_ids)
    if (
        not attempts
        or len(attempts) > 5
        or len(attempts) != len(set(attempts))
        or child.run_id not in attempts
    ):
        raise ValueError("accepted child attempt identity is incomplete")
    return {
        "event_id": job.event_id,
        "arm": job.arm,
        "seed": job.seed,
        "repeat_idx": job.repeat_idx,
        "manifest_path": _relative_file(decision.manifest_path, out_root),
        "manifest_sha256": sha256_file(decision.manifest_path),
        "result_artifact": {
            "path": "experiment_result.json",
            "sha256": sha256_file(result_path),
        },
        "attempt_run_ids": attempts,
        "accepted_run_id": child.run_id,
        "identity": {
            "run_id": child.run_id,
            "command_identity": child.command_identity,
            "config_hash_schema_version": child.config_hash_schema_version,
            "scientific_config_hash": child.scientific_config_hash,
            "model_request_config_hash": child.model_request_config_hash,
            "scientific_input_identity": child.scientific_input_identity,
            "scenario_definition_hash": child.scenario_definition_hash,
            "population_identity": child.population_identity,
            "requested_provider": child.requested_provider,
            "requested_model": child.requested_model,
            "resolved_provider": child.resolved_provider,
            "resolved_model": child.resolved_model,
            "endpoint_identity": child.endpoint_identity,
            "reported_model_aliases": aliases,
            "scientific_runtime_environment": (
                child.scientific_runtime_environment
            ),
            "scientific_runtime_environment_identity": (
                child.scientific_runtime_environment_identity
            ),
        },
    }


def _selection_builder(**kwargs) -> Mapping[str, Any]:
    """Delegate immutable selection construction to the analyzer contract."""

    from experiments.aggregate_multi_event import build_selection_document

    return build_selection_document(**kwargs)


def _input_paths(materials: Sequence[MultiEventMaterial]) -> Mapping[str, str]:
    paths: dict[str, str] = {
        "protocol": str(materials[0].protocol_path),
        "catalog": str(materials[0].catalog_path),
    }
    for index, material in enumerate(materials):
        paths[f"reference_{index:02d}"] = str(material.reference_csv)
        paths[f"timeline_{index:02d}"] = str(material.news_timeline_jsonl)
    return paths


def _launch_order_policy() -> Mapping[str, Any]:
    return {
        "schema_version": "multi_event_launch_order_v1",
        "block_order": "repeat_position_then_seed_position",
        "event_rotation": "(repeat_position+seed_position)%3",
        "arm_pairing": "both_arms_adjacent_per_event_seed_repeat",
        "social_on_first": "(repeat_position+seed_position+canonical_event_position)%2==1",
        "expected_event_temporal_positions": "8_each_of_3_positions",
        "expected_arm_first_counts_per_event": {
            "social_on": 12,
            "social_off": 12,
        },
        "resume_policy": (
            "filter_ineligible_slots_without_reordering_remaining_jobs"
        ),
    }


def _plan_document(
    *,
    protocol: Mapping[str, Any],
    protocol_hash: str,
    materials: Sequence[MultiEventMaterial],
    jobs: Sequence[MultiEventJob],
    expected_identities: Mapping[tuple[str, str, int, int], Any],
    execution_mode: str,
    protocol_adherence: bool,
    override_reason: str | None,
    seeds: Sequence[int],
    repeats: Sequence[int],
    provider: str,
    workers: int,
    dry_run: bool,
    output_root_policy: Mapping[str, Any],
    source_snapshot: Mapping[str, Any],
) -> Mapping[str, Any]:
    return {
        "schema_version": "multi_event_plan_v1",
        "protocol_id": protocol["protocol_id"],
        "protocol_sha256": protocol_hash,
        "pre_run_plan": True,
        "dry_run": dry_run,
        "execution_plan": {
            "protocol_adherence": protocol_adherence,
            "execution_mode": execution_mode,
            "seeds": list(seeds),
            "repeat_indices": list(repeats),
            "planned_runs": len(jobs),
            "override_reason": override_reason,
            "launch_order_policy": dict(_launch_order_policy()),
        },
        "provider_request": {
            "provider": provider,
            "model": (
                protocol["effective_config_freeze"]["model_request"]["model"]
                if provider == "openai"
                else None
            ),
            "temperature": protocol["acceptance_and_execution"]["temperature"],
            "cache_enabled": protocol["acceptance_and_execution"]["cache_enabled"],
            "provider_sdk_max_retries": protocol[
                "effective_config_freeze"
            ]["model_request"]["provider_sdk_max_retries"],
            "workers": workers,
            "network_access": bool(execution_mode == "openai_live"),
            "parent_network_scope": (
                "connectivity_probe_only_children_own_provider_calls"
                if execution_mode == "openai_live"
                else "none"
            ),
        },
        "output_root_policy": dict(output_root_policy),
        "source_snapshot": dict(source_snapshot),
        "health_and_retry": {
            "max_bad_frac": protocol["acceptance_and_execution"]["health_bad_frac_max"],
            "max_child_attempts": protocol["acceptance_and_execution"]["max_child_attempts"],
            "technical_retry_identity": "technical_retry_idx; excluded from repeat_idx/slot",
            "reported_model_gate": (
                "openai_live requires non-truncated exactly-one alias per child attempt; mock=[]"
            ),
            "coordination": (
                "one output-root advisory lock inherited by launched children; "
                "ACTIVE attempts never advance"
            ),
        },
        "hash_types": [
            "scientific_config_hash",
            "model_request_config_hash",
            "execution_config_hash",
            "full_effective_config_hash",
            "scientific_input_identity",
            "scenario_definition_hash",
            "multi_event_slot_v1.slot_id",
            "scientific_runtime_environment_identity",
        ],
        "reference_transform": dict(protocol["reference_phase_transform"]),
        "inputs": [
            {
                "event_id": material.event_id,
                "reference_csv_sha256": material.reference_hash,
                "news_timeline_sha256": material.timeline_hash,
                "event_definition_sha256": material.event_definition_hash,
                "reference_transform_sha256": material.reference_transform_sha256,
            }
            for material in materials
        ],
        "planned_complete_seed_pairs": len(materials) * len(seeds),
        "honest_n_complete_seed_pairs": 0,
        "jobs": [
            {
                "launch_ordinal": launch_ordinal,
                "event_id": job.event_id,
                "arm": job.arm,
                "seed": job.seed,
                "repeat_idx": job.repeat_idx,
                "slot": dict(job.slot),
                "basename": job.basename,
                "attempt_series_id": _attempt_series_id(
                    job, expected_identities[job.key]
                ),
                "allowed_attempt_run_ids": [
                    _attempt_run_id(
                        job,
                        index,
                        _attempt_series_id(job, expected_identities[job.key]),
                    )
                    for index in range(
                        1,
                        int(
                            protocol["acceptance_and_execution"][
                                "max_child_attempts"
                            ]
                        )
                        + 1,
                    )
                ],
                "child_command": list(job.base_command),
                "scientific_config_hash": expected_identities[job.key].scientific_config_hash,
                "model_request_config_hash": expected_identities[job.key].model_request_config_hash,
                "scientific_input_identity": expected_identities[job.key].scientific_input_identity,
                "scenario_definition_hash": expected_identities[job.key].scenario_definition_hash,
                "scientific_runtime_environment": (
                    expected_identities[job.key].scientific_runtime_environment
                ),
                "scientific_runtime_environment_identity": (
                    expected_identities[
                        job.key
                    ].scientific_runtime_environment_identity
                ),
            }
            for launch_ordinal, job in enumerate(jobs, start=1)
        ],
    }


def _complete_seed_pairs(
    accepted_keys: set[tuple[str, str, int, int]],
    materials: Sequence[MultiEventMaterial],
    seeds: Sequence[int],
    repeats: Sequence[int],
) -> tuple[int, Mapping[str, int]]:
    per_event = {}
    for material in materials:
        complete = 0
        for seed in seeds:
            required = {
                (material.event_id, arm, seed, repeat_idx)
                for arm in ("social_off", "social_on")
                for repeat_idx in repeats
            }
            if required <= accepted_keys:
                complete += 1
        per_event[material.event_id] = complete
    return sum(per_event.values()), per_event


def main(argv=None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        bootstrap = bootstrap_cli(
            argv,
            default_out="results_multi_event",
            command_identity=COMMAND_IDENTITY,
        )
    except BootstrapCLIError as error:
        print(f"provenance_not_created_reason={type(error).__name__}", file=sys.stderr)
        raise SystemExit(2)

    parser = build_argparser()
    try:
        args = parser.parse_args(argv)
        protocol_path = Path(args.protocol).resolve(strict=True)
        catalog_path = Path(args.catalog).resolve(strict=True)
        protocol, protocol_hash, materials = _load_materials(
            protocol_path, catalog_path
        )
        seeds, repeats, mode, adherence, override_reason = _validate_cli(
            args, protocol
        )
        out_root = _resolve_output_root(args.out, mode=mode)
        output_root_policy = _output_root_policy(mode, out_root)
        source_snapshot = _source_snapshot(
            mode=mode, protocol_path=protocol_path
        )
        jobs = _build_jobs(
            materials,
            seeds=seeds,
            repeats=repeats,
            provider=args.provider,
            out_root=out_root,
        )
        expected = {
            job.key: expected_run_seed_identity(job.base_command) for job in jobs
        }
        _validate_expected_source_snapshot(
            expected, source_snapshot, mode=mode
        )
        attempt_series_ids = {
            job.key: _attempt_series_id(job, expected[job.key]) for job in jobs
        }
    except (ManagedCLIError, OSError, TypeError, ValueError) as error:
        fail_cli(bootstrap, error, failure_stage="config_validation")

    plan = _plan_document(
        protocol=protocol,
        protocol_hash=protocol_hash,
        materials=materials,
        jobs=jobs,
        expected_identities=expected,
        execution_mode=mode,
        protocol_adherence=adherence,
        override_reason=override_reason,
        seeds=seeds,
        repeats=repeats,
        provider=args.provider,
        workers=args.workers,
        dry_run=args.dry_run,
        output_root_policy=output_root_policy,
        source_snapshot=source_snapshot,
    )
    inputs = _input_paths(materials)

    if args.dry_run:
        context = ManagedRunContext.create_driver(
            out_root=out_root,
            command_identity=COMMAND_IDENTITY,
            planned_runs=0,
            run_id=args.run_id,
            worker_count=args.workers,
            input_paths=inputs,
        )
        with context:
            context.manifest["multi_event_driver"] = {
                "schema_version": "1.0",
                "attempt_series_schema_version": ATTEMPT_SERIES_SCHEMA_VERSION,
                "protocol_id": protocol["protocol_id"],
                "protocol_sha256": protocol_hash,
                "execution_mode": mode,
                "protocol_adherence": adherence,
                "planned_child_runs": len(jobs),
                "dry_run": True,
                "provider_constructed": False,
                "network_access": False,
                "network_scope": "none",
                "output_root_policy": dict(output_root_policy),
                "source_snapshot": dict(source_snapshot),
            }
            plan_path = Path(context.run_dir) / PLAN_NAME
            with plan_path.open("x", encoding="utf-8") as handle:
                json.dump(plan, handle, indent=2, sort_keys=True)
                handle.write("\n")
            context.register_llm_runtime(
                provider="none",
                model="none",
                mode="dry_run",
                cache_enabled=False,
                network_access=False,
                network_scope="none",
            )
            context.set_experiment_completion(
                planned_runs=0, started_runs=0, completed_runs=0, failed_runs=0
            )
            context.finish()
        print(f"dry-run planned {len(jobs)} slots -> {plan_path}")
        return

    set_driver_provenance(args.workers, COMMAND_IDENTITY)
    cell_plans: dict[str, int] = {}
    for job in jobs:
        cell_plans[job.cell] = cell_plans.get(job.cell, 0) + 1
    managed = ManagedDriverCompletion.create(
        out_root=out_root,
        command_identity=COMMAND_IDENTITY,
        cell_plans=cell_plans,
        worker_count=args.workers,
        run_id=args.run_id,
        input_paths=inputs,
    )
    attempt_lock = _OutputRootAttemptLock(
        out_root, parent_run_id=managed.run_dir.name
    )
    with managed, attempt_lock:
        managed.context.manifest["multi_event_driver"] = {
            "schema_version": "1.0",
            "attempt_series_schema_version": ATTEMPT_SERIES_SCHEMA_VERSION,
            "protocol_id": protocol["protocol_id"],
            "protocol_sha256": protocol_hash,
            "execution_mode": mode,
            "protocol_adherence": adherence,
            "network_access": bool(mode == "openai_live"),
            "network_scope": (
                "connectivity_probe_only_children_own_provider_calls"
                if mode == "openai_live"
                else "none"
            ),
            "output_root_policy": dict(output_root_policy),
            "source_snapshot": dict(source_snapshot),
            "attempt_coordination": {
                "schema_version": "multi_event_attempt_lock_v1",
                "path": ATTEMPT_COORDINATION_LOCK_NAME,
                "scope": "entire_output_root_all_attempt_series",
                "child_descriptor_inheritance": True,
                "stale_recovery": "kernel_release_after_last_inherited_fd_closes",
            },
        }
        managed.context.register_llm_runtime(
            provider="none",
            model="none",
            mode=(
                "connectivity_probe_only"
                if mode == "openai_live"
                else "offline_driver"
            ),
            cache_enabled=False,
            network_access=bool(mode == "openai_live"),
            network_scope=(
                "connectivity_probe_only; LLM provider calls occur in managed children"
                if mode == "openai_live"
                else "none; mock children perform no network access"
            ),
            provider_calls_owned_by="managed_children",
        )
        managed.context.manifest.write_atomic()
        plan_path = managed.run_dir / PLAN_NAME
        with plan_path.open("x", encoding="utf-8") as handle:
            json.dump(plan, handle, indent=2, sort_keys=True)
            handle.write("\n")
        public_ledger_path = managed.run_dir / ATTEMPT_LEDGER_NAME
        private_ledger_path = managed.run_dir / PRIVATE_ATTEMPT_LEDGER_NAME
        _write_jsonl_exclusive(public_ledger_path, (), mode=0o644)
        _write_jsonl_exclusive(private_ledger_path, (), mode=0o600)
        managed.context.manifest["technical_attempt_ledger"] = {
            "schema_version": "1.0",
            "durability": "append_flush_fsync_per_record",
            "public_path": ATTEMPT_LEDGER_NAME,
            "private_path": PRIVATE_ATTEMPT_LEDGER_NAME,
            "max_child_attempts_per_series": int(
                protocol["acceptance_and_execution"]["max_child_attempts"]
            ),
        }
        managed.context.manifest.write_atomic()
    
        health_threshold = float(
            protocol["acceptance_and_execution"]["health_bad_frac_max"]
        )
        max_attempts = int(
            protocol["acceptance_and_execution"]["max_child_attempts"]
        )
        endpoint = _effective_endpoint(protocol) if mode == "openai_live" else None
        decisions: dict[tuple[str, str, int, int], ReuseDecision] = {}
        attempt_run_ids: dict[tuple[str, str, int, int], list[str]] = {
            job.key: [] for job in jobs
        }
        final_reasons: dict[tuple[str, str, int, int], list[str]] = {
            job.key: [] for job in jobs
        }
        public_attempt_ledger: list[Mapping[str, Any]] = []
        private_attempt_ledger: list[Mapping[str, Any]] = []
        last_attempt_context: dict[
            tuple[str, str, int, int], tuple[int, str] | None
        ] = {job.key: None for job in jobs}
        next_attempt_idx = {job.key: 1 for job in jobs}
        preflight_block_reason: dict[
            tuple[str, str, int, int], str | None
        ] = {job.key: None for job in jobs}
        reuse_checks: list[tuple[MultiEventJob, ReuseDecision]] = []
        lock = threading.Lock()
    
        def record_attempt(
            job: MultiEventJob,
            *,
            source: str,
            technical_retry_idx: int | None,
            run_id: str | None,
            status: str,
            reason_code: str,
            private: Mapping[str, Any] | None = None,
        ) -> None:
            public = {
                "schema_version": "1.0",
                "event_id": job.event_id,
                "arm": job.arm,
                "seed": job.seed,
                "repeat_idx": job.repeat_idx,
                "slot_id": job.slot["slot_id"],
                "source": source,
                "technical_retry_idx": technical_retry_idx,
                "run_id": run_id,
                "status": status,
                "reason_code": reason_code,
            }
            with lock:
                _append_jsonl_durable(public_ledger_path, public, mode=0o644)
                public_attempt_ledger.append(public)
                if private is not None:
                    private_record = {**public, "private": dict(private)}
                    _append_jsonl_durable(
                        private_ledger_path, private_record, mode=0o600
                    )
                    private_attempt_ledger.append(private_record)
    
        # The official reuse surface is exactly the deterministic five-attempt
        # series.  A compatibility alias is never trusted as a selector: accepting
        # arbitrary aliases would permit unlimited off-protocol sampling followed
        # by pointing the alias at a favorable child.
        for job in jobs:
            series_id = attempt_series_ids[job.key]
            allowed_ids = [
                _attempt_run_id(job, index, series_id)
                for index in range(1, max_attempts + 1)
            ]
            occupied = [
                os.path.lexists(str(out_root / "runs" / run_id))
                for run_id in allowed_ids
            ]
            if any(
                occupied[index] and not all(occupied[:index])
                for index in range(len(occupied))
            ):
                raise RuntimeError(
                    "deterministic technical-attempt series contains a gap"
                )
            occupied_count = 0
            accepted_at: int | None = None
            for technical_idx, (run_id, exists) in enumerate(
                zip(allowed_ids, occupied), start=1
            ):
                if not exists:
                    break
                occupied_count += 1
                attempt_run_ids[job.key].append(run_id)
                candidate = out_root / "runs" / run_id
                if _attempt_lifecycle(candidate) == "active":
                    record_attempt(
                        job,
                        source="resumed_attempt",
                        technical_retry_idx=technical_idx,
                        run_id=run_id,
                        status="in_flight",
                        reason_code="attempt_in_flight",
                    )
                    raise AttemptCoordinationError(
                        "an existing technical attempt is ACTIVE; refusing to "
                        "advance or classify it as rejected"
                    )
                decision = assess_run_seed_reuse(
                    candidate_path=candidate,
                    allowed_result_root=out_root,
                    child_command=job.base_command,
                    max_bad_frac=health_threshold,
                )
                if decision is None:
                    raise RuntimeError("occupied attempt path produced no reuse decision")
                decision = _gate_reported_model(decision, mode=mode)
                decision = _gate_live_source_snapshot(
                    decision,
                    mode=mode,
                    source_snapshot=source_snapshot,
                )
                reuse_checks.append((job, decision))
                if decision.reusable:
                    if decision.run_id != run_id:
                        raise RuntimeError("attempt directory run_id is not canonical")
                    if accepted_at is not None:
                        raise RuntimeError("attempt series contains multiple accepted children")
                    accepted_at = technical_idx
                    decisions[job.key] = decision
                    record_attempt(
                        job,
                        source="resumed_attempt",
                        technical_retry_idx=technical_idx,
                        run_id=run_id,
                        status="accepted",
                        reason_code="identity_and_health_valid",
                    )
                else:
                    final_reasons[job.key].extend(decision.reason_codes)
                    record_attempt(
                        job,
                        source="resumed_attempt",
                        technical_retry_idx=technical_idx,
                        run_id=run_id,
                        status="rejected",
                        reason_code=(
                            decision.primary_reason or "child_identity_rejected"
                        ),
                    )
            if accepted_at is not None and accepted_at != occupied_count:
                raise RuntimeError("attempts exist after an accepted child")
            next_attempt_idx[job.key] = occupied_count + 1
            if accepted_at is None and occupied_count >= max_attempts:
                preflight_block_reason[job.key] = "attempt_budget_exhausted"
                final_reasons[job.key].append("attempt_budget_exhausted")
    
        todo = [job for job in jobs if not decisions.get(job.key, None) or not decisions[job.key].reusable]
        print(
            "multi-event: {} planned, {} reusable, {} to execute (workers={})".format(
                len(jobs), len(jobs) - len(todo), len(todo), args.workers
            ),
            flush=True,
        )
    
        def execute(job: MultiEventJob) -> DriverJobResult:
            managed.record_started(job.cell)
            launched = 0
            last_reason = "child_run_not_started"
            blocked = preflight_block_reason[job.key]
            if blocked is not None:
                return DriverJobResult(
                    cell=job.cell,
                    tag=job.tag,
                    seed=job.seed,
                    ok=False,
                    source="failed",
                    attempts=0,
                    reason_code=blocked,
                )

            for technical_idx in range(
                next_attempt_idx[job.key], max_attempts + 1
            ):
                if endpoint is not None and not _wait_for_endpoint(endpoint):
                    last_reason = "endpoint_unreachable"
                    record_attempt(
                        job,
                        source="executed",
                        technical_retry_idx=technical_idx,
                        run_id=None,
                        status="not_launched",
                        reason_code=last_reason,
                    )
                    with lock:
                        final_reasons[job.key].append(last_reason)
                    return DriverJobResult(
                        cell=job.cell,
                        tag=job.tag,
                        seed=job.seed,
                        ok=False,
                        source="failed",
                        attempts=launched,
                        reason_code=last_reason,
                    )

                run_id = _attempt_run_id(
                    job, technical_idx, attempt_series_ids[job.key]
                )
                command = list(job.base_command) + [
                    "--technical-retry-idx",
                    str(technical_idx),
                    "--run-id",
                    run_id,
                ]
                env = {**os.environ, "PYTHONHASHSEED": "0"}
                managed.record_child_run_launched(job.cell)
                launched += 1
                with lock:
                    attempt_run_ids[job.key].append(run_id)
                    last_attempt_context[job.key] = (technical_idx, run_id)
                record_attempt(
                    job,
                    source="executed",
                    technical_retry_idx=technical_idx,
                    run_id=run_id,
                    status="launched",
                    reason_code="child_process_launched",
                )

                try:
                    process = subprocess.run(
                        command,
                        capture_output=True,
                        text=True,
                        env=env,
                        check=False,
                        pass_fds=attempt_lock.pass_fds,
                    )
                except BaseException as error:
                    interrupted = isinstance(error, (KeyboardInterrupt, SystemExit))
                    last_reason = (
                        "subprocess_interrupted"
                        if interrupted
                        else "subprocess_exception"
                    )
                    materialized = os.path.lexists(
                        str(out_root / "runs" / run_id)
                    )
                    lifecycle = _attempt_lifecycle(
                        out_root / "runs" / run_id
                    )
                    record_attempt(
                        job,
                        source="executed",
                        technical_retry_idx=technical_idx,
                        run_id=run_id,
                        status=(
                            "in_flight"
                            if lifecycle == "active"
                            else "indeterminate"
                            if materialized
                            else "rejected"
                        ),
                        reason_code=last_reason,
                        private={
                            "exception_type": type(error).__name__,
                            "exception_detail": _bounded_private(
                                managed, str(error)
                            ),
                        },
                    )
                    if interrupted:
                        raise
                    if materialized:
                        raise AttemptCoordinationError(
                            "a subprocess exception left a materialized attempt; "
                            "refusing an in-process technical retry"
                        ) from error
                    with lock:
                        final_reasons[job.key].append(last_reason)
                    return DriverJobResult(
                        cell=job.cell,
                        tag=job.tag,
                        seed=job.seed,
                        ok=False,
                        source="failed",
                        attempts=launched,
                        reason_code=last_reason,
                    )

                materialized = os.path.lexists(
                    str(out_root / "runs" / run_id)
                )
                subprocess_private = {
                    "returncode": process.returncode,
                    "stdout": _bounded_private(managed, process.stdout),
                    "stderr": _bounded_private(managed, process.stderr),
                }
                if not materialized:
                    last_reason = "child_attempt_not_materialized"
                    with lock:
                        final_reasons[job.key].append(last_reason)
                    record_attempt(
                        job,
                        source="executed",
                        technical_retry_idx=technical_idx,
                        run_id=run_id,
                        status="rejected",
                        reason_code=last_reason,
                        private=subprocess_private,
                    )
                    return DriverJobResult(
                        cell=job.cell,
                        tag=job.tag,
                        seed=job.seed,
                        ok=False,
                        source="failed",
                        attempts=launched,
                        reason_code=last_reason,
                    )

                candidate = out_root / "runs" / run_id
                lifecycle = _attempt_lifecycle(candidate)
                if lifecycle in {"active", "indeterminate"}:
                    last_reason = (
                        "attempt_in_flight"
                        if lifecycle == "active"
                        else "attempt_materialization_indeterminate"
                    )
                    record_attempt(
                        job,
                        source="executed",
                        technical_retry_idx=technical_idx,
                        run_id=run_id,
                        status=(
                            "in_flight"
                            if lifecycle == "active"
                            else "indeterminate"
                        ),
                        reason_code=last_reason,
                        private=subprocess_private,
                    )
                    raise AttemptCoordinationError(
                        "a launched attempt is not terminal; refusing to advance "
                        "the deterministic technical-attempt series"
                    )

                manifest_path = candidate / "run_manifest.json"
                decision = assess_run_seed_reuse(
                    candidate_path=manifest_path,
                    allowed_result_root=out_root,
                    child_command=job.base_command,
                    max_bad_frac=health_threshold,
                )
                if decision is None:
                    last_reason = "attempt_materialization_indeterminate"
                    record_attempt(
                        job,
                        source="executed",
                        technical_retry_idx=technical_idx,
                        run_id=run_id,
                        status="indeterminate",
                        reason_code=last_reason,
                        private=subprocess_private,
                    )
                    raise AttemptCoordinationError(
                        "a materialized attempt disappeared during assessment; "
                        "refusing to advance"
                    )
                decision = _gate_reported_model(decision, mode=mode)
                decision = _gate_live_source_snapshot(
                    decision,
                    mode=mode,
                    source_snapshot=source_snapshot,
                )
                if decision.reusable:
                    with lock:
                        decisions[job.key] = decision
                    record_attempt(
                        job,
                        source="executed",
                        technical_retry_idx=technical_idx,
                        run_id=run_id,
                        status="accepted",
                        reason_code="identity_and_health_valid",
                        private=subprocess_private,
                    )
                    return DriverJobResult(
                        cell=job.cell,
                        tag=job.tag,
                        seed=job.seed,
                        ok=True,
                        source="executed",
                        attempts=launched,
                    )

                last_reason = decision.primary_reason or (
                    "subprocess_exit"
                    if process.returncode != 0
                    else "child_identity_rejected"
                )
                with lock:
                    final_reasons[job.key].extend(decision.reason_codes)
                record_attempt(
                    job,
                    source="executed",
                    technical_retry_idx=technical_idx,
                    run_id=run_id,
                    status="rejected",
                    reason_code=last_reason,
                    private={
                        **subprocess_private,
                        "reuse_reason_codes": list(decision.reason_codes),
                    },
                )

            with lock:
                final_reasons[job.key].append(last_reason)
            return DriverJobResult(
                cell=job.cell,
                tag=job.tag,
                seed=job.seed,
                ok=False,
                source="failed",
                attempts=launched,
                reason_code=last_reason,
            )
    
        def execute_guarded(job: MultiEventJob) -> DriverJobResult:
            try:
                return execute(job)
            except BaseException as error:
                if isinstance(
                    error,
                    (KeyboardInterrupt, SystemExit, AttemptCoordinationError),
                ):
                    raise
                reason = "driver_job_exception"
                with lock:
                    final_reasons[job.key].append(reason)
                    attempt_context = last_attempt_context[job.key]
                record_attempt(
                    job,
                    source="driver",
                    technical_retry_idx=(
                        None if attempt_context is None else attempt_context[0]
                    ),
                    run_id=None if attempt_context is None else attempt_context[1],
                    status="rejected",
                    reason_code=reason,
                    private={
                        "exception_type": type(error).__name__,
                        "exception_detail": _bounded_private(managed, str(error)),
                    },
                )
                return DriverJobResult(
                    cell=job.cell,
                    tag=job.tag,
                    seed=job.seed,
                    ok=False,
                    source="failed",
                    attempts=len(attempt_run_ids[job.key]),
                    reason_code=reason,
                )
    
        failures: list[DriverJobResult] = []
        for job, decision in reuse_checks:
            managed.record_reuse_candidate(
                job.cell, tag=job.tag, seed=job.seed, decision=decision
            )
        for job in jobs:
            decision = decisions.get(job.key)
            if decision is not None and decision.reusable:
                managed.record_reused(job.cell)

        if args.workers == 1:
            results = [execute_guarded(job) for job in todo]
        else:
            with ThreadPoolExecutor(max_workers=args.workers) as executor:
                futures = {
                    executor.submit(execute_guarded, job): job for job in todo
                }
                results = [future.result() for future in as_completed(futures)]
        for index, result in enumerate(results, start=1):
            if result.ok:
                managed.record_completed(result.cell)
            else:
                managed.record_failed(result)
                failures.append(result)
            print(
                "[{}/{}] {} {}".format(
                    index, len(todo), "OK" if result.ok else "FAIL", result.tag
                ),
                flush=True,
            )

        all_attempt_run_ids = [
            run_id
            for job in jobs
            for run_id in attempt_run_ids[job.key]
        ]
        if len(all_attempt_run_ids) != len(set(all_attempt_run_ids)):
            raise RuntimeError("one child attempt run_id appears in multiple slots")
        managed.context.manifest["technical_attempt_ledger"].update(
            {
                "public_records": len(public_attempt_ledger),
                "private_records": len(private_attempt_ledger),
            }
        )
        managed.context.manifest.write_atomic()

        accepted_records = []
        rejected_records = []
        for job in jobs:
            decision = decisions.get(job.key)
            if decision is not None and decision.reusable:
                accepted_records.append(
                    _candidate_record(
                        job,
                        decision,
                        out_root=out_root,
                        mode=mode,
                        attempt_run_ids=attempt_run_ids[job.key],
                    )
                )
            else:
                reasons = list(dict.fromkeys(final_reasons[job.key]))
                if not reasons:
                    reasons = ["child_result_missing"]
                rejected_records.append(
                    {
                        "event_id": job.event_id,
                        "arm": job.arm,
                        "seed": job.seed,
                        "repeat_idx": job.repeat_idx,
                        "status": (
                            "rejected" if attempt_run_ids[job.key] else "missing"
                        ),
                        "reason_codes": reasons,
                        "attempt_run_ids": list(attempt_run_ids[job.key]),
                    }
                )

        aliases = sorted(
            {
                alias
                for child in accepted_records
                for alias in child["identity"]["reported_model_aliases"]
            }
        )
        if accepted_records:
            identity = accepted_records[0]["identity"]
            strict = (
                "model_request_config_hash",
                "requested_provider",
                "requested_model",
                "resolved_provider",
                "resolved_model",
                "endpoint_identity",
                "scientific_runtime_environment_identity",
            )
            if any(
                any(child["identity"][field] != identity[field] for field in strict)
                for child in accepted_records[1:]
            ):
                raise RuntimeError("accepted child model identities are not uniform")
        else:
            identity = expected[jobs[0].key]
            identity = {
                "model_request_config_hash": identity.model_request_config_hash,
                "requested_provider": identity.requested_provider,
                "requested_model": identity.requested_model,
                "resolved_provider": identity.resolved_provider,
                "resolved_model": identity.resolved_model,
                "endpoint_identity": identity.endpoint_identity,
                "scientific_runtime_environment": (
                    identity.scientific_runtime_environment
                ),
                "scientific_runtime_environment_identity": (
                    identity.scientific_runtime_environment_identity
                ),
            }
        study_model_identity = {
            "execution_mode": mode,
            **{field: identity[field] for field in (
                "model_request_config_hash", "requested_provider", "requested_model",
                "resolved_provider", "resolved_model", "endpoint_identity",
                "scientific_runtime_environment_identity",
            )},
            "scientific_runtime_environment": identity[
                "scientific_runtime_environment"
            ],
            "reported_model_aliases": aliases,
        }
        reference_root = Path(__file__).resolve().parents[1]
        event_records = [
            {
                "event_id": material.event_id,
                "reference_csv": {
                    "path": _relative_file(material.reference_csv, reference_root),
                    "sha256": material.reference_hash,
                },
                "news_timeline": {
                    "path": _relative_file(material.news_timeline_jsonl, reference_root),
                    "sha256": material.timeline_hash,
                },
                "transformed_reference": {
                    "schema_version": "1.0",
                    "norm_log_path": list(material.transformed.norm_log_path),
                    "sha256": material.reference_transform_sha256,
                },
            }
            for material in materials
        ]
        execution_plan = {
            "protocol_adherence": adherence,
            "execution_mode": mode,
            "seeds": list(seeds),
            "repeat_indices": list(repeats),
            "planned_runs": len(jobs),
            "override_reason": override_reason,
            "launch_order_policy": dict(_launch_order_policy()),
        }
        planned_slots = [
            {
                "event_id": job.event_id,
                "arm": job.arm,
                "seed": job.seed,
                "repeat_idx": job.repeat_idx,
            }
            for job in jobs
        ]
        selection = _selection_builder(
            protocol=protocol,
            protocol_sha256=protocol_hash,
            execution_plan=execution_plan,
            events=event_records,
            catalog_inputs=[
                {
                    "path": _relative_file(materials[0].catalog_path, reference_root),
                    "sha256": materials[0].catalog_hash,
                }
            ],
            study_model_identity=study_model_identity,
            planned_slots=planned_slots,
            accepted_children=accepted_records,
            rejected_slots=rejected_records,
        )
        selection_path = managed.run_dir / SELECTION_NAME
        with selection_path.open("x", encoding="utf-8") as handle:
            json.dump(selection, handle, indent=2, sort_keys=True)
            handle.write("\n")

        accepted_keys = {
            (item["event_id"], item["arm"], item["seed"], item["repeat_idx"])
            for item in accepted_records
        }
        complete_pairs, per_event_pairs = _complete_seed_pairs(
            accepted_keys, materials, seeds, repeats
        )
        summary = managed.finish(
            summary_extra={
                "multi_event_protocol_sha256": protocol_hash,
                "attempt_series_schema_version": (
                    ATTEMPT_SERIES_SCHEMA_VERSION
                ),
                "multi_event_plan": PLAN_NAME,
                "multi_event_selection": SELECTION_NAME,
                "multi_event_public_attempt_ledger": ATTEMPT_LEDGER_NAME,
                "selection_accepted_children": len(accepted_records),
                "selection_rejected_or_missing_slots": len(rejected_records),
                "honest_n_complete_seed_pairs": complete_pairs,
                "honest_n_complete_seed_pairs_by_event": per_event_pairs,
                "reported_model_aliases": aliases,
                "underlying_model_identity_verified": False,
                "model_specific_inference_allowed": False,
                "reported_alias_homogeneous_pooling_allowed": bool(
                    mode == "openai_live" and len(aliases) == 1
                ),
                "pooling_scope": (
                    "single_endpoint_reported_alias_not_underlying_model_proof"
                    if mode == "openai_live" and len(aliases) == 1
                    else "endpoint_mixture_or_mock_not_model_specific"
                ),
                "incomplete": bool(rejected_records),
            }
        )
    print(
        "completed={} failed={} selection={} summary={}".format(
            len(accepted_records), len(rejected_records), selection_path, summary
        ),
        flush=True,
    )
    if rejected_records:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
