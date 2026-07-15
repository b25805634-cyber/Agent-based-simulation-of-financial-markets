"""Leverage + forced-liquidation — the market AMPLIFIER (V2: FROZEN-REFERENCE,
PRICE-ONLY margin).

Each leveraged agent carries a FIXED reference leveraged long: `lev_ref_shares`
financed by `lev_debt`, established at t0 so the starting equity ratio is 1/L.
The reference long is marked to market on PRICE ALONE:

    equity_ratio(price) = (lev_ref_shares*price - lev_debt) / (lev_ref_shares*price)
                        = 1 - (1 - 1/L) * p0 / price          # price is the ONLY input

When it falls below the maintenance margin the reference long is force-liquidated
ONCE — `lev_ref_shares` of sell pressure enters the NEXT round's net flow via a
phantom seller. Falling price -> margin calls -> forced selling -> falling price
is the deleveraging cascade (Brunnermeier-Pedersen).

WHY price-only / frozen-reference (this is the V2 redesign):
  The earlier "Model A" marked margin against the agent's CURRENT shares with a
  fixed loan. An adversarial review showed that made VOLUNTARY selling (and the
  engine's unconstrained shorting) the dominant trigger — breaches fired at
  prices ABOVE 100, a position-shrink artifact, NOT the price cascade we want to
  study. Marking a FROZEN reference long against PRICE ONLY removes that entirely:
    - independent of the agent's voluntary trading / share balance -> no
      selling-artifact, no interaction with negative (short) inventory;
    - the leverage book NEVER touches the agent's shares/cash, so it never leaks
      into the LLM prompt (V1 intent: agents are not told their margin state —
      leverage reaches them only through the price tape it moves);
    - each agent liquidates at most ONCE -> no stranded-bad-debt / buyback re-fire.

Heterogeneous leverage: ratios are STAGGERED across the cohort in
[center-spread, center+spread] so breach prices are staggered (a real cascade
that walks the price down through thresholds, not one synchronized dump).

Determinism: init iterates a stable (fuel-first, then declared order) list and
assigns ratios by index; margin checks are a pure function of price. repro stays
green and the leverage book consumes NO RNG, so the no-leverage path is byte-identical.
"""
from __future__ import annotations
from dataclasses import dataclass
from .types import Order

# Phantom seller for forced-liquidation flow: contributes price impact to clearing
# but is NOT a real agent, so the normal fill loop (which iterates real agents)
# never fills it. The leverage book is settled separately, here.
LIQUIDATION_AGENT = "margin_liquidation"

_FUEL_IDS = ("retail_crowd", "fomo_momentum")


@dataclass
class LiquidationEvent:
    round: int
    agent: str
    shares: int          # reference-long shares dumped
    price: float         # price at which the long was force-liquidated
    threshold: float     # the agent's breach price (purely a function of its L)
    pnl: float           # residual equity recovered at liquidation = proceeds - loan.
                         # Liquidation fires AT the maintenance floor while still
                         # solvent, so this is ~maintenance*position (POSITIVE, not a
                         # loss); the position's loss from p0->breach happened earlier.


def breach_price(L: float, p0: float, m: float) -> float:
    """Price at/below which a reference long of leverage L breaches maintenance m."""
    return (1.0 - 1.0 / L) * p0 / (1.0 - m)


def init_leverage(agents, cfg) -> None:
    """Give a `leverage_fraction` of the LLM agents (fuel-class first) a FROZEN
    reference leveraged long, with leverage L STAGGERED across the cohort so
    breach prices are spread. No-op when leverage is disabled."""
    if not cfg.leverage_enabled:
        return
    llm = [a for a in agents if a.is_llm]
    ordered = ([a for a in llm if a.persona_id in _FUEL_IDS] +
               [a for a in llm if a.persona_id not in _FUEL_IDS])
    chosen = ordered[: int(round(cfg.leverage_fraction * len(llm)))]
    n = len(chosen)
    p0 = cfg.initial_price
    lo, hi = cfg.leverage_ratio - cfg.leverage_spread, cfg.leverage_ratio + cfg.leverage_spread
    for i, a in enumerate(chosen):
        Li = cfg.leverage_ratio if n <= 1 else (lo + (hi - lo) * i / (n - 1))
        a.is_leveraged = True
        a.lev_ratio = Li
        a.lev_ref_shares = a.shares                 # frozen reference long (= initial 50)
        a.lev_debt = (1.0 - 1.0 / Li) * a.shares * p0
        a.lev_liquidated = False
        a.lev_pnl = 0.0


def equity_ratio(a, price: float) -> float:
    pos = a.lev_ref_shares * price
    if pos <= 0:
        return 0.0
    return (pos - a.lev_debt) / pos


def margin_calls(agents, price: float, cfg, round_i: int):
    """Force-liquidate every leveraged reference long whose PRICE-marked equity
    ratio has breached maintenance (each at most once). Returns
    (sell_orders_for_next_round, events). The leverage book is settled here; the
    agent's voluntary shares/cash are untouched."""
    orders: list[Order] = []
    events: list[LiquidationEvent] = []
    if not cfg.leverage_enabled:
        return orders, events
    m = cfg.maintenance_margin
    for a in agents:
        if not getattr(a, "is_leveraged", False) or getattr(a, "lev_liquidated", True):
            continue
        if equity_ratio(a, price) >= m:
            continue
        sh = a.lev_ref_shares
        a.lev_pnl = sh * price - a.lev_debt        # residual equity recovered (analysis only; NOT shown to LLM)
        a.lev_liquidated = True
        thr = breach_price(a.lev_ratio, cfg.initial_price, m)
        events.append(LiquidationEvent(round_i, a.name, int(sh), round(price, 2),
                                       round(thr, 2), round(a.lev_pnl, 2)))
        orders.append(Order(agent=LIQUIDATION_AGENT, side="sell", quantity=int(sh),
                            limit_price=price, sentiment=0.0, public_take="",
                            rationale=f"margin liquidation (frozen ref) of {a.name}"))
    return orders, events
