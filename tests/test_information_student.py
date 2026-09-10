"""Pure, public synthetic fixtures; no ignored run, Provider, or network needed."""
from copy import deepcopy
import json
import math
import unittest

from nmsim import information_student as student
from nmsim import information_weight as domain
from nmsim import information_weight_scale as scale
from nmsim import v2_attention as attention
from nmsim import v2_distillation as distill


def public_records(groups=16):
    rows = []
    for index in range(groups):
        signal = (index % 7 - 3) / 20
        information = {name: 0.0 for name in student.INFORMATION_FIELDS}
        information.update(return_1d=signal, return_5d=signal, return_20d=signal,
                           drawdown_20d=min(0.0, signal), signed_event_surprise=signal,
                           earnings_yield_ttm=signal, debt_to_assets=0.5, current_ratio=1.0)
        account = {name: 0 for name in domain.ACCOUNT8}
        account.update(position_fraction=0.5, log10_wealth=5.0,
                       unrealized_return_mask=1, unrealized_return=signal)
        latent = f"latent/test-{index:04d}"
        latent_hash = student.stable_hash({"latent_id": latent, "information": information, "account": account})
        for profile in domain.PROFILE_IDS:
            view = {"schema_version": scale.VIEW_SCHEMA_VERSION, "latent_id": latent,
                    "latent_hash": latent_hash, "profile_id": profile,
                    "visible_fields": {name: information[name] for name in domain.PROFILE_FIELDS[profile]},
                    "account_state": dict(account)}
            update_view_hash(view)
            action = "hold" if index % 5 == 0 else "buy" if signal > 0 else "sell"
            rows.append({
                "schema_version": student.SOURCE_SCHEMA_VERSION,
                "sample_id": student.stable_hash([latent, profile]),
                "latent_state_id": latent, "profile_id": profile,
                "visible_observation": view, "status": "valid",
                "decision": {"action": action, "intensity": 0 if action == "hold" else 0.6,
                             "response_schema_version": attention.RESPONSE_SCHEMA_VERSION},
                "failure_code": None, "response_hash": student.stable_hash([latent, profile, "response"]),
                "application_attempt_count": 1, "technical_retry_count": 0,
                "prompt": {"system": "Synthetic public fixture, never sent", "user": "{}"},
            })
    return rows


def update_view_hash(view):
    view["view_hash"] = domain.stable_hash({key: view[key] for key in (
        "schema_version", "latent_hash", "profile_id", "visible_fields", "account_state")})


class InformationEncoderTests(unittest.TestCase):
    def setUp(self):
        self.row = public_records()[0]
        self.view = self.row["visible_observation"]

    def test_encoder_exact_width_and_missing_zero_distinction(self):
        visible = self.view["visible_fields"]
        account = self.view["account_state"]
        result = student.encode_observation(visible, account)
        self.assertEqual(len(result), 56)
        name = "current_ratio"
        self.assertNotIn(name, visible)
        zero_visible = dict(visible, current_ratio=0.0)
        second = student.encode_observation(zero_visible, account)
        index = student.INFORMATION_FIELDS.index(name)
        self.assertEqual(result[index], second[index])
        self.assertEqual(result[index + 24], 0)
        self.assertEqual(second[index + 24], 1)
        self.assertEqual(sum(a != b for a, b in zip(result, second)), 1)

    def test_unseen_latent_values_and_prompt_do_not_enter_features(self):
        changed = deepcopy(self.row)
        changed["latent_state"] = {"current_ratio": 9999, "hidden_secret": "not_copied"}
        changed["prompt"] = {"system": "Different irrelevant prompt", "user": "Different wording"}
        self.assertEqual(student.normalize_record(self.row), student.normalize_record(changed))
        self.assertNotIn("not_copied", json.dumps(student.normalize_record(changed)))

    def test_unknown_and_private_inputs_rejected(self):
        for key in ("raw_response", "private_rationale", "api_key"):
            changed = dict(self.row, **{key: "secret"})
            with self.subTest(key=key), self.assertRaises(student.StudentContractError):
                student.normalize_record(changed)
        changed = deepcopy(self.row)
        changed["decision"]["reasoning"] = "private"
        with self.assertRaises(student.StudentContractError):
            student.normalize_record(changed)
        with self.assertRaises(student.StudentContractError):
            student.encode_observation({"unknown": 1}, self.view["account_state"])

    def test_invalid_numeric_mask_schema_hash_and_range_fail_closed(self):
        for value in (True, float("nan"), float("inf"), "0.2"):
            changed = deepcopy(self.row)
            changed["visible_observation"]["visible_fields"]["return_1d"] = value
            with self.subTest(value=value), self.assertRaises(student.StudentContractError):
                student.normalize_record(changed)
        for mutation in ("hash", "schema", "mask", "range", "intensity", "attempt"):
            changed = deepcopy(self.row)
            view = changed["visible_observation"]
            if mutation == "hash":
                view["view_hash"] = "0" * 64
            elif mutation == "schema":
                changed["schema_version"] = "private-v1"
            elif mutation == "mask":
                view["account_state"]["post_sale_return"] = 0.2
            elif mutation == "range":
                view["visible_fields"]["return_1d"] = 0.8
                update_view_hash(view)
            elif mutation == "intensity":
                changed["decision"]["intensity"] = 0.8  # hold must be zero
            elif mutation == "attempt":
                changed["technical_retry_count"] = 9
            with self.subTest(mutation=mutation), self.assertRaises(student.StudentContractError):
                student.normalize_record(changed)

    def test_inference_accepts_finite_ood_without_clipping(self):
        changed = dict(self.view["visible_fields"], return_1d=1.0)
        self.assertEqual(student.encode_observation(changed, self.view["account_state"])[0], 1.0)


