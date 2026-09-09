"""Pure synthetic distributional sizing checks, with no research artifact I/O."""
from copy import deepcopy
import json
import math
import unittest
from unittest.mock import patch

from nmsim import information_student as student
from nmsim import intensity_distribution as sizing
from nmsim import v2_distillation as distill
from tests.test_information_learning_curve import MetadataOnlyTestRecord
from tests.test_information_student import public_records


def fixture():
    records = public_records(groups=36)
    for index, row in enumerate(records):
        # Exact atoms are actual synthetic labels, not pseudo-observations.
        if row["decision"]["action"] != "hold":
            row["decision"]["intensity"] = (index % 5) / 4
        row["family_id"] = f"sizing/family-{index // 8:03d}"
    return records


def without_runtime(result):
    result = deepcopy(result)
    result.pop("runtime_information")
    return result


def rehash(result):
    result.pop("study_semantic_hash", None)
    runtime = result.pop("runtime_information", None)
    result["study_semantic_hash"] = student.stable_hash(result)
    if runtime is not None:
        result["runtime_information"] = runtime
    return result


class DistributionMathTests(unittest.TestCase):
    def test_exact_quantile_endpoints_zero_mass_and_boundary_convention(self):
        law = {"support": [0, 0.25, 0.5, 1], "probabilities": [0, 0.25, 0, 0.75]}
        self.assertEqual(sizing.quantile_sample(law, 0), 0.25)
        self.assertEqual(sizing.quantile_sample(law, 0.2499), 0.25)
        self.assertEqual(sizing.quantile_sample(law, 0.25), 1)
        self.assertEqual(sizing.quantile_sample(law, 1), 1)
        zero_tail = {"support": [0, 0.5, 1], "probabilities": [0, 1, 0]}
        self.assertEqual(sizing.quantile_sample(zero_tail, 1), 0.5)
        rounded = {"support": [0, 0.5, 1], "probabilities": [0.6, 0.400000000001, 1e-12]}
        self.assertEqual(sizing.quantile_sample(rounded, 1), 1)
        for value in (True, -0.1, 1.01, float("nan"), "0.2"):
            with self.subTest(value=value), self.assertRaises(sizing.DistributionContractError):
                sizing.quantile_sample(law, value)

    def test_full_position_exit_uses_actual_one_without_rounding_change(self):
        law = {"support": [0.2, 1], "probabilities": [0.6, 0.4]}
        sampled = sizing.quantile_sample(law, 0.8)
        self.assertEqual(sampled, 1.0)
        self.assertEqual(int(sampled * 7), 7)
        self.assertLess(int(sizing.distribution_mean(law) * 7), 7)

    def test_crps_point_mass_continuous_observation_and_expected_propriety(self):
        point = {"support": [0.2], "probabilities": [1]}
        self.assertEqual(sizing.distribution_scores(point, 0.2), {"crps": 0, "wasserstein1_to_point": 0})
        self.assertAlmostEqual(sizing.distribution_scores(point, 0.8)["crps"], 0.6)
        mixture = {"support": [0, 1], "probabilities": [0.5, 0.5]}
        scores = sizing.distribution_scores(mixture, 0.3)
        self.assertAlmostEqual(scores["crps"], 0.25)
        self.assertAlmostEqual(scores["wasserstein1_to_point"], 0.5)
        # Under a Bernoulli(0.5) outcome, its true law beats a point-at-mean law.
        mean_point = {"support": [0.5], "probabilities": [1]}
        truth_score = sum(sizing.distribution_scores(mixture, y)["crps"] for y in (0, 1)) / 2
        mean_score = sum(sizing.distribution_scores(mean_point, y)["crps"] for y in (0, 1)) / 2
        self.assertLess(truth_score, mean_score)
        with self.assertRaises(sizing.DistributionContractError):
            sizing.distribution_scores(mixture, 1.1)

    def test_validation_is_strict_and_does_not_normalize_arbitrary_mass(self):
        invalid = [None, {}, {"support": [], "probabilities": []},
                   {"support": [0, 0], "probabilities": [0.5, 0.5]},
                   {"support": [1, 0], "probabilities": [0.5, 0.5]},
                   {"support": [0, 1], "probabilities": [0.5]},
                   {"support": [0, 1], "probabilities": [0.2, 0.2]},
                   {"support": [0, 1], "probabilities": [-1, 2]},
                   {"support": [0, 2], "probabilities": [0.5, 0.5]},
                   {"support": [True], "probabilities": [1]},
                   {"support": [float("nan")], "probabilities": [1]},
                   {"support": [0], "probabilities": [float("inf")]},
                   {"support": [0], "probabilities": [1], "private_rationale": "x"}]
        for law in invalid:
            with self.subTest(law=law), self.assertRaises(sizing.DistributionContractError):
                sizing.validate_distribution(law)


class IntensityDistributionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.records = fixture()
        cls.original = student.train_models(cls.records, epochs=3, hidden_dim=4, backend="python")
        cls.result = sizing.fit_intensity_distributions(cls.records, cls.original, epochs=8, backend="python")

    def test_original_actions_and_intensities_unchanged_with_serialization_roundtrip(self):
        original_before = deepcopy(self.original)
        result = json.loads(json.dumps(self.result, allow_nan=False))
        original_predictor = student.make_predictor(self.original["models"][self.original["selected_model"]])
        for candidate in ("selected", *sizing.CANDIDATES):
            predict = sizing.make_distribution_predictor(result, self.original, candidate=candidate)
            for row in self.records[:12]:
                view = row["visible_observation"]
                old = original_predictor(view["visible_fields"], view["account_state"])
                new = predict(view["visible_fields"], view["account_state"])
                self.assertEqual(old["action_probs"], new["action_probs"])
                self.assertEqual(old["intensities"], new["intensities"])
                for index, action in enumerate(distill.INTENSITY_ACTIONS):
                    self.assertEqual(new["intensity_means"][index], sizing.distribution_mean(new["intensity_distributions"][action]))
                    self.assertAlmostEqual(sum(new["intensity_distributions"][action]["probabilities"]), 1)
        self.assertEqual(self.original, original_before)
        self.assertFalse(result["training"]["action_model_changed"])
        self.assertFalse(result["training"]["representation_updated"])

    def test_exact_train_support_counts_and_family_partition_provenance(self):
        result = self.result
        self.assertEqual(result["split"], self.original["split"])
        prepared = student.prepare_records(self.records)
        train = prepared["examples_by_partition"]["train"]
        self.assertEqual(result["source_identity"]["training_observations_semantic_hash"], distill.canonical_observations_hash(train, stratum_key=None))
        for index, action in enumerate(distill.INTENSITY_ACTIONS):
            labels = [row["intensity_targets"][index] for row in train if row["intensity_weights"][index]]
            expected = sorted(set(labels))
            record = result["exact_training_support"][action]
            self.assertEqual(record["support"], expected)
            self.assertEqual(record["counts"], [labels.count(value) for value in expected])
            self.assertEqual(sum(record["counts"]), len(labels))
            self.assertIn(0, expected)
            self.assertIn(1, expected)
            self.assertEqual({result["split"]["sample_assignments"][key] for key in record["training_sample_ids"]}, {"train"})
            self.assertEqual(result["parameter_counts"]["conditional_softmax"][action], len(expected) * 5)
        self.assertTrue(set(result["training"]["family_ids"]).isdisjoint(result["validation"]["family_ids"]))

    def test_test_payload_is_not_accessed_even_when_invalid_or_absent(self):
        changed = deepcopy(self.records)
        excluded = set(self.result["test_exclusion"]["excluded_sample_ids"])
        for index, row in enumerate(changed):
            if row["sample_id"] in excluded:
                changed[index] = MetadataOnlyTestRecord({key: value for key, value in row.items() if key in {
                    "schema_version", "sample_id", "latent_state_id", "family_id", "profile_id"}})
        with patch.object(student, "normalize_record", wraps=student.normalize_record) as normalize:
            again = sizing.fit_intensity_distributions(changed, self.original, epochs=8, backend="python")
        self.assertEqual(without_runtime(again), without_runtime(self.result))
        self.assertTrue(all(call.args[0]["sample_id"] not in excluded for call in normalize.call_args_list))
        self.assertFalse(again["test_exclusion"]["used_for_selection"])
        self.assertEqual(again["test_exclusion"]["predictions_computed"], 0)
        for candidate in again["evaluation"].values():
            self.assertNotIn("test", candidate)

    def test_validation_continuous_off_support_values_never_enter_training_support(self):
        changed = deepcopy(self.records)
        for row in changed:
            if self.original["split"]["sample_assignments"][row["sample_id"]] == "validation" and row["decision"]["action"] != "hold":
                row["decision"]["intensity"] = 0.333333
        original = student.train_models(changed, epochs=3, hidden_dim=4, backend="python")
        result = sizing.fit_intensity_distributions(changed, original, epochs=8, backend="python")
        self.assertEqual(result["models"], self.result["models"])
        for action in distill.INTENSITY_ACTIONS:
            self.assertNotIn(0.333333, result["exact_training_support"][action]["support"])
            metric = result["evaluation"]["conditional_softmax"]["validation"][action]
            self.assertGreater(metric["n"], 0)
            self.assertEqual(metric["off_support_observations"], metric["n"])
            self.assertTrue(math.isfinite(metric["crps"]))

    def test_missing_training_endpoints_are_not_invented_from_validation(self):
        changed = deepcopy(self.records)
        for row in changed:
            if row["decision"]["action"] != "hold":
                partition = self.original["split"]["sample_assignments"][row["sample_id"]]
                row["decision"]["intensity"] = 0.6 if partition == "train" else 1.0
        original = student.train_models(changed, epochs=1, hidden_dim=2, backend="python")
        result = sizing.fit_intensity_distributions(changed, original, epochs=1, backend="python")
        for action in distill.INTENSITY_ACTIONS:
            self.assertEqual(result["exact_training_support"][action]["support"], [0.6])
            self.assertEqual(result["selection"]["selected_by_action"][action], "empirical")
            metric = result["evaluation"]["conditional_softmax"]["validation"][action]
            self.assertAlmostEqual(metric["endpoint"]["1"]["brier"], 1.0)
            self.assertAlmostEqual(metric["crps"], 0.4)

    def test_failures_remain_in_original_denominator_and_do_not_become_atoms(self):
        changed = deepcopy(self.records)
        failing = next(row for row in changed if self.original["split"]["sample_assignments"][row["sample_id"]] == "train")
        failing.update(status="failed", decision=None, failure_code="provider_exception", response_hash=None)
        original = student.train_models(changed, epochs=1, hidden_dim=2, backend="python")
        result = sizing.fit_intensity_distributions(changed, original, epochs=1, backend="python")
        self.assertEqual(result["split"]["counts"]["train"]["failed"], 1)
        self.assertEqual(result["training"]["valid_rows"], self.result["training"]["valid_rows"] - 1)
        self.assertNotIn(failing["sample_id"], result["training"]["sample_ids"])
        for action in distill.INTENSITY_ACTIONS:
            self.assertNotIn(failing["sample_id"], result["exact_training_support"][action]["training_sample_ids"])

    def test_no_validation_action_has_null_scores_and_explicit_empirical_fallback(self):
        changed = deepcopy(self.records)
        for row in changed:
            if self.original["split"]["sample_assignments"][row["sample_id"]] == "validation":
                row["decision"].update(action="hold", intensity=0)
        original = student.train_models(changed, epochs=1, hidden_dim=2, backend="python")
        result = sizing.fit_intensity_distributions(changed, original, epochs=1, backend="python")
        for action in distill.INTENSITY_ACTIONS:
            self.assertEqual(result["selection"]["selected_by_action"][action], "empirical")
            for candidate in sizing.CANDIDATES:
                self.assertEqual(result["evaluation"][candidate]["validation"][action]["n"], 0)
                self.assertIsNone(result["evaluation"][candidate]["validation"][action]["crps"])

    def test_loss_finite_decreases_and_backend_matches(self):
        for history in self.result["training"]["loss_history"].values():
            self.assertEqual(len(history), 8)
            self.assertTrue(all(math.isfinite(value) for value in history))
            self.assertLess(history[-1], history[0])
        try:
            import numpy  # noqa: F401
        except ImportError:
            self.skipTest("optional NumPy unavailable")
        fast = sizing.fit_intensity_distributions(self.records, self.original, epochs=8, backend="numpy")
        for action in distill.INTENSITY_ACTIONS:
            for a, b in zip(self.result["training"]["loss_history"][action], fast["training"]["loss_history"][action]):
                self.assertAlmostEqual(a, b, places=11)
        first = sizing.make_distribution_predictor(self.result, self.original, candidate="conditional_softmax")
        second = sizing.make_distribution_predictor(fast, self.original, candidate="conditional_softmax")
        view = self.records[0]["visible_observation"]
        for action in distill.INTENSITY_ACTIONS:
            a = first(view["visible_fields"], view["account_state"])["intensity_distributions"][action]
            b = second(view["visible_fields"], view["account_state"])["intensity_distributions"][action]
            self.assertEqual(a["support"], b["support"])
            for p, q in zip(a["probabilities"], b["probabilities"]):
                self.assertAlmostEqual(p, q, places=11)

    def test_selection_uses_crps_and_endpoint_reliability_keeps_denominators(self):
        for action in distill.INTENSITY_ACTIONS:
            scores = self.result["selection"]["validation_scores"][action]
            expected = min(sizing.CANDIDATES, key=lambda name: (scores[name], sizing.CANDIDATES.index(name)))
            self.assertEqual(self.result["selection"]["selected_by_action"][action], expected)
            for candidate in sizing.CANDIDATES:
                metric = self.result["evaluation"][candidate]["validation"][action]
                self.assertEqual(scores[candidate], metric["crps"])
                for endpoint in ("0", "1"):
                    reliability = metric["endpoint"][endpoint]
                    self.assertEqual(sum(row["n"] for row in reliability["reliability_bins"]), metric["n"])
                    self.assertGreaterEqual(reliability["brier"], 0)
                    self.assertLessEqual(reliability["brier"], 1)

    def test_source_alterations_roster_and_provenance_fail_closed(self):
        for changed in (self.records[:-1], self.records + [self.records[0]]):
            with self.assertRaises(sizing.DistributionContractError):
                sizing.fit_intensity_distributions(changed, self.original, epochs=1)
        changed = deepcopy(self.records)
        row = next(row for row in changed if self.original["split"]["sample_assignments"][row["sample_id"]] == "train" and row["decision"]["action"] != "hold")
        row["decision"]["intensity"] = 0.1234
        with self.assertRaisesRegex(sizing.DistributionContractError, "training observations differ"):
            sizing.fit_intensity_distributions(changed, self.original, epochs=1)
        changed_original = deepcopy(self.original)
        changed_original["models"]["mlp"]["parameters"]["b1"][0] += 1
        with self.assertRaisesRegex(sizing.DistributionContractError, "serialization hash"):
            sizing.fit_intensity_distributions(self.records, changed_original, epochs=1)
        changed_original = deepcopy(self.original)
        changed_original["provenance"]["normalized_records_semantic_hash"] = "f" * 64
        with self.assertRaisesRegex(sizing.DistributionContractError, "another original"):
            sizing.make_distribution_predictor(self.result, changed_original)

    def test_serialized_corruption_rejected_even_with_rehashed_envelope(self):
        changed = deepcopy(self.result)
        changed["models"]["conditional_softmax"]["buy"]["weights"][0][0] += 0.01
        with self.assertRaisesRegex(sizing.DistributionContractError, "study semantic hash"):
            sizing.make_distribution_predictor(changed, self.original)
        rehash(changed)
        with self.assertRaisesRegex(sizing.DistributionContractError, "serialization hash"):
            sizing.make_distribution_predictor(changed, self.original)
        changed = deepcopy(self.result)
        changed["models"]["conditional_softmax"]["buy"]["weights"][0] = []
        changed["model_serialization_semantic_hashes"]["conditional_softmax"] = student.stable_hash(changed["models"]["conditional_softmax"])
        rehash(changed)
        with self.assertRaisesRegex(sizing.DistributionContractError, "shape"):
            sizing.make_distribution_predictor(changed, self.original)

    def test_deterministic_order_hash_and_runtime_exclusion(self):
        again = sizing.fit_intensity_distributions(list(reversed(self.records)), self.original, epochs=8, backend="python")
        self.assertEqual(without_runtime(again), without_runtime(self.result))
        payload = without_runtime(self.result)
        expected = payload.pop("study_semantic_hash")
        self.assertEqual(student.stable_hash(payload), expected)
        self.assertGreaterEqual(self.result["runtime_information"]["total_seconds"], 0)

    def test_invalid_options_missing_action_and_work_bounds(self):
        for options in ({"epochs": 0}, {"epochs": True}, {"backend": "remote"}):
            with self.subTest(options=options), self.assertRaises(sizing.DistributionContractError):
                sizing.fit_intensity_distributions(self.records, self.original, **options)
        with patch.object(sizing, "MAX_PYTHON_MULTIPLIES", 1), self.assertRaisesRegex(sizing.DistributionContractError, "work bound"):
            sizing.fit_intensity_distributions(self.records, self.original, epochs=1, backend="python")
        with patch.object(sizing, "MAX_SUPPORT", 2), self.assertRaisesRegex(sizing.DistributionContractError, "no automatic binning"):
            sizing.fit_intensity_distributions(self.records, self.original, epochs=1, backend="python")
        fewer_epochs = sizing.fit_intensity_distributions(self.records, self.original, epochs=1, backend="python")
        self.assertNotEqual(fewer_epochs["models"]["conditional_softmax"], self.result["models"]["conditional_softmax"])
        self.assertEqual(fewer_epochs["models"]["empirical"], self.result["models"]["empirical"])
        with self.assertRaises(sizing.DistributionContractError):
            sizing.make_distribution_predictor(self.result, self.original, candidate="oracle")
        changed = deepcopy(self.records)
        for row in changed:
            row["decision"].update(action="buy", intensity=0.5)
        original = student.train_models(changed, epochs=1, hidden_dim=2, backend="python")
        with self.assertRaisesRegex(sizing.DistributionContractError, "no original training sell"):
            sizing.fit_intensity_distributions(changed, original, epochs=1, backend="python")


if __name__ == "__main__":
    unittest.main()
