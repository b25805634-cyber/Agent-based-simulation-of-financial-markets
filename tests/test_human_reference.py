"""Source-specific accounting/timing fixtures; these are not human evidence."""
import copy
import csv
import io
import json
import unittest

from nmsim.human_reference import (
    ASSETS, SOURCE_COLUMNS, audit_records, decode_csv, reconstruct_decisions,
)


def fixture_records(participant="engineering-fixture-subject"):
    rows = []
    holdings = {a: 0 for a in ASSETS}
    cash = 10000
    for period in range(1, 18):
        if period in (1, 4):
            holdings = {a: 0 for a in ASSETS}
            cash = 10000
        row = {key: "" for key in SOURCE_COLUMNS}
        row.update({"": str(period), "participant.code": participant,
                    "session.code": "engineering-fixture-session",
                    "group.id_in_subsession": "1",
                    "participant._current_app_name": "spt2",
                    "subsession.round_number": str(period),
                    "player.total_count_guess": "0"})
        buys = {a: 0 for a in ASSETS}
        sells = {a: 0 for a in ASSETS}
        if period == 1:
            buys["a"] = 1
        if period == 2:
            sells["a"] = 1
        if period == 4:
            buys["a"] = 99
        if period == 5:
            sells["a"] = 10
            buys["b"] = 11
        if period == 6:
            sells["a"] = 89
            sells["b"] = 11
        for a in ASSETS:
            row[f"player.quantity_pre_{a}"] = str(holdings[a])
            row[f"player.quantity_buy_{a}"] = str(buys[a])
            row[f"player.quantity_sell_{a}"] = str(sells[a])
            row[f"player.price_{a}"] = "100"
            holdings[a] += buys[a] - sells[a]
            row[f"player.quantity_{a}"] = str(holdings[a])
            cash += 100 * (sells[a] - buys[a])
        row["player.result_final"] = str(cash + 100 * sum(holdings.values()))
        rows.append(row)
    return rows