class InformationSplitTests(unittest.TestCase):
    def test_all_views_grouped_and_order_independent(self):
        rows = public_records()
        prepared = student.prepare_records(rows)
        reversed_result = student.prepare_records(list(reversed(rows)))
        self.assertEqual(prepared, reversed_result)
        for latent in {row["latent_state_id"] for row in rows}:
            destinations = {prepared["split"]["sample_assignments"][row["sample_id"]] for row in rows if row["latent_state_id"] == latent}
            self.assertEqual(len(destinations), 1)
        self.assertEqual(prepared["accounting"]["logical_requests"], 64)
        self.assertEqual(prepared["accounting"]["human_participants"], 0)

    def test_failure_accounting_and_assignments_precede_filtering(self):
        rows = public_records()
        original = student.prepare_records(rows)
        for row in rows[:4]:
            row.update(status="failed", decision=None, failure_code="provider_exception",
                       application_attempt_count=3, technical_retry_count=2, response_hash=None)
        after = student.prepare_records(rows)
        self.assertEqual(original["split"]["family_assignments"], after["split"]["family_assignments"])
        self.assertEqual(after["accounting"]["logical_requests"], 64)
        self.assertEqual(after["accounting"]["valid_teacher_answers"], 60)
        self.assertEqual(after["accounting"]["physical_attempts"], 72)
        self.assertEqual(after["accounting"]["technical_retries"], 8)
        self.assertEqual(after["accounting"]["complete_valid_paired_groups"], 15)
        self.assertEqual(after["accounting"]["failure_counts"], {"provider_exception": 4})

    def test_explicit_family_groups_related_latent_states(self):
        rows = public_records()
        for index, row in enumerate(rows):
            row["family_id"] = f"family/{index // 8}"
        result = student.prepare_records(rows)
        for family in {row["family_id"] for row in rows}:
            partitions = {result["split"]["latent_assignments"][row["latent_state_id"]] for row in rows if row["family_id"] == family}
            self.assertEqual(len(partitions), 1)

    def test_missing_duplicate_and_inconsistent_groups_rejected(self):
        rows = public_records()
        for changed in (rows[:-1], rows + [rows[0]]):
            with self.assertRaises(student.StudentContractError):
                student.prepare_records(changed)
        changed = deepcopy(rows)
        changed[0]["visible_observation"]["account_state"]["log10_wealth"] = 6
        update_view_hash(changed[0]["visible_observation"])
        with self.assertRaises(student.StudentContractError):
            student.prepare_records(changed)
        changed = deepcopy(rows)
        changed[0]["visible_observation"]["visible_fields"]["return_1d"] = 0
        update_view_hash(changed[0]["visible_observation"])
        with self.assertRaises(student.StudentContractError):
            student.prepare_records(changed)

    def test_seed_has_traceable_split_effect(self):
        rows = public_records()
        self.assertNotEqual(student.prepare_records(rows, seed=1)["split"]["family_assignments"],
                            student.prepare_records(rows, seed=2)["split"]["family_assignments"])


class InformationTrainingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.records = public_records()
        cls.result = student.train_models(cls.records, epochs=6, hidden_dim=4, backend="python")

    def test_json_roundtrip_parameter_counts_finite_inference(self):
        encoded = json.dumps(self.result, allow_nan=False)
        roundtrip = json.loads(encoded)
        self.assertEqual(roundtrip["training"]["parameter_counts"], {"prior": 5, "linear": 285, "mlp": 253})
        self.assertEqual(roundtrip["selected_model"], min(student.MODEL_NAMES, key=lambda name: self.result["evaluation"][name]["validation"]["action_cross_entropy"]))
        view = self.records[0]["visible_observation"]
        for name, payload in roundtrip["models"].items():
            predictor = student.make_predictor(payload)
            result = predictor(view["visible_fields"], view["account_state"])
            self.assertEqual(result, student.predict_model(payload, view["visible_fields"], view["account_state"]))
            self.assertAlmostEqual(sum(result["action_probs"]), 1)
            self.assertTrue(all(0 <= value <= 1 and math.isfinite(value) for values in result.values() for value in values))
            self.assertEqual(self.result["provenance"]["model_serialization_hashes"][name], student.stable_hash(payload))
        self.assertNotIn("Synthetic public fixture", encoded)
        self.assertNotIn('"prompt"', encoded)
        self.assertNotIn('"reasoning"', encoded)

    def test_test_labels_cannot_change_models_or_selected_candidate(self):
        changed = deepcopy(self.records)
        test_ids = {key for key, value in self.result["split"]["sample_assignments"].items() if value == "test"}
        for row in changed:
            if row["sample_id"] in test_ids:
                row["decision"].update(action="buy", intensity=0.1)
        changed_result = student.train_models(changed, epochs=6, hidden_dim=4, backend="python")
        self.assertEqual(self.result["models"], changed_result["models"])
        self.assertEqual(self.result["selection"], changed_result["selection"])
        self.assertNotEqual(self.result["evaluation"]["prior"]["test"], changed_result["evaluation"]["prior"]["test"])
        self.assertEqual(self.result["ood_reference"], changed_result["ood_reference"])

    def test_train_support_does_not_use_test_features(self):
        prepared = student.prepare_records(self.records)
        training = prepared["examples_by_partition"]["train"]
        ref = self.result["ood_reference"]
        self.assertEqual(ref["feature_min"], [min(row["features"][i] for row in training) for i in range(56)])
        view = self.records[0]["visible_observation"]
        check = student.make_support_checker(ref)
        result = check(dict(view["visible_fields"], return_1d=1.5), view["account_state"])
        self.assertTrue(result["outside_train_range"])
        self.assertIn("return_1d", result["outside_features"])
        self.assertFalse(result["joint_support_assessed"])

    def test_numpy_backend_matches_existing_loss_and_graph(self):
        try:
            import numpy  # noqa: F401
        except ImportError:
            self.skipTest("optional NumPy is not installed")
        accelerated = student.train_models(self.records, epochs=6, hidden_dim=4, backend="numpy")
        for name in ("linear", "mlp"):
            for a, b in zip(self.result["training"]["loss_history"][name]["loss"], accelerated["training"]["loss_history"][name]["loss"]):
                self.assertAlmostEqual(a, b, places=10)
            view = self.records[12]["visible_observation"]
            a = student.predict_model(self.result["models"][name], view["visible_fields"], view["account_state"])
            b = student.predict_model(accelerated["models"][name], view["visible_fields"], view["account_state"])
            for key in a:
                for x, y in zip(a[key], b[key]):
                    self.assertAlmostEqual(x, y, places=10)
        self.assertEqual(accelerated["training"]["backend"], "numpy")

    def test_repeatable_fitting_and_effective_training_parameters(self):
        repeat = student.train_models(self.records, epochs=6, hidden_dim=4, backend="python")
        self.assertEqual(repeat, self.result)
        different = student.train_models(self.records, epochs=2, hidden_dim=3, backend="python")
        self.assertNotEqual(different["models"]["mlp"], self.result["models"]["mlp"])
        self.assertEqual(len(different["training"]["loss_history"]["linear"]["loss"]), 2)
        self.assertEqual(different["training"]["parameter_counts"]["mlp"], 191)

    def test_invalid_training_options_and_too_few_groups_rejected(self):
        for kwargs in ({"epochs": 0}, {"epochs": True}, {"hidden_dim": 0}, {"backend": "remote"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(student.StudentContractError):
                student.train_models(self.records, **kwargs)
        with self.assertRaises(student.StudentContractError):
            student.train_models(public_records(1), epochs=1)


if __name__ == "__main__":
    unittest.main()
