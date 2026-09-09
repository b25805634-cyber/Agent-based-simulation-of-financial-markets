"""Numerical score tests use fixtures only, not actual recruited subjects."""
import json
import math
from unittest import TestCase, mock

from nmsim import human_benchmark as benchmark
from nmsim.information_weight import ACCOUNT8, FORBIDDEN_PROMPT_TERMS


def response(task, subject="subject-1", action="buy", intensity=0.5, source="human"):
    # This fixture exercises the declared-human branch; no run is created.
    return {"subject_id": subject, "task_id": task["task_id"], "action": action,
            "intensity": intensity, "source_kind": source}


def prediction(task, probabilities=(0.5, 0, 0.5), intensities=(0.5, 0.5), source="model"):
    return {"task_id": task["task_id"], "action_probs": list(probabilities),
            "intensities": list(intensities), "source_kind": source}


class HumanBenchmarkTests(TestCase):
    def setUp(self):
        self.tasks = benchmark.build_tasks()

    def test_tasks_are_neutral_paired_numeric_design_not_responses(self):
        self.assertEqual(len(self.tasks), 24)
        self.assertEqual(len({task["pair_id"] for task in self.tasks}), 12)
        for task in self.tasks:
            self.assertEqual(len(task["visible_fields"]), 12)
            self.assertEqual(set(task["account_state"]), set(ACCOUNT8))
            self.assertNotIn("action", task)
            self.assertEqual(task["source_kind"], "synthetic_task_not_human_response")
            for term in FORBIDDEN_PROMPT_TERMS:
                self.assertNotIn(term.lower(), task["instructions"].lower())
        self.assertEqual(benchmark.build_tasks(), self.tasks)
        self.assertNotEqual(benchmark.build_tasks(12), self.tasks)

    def test_json_roundtrip_does_not_depend_on_dictionary_order(self):
        tasks = json.loads(json.dumps(self.tasks, sort_keys=True))
        score = benchmark.score_responses(tasks, [], [])
        self.assertEqual(score["status"], "pending_human_responses")
        self.assertEqual(score["human_participants"], 0)
        self.assertFalse(score["human_likeness_validated"])

    def test_vignette_contrasts_only_change_named_numeric_conditions(self):
        first, second = self.tasks[0:2]
        self.assertEqual(first["account_state"], second["account_state"])
        self.assertNotEqual(set(first["visible_fields"]), set(second["visible_fields"]))
        first, second = self.tasks[4:6]
        self.assertEqual(first["visible_fields"], second["visible_fields"])
        differing = {key for key in first["account_state"] if first["account_state"][key] != second["account_state"][key]}
        self.assertEqual(differing, {"position_fraction"})

    def test_synthetic_fixtures_are_never_counted_as_people_or_models(self):
        result = benchmark.score_responses(self.tasks,
                                           [response(self.tasks[0], source="synthetic_fixture")],
                                           [prediction(self.tasks[0], source="synthetic_fixture")])
        self.assertEqual(result["human_participants"], 0)
        self.assertEqual(result["human_responses"], 0)
        self.assertEqual(result["excluded_synthetic_response_count"], 1)
        self.assertEqual(result["excluded_synthetic_prediction_count"], 1)
        self.assertEqual(result["status"], "pending_human_responses")
        self.assertEqual(result["per_task"], [])

    def test_distribution_nll_brier_and_intensity_metrics_known_values(self):
        task = self.tasks[0]
        humans = [response(task, "s1", "buy", 0.4), response(task, "s2", "sell", 0.6)]
        result = benchmark.score_responses(self.tasks, humans, [prediction(task, intensities=(0.4, 0.6))])
        row = result["per_task"][0]
        self.assertAlmostEqual(row["js_divergence_nats"], 0)
        self.assertAlmostEqual(row["mean_nll_nats"], math.log(2))
        self.assertAlmostEqual(row["mean_brier_sum_three_classes"], 0.5)
        self.assertEqual(row["conditional_intensity_mae"], {"buy": 0, "sell": 0})
        self.assertEqual(result["human_participants"], 2)
        self.assertEqual(result["scored_human_responses"], 2)
        self.assertFalse(result["human_likeness_validated"])
        self.assertEqual(len(result["missing_human_task_ids"]), 23)

    def test_zero_predicted_probability_is_infinite_not_silently_clamped(self):
        task = self.tasks[0]
        result = benchmark.score_responses(self.tasks, [response(task)], [prediction(task, (0, 1, 0))])
        self.assertIsNone(result["response_weighted_nll_nats"])
        self.assertEqual(result["zero_predicted_probability_responses"], 1)
        self.assertEqual(result["per_task"][0]["nll_status"], "infinite_zero_predicted_probability")
        self.assertAlmostEqual(result["per_task"][0]["js_divergence_nats"], math.log(2))
        json.dumps(result, allow_nan=False)

    def test_paired_contrast_counts_participants_not_independent_answers(self):
        first, second = self.tasks[:2]
        human = [response(first, action="sell", intensity=0.5),
                 response(second, action="buy", intensity=0.5),
                 response(first, subject="incomplete", action="hold", intensity=0)]
        result = benchmark.score_responses(self.tasks, human,
                                           [prediction(first, (0, 0, 1)), prediction(second, (1, 0, 0))])
        contrast = result["paired_contrasts"][0]
        self.assertEqual(contrast["complete_human_participants"], 1)
        self.assertEqual(contrast["human_mean_signed_action_change"], 2)
        self.assertEqual(contrast["model_expected_signed_action_change"], 2)
        self.assertEqual(contrast["human_mean_signed_intensity_change"], 1)
        self.assertEqual(result["human_participants"], 2)
        self.assertEqual(result["human_responses"], 3)

    def test_missing_predictions_are_pending_not_passed(self):
        result = benchmark.score_responses(self.tasks, [response(self.tasks[0])], [])
        self.assertEqual(result["status"], "pending_matching_predictions")
        self.assertIsNone(result["response_weighted_brier"])

    def test_duplicates_pii_extra_fields_invalid_labels_fail(self):
        original = response(self.tasks[0])
        for rows in [[original, original],
                     [{**original, "subject_id": "name@example.com"}],
                     [{**original, "reasoning": "private"}],
                     [{**original, "source_kind": "teacher"}],
                     [{**original, "action": "hold", "intensity": 0.5}],
                     [{**original, "intensity": math.nan}],
                     [{**original, "task_id": "missing"}]]:
            with self.assertRaises(ValueError):
                benchmark.score_responses(self.tasks, rows, [])
        model = prediction(self.tasks[0])
        with self.assertRaises(ValueError):
            benchmark.score_responses(self.tasks, [], [model, model])

    def test_output_contains_aggregates_not_subject_identifiers(self):
        result = benchmark.score_responses(self.tasks,
                                           [response(self.tasks[0], subject="anonymous-sensitive-id")],
                                           [prediction(self.tasks[0])])
        self.assertNotIn("anonymous-sensitive-id", json.dumps(result))

    def test_no_files_network_or_execution_context_needed_for_pure_test(self):
        with mock.patch("socket.socket", side_effect=AssertionError("network")), \
                mock.patch("builtins.open", side_effect=AssertionError("file")):
            tasks = benchmark.build_tasks()
            score = benchmark.score_responses(tasks, [], [])
        self.assertEqual(score["human_responses"], 0)
