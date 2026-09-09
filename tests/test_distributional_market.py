"""Sizing-law and conserving-market integration with synthetic fixtures."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from experiments import intensity_distribution as training
from experiments import information_market as entry
from nmsim import information_market as market
from nmsim.information_artifacts import read_json, verify_run
from tests import test_information_market_entrypoint as helpers


def policy(visible, account):
    return {"action_probs": [0, 0, 1] if account["position_fraction"] > 0.4 else [1, 0, 0],
            "intensities": [0.6, 0.6],
            "intensity_distributions": {
                "buy": {"support": [1.0], "probabilities": [1.0]},
                "sell": {"support": [0.5, 1.0], "probabilities": [0.5, 0.5]}},
            "private_rationale": "never forward this"}


class DistributionalMarketTests(unittest.TestCase):
    def test_one_share_requires_full_exit_atom_and_counterparty_to_fill(self):
        real_account = market.Account
        for sell_intensity, buyer_active, expected_order, expected_fill in (
            (0.5, True, 0, 0), (1.0, True, 1, 1), (1.0, False, 1, 0)
        ):
            with self.subTest(sell_intensity=sell_intensity, buyer_active=buyer_active):
                def one_share_policy(visible, account):
                    seller = account["position_fraction"] == 1.0
                    return {
                        "action_probs": ([0, 0, 1] if seller else
                                         [1, 0, 0] if buyer_active else [0, 1, 0]),
                        "intensities": [1.0, sell_intensity],
                        "intensity_distributions": {
                            "buy": {"support": [1.0], "probabilities": [1.0]},
                            "sell": {"support": [sell_intensity], "probabilities": [1.0]},
                        },
                    }
                # Only initial accounts and quotes are fixed; production
                # constraints, clearing and settlement still execute normally.
                initial = [real_account("a000000", 0, 1), real_account("a000001", 10000, 0)]
                with patch.object(market, "Account", side_effect=initial), \
                     patch.object(market, "quote_price", return_value=10000), \
                     patch.object(market, "constrain_orders", wraps=market.constrain_orders) as constrain, \
                     patch("socket.create_connection", side_effect=AssertionError("network")):
                    result = market.run_market(one_share_policy, agents=2, rounds=1,
                                               observation_policy="available_only",
                                               sizing_policy="distribution_sampled")
                sell_orders = [order for order in constrain.call_args.args[1] if order.side == "sell"]
                self.assertEqual(sum(order.quantity for order in sell_orders), expected_order)
                row = result["ledger"][0]
                self.assertEqual(row["clearing"]["matched_volume_shares"], expected_fill)
                self.assertEqual(row["accounts_after"][0]["shares"], 1 - expected_fill)
                self.assertEqual(result["sizing_audit"]["counts"].get("closed_position_events", 0), expected_fill)
                self.assertEqual(row["decisions"][0]["order_status"],
                                 "submitted" if expected_order else "sub_share_or_zero_intensity")
                self.assertTrue(result["summary"]["conservation_passed"])

    def test_distribution_sampling_reaches_full_exit_without_rounding_change(self):
        mean = market.run_market(policy, agents=40, rounds=2, seed=11,
                                 observation_policy="available_only", sizing_policy="distribution_mean")
        sampled = market.run_market(policy, agents=40, rounds=2, seed=11,
                                    observation_policy="available_only", sizing_policy="distribution_sampled")
        self.assertEqual(mean["initial_accounts"], sampled["initial_accounts"])
        self.assertEqual(mean["world_hash"], sampled["world_hash"])
        self.assertEqual(mean["sizing_audit"]["counts"].get("full_sell_intents", 0), 0)
        self.assertGreater(sampled["sizing_audit"]["counts"]["full_sell_intents"], 0)
        self.assertGreater(sampled["sizing_audit"]["counts"]["closed_position_events"], 0)
        self.assertTrue(sampled["summary"]["conservation_passed"])
        self.assertEqual(sampled["sizing_audit"]["quantity_rounding"], "floor_unchanged")
        self.assertNotIn("never forward this", str(sampled))
        for left, right in zip(mean["ledger"][0]["decisions"], sampled["ledger"][0]["decisions"]):
            self.assertEqual(left["action_probs"], right["action_probs"])
            self.assertEqual(left["intensities"], right["intensities"])
            self.assertEqual(left["intensity_distributions"], right["intensity_distributions"])

    def test_point_mass_recovers_original_numeric_market(self):
        def point(visible, account):
            value = policy(visible, account)
            value["intensities"] = [1.0, 1.0]
            value["intensity_distributions"]["sell"] = {"support": [1.0], "probabilities": [1.0]}
            return value
        old = market.run_market(point, agents=20, rounds=4, seed=5, observation_policy="available_only")
        new = market.run_market(point, agents=20, rounds=4, seed=5, observation_policy="available_only", sizing_policy="distribution_sampled")
        self.assertEqual(old["summary"], new["summary"])
        for left, right in zip(old["ledger"], new["ledger"]):
            self.assertEqual(left["clearing"], right["clearing"])
            self.assertEqual(left["accounts_after"], right["accounts_after"])

    def test_same_seed_repeats_and_invalid_distribution_rejected(self):
        args = dict(agents=8, rounds=2, observation_policy="available_only", sizing_policy="distribution_sampled")
        self.assertEqual(market.run_market(policy, **args), market.run_market(policy, **args))
        with self.assertRaises(ValueError):
            market.run_market(policy, sizing_policy="distribution_sampled")
        with self.assertRaises(ValueError):
            market.run_market(market.random_policy, **args)
        def bad(visible, account):
            result = policy(visible, account)
            result["intensity_distributions"]["sell"]["probabilities"] = [1, 1]
            return result
        with self.assertRaises(ValueError):
            market.run_market(bad, **args)


class ManagedDistributionTests(unittest.TestCase):
    def setUp(self):
        self.helper = helpers.InformationMarketEntrypointTests()
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        self.model = self.helper.train()
        self.root = self.helper.base

    def test_fit_and_market_with_verified_distribution_source(self):
        with redirect_stdout(io.StringIO()), patch("socket.create_connection", side_effect=AssertionError("network")):
            training.main(["--source-run", str(self.helper.source), "--student-run", str(self.model),
                           "--epochs", "2", "--backend", "python", "--out", str(self.root/"distribution"), "--run-id", "fit"])
            distribution = self.root/"distribution/runs/fit"
            entry.main(["--task", "simulate", "--model-run", str(self.model), "--distribution-run", str(distribution),
                        "--sizing-policy", "distribution_sampled", "--observation-policy", "available_only",
                        "--policies", "selected", "--agents", "8", "--rounds", "2", "--seeds", "1",
                        "--out", str(self.root/"market"), "--run-id", "run"])
        run = self.root/"market/runs/run"
        result = read_json(run/"summary.json")
        self.assertEqual(result["sizing_policy"], "distribution_sampled")
        self.assertEqual(result["honest_n"]["market_runs"], 2)
        self.assertTrue(all(cell["conservation_passed"] for cell in result["markets"]))
        self.assertEqual(verify_run(distribution)["manifest_sha256"], read_json(run/"distribution_source_receipt.json")["manifest_sha256"])
        verify_run(run)

    def test_dry_training_does_not_fit_or_make_provider(self):
        with redirect_stdout(io.StringIO()), patch("nmsim.intensity_distribution.fit_intensity_distributions", side_effect=AssertionError("fit")):
            training.main(["--source-run", str(self.helper.source), "--student-run", str(self.model),
                           "--dry-run", "--out", str(self.root/"dry"), "--run-id", "run"])
        self.assertFalse((self.root/"dry/runs/run/intensity_study.json").exists())

    def test_unused_or_unbound_sizing_flags_fail_closed(self):
        for args in (["--distribution-run", "ignored"], ["--sizing-policy", "distribution_sampled"],
                     ["--sizing-candidate", "empirical"]):
            config = entry.build_parser().parse_args(["--task", "simulate", "--model-run", str(self.model), *args])
            with self.assertRaises(ValueError):
                entry._validate(config)

    def test_output_inside_historical_source_is_refused_before_writes(self):
        before = verify_run(self.model)
        target = self.model/"nested-output"
        with self.assertRaises(SystemExit):
            training.main(["--source-run", str(self.helper.source), "--student-run", str(self.model),
                           "--out", str(target), "--dry-run"])
        self.assertFalse(target.exists())
        self.assertEqual(verify_run(self.model)["snapshot_hash"], before["snapshot_hash"])
        with self.assertRaises(SystemExit):
            training.main(["--source-run", str(self.helper.source), "--student-run", str(self.model),
                           "--out", str(target), "--epochs", "0", "--unknown-option"])
        self.assertFalse(target.exists())
        self.assertEqual(verify_run(self.model)["snapshot_hash"], before["snapshot_hash"])
