"""Shared safety helpers for experiment driver subprocesses."""
from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import json
import os
from pathlib import Path
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional, Sequence

from nmsim.run_context import ManagedRunContext


DRIVER_SUMMARY_SCHEMA_VERSION = "1.0"


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
        self._cells: dict[str, dict[str, int | str]] = {
            cell: {
                "unit": "runs",
                "planned_runs": int(planned),
                "started_runs": 0,
                "completed_runs": 0,
                "failed_runs": 0,
                "honest_n_runs": 0,
                "reused_runs": 0,
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
                "completed_runs",
                "failed_runs",
                "honest_n_runs",
                "reused_runs",
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
        """Count a child job when its worker actually begins execution."""

        with self._lock:
            cell = self._cell(cell_name)
            cell["started_runs"] = int(cell["started_runs"]) + 1
            if int(cell["started_runs"]) > int(cell["planned_runs"]):
                raise RuntimeError("driver started_runs exceeds planned child runs")
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

    def _public_summary(self, *, legacy_failures_log: Optional[str]) -> dict[str, Any]:
        totals = self._totals()
        return {
            "schema_version": DRIVER_SUMMARY_SCHEMA_VERSION,
            "run_id": self.context.manifest["run_id"],
            "driver": self.command_identity,
            "unit": "runs",
            "worker_count": self.worker_count,
            "planned_runs": totals["planned_runs"],
            "started_runs": totals["started_runs"],
            "completed_runs": totals["completed_runs"],
            "failed_runs": totals["failed_runs"],
            "honest_n_runs": totals["honest_n_runs"],
            "reused_runs": totals["reused_runs"],
            "cells": self.cells,
            "failure_codes": dict(sorted(self._failure_codes.items())),
            "private_failure_details": self.PRIVATE_FAILURES_NAME,
            "legacy_failures_log": legacy_failures_log,
        }

    def _write_artifacts(self, *, legacy_failures_log: Optional[str]) -> Path:
        public_path = self.run_dir / self.PUBLIC_SUMMARY_NAME
        private_path = self.run_dir / self.PRIVATE_FAILURES_NAME

        with public_path.open("x", encoding="utf-8") as handle:
            json.dump(
                self._public_summary(legacy_failures_log=legacy_failures_log),
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

    def finish(self, *, legacy_failures_log: Optional[str] = None) -> Path:
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
                legacy_failures_log=legacy_failures_log
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
    is_healthy: Callable[[Any], bool],
    run_job: Callable[[Any], tuple[str, bool, str]],
) -> tuple[list[DriverJobResult], Path]:
    """Execute a legacy child-job driver inside one run-unit lifecycle.

    Existing healthy child results count as completed/reused runs but not as
    newly started work.  Captured legacy messages stay in the mode-0600 driver
    failure artifact; the public failure log contains controlled reason codes.
    The helper changes no child command, retry rule, health gate, or statistic.
    """

    plans = Counter(cell_name(job) for job in jobs)
    managed = ManagedDriverCompletion.create(
        out_root=out_root,
        command_identity=command_identity,
        cell_plans=plans,
        worker_count=workers,
    )
    todo = [job for job in jobs if not is_healthy(job)]
    failures: list[DriverJobResult] = []

    def execute(job: Any) -> DriverJobResult:
        cell = cell_name(job)
        seed = int(seed_identity(job))
        managed.record_started(cell)
        try:
            tag, ok, message = run_job(job)
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
            attempts=1,
            reason_code=None if ok else "child_run_failed",
            private_details=() if ok else ({"legacy_message": str(message)},),
        )

    with managed:
        for job in jobs:
            if is_healthy(job):
                managed.record_reused(cell_name(job))

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
