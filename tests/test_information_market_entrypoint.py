"""Managed boundary tests using an explicitly synthetic public data source."""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from experiments import information_market as entry
from nmsim import entrypoints
from nmsim.information_artifacts import file_sha256, read_json, verify_run
from nmsim.information_reporting import render_html, render_markdown
from tests.test_information_student import public_records


class InformationMarketEntrypointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="information-market-entrypoint-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base/"synthetic-source"
        self.source.mkdir()
        rows = public_records(groups=12)
        samples = self.source/"information_weight_scale_samples.jsonl"
        samples.write_text("".join(json.dumps(row)+"\n" for row in rows), encoding="utf-8")
        manifest = {"run_id": "synthetic-test-only", "status": "finished",
                    "outputs_complete": True, "managed_run_completed": True,
                    "evidence_kind": "synthetic_fixture_not_endpoint_or_human",
                    "results": [{"path": samples.name, "inside_run_directory": True, "kind": "file",
                                 "exists": True, "error": None, "size_bytes": samples.stat().st_size,
                                 "sha256": file_sha256(samples)}]}
        (self.source/"run_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def invoke(self, *args):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), \
                patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden")), \
                patch("nmsim.llm.build_llm", side_effect=AssertionError("Provider forbidden")):
            entry.main(list(args))

    def train(self, run_id="train-test"):
        out = self.base/"training"
        self.invoke("--task", "train", "--source-run", str(self.source), "--epochs", "2",
                    "--hidden-dim", "4", "--backend", "python", "--out", str(out), "--run-id", run_id)
        return out/"runs"/run_id

    def test_registry_and_help_create_no_run(self):
        self.assertIn("experiments.information_market", {x.entrypoint_id for x in entrypoints.ENTRYPOINTS})
        out = self.base/"help"
        with self.assertRaises(SystemExit) as result:
            self.invoke("--help", "--out", str(out))
        self.assertEqual(result.exception.code, 0)
        self.assertFalse(out.exists())

    def test_dry_run_no_training_no_simulation_no_network(self):
        out = self.base/"dry"
        with patch("nmsim.information_student.train_models", side_effect=AssertionError("no training")), \
                patch("nmsim.information_market.run_market", side_effect=AssertionError("no market")):
            self.invoke("--source-run", str(self.source), "--dry-run", "--out", str(out), "--run-id", "dry")
        run = out/"runs/dry"
        manifest = read_json(run/"run_manifest.json")
        self.assertEqual(manifest["status"], "finished")
        self.assertFalse(manifest["llm"]["runtime"]["network_access"])
        self.assertEqual(read_json(run/"summary.json")["honest_n"]["trained_models"], 0)
        self.assertFalse((run/"student_study.json").exists())

    def test_real_library_training_and_market_integration(self):
        before = verify_run(self.source)
        trained = self.train()
        source_receipt = verify_run(trained)
        study = read_json(trained/"student_study.json")
        self.assertEqual(study["accounting"]["human_participants"], 0)
        self.assertEqual(before["snapshot_hash"], verify_run(self.source)["snapshot_hash"])
        out = self.base/"simulation"
        self.invoke("--task", "simulate", "--model-run", str(trained),
                    "--model-manifest-sha256", source_receipt["manifest_sha256"],
                    "--agents", "8", "--rounds", "3", "--seeds", "1",
                    "--out", str(out), "--run-id", "market-test")
        run = out/"runs/market-test"
        summary = read_json(run/"summary.json")
        self.assertEqual(summary["honest_n"]["market_runs"], 6)
        self.assertEqual(summary["honest_n"]["agent_decisions"], 144)
        self.assertEqual(summary["honest_n"]["rounds"], 18)
        self.assertTrue(all(cell["conservation_passed"] for cell in summary["markets"]))
        self.assertEqual(len({cell["world_hash"] for cell in summary["markets"]}), 1)
        self.assertTrue(all(cell["train_support_diagnostic"]["evaluated_agent_rounds"] == 24
                            for cell in summary["markets"]))
        manifest = read_json(run/"run_manifest.json")
        self.assertEqual(manifest["completion"]["simulation_runs"]["completed"], 6)
        self.assertEqual(manifest["completion"]["agent_decisions"]["completed"], 144)
        self.assertEqual(manifest["completion"]["llm_logical_requests"]["attempted"], 0)
        verify_run(run)

    def test_source_corruption_fails_before_training_with_honest_n_zero(self):
        samples = self.source/"information_weight_scale_samples.jsonl"
        samples.write_text(samples.read_text()+"{}\n")
        out = self.base/"bad"
        with patch("nmsim.information_student.train_models", side_effect=AssertionError("no training")):
            with self.assertRaises(ValueError):
                self.invoke("--source-run", str(self.source), "--out", str(out), "--run-id", "bad")
        manifest = read_json(out/"runs/bad/run_manifest.json")
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["failure_stage"], "config_validation")
        self.assertEqual(manifest["honest_n"], 0)

    def test_human_task_export_is_managed_and_pending_without_people(self):
        out = self.base/"human"
        self.invoke("--task", "benchmark", "--out", str(out), "--run-id", "human-tasks")
        run = out/"runs/human-tasks"
        tasks = read_json(run/"human_tasks.json")
        self.assertEqual(len(tasks), 24)
        self.assertEqual(set(tasks[0]["field_semantics"]),
                         set(tasks[0]["visible_fields"]) | set(tasks[0]["account_state"]))
        scores = read_json(run/"human_comparison.json")
        self.assertEqual(scores["status"], "pending_human_responses")
        self.assertFalse(scores["human_likeness_validated"])
        self.assertEqual(scores["human_participants"], 0)
        self.assertEqual(read_json(run/"summary.json")["honest_n"]["market_runs"], 0)
        verify_run(run)

    def test_available_only_task_bank_and_cli_explicit_contract(self):
        out = self.base/"available"
        self.invoke("--task", "benchmark", "--observation-policy", "available_only",
                    "--out", str(out), "--run-id", "available-tasks")
        tasks = read_json(out/"runs/available-tasks/human_tasks.json")
        for task in tasks:
            self.assertEqual(task["schema"], "human-information-market-task/1.1.0")
            self.assertNotIn("intraday_range_5d_mean", task["visible_fields"])
            self.assertNotIn("intraday_range_5d_mean", task["field_semantics"])
        with self.assertRaises(SystemExit):
            self.invoke("--task", "train", "--source-run", str(self.source),
                        "--observation-policy", "available_only", "--out", str(self.base/"bad-train"))

    def test_existing_output_never_overwritten(self):
        run = self.train()
        before = file_sha256(run/"run_manifest.json")
        with self.assertRaises(SystemExit):
            self.train()
        self.assertEqual(file_sha256(run/"run_manifest.json"), before)

    def test_model_encoder_order_and_payload_hash_rejected(self):
        trained = self.train()
        receipt = verify_run(trained)
        for change in ("encoder", "hash"):
            study = read_json(trained/"student_study.json")
            if change == "encoder":
                study["encoder"] = {"wrong_order": True}
            else:
                study["provenance"]["model_serialization_hashes"]["prior"] = "0"*64
            with patch.object(entry, "read_json", return_value=study), self.assertRaises(ValueError):
                entry._load_study(trained, receipt)

    def test_input_execution_metadata_does_not_pollute_scientific_identity(self):
        args = entry.build_parser().parse_args(["--source-run", str(self.source)])
        first = verify_run(self.source)
        second = deepcopy(first)
        second["manifest_sha256"] = "a" * 64
        second["snapshot_hash"] = "b" * 64
        a, b = entry.build_identities(args, first), entry.build_identities(args, second)
        self.assertEqual(a["scientific_config_hash"], b["scientific_config_hash"])
        self.assertNotEqual(a["execution_config_hash"], b["execution_config_hash"])
        self.assertNotEqual(a["full_effective_config_hash"], b["full_effective_config_hash"])

    def test_reports_discard_non_public_extra_fields_and_escape_html(self):
        summary = {"run_id": "<script>alert(1)</script>", "task": "train", "honest_n": {},
                   "private_reasoning": "secret-reasoning", "api_key": "secret-key", "raw_endpoint": "secret-endpoint"}
        rendered = render_html(summary)+render_markdown(summary)
        self.assertNotIn("secret-reasoning", rendered)
        self.assertNotIn("secret-key", rendered)
        self.assertNotIn("secret-endpoint", rendered)
        self.assertNotIn("<script>", render_html(summary))
