from __future__ import annotations

import random
import unittest
from dataclasses import FrozenInstanceError

from nmsim.v2_market import (
    AcceptedOrder,
    Account,
    CreditFacility,
    OrderIntent,
    assert_system_invariants,
    clear_call_auction,
    constrain_orders,
    run_synchronous_market,
    settle,
)


def accepted(
    order_id: str,
    agent_id: str,
    side: str,
    quantity: int,
    limit_price_cents: int,
) -> AcceptedOrder:
    return AcceptedOrder(
        order_id=order_id,
        agent_id=agent_id,
        side=side,  # type: ignore[arg-type]
        quantity=quantity,
        limit_price_cents=limit_price_cents,
        requested_quantity=quantity,
    )


class ConstraintTests(unittest.TestCase):
    def test_finite_mode_clips_overbuy_and_oversell(self) -> None:
        accounts = (
            Account("buyer", cash_cents=1_000, shares=0),
            Account("seller", cash_cents=0, shares=4),
        )
        result = constrain_orders(
            accounts,
            (
                OrderIntent("b", "buyer", "buy", 10, 300),
                OrderIntent("s", "seller", "sell", 10, 100),
            ),
        )

        self.assertEqual(
            {order.order_id: order.quantity for order in result.accepted_orders},
            {"b": 3, "s": 4},
        )
        self.assertTrue(all(order.was_clipped for order in result.accepted_orders))
        self.assertEqual(result.rejected_orders, ())

    def test_credit_mode_uses_only_unused_account_credit(self) -> None:
        buyer = Account(
            "buyer",
            cash_cents=100,
            shares=0,
            debt_cents=200,
            credit_limit_cents=500,
        )
        result = constrain_orders(
            (buyer,),
            (OrderIntent("b", "buyer", "buy", 10, 150),),
            financing="credit",
        )
        self.assertEqual(result.accepted_orders[0].quantity, 2)

        finite = constrain_orders(
            (buyer,),
            (OrderIntent("b", "buyer", "buy", 10, 150),),
            financing="finite",
        )
        self.assertEqual(finite.rejected_orders[0].reason, "insufficient_buying_power")

    def test_duplicate_agent_intents_are_all_rejected_order_independently(self) -> None:
        account = Account("a", cash_cents=10_000, shares=10)
        intents = (
            OrderIntent("z", "a", "buy", 1, 100),
            OrderIntent("a", "a", "sell", 1, 100),
        )
        forward = constrain_orders((account,), intents)
        reverse = constrain_orders((account,), reversed(intents))

        self.assertEqual(forward, reverse)
        self.assertEqual(forward.accepted_orders, ())
        self.assertEqual(
            [rejection.reason for rejection in forward.rejected_orders],
            ["multiple_active_orders", "multiple_active_orders"],
        )

    def test_unknown_agent_is_audited_and_duplicate_order_id_is_invalid(self) -> None:
        account = Account("a", cash_cents=100, shares=0)
        result = constrain_orders(
            (account,), (OrderIntent("u", "unknown", "buy", 1, 100),)
        )
        self.assertEqual(result.rejected_orders[0].reason, "unknown_agent")

        with self.assertRaisesRegex(ValueError, "order_id"):
            constrain_orders(
                (account,),
                (
                    OrderIntent("same", "a", "buy", 1, 100),
                    OrderIntent("same", "unknown", "buy", 1, 100),
                ),
            )


