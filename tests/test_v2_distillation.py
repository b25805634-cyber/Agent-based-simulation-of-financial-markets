from __future__ import annotations

import json
import math
import random
import unittest

from nmsim.v2_distillation import (
    ACTIONS,
    INTENSITY_ACTIONS,
    ActionPrior,
    DistillationContractError,
    LinearSoftmaxStudent,
    OODReference,
    Standardizer,
    TanhMLPStudent,
    apply_frozen_family_assignments,
    build_ood_reference,
    canonical_observations_hash,
    deterministic_group_split,
    evaluate_model,
    evaluate_predictions,
    ood_diagnostics,
    ood_diagnostic_descriptor,
)


def _row(
    family_id: str,
    state_id: str,
    features: list[float],
    target_probs: list[float] | None = None,
    *,
    stratum: str | None = None,
) -> dict:
    value = {
        "family_id": family_id,
        "state_id": state_id,
        "features": features,
        "target_probs": target_probs or [0.2, 0.5, 0.3],
        "intensity_targets": [0.25, 0.75],
        "intensity_weights": [0.2, 0.3],
    }
    if stratum is not None:
        value["stratum"] = stratum
    return value


def _learnable_rows() -> list[dict]:
    rows = []
    for index in range(60):
        x_value = -1.0 + 2.0 * index / 59.0
        if x_value < -0.3:
            probabilities = [0.05, 0.05, 0.90]
            intensities = [0.20, 0.80]
        elif x_value > 0.3:
            probabilities = [0.90, 0.05, 0.05]
            intensities = [0.80, 0.20]
        else:
            probabilities = [0.05, 0.90, 0.05]
            intensities = [0.30, 0.30]
        row = _row(
            f"family-{index:03d}",
            f"state-{index:03d}",
            [x_value, x_value * x_value],
            probabilities,
        )
        row["intensity_targets"] = intensities
        row["intensity_weights"] = [probabilities[0], probabilities[2]]
        rows.append(row)
    return rows


class ObservationContractTests(unittest.TestCase):
    def test_action_orders_are_frozen(self) -> None:
        self.assertEqual(ACTIONS, ("buy", "hold", "sell"))
        self.assertEqual(INTENSITY_ACTIONS, ("buy", "sell"))

    def test_canonical_hash_is_row_order_independent_and_content_sensitive(self) -> None:
        rows = [
            _row("f-2", "s-2", [1.0, -0.0]),
            _row("f-1", "s-1", [0.0, 2.0]),
        ]
        self.assertEqual(
            canonical_observations_hash(rows),
            canonical_observations_hash(list(reversed(rows))),
        )
        positive_zero = [dict(rows[0]), dict(rows[1])]
        positive_zero[0]["features"] = [1.0, 0.0]
        self.assertEqual(
            canonical_observations_hash(rows),
            canonical_observations_hash(positive_zero),
        )
        changed = [dict(rows[0]), dict(rows[1])]
        changed[0]["target_probs"] = [0.3, 0.4, 0.3]
        self.assertNotEqual(
            canonical_observations_hash(rows), canonical_observations_hash(changed)
        )

    def test_contract_rejects_nonfinite_duplicate_and_bad_probability_rows(self) -> None:
        with self.assertRaises(DistillationContractError):
            canonical_observations_hash([_row("f", "s", [math.nan])])
        duplicate = [_row("f", "s", [0.0]), _row("f", "s", [1.0])]
        with self.assertRaises(DistillationContractError):
            canonical_observations_hash(duplicate)
        invalid = _row("f", "s", [0.0], [0.2, 0.2, 0.2])
        with self.assertRaises(DistillationContractError):
            canonical_observations_hash([invalid])


