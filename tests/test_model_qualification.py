"""Phase 1.2A model-qualification protocol and offline managed CLI tests.

Every provider used here is in-process.  Any attempt to construct a production
provider or open a socket is treated as a test failure.
"""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import copy
import io
import json
from pathlib import Path
import socket
import stat
import tempfile
import unittest
from unittest import mock

from experiments import model_qualification as qualification


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _single_run(out_root: Path) -> Path:
    manifests = sorted((out_root / "runs").glob("*/run_manifest.json"))
    if len(manifests) != 1:
        raise AssertionError("expected one managed run, found {}".format(len(manifests)))
    return manifests[0].parent


class QualificationProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bundle = qualification.load_protocol_bundle()
        cls.cases = qualification.build_cases(cls.bundle)

    def test_exact_six_by_eight_matrix_has_48_stable_unique_cases(self):
        self.assertEqual(len(self.bundle["protocol"]["persona_ids"]), 6)
        self.assertEqual(len(self.bundle["observations"]["fixtures"]), 8)
        self.assertEqual(len(self.cases), 48)
        self.assertEqual(len({case.case_id for case in self.cases}), 48)
        rebuilt = qualification.build_cases(qualification.load_protocol_bundle())
        self.assertEqual(
            [case.case_id for case in self.cases],
            [case.case_id for case in rebuilt],
        )

    def test_fixture_order_does_not_change_fixture_set_hash(self):
        fixtures = self.bundle["observations"]["fixtures"]
        self.assertEqual(
            qualification.fixture_set_hash(fixtures),
            qualification.fixture_set_hash(list(reversed(fixtures))),
        )

    def test_fixture_content_change_changes_fixture_set_hash(self):
        fixtures = copy.deepcopy(self.bundle["observations"]["fixtures"])
        original = qualification.fixture_set_hash(fixtures)
        fixtures[0]["cash"] += 1.0
        self.assertNotEqual(original, qualification.fixture_set_hash(fixtures))

    def test_rubric_content_change_changes_rubric_hash(self):
        rubric = copy.deepcopy(self.bundle["rubric"])
        original = qualification.stable_json_hash(rubric)
        rubric["not_scored_policy"] += " changed"
        self.assertNotEqual(original, qualification.stable_json_hash(rubric))

    def test_protocol_version_and_frozen_rubric_are_recordable(self):
        self.assertEqual(self.bundle["protocol"]["protocol_version"], "1.0")
        self.assertEqual(self.bundle["rubric"]["rubric_version"], "1.0")
        self.assertTrue(self.bundle["rubric"]["frozen_before_external_provider_calls"])
        for key in ("protocol_hash", "fixture_set_hash", "rubric_hash"):
            self.assertRegex(self.bundle[key], r"^[0-9a-f]{64}$")

    def test_observations_exclude_private_future_and_expected_answer_payloads(self):
        forbidden = {
            "future_prices",
            "market_clearing_formula",
            "other_agent_private_rationale",
            "private_margin_reference_book",
            "expected_answer",
            "evaluation_rubric",
        }
        for fixture in self.bundle["observations"]["fixtures"]:
            with self.subTest(fixture=fixture["fixture_id"]):
                self.assertFalse(forbidden.intersection(fixture))
                self.assertTrue(forbidden.issubset(set(fixture["invisible_fields"])))
                self.assertEqual(
                    fixture["input_hash"], qualification.fixture_input_hash(fixture)
                )

    def test_missing_public_take_never_uses_private_text_as_public(self):
        raw = json.dumps(
            {
                "action": "buy",
                "quantity": 2,
                "limit_price": 100.0,
                "sentiment": 0.5,
                "reasoning": "PRIVATE_TEXT_MUST_STAY_PRIVATE",
            }
        )
        public, private = qualification.evaluate_response(self.cases[0], raw)
        self.assertEqual(public["public_take"], "")
        self.assertTrue(public["diagnostic_flags"]["missing_public_take"])
        self.assertNotIn("PRIVATE_TEXT_MUST_STAY_PRIVATE", json.dumps(public))
        self.assertIn("PRIVATE_TEXT_MUST_STAY_PRIVATE", json.dumps(private))

    def test_engineering_and_behavior_metrics_preserve_raw_distributions(self):
        raw_values = (
            json.dumps(
                {
                    "action": "buy",
                    "quantity": 1,
                    "limit_price": 100.0,
                    "sentiment": -0.5,
                    "public_take": "Buying despite concern.",
                    "reasoning": "private one",
                }
            ),
            json.dumps(
                {
                    "action": "hold",
                    "quantity": "not-an-integer",
                    "limit_price": 100.0,
                    "sentiment": 0.0,
                    "public_take": "Waiting.",
                    "reasoning": "private two",
                }
            ),
        )
        rows = [
            qualification.evaluate_response(case, raw)[0]
            for case, raw in zip(self.cases[:2], raw_values)
        ]
        metrics = qualification.aggregate_results(rows)
        engineering = metrics["engineering"]
        behavior = metrics["behavioral_diagnostics"]
        self.assertEqual(engineering["schema_success_rate"], 1.0)
        self.assertEqual(engineering["parse_success_rate"], 1.0)
        self.assertEqual(engineering["invalid_quantity_count"], 1)
        self.assertEqual(engineering["validation_failure_rate"], 0.5)
        self.assertEqual(behavior["sentiment_action_consistency"], 0.5)
        self.assertIn("raw_action_distributions", behavior)
        self.assertEqual(behavior["raw_action_distributions"]["global"]["buy"], 1)
        self.assertNotEqual(set(behavior), {"score"})


