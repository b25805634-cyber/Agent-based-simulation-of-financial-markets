"""Public synthetic test states only; no ignored research data required."""
from copy import deepcopy
import asyncio
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from experiments import rollout_fidelity as entry
from nmsim.information_market import run_market, random_policy
from nmsim.information_artifacts import canonical_hash, file_sha256, read_json, verify_run
from nmsim.information_weight import P8
from nmsim import rollout_probes as probes


def candidates():
    result = run_market(random_policy, agents=8, rounds=3, observation_policy="available_only")
    for row in result["ledger"]:
        for decision in row["decisions"]:
            yield {"source_run_id": "test-market", "cell": "test-cell", "rounds": 3,
                   "quote_rule": "independent", "decision_day": row["decision_day"],
                   "agent_id": decision["agent_id"], "profile_id": decision["profile_id"],
                   "visible_fields": decision["visible_fields"], "account_state": decision["account_state"],
                   "student_prediction": {key: decision[key] for key in ("action_probs", "intensities")},
                   "base_state": {**{field: row["market_effective"][field] for field in P8[:6]}, **decision["account_state"]}}


class ProbePlanTests(unittest.TestCase):
    def test_order_independent_selection_and_replicate_plan(self):
        rows = list(candidates())
        first = probes.build_plan(rows, max_states=12, replicates=3)
        self.assertEqual(first, probes.build_plan(list(reversed(rows)), max_states=12, replicates=3))
        probes.validate_plan(first)
        self.assertEqual(first["planned_logical_requests"], 36)
        self.assertEqual(len({sample["sample_id"] for sample in first["samples"]}), 36)
        self.assertEqual(first["candidate_agent_rounds"], 24)

    def test_prompt_contains_no_unavailable_or_identity_metadata(self):
        plan = probes.build_plan(candidates(), max_states=4, replicates=2)
        for case in plan["cases"]:
            text = case["prompt"]["user"]
            self.assertNotIn("intraday_range_5d_mean", text)
            self.assertNotIn("profile_id", text)
            self.assertNotIn("test-market", text)
            self.assertNotIn("student_prediction", text)
            payload = json.loads(text)
            self.assertEqual(payload["observable_state"], case["visible_fields"])

    def test_legacy_range_proxy_and_corrupted_plan_fail_closed(self):
        row = next(candidates())
        row["visible_fields"]["intraday_range_5d_mean"] = 0.01
        with self.assertRaises(ValueError):
            probes.build_plan([row])
        plan = probes.build_plan(candidates(), max_states=4, replicates=2)
        plan["samples"].reverse()
        plan["plan_hash"] = canonical_hash({k: v for k, v in plan.items() if k != "plan_hash"})
        with self.assertRaises(ValueError):
            probes.validate_plan(plan)

    def test_empty_honest_n_and_fake_not_endpoint(self):
        plan = probes.build_plan(candidates(), max_states=4, replicates=2)
        summary = probes.summarize(plan, [], real_endpoint=True)
        self.assertEqual(summary["unresolved_logical_requests"], 8)
        self.assertEqual(summary["valid_endpoint_responses"], 0)

    def test_intensity_fidelity_counts_variance_and_conditional_error(self):
        plan = probes.build_plan(candidates(), max_states=1, replicates=2)
        rows = [{"schema_version": probes.SAMPLE_SCHEMA, **sample, "source_kind": "openai",
                 "status": "valid", "decision": {"action": "buy", "intensity": intensity}}
                for sample, intensity in zip(plan["samples"], (0.1, 0.9))]
        summary = probes.summarize(plan, rows, real_endpoint=True)
        result = summary["cases"][0]["conditional_intensity"]
        self.assertEqual(result["buy"]["n"], 2)
        self.assertAlmostEqual(result["buy"]["teacher_sample_variance"], 0.32)
        self.assertAlmostEqual(result["buy"]["mean_absolute_error"], 0.4)
        self.assertIsNone(result["sell"]["mean_absolute_error"])
        row = {"schema_version": probes.SAMPLE_SCHEMA, **plan["samples"][0],
               "source_kind": "fake_test_teacher", "status": "valid", "decision": {"action": "hold", "intensity": 0}}
        with self.assertRaises(ValueError):
            probes.summarize(plan, [row], real_endpoint=True)
        summary = probes.summarize(plan, [row], real_endpoint=False)
        self.assertEqual(summary["valid_responses"], 1)
        self.assertEqual(summary["valid_endpoint_responses"], 0)


class ProbeAcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rollout-probe-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root/"plan"
        self.source.mkdir()
        self.plan = probes.build_plan(candidates(), max_states=4, replicates=2)
        path = self.source/"probe_plan.json"
        path.write_text(json.dumps(self.plan))
        manifest = {"run_id": "test-plan", "status": "finished", "managed_run_completed": True,
                    "outputs_complete": True, "results": [{"path": path.name, "kind": "file", "error": None,
                    "exists": True, "inside_run_directory": True, "sha256": file_sha256(path),
                    "size_bytes": path.stat().st_size}]}
        (self.source/"run_manifest.json").write_text(json.dumps(manifest))

    def invoke(self, *extra):
        with redirect_stdout(io.StringIO()), patch("socket.create_connection", side_effect=AssertionError("network")):
            entry.main(["--task", "acquire", "--plan-run", str(self.source), "--out", str(self.root/"out"),
                        "--run-id", "probe-test", *extra])

    def test_fake_acquisition_managed_private_raw_zero_endpoint_n(self):
        with patch.object(entry, "OpenAITeacherProvider", side_effect=AssertionError("real Provider")):
            self.invoke()
        run = self.root/"out/runs/probe-test"
        summary = read_json(run/"fidelity_summary.json")
        self.assertEqual(summary["valid_responses"], 8)
        self.assertEqual(summary["physical_attempts"], 8)
        self.assertEqual(summary["valid_endpoint_responses"], 0)
        self.assertEqual((run/"private_probe_records.jsonl").stat().st_mode & 0o777, 0o600)
        public = (run/"probe_samples.jsonl").read_text()
        self.assertNotIn("fake null control", public)
        self.assertNotIn("reasoning", public)
        verify_run(run)

    def test_dry_run_never_constructs_provider(self):
        with patch.object(entry, "FakeNullTeacher", side_effect=AssertionError("Provider")), \
                patch.object(entry, "OpenAITeacherProvider", side_effect=AssertionError("Provider")):
            self.invoke("--dry-run")
        run = self.root/"out/runs/probe-test"
        self.assertFalse((run/"probe_samples.jsonl").exists())

    def test_live_requires_matching_explicit_count_before_provider(self):
        with patch.object(entry, "OpenAITeacherProvider", side_effect=AssertionError("Provider")):
            with self.assertRaises(ValueError):
                self.invoke("--provider", "openai", "--live", "--confirm-request-count", "7")
        manifest = read_json(self.root/"out/runs/probe-test/run_manifest.json")
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["failure_stage"], "config_validation")

    def test_acquisition_cannot_silently_ignore_selection_overrides(self):
        with self.assertRaises(SystemExit):
            self.invoke("--replicates", "3")
        manifest = read_json(self.root/"out/runs/probe-test/run_manifest.json")
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["honest_n"], 0)

    def test_unexpected_parser_failure_preserves_received_private_response(self):
        with patch.object(entry, "parse_teacher_response", side_effect=RecursionError("test parser")):
            with self.assertRaises(RecursionError):
                self.invoke()
        run = self.root/"out/runs/probe-test"
        private = [json.loads(line) for line in (run/"private_probe_records.jsonl").read_text().splitlines()]
        completions = [row for row in private if row["kind"] == "completion"]
        self.assertEqual(len(completions), 1)
        self.assertIn('"action":"hold"', completions[0]["completion"]["raw_response"])
        self.assertEqual(read_json(run/"run_manifest.json")["status"], "failed")

    def test_callback_failure_drains_other_owned_batch_requests(self):
        completed = []
        class Provider:
            workers = 2
            batch_sizes = []
            async def _one(self, index, system, user, semaphore, before, audit):
                await asyncio.sleep(0 if index == 0 else 0.01)
                return index, "response"
        def completion(index, value):
            completed.append(index)
            if index == 0:
                raise ValueError("callback failure")
        with self.assertRaises(ValueError):
            asyncio.run(entry._complete_batch(Provider(), [("", ""), ("", "")],
                before_attempt=lambda _: None, on_application_attempt=lambda *_: None,
                on_completion=completion))
        self.assertEqual(completed, [0, 1])
