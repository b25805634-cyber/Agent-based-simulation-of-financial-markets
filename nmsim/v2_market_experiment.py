"""Paired budget-by-behaviour experiment for the isolated V2 market.

The experiment is intentionally a pure in-memory orchestrator.  It performs
no provider calls and no filesystem writes.  A caller supplies a fitted
Student exposing ``predict(feature_vector)`` and receives a JSON-serialisable
ledger for all four paired cells::

    finite x distilled       finite x momentum
    credit x distilled       credit x momentum

Initial cash/inventory, the 21-close warm-up tape, facility cash, and each
agent-round decision uniform are identical across the four cells for a seed.
Only the configured financing mechanism and policy differ.  The momentum
control is probabilistic and consumes tape features only.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
import random
import statistics
from typing import Any, Callable, Mapping, Optional, Sequence

from nmsim.v2_attention import (
    ACTION_ORDER,
    FEATURE_ORDER,
    FEATURE_RANGES,
    MARKET_FEATURES,
    STATE_SCHEMA_VERSION,
    V2AttentionState,
    derive_seed,
)
from nmsim.v2_market import (
    Account,
    CreditFacility,
    MarketSnapshot,
    OrderIntent,
    assert_system_invariants,
    clear_call_auction,
    constrain_orders,
    settle,
)
from nmsim.v2_distillation import OODReference, OOD_Z_THRESHOLD, ood_diagnostics


MARKET_EXPERIMENT_SCHEMA_VERSION = "v2-budget-behavior-2x2/1.0.0"
LEDGER_SCHEMA_VERSION = "v2-market-ledger/1.0.0"

BUDGET_MODES = ("finite", "credit")
BEHAVIOR_MODES = ("distilled", "momentum")
CELL_ORDER = (
    ("finite", "distilled"),
    ("finite", "momentum"),
    ("credit", "distilled"),
    ("credit", "momentum"),
)

# Frozen engineering parameters.  They are returned by
# market_experiment_descriptor so an official entrypoint can bind them into its
# named scientific-config identity.
WARMUP_CLOSES = 21
INITIAL_WEALTH_MIN_CENTS = 1_500_000
INITIAL_WEALTH_SPAN_CENTS = 2_500_001
INITIAL_POSITION_MIN_BPS = 1_500
INITIAL_POSITION_SPAN_BPS = 7_001
CREDIT_LIMIT_BPS = 5_000
LIMIT_URGENCY_BPS = 300
LIMIT_HETEROGENEITY_BPS = 200
MAX_LIMIT_OFFSET_BPS = 500
DAYS_SINCE_TRADE_SCALE = 20
ANNUALIZATION_DAYS = 252


class MarketExperimentError(ValueError):
    """The paired experiment or supplied Student violates its contract."""


@dataclass(frozen=True)
class AgentTradeMetadata:
    """Private account-history state needed for the numeric Student features."""

    basis_price_cents: int | None
    last_trade_round: int | None = None
    last_sale_price_cents: int | None = None
    last_sale_round: int | None = None

    def __post_init__(self) -> None:
        for name in ("basis_price_cents", "last_sale_price_cents"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
            ):
                raise MarketExperimentError(f"{name} must be a positive integer or None")
        for name in ("last_trade_round", "last_sale_round"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise MarketExperimentError(
                    f"{name} must be a non-negative integer or None"
                )


@dataclass(frozen=True)
class FeatureBuildResult:
    state: V2AttentionState
    raw_features: dict[str, float | int]
    clamped_features: tuple[str, ...]


@dataclass(frozen=True)
class _InitialCondition:
    seed_index: int
    run_seed: int
    price_history_cents: tuple[int, ...]
    volume_history_shares: tuple[int, ...]
    base_accounts: tuple[Account, ...]
    credit_limits_cents: dict[str, int]
    facility_cash_cents: int
    metadata: dict[str, AgentTradeMetadata]
    initialization_id: str
    decision_stream_id: str
    limit_stream_id: str


def _require_integer(name: str, value: int, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MarketExperimentError(f"{name} must be an integer")
    if value < minimum:
        raise MarketExperimentError(f"{name} must be >= {minimum}")
    return value


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def market_experiment_descriptor() -> dict[str, Any]:
    """Return every fixed mechanism parameter that changes V2 market meaning."""

    return {
        "schema_version": MARKET_EXPERIMENT_SCHEMA_VERSION,
        "state_schema_version": STATE_SCHEMA_VERSION,
        "feature_order": list(FEATURE_ORDER),
        "budget_modes": list(BUDGET_MODES),
        "behavior_modes": list(BEHAVIOR_MODES),
        "warmup_closes": WARMUP_CLOSES,
        "initial_wealth_min_cents": INITIAL_WEALTH_MIN_CENTS,
        "initial_wealth_span_cents": INITIAL_WEALTH_SPAN_CENTS,
        "initial_position_min_bps": INITIAL_POSITION_MIN_BPS,
        "initial_position_span_bps": INITIAL_POSITION_SPAN_BPS,
        "credit_limit_bps": CREDIT_LIMIT_BPS,
        "credit_interest_bps_per_round": 0,
        "credit_fees_cents": 0,
        "credit_repayment_rule": (
            "none_within_finite_horizon; borrowed principal remains outstanding "
            "through the terminal ledger"
        ),
        "facility_initial_cash_rule": "sum_preregistered_agent_credit_limits",
        "limit_urgency_bps": LIMIT_URGENCY_BPS,
        "limit_heterogeneity_bps": LIMIT_HETEROGENEITY_BPS,
        "maximum_limit_offset_bps": MAX_LIMIT_OFFSET_BPS,
        "days_since_trade_scale": DAYS_SINCE_TRADE_SCALE,
        "annualization_days": ANNUALIZATION_DAYS,
        "action_sampling": "one_common_uniform_per_seed_round_agent",
        "limit_sampling": "one_common_uniform_per_seed_round_agent",
        "auction": (
            "last_and_limit_candidates; max matched; min absolute imbalance; "
            "min distance to last; lower price final tie"
        ),
        "momentum_control_inputs": list(MARKET_FEATURES),
        "momentum_control": "probabilistic_softmax_tape_only_v1",
    }


def _simple_return(new_price: int, old_price: int) -> float:
    if old_price <= 0:
        raise MarketExperimentError("price history must contain positive prices")
    return new_price / old_price - 1.0


def _market_features(
    price_history_cents: Sequence[int],
    volume_history_shares: Sequence[int],
) -> dict[str, float]:
    if len(price_history_cents) < WARMUP_CLOSES:
        raise MarketExperimentError(
            f"price history must contain at least {WARMUP_CLOSES} closes"
        )
    if len(volume_history_shares) < WARMUP_CLOSES:
        raise MarketExperimentError(
            f"volume history must contain at least {WARMUP_CLOSES} observations"
        )
    prices = tuple(price_history_cents)
    volumes = tuple(volume_history_shares)
    for price in prices:
        if isinstance(price, bool) or not isinstance(price, int) or price <= 0:
            raise MarketExperimentError("price history must use positive integer cents")
    for volume in volumes:
        if isinstance(volume, bool) or not isinstance(volume, int) or volume < 0:
            raise MarketExperimentError("volume history must use non-negative shares")

    trailing = prices[-WARMUP_CLOSES:]
    daily_returns = [
        _simple_return(trailing[index], trailing[index - 1])
        for index in range(1, len(trailing))
    ]
    realized_volatility = statistics.pstdev(daily_returns) * math.sqrt(
        ANNUALIZATION_DAYS
    )
    recent_volumes = volumes[-WARMUP_CLOSES:]
    volume_reference = recent_volumes[:-1]
    volume_scale = statistics.pstdev(volume_reference)
    if volume_scale == 0.0:
        difference = recent_volumes[-1] - volume_reference[-1]
        if difference == 0:
            volume_z = 0.0
        else:
            low, high = FEATURE_RANGES["volume_z"]
            volume_z = high if difference > 0 else low
    else:
        volume_z = (
            recent_volumes[-1] - statistics.fmean(volume_reference)
        ) / volume_scale

    last = prices[-1]
    return {
        "return_1d": _simple_return(last, prices[-2]),
        "return_5d": _simple_return(last, prices[-6]),
        "return_20d": _simple_return(last, prices[-21]),
        "realized_vol_20d": realized_volatility,
        "drawdown_20d": last / max(trailing) - 1.0,
        "volume_z": volume_z,
    }


def _clamp_features(
    raw_features: Mapping[str, float | int],
) -> tuple[dict[str, float | int], tuple[str, ...]]:
    if set(raw_features) != set(FEATURE_ORDER):
        raise MarketExperimentError("raw features do not match FEATURE_ORDER")
    clamped: dict[str, float | int] = {}
    changed: list[str] = []
    for name in FEATURE_ORDER:
        raw = raw_features[name]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise MarketExperimentError(f"feature {name} is not numeric")
        number = float(raw)
        if not math.isfinite(number):
            raise MarketExperimentError(f"feature {name} is not finite")
        low, high = FEATURE_RANGES[name]
        bounded = min(high, max(low, number))
        if bounded != number:
            changed.append(name)
        if name.endswith("_mask"):
            if bounded not in (0.0, 1.0):
                raise MarketExperimentError(f"mask feature {name} must be 0 or 1")
            clamped[name] = int(bounded)
        else:
            clamped[name] = 0.0 if bounded == 0.0 else bounded
    return clamped, tuple(changed)


def build_attention_state(
    snapshot: MarketSnapshot,
    agent_id: str,
    *,
    volume_history_shares: Sequence[int],
    metadata: AgentTradeMetadata,
) -> FeatureBuildResult:
    """Build, explicitly clamp, and validate one effective Student state."""

    if not isinstance(snapshot, MarketSnapshot):
        raise MarketExperimentError("snapshot must be MarketSnapshot")
    if not isinstance(metadata, AgentTradeMetadata):
        raise MarketExperimentError("metadata must be AgentTradeMetadata")
    try:
        account = snapshot.account(agent_id)
    except KeyError as error:
        raise MarketExperimentError(f"unknown agent_id {agent_id!r}") from error

    market = _market_features(
        snapshot.price_history_cents,
        volume_history_shares,
    )
    price = snapshot.last_price_cents
    security_value = account.shares * price
    gross_assets = account.cash_cents + security_value
    position_fraction = security_value / gross_assets if gross_assets > 0 else 0.0
    net_wealth = gross_assets - account.debt_cents

    if account.shares > 0 and metadata.basis_price_cents is not None:
        unrealized_return = price / metadata.basis_price_cents - 1.0
        unrealized_mask = 1
    else:
        unrealized_return = 0.0
        unrealized_mask = 0

    if metadata.last_trade_round is None:
        days_since_trade = 0.0
        days_mask = 0
    else:
        elapsed = max(0, snapshot.round_index - metadata.last_trade_round)
        days_since_trade = elapsed / DAYS_SINCE_TRADE_SCALE
        days_mask = 1

    if metadata.last_sale_price_cents is None:
        post_sale_return = 0.0
        post_sale_mask = 0
    else:
        post_sale_return = price / metadata.last_sale_price_cents - 1.0
        post_sale_mask = 1

    # log10_wealth uses cents consistently across design and simulation.  A
    # non-positive net account value is represented at the lower closed-domain
    # edge and counted as an explicit clamp.
    wealth_for_log = max(1.0, float(net_wealth))
    raw: dict[str, float | int] = {
        **market,
        "position_fraction": position_fraction,
        "unrealized_return": unrealized_return,
        "unrealized_return_mask": unrealized_mask,
        "days_since_trade_scaled": days_since_trade,
        "days_since_trade_scaled_mask": days_mask,
        "post_sale_return": post_sale_return,
        "post_sale_return_mask": post_sale_mask,
        "log10_wealth": math.log10(wealth_for_log),
    }
    effective, changed = _clamp_features(raw)
    state = V2AttentionState.from_mapping(effective)
    return FeatureBuildResult(
        state=state,
        raw_features={name: raw[name] for name in FEATURE_ORDER},
        clamped_features=changed,
    )


def _softmax(values: Sequence[float]) -> list[float]:
    maximum = max(values)
    exponentials = [math.exp(value - maximum) for value in values]
    total = sum(exponentials)
    return [value / total for value in exponentials]


def _momentum_prediction(market: Mapping[str, float]) -> dict[str, list[float]]:
    """Probabilistic tape-only null/control policy."""

    if set(market) != set(MARKET_FEATURES):
        raise MarketExperimentError("momentum control accepts tape features only")
    trend = (
        0.55 * market["return_1d"]
        + 0.30 * market["return_5d"] / 5.0
        + 0.15 * market["return_20d"] / 20.0
    )
    daily_volatility = max(
        0.004,
        market["realized_vol_20d"] / math.sqrt(ANNUALIZATION_DAYS),
    )
    score = max(-3.0, min(3.0, trend / daily_volatility))
    volume_tilt = max(-0.35, min(0.35, market["volume_z"] * 0.04))
    logits = (
        score + volume_tilt,
        0.75 - 0.20 * abs(score),
        -score - volume_tilt,
    )
    action_probs = _softmax(logits)
    # A neutral tape remains a probabilistic policy with moderate reservation
    # urgency; it is not a hard common direction and does not mechanically lock
    # every buy below every sell.
    intensity = min(0.80, 0.50 + 0.10 * abs(score))
    return {
        "action_probs": action_probs,
        "intensities": [intensity, intensity],
    }


def _normalise_prediction(value: Any) -> dict[str, list[float]]:
    if not isinstance(value, Mapping):
        raise MarketExperimentError("Student prediction must be a mapping")
    action_probs = value.get("action_probs")
    intensities = value.get("intensities")
    if (
        isinstance(action_probs, (str, bytes))
        or not isinstance(action_probs, Sequence)
        or len(action_probs) != 3
    ):
        raise MarketExperimentError("action_probs must have length three")
    if (
        isinstance(intensities, (str, bytes))
        or not isinstance(intensities, Sequence)
        or len(intensities) != 2
    ):
        raise MarketExperimentError("intensities must have length two")
    probabilities: list[float] = []
    for item in action_probs:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise MarketExperimentError("action probabilities must be numeric")
        number = float(item)
        if not math.isfinite(number) or not 0.0 <= number <= 1.0:
            raise MarketExperimentError("action probabilities must lie in [0,1]")
        probabilities.append(number)
    if not math.isclose(sum(probabilities), 1.0, rel_tol=0.0, abs_tol=1e-8):
        raise MarketExperimentError("action probabilities must sum to one")
    probability_sum = sum(probabilities)
    probabilities = [value / probability_sum for value in probabilities]
    intensity_values: list[float] = []
    for item in intensities:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise MarketExperimentError("intensities must be numeric")
        number = float(item)
        if not math.isfinite(number) or not 0.0 <= number <= 1.0:
            raise MarketExperimentError("intensities must lie in [0,1]")
        intensity_values.append(number)
    return {"action_probs": probabilities, "intensities": intensity_values}


def _sample_action(probabilities: Sequence[float], uniform_draw: float) -> str:
    cumulative = 0.0
    for action, probability in zip(ACTION_ORDER, probabilities):
        cumulative += probability
        if uniform_draw < cumulative:
            return action
    return ACTION_ORDER[-1]


def _decision_uniform(run_seed: int, round_index: int, agent_id: str) -> float:
    integer = derive_seed(
        run_seed,
        round_index,
        agent_id,
        namespace="v2-market-common-decision-uniform/1",
    )
    return (integer + 0.5) / float(2**64)


def _limit_uniform(run_seed: int, round_index: int, agent_id: str) -> float:
    integer = derive_seed(
        run_seed,
        round_index,
        agent_id,
        namespace="v2-market-common-limit-uniform/1",
    )
    return (integer + 0.5) / float(2**64)


def _limit_price(
    last_price_cents: int,
    side: str,
    intensity: float,
    limit_uniform: float,
) -> int:
    """Map urgency plus paired reservation heterogeneity to a finite limit.

    Always-marketable buy-above/sell-below limits would force the previous close
    to win the auction's distance tie every round.  Centering reservation prices
    around the close lets supply and demand reveal a new finite price while the
    total offset remains bounded at five percent.
    """

    urgency = int(round((2.0 * intensity - 1.0) * LIMIT_URGENCY_BPS))
    heterogeneity = int(
        round((2.0 * limit_uniform - 1.0) * LIMIT_HETEROGENEITY_BPS)
    )
    signed_offset = max(
        -MAX_LIMIT_OFFSET_BPS,
        min(MAX_LIMIT_OFFSET_BPS, urgency + heterogeneity),
    )
    multiplier = 10_000 + signed_offset if side == "buy" else 10_000 - signed_offset
    return max(1, (last_price_cents * multiplier + 5_000) // 10_000)


def _intent_from_decision(
    *,
    account: Account,
    action: str,
    intensity: float,
    financing: str,
    last_price_cents: int,
    round_index: int,
    limit_uniform: float,
) -> tuple[OrderIntent | None, str]:
    if action == "hold":
        return None, "sampled_hold"
    if intensity <= 0.0:
        return None, "zero_intensity"
    limit = _limit_price(last_price_cents, action, intensity, limit_uniform)
    if action == "buy":
        spendable = account.cash_cents
        if financing == "credit":
            spendable += account.unused_credit_cents
        intended_value = int(spendable * intensity)
        quantity = intended_value // limit
        empty_reason = "sub_share_buy_budget"
    else:
        quantity = int(account.shares * intensity)
        empty_reason = "sub_share_sell_quantity"
    if quantity <= 0:
        return None, empty_reason
    return (
        OrderIntent(
            order_id=f"r{round_index:05d}-{account.agent_id}",
            agent_id=account.agent_id,
            side=action,  # type: ignore[arg-type]
            quantity=quantity,
            limit_price_cents=limit,
        ),
        "submitted",
    )


def _make_initial_condition(
    *,
    n_agents: int,
    seed_index: int,
    master_seed: int,
) -> _InitialCondition:
    run_seed = derive_seed(
        master_seed,
        seed_index,
        namespace="v2-market-paired-seed/1",
    )
    tape_rng = random.Random(
        derive_seed(run_seed, namespace="v2-market-initial-tape/1")
    )
    first_price = tape_rng.randint(8_000, 12_000)
    price_history = [first_price]
    tape_drift = tape_rng.uniform(-0.0025, 0.0025)
    for _ in range(WARMUP_CLOSES - 1):
        return_value = tape_drift + tape_rng.uniform(-0.0125, 0.0125)
        price_history.append(max(100, int(round(price_history[-1] * (1 + return_value)))))
    volume_history = [
        tape_rng.randint(50 * n_agents, 140 * n_agents)
        for _ in range(WARMUP_CLOSES)
    ]

    account_rng = random.Random(
        derive_seed(run_seed, namespace="v2-market-initial-accounts/1")
    )
    last_price = price_history[-1]
    accounts: list[Account] = []
    metadata: dict[str, AgentTradeMetadata] = {}
    credit_limits: dict[str, int] = {}
    for index in range(n_agents):
        agent_id = f"agent-{index:04d}"
        wealth = INITIAL_WEALTH_MIN_CENTS + account_rng.randrange(
            INITIAL_WEALTH_SPAN_CENTS
        )
        position_bps = INITIAL_POSITION_MIN_BPS + account_rng.randrange(
            INITIAL_POSITION_SPAN_BPS
        )
        shares = (wealth * position_bps // 10_000) // last_price
        cash = wealth - shares * last_price
        accounts.append(Account(agent_id, cash_cents=cash, shares=shares))
        basis_multiplier = account_rng.uniform(0.82, 1.18)
        metadata[agent_id] = AgentTradeMetadata(
            basis_price_cents=max(1, int(round(last_price * basis_multiplier)))
            if shares
            else None
        )
        credit_limits[agent_id] = wealth * CREDIT_LIMIT_BPS // 10_000
    facility_cash = sum(credit_limits.values())

    identity_payload = {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "seed_index": seed_index,
        "run_seed": run_seed,
        "prices": price_history,
        "volumes": volume_history,
        "accounts": [asdict(account) for account in accounts],
        "metadata": {
            key: asdict(value) for key, value in sorted(metadata.items())
        },
        "facility_cash_cents": facility_cash,
        "potential_credit_limits_cents": dict(sorted(credit_limits.items())),
    }
    return _InitialCondition(
        seed_index=seed_index,
        run_seed=run_seed,
        price_history_cents=tuple(price_history),
        volume_history_shares=tuple(volume_history),
        base_accounts=tuple(accounts),
        credit_limits_cents=credit_limits,
        facility_cash_cents=facility_cash,
        metadata=metadata,
        initialization_id=_canonical_hash(identity_payload),
        decision_stream_id=_canonical_hash(
            {
                "schema": "v2-market-common-decision-uniform/1",
                "run_seed": run_seed,
            }
        ),
        limit_stream_id=_canonical_hash(
            {
                "schema": "v2-market-common-limit-uniform/1",
                "run_seed": run_seed,
            }
        ),
    )


def _cell_accounts(
    initial: _InitialCondition, financing: str
) -> tuple[Account, ...]:
    return tuple(
        Account(
            agent_id=account.agent_id,
            cash_cents=account.cash_cents,
            shares=account.shares,
            debt_cents=0,
            credit_limit_cents=(
                initial.credit_limits_cents[account.agent_id]
                if financing == "credit"
                else 0
            ),
        )
        for account in initial.base_accounts
    )


def _update_metadata(
    prior: Mapping[str, AgentTradeMetadata],
    prior_accounts: Sequence[Account],
    post_accounts: Sequence[Account],
    fills: Sequence[Any],
    *,
    round_index: int,
) -> dict[str, AgentTradeMetadata]:
    before = {account.agent_id: account for account in prior_accounts}
    after = {account.agent_id: account for account in post_accounts}
    fill_by_agent = {fill.agent_id: fill for fill in fills}
    updated: dict[str, AgentTradeMetadata] = {}
    for agent_id in sorted(before):
        existing = prior[agent_id]
        fill = fill_by_agent.get(agent_id)
        if fill is None:
            updated[agent_id] = existing
            continue
        basis = existing.basis_price_cents
        last_sale_price = existing.last_sale_price_cents
        last_sale_round = existing.last_sale_round
        if fill.side == "buy":
            old_shares = before[agent_id].shares
            new_shares = after[agent_id].shares
            old_basis = basis if basis is not None else fill.price_cents
            numerator = old_basis * old_shares + fill.price_cents * fill.quantity
            basis = (numerator + new_shares // 2) // new_shares
        else:
            if after[agent_id].shares == 0:
                basis = None
            last_sale_price = fill.price_cents
            last_sale_round = round_index
        updated[agent_id] = AgentTradeMetadata(
            basis_price_cents=basis,
            last_trade_round=round_index,
            last_sale_price_cents=last_sale_price,
            last_sale_round=last_sale_round,
        )
    return updated


def _max_drawdown(prices: Sequence[int]) -> float:
    peak = prices[0]
    worst = 0.0
    for price in prices:
        peak = max(peak, price)
        worst = min(worst, price / peak - 1.0)
    return worst


def _named_ood_diagnostics(
    feature_rows: Sequence[Sequence[float]], reference: OODReference
) -> dict[str, Any]:
    diagnostics = ood_diagnostics(
        feature_rows,
        reference,
        z_threshold=OOD_Z_THRESHOLD,
    )
    diagnostics["feature_order"] = list(FEATURE_ORDER)
    diagnostics["outside_train_range_count_by_feature_name"] = {
        name: diagnostics["outside_train_range_by_feature"][index]
        for index, name in enumerate(FEATURE_ORDER)
    }
    diagnostics["outside_train_range_fraction_by_feature_name"] = {
        name: diagnostics["outside_train_range_fraction_by_feature"][index]
        for index, name in enumerate(FEATURE_ORDER)
    }
    diagnostics["z_exceedance_count_by_feature_name"] = {
        name: diagnostics["z_exceedances_by_feature"][index]
        for index, name in enumerate(FEATURE_ORDER)
    }
    diagnostics["z_exceedance_fraction_by_feature_name"] = {
        name: diagnostics["z_exceedance_fraction_by_feature"][index]
        for index, name in enumerate(FEATURE_ORDER)
    }
    return diagnostics


def _feature_rows_from_runs(
    runs: Sequence[Mapping[str, Any]],
) -> list[list[float]]:
    return [
        [float(decision["effective_state"][name]) for name in FEATURE_ORDER]
        for run in runs
        for round_row in run["rounds"]
        for decision in round_row["decision_records"]
    ]


def _run_cell(
    student: Any,
    initial: _InitialCondition,
    *,
    financing: str,
    behavior: str,
    rounds: int,
    ood_reference: OODReference | None = None,
    on_round_started: Optional[Callable[[int], None]] = None,
    on_round_completed: Optional[Callable[[dict[str, Any]], None]] = None,
) -> dict[str, Any]:
    accounts = _cell_accounts(initial, financing)
    facility = CreditFacility(initial.facility_cash_cents, 0)
    assert_system_invariants(accounts, facility)
    metadata = dict(initial.metadata)
    prices = list(initial.price_history_cents)
    volumes = list(initial.volume_history_shares)
    initial_accounts_json = [asdict(account) for account in accounts]
    initial_metadata_json = {
        key: asdict(value) for key, value in sorted(metadata.items())
    }
    round_ledgers: list[dict[str, Any]] = []
    clamp_counts = {name: 0 for name in FEATURE_ORDER}
    effective_feature_rows: list[list[float]] = []
    cumulative_borrowed = 0

    for round_index in range(rounds):
        if on_round_started is not None:
            on_round_started(round_index)
        snapshot = MarketSnapshot(
            round_index=round_index,
            last_price_cents=prices[-1],
            price_history_cents=tuple(prices),
            accounts=accounts,
            credit_facility=facility,
            financing=financing,  # type: ignore[arg-type]
        )
        intents: list[OrderIntent] = []
        decision_records: list[dict[str, Any]] = []
        for account in accounts:
            built = build_attention_state(
                snapshot,
                account.agent_id,
                volume_history_shares=volumes,
                metadata=metadata[account.agent_id],
            )
            for name in built.clamped_features:
                clamp_counts[name] += 1
            effective_feature_rows.append(list(built.state.to_feature_vector()))
            if behavior == "distilled":
                prediction = _normalise_prediction(
                    student.predict(built.state.to_feature_vector())
                )
                policy_feature_names = list(FEATURE_ORDER)
            else:
                tape = {
                    name: float(getattr(built.state, name))
                    for name in MARKET_FEATURES
                }
                prediction = _normalise_prediction(_momentum_prediction(tape))
                policy_feature_names = list(MARKET_FEATURES)
            uniform_draw = _decision_uniform(
                initial.run_seed, round_index, account.agent_id
            )
            limit_uniform = _limit_uniform(
                initial.run_seed, round_index, account.agent_id
            )
            action = _sample_action(prediction["action_probs"], uniform_draw)
            if action == "buy":
                intensity = prediction["intensities"][0]
            elif action == "sell":
                intensity = prediction["intensities"][1]
            else:
                intensity = 0.0
            intent, disposition = _intent_from_decision(
                account=account,
                action=action,
                intensity=intensity,
                financing=financing,
                last_price_cents=snapshot.last_price_cents,
                round_index=round_index,
                limit_uniform=limit_uniform,
            )
            if intent is not None:
                intents.append(intent)
            decision_records.append(
                {
                    "agent_id": account.agent_id,
                    "common_uniform_draw": uniform_draw,
                    "common_limit_uniform_draw": limit_uniform,
                    "policy_feature_names": policy_feature_names,
                    "raw_features": dict(built.raw_features),
                    "effective_state": built.state.to_dict(),
                    "clamped_features": list(built.clamped_features),
                    "action_probs": list(prediction["action_probs"]),
                    "intensities": list(prediction["intensities"]),
                    "sampled_action": action,
                    "selected_intensity": intensity,
                    "intent_disposition": disposition,
                    "intent": asdict(intent) if intent is not None else None,
                }
            )

        constrained = constrain_orders(
            accounts,
            intents,
            financing=financing,  # type: ignore[arg-type]
        )
        clearing = clear_call_auction(
            constrained,
            last_price_cents=snapshot.last_price_cents,
        )
        settlement = settle(
            accounts,
            facility,
            clearing,
            financing=financing,  # type: ignore[arg-type]
        )
        updated_metadata = _update_metadata(
            metadata,
            accounts,
            settlement.accounts,
            clearing.fills,
            round_index=round_index,
        )
        cumulative_borrowed += settlement.borrowed_cents
        conservation = {
            "cash_including_facility_conserved": (
                settlement.totals_before.cash_cents
                == settlement.totals_after.cash_cents
            ),
            "shares_conserved": (
                settlement.totals_before.shares == settlement.totals_after.shares
            ),
            "debt_equals_facility_loan_asset": (
                settlement.totals_after.debt_cents
                == settlement.totals_after.loan_asset_cents
            ),
            "buy_volume_equals_sell_volume": (
                sum(fill.quantity for fill in clearing.fills if fill.side == "buy")
                == sum(
                    fill.quantity for fill in clearing.fills if fill.side == "sell"
                )
                == clearing.matched_volume_shares
            ),
            "all_fill_limits_satisfied": all(
                (
                    fill.price_cents <= fill.limit_price_cents
                    if fill.side == "buy"
                    else fill.price_cents >= fill.limit_price_cents
                )
                for fill in clearing.fills
            ),
        }
        if not all(conservation.values()):
            raise AssertionError("V2 round conservation audit failed")
        round_ledger = {
                "round_index": round_index,
                "round_start_price_cents": snapshot.last_price_cents,
                "price_cents": clearing.clearing_price_cents,
                "matched_volume_shares": clearing.matched_volume_shares,
                "status": clearing.status,
                "borrowed_cents": settlement.borrowed_cents,
                "cumulative_borrowed_cents": cumulative_borrowed,
                "decision_records": decision_records,
                "submitted_intents": [
                    asdict(intent) for intent in clearing.submitted_intents
                ],
                "accepted_orders": [
                    asdict(order) for order in clearing.accepted_orders
                ],
                "rejected_orders": [
                    asdict(rejection) for rejection in clearing.rejected_orders
                ],
                "candidate_levels": [
                    asdict(level) for level in clearing.candidate_levels
                ],
                "fills": [asdict(fill) for fill in clearing.fills],
                "accounts_before": [asdict(account) for account in accounts],
                "accounts_after": [
                    asdict(account) for account in settlement.accounts
                ],
                "credit_facility_before": asdict(facility),
                "credit_facility_after": asdict(settlement.credit_facility),
                "trade_metadata_after": {
                    key: asdict(value)
                    for key, value in sorted(updated_metadata.items())
                },
                "totals_before": asdict(settlement.totals_before),
                "totals_after": asdict(settlement.totals_after),
                "conservation": conservation,
            }
        round_ledgers.append(round_ledger)
        accounts = settlement.accounts
        facility = settlement.credit_facility
        metadata = updated_metadata
        prices.append(clearing.clearing_price_cents)
        volumes.append(clearing.matched_volume_shares)
        if on_round_completed is not None:
            on_round_completed(round_ledger)

    experiment_prices = prices[WARMUP_CLOSES - 1 :]
    peak = max(experiment_prices)
    peak_index = experiment_prices.index(peak)
    metrics = {
        "final_return": experiment_prices[-1] / experiment_prices[0] - 1.0,
        "max_runup": peak / experiment_prices[0] - 1.0,
        "reversal_from_peak": experiment_prices[-1] / peak - 1.0,
        "peak_round_index": peak_index - 1,
        "max_drawdown": _max_drawdown(experiment_prices),
        "turnover_shares": sum(
            ledger["matched_volume_shares"] for ledger in round_ledgers
        ),
        "locked_rounds": sum(
            ledger["matched_volume_shares"] == 0 for ledger in round_ledgers
        ),
        "credit_used_cents": cumulative_borrowed,
        "ending_debt_cents": sum(account.debt_cents for account in accounts),
        "clamped_feature_values": sum(clamp_counts.values()),
    }
    market_ood = (
        {
            "reference_hash": _canonical_hash(ood_reference.to_dict()),
            "state_kind": "effective_clamped_pre_decision_state",
            "student_consumes_all_features": behavior == "distilled",
            "diagnostics": _named_ood_diagnostics(
                effective_feature_rows, ood_reference
            ),
        }
        if ood_reference is not None
        else None
    )
    return {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "cell": f"{financing}_{behavior}",
        "financing": financing,
        "behavior": behavior,
        "seed_index": initial.seed_index,
        "run_seed": initial.run_seed,
        "paired_initialization_id": initial.initialization_id,
        "paired_decision_stream_id": initial.decision_stream_id,
        "paired_limit_stream_id": initial.limit_stream_id,
        "control_contract": {
            "differing_factors": {
                "financing": financing,
                "policy": behavior,
            },
            "common_initial_cash_and_shares": True,
            "common_initial_21_close_tape": True,
            "common_facility_initial_cash": True,
            "common_uniform_decision_draws": True,
            "common_uniform_limit_draws": True,
            "finite_credit_limit_rule": "zero",
            "credit_limit_rule": f"{CREDIT_LIMIT_BPS}_bps_of_initial_wealth",
            "policy_inputs": (
                list(FEATURE_ORDER)
                if behavior == "distilled"
                else list(MARKET_FEATURES)
            ),
        },
        "initial": {
            "accounts": initial_accounts_json,
            "trade_metadata": initial_metadata_json,
            "price_history_cents": list(initial.price_history_cents),
            "volume_history_shares": list(initial.volume_history_shares),
            "credit_facility": {
                "cash_cents": initial.facility_cash_cents,
                "loan_asset_cents": 0,
            },
            "potential_credit_limits_cents": dict(
                sorted(initial.credit_limits_cents.items())
            ),
        },
        "rounds": round_ledgers,
        "final": {
            "accounts": [asdict(account) for account in accounts],
            "credit_facility": asdict(facility),
            "trade_metadata": {
                key: asdict(value) for key, value in sorted(metadata.items())
            },
            "price_history_cents": prices,
            "volume_history_shares": volumes,
        },
        "clamp_counts": clamp_counts,
        "market_vs_train_ood": market_ood,
        "metrics": metrics,
    }


def _mean(runs: Sequence[Mapping[str, Any]], metric: str) -> float:
    return statistics.fmean(float(run["metrics"][metric]) for run in runs)


def _seed_indices(seeds: int | Sequence[int]) -> tuple[int, ...]:
    if isinstance(seeds, int) and not isinstance(seeds, bool):
        _require_integer("seeds", seeds, 1)
        return tuple(range(seeds))
    if isinstance(seeds, (str, bytes)) or not isinstance(seeds, Sequence):
        raise MarketExperimentError("seeds must be a positive count or integer sequence")
    values = tuple(seeds)
    if not values:
        raise MarketExperimentError("seed sequence cannot be empty")
    for value in values:
        _require_integer("seed index", value, 0)
    if len(values) != len(set(values)):
        raise MarketExperimentError("seed indices must be unique")
    return values


def run_budget_behavior_2x2(
    student: Any,
    *,
    n_agents: int,
    rounds: int,
    seeds: int | Sequence[int],
    master_seed: int,
    ood_reference: OODReference | None = None,
    on_run_started: Optional[Callable[[str, int], None]] = None,
    on_run_completed: Optional[Callable[[dict[str, Any]], None]] = None,
    on_round_started: Optional[Callable[[str, int, int], None]] = None,
    on_round_completed: Optional[
        Callable[[str, int, dict[str, Any]], None]
    ] = None,
) -> dict[str, Any]:
    """Execute and aggregate the four paired V2 engineering-control cells.

    Optional lifecycle callbacks let a managed caller durably persist each
    independent seed/cell ledger before later cells run.  The default remains
    a pure in-memory function with no I/O.
    """

    _require_integer("n_agents", n_agents, 4)
    _require_integer("rounds", rounds, 1)
    _require_integer("master_seed", master_seed, 0)
    seed_indices = _seed_indices(seeds)
    if not callable(getattr(student, "predict", None)):
        raise MarketExperimentError("student must expose predict(feature_vector)")
    if ood_reference is not None and not isinstance(ood_reference, OODReference):
        raise MarketExperimentError("ood_reference must be an OODReference or None")
    if ood_reference is not None and ood_reference.feature_dim != len(FEATURE_ORDER):
        raise MarketExperimentError("ood_reference width does not match FEATURE_ORDER")

    initial_conditions = [
        _make_initial_condition(
            n_agents=n_agents,
            seed_index=seed_index,
            master_seed=master_seed,
        )
        for seed_index in seed_indices
    ]
    runs: list[dict[str, Any]] = []
    for financing, behavior in CELL_ORDER:
        for initial in initial_conditions:
            cell = f"{financing}_{behavior}"
            if on_run_started is not None:
                on_run_started(cell, initial.seed_index)
            run = _run_cell(
                student,
                initial,
                financing=financing,
                behavior=behavior,
                rounds=rounds,
                ood_reference=ood_reference,
                on_round_started=(
                    (
                        lambda round_index, cell=cell, seed_index=initial.seed_index: on_round_started(
                            cell, seed_index, round_index
                        )
                    )
                    if on_round_started is not None
                    else None
                ),
                on_round_completed=(
                    (
                        lambda round_ledger, cell=cell, seed_index=initial.seed_index: on_round_completed(
                            cell, seed_index, round_ledger
                        )
                    )
                    if on_round_completed is not None
                    else None
                ),
            )
            runs.append(run)
            if on_run_completed is not None:
                on_run_completed(run)

    summaries: list[dict[str, Any]] = []
    for financing, behavior in CELL_ORDER:
        cell = f"{financing}_{behavior}"
        cell_runs = [run for run in runs if run["cell"] == cell]
        first = min(cell_runs, key=lambda run: run["seed_index"])
        aggregate_clamps = {
            name: sum(int(run["clamp_counts"][name]) for run in cell_runs)
            for name in FEATURE_ORDER
        }
        summaries.append(
            {
                "cell": cell,
                "financing": financing,
                "behavior": behavior,
                "planned_seeds": len(seed_indices),
                "completed_seeds": len(cell_runs),
                "mean_final_return": _mean(cell_runs, "final_return"),
                "mean_max_runup": _mean(cell_runs, "max_runup"),
                "mean_reversal_from_peak": _mean(
                    cell_runs, "reversal_from_peak"
                ),
                "mean_max_drawdown": _mean(cell_runs, "max_drawdown"),
                "mean_turnover_shares": _mean(cell_runs, "turnover_shares"),
                "mean_locked_rounds": _mean(cell_runs, "locked_rounds"),
                "mean_credit_used_cents": _mean(cell_runs, "credit_used_cents"),
                "mean_ending_debt_cents": _mean(cell_runs, "ending_debt_cents"),
                "representative_seed_index": first["seed_index"],
                "representative_price_path": first["final"][
                    "price_history_cents"
                ][WARMUP_CLOSES - 1 :],
                "clamp_counts": aggregate_clamps,
                "market_vs_train_ood": (
                    {
                        "reference_hash": _canonical_hash(ood_reference.to_dict()),
                        "state_kind": "effective_clamped_pre_decision_state",
                        "student_consumes_all_features": behavior == "distilled",
                        "diagnostics": _named_ood_diagnostics(
                            _feature_rows_from_runs(cell_runs), ood_reference
                        ),
                    }
                    if ood_reference is not None
                    else None
                ),
            }
        )

    initialization_ids = {
        str(initial.seed_index): initial.initialization_id
        for initial in initial_conditions
    }
    decision_stream_ids = {
        str(initial.seed_index): initial.decision_stream_id
        for initial in initial_conditions
    }
    limit_stream_ids = {
        str(initial.seed_index): initial.limit_stream_id
        for initial in initial_conditions
    }
    result = {
        "schema_version": MARKET_EXPERIMENT_SCHEMA_VERSION,
        "descriptor": market_experiment_descriptor(),
        "paired_design": {
            "seed_indices": list(seed_indices),
            "initialization_ids": initialization_ids,
            "decision_stream_ids": decision_stream_ids,
            "limit_stream_ids": limit_stream_ids,
            "factors": {
                "financing": list(BUDGET_MODES),
                "policy": list(BEHAVIOR_MODES),
            },
            "only_cell_differences": ["financing", "policy"],
        },
        "planned_runs": 4 * len(seed_indices),
        "honest_n_market_runs": len(runs),
        "runs": runs,
        "cell_summaries": summaries,
        "market_vs_train_ood": (
            {
                "schema_version": "v2-market-vs-train-ood/0.1",
                "reference_hash": _canonical_hash(ood_reference.to_dict()),
                "reference_feature_order": list(FEATURE_ORDER),
                "state_kind": "effective_clamped_pre_decision_state",
                "support_geometry": "per_feature_train_min_max_rectangle",
                "joint_support_assessed": False,
                "all_cells": _named_ood_diagnostics(
                    _feature_rows_from_runs(runs), ood_reference
                ),
                "by_cell": {
                    summary["cell"]: summary["market_vs_train_ood"]
                    for summary in summaries
                },
                "interpretation": (
                    "rectangular train-support diagnostic only; outside-range "
                    "states are not automatically invalid; joint/manifold "
                    "support and distribution shift are not assessed"
                ),
            }
            if ood_reference is not None
            else None
        ),
    }
    # Exercise the exact public return tree rather than merely assuming its
    # dataclass conversions are JSON-safe.
    json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False)
    return result


__all__ = [
    "AgentTradeMetadata",
    "BEHAVIOR_MODES",
    "BUDGET_MODES",
    "CELL_ORDER",
    "FeatureBuildResult",
    "LEDGER_SCHEMA_VERSION",
    "MARKET_EXPERIMENT_SCHEMA_VERSION",
    "MarketExperimentError",
    "build_attention_state",
    "market_experiment_descriptor",
    "run_budget_behavior_2x2",
]
