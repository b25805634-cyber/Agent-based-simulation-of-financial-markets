"""Agents — persona-driven LLM traders + cheap noise agents.

Behavior is prompt-driven: the persona library + decision-prompt builder live in
`prompts.py` (the strategy partner's design). A real LLM reasons from the persona
text via `prompts.build_system/build_user`; the MockLLM ignores the persona text
and uses the per-id `mock_params` below so the loop still runs with no API key.

Each persona's four character dials (in prompts.py) also set the agent's
mechanical `social_weight` so prompt-described susceptibility matches the
contagion mechanism. Reasoning traces are always logged; only the `public_take`
is broadcast on the social channel.
"""
from __future__ import annotations
import json
import random
from dataclasses import dataclass, field

from .types import Order, Statement
from .llm import parse_order
from . import prompts as P


# Mock-only parameters, one per persona id. These reproduce each persona's
# CHARACTER for the deterministic MockLLM; a real LLM ignores them entirely.
# (Kept here, not in prompts.py, so the prompt-design file stays pure.)
_MOCK_PARAMS_BY_ID = {
    "retail_crowd":         {"w_value": 0.0, "news_value": 2, "w_trend": 1.0, "w_senti": 0.12,
                             "w_social": 1.8, "bias": 0.0, "size": 30, "aggro": 0.02,
                             "inertia": 0.008, "noise": 0.02},
    "fomo_momentum":        {"w_value": 0.0, "news_value": 1, "w_trend": 1.6, "w_senti": 0.08,
                             "w_social": 1.4, "bias": 0.01, "size": 26, "aggro": 0.018,
                             "inertia": 0.01, "noise": 0.018},
    "value_institution":    {"w_value": 1.2, "news_value": 3, "w_trend": 0.0, "w_senti": 0.0,
                             "w_social": 0.2, "bias": 0.0, "size": 25, "aggro": 0.008,
                             "inertia": 0.02, "noise": 0.01},
    "quant_arb":            {"w_value": 0.2, "news_value": 0, "w_trend": -1.0, "w_senti": 0.0,
                             "w_social": 0.0, "bias": 0.0, "size": 24, "aggro": 0.01,
                             "inertia": 0.012, "noise": 0.008},
    "influencer_amplifier": {"w_value": 0.0, "news_value": 2, "w_trend": 0.8, "w_senti": 0.10,
                             "w_social": 0.6, "bias": 0.0, "size": 12, "aggro": 0.015,
                             "inertia": 0.008, "noise": 0.02},
    "contrarian_fund":      {"w_value": 0.7, "news_value": 1, "w_trend": -2.0, "w_senti": -0.05,
                             "w_social": -0.8, "bias": 0.0, "size": 20, "aggro": 0.012,
                             "inertia": 0.015, "noise": 0.01},
}

INFLUENCER_IDS = ("influencer_amplifier",)


@dataclass
class Agent:
    name: str                          # persona id (stable handle in logs/graph)
    persona_id: str
    persona_dict: dict                 # full prompts.py persona (used by a real LLM)
    persona: str                       # natural-language description
    mock_params: dict                  # MockLLM-only steering (ignored by a real LLM)
    social_weight: float = 0.5         # per-agent coupling to the crowd (∝ social_susceptibility)
    is_influencer: bool = False
    cash: float = 10000.0
    shares: int = 50
    # ---- leverage book (frozen-reference, price-only; SEPARATE from shares/cash
    #      above so it never leaks into the LLM prompt — see nmsim/leverage.py) ----
    is_leveraged: bool = False         # carries a leveraged reference long (set by leverage.init)
    lev_ratio: float = 0.0             # this agent's leverage L (staggered across the cohort)
    lev_ref_shares: int = 0            # frozen reference long size (marked to market on price)
    lev_debt: float = 0.0              # fixed loan financing the reference long
    lev_liquidated: bool = False       # the reference long has been force-closed (once)
    lev_pnl: float = 0.0               # realised book P&L at liquidation (analysis only)
    memory: list = field(default_factory=list)
    last_sentiment: float = 0.0        # for stance-flip detection (Phase 3)
    is_llm: bool = True

    def build_prompt(self, mode, round_i, last_price, recent, fundamental, news,
                     social_feed=None, social_mean: float = 0.0,
                     effective_weight: float = 0.0, mic_note: str = ""):
        """Return (system, user).

        mode="real" -> use the prompts.py templates (persona text + social feed).
        mode="mock" -> a structured prompt the MockLLM parses (with MOCK_PARAMS).
        `social_feed` is a list of (sentiment, public_take) tuples from neighbors.
        `mic_note` (ablation): a sentence telling the influencer whether its voice carries.
        """
        if mode == "real":
            system = P.build_system(self.persona_dict)
            user = P.build_user(
                round_i=round_i, price=last_price, recent=list(recent),
                news=news or None,
                social_feed=(social_feed if effective_weight > 0 else None),
                shares=self.shares, cash=self.cash, memory=self.memory,
                mic_note=mic_note)
            return system, user

        # ----- mock structured prompt -----
        trend = 0.0 if len(recent) < 2 else (recent[-1] - recent[0]) / recent[0]
        system = (
            "You are a trader in a simulated stock market.\n"
            f"PERSONA: {self.persona}\n"
            'Respond ONLY with a JSON object: '
            '{"side":"buy"|"sell"|"hold","quantity":int,"limit_price":float,'
            '"sentiment":float,"public_take":str,"reasoning":str}.\n'
            f"MOCK_PARAMS: {json.dumps(self.mock_params)}"
        )
        notes = " | ".join(self.memory[-3:]) if self.memory else "none"
        social_block = ""
        if effective_weight > 0:
            social_block = (
                f"SOCIAL_SENTIMENT: {social_mean:.4f}\n"
                f"SOCIAL_WEIGHT: {effective_weight:.2f}\n"
            )
        user = (
            f"ROUND: {round_i}\n"
            f"NEWS: {news or 'no fresh news'}\n"
            f"LAST_PRICE: {last_price:.2f}\n"
            f"FUNDAMENTAL: {fundamental:.2f}\n"
            f"RECENT_PRICES: {', '.join(f'{p:.2f}' for p in recent)}\n"
            f"TREND: {trend:.4f}\n"
            f"SHARES: {self.shares}\n"
            f"CASH: {self.cash:.2f}\n"
            f"YOUR_RECENT_NOTES: {notes}\n"
            f"{social_block}"
            "Decide your order."
        )
        return system, user

    def ingest(self, raw: str, round_i: int, last_price: float) -> Order:
        """Parse a completion into an Order, update memory + last_sentiment."""
        order = parse_order(raw, last_price)
        order["agent"] = self.name
        self.last_sentiment = order["sentiment"]
        self.memory.append(
            f"r{round_i}: price {last_price:.2f}, did {order['side']} {order['quantity']} "
            f"- {(order['public_take'] or order['rationale'])[:60]}")
        return order

    def statement(self, order: Order) -> Statement:
        # Only the public_take + sentiment propagate (not the private reasoning).
        return Statement(agent=self.name, sentiment=order["sentiment"],
                         side=order["side"], text=order["public_take"][:90])