class HumanReferenceTest(unittest.TestCase):
    def test_exact_source_csv_header_and_raw_missing_values(self):
        rows = fixture_records()
        rows[0]["player.age"] = "NA"
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=SOURCE_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
        decoded = decode_csv(stream.getvalue())
        self.assertEqual(decoded, rows)
        self.assertEqual(len(SOURCE_COLUMNS), 66)
        self.assertEqual(decoded[0]["player.age"], "NA")
        self.assertEqual(decoded[1]["player.age"], "")

    def test_rejects_reordered_duplicate_and_ragged_csv(self):
        with self.assertRaises(ValueError):
            decode_csv("a,a\n1,2\n")
        header = ",".join(SOURCE_COLUMNS)
        with self.assertRaises(ValueError):
            decode_csv(header + "\n1,2\n")
        with self.assertRaises(ValueError):
            decode_csv(123)

    def test_practice_and_main_have_separate_units_and_reset(self):
        rows = fixture_records()
        audit = audit_records(rows)
        decisions = reconstruct_decisions(rows)
        self.assertEqual(audit["human_subjects"], 1)
        self.assertEqual(audit["practice_subject_periods"], 3)
        self.assertEqual(audit["main_subject_periods"], 14)
        self.assertEqual(audit["main_stock_opportunities"], 84)
        self.assertEqual(decisions[3]["main_round"], 1)
        self.assertEqual(decisions[3]["state"]["cash_before_talers"], 10000)
        self.assertEqual(sum(decisions[3]["state"]["holdings_before"].values()), 0)
        self.assertFalse(audit["scientific_scope"]["human_likeness_validated"])
        self.assertFalse(audit["scientific_scope"]["current_single_asset_benchmark_compatible"])

    def test_joint_sale_finances_other_asset_without_scalar_intensity(self):
        decision = reconstruct_decisions(fixture_records())[4]
        self.assertEqual(decision["state"]["cash_before_talers"], 100)
        self.assertEqual(decision["joint_action"]["a"]["action"], "sell")
        self.assertEqual(decision["joint_action"]["b"]["action"], "buy")
        self.assertEqual(decision["settlement"]["gross_buy_cost_talers"], 1100)
        self.assertEqual(decision["settlement"]["gross_sell_proceeds_talers"], 1000)
        self.assertEqual(decision["settlement"]["cash_after_talers"], 0)
        self.assertTrue(decision["settlement"]["buy_cost_exceeds_start_cash"])
        self.assertNotIn("intensity", json.dumps(decision))
        audit = audit_records(fixture_records())
        self.assertEqual(audit["main_gross_buy_exceeds_start_cash_count"], 1)
        self.assertEqual(audit["status"], "input_audit_passed")

    def test_future_result_final_never_changes_any_reconstructed_decision(self):
        rows = fixture_records()
        before = reconstruct_decisions(rows)
        changed = copy.deepcopy(rows)
        for row in changed:
            row["player.result_final"] = str(int(row["player.result_final"]) + 123456)
        self.assertEqual(before, reconstruct_decisions(changed))
        self.assertNotIn("result_final", json.dumps(before))
        audit = audit_records(changed)
        self.assertEqual(audit["status"], "input_audit_failed")
        self.assertEqual(audit["next_price_portfolio_value_audit"]["mismatches"], 16)
        self.assertFalse(audit["next_price_portfolio_value_audit"]["result_final_used_in_predecision_state"])

    def test_later_prices_and_actions_do_not_enter_earlier_states(self):
        rows = fixture_records()
        before = reconstruct_decisions(rows)
        rows[-1]["player.price_a"] = "200"
        after = reconstruct_decisions(rows)
        self.assertEqual(before[:-1], after[:-1])
        for decision in after:
            raw_round = decision["source_raw_round"]
            self.assertTrue(all(x["raw_round"] <= raw_round for x in decision["state"]["recorded_price_history"]))
            self.assertTrue(all(x["raw_round"] < raw_round for x in decision["state"]["recorded_own_trade_history"]))

    def test_exact_valuation_audit_and_unobserved_last_price(self):
        audit = audit_records(fixture_records())
        valuation = audit["next_price_portfolio_value_audit"]
        self.assertEqual(valuation["exact_matches"], 16)
        self.assertEqual(valuation["mismatches"], 0)
        self.assertEqual(valuation["unverified_final_subject_periods"], 1)
        self.assertEqual(audit["checks"]["stock_holding_balance"], 17 * 6)
        self.assertEqual(audit["checks"]["stock_holdings_continuity"], 15 * 6)

    def test_cash_is_independently_reconstructed_from_execution_prices(self):
        rows = fixture_records()
        rows[3]["player.result_final"] = "10099"
        self.assertEqual(reconstruct_decisions(rows)[4]["state"]["cash_before_talers"], 100)
        self.assertEqual(reconstruct_decisions(rows)[3]["state"]["portfolio_value_before_talers"], 10000)

    def test_negative_cash_fails_closed(self):
        rows = fixture_records()
        rows[-1]["player.quantity_buy_a"] = "101"
        rows[-1]["player.quantity_a"] = "101"
        audit = audit_records(rows)
        self.assertEqual(audit["accounting_violations"]["negative_joint_net_cash"], 1)
        with self.assertRaisesRegex(ValueError, "negative_joint_net_cash"):
            reconstruct_decisions(rows)

    def test_holdings_balance_continuity_and_reset_fail_closed(self):
        for index, key in ((3, "player.quantity_pre_a"), (8, "player.quantity_pre_a"),
                           (8, "player.quantity_a"), (8, "player.quantity_sell_a")):
            with self.subTest(index=index, key=key):
                rows = fixture_records()
                rows[index][key] = "7"
                self.assertEqual(audit_records(rows)["status"], "input_audit_failed")
                with self.assertRaises(ValueError):
                    reconstruct_decisions(rows)

    def test_unknown_condition_missing_round_duplicate_key_rejected(self):
        variants = []
        rows = fixture_records()
        rows[0]["participant._current_app_name"] = "other"
        variants.append(rows)
        variants.append(fixture_records()[1:])
        rows = fixture_records()
        rows.append(copy.deepcopy(rows[0]))
        variants.append(rows)
        for variant in variants:
            with self.assertRaises(ValueError):
                audit_records(variant)

    def test_missing_trade_values_are_not_zero_imputed(self):
        for bad in ("NA", "", "1.5", " 1", "01", 1, True):
            rows = fixture_records()
            rows[0]["player.quantity_buy_a"] = bad
            with self.assertRaises(ValueError):
                reconstruct_decisions(rows)

    def test_summary_excludes_identifiers_and_input_is_unchanged(self):
        rows = fixture_records()
        before = copy.deepcopy(rows)
        encoded = json.dumps(audit_records(rows))
        self.assertNotIn("engineering-fixture-subject", encoded)
        self.assertNotIn("engineering-fixture-session", encoded)
        reconstruct_decisions(rows)
        self.assertEqual(rows, before)

    def test_order_is_canonical_and_peer_groups_are_clustered(self):
        records = sum((fixture_records(f"subject-{i}") for i in range(3)), [])
        self.assertEqual(reconstruct_decisions(records), reconstruct_decisions(list(reversed(records))))
        audit = audit_records(records)
        self.assertEqual(audit["human_subjects"], 3)
        self.assertEqual(audit["peer_groups"], 1)
        self.assertEqual(audit["peer_group_size_counts"], {"3": 1})


if __name__ == "__main__":
    unittest.main()
