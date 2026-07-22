"""Shared safety helpers for experiment driver subprocesses."""
from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
import json
import math
import os
from pathlib import Path
import stat
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional, Sequence

from nmsim.config import Config
from nmsim.decision_contract import MULTI_EVENT_DECISION_RESPONSE_SCHEMA
from nmsim.run_context import ManagedRunContext
from nmsim.result_reuse import (
    ExpectedRunIdentity,
    ARTIFACT_INVALID,
    HEALTH_GATE_REJECTED,
    RESULT_REUSE_POLICY_VERSION,
    ReusableRunCandidate,
    ReuseDecision,
    validate_child_run_reuse,
)


DRIVER_SUMMARY_SCHEMA_VERSION = "1.0"
MULTI_EVENT_PRIVATE_ARTIFACTS = (
    "llm_records.jsonl",
    "private_events.jsonl",
)


def _gate_multi_event_private_artifacts(
    decision: ReuseDecision, expected: ExpectedRunIdentity
) -> ReuseDecision:
    """Require exact private-file type and mode without changing legacy reuse."""

    if (
        not decision.reusable
        or expected.experiment_slot is None
        or decision.manifest_path is None
    ):
        return decision
    run_dir = decision.manifest_path.parent
    try:
        for relative in MULTI_EVENT_PRIVATE_ARTIFACTS:
            path = run_dir / relative
            info = path.lstat()
            if (
                path.is_symlink()
                or not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise ValueError("private artifact policy mismatch")
    except (OSError, ValueError):
        return replace(
            decision,
            reusable=False,
            reason_codes=(ARTIFACT_INVALID,),
            cross_commit_same_scientific_fingerprint=False,
        )
    return decision


@dataclass(frozen=True)
class DriverJobResult:
    """One child-run outcome with public identity separate from private output."""

    cell: str
    tag: str
    seed: int
    ok: bool
    source: str
    attempts: int
    reason_code: Optional[str] = None
    private_details: tuple[Mapping[str, Any], ...] = ()


class ManagedDriverCompletion:
    """Central run-level completion and artifact writer for experiment drivers.

    ``honest_n_runs`` is deliberately the number of accepted child simulation
    runs.  It never derives from agent decisions, LLM responses, or rows in a
    result file.  Public artifacts contain only controlled reason codes; raw
    child stdout/stderr is kept in the mode-0600 private failure artifact.
    """

    PUBLIC_SUMMARY_NAME = "driver_summary.json"
    PRIVATE_FAILURES_NAME = "driver_failures.private.jsonl"

    @classmethod
    def create(
        cls,
        *,
        out_root: os.PathLike[str] | str,
        command_identity: str,
        cell_plans: Mapping[str, int],
        worker_count: Optional[int] = None,
        run_id: Optional[str] = None,
        input_paths: Any = None,
    ) -> "ManagedDriverCompletion":
        plans = {str(cell): int(count) for cell, count in cell_plans.items()}
        if any(count < 0 for count in plans.values()):
            raise ValueError("driver cell planned_runs must be non-negative")
        context = ManagedRunContext.create_driver(
            out_root=out_root,
            command_identity=command_identity,
            planned_runs=sum(plans.values()),
            run_id=run_id,
            worker_count=worker_count,
            input_paths=input_paths,
        )
        return cls(
            context=context,
            command_identity=command_identity,
            cell_plans=plans,
            worker_count=worker_count,
        )

    def __init__(
        self,
        *,
        context: ManagedRunContext,
        command_identity: str,
        cell_plans: Mapping[str, int],
        worker_count: Optional[int],
    ) -> None:
        self.context = context
        self.command_identity = command_identity
        self.worker_count = worker_count
        self._lock = threading.RLock()
        self._finished = False
        self._private_failures: list[dict[str, Any]] = []
        self._failure_codes: Counter[str] = Counter()
        self._reuse_rejection_codes: Counter[str] = Counter()
        self._reuse_audit: list[dict[str, Any]] = []
        self._cells: dict[str, dict[str, int | str]] = {
            cell: {
                "unit": "runs",
                "planned_runs": int(planned),
                "started_runs": 0,
                "executed_runs": 0,
                "completed_runs": 0,
                "failed_runs": 0,
                "honest_n_runs": 0,
                "reused_runs": 0,
                "reuse_candidates_examined": 0,
                "reuse_candidates_rejected": 0,
            }
            for cell, planned in sorted(cell_plans.items())
        }
        self._sync()

    @property
    def run_dir(self) -> Path:
        return Path(self.context.run_dir)

    @property
    def cells(self) -> dict[str, dict[str, int | str]]:
        with self._lock:
            return {name: dict(values) for name, values in self._cells.items()}

    def __enter__(self) -> "ManagedDriverCompletion":
        self.context.__enter__()
        self.context.set_stage("simulation")
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        return self.context.__exit__(exc_type, exc, traceback)

    def _cell(self, name: str) -> dict[str, int | str]:
        try:
            return self._cells[name]
        except KeyError as error:
            raise KeyError(f"unknown driver cell: {name}") from error

    def _totals(self) -> dict[str, int]:
        return {
            key: sum(int(cell[key]) for cell in self._cells.values())
            for key in (
                "planned_runs",
                "started_runs",
                "executed_runs",
                "completed_runs",
                "failed_runs",
                "honest_n_runs",
                "reused_runs",
                "reuse_candidates_examined",
                "reuse_candidates_rejected",
            )
        }

    def _assert_capacity(self, cell: Mapping[str, int | str]) -> None:
        accounted = int(cell["completed_runs"]) + int(cell["failed_runs"])
        if accounted > int(cell["planned_runs"]):
            raise RuntimeError("driver completion exceeds planned child runs")

    def _sync(self) -> None:
        totals = self._totals()
        self.context.set_experiment_completion(
            planned_runs=totals["planned_runs"],
            started_runs=totals["started_runs"],
            completed_runs=totals["completed_runs"],
            failed_runs=totals["failed_runs"],
            cells=self.cells,
        )

    def record_reuse_candidate(
        self,
        cell_name: str,
        *,
        tag: str,
        seed: int,
        decision: ReuseDecision,
    ) -> None:
        """Record a public-safe reuse decision without counting it as a run."""

        with self._lock:
            cell = self._cell(cell_name)
            cell["reuse_candidates_examined"] = (
                int(cell["reuse_candidates_examined"]) + 1
            )
            if not decision.reusable:
                cell["reuse_candidates_rejected"] = (
                    int(cell["reuse_candidates_rejected"]) + 1
                )
                for code in decision.reason_codes:
                    self._reuse_rejection_codes[str(code)] += 1
            self._reuse_audit.append(
                {
                    "cell": str(cell_name),
                    "tag": str(tag),
                    "seed": int(seed),
                    **decision.public_summary(),
                }
            )
            self._sync()

    def record_reused(self, cell_name: str, count: int = 1) -> None:
        """Count accepted pre-existing child results without pretending to run them."""

        with self._lock:
            cell = self._cell(cell_name)
            cell["completed_runs"] = int(cell["completed_runs"]) + int(count)
            cell["honest_n_runs"] = int(cell["honest_n_runs"]) + int(count)
            cell["reused_runs"] = int(cell["reused_runs"]) + int(count)
            self._assert_capacity(cell)
            self._sync()

    def record_started(self, cell_name: str) -> None:
        """Count a planned replicate slot when its worker begins execution."""

        with self._lock:
            cell = self._cell(cell_name)
            cell["started_runs"] = int(cell["started_runs"]) + 1
            if int(cell["started_runs"]) > int(cell["planned_runs"]):
                raise RuntimeError("driver started_runs exceeds planned child runs")
            self._sync()

    def record_child_run_launched(self, cell_name: str) -> None:
        """Count one newly launched managed child attempt.

        Retries are distinct immutable managed child attempts, so this count
        may exceed ``planned_runs`` while completed/honest-N remains bounded by
        the number of planned replicate slots.
        """

        with self._lock:
            cell = self._cell(cell_name)
            cell["executed_runs"] = int(cell["executed_runs"]) + 1
            self._sync()

    def record_completed(self, cell_name: str, *, reused: bool = False) -> None:
        with self._lock:
            cell = self._cell(cell_name)
            cell["completed_runs"] = int(cell["completed_runs"]) + 1
            cell["honest_n_runs"] = int(cell["honest_n_runs"]) + 1
            if reused:
                cell["reused_runs"] = int(cell["reused_runs"]) + 1
            self._assert_capacity(cell)
            self._sync()

    def record_failed(self, result: DriverJobResult) -> None:
        """Record a safe public failure code and mode-0600 private detail."""

        if result.ok:
            raise ValueError("record_failed requires an unsuccessful result")
        reason_code = result.reason_code or "child_run_failed"
        with self._lock:
            cell = self._cell(result.cell)
            cell["failed_runs"] = int(cell["failed_runs"]) + 1
            self._assert_capacity(cell)
            self._failure_codes[reason_code] += 1
            self._private_failures.append(
                {
                    "cell": result.cell,
                    "tag": result.tag,
                    "seed": int(result.seed),
                    "attempts": int(result.attempts),
                    "reason_code": reason_code,
                    "details": list(result.private_details),
                }
            )
            self._sync()

    def _public_summary(
        self,
        *,
        legacy_failures_log: Optional[str],
        summary_extra: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        totals = self._totals()
        payload = {
            "schema_version": DRIVER_SUMMARY_SCHEMA_VERSION,
            "result_reuse_policy_version": RESULT_REUSE_POLICY_VERSION,
            "run_id": self.context.manifest["run_id"],
            "driver": self.command_identity,
            "unit": "runs",
            "worker_count": self.worker_count,
            "planned_runs": totals["planned_runs"],
            "started_runs": totals["started_runs"],
            "executed_runs": totals["executed_runs"],
            "completed_runs": totals["completed_runs"],
            "failed_runs": totals["failed_runs"],
            "honest_n_runs": totals["honest_n_runs"],
            "reused_runs": totals["reused_runs"],
            "reuse_candidates_examined": totals["reuse_candidates_examined"],
            "reuse_candidates_rejected": totals["reuse_candidates_rejected"],
            "cells": self.cells,
            "failure_codes": dict(sorted(self._failure_codes.items())),
            "reuse_rejection_codes": dict(
                sorted(self._reuse_rejection_codes.items())
            ),
            "reuse_audit": list(self._reuse_audit),
            "private_failure_details": self.PRIVATE_FAILURES_NAME,
            "legacy_failures_log": legacy_failures_log,
        }
        extra = dict(summary_extra or {})
        overlap = sorted(set(extra) & set(payload))
        if overlap:
            raise ValueError(
                "driver summary extra cannot replace core fields: {}".format(overlap)
            )
        payload.update(extra)
        return payload

    def _write_artifacts(
        self,
        *,
        legacy_failures_log: Optional[str],
        summary_extra: Optional[Mapping[str, Any]] = None,
    ) -> Path:
        public_path = self.run_dir / self.PUBLIC_SUMMARY_NAME
        private_path = self.run_dir / self.PRIVATE_FAILURES_NAME

        with public_path.open("x", encoding="utf-8") as handle:
            json.dump(
                self._public_summary(
                    legacy_failures_log=legacy_failures_log,
                    summary_extra=summary_extra,
                ),
                handle,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
        os.chmod(public_path, 0o644)

        fd = os.open(private_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for failure in self._private_failures:
                handle.write(json.dumps(failure, sort_keys=True, default=str))
                handle.write("\n")
        os.chmod(private_path, 0o600)
        return public_path

    def finish(
        self,
        *,
        legacy_failures_log: Optional[str] = None,
        summary_extra: Optional[Mapping[str, Any]] = None,
    ) -> Path:
        """Write non-overwriting driver artifacts and finish the parent run."""

        with self._lock:
            if self._finished:
                return self.run_dir / self.PUBLIC_SUMMARY_NAME
            self.context.set_stage("result_export")
            totals = self._totals()
            if totals["completed_runs"] + totals["failed_runs"] != totals["planned_runs"]:
                raise RuntimeError("driver finished before every planned run was accounted for")
            if totals["honest_n_runs"] != totals["completed_runs"]:
                raise RuntimeError("driver honest_n_runs must equal accepted completed runs")
            self._sync()
            summary_path = self._write_artifacts(
                legacy_failures_log=legacy_failures_log,
                summary_extra=summary_extra,
            )
            self.context.finish()
            self._finished = True
            return summary_path


def run_managed_driver_jobs(
    *,
    out_root: os.PathLike[str] | str,
    command_identity: str,
    jobs: Sequence[Any],
    workers: int,
    cell_name: Callable[[Any], str],
    seed_identity: Callable[[Any], int],
    assess_reuse: Callable[[Any], Optional[ReuseDecision]],
    run_job: Callable[[Any, Callable[[], None]], tuple[str, bool, str]],
) -> tuple[list[DriverJobResult], Path]:
    """Execute child jobs inside one run-unit lifecycle.

    A pre-existing child counts as reused only after ``assess_reuse`` applies
    the centralized identity and artifact gate.  Rejected candidates are
    audited and then scheduled as fresh immutable child attempts; the old file
    is never counted merely because its name and health field look plausible.
    """

    plans = Counter(cell_name(job) for job in jobs)
    managed = ManagedDriverCompletion.create(
        out_root=out_root,
        command_identity=command_identity,
        cell_plans=plans,
        worker_count=workers,
    )
    assessments = [assess_reuse(job) for job in jobs]
    todo = [
        job for job, decision in zip(jobs, assessments)
        if decision is None or not decision.reusable
    ]
    failures: list[DriverJobResult] = []

    def execute(job: Any) -> DriverJobResult:
        cell = cell_name(job)
        seed = int(seed_identity(job))
        managed.record_started(cell)
        launched = 0

        def child_launched() -> None:
            nonlocal launched
            managed.record_child_run_launched(cell)
            launched += 1

        try:
            tag, ok, message = run_job(job, child_launched)
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            return DriverJobResult(
                cell=cell,
                tag="{} seed {}".format(cell, seed),
                seed=seed,
                ok=False,
                source="failed",
                attempts=0,
                reason_code="driver_job_exception",
                private_details=(
                    {
                        "exception_type": type(error).__name__,
                        "message": str(error),
                    },
                ),
            )
        return DriverJobResult(
            cell=cell,
            tag=str(tag),
            seed=seed,
            ok=bool(ok),
            source="executed" if ok else "failed",
            attempts=launched,
            reason_code=None if ok else "child_run_failed",
            private_details=() if ok else ({"legacy_message": str(message)},),
        )

    with managed:
        for job, decision in zip(jobs, assessments):
            if decision is None:
                continue
            cell = cell_name(job)
            seed = int(seed_identity(job))
            managed.record_reuse_candidate(
                cell,
                tag="{} seed {}".format(cell, seed),
                seed=seed,
                decision=decision,
            )
            if decision.reusable:
                managed.record_reused(cell)

        if workers > 1:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {executor.submit(execute, job): job for job in todo}
                results = [future.result() for future in as_completed(futures)]
        else:
            results = [execute(job) for job in todo]

        for index, result in enumerate(results, 1):
            if result.ok:
                managed.record_completed(result.cell)
            else:
                managed.record_failed(result)
                failures.append(result)
            print(
                "[{}/{}] {} {}".format(
                    index,
                    len(todo),
                    "OK" if result.ok else "FAIL",
                    result.tag,
                ),
                flush=True,
            )

        managed.context.set_stage("result_export")
        failure_log = Path(out_root) / "failures.log"
        with failure_log.open("a", encoding="utf-8") as stream:
            for result in failures:
                stream.write(
                    "{}: {}\n".format(
                        result.tag, result.reason_code or "child_run_failed"
                    )
                )
        summary = managed.finish(legacy_failures_log="failures.log")
    return failures, summary


def set_driver_provenance(workers: int, driver: str) -> None:
    """Expose driver-level concurrency to each run_seed subprocess."""
    os.environ["NMSIM_DRIVER_WORKERS"] = str(workers)
    os.environ["NMSIM_DRIVER"] = driver


def expected_run_seed_identity(command: Sequence[str]) -> ExpectedRunIdentity:
    """Derive the exact child identity from run_seed CLI arguments, offline.

    Parsing applies every run_seed default before the Config hashes are built.
    The helper never constructs an LLM Provider and never accesses a network.
    """

    tokens = [str(item) for item in command]
    try:
        module_index = tokens.index("experiments.run_seed")
    except ValueError as error:
        raise ValueError("child command is not experiments.run_seed") from error
    from experiments.run_seed import build_argparser, config_from_args

    args = build_argparser().parse_args(tokens[module_index + 1 :])
    effective_environment = None
    if args.reference_csv is not None:
        # Multi-event expected identity is reconstructed from the explicit
        # child command and frozen protocol, never from the analyzer/driver's
        # ambient shell at the later reuse-check time.
        effective_environment = {"LLM_PROVIDER": str(args.provider)}
        if args.provider == "openai":
            effective_environment.update(
                {
                    "LLM_MODEL": str(args.model or Config().openai_model),
                    "OPENAI_BASE_URL": Config().openai_base_url,
                }
            )
    cfg = config_from_args(args, environment=effective_environment)
    if args.multi_event_material is not None:
        from nmsim.multi_event import (
            canonical_multi_event_basename,
            multi_event_material_identity,
            multi_event_result_identity,
        )

        basename = canonical_multi_event_basename(args.experiment_slot)
        result_cell_identity = multi_event_result_identity(
            args.multi_event_material, args.experiment_slot
        )
        material_identity = multi_event_material_identity(
            args.multi_event_material, args.experiment_slot
        )
    else:
        rep_suffix = (
            "_r{}".format(args.repeat_idx)
            if args.repeat_idx is not None
            else ""
        )
        basename = "{}_s{}{}.json".format(args.label, args.seed, rep_suffix)
        result_cell_identity = None
        material_identity = None
    input_paths: dict[str, str] = {}
    for label, attribute in (
        ("price_csv", "price_csv"),
        ("traces_csv", "traces_csv"),
        ("propagation_csv", "propagation_csv"),
        ("news_timeline_jsonl", "news_timeline_jsonl"),
        ("multi_event_protocol", "protocol"),
        ("reference_catalog", "catalog"),
    ):
        value = getattr(args, attribute, None)
        if value:
            input_paths[label] = str(value)
    required_artifacts = ["experiment_result.json", basename]
    if args.multi_event_material is not None:
        required_artifacts.extend(
            (
                "llm_records.jsonl",
                "events.jsonl",
                "private_events.jsonl",
            )
        )
    return ExpectedRunIdentity.from_effective_config(
        cfg,
        command_identity="python -m experiments.run_seed",
        run_kind="simulation",
        input_paths=input_paths or None,
        required_artifacts=required_artifacts,
        experiment_slot=args.experiment_slot,
        multi_event_identity=result_cell_identity,
        multi_event_material_identity=material_identity,
        effective_environment=effective_environment,
    )


def assess_run_seed_reuse(
    *,
    candidate_path: os.PathLike[str] | str,
    allowed_result_root: os.PathLike[str] | str,
    child_command: Sequence[str],
    max_bad_frac: Optional[float] = None,
) -> Optional[ReuseDecision]:
    """Return ``None`` for no candidate, otherwise a fail-closed decision."""

    candidate = Path(candidate_path)
    if not os.path.lexists(str(candidate)):
        return None
    expected = expected_run_seed_identity(child_command)
    decision = validate_child_run_reuse(
        ReusableRunCandidate(candidate, Path(allowed_result_root)), expected
    )
    decision = _gate_multi_event_private_artifacts(decision, expected)
    if not decision.reusable or max_bad_frac is None:
        return decision
    # The canonical health projection lives in the registered result artifact,
    # irrespective of whether the caller selected the compatibility result,
    # the managed run directory, or run_manifest.json as its candidate.  The
    # central reuse gate above has already re-hashed this exact artifact.
    if decision.manifest_path is None:
        return replace(
            decision,
            reusable=False,
            reason_codes=(HEALTH_GATE_REJECTED,),
            cross_commit_same_scientific_fingerprint=False,
        )
    result_path = decision.manifest_path.parent / "experiment_result.json"
    try:
        with result_path.open(encoding="utf-8") as stream:
            health = json.load(stream)["health"]
        bad_orders = health["bad_orders"]
        total_orders = health["total_llm_orders"]
        declared_fraction = float(health["bad_frac"])
        if (
            isinstance(bad_orders, bool)
            or not isinstance(bad_orders, int)
            or isinstance(total_orders, bool)
            or not isinstance(total_orders, int)
            or bad_orders < 0
            or total_orders <= 0
            or bad_orders > total_orders
        ):
            raise ValueError("invalid health counts")
        raw_fraction = bad_orders / total_orders if total_orders else 0.0
        if expected.experiment_slot is not None:
            if set(health) != {
                "bad_orders",
                "total_llm_orders",
                "bad_frac",
                "schema_version",
                "decision_response_schema",
                "failure_union",
                "failure_union_counts",
            }:
                raise ValueError("multi-event health schema changed")
            union_counts = health["failure_union_counts"]
            count_keys = {
                "strict_schema_only",
                "legacy_parse_only",
                "provider_fallback_only",
                "multiple_failure_causes",
                "valid_decisions",
            }
            if (
                health["schema_version"] != "multi_event_health_v1"
                or health["decision_response_schema"]
                != MULTI_EVENT_DECISION_RESPONSE_SCHEMA
                or health["failure_union"]
                != "strict_schema_or_legacy_parse_or_provider_fallback"
                or not isinstance(union_counts, Mapping)
                or set(union_counts) != count_keys
                or any(
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or value < 0
                    for value in union_counts.values()
                )
                or sum(union_counts.values()) != total_orders
                or sum(
                    union_counts[key]
                    for key in count_keys - {"valid_decisions"}
                )
                != bad_orders
            ):
                raise ValueError("multi-event health union is inconsistent")
        if (
            not math.isfinite(declared_fraction)
            or not 0.0 <= declared_fraction <= 1.0
            or declared_fraction != round(raw_fraction, 4)
        ):
            raise ValueError("invalid health fraction")
    except (
        OSError,
        TypeError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
    ):
        raw_fraction = float("inf")
    if raw_fraction > float(max_bad_frac):
        return replace(
            decision,
            reusable=False,
            reason_codes=(HEALTH_GATE_REJECTED,),
            cross_commit_same_scientific_fingerprint=False,
        )
    return decision


def archive_rejected_result(path: str, reason: str) -> str | None:
    """Move an unhealthy legacy result out of analyzer globs without deleting it.

    The destination and its reason sidecar are uniquely created.  The temporary
    destination reservation makes the operation non-overwriting even in the
    unlikely event of a generated-name collision.
    """
    if not os.path.exists(path):
        return None

    rejected_dir = os.path.join(os.path.dirname(path), "rejected")
    os.makedirs(rejected_dir, exist_ok=True)
    stem, ext = os.path.splitext(os.path.basename(path))
    archived_at = datetime.now(timezone.utc).isoformat()

    while True:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        token = uuid.uuid4().hex[:12]
        archived = os.path.join(rejected_dir, f"{stem}__{stamp}-{token}{ext}")
        try:
            fd = os.open(archived, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        else:
            os.close(fd)
            break

    try:
        os.replace(path, archived)
    except Exception:
        os.unlink(archived)
        raise

    sidecar = os.path.splitext(archived)[0] + ".reason.json"
    with open(sidecar, "x") as fh:
        json.dump({
            "schema_version": "1.0",
            "archived_at": archived_at,
            "original_path": os.path.abspath(path),
            "archived_path": os.path.abspath(archived),
            "reason": reason,
        }, fh, indent=2)
    return archived
