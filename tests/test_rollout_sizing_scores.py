"""Synthetic offline sizing scores; no ignored results or endpoint requests."""
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import io
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from experiments import rollout_sizing_scores as entry
from nmsim import information_student as student
from nmsim import intensity_distribution as sizing
from nmsim import rollout_probes as probes
from nmsim import rollout_sizing_scores as scores
from nmsim.information_artifacts import canonical_hash, read_json, file_sha256, verify_run
from nmsim.information_market import run_market
from nmsim.information_weight import P8
from tests.test_intensity_distribution import fixture


def make_plan(original):
    predictor = student.make_predictor(original["models"][original["selected_model"]])
    market = run_market(predictor, agents=8, rounds=3, observation_policy="available_only")
    candidates = []
    for row in market["ledger"]:
        for decision in row["decisions"]:
            candidates.append({"source_run_id": "synthetic-mean-market", "cell": "synthetic-cell", "rounds": 3,
                "quote_rule": "independent", "decision_day": row["decision_day"], "agent_id": decision["agent_id"],
                "profile_id": decision["profile_id"], "visible_fields": decision["visible_fields"],
                "account_state": decision["account_state"],
                "student_prediction": {key: decision[key] for key in ("action_probs", "intensities")},
                "base_state": {**{key: row["market_effective"][key] for key in P8[:6]}, **decision["account_state"]}})
    return probes.build_plan(candidates, max_states=3, replicates=3)


def make_rows(plan, *, complete=False, hold_only=False):
    decisions = ((('sell', 0.5), ('sell', 1.0), ('hold', 0)),
                 (('sell', 1.0), ('hold', 0), None),
                 (('buy', 0.25), ('buy', 0.75), None))
    rows = []
    for sample in plan["samples"]:
        if not complete and sample["case_index"] == 2 and sample["replicate"] == 2:
            continue
        pair = ('hold', 0) if hold_only else decisions[sample["case_index"]][sample["replicate"]]
        row = {"schema_version": probes.SAMPLE_SCHEMA,
               **{key: sample[key] for key in ("sample_id", "case_id", "replicate", "request_index")},
               "source_kind": "fake_test_teacher", "status": "valid" if pair is not None else "failed",
               "decision": {"action": pair[0], "intensity": pair[1]} if pair is not None else None,
               "failure_code": None if pair is not None else "provider_exception_or_shape",
               "raw_response_sha256": canonical_hash([sample["sample_id"], pair]) if pair is not None else None,
               "application_attempt_count": 1, "reported_model": "fake-null-control" if pair is not None else None,
               "finish_reason": "stop" if pair is not None else None, "input_tokens": 0, "output_tokens": 0}
        rows.append(row)
    return rows


def register_fixture(root, artifacts, *, run_kind, status="finished"):
    root.mkdir()
    results = []
    for name, value in artifacts.items():
        path = root/name
        path.write_text(value if isinstance(value, str) else json.dumps(value, allow_nan=False), encoding="utf-8")
        if name.startswith("private_"):
            path.chmod(0o600)
        results.append({"path": name, "inside_run_directory": True, "kind": "file", "exists": True,
                        "error": None, "sha256": file_sha256(path), "size_bytes": path.stat().st_size})
    (root/"run_manifest.json").write_text(json.dumps({"run_id": "synthetic-test-only-"+root.name,
        "status": status, "managed_run_completed": status == "finished", "outputs_complete": status == "finished",
        "managed_context": {"run_kind": run_kind}, "evidence_kind": "synthetic_fixture_not_endpoint_or_human",
        "results": results}), encoding="utf-8")


class RolloutSizingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.records = fixture()
        cls.original = student.train_models(cls.records, epochs=2, hidden_dim=4, backend="python")
        cls.distribution = sizing.fit_intensity_distributions(cls.records, cls.original, epochs=3, backend="python")
        cls.plan = make_plan(cls.original)
        cls.rows = make_rows(cls.plan)

    def score(self, rows=None, plan=None, original=None, distribution=None):
        return scores.score_rollout_sizing(plan if plan is not None else self.plan,
            rows if rows is not None else self.rows, original if original is not None else self.original,
            distribution if distribution is not None else self.distribution, source_kind="fake_test_teacher")

    def test_proper_scores_and_two_weightings_match_explicit_arithmetic(self):
        result = self.score()
        for candidate in scores.CANDIDATES:
            for side in scores.SIDES:
                cells = [case["scores"][candidate][side] for case in result["cases"]]
                active = [row for row in cells if row["n"]]
                expected_n = sum(row["n"] for row in active)
                pooled = result["aggregate"][candidate]["row_weighted"][side]
                equal = result["aggregate"][candidate]["equal_case"][side]
                self.assertEqual(pooled["scored_responses"], expected_n)
                self.assertEqual(equal["contributing_cases"], len(active))
                for metric in scores.SCORE_NAMES:
                    self.assertAlmostEqual(pooled[metric], sum(row[metric] * row["n"] for row in active) / expected_n)
                    self.assertAlmostEqual(equal[metric], sum(row[metric] for row in active) / len(active))
        first_case = result["cases"][0]
        old = first_case["scores"]["legacy_mean"]["sell"]
        expected_mae = (abs(old["predicted_mean"]-0.5) + abs(old["predicted_mean"]-1)) / 2
        self.assertAlmostEqual(old["mean_absolute_error"], expected_mae)
        self.assertAlmostEqual(old["crps"], expected_mae)
        self.assertEqual(old["endpoint1_brier"], 0.5)
        self.assertNotEqual(result["aggregate"]["legacy_mean"]["row_weighted"]["sell"]["crps"],
                            result["aggregate"]["legacy_mean"]["equal_case"]["sell"]["crps"])

    def test_failure_missing_and_hold_counts_not_imputed(self):
        result = self.score()
        self.assertEqual(result["honest_n"]["planned_logical_requests"], 9)
        self.assertEqual(result["honest_n"]["resolved_logical_requests"], 8)
        self.assertEqual(result["honest_n"]["valid_responses"], 7)
        self.assertEqual(result["honest_n"]["failed_responses"], 1)
        self.assertEqual(result["honest_n"]["missing_logical_requests"], 1)
        self.assertEqual(result["honest_n"]["valid_action_counts"], {"buy": 2, "hold": 2, "sell": 3})
        self.assertEqual(result["honest_n"]["source_valid_endpoint_responses"], 0)
        self.assertEqual(result["cases"][2]["missing_responses"], 1)
        self.assertEqual(result["cases"][1]["failed_responses"], 1)
        for candidate in scores.CANDIDATES:
            self.assertIsNone(result["cases"][0]["scores"][candidate]["buy"]["crps"])
            self.assertEqual(result["aggregate"][candidate]["row_weighted"]["all_sides"]["scored_responses"], 5)
        self.assertIn("not unprobed trajectories", result["descriptor"]["trajectory_scope"])

    def test_no_observations_returns_null_not_zero_scores(self):
        for rows in ([], make_rows(self.plan, complete=True, hold_only=True)):
            result = self.score(rows=rows)
            for candidate in scores.CANDIDATES:
                for method in ("equal_case", "row_weighted"):
                    for side in (*scores.SIDES, "all_sides"):
                        values = result["aggregate"][candidate][method][side]
                        self.assertEqual(values["denominator"], 0)
                        self.assertIsNone(values["crps"])

    def test_equal_case_all_sides_combines_each_cases_observed_actions_first(self):
        changed = make_rows(self.plan, complete=True)
        for row in changed:
            if row["case_id"] == self.plan["cases"][0]["case_id"] and row["replicate"] == 2:
                row["decision"] = {"action": "buy", "intensity": 0.2}
        result = self.score(rows=changed)
        for candidate in scores.CANDIDATES:
            case_means = []
            for case in result["cases"]:
                side_metrics = [case["scores"][candidate][side] for side in scores.SIDES]
                count = sum(value["n"] for value in side_metrics)
                if count:
                    case_means.append(sum(value["crps"] * value["n"] for value in side_metrics if value["n"]) / count)
            self.assertAlmostEqual(result["aggregate"][candidate]["equal_case"]["all_sides"]["crps"], sum(case_means)/len(case_means))

    def test_fixed_selected_candidate_is_not_reselected_or_refitted(self):
        original_before, distribution_before = deepcopy(self.original), deepcopy(self.distribution)
        with patch.object(student, "train_models", side_effect=AssertionError("no fitting")), \
             patch.object(sizing, "fit_intensity_distributions", side_effect=AssertionError("no fitting")):
            result = self.score()
        self.assertFalse(result["selection_performed"])
        for case in result["cases"]:
            for side in scores.SIDES:
                selected = self.distribution["selection"]["selected_by_action"][side]
                self.assertEqual(case["scores"]["selected"][side], case["scores"][selected][side])
        self.assertEqual(self.original, original_before)
        self.assertEqual(self.distribution, distribution_before)

    def test_exact_endpoint_probability_and_off_support_continuous_score(self):
        result = self.score()
        for case in result["cases"]:
            for candidate in scores.CANDIDATES:
                for side in scores.SIDES:
                    metric = case["scores"][candidate][side]
                    law = case["predictions"][candidate][side]
                    p1 = sum(probability for atom, probability in zip(law["support"], law["probabilities"]) if atom == 1)
                    self.assertEqual(metric["predicted_endpoint1_probability"], p1)
                    if metric["n"]:
                        self.assertTrue(all(math.isfinite(metric[name]) for name in scores.SCORE_NAMES))
        changed = deepcopy(self.rows)
        changed[0]["decision"] = {"action": "sell", "intensity": 0.33333}
        altered = self.score(rows=changed)
        self.assertGreater(altered["cases"][0]["scores"]["conditional_softmax"]["sell"]["off_support_observations"], 0)
        self.assertEqual(altered["cases"][0]["predictions"], result["cases"][0]["predictions"])

    def test_source_kind_roster_and_failed_row_contradictions_rejected(self):
        mutations = (lambda row: row.update(source_kind="openai"),
                     lambda row: row.update(sample_id="f"*64),
                     lambda row: row.update(replicate=55),
                     lambda row: row.update(request_index=True),
                     lambda row: row.update(failure_code="teacher_response_invalid"),
                     lambda row: row.update(status="failed", failure_code="provider_exception_or_shape"),
                     lambda row: row.update(status="failed", decision=None, failure_code="private text"),
                     lambda row: row.update(application_attempt_count=0),
                     lambda row: row.update(decision={"action":"hold", "intensity":0.1}),
                     lambda row: row.update(decision={"action":"sell", "intensity":float("nan")}),
                     lambda row: row.update(finish_reason="length"))
        for mutate in mutations:
            changed = deepcopy(self.rows)
            mutate(changed[0])
            with self.subTest(mutation=mutate), self.assertRaises(ValueError):
                self.score(rows=changed)
        with self.assertRaises(ValueError):
            self.score(rows=self.rows+[self.rows[0]])

    def test_no_private_fields_are_accepted_or_copied(self):
        for name in ("private_rationale", "raw_response", "api_key"):
            changed = deepcopy(self.rows)
            changed[0][name] = "sensitive-marker"
            with self.subTest(field=name), self.assertRaises(ValueError):
                self.score(rows=changed)
        changed = deepcopy(self.rows)
        changed[0]["decision"]["reasoning"] = "sensitive-marker"
        with self.assertRaises(ValueError):
            self.score(rows=changed)
        result = json.dumps(self.score(), allow_nan=False)
        self.assertNotIn('"prompt"', result)
        self.assertNotIn('"raw_response"', result)

    def test_wrong_bound_model_or_case_prediction_and_plan_hash_rejected(self):
        changed = deepcopy(self.plan)
        changed["cases"][0]["student_prediction"]["intensities"][0] += 0.01
        changed["plan_hash"] = canonical_hash({k:v for k,v in changed.items() if k != "plan_hash"})
        with self.assertRaisesRegex(ValueError, "bound original Student"):
            self.score(plan=changed)
        changed["samples"].reverse()
        with self.assertRaises(ValueError):
            self.score(plan=changed)
        changed_original = deepcopy(self.original)
        changed_original["provenance"]["normalized_records_semantic_hash"] = "f"*64
        with self.assertRaisesRegex(ValueError, "another original Student"):
            self.score(original=changed_original)

    def test_no_original_test_payload_or_evaluation_is_accessed(self):
        class Forbidden(dict):
            def __getitem__(self, _key):
                raise AssertionError("original test/evaluation accessed")
            def get(self, _key, _default=None):
                raise AssertionError("original test/evaluation accessed")
        changed = deepcopy(self.original)
        changed["evaluation"] = Forbidden()
        result = self.score(original=changed)
        self.assertEqual(result["honest_n"]["old_test_predictions"], 0)
        self.assertEqual(result, self.score())

    def test_explicit_openai_source_is_not_confused_with_fake_fixture(self):
        # Synthetic labelled responses exercise schema handling only. No API is
        # called; these in-memory values are not a saved real-endpoint run.
        rows = deepcopy(self.rows)
        for row in rows:
            row["source_kind"] = "openai"
            if row["status"] == "valid":
                row["reported_model"] = "HiggsAI"
        result = scores.score_rollout_sizing(self.plan, rows, self.original, self.distribution, source_kind="openai")
        self.assertEqual(result["honest_n"]["source_valid_endpoint_responses"], 7)
        self.assertEqual(result["honest_n"]["new_teacher_requests"], 0)
        self.assertEqual(result["honest_n"]["human_participants"], 0)
        with self.assertRaises(ValueError):
            scores.score_rollout_sizing(self.plan, rows, self.original, self.distribution, source_kind="fake_test_teacher")

    def test_json_semantic_hash_order_independence_and_validation_receipt(self):
        result = self.score()
        self.assertEqual(self.score(rows=list(reversed(self.rows))), result)
        payload = json.loads(json.dumps(result, allow_nan=False))
        expected = payload.pop("score_semantic_hash")
        self.assertEqual(student.stable_hash(payload), expected)
        receipt = scores.validate_scoring_inputs(self.plan, self.rows, self.original, self.distribution, source_kind="fake_test_teacher")
        self.assertEqual(receipt["bindings"], result["bindings"])
        self.assertEqual(receipt["source_counts"], result["honest_n"])
        self.assertEqual(receipt["public_probe_rows_identity"], result["public_probe_rows_identity"])


class ManagedRolloutSizingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        records = fixture()
        cls.original = student.train_models(records, epochs=1, hidden_dim=3, backend="python")
        cls.distribution = sizing.fit_intensity_distributions(records, cls.original, epochs=1, backend="python")
        cls.plan = make_plan(cls.original)
        cls.rows = make_rows(cls.plan, complete=True, hold_only=True)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rollout-sizing-scores-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.model, self.density = self.root/"source", self.root/"model", self.root/"distribution"
        plan = self.plan
        config = {"scientific_config": {"plan_hash": plan["plan_hash"]},
                  "model_request_config": {"provider": "fake_test_teacher"},
                  "execution_config": {"task": "acquire", "dry_run": False, "live": False}}
        identities = {"schema_version": "rollout-fidelity-identities/1.0",
                      **{key+"_hash":canonical_hash(value) for key,value in config.items()}}
        identities["full_effective_config_hash"] = canonical_hash(identities)
        summary = probes.summarize(plan, self.rows, real_endpoint=False)
        summary.update(physical_attempts=len(self.rows), attempted_logical_requests=len(self.rows), attempts_with_terminal_audit=len(self.rows))
        register_fixture(self.source, {"probe_plan.json": plan, "probe_samples.jsonl": "".join(json.dumps(row)+"\n" for row in self.rows),
            "fidelity_summary.json": summary, "identities.json": {**identities, **config},
            "private_probe_records.jsonl": '{"private_rationale":"never-copy-me"}\n'}, run_kind="rollout_fidelity_acquire")
        register_fixture(self.model, {"student_study.json": self.original}, run_kind="information_market_train")
        register_fixture(self.density, {"intensity_study.json": self.distribution}, run_kind="intensity_distribution_fit")

    def invoke(self, *extra):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), \
             patch("socket.create_connection", side_effect=AssertionError("network forbidden")), \
             patch("nmsim.llm.build_llm", side_effect=AssertionError("Provider forbidden")), \
             patch.object(student, "train_models", side_effect=AssertionError("fit forbidden")), \
             patch.object(sizing, "fit_intensity_distributions", side_effect=AssertionError("fit forbidden")):
            entry.main(["--source-run", str(self.source), "--model-run", str(self.model),
                        "--distribution-run", str(self.density), "--out", str(self.root/"out"), "--run-id", "score", *extra])

    def test_managed_complete_source_pinned_and_inputs_unchanged(self):
        receipts = [verify_run(path) for path in (self.source, self.model, self.density)]
        self.invoke("--source-manifest-sha256", receipts[0]["manifest_sha256"],
                    "--model-manifest-sha256", receipts[1]["manifest_sha256"],
                    "--distribution-manifest-sha256", receipts[2]["manifest_sha256"])
        target = self.root/"out/runs/score"
        verified = verify_run(target)
        self.assertIn("rollout_sizing_scores.json", verified["registered_artifact_paths"])
        result = read_json(target/"rollout_sizing_scores.json")
        self.assertEqual(result["honest_n"]["source_valid_endpoint_responses"], 0)
        self.assertEqual(result["honest_n"]["valid_action_counts"]["hold"], 9)
        self.assertFalse(read_json(target/"run_manifest.json")["llm"]["runtime"]["network_access"])
        for path, before in zip((self.source, self.model, self.density), receipts):
            self.assertEqual(verify_run(path)["snapshot_hash"], before["snapshot_hash"])
        all_output = "".join(path.read_text() for path in target.iterdir() if path.is_file())
        self.assertNotIn("never-copy-me", all_output)
        identities = read_json(target/"identities.json")
        for prefix in ("scientific", "execution", "model_request"):
            self.assertEqual(canonical_hash(identities[prefix+"_config"]), identities[prefix+"_config_hash"])

    def test_help_has_no_run_and_dry_does_not_score(self):
        with self.assertRaises(SystemExit) as stopped:
            self.invoke("--help")
        self.assertEqual(stopped.exception.code, 0)
        self.assertFalse((self.root/"out").exists())
        with patch.object(scores, "score_rollout_sizing", side_effect=AssertionError("score forbidden")):
            self.invoke("--dry-run")
        target = self.root/"out/runs/score"
        self.assertFalse((target/"rollout_sizing_scores.json").exists())
        self.assertEqual(read_json(target/"summary.json")["honest_n"]["scored_source_responses"], 0)

    def test_running_or_failed_source_rejected_before_payload_loading(self):
        manifest = read_json(self.source/"run_manifest.json")
        for status in ("running", "failed"):
            changed = dict(manifest, status=status, managed_run_completed=False, outputs_complete=False)
            (self.source/"run_manifest.json").write_text(json.dumps(changed))
            with patch.object(entry, "_load_probe_source", side_effect=AssertionError("payload must not open")):
                with self.assertRaisesRegex(ValueError, "completed managed run"):
                    self.invoke("--run-id", "refuse-"+status)
            failed = read_json(self.root/"out/runs"/("refuse-"+status)/"run_manifest.json")
            self.assertEqual(failed["honest_n"], 0)
            self.assertEqual(failed["failure_stage"], "config_validation")

    def test_source_kind_config_hash_summary_and_payload_corruption_fail(self):
        receipt = verify_run(self.source)
        actual_read = entry.read_json
        for mutation in ("kind", "summary", "identities", "run_kind"):
            def changed_read(path):
                value = actual_read(path)
                if mutation == "kind" and path.name == "identities.json":
                    value["model_request_config"]["provider"] = "openai"
                elif mutation == "summary" and path.name == "fidelity_summary.json":
                    value["valid_responses"] += 1
                elif mutation == "identities" and path.name == "identities.json":
                    value["scientific_config_hash"] = "f"*64
                elif mutation == "run_kind" and path.name == "run_manifest.json":
                    value["managed_context"]["run_kind"] = "rollout_fidelity_plan"
                return value
            with self.subTest(mutation=mutation), patch.object(entry, "read_json", side_effect=changed_read), self.assertRaises(ValueError):
                entry._load_probe_source(self.source, receipt)
        path = self.source/"probe_samples.jsonl"
        path.write_text(path.read_text()+"{}\n")
        with self.assertRaisesRegex(ValueError, "artifact integrity"):
            self.invoke()

    def test_finished_source_with_missing_rows_is_not_published_as_complete(self):
        rows = self.rows[:-1]
        summary = probes.summarize(self.plan, rows, real_endpoint=False)
        summary.update(physical_attempts=len(rows), attempted_logical_requests=len(rows), attempts_with_terminal_audit=len(rows))
        partial = self.root/"incomplete"
        register_fixture(partial, {"probe_plan.json": self.plan,
            "probe_samples.jsonl": "".join(json.dumps(row)+"\n" for row in rows),
            "fidelity_summary.json": summary, "identities.json": read_json(self.source/"identities.json")},
            run_kind="rollout_fidelity_acquire")
        with self.assertRaisesRegex(ValueError, "unresolved frozen requests"):
            self.invoke("--source-run", str(partial))
        target = self.root/"out/runs/score"
        self.assertFalse((target/"rollout_sizing_scores.json").exists())
        self.assertEqual(read_json(target/"run_manifest.json")["honest_n"], 0)

    def test_acquisition_false_identity_provider_cannot_be_relabelled_by_summary(self):
        original_read = entry.read_json
        receipt = verify_run(self.source)
        def changed_read(path):
            value = original_read(path)
            if path.name == "identities.json":
                value["model_request_config"]["provider"] = "openai"
                value["model_request_config_hash"] = canonical_hash(value["model_request_config"])
                value["full_effective_config_hash"] = canonical_hash({key:value[key] for key in
                    ("schema_version", "scientific_config_hash", "model_request_config_hash", "execution_config_hash")})
            return value
        with patch.object(entry, "read_json", side_effect=changed_read), self.assertRaisesRegex(ValueError, "live gate"):
            entry._load_probe_source(self.source, receipt)

    def test_output_overlap_and_repeated_run_do_not_overwrite_history(self):
        receipt = verify_run(self.source)
        with self.assertRaises(SystemExit):
            self.invoke("--out", str(self.source/"nested"), "--unknown-option")
        self.assertFalse((self.source/"nested").exists())
        self.assertEqual(verify_run(self.source)["snapshot_hash"], receipt["snapshot_hash"])
        self.invoke()
        before = verify_run(self.root/"out/runs/score")
        with self.assertRaises(SystemExit):
            self.invoke()
        self.assertEqual(verify_run(self.root/"out/runs/score")["snapshot_hash"], before["snapshot_hash"])


if __name__ == "__main__":
    unittest.main()
