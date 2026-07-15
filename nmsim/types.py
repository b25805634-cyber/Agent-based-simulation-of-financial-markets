"""Shared typed structures used across the package."""
from __future__ import annotations
from typing import TypedDict, Literal

Side = Literal["buy", "sell", "hold"]


class Order(TypedDict):
    agent: str
    side: Side
    quantity: int
    limit_price: float
    sentiment: float      # agent stance in [-1, 1] (their narrative direction/conviction)
    public_take: str      # one punchy sentence others see — this is what propagates
    rationale: str        # private reasoning (logs only; NOT broadcast)


class Statement(TypedDict):
    """What an agent broadcasts on the social channel each round.

    Per the prompt design, the (sentiment, public_take) pair — not the order —
    is what propagates through the social channel and drives contagion.
    """
    agent: str
    sentiment: float
    side: Side
    text: str             # the agent's public_take
