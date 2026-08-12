from __future__ import annotations

import json
import unittest

from nmsim.v2_attention import (
    FEATURE_ORDER,
    FEATURE_RANGES,
    MARKET_FEATURES,
    V2AttentionState,
    design_anchor_states,
)
from nmsim.v2_distillation import build_ood_reference
from nmsim.v2_market import Account, CreditFacility, MarketSnapshot
from nmsim.v2_market_experiment import (
    AgentTradeMetadata,
    MarketExperimentError,
    _market_features,
    build_attention_state,
    market_experiment_descriptor,
    run_budget_behavior_2x2,
)


class BalancedStudent:
    def __init__(self) -> None:
        self.features: list[tuple[float, ...]] = []

    def predict(self, feature_vector):
        self.features.append(tuple(feature_vector))
        return {
            "action_probs": [0.50, 0.0, 0.50],
            "intensities": [0.90, 0.90],
        }


class InvalidStudent:
    def predict(self, feature_vector):
        del feature_vector
        return {"action_probs": [0.8, 0.8, -0.6], "intensities": [0.5, 0.5]}


class FeatureConstructionTests(unittest.TestCase):
    def test_zero_volume_variance_uses_frozen_signed_domain_sentinel(self) -> None:
        prices = [10_000] * 21
        self.assertEqual(
            _market_features(prices, [100] * 21)["volume_z"], 0.0
        )
        self.assertEqual(
            _market_features(prices, [100] * 20 + [101])["volume_z"], 6.0
        )
        self.assertEqual(
            _market_features(prices, [100] * 20 + [99])["volume_z"], -6.0
        )

    def test_out_of_domain_values_are_explicitly_clamped_into_valid_state(self) -> None:
        prices = tuple([100] * 20 + [10_000])
        volumes = tuple([1] * 20 + [1_000_000])
        account = Account("a", cash_cents=10**12, shares=2)
        snapshot = MarketSnapshot(
            round_index=50,
            last_price_cents=10_000,
            price_history_cents=prices,
            accounts=(account,),
            credit_facility=CreditFacility(0),
            financing="finite",
        )
        result = build_attention_state(
            snapshot,
            "a",
            volume_history_shares=volumes,
            metadata=AgentTradeMetadata(
                basis_price_cents=1,
                last_trade_round=0,
                last_sale_price_cents=1,
                last_sale_round=0,
            ),
        )

        expected_clamps = {
            "return_1d",
            "return_5d",
            "return_20d",
            "realized_vol_20d",
            "unrealized_return",
            "days_since_trade_scaled",
            "post_sale_return",
            "log10_wealth",
        }
        self.assertTrue(expected_clamps.issubset(set(result.clamped_features)))
        self.assertEqual(tuple(result.state.to_dict()), FEATURE_ORDER)
        self.assertEqual(len(result.state.to_feature_vector()), len(FEATURE_ORDER))
        for name, value in result.state.to_dict().items():
            low, high = FEATURE_RANGES[name]
            self.assertGreaterEqual(value, low)
            self.assertLessEqual(value, high)
        # Reconstructing through the frozen contract proves mask/value semantics.
        self.assertEqual(
            V2AttentionState.from_mapping(result.state.to_dict()), result.state
        )

    def test_missing_account_history_uses_canonical_masks(self) -> None:
        prices = tuple([10_000] * 21)
        snapshot = MarketSnapshot(
            round_index=0,
            last_price_cents=10_000,
            price_history_cents=prices,
            accounts=(Account("cash", 1_000_000, 0),),
            credit_facility=CreditFacility(0),
            financing="finite",
        )
        result = build_attention_state(
            snapshot,
            "cash",
            volume_history_shares=[100] * 21,
            metadata=AgentTradeMetadata(None),
        )
        state = result.state
        self.assertEqual(state.unrealized_return, 0.0)
        self.assertEqual(state.unrealized_return_mask, 0)
        self.assertEqual(state.days_since_trade_scaled, 0.0)
        self.assertEqual(state.days_since_trade_scaled_mask, 0)
        self.assertEqual(state.post_sale_return, 0.0)
        self.assertEqual(state.post_sale_return_mask, 0)


