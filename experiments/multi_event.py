"""Managed 3-event x 2-arm x N-seed x K-repeat distribution driver.

The parent owns an explicit immutable plan and run-count lifecycle.  Every
market simulation remains an independent managed ``experiments.run_seed``
child.  Dry-run and mock paths never probe a socket; real OpenAI-compatible
access requires ``--live`` and the frozen protocol environment.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import socket
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
from nmsim.result_reuse import (
    ChildRunIdentity,
    REPORTED_MODEL_GATE_REJECTED,
    ReuseDecision,
)
from nmsim.run_context import ManagedRunContext


COMMAND_IDENTITY = "experiments.multi_event"
PROTOCOL_PATH = Path(__file__).with_name("multi_event_protocol.json")
CATALOG_PATH = (
    Path(__file__).resolve().parents[1]
    / "nmsim"
    / "reference_data"
    / "v1"
    / "catalog.json"
)
PLAN_NAME = "multi_event_plan.json"
SELECTION_NAME = "multi_event_selection.json"
ATTEMPT_LEDGER_NAME = "multi_event_attempts.jsonl"
PRIVATE_ATTEMPT_LEDGER_NAME = "multi_event_attempts.private.jsonl"
MAX_PRIVATE_CHARS = 32768


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
    for material in materials:
        for arm in ("social_off", "social_on"):
            for seed in seeds:
                for repeat_idx in repeats:
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
        not isinstance(alias, str) or not alias for alias in raw
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
            "workers": workers,
            "network_access": bool(execution_mode == "openai_live"),
        },
        "health_and_retry": {
            "max_bad_frac": protocol["acceptance_and_execution"]["health_bad_frac_max"],
            "max_child_attempts": protocol["acceptance_and_execution"]["max_child_attempts"],
            "technical_retry_identity": "technical_retry_idx; excluded from repeat_idx/slot",
            "reported_model_gate": (
                "openai_live requires non-truncated exactly-one alias per child attempt; mock=[]"
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
            }
            for job in jobs
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
        out_root = Path(args.out).resolve()
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
    with managed:
        managed.context.manifest["multi_event_driver"] = {
            "schema_version": "1.0",
            "attempt_series_schema_version": ATTEMPT_SERIES_SCHEMA_VERSION,
            "protocol_id": protocol["protocol_id"],
            "protocol_sha256": protocol_hash,
            "execution_mode": mode,
            "protocol_adherence": adherence,
        }
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
                decision = assess_run_seed_reuse(
                    candidate_path=candidate,
                    allowed_result_root=out_root,
                    child_command=job.base_command,
                    max_bad_frac=health_threshold,
                )
                if decision is None:
                    raise RuntimeError("occupied attempt path produced no reuse decision")
                decision = _gate_reported_model(decision, mode=mode)
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
                    record_attempt(
                        job,
                        source="executed",
                        technical_retry_idx=technical_idx,
                        run_id=run_id,
                        status="rejected",
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
                        continue
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
                if process.returncode != 0:
                    last_reason = "subprocess_exit"
                    record_attempt(
                        job,
                        source="executed",
                        technical_retry_idx=technical_idx,
                        run_id=run_id,
                        status="rejected",
                        reason_code=last_reason,
                        private=subprocess_private,
                    )
                    if not materialized:
                        last_reason = "child_attempt_not_materialized"
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
                    continue

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

                manifest_path = out_root / "runs" / run_id / "run_manifest.json"
                decision = assess_run_seed_reuse(
                    candidate_path=manifest_path,
                    allowed_result_root=out_root,
                    child_command=job.base_command,
                    max_bad_frac=health_threshold,
                )
                if decision is None:
                    last_reason = "manifest_missing"
                    record_attempt(
                        job,
                        source="executed",
                        technical_retry_idx=technical_idx,
                        run_id=run_id,
                        status="rejected",
                        reason_code=last_reason,
                        private=subprocess_private,
                    )
                    continue
                decision = _gate_reported_model(decision, mode=mode)
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

                last_reason = decision.primary_reason or "child_identity_rejected"
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
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
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
            }
        study_model_identity = {
            "execution_mode": mode,
            **{field: identity[field] for field in (
                "model_request_config_hash", "requested_provider", "requested_model",
                "resolved_provider", "resolved_model", "endpoint_identity"
            )},
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