class GroupSplitTests(unittest.TestCase):
    def test_stratified_family_split_is_deterministic_balanced_and_leak_free(self) -> None:
        rows = []
        for family_index in range(40):
            stratum = "low" if family_index < 20 else "high"
            for state_index in range(2):
                rows.append(
                    _row(
                        f"family-{family_index:02d}",
                        f"state-{state_index}",
                        [float(family_index), float(state_index)],
                        stratum=stratum,
                    )
                )
        shuffled = list(rows)
        random.Random(991).shuffle(shuffled)
        first = deterministic_group_split(rows, seed=17, stratum_key="stratum")
        second = deterministic_group_split(shuffled, seed=17, stratum_key="stratum")
        self.assertEqual(first.family_assignments, second.family_assignments)
        self.assertEqual(first.train, second.train)
        self.assertEqual(first.validation, second.validation)
        self.assertEqual(first.test, second.test)
        counts = first.to_dict()["counts"]
        self.assertEqual(counts["train_families"], 28)
        self.assertEqual(counts["validation_families"], 6)
        self.assertEqual(counts["test_families"], 6)

        family_sets = [
            {row["family_id"] for row in first.train},
            {row["family_id"] for row in first.validation},
            {row["family_id"] for row in first.test},
        ]
        self.assertFalse(family_sets[0] & family_sets[1])
        self.assertFalse(family_sets[0] & family_sets[2])
        self.assertFalse(family_sets[1] & family_sets[2])
        self.assertEqual(set.union(*family_sets), set(first.family_assignments))

    def test_family_cannot_span_strata(self) -> None:
        rows = [
            _row("same", "one", [0.0], stratum="low"),
            _row("same", "two", [1.0], stratum="high"),
        ]
        with self.assertRaises(DistillationContractError):
            deterministic_group_split(rows, stratum_key="stratum")

    def test_frozen_assignment_survives_an_all_failed_family(self) -> None:
        designed = [
            _row(
                f"family-{index:02d}",
                f"state-{index:02d}",
                [float(index)],
                stratum="shared",
            )
            for index in range(20)
        ]
        planned = deterministic_group_split(
            designed, seed=41, stratum_key="stratum"
        )
        omitted_family = next(
            family
            for family, partition in planned.family_assignments.items()
            if partition == "test"
        )
        eligible = [
            row for row in designed if row["family_id"] != omitted_family
        ]
        actual = apply_frozen_family_assignments(
            eligible,
            family_assignments=planned.family_assignments,
            family_strata=planned.family_strata,
            seed=planned.seed,
            fractions=planned.fractions,
            stratum_key="stratum",
        )
        self.assertEqual(actual.family_assignments, planned.family_assignments)
        self.assertNotIn(omitted_family, {row["family_id"] for row in actual.all_rows()})
        self.assertEqual(
            actual.to_dict()["counts"]["test_families"],
            planned.to_dict()["counts"]["test_families"],
        )

        unknown = [_row("not-planned", "state-x", [1.0], stratum="shared")]
        with self.assertRaises(DistillationContractError):
            apply_frozen_family_assignments(
                unknown,
                family_assignments=planned.family_assignments,
                family_strata=planned.family_strata,
                seed=planned.seed,
                fractions=planned.fractions,
                stratum_key="stratum",
            )

        changed = [dict(eligible[0])]
        changed[0]["stratum"] = "changed-after-freeze"
        with self.assertRaises(DistillationContractError):
            apply_frozen_family_assignments(
                changed,
                family_assignments=planned.family_assignments,
                family_strata=planned.family_strata,
                seed=planned.seed,
                fractions=planned.fractions,
                stratum_key="stratum",
            )


class StandardizerAndPriorTests(unittest.TestCase):
    def test_standardizer_is_fit_only_from_explicit_train_rows(self) -> None:
        standardizer = Standardizer.fit([[0.0, 5.0], [2.0, 5.0]])
        self.assertEqual(standardizer.means, (1.0, 5.0))
        self.assertEqual(standardizer.scales, (1.0, 1.0))
        # A held-out extreme remains extreme; it was not used to refit the scaler.
        self.assertEqual(standardizer.transform([101.0, 5.0]), [100.0, 0.0])
        loaded = Standardizer.from_dict(json.loads(json.dumps(standardizer.to_dict())))
        self.assertEqual(loaded, standardizer)

    def test_action_prior_uses_soft_labels_and_conditional_intensity_weights(self) -> None:
        rows = [
            _row("f1", "s1", [0.0], [0.8, 0.1, 0.1]),
            _row("f2", "s2", [1.0], [0.2, 0.3, 0.5]),
        ]
        rows[0]["intensity_targets"] = [0.2, 0.9]
        rows[0]["intensity_weights"] = [1.0, 0.0]
        rows[1]["intensity_targets"] = [0.8, 0.4]
        rows[1]["intensity_weights"] = [3.0, 2.0]
        prior = ActionPrior.fit(rows)
        self.assertEqual(prior.action_probs, (0.5, 0.2, 0.3))
        self.assertAlmostEqual(prior.intensities[0], 0.65)
        self.assertAlmostEqual(prior.intensities[1], 0.4)
        loaded = ActionPrior.from_dict(json.loads(json.dumps(prior.to_dict())))
        self.assertEqual(loaded, prior)
        self.assertEqual(loaded.predict([999.0]), prior.predict([0.0]))


