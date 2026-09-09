"""Pure, explicitly versioned daily information-market research mechanism.

The policy sees numeric observations and its own account only.  The library
does not call a Provider, create runs, write files, or propagate explanations.
Its accounting/news scenario is synthetic and is not human-behavior evidence.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import math
import random
import statistics
from typing import Any, Callable, Mapping, Sequence

from nmsim.information_weight import (
    ACCOUNT8, F8, FIELD_RANGES, N8, P8, PROFILE_FIELDS, PROFILE_IDS,
)
from nmsim.v2_attention import ACTION_ORDER, derive_seed
from nmsim.v2_market import (
    Account, CreditFacility, OrderIntent, clear_call_auction, constrain_orders,
    settle,
)
from nmsim.v2_market_experiment import (
    AgentTradeMetadata, _limit_price, _market_features, _normalise_prediction,
    _sample_action, _update_metadata,
)

SCHEMA_VERSION = "information-market/1.0.0"
AVAILABLE_SCHEMA_VERSION = "information-market/1.1.0"
SIZING_SCHEMA_VERSION = "information-market/1.2.0"
OBSERVATION_POLICIES = ("legacy_proxy", "available_only")
SIZING_POLICIES = ("legacy_mean", "distribution_mean", "distribution_sampled")
WORLD_SCHEMA_VERSION = "synthetic-daily-company-news/1.0.0"
PROJECTION_SCHEMA_VERSION = "information-market-explicit-domain-projection/1.0.0"
QUOTE_SCHEMAS = {
    "independent": "independent-reservation-offset-200bps/1.0.0",
    "legacy_intensity_linked": "v2-intensity-urgency300-reservation200-cap500bps/1.0.0",
}
Policy = Callable[[Mapping[str, float], Mapping[str, float]], Mapping[str, Any]]


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _integer(name: str, value: int, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _uniform(seed: int, day: int, agent_id: str, purpose: str) -> float:
    return (derive_seed(seed, day, agent_id, namespace=f"information-market/{purpose}/1")
            + 0.5) / float(2**64)


def descriptor(observation_policy: str = "legacy_proxy", sizing_policy: str = "legacy_mean") -> dict[str, Any]:
    """Fixed scientific assumptions that the managed entrypoint must identify."""
    if observation_policy not in OBSERVATION_POLICIES:
        raise ValueError("unknown observation policy")
    if sizing_policy not in SIZING_POLICIES:
        raise ValueError("unknown sizing policy")
    if sizing_policy != "legacy_mean" and observation_policy != "available_only":
        raise ValueError("distributional sizing requires available_only observations")
    result = {
        "schema": SCHEMA_VERSION,
        "world_schema": WORLD_SCHEMA_VERSION,
        "projection_schema": PROJECTION_SCHEMA_VERSION,
        "quote_schemas": dict(QUOTE_SCHEMAS),
        "profiles": {key: list(value) for key, value in PROFILE_FIELDS.items()},
        "time": "observe close t and releases through t; settle one batch at t+1",
        "warmup_closes": 21,
        "initial_price_cents": 10000,
        "initial_cash_cents_range": [1000000, 4000000],
        "initial_shares_range": [50, 250],
        "financing": "finite; no borrowing, fees, dividends, issuance or redemption",
        "world_schedule": "one public event/report each 10 days; deterministic seeded scenario",
        "company_initial_assets_per_share_cents": 10000,
        "company_initial_liabilities_fraction": 0.4,
        "company_base_daily_revenue_per_share_cents": 40,
        "company_net_margin": 0.08,
        "company_operating_margin": 0.12,
        "company_operating_cashflow_margin": 0.09,
        "company_current_assets_fraction": 0.3,
        "company_current_liabilities_fraction": 0.25,
        "company_accounting_year_sessions": 252,
        "company_revenue_seasonal_amplitude": 0.06,
        "company_revenue_seasonal_divisor": 13,
        "event_surprises": [-0.8, -0.4, 0.0, 0.4, 0.8],
        "event_affected_fraction_range": [0.05, 0.4],
        "event_stated_duration_days": 10,
        "news_modes": ["eventful", "neutral"],
        "intraday_range_approximation": (
            "mean absolute close-to-close return for latest five sessions; "
            "daily call has no intraday tape; approximation is structurally OOD"
        ),
        "zero_turnover_reference": (
            "zero/zero maps to 0; positive/zero maps to 5 with explicit undefined-ratio audit"
        ),
        "domain_projection": "raw values retained; effective closed-domain inputs and counts recorded",
        "evidence_scope": "synthetic exploratory simulation, no human validation",
    }
    if observation_policy == "available_only":
        result.update(schema=AVAILABLE_SCHEMA_VERSION, observation_policy=observation_policy,
                      unavailable_fields=["intraday_range_5d_mean"],
                      conditionally_unavailable_fields={"turnover_change_5d": "prior-five volume mean is zero"},
                      effective_profile_fields={key: [field for field in fields
                          if field != "intraday_range_5d_mean"] for key, fields in PROFILE_FIELDS.items()},
                      intraday_range_approximation=None,
                      zero_turnover_reference="undefined turnover ratio omitted, including zero/zero",
                      missing_information_rule="unobserved field omitted; encoder mask=0, not observed zero")
    if sizing_policy != "legacy_mean":
        result.update(schema=SIZING_SCHEMA_VERSION, sizing_policy=sizing_policy,
                      intensity_sampling_namespace="information-market/sizing/1",
                      quantity_rounding="unchanged floor of feasible cash/share fraction",
                      action_model="unchanged original action probabilities",
                      sizing_control="conditional distribution mean versus draw from the same distribution")
    return result


def build_world(*, seed: int, rounds: int, total_shares: int,
                news_mode: str = "eventful") -> list[dict[str, Any]]:
    """Precommit exogenous accounting and releases, independently of policy/mix.