class ClearingTests(unittest.TestCase):
    def test_no_orders_and_no_cross_preserve_last_price(self) -> None:
        no_orders = clear_call_auction((), last_price_cents=100)
        self.assertEqual(no_orders.status, "no_orders")
        self.assertEqual(no_orders.clearing_price_cents, 100)
        self.assertEqual(no_orders.fills, ())

        no_cross = clear_call_auction(
            (
                accepted("b", "buyer", "buy", 5, 90),
                accepted("s", "seller", "sell", 5, 110),
            ),
            last_price_cents=100,
        )
        self.assertEqual(no_cross.status, "no_cross")
        self.assertEqual(no_cross.clearing_price_cents, 100)
        self.assertEqual(no_cross.matched_volume_shares, 0)

    def test_exact_cross_uses_last_price_when_objectives_tie_across_plateau(self) -> None:
        result = clear_call_auction(
            (
                accepted("b", "buyer", "buy", 5, 110),
                accepted("s", "seller", "sell", 5, 90),
            ),
            last_price_cents=100,
        )
        self.assertEqual(result.status, "cleared")
        self.assertEqual(result.clearing_price_cents, 100)
        self.assertEqual(result.matched_volume_shares, 5)
        self.assertEqual(sum(f.quantity for f in result.fills if f.side == "buy"), 5)
        self.assertEqual(sum(f.quantity for f in result.fills if f.side == "sell"), 5)

    def test_price_objectives_choose_maximum_matched_before_distance(self) -> None:
        result = clear_call_auction(
            (
                accepted("b", "buyer", "buy", 10, 110),
                accepted("s1", "seller-1", "sell", 4, 90),
                accepted("s2", "seller-2", "sell", 6, 105),
            ),
            last_price_cents=100,
        )
        self.assertEqual(result.clearing_price_cents, 105)
        self.assertEqual(result.matched_volume_shares, 10)

    def test_largest_remainder_uses_order_id_and_is_input_order_independent(self) -> None:
        orders = (
            accepted("buy-z", "buyer-z", "buy", 1, 100),
            accepted("buy-a", "buyer-a", "buy", 1, 100),
            accepted("sell", "seller", "sell", 1, 100),
        )
        forward = clear_call_auction(orders, last_price_cents=100)
        reverse = clear_call_auction(tuple(reversed(orders)), last_price_cents=100)

        self.assertEqual(forward, reverse)
        buy_fills = [fill for fill in forward.fills if fill.side == "buy"]
        self.assertEqual([(fill.order_id, fill.quantity) for fill in buy_fills], [("buy-a", 1)])

    def test_constraint_and_clearing_diagnostics_are_preserved(self) -> None:
        accounts = (
            Account("buyer", 100, 0),
            Account("seller", 0, 1),
            Account("empty", 0, 0),
        )
        constrained = constrain_orders(
            accounts,
            (
                OrderIntent("b", "buyer", "buy", 1, 100),
                OrderIntent("s", "seller", "sell", 1, 100),
                OrderIntent("r", "empty", "sell", 1, 100),
            ),
        )
        cleared = clear_call_auction(constrained, last_price_cents=100)
        self.assertEqual(cleared.submitted_intents, constrained.submitted_intents)
        self.assertEqual(cleared.accepted_orders, constrained.accepted_orders)
        self.assertEqual(cleared.rejected_orders, constrained.rejected_orders)


