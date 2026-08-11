"""Isolated, integer-only market core for the V2 research prototype.

This module deliberately does not alter the legacy :mod:`nmsim.market` clearing
semantics.  It implements a small closing call auction with explicit resources:

* money is represented by integer cents and inventory by integer shares;
* an agent can submit at most one active order per round;
* all policies observe the same immutable round-start snapshot;
* credit is a transfer from an explicit, finite facility, not money creation;
* settlement is staged and committed only after every invariant is checked.

The implementation is a research mechanism, not a brokerage or a limit-order
book.  In particular, it has no order persistence, intraday priority, fees,
interest, short selling, or leverage beyond the explicit per-account credit cap.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Callable, Iterable, Literal, Mapping, Sequence


Side = Literal["buy", "sell"]
FinancingMode = Literal["finite", "credit"]
Policy = Callable[[str, "MarketSnapshot"], "OrderIntent | None"]


def _require_int(name: str, value: int, *, minimum: int = 0) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")


def _require_identifier(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _validate_financing(financing: str) -> FinancingMode:
    if financing not in ("finite", "credit"):
        raise ValueError("financing must be 'finite' or 'credit'")
    return financing


@dataclass(frozen=True)
class Account:
    """A self-contained account state at a round boundary."""

    agent_id: str
    cash_cents: int
    shares: int
    debt_cents: int = 0
    credit_limit_cents: int = 0

    def __post_init__(self) -> None:
        _require_identifier("agent_id", self.agent_id)
        _require_int("cash_cents", self.cash_cents)
        _require_int("shares", self.shares)
        _require_int("debt_cents", self.debt_cents)
        _require_int("credit_limit_cents", self.credit_limit_cents)
        if self.debt_cents > self.credit_limit_cents:
            raise ValueError("debt_cents cannot exceed credit_limit_cents")

    @property
    def unused_credit_cents(self) -> int:
        return self.credit_limit_cents - self.debt_cents


@dataclass(frozen=True)
class CreditFacility:
    """The explicit counterparty that funds the credit experimental arm."""

    cash_cents: int
    loan_asset_cents: int = 0

    def __post_init__(self) -> None:
        _require_int("cash_cents", self.cash_cents)
        _require_int("loan_asset_cents", self.loan_asset_cents)


@dataclass(frozen=True)
class OrderIntent:
    """An unconstrained policy output for one closing auction."""

    order_id: str
    agent_id: str
    side: Side
    quantity: int
    limit_price_cents: int

    def __post_init__(self) -> None:
        _require_identifier("order_id", self.order_id)
        _require_identifier("agent_id", self.agent_id)
        if self.side not in ("buy", "sell"):
            raise ValueError("side must be 'buy' or 'sell'")
        _require_int("quantity", self.quantity, minimum=1)
        _require_int("limit_price_cents", self.limit_price_cents, minimum=1)


@dataclass(frozen=True)
class AcceptedOrder:
    """A resource-constrained order admitted to the call auction."""

    order_id: str
    agent_id: str
    side: Side
    quantity: int
    limit_price_cents: int
    requested_quantity: int

    def __post_init__(self) -> None:
        _require_identifier("order_id", self.order_id)
        _require_identifier("agent_id", self.agent_id)
        if self.side not in ("buy", "sell"):
            raise ValueError("side must be 'buy' or 'sell'")
        _require_int("quantity", self.quantity, minimum=1)
        _require_int("limit_price_cents", self.limit_price_cents, minimum=1)
        _require_int("requested_quantity", self.requested_quantity, minimum=1)
        if self.quantity > self.requested_quantity:
            raise ValueError("accepted quantity cannot exceed requested quantity")

    @property
    def was_clipped(self) -> bool:
        return self.quantity != self.requested_quantity


@dataclass(frozen=True)
class RejectedOrder:
    intent: OrderIntent
    reason: str

    def __post_init__(self) -> None:
        _require_identifier("reason", self.reason)


@dataclass(frozen=True)
class ConstraintResult:
    """Complete constraint-stage audit, including every submitted intent."""

    submitted_intents: tuple[OrderIntent, ...]
    accepted_orders: tuple[AcceptedOrder, ...]
    rejected_orders: tuple[RejectedOrder, ...]
    financing: FinancingMode

    def __post_init__(self) -> None:
        _validate_financing(self.financing)
        accepted_agents = [order.agent_id for order in self.accepted_orders]
        if len(accepted_agents) != len(set(accepted_agents)):
            raise ValueError("at most one accepted order is allowed per agent")
        submitted_ids = [intent.order_id for intent in self.submitted_intents]
        if len(submitted_ids) != len(set(submitted_ids)):
            raise ValueError("submitted order_id values must be unique")


@dataclass(frozen=True)
class PriceLevel:
    price_cents: int
    demand_shares: int
    supply_shares: int
    matched_shares: int
    absolute_imbalance_shares: int

    def __post_init__(self) -> None:
        _require_int("price_cents", self.price_cents, minimum=1)
        _require_int("demand_shares", self.demand_shares)
        _require_int("supply_shares", self.supply_shares)
        _require_int("matched_shares", self.matched_shares)
        _require_int("absolute_imbalance_shares", self.absolute_imbalance_shares)
        if self.matched_shares != min(self.demand_shares, self.supply_shares):
            raise ValueError("matched_shares must equal min(demand, supply)")
        if self.absolute_imbalance_shares != abs(
            self.demand_shares - self.supply_shares
        ):
            raise ValueError("absolute_imbalance_shares is inconsistent")


@dataclass(frozen=True)
class Fill:
    """An order-level fill; buy and sell fills are balanced exactly."""

    order_id: str
    agent_id: str
    side: Side
    quantity: int
    price_cents: int
    limit_price_cents: int

    def __post_init__(self) -> None:
        _require_identifier("order_id", self.order_id)
        _require_identifier("agent_id", self.agent_id)
        if self.side not in ("buy", "sell"):
            raise ValueError("side must be 'buy' or 'sell'")
        _require_int("quantity", self.quantity, minimum=1)
        _require_int("price_cents", self.price_cents, minimum=1)
        _require_int("limit_price_cents", self.limit_price_cents, minimum=1)
        if self.side == "buy" and self.price_cents > self.limit_price_cents:
            raise ValueError("buy fill exceeds its limit price")
        if self.side == "sell" and self.price_cents < self.limit_price_cents:
            raise ValueError("sell fill is below its limit price")


@dataclass(frozen=True)
class ClearingResult:
    """Complete deterministic call-auction result and its diagnostics."""

    last_price_cents: int
    clearing_price_cents: int
    matched_volume_shares: int
    status: Literal["cleared", "no_orders", "no_cross"]
    submitted_intents: tuple[OrderIntent, ...]
    accepted_orders: tuple[AcceptedOrder, ...]
    rejected_orders: tuple[RejectedOrder, ...]
    candidate_levels: tuple[PriceLevel, ...]
    fills: tuple[Fill, ...]

    def __post_init__(self) -> None:
        _require_int("last_price_cents", self.last_price_cents, minimum=1)
        _require_int("clearing_price_cents", self.clearing_price_cents, minimum=1)
        _require_int("matched_volume_shares", self.matched_volume_shares)
        if self.status not in ("cleared", "no_orders", "no_cross"):
            raise ValueError("invalid clearing status")
        buy_filled = sum(fill.quantity for fill in self.fills if fill.side == "buy")
        sell_filled = sum(fill.quantity for fill in self.fills if fill.side == "sell")
        if buy_filled != sell_filled:
            raise ValueError("buy and sell fill volume must be equal")
        if buy_filled != self.matched_volume_shares:
            raise ValueError("fill volume does not match matched_volume_shares")
        if self.status == "cleared" and self.matched_volume_shares <= 0:
            raise ValueError("cleared status requires positive matched volume")
        if self.status != "cleared" and self.matched_volume_shares != 0:
            raise ValueError("non-cleared status requires zero matched volume")
        if self.status != "cleared" and self.clearing_price_cents != self.last_price_cents:
            raise ValueError("a non-clearing auction must preserve the last price")


@dataclass(frozen=True)
class SystemTotals:
    cash_cents: int
    shares: int
    debt_cents: int
    loan_asset_cents: int


@dataclass(frozen=True)
class SettlementResult:
    accounts: tuple[Account, ...]
    credit_facility: CreditFacility
    borrowed_cents: int
    totals_before: SystemTotals
    totals_after: SystemTotals

    def account(self, agent_id: str) -> Account:
        for account in self.accounts:
            if account.agent_id == agent_id:
                return account
        raise KeyError(agent_id)


@dataclass(frozen=True)
class MarketSnapshot:
    """The shared immutable observation supplied to every policy in a round."""

    round_index: int
    last_price_cents: int
    price_history_cents: tuple[int, ...]
    accounts: tuple[Account, ...]
    credit_facility: CreditFacility
    financing: FinancingMode

    def __post_init__(self) -> None:
        _require_int("round_index", self.round_index)
        _require_int("last_price_cents", self.last_price_cents, minimum=1)
        _validate_financing(self.financing)
        if not self.price_history_cents:
            raise ValueError("price_history_cents cannot be empty")
        for price in self.price_history_cents:
            _require_int("price_history_cents item", price, minimum=1)
        if self.price_history_cents[-1] != self.last_price_cents:
            raise ValueError("last price must equal the end of price history")

    def account(self, agent_id: str) -> Account:
        for account in self.accounts:
            if account.agent_id == agent_id:
                return account
        raise KeyError(agent_id)


@dataclass(frozen=True)
class RoundAudit:
    round_index: int
    snapshot: MarketSnapshot
    constraint_result: ConstraintResult
    clearing_result: ClearingResult
    settlement_result: SettlementResult


@dataclass(frozen=True)
class SimulationResult:
    initial_accounts: tuple[Account, ...]
    initial_credit_facility: CreditFacility
    initial_price_cents: int
    financing: FinancingMode
    rounds: tuple[RoundAudit, ...]
    final_accounts: tuple[Account, ...]
    final_credit_facility: CreditFacility
    price_history_cents: tuple[int, ...]

    @property
    def final_price_cents(self) -> int:
        return self.price_history_cents[-1]


def _normalise_accounts(
    accounts: Sequence[Account] | Mapping[str, Account],
) -> tuple[Account, ...]:
    if isinstance(accounts, Mapping):
        values = tuple(accounts.values())
        for key, account in accounts.items():
            if key != account.agent_id:
                raise ValueError("account mapping keys must match account.agent_id")
    else:
        values = tuple(accounts)
    identifiers = [account.agent_id for account in values]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("account agent_id values must be unique")
    return tuple(sorted(values, key=lambda account: account.agent_id))


def system_totals(
    accounts: Sequence[Account] | Mapping[str, Account],
    credit_facility: CreditFacility,
) -> SystemTotals:
    normalised = _normalise_accounts(accounts)
    return SystemTotals(
        cash_cents=sum(account.cash_cents for account in normalised)
        + credit_facility.cash_cents,
        shares=sum(account.shares for account in normalised),
        debt_cents=sum(account.debt_cents for account in normalised),
        loan_asset_cents=credit_facility.loan_asset_cents,
    )


def assert_system_invariants(
    accounts: Sequence[Account] | Mapping[str, Account],
    credit_facility: CreditFacility,
) -> SystemTotals:
    """Validate non-negativity and the borrower/lender balance-sheet identity."""

    normalised = _normalise_accounts(accounts)
    for account in normalised:
        # Account.__post_init__ already checks all field-level constraints; these
        # explicit checks make the system boundary self-documenting.
        if min(account.cash_cents, account.shares, account.debt_cents) < 0:
            raise AssertionError("account resources cannot be negative")
        if account.debt_cents > account.credit_limit_cents:
            raise AssertionError("account debt exceeds its credit limit")
    if credit_facility.cash_cents < 0 or credit_facility.loan_asset_cents < 0:
        raise AssertionError("credit facility resources cannot be negative")
    totals = system_totals(normalised, credit_facility)
    if totals.debt_cents != totals.loan_asset_cents:
        raise AssertionError("sum(account debt) must equal facility loan asset")
    return totals


def constrain_orders(
    accounts: Sequence[Account] | Mapping[str, Account],
    intents: Iterable[OrderIntent],
    *,
    financing: FinancingMode = "finite",
) -> ConstraintResult:
    """Apply account resource limits without mutating accounts.

    Duplicate active intents from one agent are all rejected (fail closed).  This
    choice makes the result independent of input ordering and prevents an
    arbitrary order from receiving priority.  Buy quantities are capped at the
    worst-case limit-price cost; credit mode additionally exposes only unused
    account credit.  Actual borrowing occurs later and only for executed value.
    """

    mode = _validate_financing(financing)
    normalised_accounts = _normalise_accounts(accounts)
    account_by_id = {account.agent_id: account for account in normalised_accounts}
    submitted = tuple(sorted(tuple(intents), key=lambda item: item.order_id))
    order_ids = [intent.order_id for intent in submitted]
    if len(order_ids) != len(set(order_ids)):
        raise ValueError("submitted order_id values must be unique")

    by_agent: dict[str, list[OrderIntent]] = defaultdict(list)
    rejected: list[RejectedOrder] = []
    for intent in submitted:
        if intent.agent_id not in account_by_id:
            rejected.append(RejectedOrder(intent, "unknown_agent"))
        else:
            by_agent[intent.agent_id].append(intent)

    accepted: list[AcceptedOrder] = []
    for agent_id in sorted(by_agent):
        agent_intents = sorted(by_agent[agent_id], key=lambda item: item.order_id)
        if len(agent_intents) != 1:
            rejected.extend(
                RejectedOrder(intent, "multiple_active_orders")
                for intent in agent_intents
            )
            continue

        intent = agent_intents[0]
        account = account_by_id[agent_id]
        if intent.side == "buy":
            spendable = account.cash_cents
            if mode == "credit":
                spendable += account.unused_credit_cents
            maximum_quantity = spendable // intent.limit_price_cents
            constrained_quantity = min(intent.quantity, maximum_quantity)
            if constrained_quantity <= 0:
                rejected.append(RejectedOrder(intent, "insufficient_buying_power"))
                continue
        else:
            constrained_quantity = min(intent.quantity, account.shares)
            if constrained_quantity <= 0:
                rejected.append(RejectedOrder(intent, "insufficient_shares"))
                continue

        accepted.append(
            AcceptedOrder(
                order_id=intent.order_id,
                agent_id=intent.agent_id,
                side=intent.side,
                quantity=constrained_quantity,
                limit_price_cents=intent.limit_price_cents,
                requested_quantity=intent.quantity,
            )
        )

    return ConstraintResult(
        submitted_intents=submitted,
        accepted_orders=tuple(sorted(accepted, key=lambda item: item.order_id)),
        rejected_orders=tuple(
            sorted(rejected, key=lambda item: (item.intent.order_id, item.reason))
        ),
        financing=mode,
    )


def _allocate_pro_rata(
    orders: Sequence[AcceptedOrder], matched_quantity: int
) -> dict[str, int]:
    """Allocate integer shares using largest remainder, then ``order_id``."""

    _require_int("matched_quantity", matched_quantity)
    if matched_quantity == 0:
        return {}
    ordered = tuple(sorted(orders, key=lambda order: order.order_id))
    total_requested = sum(order.quantity for order in ordered)
    if matched_quantity > total_requested:
        raise ValueError("cannot allocate more than the eligible side requested")
    allocations = {
        order.order_id: (matched_quantity * order.quantity) // total_requested
        for order in ordered
    }
    remainders = {
        order.order_id: (matched_quantity * order.quantity) % total_requested
        for order in ordered
    }
    remaining = matched_quantity - sum(allocations.values())
    priority = sorted(
        ordered,
        key=lambda order: (-remainders[order.order_id], order.order_id),
    )
    for order in priority[:remaining]:
        allocations[order.order_id] += 1
    if sum(allocations.values()) != matched_quantity:
        raise AssertionError("pro-rata allocation failed to conserve volume")
    return allocations


def clear_call_auction(
    orders: Sequence[AcceptedOrder] | ConstraintResult,
    *,
    last_price_cents: int,
) -> ClearingResult:
    """Clear one deterministic closing call auction.

    Candidate prices are the previous close plus all admitted limit prices.  The
    lexicographic objective is: maximum matched volume, minimum absolute
    imbalance, minimum distance from the previous close, then the lower price as
    a fixed final direction.  The final tie rule is deliberately fixed rather
    than inferred from order arrival, so input ordering never changes outcomes.
    """

    _require_int("last_price_cents", last_price_cents, minimum=1)
    if isinstance(orders, ConstraintResult):
        submitted = orders.submitted_intents
        accepted = tuple(sorted(orders.accepted_orders, key=lambda item: item.order_id))
        rejected = orders.rejected_orders
    else:
        accepted = tuple(sorted(tuple(orders), key=lambda item: item.order_id))
        submitted = ()
        rejected = ()

    order_ids = [order.order_id for order in accepted]
    if len(order_ids) != len(set(order_ids)):
        raise ValueError("accepted order_id values must be unique")
    agent_counts = Counter(order.agent_id for order in accepted)
    if any(count > 1 for count in agent_counts.values()):
        raise ValueError("at most one accepted order is allowed per agent")

    candidates = sorted(
        {last_price_cents, *(order.limit_price_cents for order in accepted)}
    )
    levels: list[PriceLevel] = []
    for price in candidates:
        demand = sum(
            order.quantity
            for order in accepted
            if order.side == "buy" and order.limit_price_cents >= price
        )
        supply = sum(
            order.quantity
            for order in accepted
            if order.side == "sell" and order.limit_price_cents <= price
        )
        levels.append(
            PriceLevel(
                price_cents=price,
                demand_shares=demand,
                supply_shares=supply,
                matched_shares=min(demand, supply),
                absolute_imbalance_shares=abs(demand - supply),
            )
        )

    if not accepted:
        return ClearingResult(
            last_price_cents=last_price_cents,
            clearing_price_cents=last_price_cents,
            matched_volume_shares=0,
            status="no_orders",
            submitted_intents=submitted,
            accepted_orders=accepted,
            rejected_orders=rejected,
            candidate_levels=tuple(levels),
            fills=(),
        )

    best = min(
        levels,
        key=lambda level: (
            -level.matched_shares,
            level.absolute_imbalance_shares,
            abs(level.price_cents - last_price_cents),
            level.price_cents,
        ),
    )
    if best.matched_shares == 0:
        return ClearingResult(
            last_price_cents=last_price_cents,
            clearing_price_cents=last_price_cents,
            matched_volume_shares=0,
            status="no_cross",
            submitted_intents=submitted,
            accepted_orders=accepted,
            rejected_orders=rejected,
            candidate_levels=tuple(levels),
            fills=(),
        )

    eligible_buys = tuple(
        order
        for order in accepted
        if order.side == "buy" and order.limit_price_cents >= best.price_cents
    )
    eligible_sells = tuple(
        order
        for order in accepted
        if order.side == "sell" and order.limit_price_cents <= best.price_cents
    )
    buy_allocations = _allocate_pro_rata(eligible_buys, best.matched_shares)
    sell_allocations = _allocate_pro_rata(eligible_sells, best.matched_shares)
    fills: list[Fill] = []
    for order in (*eligible_buys, *eligible_sells):
        allocation = (
            buy_allocations if order.side == "buy" else sell_allocations
        ).get(order.order_id, 0)
        if allocation:
            fills.append(
                Fill(
                    order_id=order.order_id,
                    agent_id=order.agent_id,
                    side=order.side,
                    quantity=allocation,
                    price_cents=best.price_cents,
                    limit_price_cents=order.limit_price_cents,
                )
            )

    return ClearingResult(
        last_price_cents=last_price_cents,
        clearing_price_cents=best.price_cents,
        matched_volume_shares=best.matched_shares,
        status="cleared",
        submitted_intents=submitted,
        accepted_orders=accepted,
        rejected_orders=rejected,
        candidate_levels=tuple(levels),
        fills=tuple(sorted(fills, key=lambda fill: fill.order_id)),
    )


def settle(
    accounts: Sequence[Account] | Mapping[str, Account],
    credit_facility: CreditFacility,
    clearing: ClearingResult,
    *,
    financing: FinancingMode = "finite",
) -> SettlementResult:
    """Atomically settle fills and, in credit mode, their exact cash shortfall."""

    mode = _validate_financing(financing)
    normalised = _normalise_accounts(accounts)
    before = assert_system_invariants(normalised, credit_facility)
    account_by_id = {account.agent_id: account for account in normalised}
    accepted_by_id = {order.order_id: order for order in clearing.accepted_orders}

    fills_by_agent: Counter[str] = Counter(fill.agent_id for fill in clearing.fills)
    if any(count > 1 for count in fills_by_agent.values()):
        raise ValueError("an account cannot have more than one fill in a round")
    for fill in clearing.fills:
        if fill.order_id not in accepted_by_id:
            raise ValueError("fill does not reference an accepted order")
        order = accepted_by_id[fill.order_id]
        if (
            fill.agent_id != order.agent_id
            or fill.side != order.side
            or fill.limit_price_cents != order.limit_price_cents
            or fill.quantity > order.quantity
            or fill.price_cents != clearing.clearing_price_cents
        ):
            raise ValueError("fill is inconsistent with its accepted order")
        if fill.agent_id not in account_by_id:
            raise ValueError("fill references an unknown account")

    buy_volume = sum(fill.quantity for fill in clearing.fills if fill.side == "buy")
    sell_volume = sum(fill.quantity for fill in clearing.fills if fill.side == "sell")
    if buy_volume != sell_volume or buy_volume != clearing.matched_volume_shares:
        raise AssertionError("clearing volume is not balanced")

    staged = {
        account.agent_id: {
            "cash": account.cash_cents,
            "shares": account.shares,
            "debt": account.debt_cents,
            "limit": account.credit_limit_cents,
        }
        for account in normalised
    }

    # Determine the full borrowing plan before changing any staged balance.
    shortfalls: dict[str, int] = {}
    for fill in clearing.fills:
        if fill.side != "buy":
            continue
        cost = fill.quantity * fill.price_cents
        current_cash = staged[fill.agent_id]["cash"]
        shortfall = max(0, cost - current_cash)
        if shortfall and mode != "credit":
            raise ValueError("finite financing cannot settle beyond account cash")
        unused_credit = staged[fill.agent_id]["limit"] - staged[fill.agent_id]["debt"]
        if shortfall > unused_credit:
            raise ValueError("fill cash shortfall exceeds unused account credit")
        shortfalls[fill.agent_id] = shortfall

    total_borrowed = sum(shortfalls.values())
    if total_borrowed > credit_facility.cash_cents:
        raise ValueError("credit facility lacks cash for the settlement shortfall")

    facility_cash = credit_facility.cash_cents
    facility_loan_asset = credit_facility.loan_asset_cents
    for agent_id in sorted(shortfalls):
        shortfall = shortfalls[agent_id]
        facility_cash -= shortfall
        facility_loan_asset += shortfall
        staged[agent_id]["cash"] += shortfall
        staged[agent_id]["debt"] += shortfall

    # Only after the lending leg is valid do the security and cash legs move.
    for fill in clearing.fills:
        value = fill.quantity * fill.price_cents
        if fill.side == "buy":
            staged[fill.agent_id]["cash"] -= value
            staged[fill.agent_id]["shares"] += fill.quantity
        else:
            staged[fill.agent_id]["cash"] += value
            staged[fill.agent_id]["shares"] -= fill.quantity

    post_accounts = tuple(
        Account(
            agent_id=account.agent_id,
            cash_cents=staged[account.agent_id]["cash"],
            shares=staged[account.agent_id]["shares"],
            debt_cents=staged[account.agent_id]["debt"],
            credit_limit_cents=staged[account.agent_id]["limit"],
        )
        for account in normalised
    )
    post_facility = CreditFacility(
        cash_cents=facility_cash,
        loan_asset_cents=facility_loan_asset,
    )
    after = assert_system_invariants(post_accounts, post_facility)
    if after.cash_cents != before.cash_cents:
        raise AssertionError("total cash including the facility was not conserved")
    if after.shares != before.shares:
        raise AssertionError("total shares were not conserved")
    if after.debt_cents != after.loan_asset_cents:
        raise AssertionError("borrower debt and facility loan asset diverged")

    return SettlementResult(
        accounts=post_accounts,
        credit_facility=post_facility,
        borrowed_cents=total_borrowed,
        totals_before=before,
        totals_after=after,
    )


def run_synchronous_market(
    accounts: Sequence[Account] | Mapping[str, Account],
    credit_facility: CreditFacility,
    policies: Mapping[str, Policy],
    *,
    rounds: int,
    initial_price_cents: int,
    financing: FinancingMode = "finite",
) -> SimulationResult:
    """Run the minimal synchronous intent -> constraint -> clear -> settle loop."""

    _require_int("rounds", rounds)
    _require_int("initial_price_cents", initial_price_cents, minimum=1)
    mode = _validate_financing(financing)
    initial_accounts = _normalise_accounts(accounts)
    assert_system_invariants(initial_accounts, credit_facility)
    known_agents = {account.agent_id for account in initial_accounts}
    unknown_policies = set(policies) - known_agents
    if unknown_policies:
        raise ValueError(f"policies reference unknown agents: {sorted(unknown_policies)!r}")

    current_accounts = initial_accounts
    current_facility = credit_facility
    history = [initial_price_cents]
    audits: list[RoundAudit] = []

    for round_index in range(rounds):
        snapshot = MarketSnapshot(
            round_index=round_index,
            last_price_cents=history[-1],
            price_history_cents=tuple(history),
            accounts=current_accounts,
            credit_facility=current_facility,
            financing=mode,
        )
        intents: list[OrderIntent] = []
        # No state changes occur while policies are queried.  Every callable gets
        # this exact snapshot object, not a partially updated per-agent view.
        for agent_id in sorted(known_agents):
            policy = policies.get(agent_id)
            if policy is None:
                continue
            intent = policy(agent_id, snapshot)
            if intent is None:
                continue
            if not isinstance(intent, OrderIntent):
                raise TypeError("policies must return OrderIntent or None")
            if intent.agent_id != agent_id:
                raise ValueError("a policy may only submit for its own agent_id")
            intents.append(intent)

        constrained = constrain_orders(
            current_accounts,
            intents,
            financing=mode,
        )
        clearing = clear_call_auction(
            constrained,
            last_price_cents=snapshot.last_price_cents,
        )
        settlement = settle(
            current_accounts,
            current_facility,
            clearing,
            financing=mode,
        )
        audits.append(
            RoundAudit(
                round_index=round_index,
                snapshot=snapshot,
                constraint_result=constrained,
                clearing_result=clearing,
                settlement_result=settlement,
            )
        )
        current_accounts = settlement.accounts
        current_facility = settlement.credit_facility
        history.append(clearing.clearing_price_cents)

    return SimulationResult(
        initial_accounts=initial_accounts,
        initial_credit_facility=credit_facility,
        initial_price_cents=initial_price_cents,
        financing=mode,
        rounds=tuple(audits),
        final_accounts=current_accounts,
        final_credit_facility=current_facility,
        price_history_cents=tuple(history),
    )


__all__ = [
    "AcceptedOrder",
    "Account",
    "ClearingResult",
    "ConstraintResult",
    "CreditFacility",
    "Fill",
    "MarketSnapshot",
    "OrderIntent",
    "Policy",
    "PriceLevel",
    "RejectedOrder",
    "RoundAudit",
    "SettlementResult",
    "SimulationResult",
    "SystemTotals",
    "assert_system_invariants",
    "clear_call_auction",
    "constrain_orders",
    "run_synchronous_market",
    "settle",
    "system_totals",
]
