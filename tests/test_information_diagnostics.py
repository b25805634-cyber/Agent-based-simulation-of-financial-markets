"""Managed offline diagnostic coverage; synthetic inputs stay in temp roots."""
from contextlib import redirect_stdout
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from experiments import information_diagnostics as entry
from nmsim.information_artifacts import file_sha256, read_json, verify_run
from tests import test_information_market_entrypoint as market_tests
from tests.test_human_reference import fixture_records
from nmsim.human_reference import SOURCE_COLUMNS


class ManagedInformationDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="information-diagnostics-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_learning_curves_integrate_frozen_source_without_test_metrics(self):
        helper = market_tests.InformationMarketEntrypointTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        model = helper.train()
        with redirect_stdout(io.StringIO()), patch("socket.create_connection", side_effect=AssertionError("network")):
            entry.main(["--task", "learning-curves", "--source-run", str(helper.source), "--model-run", str(model),
                        "--epochs", "2", "--hidden-dim", "4", "--backend", "python", "--fractions", "0.5", "1",
                        "--out", str(self.root), "--run-id", "curve-test"])
        run = self.root/"runs/curve-test"
        result = read_json(run/"diagnostic_result.json")
        self.assertEqual(len(result["points"]), 2)
        for point in result["points"]:
            for metrics in point["evaluation"].values():
                self.assertEqual(set(metrics), {"train", "validation"})
        self.assertEqual(read_json(run/"summary.json")["honest_n"]["model_fits"], 6)
        verify_run(run)

    def test_wrong_human_csv_bytes_fail_before_reconstruction(self):
        path = self.root/"not_the_published_file.csv"
        path.write_text("fake\n")
        with redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
            entry.main(["--task", "human-reference", "--csv", str(path), "--out", str(self.root), "--run-id", "bad-source"])
        manifest = read_json(self.root/"runs/bad-source/run_manifest.json")
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["honest_n"], 0)

    def test_human_audit_keeps_joint_records_private_and_future_outcome_out(self):
        path = self.root/"synthetic_only.csv"
        with path.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=SOURCE_COLUMNS)
            writer.writeheader()
            writer.writerows(fixture_records())
        with patch("nmsim.human_reference.SOURCE_CSV_SHA256", file_sha256(path)), redirect_stdout(io.StringIO()), \
                patch("socket.create_connection", side_effect=AssertionError("network")):
            entry.main(["--task", "human-reference", "--csv", str(path), "--out", str(self.root), "--run-id", "synthetic-reference"])
        run = self.root/"runs/synthetic-reference"
        private = run/"private_joint_decisions.jsonl"
        self.assertEqual(private.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("result_final", private.read_text())
        self.assertNotIn("engineering-fixture-subject", (run/"diagnostic_result.json").read_text())
        self.assertFalse(read_json(run/"summary.json")["human_likeness_validated"])
        verify_run(run)

    def test_human_early_export_prevents_cross_task_label_leakage(self):
        path = self.root/"synthetic_only.csv"
        with path.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=SOURCE_COLUMNS)
            writer.writeheader()
            writer.writerows(fixture_records())
        with patch("nmsim.human_reference.SOURCE_CSV_SHA256", file_sha256(path)), redirect_stdout(io.StringIO()), \
                patch("socket.socket", side_effect=AssertionError("network")):
            entry.main(["--task", "human-early", "--csv", str(path), "--out", str(self.root), "--run-id", "early"])
        run = self.root/"runs/early"
        prompt_path = run/"private_human_early_prompts.json"
        private_prompts = read_json(prompt_path)
        self.assertEqual(len(private_prompts), 5)
        for row in private_prompts:
            self.assertNotIn("human_action", row)
            self.assertNotIn("source_origin", row)
        self.assertEqual(prompt_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual((run/"private_human_early_tasks.json").stat().st_mode & 0o777, 0o600)
        private_tasks = read_json(run/"private_human_early_tasks.json")
        # Per-row omission of human_action does not protect the entire bank:
        # the following prompt legitimately includes the earlier human choice.
        for index in range(4):
            self.assertEqual(
                private_tasks[index]["human_action"]["signed_quantities"],
                private_prompts[index+1]["state"]["own_prior_trades"][-1]["signed_quantities"],
            )
        self.assertFalse((run/"human_early_prompts.json").exists())
        catalog_path = run/"human_early_catalog.json"
        catalog = read_json(catalog_path)
        self.assertEqual(catalog["task_ids"], [task["task_id"] for task in private_tasks])
        self.assertEqual(catalog["units"]["joint_decision_tasks"], 5)
        self.assertEqual(catalog["units"]["source_subjects"], 1)
        self.assertEqual(catalog["units"]["stock_opportunities"], 30)
        self.assertEqual(set(catalog), {"schema", "task_bank_hash", "task_protocol", "units",
                                        "task_ids", "acquisition_policy"})
        for forbidden in ("human_action", "source_origin", "own_prior_trades", "signed_quantities",
                          "observed_price_history", "engineering-fixture-subject",
                          "engineering-fixture-session"):
            self.assertNotIn(forbidden, catalog_path.read_text())
        self.assertIn("independent request context", catalog["acquisition_policy"])
        result = read_json(run/"diagnostic_result.json")
        self.assertEqual(result["hold_null_baseline"]["prediction_source"], "synthetic_hold_null_not_Teacher")
        self.assertFalse(result["human_likeness_validated"])
        honest = read_json(run/"summary.json")["honest_n"]
        self.assertEqual(honest["new_teacher_requests"], 0)
        self.assertEqual(honest["new_human_participants"], 0)
        self.assertEqual(honest["historical_main_joint_decisions"], 5)
        self.assertEqual(honest["historical_main_stock_opportunities"], 30)
        self.assertEqual(honest["synthetic_null_predictions"], 5)
        verify_run(run)