class QualificationManagedCLITests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)

    def test_help_creates_no_run(self):
        out = self.root / "help"
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as raised:
            qualification.main(["--help", "--out", str(out)])
        self.assertEqual(raised.exception.code, 0)
        self.assertFalse(out.exists())

    def test_dry_run_never_constructs_provider_and_records_zero_calls(self):
        out = self.root / "dry"
        stdout = io.StringIO()
        with mock.patch.object(
            qualification,
            "_build_provider",
            side_effect=AssertionError("provider constructed during dry-run"),
        ) as build, mock.patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError("network attempted"),
        ) as network, redirect_stdout(stdout):
            qualification.main(
                ["--provider", "mock", "--dry-run", "--out", str(out)]
            )
        build.assert_not_called()
        network.assert_not_called()
        run_dir = _single_run(out)
        manifest = _read_json(run_dir / "run_manifest.json")
        summary = _read_json(run_dir / "dry_run_summary.json")
        self.assertEqual(summary["case_count"], 48)
        self.assertEqual(summary["persona_count"], 6)
        self.assertEqual(summary["fixture_count"], 8)
        self.assertEqual(summary["provider_calls"], 0)
        self.assertFalse(summary["network_access"])
        self.assertEqual(manifest["completion"]["provider_calls"]["attempted"], 0)
        self.assertFalse(manifest["llm"]["runtime"]["network_access"])
        self.assertEqual(manifest["managed_context"]["run_kind"], "model_qualification")

    def test_external_provider_is_managed_rejection_before_provider_or_network(self):
        out = self.root / "external-rejected"
        stderr = io.StringIO()
        with mock.patch.object(
            qualification,
            "_build_provider",
            side_effect=AssertionError("external provider constructed"),
        ) as build, mock.patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError("network attempted"),
        ) as network, redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            qualification.main(["--provider", "openai", "--out", str(out)])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("forbids external provider", stderr.getvalue())
        build.assert_not_called()
        network.assert_not_called()
        manifest = _read_json(_single_run(out) / "run_manifest.json")
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["failure_stage"], "provider_setup")
        self.assertEqual(manifest["completion"]["provider_calls"]["attempted"], 0)
        self.assertFalse(manifest["llm"]["runtime"]["network_access"])

    def test_mock_qualification_completes_48_cases_without_simulation(self):
        out = self.root / "mock"
        with mock.patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError("network attempted"),
        ) as network, redirect_stdout(io.StringIO()):
            qualification.main(["--provider", "mock", "--out", str(out)])
        network.assert_not_called()
        run_dir = _single_run(out)
        manifest = _read_json(run_dir / "run_manifest.json")
        summary = _read_json(run_dir / "qualification_summary.json")
        cases = [
            json.loads(line)
            for line in (run_dir / "case_results.jsonl").read_text().splitlines()
        ]
        self.assertEqual(manifest["status"], "finished")
        self.assertEqual(manifest["managed_context"]["run_kind"], "model_qualification")
        self.assertEqual(manifest["qualification"]["qualification_cases"]["completed"], 48)
        self.assertEqual(manifest["qualification"]["honest_n_cases"], 48)
        self.assertEqual(manifest["honest_n_runs"], 0)
        self.assertEqual(manifest["completion"]["simulation_runs"]["planned"], 0)
        self.assertEqual(manifest["completion"]["simulation_runs"]["completed"], 0)
        self.assertEqual(manifest["completion"]["rounds"]["planned"], 0)
        self.assertEqual(manifest["completion"]["llm_logical_requests"]["completed"], 48)
        self.assertEqual(manifest["completion"]["agent_decisions"]["completed"], 48)
        self.assertEqual(manifest["completion"]["provider_calls"]["succeeded"], 48)
        self.assertEqual(manifest["completion"]["response_sources"]["provider"], 48)
        self.assertFalse(manifest["llm"]["runtime"]["network_access"])
        self.assertEqual(summary["honest_n_cases"], 48)
        self.assertEqual(summary["honest_n_runs"], 0)
        self.assertEqual(len(cases), 48)
        self.assertFalse((run_dir / "price_path.csv").exists())
        self.assertFalse((run_dir / "sim_overview.png").exists())

    def test_public_private_outputs_are_separated_and_private_mode_is_0600(self):
        out = self.root / "privacy"
        with redirect_stdout(io.StringIO()):
            qualification.main(["--provider", "fake_test_provider", "--out", str(out)])
        run_dir = _single_run(out)
        public_paths = [
            run_dir / "run_manifest.json",
            run_dir / "events.jsonl",
            run_dir / "case_results.jsonl",
            run_dir / "qualification_summary.json",
        ]
        private_path = run_dir / "private_case_records.jsonl"
        private_text = private_path.read_text(encoding="utf-8")
        for path in public_paths:
            public_text = path.read_text(encoding="utf-8")
            self.assertNotIn("private deterministic test-double explanation", public_text)
            self.assertNotIn('"raw_response":', public_text)
            self.assertNotIn('"system_prompt":', public_text)
            self.assertNotIn('"user_prompt":', public_text)
        self.assertIn("private deterministic test-double explanation", private_text)
        self.assertIn("raw_response", private_text)
        self.assertEqual(stat.S_IMODE(private_path.stat().st_mode), 0o600)
        self.assertEqual(
            stat.S_IMODE((run_dir / "private_events.jsonl").stat().st_mode), 0o600
        )

    def test_public_summary_reports_distributions_not_only_total_score(self):
        out = self.root / "distribution"
        with redirect_stdout(io.StringIO()):
            qualification.main(["--provider", "mock", "--out", str(out)])
        summary = _read_json(_single_run(out) / "qualification_summary.json")
        behavior = summary["metrics"]["behavioral_diagnostics"]
        raw = behavior["raw_action_distributions"]
        self.assertEqual(len(raw["by_persona"]), 6)
        self.assertEqual(len(raw["by_fixture"]), 8)
        self.assertEqual(len(raw["persona_action_vectors"]), 6)
        self.assertTrue(all(len(vector) == 8 for vector in raw["persona_action_vectors"].values()))
        self.assertIn("persona_distinctiveness", behavior)
        self.assertIn("same_model_persona_collapse", behavior)


if __name__ == "__main__":
    unittest.main()