class PairedExperimentTests(unittest.TestCase):
    def test_market_reports_train_support_ood_by_cell_and_feature(self) -> None:
        reference = build_ood_reference(
            [
                list(observation.state.to_feature_vector())
                for observation in design_anchor_states("ood-study")
            ]
        )
        result = run_budget_behavior_2x2(
            BalancedStudent(),
            n_agents=6,
            rounds=3,
            seeds=1,
            master_seed=8,
            ood_reference=reference,
        )
        diagnostics = result["market_vs_train_ood"]
        self.assertIsNotNone(diagnostics)
        self.assertEqual(diagnostics["all_cells"]["n"], 4 * 6 * 3)
        self.assertEqual(len(diagnostics["by_cell"]), 4)
        for summary in result["cell_summaries"]:
            named = summary["market_vs_train_ood"]["diagnostics"]
            self.assertEqual(named["n"], 6 * 3)
            self.assertEqual(
                set(named["outside_train_range_count_by_feature_name"]),
                set(FEATURE_ORDER),
            )

    def test_four_cells_honest_n_summaries_and_json_contract(self) -> None:
        result = run_budget_behavior_2x2(
            BalancedStudent(),
            n_agents=8,
            rounds=5,
            seeds=2,
            master_seed=20260811,
        )

        self.assertEqual(result["planned_runs"], 8)
        self.assertEqual(result["honest_n_market_runs"], 8)
        self.assertEqual(len(result["runs"]), 8)
        self.assertEqual(
            [summary["cell"] for summary in result["cell_summaries"]],
            [
                "finite_distilled",
                "finite_momentum",
                "credit_distilled",
                "credit_momentum",
            ],
        )
        for summary in result["cell_summaries"]:
            self.assertEqual(summary["planned_seeds"], 2)
            self.assertEqual(summary["completed_seeds"], 2)
            self.assertEqual(len(summary["representative_price_path"]), 6)
            if summary["financing"] == "finite":
                self.assertEqual(summary["mean_ending_debt_cents"], 0.0)
            else:
                self.assertEqual(
                    summary["mean_ending_debt_cents"],
                    summary["mean_credit_used_cents"],
                )
        json.dumps(result, allow_nan=False)

    def test_initialization_and_random_substreams_are_paired_across_cells(self) -> None:
        result = run_budget_behavior_2x2(
            BalancedStudent(),
            n_agents=8,
            rounds=3,
            seeds=1,
            master_seed=77,
        )
        runs = result["runs"]
        self.assertEqual(len({run["paired_initialization_id"] for run in runs}), 1)
        self.assertEqual(len({run["paired_decision_stream_id"] for run in runs}), 1)
        self.assertEqual(len({run["paired_limit_stream_id"] for run in runs}), 1)
        self.assertEqual(
            len(
                {
                    tuple(run["initial"]["price_history_cents"])
                    for run in runs
                }
            ),
            1,
        )
        self.assertEqual(
            len(
                {
                    tuple(run["initial"]["volume_history_shares"])
                    for run in runs
                }
            ),
            1,
        )
        self.assertEqual(
            len(
                {
                    run["initial"]["credit_facility"]["cash_cents"]
                    for run in runs
                }
            ),
            1,
        )

        cash_share_views = []
        for run in runs:
            cash_share_views.append(
                tuple(
                    (row["agent_id"], row["cash_cents"], row["shares"])
                    for row in run["initial"]["accounts"]
                )
            )
        self.assertEqual(len(set(cash_share_views)), 1)
        for run in runs:
            credit_limits = [
                account["credit_limit_cents"]
                for account in run["initial"]["accounts"]
            ]
            if run["financing"] == "finite":
                self.assertTrue(all(value == 0 for value in credit_limits))
            else:
                self.assertTrue(all(value > 0 for value in credit_limits))

        # Each agent-round draw is common even though differing policies can map
        # that same draw to different actions.
        for round_index in range(3):
            for agent_index in range(8):
                agent_id = f"agent-{agent_index:04d}"
                decisions = []
                for run in runs:
                    record = next(
                        row
                        for row in run["rounds"][round_index]["decision_records"]
                        if row["agent_id"] == agent_id
                    )
                    decisions.append(
                        (
                            record["common_uniform_draw"],
                            record["common_limit_uniform_draw"],
                        )
                    )
                self.assertEqual(len(set(decisions)), 1)

    def test_reproducible_for_same_student_and_master_seed(self) -> None:
        first = run_budget_behavior_2x2(
            BalancedStudent(),
            n_agents=8,
            rounds=4,
            seeds=[2, 9],
            master_seed=101,
        )
        second = run_budget_behavior_2x2(
            BalancedStudent(),
            n_agents=8,
            rounds=4,
            seeds=[2, 9],
            master_seed=101,
        )
        self.assertEqual(first, second)

    def test_every_round_conserves_and_credit_has_balanced_counter_asset(self) -> None:
        result = run_budget_behavior_2x2(
            BalancedStudent(),
            n_agents=12,
            rounds=12,
            seeds=1,
            master_seed=2026,
        )
        credit_borrowing = 0
        for run in result["runs"]:
            for ledger in run["rounds"]:
                self.assertTrue(all(ledger["conservation"].values()))
                self.assertEqual(
                    ledger["totals_before"]["cash_cents"],
                    ledger["totals_after"]["cash_cents"],
                )
                self.assertEqual(
                    ledger["totals_before"]["shares"],
                    ledger["totals_after"]["shares"],
                )
                self.assertEqual(
                    ledger["totals_after"]["debt_cents"],
                    ledger["totals_after"]["loan_asset_cents"],
                )
                if run["financing"] == "finite":
                    self.assertEqual(ledger["borrowed_cents"], 0)
                else:
                    credit_borrowing += ledger["borrowed_cents"]
        self.assertGreater(credit_borrowing, 0)

    def test_effective_states_and_policy_input_boundaries_are_auditable(self) -> None:
        student = BalancedStudent()
        result = run_budget_behavior_2x2(
            student,
            n_agents=6,
            rounds=3,
            seeds=1,
            master_seed=44,
        )
        self.assertEqual(len(student.features), 6 * 3 * 2)
        self.assertTrue(all(len(row) == len(FEATURE_ORDER) for row in student.features))
        for run in result["runs"]:
            expected_names = (
                list(FEATURE_ORDER)
                if run["behavior"] == "distilled"
                else list(MARKET_FEATURES)
            )
            self.assertEqual(run["control_contract"]["policy_inputs"], expected_names)
            for ledger in run["rounds"]:
                for decision in ledger["decision_records"]:
                    self.assertEqual(decision["policy_feature_names"], expected_names)
                    self.assertEqual(
                        tuple(decision["effective_state"]), FEATURE_ORDER
                    )
                    V2AttentionState.from_mapping(decision["effective_state"])
        descriptor = market_experiment_descriptor()
        self.assertEqual(descriptor["feature_order"], list(FEATURE_ORDER))
        self.assertEqual(
            descriptor["momentum_control_inputs"], list(MARKET_FEATURES)
        )

    def test_fill_updates_basis_trade_clock_and_post_sale_metadata(self) -> None:
        result = run_budget_behavior_2x2(
            BalancedStudent(),
            n_agents=10,
            rounds=5,
            seeds=1,
            master_seed=13,
        )
        run = next(row for row in result["runs"] if row["cell"] == "finite_distilled")
        seen_fill = False
        seen_sale = False
        for ledger in run["rounds"]:
            metadata = ledger["trade_metadata_after"]
            for fill in ledger["fills"]:
                seen_fill = True
                agent_metadata = metadata[fill["agent_id"]]
                self.assertEqual(
                    agent_metadata["last_trade_round"], ledger["round_index"]
                )
                if fill["side"] == "buy":
                    self.assertIsNotNone(agent_metadata["basis_price_cents"])
                else:
                    seen_sale = True
                    self.assertEqual(
                        agent_metadata["last_sale_price_cents"], fill["price_cents"]
                    )
                    self.assertEqual(
                        agent_metadata["last_sale_round"], ledger["round_index"]
                    )
        self.assertTrue(seen_fill)
        self.assertTrue(seen_sale)

    def test_invalid_inputs_and_student_predictions_fail_closed(self) -> None:
        with self.assertRaises(MarketExperimentError):
            run_budget_behavior_2x2(
                BalancedStudent(),
                n_agents=3,
                rounds=1,
                seeds=1,
                master_seed=0,
            )
        with self.assertRaisesRegex(MarketExperimentError, "probabilities"):
            run_budget_behavior_2x2(
                InvalidStudent(),
                n_agents=4,
                rounds=1,
                seeds=1,
                master_seed=0,
            )


if __name__ == "__main__":
    unittest.main()