class NoiseAgent(Agent):
    """Cheap background liquidity: random small orders around last price.

    Does not call the LLM (is_llm=False) and is excluded from the social graph.
    """
    def __init__(self, name: str, idx: int = 0, base_seed: int = 0):
        super().__init__(name=name, persona_id="noise", persona_dict={},
                         persona="random liquidity", mock_params={},
                         social_weight=0.0, is_llm=False)
        self.idx = idx
        self.base_seed = base_seed

    def noise_order(self, round_i: int, last_price: float) -> Order:
        # M1: seed the RNG from cfg.seed + INTEGER index/round only. The old
        # `hash((self.name, round_i))` used Python's PYTHONHASHSEED-salted string
        # hash, which differs per process -> same-seed runs were NOT reproducible
        # (each (gain,seed) is its own subprocess). Integers hash to themselves.
        rng = random.Random((self.base_seed * 1_000_003) ^ (self.idx * 9176 + round_i))
        side = rng.choice(("buy", "sell"))
        return Order(agent=self.name, side=side, quantity=rng.randint(1, 8),
                     limit_price=round(last_price * (1 + rng.uniform(-0.03, 0.03)), 2),
                     sentiment=0.0, public_take="", rationale="noise")


def make_agents(cfg) -> list[Agent]:
    """Instantiate the persona cast (from prompts.py) + noise agents.

    cfg.population ({persona_id: count}) allows MULTIPLE COPIES of a persona —
    copies share the same prompt/character and get numbered names ("retail_crowd",
    "retail_crowd#2", ...). The influencer is capped at exactly 1 (its hub+seed
    structural role must stay unique). Without cfg.population, legacy behavior:
    the first n_llm_agents personas, one each.

    Each agent's social_weight is set ∝ its persona's social_susceptibility, so
    the mechanism matches the prompt-described character.
    """
    by_id = {p["id"]: p for p in P.PERSONAS}
    if cfg.population:
        cast: list[str] = []
        for pid, count in cfg.population.items():
            if pid not in by_id:
                raise ValueError(f"unknown persona id in population: {pid}")
            if pid in INFLUENCER_IDS:
                count = min(int(count), 1)
            cast.extend([pid] * int(count))
        cast = cast[: cfg.max_llm_agents]
    else:
        cast = [p["id"] for p in
                P.PERSONAS[: min(cfg.n_llm_agents, cfg.max_llm_agents, len(P.PERSONAS))]]

    agents: list[Agent] = []
    counters: dict[str, int] = {}
    for pid in cast:
        k = counters.get(pid, 0) + 1
        counters[pid] = k
        name = pid if k == 1 else f"{pid}#{k}"
        p = by_id[pid]
        agents.append(Agent(
            name=name, persona_id=pid, persona_dict=p, persona=p["persona"],
            mock_params=dict(_MOCK_PARAMS_BY_ID.get(pid, {})),
            social_weight=float(p.get("social_susceptibility", 0.5)),
            is_influencer=pid in INFLUENCER_IDS))
    agents += [NoiseAgent(f"noise-{i}", idx=i, base_seed=cfg.seed)
               for i in range(cfg.n_noise_agents)]
    return agents
