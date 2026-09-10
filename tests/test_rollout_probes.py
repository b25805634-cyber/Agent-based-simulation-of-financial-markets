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
from nmsim import information_market as market
from nmsim import information_student as student
from nmsim import intensity_distribution as sizing
from tests.test_information_student import public_records


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

    def test_acquisition_rejects_distribution_and_source_pin_overrides(self):
        for flag, value in (("--distribution-run", "sizing"),
                            ("--distribution-manifest-sha256", "a"*64),
                            ("--market-manifest-sha256", "a"*64),
                            ("--model-manifest-sha256", "a"*64)):
            with self.subTest(flag=flag), self.assertRaises(ValueError):
                entry._validate(entry.parser().parse_args([
                    "--task", "acquire", "--plan-run", str(self.source), flag, value]))

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


class SizingRolloutPlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        records = public_records(groups=36)
        for record in records:
            if record["decision"]["action"] != "hold":
                record["decision"]["intensity"] = 1.0
        cls.study = student.train_models(records, epochs=2, hidden_dim=4, backend="python")
        cls.distribution = sizing.fit_intensity_distributions(records, cls.study, epochs=2, backend="python")
        predictor = sizing.make_distribution_predictor(cls.distribution, cls.study)
        real_account = market.Account
        def initial_account(agent_id, cash, shares):
            return real_account(agent_id, 10000, 1)
        with patch.object(market, "Account", side_effect=initial_account), \
             patch.object(market, "quote_price", return_value=10000), \
             patch("socket.create_connection", side_effect=AssertionError("network")):
            cls.result = market.run_market(predictor, agents=20, rounds=2, seed=11,
                observation_policy="available_only", sizing_policy="distribution_sampled")
            cls.mean_result = market.run_market(predictor, agents=20, rounds=2, seed=11,
                observation_policy="available_only", sizing_policy="distribution_mean")

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="sizing-rollout-plan-test-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.model, self.model_receipt = self.write_run("model", {"student_study.json": self.study})
        self.sizing, self.sizing_receipt = self.write_run("sizing", {"intensity_study.json": self.distribution})

    def write_run(self, name, artifacts):
        root = self.root/name
        root.mkdir()
        results = []
        for filename, value in artifacts.items():
            path = root/filename
            path.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")
            results.append({"path": filename, "inside_run_directory": True, "kind": "file", "exists": True,
                            "error": None, "size_bytes": path.stat().st_size, "sha256": file_sha256(path)})
        (root/"run_manifest.json").write_text(json.dumps({"run_id": name, "status": "finished",
            "managed_run_completed": True, "outputs_complete": True,
            "evidence_kind": "synthetic_fixture_not_endpoint_or_human", "results": results}), encoding="utf-8")
        return root, verify_run(root)

    def write_market(self, name="market", result=None):
        metadata = deepcopy(self.result if result is None else result)
        ledger = metadata.pop("ledger")
        metadata.update(model_input_manifest_sha256=self.model_receipt["manifest_sha256"],
                        ledger_artifact="cell_rounds.jsonl")
        summary = {"markets": [{"cell": "cell", "policy": "selected", "quote_rule": "independent"}]}
        if metadata["schema"] == "information-market/1.2.0":
            metadata.setdefault("distribution_input_manifest_sha256", self.sizing_receipt["manifest_sha256"])
            metadata.setdefault("sizing_candidate", "selected")
            summary.update(sizing_candidate="selected", sizing_policy=metadata["config"]["sizing_policy"])
        return self.write_run(name, {"summary.json": summary, "cell.json": metadata,
                                    "cell_rounds.jsonl": "".join(json.dumps(row)+"\n" for row in ledger)})

    def collect(self, root, receipt, **kwargs):
        return list(entry._candidates(root, receipt, self.model_receipt, self.study, **kwargs))

    def test_full_liquidation_states_enter_unchanged_plan_without_sizing_prompt_leakage(self):
        self.assertGreater(self.result["sizing_audit"]["counts"].get("closed_position_events", 0), 0)
        root, receipt = self.write_market()
        rows = self.collect(root, receipt, distribution_receipt=self.sizing_receipt, distribution_study=self.distribution)
        empty = [row for row in rows if row["decision_day"] == 1 and row["account_state"]["position_fraction"] == 0]
        self.assertTrue(empty)
        plan = probes.build_plan(rows, max_states=len(rows), replicates=2)
        probes.validate_plan(plan)
        self.assertTrue(any(case["account_state"]["position_fraction"] == 0 for case in plan["cases"]))
        self.assertEqual(plan["schema_version"], "rollout-fidelity-plan/1.0")
        for case in plan["cases"]:
            payload = json.loads(case["prompt"]["user"])
            self.assertEqual(payload["observable_state"], case["visible_fields"])
            self.assertEqual(payload["account_state"], case["account_state"])
            for forbidden in ("intensity_distributions", "sizing_candidate", "sizing_policy", "intensity_means", "profile_id"):
                self.assertNotIn(forbidden, case["prompt"]["user"])
        with redirect_stdout(io.StringIO()), patch.object(entry, "OpenAITeacherProvider", side_effect=AssertionError("Provider")), \
             patch.object(entry, "FakeNullTeacher", side_effect=AssertionError("Provider")), \
             patch("socket.create_connection", side_effect=AssertionError("network")):
            entry.main(["--task", "plan", "--market-run", str(root), "--model-run", str(self.model),
                        "--distribution-run", str(self.sizing),
                        "--distribution-manifest-sha256", self.sizing_receipt["manifest_sha256"],
                        "--max-states", str(len(rows)), "--replicates", "2",
                        "--out", str(self.root/"plans"), "--run-id", "synthetic-plan"])
        planned = self.root/"plans/runs/synthetic-plan"
        self.assertEqual(read_json(planned/"probe_plan.json"), plan)
        identities = read_json(planned/"identities.json")
        self.assertEqual(identities["scientific_config"]["distribution_study_semantic_hash"], self.distribution["study_semantic_hash"])
        self.assertEqual(identities["execution_config"]["inputs"]["distribution"], self.sizing_receipt["manifest_sha256"])
        self.assertEqual(read_json(planned/"source_receipts.json")["distribution"],
                         {**self.sizing_receipt, "manifest_pin_supplied": True})
        verify_run(planned)

    def test_distribution_mean_rollout_is_checked_without_a_sampled_draw(self):
        root, receipt = self.write_market(result=self.mean_result)
        rows = self.collect(root, receipt, distribution_receipt=self.sizing_receipt, distribution_study=self.distribution)
        self.assertEqual(len(rows), 40)
        result = deepcopy(self.mean_result)
        decision = next(row for row in result["ledger"][0]["decisions"] if row["action"] != "hold")
        decision["sizing_uniform"] = 0.5
        root, receipt = self.write_market("bad-mean", result)
        with self.assertRaisesRegex(ValueError, "must not claim a sampled uniform"):
            self.collect(root, receipt, distribution_receipt=self.sizing_receipt, distribution_study=self.distribution)

    def test_sizing_sources_and_original_model_must_match(self):
        root, receipt = self.write_market()
        with self.assertRaisesRegex(ValueError, "verified --distribution-run"):
            self.collect(root, receipt)
        wrong_receipt = {**self.sizing_receipt, "manifest_sha256": "0"*64}
        with self.assertRaisesRegex(ValueError, "sizing input manifests differ"):
            self.collect(root, receipt, distribution_receipt=wrong_receipt, distribution_study=self.distribution)
        wrong_model_receipt = {**self.model_receipt, "manifest_sha256": "0"*64}
        with self.assertRaisesRegex(ValueError, "model input manifests differ"):
            list(entry._candidates(root, receipt, wrong_model_receipt, self.study,
                                  self.sizing_receipt, self.distribution))
        wrong_study = deepcopy(self.study)
        wrong_study["provenance"]["normalized_records_semantic_hash"] = "0"*64
        with self.assertRaisesRegex(ValueError, "another original Student"):
            list(entry._candidates(root, receipt, self.model_receipt, wrong_study,
                                  self.sizing_receipt, self.distribution))

    def test_recorded_sizing_and_original_prediction_tampering_is_rejected(self):
        for index, field in enumerate(("action_probs", "intensities", "intensity_distributions", "intensity_means",
                                      "sizing_policy", "sizing_uniform", "intensity", "sizing_candidate")):
            result = deepcopy(self.result)
            decision = next(row for row in result["ledger"][0]["decisions"] if row["action"] != "hold")
            if field == "action_probs":
                decision[field] = [1.0, 0.0, 0.0]
            elif field in ("intensities", "intensity_means"):
                decision[field] = [0.0, 0.0]
            elif field == "intensity_distributions":
                decision[field]["sell"] = {"support": [0.5], "probabilities": [1.0]}
            elif field == "sizing_policy":
                decision[field] = "legacy_mean"
            elif field in ("sizing_uniform", "intensity"):
                decision[field] = -1.0
            else:
                result[field] = "empirical"
            root, receipt = self.write_market(f"bad-{index}", result)
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.collect(root, receipt, distribution_receipt=self.sizing_receipt, distribution_study=self.distribution)

    def test_legacy_11_plan_is_unchanged_and_rejects_unused_distribution(self):
        predictor = student.make_predictor(self.study["models"][self.study["selected_model"]])
        result = market.run_market(predictor, agents=8, rounds=2, observation_policy="available_only")
        root, receipt = self.write_market(result=result)
        rows = self.collect(root, receipt)
        expected = []
        for row in result["ledger"]:
            for decision in row["decisions"]:
                expected.append({"source_run_id": receipt["run_id"], "cell": "cell", "agent_id": decision["agent_id"],
                    "decision_day": row["decision_day"], "rounds": 2, "quote_rule": "independent",
                    "profile_id": decision["profile_id"], "visible_fields": decision["visible_fields"],
                    "account_state": decision["account_state"],
                    "student_prediction": predictor(decision["visible_fields"], decision["account_state"]),
                    "base_state": {**{key: row["market_effective"][key] for key in P8[:6]}, **decision["account_state"]}})
        self.assertEqual(probes.build_plan(rows), probes.build_plan(expected))
        with self.assertRaisesRegex(ValueError, "unused sizing input"):
            self.collect(root, receipt, distribution_receipt=self.sizing_receipt, distribution_study=self.distribution)

    def test_distribution_pin_cannot_be_unused(self):
        with self.assertRaisesRegex(ValueError, "requires --distribution-run"):
            entry._validate(entry.parser().parse_args(["--market-run", "market", "--model-run", "model",
                "--distribution-manifest-sha256", "a"*64]))