class StudentTrainingTests(unittest.TestCase):
    def test_linear_and_mlp_learn_beyond_prior_and_json_round_trip(self) -> None:
        rows = _learnable_rows()
        prior_metrics = evaluate_model(ActionPrior.fit(rows), rows)

        standardizer = Standardizer.fit(rows)
        linear = LinearSoftmaxStudent(2, seed=7)
        linear_history = linear.fit(
            rows,
            standardizer,
            epochs=240,
            learning_rate=0.03,
        )
        mlp = TanhMLPStudent(2, hidden_dim=8, seed=7)
        mlp_history = mlp.fit(
            rows,
            standardizer,
            epochs=240,
            learning_rate=0.03,
        )
        linear_metrics = evaluate_model(linear, rows)
        mlp_metrics = evaluate_model(mlp, rows)
        self.assertLess(linear_history["loss"][-1], linear_history["loss"][0])
        self.assertLess(mlp_history["loss"][-1], mlp_history["loss"][0])
        self.assertLess(
            linear_metrics["action_cross_entropy"],
            prior_metrics["action_cross_entropy"],
        )
        self.assertLess(
            mlp_metrics["action_cross_entropy"],
            linear_metrics["action_cross_entropy"],
        )

        for model, loader in (
            (linear, LinearSoftmaxStudent.from_dict),
            (mlp, TanhMLPStudent.from_dict),
        ):
            serialized = json.loads(
                json.dumps(model.to_dict(), sort_keys=True, allow_nan=False)
            )
            loaded = loader(serialized)
            for features in ([-0.8, 0.64], [0.0, 0.0], [0.8, 0.64]):
                self.assertEqual(loaded.predict(features), model.predict(features))
                prediction = loaded.predict(features)
                self.assertAlmostEqual(sum(prediction["action_probs"]), 1.0)
                self.assertTrue(
                    all(math.isfinite(value) for value in prediction["action_probs"])
                )
                self.assertTrue(
                    all(0.0 <= value <= 1.0 for value in prediction["intensities"])
                )
            self.assertEqual(loaded.model_hash(), model.model_hash())

    def test_fixed_seed_and_rows_produce_identical_mlp(self) -> None:
        rows = _learnable_rows()
        first = TanhMLPStudent(2, hidden_dim=6, seed=101)
        second = TanhMLPStudent(2, hidden_dim=6, seed=101)
        first.fit(rows, epochs=80, learning_rate=0.02)
        second.fit(list(reversed(rows)), epochs=80, learning_rate=0.02)
        # Training itself is intentionally order-insensitive: canonical
        # normalization plus full-batch sums should yield the same model.
        self.assertEqual(first.to_dict(), second.to_dict())

    def test_metrics_include_soft_ce_brier_accuracy_and_conditional_mae(self) -> None:
        row = _row("f", "s", [0.0], [1.0, 0.0, 0.0])
        metrics = evaluate_predictions(
            [row],
            [{"action_probs": [0.8, 0.1, 0.1], "intensities": [0.5, 0.5]}],
        )
        self.assertAlmostEqual(metrics["action_cross_entropy"], -math.log(0.8))
        self.assertAlmostEqual(metrics["action_brier"], 0.06)
        self.assertEqual(metrics["action_accuracy"], 1.0)
        self.assertAlmostEqual(metrics["conditional_intensity_mae"], 0.25)


class OODDiagnosticTests(unittest.TestCase):
    def test_default_threshold_is_frozen_in_machine_readable_descriptor(self) -> None:
        reference = build_ood_reference([[-1.0], [0.0], [1.0]])
        diagnostics = ood_diagnostics([[0.0]], reference)
        descriptor = ood_diagnostic_descriptor()
        self.assertEqual(diagnostics["z_threshold"], descriptor["z_threshold"])
        self.assertEqual(descriptor["z_threshold"], 3.0)
        self.assertFalse(descriptor["joint_support_assessed"])
        self.assertTrue(descriptor["diagnostic_only"])

    def test_train_reference_flags_range_and_z_exceedances(self) -> None:
        reference = build_ood_reference([[-1.0, 5.0], [0.0, 5.0], [1.0, 5.0]])
        diagnostics = ood_diagnostics(
            [[0.0, 5.0], [4.0, 5.0]], reference, z_threshold=2.0
        )
        self.assertEqual(diagnostics["outside_train_range_rows"], 1)
        self.assertEqual(diagnostics["outside_train_range_by_feature"], [1, 0])
        self.assertEqual(diagnostics["z_exceedances_by_feature"], [1, 0])
        self.assertGreater(diagnostics["max_abs_z"], 2.0)
        loaded = OODReference.from_dict(
            json.loads(json.dumps(reference.to_dict(), allow_nan=False))
        )
        self.assertEqual(loaded, reference)


if __name__ == "__main__":
    unittest.main()
