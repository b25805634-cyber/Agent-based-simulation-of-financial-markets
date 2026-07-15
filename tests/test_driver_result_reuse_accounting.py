from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from experiments.driver_utils import ManagedDriverCompletion, run_managed_driver_jobs
from experiments.grid2x2 import _command as grid_command
from nmsim.result_reuse import (
    ARTIFACT_HASH_MISMATCH,
    HEALTH_GATE_REJECTED,
    MODEL_MISMATCH,
    RESULT_REUSE_POLICY_VERSION,
    ReuseDecision,
)


def _decision(*, reusable: bool, reasons: tuple[str, ...] = ()) -> ReuseDecision:
    return ReuseDecision(
        policy_version=RESULT_REUSE_POLICY_VERSION,
        reusable=reusable,
        reason_codes=reasons,
        run_id="candidate-run",
        manifest_path=None,
        artifacts_verified=5 if reusable else 0,
        cross_commit_same_scientific_fingerprint=False,
    )


class DriverResultReuseAccountingTests(unittest.TestCase):
    def _managed(self, root: Path, *, planned: int = 1) -> ManagedDriverCompletion:
        return ManagedDriverCompletion.create(
            out_root=root,
            command_identity="tests.driver_result_reuse",
            cell_plans={"cell": planned},
            worker_count=1,
        )

    def _read_finished_summary(self, managed: ManagedDriverCompletion) -> dict:
        path = managed.finish()
        return json.loads(path.read_text(encoding="utf-8"))

    def test_summary_declares_reuse_policy_even_without_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with self._managed(Path(raw), planned=0) as managed:
                summary = self._read_finished_summary(managed)
        self.assertEqual(
            summary["result_reuse_policy_version"],
            RESULT_REUSE_POLICY_VERSION,
        )

    def test_valid_reuse_counts_reused_and_honest_but_not_executed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with self._managed(Path(raw)) as managed:
                managed.record_reuse_candidate(
                    "cell", tag="cell seed 1", seed=1,
                    decision=_decision(reusable=True),
                )
                managed.record_reused("cell")
                summary = self._read_finished_summary(managed)
        self.assertEqual(summary["reused_runs"], 1)
        self.assertEqual(summary["honest_n_runs"], 1)
        self.assertEqual(summary["executed_runs"], 0)

    def test_rejected_candidate_is_not_counted_before_fresh_execution(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            managed = self._managed(Path(raw))
            with managed:
                managed.record_reuse_candidate(
                    "cell", tag="cell seed 1", seed=1,
                    decision=_decision(reusable=False, reasons=(MODEL_MISMATCH,)),
                )
                interim = managed._public_summary(legacy_failures_log=None)
                self.assertEqual(interim["completed_runs"], 0)
                self.assertEqual(interim["honest_n_runs"], 0)
                managed.record_started("cell")
                managed.record_child_run_launched("cell")
                managed.record_completed("cell")
                self._read_finished_summary(managed)

    def test_rejected_candidate_records_stable_reason_count(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with self._managed(Path(raw)) as managed:
                managed.record_reuse_candidate(
                    "cell", tag="cell seed 1", seed=1,
                    decision=_decision(
                        reusable=False,
                        reasons=(ARTIFACT_HASH_MISMATCH,),
                    ),
                )
                managed.record_started("cell")
                managed.record_child_run_launched("cell")
                managed.record_completed("cell")
                summary = self._read_finished_summary(managed)
        self.assertEqual(summary["reuse_candidates_examined"], 1)
        self.assertEqual(summary["reuse_candidates_rejected"], 1)
        self.assertEqual(
            summary["reuse_rejection_codes"],
            {ARTIFACT_HASH_MISMATCH: 1},
        )

    def test_health_gate_rejection_uses_the_same_accounting_path(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with self._managed(Path(raw)) as managed:
                managed.record_reuse_candidate(
                    "cell", tag="cell seed 1", seed=1,
                    decision=_decision(
                        reusable=False, reasons=(HEALTH_GATE_REJECTED,)
                    ),
                )
                managed.record_started("cell")
                managed.record_child_run_launched("cell")
                managed.record_completed("cell")
                summary = self._read_finished_summary(managed)
        self.assertEqual(summary["reuse_rejection_codes"][HEALTH_GATE_REJECTED], 1)

    def test_fresh_success_counts_executed_once(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with self._managed(Path(raw)) as managed:
                managed.record_started("cell")
                managed.record_child_run_launched("cell")
                managed.record_completed("cell")
                summary = self._read_finished_summary(managed)
        self.assertEqual(summary["started_runs"], 1)
        self.assertEqual(summary["executed_runs"], 1)
        self.assertEqual(summary["reused_runs"], 0)
        self.assertEqual(summary["honest_n_runs"], 1)

    def test_retry_launches_can_exceed_planned_without_inflating_honest_n(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with self._managed(Path(raw)) as managed:
                managed.record_started("cell")
                managed.record_child_run_launched("cell")
                managed.record_child_run_launched("cell")
                managed.record_completed("cell")
                summary = self._read_finished_summary(managed)
        self.assertEqual(summary["planned_runs"], 1)
        self.assertEqual(summary["started_runs"], 1)
        self.assertEqual(summary["executed_runs"], 2)
        self.assertEqual(summary["completed_runs"], 1)
        self.assertEqual(summary["honest_n_runs"], 1)

    def test_shared_driver_hook_counts_each_actual_child_launch(self) -> None:
        def run_job(_job, child_launched):
            child_launched()
            child_launched()
            return "cell seed 1", True, "second attempt succeeded"

        with tempfile.TemporaryDirectory() as raw:
            failures, summary_path = run_managed_driver_jobs(
                out_root=Path(raw),
                command_identity="tests.driver-retry-hook",
                jobs=(("cell", 1),),
                workers=1,
                cell_name=lambda job: job[0],
                seed_identity=lambda job: job[1],
                assess_reuse=lambda _job: None,
                run_job=run_job,
            )
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        self.assertEqual(failures, [])
        self.assertEqual(summary["started_runs"], 1)
        self.assertEqual(summary["executed_runs"], 2)
        self.assertEqual(summary["honest_n_runs"], 1)

    def test_reuse_audit_is_public_identity_metadata_only(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with self._managed(Path(raw)) as managed:
                managed.record_reuse_candidate(
                    "cell", tag="cell seed 1", seed=1,
                    decision=_decision(reusable=True),
                )
                managed.record_reused("cell")
                summary = self._read_finished_summary(managed)
        encoded = json.dumps(summary, sort_keys=True)
        self.assertNotIn("private rationale", encoded.lower())
        self.assertNotIn("authorization", encoded.lower())
        self.assertEqual(summary["reuse_audit"][0]["artifacts_verified"], 5)

    def test_grid_model_override_enters_the_child_request_identity(self) -> None:
        command = grid_command(
            "real_on", "real", "on", 1, "mock", 0.0, "/tmp/results",
            "mock-alternate-model",
        )
        model_index = command.index("--model")
        self.assertEqual(command[model_index + 1], "mock-alternate-model")


if __name__ == "__main__":
    unittest.main()