class SettlementTests(unittest.TestCase):
    def test_finite_settlement_conserves_integer_cash_and_shares(self) -> None:
        accounts = (
            Account("buyer", cash_cents=1_000, shares=0),
            Account("seller", cash_cents=0, shares=10),
        )
        facility = CreditFacility(cash_cents=0)
        constrained = constrain_orders(
            accounts,
            (
                OrderIntent("b", "buyer", "buy", 5, 100),
                OrderIntent("s", "seller", "sell", 5, 100),
            ),
        )
        clearing = clear_call_auction(constrained, last_price_cents=100)
        result = settle(accounts, facility, clearing)

        self.assertEqual(result.account("buyer"), Account("buyer", 500, 5))
        self.assertEqual(result.account("seller"), Account("seller", 500, 5))
        self.assertEqual(result.totals_before, result.totals_after)
        self.assertEqual(result.borrowed_cents, 0)

    def test_credit_is_only_actual_fill_shortfall_and_balances_facility(self) -> None:
        accounts = (
            Account("buyer", cash_cents=100, shares=0, credit_limit_cents=1_000),
            Account("seller", cash_cents=0, shares=10),
        )
        facility = CreditFacility(cash_cents=2_000)
        constrained = constrain_orders(
            accounts,
            (
                OrderIntent("b", "buyer", "buy", 5, 100),
                OrderIntent("s", "seller", "sell", 5, 100),
            ),
            financing="credit",
        )
        clearing = clear_call_auction(constrained, last_price_cents=100)
        result = settle(accounts, facility, clearing, financing="credit")

        self.assertEqual(result.borrowed_cents, 400)
        self.assertEqual(result.account("buyer"), Account("buyer", 0, 5, 400, 1_000))
        self.assertEqual(result.account("seller"), Account("seller", 500, 5))
        self.assertEqual(result.credit_facility, CreditFacility(1_600, 400))
        self.assertEqual(result.totals_before.cash_cents, result.totals_after.cash_cents)
        self.assertEqual(result.totals_after.debt_cents, result.totals_after.loan_asset_cents)

    def test_sale_proceeds_do_not_implicitly_repay_credit_within_horizon(self) -> None:
        accounts = (
            Account("borrower", 0, 5, debt_cents=400, credit_limit_cents=1_000),
            Account("cash-buyer", 500, 5),
        )
        facility = CreditFacility(cash_cents=1_600, loan_asset_cents=400)
        clearing = clear_call_auction(
            constrain_orders(
                accounts,
                (
                    OrderIntent("sell", "borrower", "sell", 1, 100),
                    OrderIntent("buy", "cash-buyer", "buy", 1, 100),
                ),
                financing="credit",
            ),
            last_price_cents=100,
        )
        result = settle(accounts, facility, clearing, financing="credit")
        self.assertEqual(result.borrowed_cents, 0)
        self.assertEqual(result.account("borrower").cash_cents, 100)
        self.assertEqual(result.account("borrower").debt_cents, 400)
        self.assertEqual(result.credit_facility, facility)

    def test_partial_fill_borrows_for_executed_value_not_accepted_value(self) -> None:
        accounts = (
            Account("buyer", cash_cents=100, shares=0, credit_limit_cents=1_000),
            Account("seller", cash_cents=0, shares=2),
        )
        facility = CreditFacility(cash_cents=1_000)
        constrained = constrain_orders(
            accounts,
            (
                OrderIntent("b", "buyer", "buy", 5, 100),
                OrderIntent("s", "seller", "sell", 2, 100),
            ),
            financing="credit",
        )
        self.assertEqual(constrained.accepted_orders[0].quantity, 5)
        clearing = clear_call_auction(constrained, last_price_cents=100)
        self.assertEqual(clearing.matched_volume_shares, 2)

        result = settle(accounts, facility, clearing, financing="credit")
        self.assertEqual(result.borrowed_cents, 100)
        self.assertEqual(result.account("buyer").debt_cents, 100)
        self.assertEqual(result.credit_facility.loan_asset_cents, 100)

    def test_insufficient_facility_fails_before_any_commit(self) -> None:
        accounts = (
            Account("buyer", cash_cents=100, shares=0, credit_limit_cents=1_000),
            Account("seller", cash_cents=0, shares=10),
        )
        facility = CreditFacility(cash_cents=300)
        clearing = clear_call_auction(
            constrain_orders(
                accounts,
                (
                    OrderIntent("b", "buyer", "buy", 5, 100),
                    OrderIntent("s", "seller", "sell", 5, 100),
                ),
                financing="credit",
            ),
            last_price_cents=100,
        )
        with self.assertRaisesRegex(ValueError, "facility lacks cash"):
            settle(accounts, facility, clearing, financing="credit")
        self.assertEqual(accounts[0].cash_cents, 100)
        self.assertEqual(facility, CreditFacility(300, 0))

    def test_randomized_constraint_clear_settle_properties(self) -> None:
        random_source = random.Random(1_970_010_1)
        for trial in range(200):
            accounts = []
            intents = []
            for index in range(12):
                agent_id = f"a-{index:02d}"
                account = Account(
                    agent_id,
                    cash_cents=random_source.randint(0, 25_000),
                    shares=random_source.randint(0, 100),
                )
                accounts.append(account)
                side = random_source.choice(("buy", "sell"))
                intents.append(
                    OrderIntent(
                        f"o-{trial:03d}-{index:02d}",
                        agent_id,
                        side,
                        random_source.randint(1, 150),
                        random_source.randint(50, 150),
                    )
                )
            random_source.shuffle(intents)
            constrained = constrain_orders(accounts, intents)
            clearing = clear_call_auction(
                constrained,
                last_price_cents=random_source.randint(75, 125),
            )
            buy_volume = sum(f.quantity for f in clearing.fills if f.side == "buy")
            sell_volume = sum(f.quantity for f in clearing.fills if f.side == "sell")
            self.assertEqual(buy_volume, sell_volume)
            self.assertEqual(buy_volume, clearing.matched_volume_shares)
            for fill in clearing.fills:
                if fill.side == "buy":
                    self.assertLessEqual(fill.price_cents, fill.limit_price_cents)
                else:
                    self.assertGreaterEqual(fill.price_cents, fill.limit_price_cents)
            settlement = settle(accounts, CreditFacility(0), clearing)
            self.assertEqual(
                settlement.totals_before.cash_cents,
                settlement.totals_after.cash_cents,
            )
            self.assertEqual(settlement.totals_before.shares, settlement.totals_after.shares)
            assert_system_invariants(settlement.accounts, settlement.credit_facility)