The latest released report is frozen between report days.  Market-dependent
ratios are derived separately using the actually observed price.  Although the
entire scenario is returned for audit, the policy receives only day-t scalars.
"""
    _integer("seed", seed)
    _integer("rounds", rounds, 1)
    _integer("total_shares", total_shares, 1)
    if news_mode not in ("eventful", "neutral"):
        raise ValueError("news_mode must be eventful or neutral")
    rng = random.Random(derive_seed(seed, namespace="information-market-world/1"))
    base = total_shares * 40
    revenues = [base] * 504
    assets = total_shares * 10000
    liabilities = assets * 4 // 10
    result = []
    event: dict[str, Any] = {}
    report: dict[str, Any] = {}
    for day in range(rounds):
        if day % 10 == 0:
            event = {
                "event_id": f"event-{day:06d}", "release_day": day,
                "signed_event_surprise": rng.choice([-0.8, -0.4, 0.0, 0.4, 0.8]),
                "affected_revenue_fraction": rng.uniform(0.05, 0.4),
                "official_source_mask": 1,
                "scheduled_event_mask": int((day // 10) % 2 == 0),
                "stated_duration_days": 10,
                "source_disagreement": rng.uniform(0.0, 0.3),
            }
            if news_mode == "neutral":
                event["signed_event_surprise"] = 0.0
                event["affected_revenue_fraction"] = 0.0
        daily_revenue = round(base * (1 + 0.06 * math.sin(day / 13)) * (
            1 + event["signed_event_surprise"] * event["affected_revenue_fraction"]))
        revenues.append(daily_revenue)
        assets += round(daily_revenue * 0.08)  # retained earnings, no distributions
        if day % 10 == 0:
            revenue = sum(revenues[-252:])
            report = {
                "release_day": day, "period_end_day": day,
                "total_assets_cents": assets, "total_liabilities_cents": liabilities,
                "common_equity_cents": assets - liabilities,
                "current_assets_cents": round(0.3 * assets),
                "current_liabilities_cents": round(0.25 * liabilities),
                "revenue_ttm_cents": revenue,
                "revenue_previous_year_cents": sum(revenues[-504:-252]),
                "net_income_ttm_cents": round(0.08 * revenue),
                "operating_income_ttm_cents": round(0.12 * revenue),
                "operating_cashflow_ttm_cents": round(0.09 * revenue),
            }
        elapsed = day - event["release_day"]
        news = {name: event[name] for name in N8 if name in event}
        news.update(age_20d_scaled=min(1.0, elapsed / 20),
                    confirmation_count_scaled=min(1.0, (1 + elapsed // 2) / 10),
                    duration_20d_scaled=event["stated_duration_days"] / 20)
        result.append({"day": day, "report": dict(report), "event": dict(event),
                       "news": news})
    return result


def fundamental_fields(report: Mapping[str, Any], price_cents: int,
                       total_shares: int) -> dict[str, float]:
    _integer("price_cents", price_cents, 1)
    _integer("total_shares", total_shares, 1)
    cap = price_cents * total_shares
    assets = report["total_assets_cents"]
    revenue = report["revenue_ttm_cents"]
    return dict(zip(F8, (
        report["net_income_ttm_cents"] / cap,
        revenue / report["revenue_previous_year_cents"] - 1,
        report["total_liabilities_cents"] / assets,
        report["operating_income_ttm_cents"] / revenue,
        report["net_income_ttm_cents"] / assets,
        report["operating_cashflow_ttm_cents"] / assets,
        report["common_equity_cents"] / cap,
        report["current_assets_cents"] / report["current_liabilities_cents"],
    )))


def project_fields(raw: Mapping[str, float]) -> tuple[dict[str, float], list[dict[str, Any]]]:
    """Project only explicitly, retaining every changed field's raw value."""
    effective, changes = {}, []
    for name, value in raw.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{name} must be finite numeric input")
        low, high = FIELD_RANGES[name]
        bounded = min(high, max(low, value))
        effective[name] = bounded
        if bounded != value:
            changes.append({"field": name, "raw": value, "effective": bounded})
    return effective, changes


