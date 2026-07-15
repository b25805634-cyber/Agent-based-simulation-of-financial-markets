"""Market clearing: impact-based, NOT a microstructure LOB.

Price moves with the net order-flow imbalance at a coarse timescale. A
market-maker is assumed to absorb the imbalance, so orders execute at the
cleared price; `limit_price` is recorded for later analysis but not enforced.
This is intentional (see BUILD_BRIEF guardrails).
"""
from __future__ import annotations
from typing import Iterable
from .types import Order


def clear_by_pressure(orders: Iterable[Order], last_price: float, kappa: float):
    """Return (new_price, volume, fills) given this round's orders."""
    orders = list(orders)
    buy_q = sum(o["quantity"] for o in orders if o["side"] == "buy")
    sell_q = sum(o["quantity"] for o in orders if o["side"] == "sell")
    depth = buy_q + sell_q
    if depth == 0:
        return last_price, 0, {}
    net = buy_q - sell_q                       # >0 net buying, <0 net selling
    new_price = round(max(0.01, last_price * (1 + kappa * net / depth)), 2)
    fills: dict[str, int] = {}
    for o in orders:
        if o["quantity"] > 0 and o["side"] in ("buy", "sell"):
            signed = o["quantity"] if o["side"] == "buy" else -o["quantity"]
            fills[o["agent"]] = fills.get(o["agent"], 0) + signed
    return new_price, min(buy_q, sell_q), fills