class SynchronousSimulationTests(unittest.TestCase):
    def test_every_policy_sees_same_frozen_round_start_snapshot(self) -> None:
        observations: dict[int, list[tuple[str, int, tuple[tuple[str, int, int], ...]]]] = {}

        def policy(agent_id: str, snapshot):
            state = tuple(
                (account.agent_id, account.cash_cents, account.shares)
                for account in snapshot.accounts
            )
            observations.setdefault(snapshot.round_index, []).append(
                (agent_id, id(snapshot), state)
            )
            if agent_id == "buyer":
                return OrderIntent(
                    f"b-{snapshot.round_index}", agent_id, "buy", 1, 100
                )
            return OrderIntent(
                f"s-{snapshot.round_index}", agent_id, "sell", 1, 100
            )

        result = run_synchronous_market(
            (
                Account("buyer", cash_cents=1_000, shares=0),
                Account("seller", cash_cents=0, shares=5),
            ),
            CreditFacility(0),
            {"buyer": policy, "seller": policy},
            rounds=2,
            initial_price_cents=100,
        )

        for round_observations in observations.values():
            self.assertEqual(len({observation[1] for observation in round_observations}), 1)
            self.assertEqual(
                len({observation[2] for observation in round_observations}), 1
            )
        self.assertEqual(observations[0][0][2], (("buyer", 1_000, 0), ("seller", 0, 5)))
        self.assertEqual(observations[1][0][2], (("buyer", 900, 1), ("seller", 100, 4)))
        self.assertEqual(result.price_history_cents, (100, 100, 100))
        self.assertEqual(result.final_accounts[0], Account("buyer", 800, 2))
        self.assertEqual(len(result.rounds[0].clearing_result.submitted_intents), 2)
        with self.assertRaises(FrozenInstanceError):
            result.rounds[0].snapshot.last_price_cents = 99  # type: ignore[misc]

    def test_policy_cannot_submit_for_another_agent(self) -> None:
        def bad_policy(agent_id: str, snapshot):
            del agent_id, snapshot
            return OrderIntent("bad", "other", "buy", 1, 100)

        with self.assertRaisesRegex(ValueError, "own agent_id"):
            run_synchronous_market(
                (Account("a", 100, 0),),
                CreditFacility(0),
                {"a": bad_policy},
                rounds=1,
                initial_price_cents=100,
            )


if __name__ == "__main__":
    unittest.main()
