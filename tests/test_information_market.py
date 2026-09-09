"""Causal and accounting checks for the new pure market, not research runs."""
from __future__ import annotations

import json
import math
from unittest import TestCase, mock

from nmsim import information_market as market
from nmsim.information_weight import ACCOUNT8, FIELD_RANGES, P8, PROFILE_FIELDS, PROFILE_IDS
from nmsim.v2_attention import V2AttentionState
from nmsim.v2_market_experiment import _limit_price


class InformationMarketTests(TestCase):
    def test_finite_settlement_conservation_and_order_limits_every_round(self):
        result = market.run_market(market.random_policy, agents=20, rounds=20, seed=14)
        summary = result["summary"]
        before = {a["agent_id"]: a for a in result["initial_accounts"]}
        matched = 0
        for row in result["ledger"]:
            for order in row["clearing"]["accepted_orders"]:
                account = before[order["agent_id"]]
                if order["side"] == "buy":
                    self.assertLessEqual(order["quantity"] * order["limit_price_cents"], account["cash_cents"])
                else:
                    self.assertLessEqual(order["quantity"], account["shares"])
            for fill in row["clearing"]["fills"]:
                self.assertTrue(fill["price_cents"] <= fill["limit_price_cents"]
                                if fill["side"] == "buy" else fill["price_cents"] >= fill["limit_price_cents"])
            after = row["accounts_after"]
            self.assertEqual(sum(a["cash_cents"] for a in after), summary["initial_cash_cents"])
            self.assertEqual(sum(a["shares"] for a in after), summary["initial_shares"])
            self.assertTrue(all(a["cash_cents"] >= 0 and a["shares"] >= 0 and a["debt_cents"] == 0 for a in after))
            before = {a["agent_id"]: a for a in after}
            matched += row["clearing"]["matched_volume_shares"]
        self.assertGreater(matched, 0)
        self.assertEqual(summary["matched_volume_shares"], matched)

    def test_hold_and_one_sided_null_do_not_manufacture_price_or_volume(self):
        for probabilities in [(0, 1, 0), (1, 0, 0), (0, 0, 1)]:
            result = market.run_market(market.prior_policy(probabilities), agents=8, rounds=6)
            self.assertEqual(result["summary"]["matched_volume_shares"], 0)
            self.assertEqual(result["summary"]["initial_price_cents"], result["summary"]["final_price_cents"])
            self.assertEqual(result["initial_accounts"], result["ledger"][-1]["accounts_after"])

    def test_policy_gets_only_visible_numeric_fields_and_own_account(self):
        observed = []
        def policy(visible, account):
            observed.append((dict(visible), dict(account)))
            self.assertIn(tuple(visible), PROFILE_FIELDS.values())
            self.assertEqual(set(account), set(ACCOUNT8))
            self.assertTrue(all(isinstance(x, (int, float)) for x in (*visible.values(), *account.values())))
            self.assertNotIn("profile_id", visible)
            # Deliberate mutation must not change another agent or recorded view.
            visible.clear()
            account.clear()
            return {"action_probs": [0, 1, 0], "intensities": [0, 0],
                    "reasoning": "SECRET_PRIVATE_REASONING", "api_key": "SECRET_API_KEY"}
        result = market.run_market(policy, agents=8, rounds=2)
        self.assertEqual(len(observed), 16)
        encoded = json.dumps(result, allow_nan=False)
        self.assertNotIn("SECRET_PRIVATE_REASONING", encoded)
        self.assertNotIn("SECRET_API_KEY", encoded)
        self.assertTrue(all(len(probe["visible_fields"]) == 12 for probe in result["state_probes"]))

    def test_world_and_account_initialization_paired_across_profiles_and_policies(self):
        first = market.run_market(market.random_policy, agents=8, rounds=11, seed=8,
                                  profile_counts={PROFILE_IDS[0]: 8})
        second = market.run_market(market.prior_policy((0, 1, 0)), agents=8, rounds=11, seed=8,
                                   profile_counts={PROFILE_IDS[2]: 8})
        self.assertEqual(first["world_schedule"], second["world_schedule"])
        self.assertEqual(first["world_hash"], second["world_hash"])
        self.assertEqual(first["initial_accounts"], second["initial_accounts"])
        self.assertEqual(first["warmup"], second["warmup"])
        self.assertNotEqual(set(first["state_probes"][0]["visible_fields"]),
                            set(second["state_probes"][0]["visible_fields"]))

    def test_information_selection_causally_changes_policy_inputs_and_actions(self):
        def policy(visible, account):
            return {"action_probs": [1, 0, 0] if "source_disagreement" in visible else [0, 0, 1],
                    "intensities": [0.5, 0.5]}
        price = market.run_market(policy, agents=8, rounds=2, profile_counts={PROFILE_IDS[0]: 8})
        news = market.run_market(policy, agents=8, rounds=2, profile_counts={PROFILE_IDS[2]: 8})
        self.assertEqual(price["summary"]["action_counts"], {"sell": 16})
        self.assertEqual(news["summary"]["action_counts"], {"buy": 16})

    def test_world_prefix_and_rollout_have_no_lookahead(self):
        short = market.run_market(market.random_policy, agents=8, rounds=5, seed=9)
        long = market.run_market(market.random_policy, agents=8, rounds=14, seed=9)
        self.assertEqual(short["world_schedule"], long["world_schedule"][:5])
        self.assertEqual(short["ledger"], long["ledger"][:5])
        for row in long["ledger"]:
            self.assertLessEqual(row["report_release_day"], row["information_cutoff_day"])
            self.assertLessEqual(row["event_release_day"], row["information_cutoff_day"])
            self.assertEqual(row["settlement_day"], row["decision_day"] + 1)

    def test_company_balance_sheet_and_price_sensitive_ratios(self):
        world = market.build_world(seed=7, rounds=21, total_shares=300)
        self.assertEqual(world[0]["report"], world[9]["report"])
        self.assertNotEqual(world[9]["report"], world[10]["report"])
        for day in world:
            report = day["report"]
            self.assertEqual(report["common_equity_cents"] + report["total_liabilities_cents"], report["total_assets_cents"])
            self.assertGreater(report["revenue_ttm_cents"], 0)
            low = market.fundamental_fields(report, 10000, 300)
            high = market.fundamental_fields(report, 20000, 300)
            self.assertAlmostEqual(low["earnings_yield_ttm"], 2 * high["earnings_yield_ttm"])
            self.assertAlmostEqual(low["book_to_market"], 2 * high["book_to_market"])
            for name in set(low) - {"earnings_yield_ttm", "book_to_market"}:
                self.assertEqual(low[name], high[name])
        self.assertEqual(world[3]["news"]["age_20d_scaled"], 3 / 20)
        self.assertEqual(world[10]["news"]["age_20d_scaled"], 0)

    def test_neutral_event_ablation_keeps_release_timing_and_changes_exposure(self):
        eventful = market.build_world(seed=7, rounds=21, total_shares=300)
        neutral = market.build_world(seed=7, rounds=21, total_shares=300, news_mode="neutral")
        for event_row, null_row in zip(eventful, neutral):
            self.assertEqual(event_row["event"]["release_day"], null_row["event"]["release_day"])
            self.assertEqual(event_row["news"]["source_disagreement"], null_row["news"]["source_disagreement"])
            self.assertEqual(null_row["news"]["signed_event_surprise"], 0)
            self.assertEqual(null_row["news"]["affected_revenue_fraction"], 0)
        self.assertNotEqual(eventful[-1]["report"]["revenue_ttm_cents"], neutral[-1]["report"]["revenue_ttm_cents"])

    def test_persistent_trade_cost_and_last_sale_history(self):
        result = market.run_market(market.random_policy, agents=12, rounds=10, seed=3)
        recorded = {}
        for row in result["ledger"]:
            for decision in row["decisions"]:
                agent = decision["agent_id"]
                state = decision["account_state"]
                if agent in recorded:
                    last_trade_day, last_sale = recorded[agent]
                    self.assertEqual(state["days_since_trade_scaled_mask"], 1)
                    self.assertAlmostEqual(state["days_since_trade_scaled"], min(1, (row["decision_day"] - last_trade_day) / 20))
                    if last_sale is not None:
                        self.assertEqual(state["post_sale_return_mask"], 1)
                        self.assertAlmostEqual(decision["account_state_raw"]["post_sale_return"],
                                               row["clearing"]["last_price_cents"] / last_sale - 1)
            for fill in row["clearing"]["fills"]:
                old_sale = recorded.get(fill["agent_id"], (None, None))[1]
                recorded[fill["agent_id"]] = (row["settlement_day"], fill["price_cents"] if fill["side"] == "sell" else old_sale)
        self.assertGreater(len(recorded), 0)

    def test_quote_ablation_removes_only_intensity_quote_link(self):
        for side in ["buy", "sell"]:
            fixed_low = market.quote_price(10000, side, 0.1, 0.7)
            fixed_high = market.quote_price(10000, side, 0.9, 0.7)
            self.assertEqual(fixed_low, fixed_high)
            linked_low = market.quote_price(10000, side, 0.1, 0.7, "legacy_intensity_linked")
            linked_high = market.quote_price(10000, side, 0.9, 0.7, "legacy_intensity_linked")
            self.assertNotEqual(linked_low, linked_high)
            self.assertEqual(linked_low, _limit_price(10000, side, 0.1, 0.7))

    def test_raw_effective_and_structural_ood_are_explicit(self):
        effective, changes = market.project_fields({"volume_z": 12, "book_to_market": 0.4})
        self.assertEqual(effective, {"volume_z": 6, "book_to_market": 0.4})
        self.assertEqual(changes, [{"field": "volume_z", "raw": 12, "effective": 6}])
        with self.assertRaises(ValueError):
            market.project_fields({"volume_z": math.nan})
        result = market.run_market(market.prior_policy((0, 1, 0)), agents=8, rounds=8)
        self.assertGreater(result["summary"]["domain_projection_counts"]["turnover_change_5d"], 0)
        self.assertEqual(result["summary"]["intraday_range_approximation_rounds"], 8)
        for row in result["ledger"]:
            for key, value in row["market_effective"].items():
                self.assertLessEqual(FIELD_RANGES[key][0], value)
                self.assertLessEqual(value, FIELD_RANGES[key][1])
            for decision in row["decisions"]:
                V2AttentionState.from_mapping({**{key: row["market_effective"][key] for key in P8[:6]},
                                               **decision["account_state"]})

    def test_allocation_validates_and_largest_remainder_preserves_count(self):
        self.assertEqual(sum(market.profile_allocation(7).values()), 7)
        counts = market.profile_allocation(20, profile_weights={PROFILE_IDS[0]: 0.8, PROFILE_IDS[1]: 0.2})
        self.assertEqual(counts[PROFILE_IDS[0]], 16)
        self.assertEqual(counts[PROFILE_IDS[1]], 4)
        for kwargs in [{"profile_counts": {PROFILE_IDS[0]: 9}},
                       {"profile_weights": {"unknown": 1}},
                       {"profile_weights": {PROFILE_IDS[0]: -1}},
                       {"profile_weights": {}},
                       {"profile_counts": {}, "profile_weights": {}}]:
            with self.assertRaises(ValueError):
                market.profile_allocation(8, **kwargs)

    def test_seed_controls_replay_and_changes_scenario(self):
        first = market.run_market(market.random_policy, agents=8, rounds=3, seed=1)
        repeated = market.run_market(market.random_policy, agents=8, rounds=3, seed=1)
        different = market.run_market(market.random_policy, agents=8, rounds=3, seed=2)
        self.assertEqual(first, repeated)
        self.assertNotEqual(first["world_hash"], different["world_hash"])

    def test_pure_run_does_not_access_files_or_network_and_callback_isolated(self):
        def callback(row):
            row["market_raw"].clear()
            row["decisions"].clear()
        with mock.patch("socket.socket", side_effect=AssertionError("network")), \
                mock.patch("builtins.open", side_effect=AssertionError("file I/O")):
            result = market.run_market(market.random_policy, agents=8, rounds=2, on_round=callback)
        self.assertEqual(len(result["ledger"][0]["decisions"]), 8)
        self.assertEqual(len(result["ledger"][0]["market_raw"]), 24)

    def test_honest_units_and_supported_populations(self):
        for size in [200, 1000]:
            result = market.run_market(market.prior_policy((0, 1, 0)), agents=size, rounds=1)
            self.assertEqual(result["summary"]["agent_decisions"], size)
            self.assertEqual(result["summary"]["market_runs"], 1)
            self.assertEqual(result["summary"]["teacher_logical_requests"], 0)
            self.assertEqual(result["summary"]["human_participants"], 0)

    def test_fail_closed_policy_and_configuration(self):
        for kwargs in [{"agents": 1}, {"rounds": 0}, {"seed": True}, {"quote_rule": "magic"},
                       {"news_mode": "magic"}]:
            with self.assertRaises(ValueError):
                market.run_market(market.random_policy, **kwargs)
        with self.assertRaises(ValueError):
            market.run_market(lambda *_: {"action_probs": [1, 1, 0], "intensities": [1, 1]},
                              agents=2, rounds=1)
