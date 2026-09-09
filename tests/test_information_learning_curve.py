"""Training-only diagnostics on complete public synthetic four-view families."""
from copy import deepcopy
import json
import math
import unittest
from unittest.mock import patch

from nmsim import information_learning_curve as curves
from nmsim import information_student as student
from nmsim import v2_distillation as distill
from tests.test_information_student import public_records


class MetadataOnlyTestRecord(dict):
    """Explode if even a malformed excluded test payload is inspected."""

    def __getitem__(self, key):
        if key not in {"schema_version", "sample_id", "latent_state_id", "family_id", "profile_id"}:
            raise AssertionError(f"excluded test payload was accessed: {key}")
        return super().__getitem__(key)

    def get(self, key, default=None):
        if key not in {"schema_version", "sample_id", "latent_state_id", "family_id", "profile_id"}:
            raise AssertionError(f"excluded test payload was accessed: {key}")
        return super().get(key, default)


def without_runtime(result):
    payload = deepcopy(result)
    payload.pop("runtime_information")
    return payload


class InformationLearningCurveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.records = public_records(groups=24)
        for index, row in enumerate(cls.records):
            # Two related states, and all eight views, must remain inseparable.
            row["family_id"] = f"family/test-{index // 8:03d}"
        cls.study = student.train_models(cls.records, epochs=3, hidden_dim=3, backend="python")
        cls.result = curves.learning_curves(cls.records, cls.study, fractions=(0.25, 0.5, 1.0),
                                            epochs=3, hidden_dim=3, backend="python")

    def test_original_assignments_and_nested_whole_families(self):
        result = self.result
        self.assertEqual(result["split"], self.study["split"])
        original = self.study["split"]["sample_assignments"]
        previous = set()
        for point in result["points"]:
            selected = set(point["family_ids"])
            self.assertTrue(previous <= selected)
            self.assertEqual(point["family_ids"], result["nested_training_family_order"][:point["family_count"]])
            expected = {row["sample_id"] for row in self.records if row["family_id"] in selected}
            self.assertEqual(set(point["training_sample_ids"]), expected)
            self.assertEqual({original[sample] for sample in expected}, {"train"})
            self.assertEqual(point["latent_group_count"], 2 * point["family_count"])
            self.assertEqual(point["requested_rows"], 8 * point["family_count"])
            self.assertEqual(point["valid_rows"], point["requested_rows"])
            previous = selected
        self.assertEqual(previous, {family for family, partition in self.study["split"]["family_assignments"].items() if partition == "train"})
        self.assertEqual(set(result["validation"]["sample_ids"]), {sample for sample, partition in original.items() if partition == "validation"})

    def test_full_fraction_reproduces_original_fit_and_train_validation_metrics(self):
        full = self.result["points"][-1]
        self.assertEqual(full["models"], self.study["models"])
        for name in student.MODEL_NAMES:
            for partition in ("train", "validation"):
                self.assertEqual(full["evaluation"][name][partition], self.study["evaluation"][name][partition])
            self.assertNotIn("test", full["evaluation"][name])
        self.assertFalse(self.result["settings"]["model_selection_performed"])

    def test_standardizer_fits_each_training_subset_only(self):
        prepared = student.prepare_records(self.records)
        training = prepared["examples_by_partition"]["train"]
        for point in self.result["points"]:
            ids = set(point["training_sample_ids"])
            rows = [row for row in training if row["state_id"] in ids]
            self.assertEqual(point["standardizer"], distill.Standardizer.fit(rows).to_dict())
            self.assertEqual(point["models"]["mlp"]["standardizer"], point["standardizer"])
        self.assertNotEqual(self.result["points"][0]["standardizer"], self.result["points"][-1]["standardizer"])

    def test_malformed_test_labels_and_observations_are_not_accessed(self):
        changed = deepcopy(self.records)
        excluded = set(self.result["test_exclusion"]["excluded_sample_ids"])
        for index, record in enumerate(changed):
            if record["sample_id"] in excluded:
                record.update(decision={"action": "INVALID", "intensity": float("nan")},
                              status=object(), response_hash=object(), visible_observation=None,
                              application_attempt_count=None, technical_retry_count=None)
                changed[index] = MetadataOnlyTestRecord(record)
        with patch.object(student, "normalize_record", wraps=student.normalize_record) as normalize:
            again = curves.learning_curves(changed, self.study, fractions=(0.25, 0.5, 1.0),
                                           epochs=3, hidden_dim=3, backend="python")
        self.assertEqual(without_runtime(again), without_runtime(self.result))
        self.assertTrue(all(call.args[0]["sample_id"] not in excluded for call in normalize.call_args_list))
        self.assertFalse(again["test_exclusion"]["labels_accessed"])
        self.assertEqual(again["test_exclusion"]["predictions_computed"], 0)

    def test_reversed_records_and_additional_fraction_leave_existing_fits_unchanged(self):
        again = curves.learning_curves(list(reversed(self.records)), self.study, fractions=(0.125, 0.25, 0.5, 1.0),
                                       epochs=3, hidden_dim=3, backend="python")
        self.assertEqual(again["points"][1:], self.result["points"])
        self.assertEqual(again["source_identity"], self.result["source_identity"])

    def test_json_model_sizes_timings_and_semantic_hash(self):
        result = json.loads(json.dumps(self.result, allow_nan=False))
        expected_hash = result.pop("curve_semantic_hash")
        timings = result.pop("runtime_information")
        self.assertEqual(expected_hash, student.stable_hash(result))
        for point in result["points"]:
            self.assertEqual(point["parameter_counts"], {"prior": 5, "linear": 285, "mlp": 191})
            for name, model in point["models"].items():
                self.assertEqual(point["model_serialization_hashes"][name], student.stable_hash(model))
        for point in timings["points"]:
            self.assertGreaterEqual(point["standardizer_seconds"], 0)
            self.assertTrue(all(math.isfinite(value) and value >= 0 for value in point["fit_seconds_by_model"].values()))
        self.assertTrue(timings["excluded_from_curve_semantic_hash"])

    def test_optional_numpy_matches_python_predictions_and_losses(self):
        try:
            import numpy  # noqa: F401
        except ImportError:
            self.skipTest("optional NumPy is not installed")
        fast = curves.learning_curves(self.records, self.study, fractions=(0.25, 0.5, 1.0),
                                     epochs=3, hidden_dim=3, backend="numpy")
        view = self.records[0]["visible_observation"]
        for first, second in zip(self.result["points"], fast["points"]):
            for name in ("linear", "mlp"):
                for a, b in zip(first["loss_history"][name]["loss"], second["loss_history"][name]["loss"]):
                    self.assertAlmostEqual(a, b, places=10)
                a = student.predict_model(first["models"][name], view["visible_fields"], view["account_state"])
                b = student.predict_model(second["models"][name], view["visible_fields"], view["account_state"])
                for key in a:
                    for x, y in zip(a[key], b[key]):
                        self.assertAlmostEqual(x, y, places=10)

    def test_failures_remain_requested_and_never_become_training_labels(self):
        changed = deepcopy(self.records)
        family = self.result["nested_training_family_order"][0]
        failing = next(row for row in changed if row["family_id"] == family)
        failing.update(status="failed", decision=None, failure_code="provider_exception", response_hash=None)
        original = student.train_models(changed, epochs=1, hidden_dim=2, backend="python")
        result = curves.learning_curves(changed, original, fractions=(0.25, 1.0), epochs=1, hidden_dim=2, backend="python")
        for point in result["points"]:
            self.assertEqual(point["failed_rows"], 1)
            self.assertEqual(point["requested_rows"], point["valid_rows"] + 1)
            self.assertNotIn(failing["sample_id"], point["training_sample_ids"])
        self.assertEqual(result["split"]["sample_assignments"], self.study["split"]["sample_assignments"])

    def test_changed_split_roster_or_non_test_payload_rejected(self):
        modified_study = deepcopy(self.study)
        modified_study["split"]["seed"] += 1
        with self.assertRaisesRegex(curves.LearningCurveContractError, "hash mismatch"):
            curves.learning_curves(self.records, modified_study, epochs=1)
        for changed in (self.records[:-1], self.records + [self.records[0]]):
            with self.assertRaises(curves.LearningCurveContractError):
                curves.learning_curves(changed, self.study, epochs=1)
        changed = deepcopy(self.records)
        train = next(row for row in changed if self.study["split"]["sample_assignments"][row["sample_id"]] == "train")
        train["decision"] = {"invalid": True}
        with self.assertRaises(curves.LearningCurveContractError):
            curves.learning_curves(changed, self.study, epochs=1)

    def test_config_options_have_effect_and_invalid_values_fail(self):
        changed = curves.learning_curves(self.records, self.study, fractions=(1.0,), epochs=2, hidden_dim=4, backend="python")
        self.assertEqual(changed["points"][0]["parameter_counts"]["mlp"], 253)
        self.assertEqual(len(changed["points"][0]["loss_history"]["mlp"]["loss"]), 2)
        self.assertNotEqual(changed["points"][0]["models"]["mlp"], self.result["points"][-1]["models"]["mlp"])
        for kwargs in ({"fractions": ()}, {"fractions": (0, 1)}, {"fractions": (0.5, 0.25)},
                       {"fractions": (0.5, 0.5)}, {"fractions": (float("nan"),)}, {"fractions": (True,)},
                       {"fractions": (1.1,)}, {"epochs": 0}, {"hidden_dim": True}, {"backend": "remote"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(curves.LearningCurveContractError):
                curves.learning_curves(self.records, self.study, **kwargs)


if __name__ == "__main__":
    unittest.main()
