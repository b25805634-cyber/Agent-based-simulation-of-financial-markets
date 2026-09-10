"""Manufactured fixtures for early human-task reconstruction, not human evidence."""
import copy
import json
import unittest
from unittest.mock import patch

from nmsim.human_early_tasks import (
    TASK_SCHEMA, build_early_tasks, evaluate_joint_predictions,
    hold_baseline_predictions, parse_joint_response,
)
from tests.test_human_reference import fixture_records


def response(quantities=None, reason="PRIVATE_REASONING_SENTINEL"):
    return json.dumps({"signed_quantities": quantities or dict.fromkeys("abcdef", 0),
                       "private_reasoning": reason})


class HumanEarlyTasksTest(unittest.TestCase):
    def setUp(self):
        self.records = fixture_records()
        self.tasks = build_early_tasks(self.records)

    def test_only_control_first_five_main_rounds_and_zero_rankings(self):
        treatment = fixture_records("treatment-fixture")
        for row in treatment:
            row["participant._current_app_name"] = "spt1"
        tasks = build_early_tasks(self.records + treatment)
        self.assertEqual(len(tasks), 5)
        self.assertEqual([t["source_origin"]["source_raw_round"] for t in tasks], [4, 5, 6, 7, 8])
        self.assertEqual([t["state"]["main_round"] for t in tasks], [1, 2, 3, 4, 5])
        for task in tasks:
            self.assertEqual(task["schema"], TASK_SCHEMA)
            self.assertEqual(task["protocol"]["realized_peer_rankings_in_window"], 0)
            self.assertFalse(task["protocol"]["original_ui_fully_recovered"])
            self.assertFalse(task["protocol"]["human_likeness_validated"])
            self.assertIn("After paid rounds 5, 10 and 14", task["prompt"])

    def test_price_clock_reset_and_memory_are_explicit(self):
        first, second = self.tasks[:2]
        self.assertEqual([h["published_price_index"] for h in first["state"]["observed_price_history"]],
                         [-3, -2, -1, 0])
        self.assertEqual(first["state"]["cash_before_talers"], 10000)
        self.assertEqual(sum(first["state"]["holdings_before"].values()), 0)
        self.assertEqual([h["phase"] for h in first["state"]["own_prior_trades"]], ["practice"] * 3)
        self.assertEqual(second["state"]["cash_before_talers"], 100)
        self.assertEqual(second["state"]["holdings_before"]["a"], 99)
        self.assertEqual(second["state"]["own_prior_trades"][-1]["signed_quantities"]["a"], 99)
        for task in self.tasks:
            now = task["state"]["current_published_price_index"]
            self.assertTrue(all(h["published_price_index"] <= now for h in task["state"]["observed_price_history"]))
            self.assertTrue(all(h["published_price_index"] < now for h in task["state"]["own_prior_trades"]))

    def test_source_identity_demographics_and_outcomes_never_enter_prompt(self):
        changed = copy.deepcopy(self.records)
        for row in changed:
            row["participant.code"] = "DIFFERENT_SUBJECT_SENTINEL"
            row["session.code"] = "DIFFERENT_SESSION_SENTINEL"
            row["group.id_in_subsession"] = "99"
            row["player.age"] = "DEMOGRAPHIC_SENTINEL"
            row["player.result_final"] = "123456789"
            row["player.total_count_guess"] = "GUESS_SENTINEL"
        other = build_early_tasks(changed)
        self.assertEqual([t["prompt"] for t in self.tasks], [t["prompt"] for t in other])
        self.assertNotEqual(self.tasks[0]["task_id"], other[0]["task_id"])
        for task in other:
            self.assertNotIn("SENTINEL", task["prompt"])
            self.assertNotIn("result_final", task["prompt"])
            self.assertNotIn("human_action", task["prompt"])
            self.assertNotIn("source_origin", task["prompt"])
            self.assertIn("not told which stock", task["prompt"])

    def test_current_label_does_not_leak_and_future_changes_do_not_change_tasks(self):
        changed = copy.deepcopy(self.records)
        changed[7]["player.quantity_buy_c"] = "1"
        changed[7]["player.quantity_c"] = "1"
        for row in changed[8:]:
            row["player.quantity_pre_c"] = "1"
            row["player.quantity_c"] = "1"
        other = build_early_tasks(changed)
        self.assertEqual([t["prompt"] for t in self.tasks], [t["prompt"] for t in other])
        self.assertNotEqual(self.tasks[-1]["human_action"], other[-1]["human_action"])
        later = copy.deepcopy(self.records)
        later[-1]["player.price_a"] = "999"
        self.assertEqual(self.tasks, build_early_tasks(later))

    def test_source_order_stability_no_input_mutation_or_network(self):
        before = copy.deepcopy(self.records)
        with patch("socket.socket", side_effect=AssertionError("network forbidden")):
            tasks = build_early_tasks(list(reversed(self.records)))
            evaluate_joint_predictions(tasks, hold_baseline_predictions(tasks))
        self.assertEqual(tasks, self.tasks)
        self.assertEqual(before, self.records)
        self.assertEqual(len({t["task_id"] for t in tasks}), 5)
        json.dumps(tasks, allow_nan=False)

    def test_parser_settles_joint_sales_and_purchases_exactly(self):
        task = self.tasks[1]
        parsed = parse_joint_response(response(task["human_action"]["signed_quantities"]), task)
        public = parsed["public_decision"]
        self.assertEqual(public["cash_after_talers"], 0)
        self.assertEqual(public["holdings_after"]["a"], 89)
        self.assertEqual(public["holdings_after"]["b"], 11)
        self.assertEqual(public["gross_buy_cost_talers"], 1100)
        self.assertEqual(public["gross_sell_proceeds_talers"], 1000)
        self.assertEqual(parsed["private_rationale"], "PRIVATE_REASONING_SENTINEL")
        self.assertNotIn("PRIVATE_REASONING_SENTINEL", json.dumps(public))
        before = copy.deepcopy(task)
        parse_joint_response(response(), task)
        self.assertEqual(task, before)

    def test_rejects_short_sale_and_joint_budget_violation(self):
        for action, pattern in (({"a": -1}, "shares held"), ({"a": 101}, "net cash")):
            quantities = dict.fromkeys("abcdef", 0)
            quantities.update(action)
            with self.assertRaisesRegex(ValueError, pattern):
                parse_joint_response(response(quantities), self.tasks[0])
        quantities = dict.fromkeys("abcdef", 0)
        quantities.update({"a": -10, "b": 12})
        with self.assertRaisesRegex(ValueError, "net cash"):
            parse_joint_response(response(quantities), self.tasks[1])

    def test_strict_integer_schema_rejects_bool_float_missing_extra(self):
        for invalid in (True, 1.0, "1", None):
            quantities = dict.fromkeys("abcdef", 0)
            quantities["a"] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                parse_joint_response(response(quantities), self.tasks[0])
        for quantities in ({"a": 0}, {**dict.fromkeys("abcdef", 0), "g": 0}):
            with self.assertRaises(ValueError):
                parse_joint_response(response(quantities), self.tasks[0])
        for raw in ('[]', 'null', '{}', '{"signed_quantities": {}}', response().replace('"a": 0', '"a": NaN'),
                    response().replace('"private_reasoning": "PRIVATE_REASONING_SENTINEL"', '"private_reasoning": 7'),
                    response().replace('"a": 0', '"a": 0, "a": 1'),
                    response().replace('"signed_quantities":', '"extra": 1, "signed_quantities":'),
                    '```json\n' + response() + '\n```', response() + ' trailing', None):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_joint_response(raw, self.tasks[0])

    def test_bad_task_state_is_rejected(self):
        for kind in ("schema", "cash", "prices", "holdings"):
            bad = copy.deepcopy(self.tasks[0])
            if kind == "schema": bad["schema"] = "wrong"
            if kind == "cash": bad["state"]["cash_before_talers"] = True
            if kind == "prices": bad["state"]["current_prices_talers"]["a"] = 0
            if kind == "holdings": bad["state"]["holdings_before"]["a"] = -1
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                parse_joint_response(response(), bad)

    def test_exact_predictions_have_exact_agreement_but_no_human_validity_claim(self):
        predictions = [{"task_id": task["task_id"],
                        "raw_response": response(task["human_action"]["signed_quantities"])}
                       for task in self.tasks]
        report = evaluate_joint_predictions(self.tasks, predictions)
        self.assertEqual(report["valid_joint_predictions"], 5)
        self.assertEqual(report["valid_coverage"], 1)
        self.assertEqual(report["paired_valid_comparison"]["exact_joint_fraction"], 1)
        self.assertEqual(report["paired_valid_comparison"]["mean_absolute_signed_share_error"], 0)
        self.assertFalse(report["human_likeness_validated"])
        self.assertNotIn("PRIVATE_REASONING_SENTINEL", json.dumps(report))
        self.assertNotIn("engineering-fixture-subject", json.dumps(report))

    def test_hold_control_has_independently_calculated_fixture_agreement(self):
        report = evaluate_joint_predictions(self.tasks, hold_baseline_predictions(self.tasks))
        scores = report["paired_valid_comparison"]
        self.assertEqual(scores["human_stock_actions"], {"buy": 2, "hold": 25, "sell": 3})
        self.assertEqual(scores["model_stock_actions"], {"buy": 0, "hold": 30, "sell": 0})
        self.assertEqual(scores["exact_joint_fraction"], 2 / 5)
        self.assertEqual(scores["stock_action_agreement"], 25 / 30)
        self.assertAlmostEqual(scores["mean_absolute_signed_share_error"], 220 / 30)
        self.assertEqual(report["units"]["source_subjects"], 1)
        self.assertEqual(report["units"]["joint_decision_tasks"], 5)
        self.assertEqual(report["units"]["stock_opportunities"], 30)

    def test_invalid_missing_unknown_and_duplicate_predictions_remain_explicit(self):
        predictions = hold_baseline_predictions(self.tasks[:2])
        predictions[1]["raw_response"] = "not JSON PRIVATE_REASONING_SENTINEL"
        report = evaluate_joint_predictions(self.tasks, predictions)
        self.assertEqual(report["valid_joint_predictions"], 1)
        self.assertEqual(report["invalid_joint_predictions"], 1)
        self.assertEqual(report["missing_predictions"], 3)
        self.assertEqual(report["paired_valid_comparison"]["denominator_joint_decisions"], 1)
        self.assertNotIn("PRIVATE_REASONING_SENTINEL", json.dumps(report))
        for invalid in (predictions[:1] * 2, [{"task_id": "unknown", "raw_response": response()}],
                        [{"task_id": self.tasks[0]["task_id"], "raw_response": response(), "extra": 1}]):
            with self.assertRaises(ValueError):
                evaluate_joint_predictions(self.tasks, invalid)
        with self.assertRaises(ValueError):
            evaluate_joint_predictions(self.tasks * 2, [])

    def test_no_valid_pairs_is_missing_metric_not_zero(self):
        report = evaluate_joint_predictions(self.tasks, [])
        self.assertEqual(report["valid_coverage"], 0)
        self.assertIsNone(report["paired_valid_comparison"]["exact_joint_fraction"])
        self.assertIsNone(report["paired_valid_comparison"]["mean_absolute_signed_share_error"])
        self.assertIsNone(evaluate_joint_predictions([], [])["valid_coverage"])


if __name__ == "__main__":
    unittest.main()