def _account_fields(account: Account, meta: AgentTradeMetadata, price: int,
                    day: int) -> dict[str, float]:
    wealth = account.cash_cents + account.shares * price
    known = account.shares > 0 and meta.basis_price_cents is not None
    return {
        "position_fraction": account.shares * price / wealth if wealth else 0.0,
        "unrealized_return": price / meta.basis_price_cents - 1 if known else 0.0,
        "unrealized_return_mask": int(known),
        "days_since_trade_scaled": min(1.0, (day - meta.last_trade_round) / 20)
        if meta.last_trade_round is not None else 0.0,
        "days_since_trade_scaled_mask": int(meta.last_trade_round is not None),
        "post_sale_return": price / meta.last_sale_price_cents - 1
        if meta.last_sale_price_cents is not None else 0.0,
        "post_sale_return_mask": int(meta.last_sale_price_cents is not None),
        "log10_wealth": math.log10(max(1, wealth)),
    }


def profile_allocation(agents: int, profile_counts: Mapping[str, int] | None = None,
                       profile_weights: Mapping[str, float] | None = None) -> dict[str, int]:
    _integer("agents", agents, 2)
    if profile_counts is not None and profile_weights is not None:
        raise ValueError("choose profile_counts or profile_weights")
    source = profile_counts if profile_counts is not None else profile_weights
    if source is not None and (not isinstance(source, Mapping) or set(source) - set(PROFILE_IDS)):
        raise ValueError("unknown profile allocation")
    if profile_counts is not None:
        counts = {key: _integer(key, profile_counts.get(key, 0)) for key in PROFILE_IDS}
        if sum(counts.values()) != agents:
            raise ValueError("profile counts must sum to agents")
        return counts
    weights = {key: 1.0 if profile_weights is None else profile_weights.get(key, 0.0)
               for key in PROFILE_IDS}
    if any(isinstance(x, bool) or not isinstance(x, (float, int)) or
           not math.isfinite(x) or x < 0 for x in weights.values()) or not 0 < sum(weights.values()) < math.inf:
        raise ValueError("profile weights must be finite nonnegative with positive sum")
    fractional = {key: value * agents / sum(weights.values()) for key, value in weights.items()}
    counts = {key: math.floor(value) for key, value in fractional.items()}
    ranked = sorted(PROFILE_IDS, key=lambda key: (-(fractional[key] - counts[key]), PROFILE_IDS.index(key)))
    for key in ranked[:agents - sum(counts.values())]:
        counts[key] += 1
    return counts


def quote_price(price_cents: int, action: str, intensity: float,
                uniform: float, rule: str = "independent") -> int:
    _integer("price_cents", price_cents, 1)
    if action not in ("buy", "sell"):
        raise ValueError("quote action must be buy or sell")
    for name, value in (("intensity", intensity), ("uniform", uniform)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
            raise ValueError(f"{name} must lie in [0,1]")
    if rule not in QUOTE_SCHEMAS:
        raise ValueError("unknown quote rule")
    # Holding legacy urgency at zero (intensity=.5) isolates amount from quote.
    return _limit_price(price_cents, action,
                        intensity if rule == "legacy_intensity_linked" else 0.5, uniform)


def prior_policy(action_probs: Sequence[float] = (1 / 3, 1 / 3, 1 / 3),
                 intensities: Sequence[float] = (0.5, 0.5)) -> Policy:
    validated = _normalise_prediction({"action_probs": action_probs, "intensities": intensities})

    def predict(visible_fields: Mapping[str, float], account_state: Mapping[str, float]) -> dict[str, Any]:
        return deepcopy(validated)
    return predict


def random_policy(visible_fields: Mapping[str, float], account_state: Mapping[str, float]) -> dict[str, Any]:
    """Uniform direction null; run_market supplies independent seeded draws."""
    return {"action_probs": [1 / 3, 1 / 3, 1 / 3], "intensities": [0.5, 0.5]}


def run_market(policy: Policy, *, seed: int = 0, agents: int = 40, rounds: int = 60,
               profile_counts: Mapping[str, int] | None = None,
               profile_weights: Mapping[str, float] | None = None,
               quote_rule: str = "independent",
               news_mode: str = "eventful",
               observation_policy: str = "legacy_proxy",
               sizing_policy: str = "legacy_mean",
               on_round: Callable[[Mapping[str, Any]], None] | None = None) -> dict[str, Any]:
    """Run finite-budget synchronous trading, preserving public replay evidence.

    All agents get the same policy callable, with their own numeric account and
    selected field set.  No profile label, future scenario, or other agent's
    account is passed to that callable.  It must be a pure callable for replay.
    """
    _integer("seed", seed)
    _integer("rounds", rounds, 1)
    counts = profile_allocation(agents, profile_counts, profile_weights)
    effective_descriptor = descriptor(observation_policy, sizing_policy)
    available_only = observation_policy == "available_only"
    use_distribution = sizing_policy != "legacy_mean"
    if use_distribution:
        from nmsim.intensity_distribution import validate_distribution, distribution_mean, quantile_sample
    if quote_rule not in QUOTE_SCHEMAS:
        raise ValueError("unknown quote rule")
    if not callable(policy):
        raise ValueError("policy must be callable")
    rng = random.Random(derive_seed(seed, namespace="information-market-initial/1"))
    accounts = tuple(Account(f"a{index:06d}", rng.randint(1000000, 4000000),
                             rng.randint(50, 250)) for index in range(agents))
    initial_accounts = [asdict(account) for account in accounts]
    assignments = [profile for profile in PROFILE_IDS for _ in range(counts[profile])]
    # Allocate independently of wealth, preserving matched accounts across arms.
    random.Random(derive_seed(seed, namespace="information-market-allocation/1")).shuffle(assignments)
    profiles = {account.agent_id: assignments[index] for index, account in enumerate(accounts)}
    total_shares = sum(account.shares for account in accounts)
    initial_cash = sum(account.cash_cents for account in accounts)
    world = build_world(seed=seed, rounds=rounds, total_shares=total_shares, news_mode=news_mode)
    warm_rng = random.Random(derive_seed(seed, namespace="information-market-warmup/1"))
    prices = [10000]
    for _ in range(20):
        prices.append(max(1, round(prices[-1] * (1 + warm_rng.uniform(-0.01, 0.01)))))
    # Normalize so all initial position bases equal the first observed close.
    prices = [max(1, round(price * 10000 / prices[-1])) for price in prices]
    volumes = [warm_rng.randint(max(1, total_shares // 100), max(2, total_shares // 10)) for _ in prices]
    warmup = {"price_cents": list(prices), "volume_shares": list(volumes),
              "synthetic": True, "historical_transactions_replayed": False}
    metadata = {account.agent_id: AgentTradeMetadata(10000) for account in accounts}
    ledger, probes = [], []
    clips, selected = Counter(), Counter()
    undefined_turnover_count = 0
    sizing_counts = Counter()
    closed_agents = set()
    for day in range(rounds):
        price = prices[-1]
        raw_market = _market_features(prices, volumes)
        if not available_only:
            raw_market["intraday_range_5d_mean"] = statistics.fmean(
                abs(prices[index] / prices[index - 1] - 1) for index in range(len(prices) - 5, len(prices)))
        volume_reference = statistics.fmean(volumes[-6:-1])
        undefined_turnover = volume_reference == 0 and (available_only or volumes[-1] > 0)
        undefined_turnover_count += int(undefined_turnover)
        if volume_reference or not available_only:
            raw_market["turnover_change_5d"] = (volumes[-1] / volume_reference - 1 if volume_reference
                                               else (5.0 if volumes[-1] else 0.0))
        raw_market.update(fundamental_fields(world[day]["report"], price, total_shares))
        raw_market.update(world[day]["news"])
        effective_market, market_projection = project_fields(raw_market)
        clips.update(item["field"] for item in market_projection)
        decisions, intents = [], []
        probed_profiles = set()
        for account in accounts:
            profile = profiles[account.agent_id]
            visible = {name: effective_market[name] for name in PROFILE_FIELDS[profile]
                       if name in effective_market}
            raw_account = _account_fields(account, metadata[account.agent_id], price, day)
            effective_account, account_projection = project_fields(raw_account)
            clips.update(item["field"] for item in account_projection)
            raw_prediction = policy(dict(visible), dict(effective_account))
            prediction = _normalise_prediction(raw_prediction)
            sizing_detail = {}
            if use_distribution:
                laws = raw_prediction.get("intensity_distributions")
                if not isinstance(laws, Mapping) or set(laws) != {"buy", "sell"}:
                    raise ValueError("distributional sizing needs explicit buy/sell distributions")
                laws = {side: validate_distribution(laws[side]) for side in ("buy", "sell")}
                means = [distribution_mean(laws[side]) for side in ("buy", "sell")]
                sizing_detail = {"intensity_distributions": laws, "intensity_means": means,
                                 "sizing_policy": sizing_policy}
            action = _sample_action(prediction["action_probs"], _uniform(seed, day, account.agent_id, "decision"))
            intensity = prediction["intensities"][0 if action == "buy" else 1] if action != "hold" else 0.0
            if use_distribution and action != "hold":
                draw = _uniform(seed, day, account.agent_id, "sizing")
                intensity = (distribution_mean(laws[action]) if sizing_policy == "distribution_mean"
                             else quantile_sample(laws[action], draw))
                sizing_detail["sizing_uniform"] = draw if sizing_policy == "distribution_sampled" else None
            selected[action] += 1
            quantity, status = 0, "sampled_hold"
            if action != "hold":
                limit = quote_price(price, action, intensity, _uniform(seed, day, account.agent_id, "quote"), quote_rule)
                quantity = (int(account.cash_cents * intensity) // limit if action == "buy"
                            else int(account.shares * intensity))
                status = "submitted" if quantity else "sub_share_or_zero_intensity"
                if quantity:
                    intents.append(OrderIntent(f"d{day:06d}-{account.agent_id}", account.agent_id,
                                               action, quantity, limit))
                    if use_distribution and intensity == 1:
                        sizing_counts["full_"+action+"_intents"] += 1
                elif use_distribution and action == "sell" and account.shares == 1 and intensity > 0:
                    sizing_counts["one_share_positive_sell_without_order"] += 1
            decisions.append({"agent_id": account.agent_id, "profile_id": profile,
                              "account_state_raw": raw_account, "account_state": effective_account,
                              "account_projection": account_projection, **prediction,
                              "action": action, "intensity": intensity, "order_status": status, **sizing_detail})
            if available_only:
                decisions[-1]["visible_fields"] = dict(visible)
            if profile not in probed_profiles:
                probes.append({"decision_day": day, "agent_id": account.agent_id,
                               "profile_id": profile, "visible_fields": visible,
                               "account_state": effective_account})
                probed_profiles.add(profile)
        constrained = constrain_orders(accounts, intents, financing="finite")
        clearing = clear_call_auction(constrained, last_price_cents=price)
        settled = settle(accounts, CreditFacility(0), clearing, financing="finite")
        if use_distribution:
            after = {account.agent_id: account for account in settled.accounts}
            for account in accounts:
                if account.shares > 0 and after[account.agent_id].shares == 0:
                    sizing_counts["closed_position_events"] += 1
                    closed_agents.add(account.agent_id)
        metadata = _update_metadata(metadata, accounts, settled.accounts, clearing.fills,
                                    round_index=day + 1)
        accounts = settled.accounts
        if (sum(a.cash_cents for a in accounts) != initial_cash or
                sum(a.shares for a in accounts) != total_shares):
            raise RuntimeError("finite-market conservation failed")
        prices.append(clearing.clearing_price_cents)
        volumes.append(clearing.matched_volume_shares)
        row = {"decision_day": day, "settlement_day": day + 1,
               "information_cutoff_day": day, "report_release_day": world[day]["report"]["release_day"],
               "event_release_day": world[day]["event"]["release_day"],
               "market_raw": raw_market, "market_effective": effective_market,
               "market_projection": market_projection, "undefined_turnover_ratio": undefined_turnover,
               "decisions": decisions, "clearing": asdict(clearing),
               "accounts_after": [asdict(account) for account in accounts],
               "conservation_passed": True}
        ledger.append(row)
        if on_round is not None:
            on_round(deepcopy(row))
    simulated_prices = prices[20:]
    returns = [right / left - 1 for left, right in zip(simulated_prices, simulated_prices[1:])]
    peak = simulated_prices[0]
    max_drawdown = 0.0
    for price in simulated_prices:
        peak = max(peak, price)
        max_drawdown = min(max_drawdown, price / peak - 1)
    return {
        "schema": SIZING_SCHEMA_VERSION if use_distribution else AVAILABLE_SCHEMA_VERSION if available_only else SCHEMA_VERSION,
        "config": {"seed": seed, "agents": agents, "rounds": rounds,
                   "profile_counts": counts, "quote_rule": quote_rule,
                   "news_mode": news_mode,
                   "quote_schema": QUOTE_SCHEMAS[quote_rule], "descriptor": effective_descriptor,
                   **({"observation_policy": observation_policy} if available_only else {}),
                   **({"sizing_policy": sizing_policy} if use_distribution else {})},
        "world_hash": _hash({"schema": WORLD_SCHEMA_VERSION, "schedule": world}),
        "world_schedule": world, "warmup": warmup, "initial_accounts": initial_accounts,
        "profile_assignments": profiles, "ledger": ledger, "state_probes": probes,
        "final_account_metadata": {key: asdict(value) for key, value in metadata.items()},
        "summary": {"market_runs": 1, "rounds": rounds, "agent_decisions": agents * rounds,
                    "teacher_logical_requests": 0, "teacher_physical_attempts": 0,
                    "human_participants": 0, "action_counts": dict(selected),
                    "initial_cash_cents": initial_cash, "final_cash_cents": sum(a.cash_cents for a in accounts),
                    "initial_shares": total_shares, "final_shares": sum(a.shares for a in accounts),
                    "initial_price_cents": simulated_prices[0], "final_price_cents": simulated_prices[-1],
                    "price_return": simulated_prices[-1] / simulated_prices[0] - 1,
                    "realized_volatility_annualized": statistics.pstdev(returns) * math.sqrt(252),
                    "max_drawdown": max_drawdown,
                    "matched_volume_shares": sum(volumes[21:]),
                    "no_trade_rounds": sum(row["clearing"]["matched_volume_shares"] == 0 for row in ledger),
                    "conservation_passed": True, "domain_projection_counts": dict(clips),
                    "domain_projection_count_unit": "once per common market field per round plus once per account field per decision",
                    "undefined_turnover_ratio_rounds": undefined_turnover_count,
                    "intraday_range_approximation_rounds": 0 if available_only else rounds,
                    "structural_ood": (["unobserved intraday range omitted; new visibility pattern needs fidelity evidence"]
                                       if available_only else ["daily-close-range proxy replaces unobserved intraday range"]),
                    "evidence_scope": "exploratory synthetic simulation; human resemblance unvalidated"},
        **({"sizing_audit": {"policy": sizing_policy, "counts": dict(sizing_counts),
                             "agents_ever_closed_position": len(closed_agents),
                             "quantity_rounding": "floor_unchanged", "new_teacher_requests": 0}}
           if use_distribution else {}),
    }


__all__ = ["SCHEMA_VERSION", "AVAILABLE_SCHEMA_VERSION", "SIZING_SCHEMA_VERSION", "OBSERVATION_POLICIES", "SIZING_POLICIES", "WORLD_SCHEMA_VERSION", "PROJECTION_SCHEMA_VERSION",
           "QUOTE_SCHEMAS", "descriptor", "build_world", "fundamental_fields",
           "project_fields", "profile_allocation", "quote_price", "prior_policy",
           "random_policy", "run_market"]
