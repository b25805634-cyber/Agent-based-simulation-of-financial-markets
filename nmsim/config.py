"""Central configuration for the Narrative Market Sim.

All knobs live here so experiments are reproducible from a single object.
Secrets (API keys) are NEVER stored here - they come from environment variables
read inside llm.py. Everything is seeded.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Literal
import json

Topology = Literal["fully_connected", "random", "scale_free"]
SocialMode = Literal["feed", "network"]


@dataclass
class Config:
    # ---- reproducibility ----
    seed: int = 7

    # ---- market / timeline ----
    n_rounds: int = 24
    news_round: int = 12
    news_text: str = ("BREAKING: the company missed Q2 earnings by a wide margin "
                       "and cut full-year guidance sharply.")
    initial_price: float = 100.0
    fundamental_value: float = 100.0
    recent_window: int = 5
    kappa: float = 0.12               # price-impact strength

    # ---- population ----
    n_llm_agents: int = 6             # how many persona agents (prompts.py has 6)
    n_noise_agents: int = 8           # FIXED background liquidity (does not scale)
    max_llm_agents: int = 40          # hard cap (cost control)
    # Explicit cast: {persona_id: count}. None -> legacy (first n_llm_agents
    # personas, one each). Multiple copies of a persona get numbered names
    # like "retail_crowd#2" but share the same prompt/character.
    population: dict | None = None

    # ---- LLM provider ----
    provider: str = "auto"            # auto | mock | anthropic | openai (env LLM_PROVIDER overrides)
    model: str = ""                   # "" -> provider default (env LLM_MODEL overrides)
    cheap_model: str = ""             # optional cheaper model (env LLM_CHEAP_MODEL)
    use_cheap_model: bool = False

    # OpenAI-compatible endpoint (e.g. a local vLLM / minimax server).
    openai_base_url: str = "http://10.214.32.152:8000/v1"  # env OPENAI_BASE_URL overrides
    openai_api_key: str = "EMPTY"                          # env OPENAI_API_KEY overrides
    openai_model: str = "MiniMax-M2.7"  # the model actually served at the endpoint
                                        # (the requested "minimax-m2.5" does not exist;
                                        #  also available: "HiggsAI"). env LLM_MODEL overrides
    temperature: float = 0.0          # 0 for reproducibility
    max_tokens: int = 1024            # headroom for verbose JSON (reasoning field);
                                      # cost scales with tokens generated, not the cap
    cache_enabled: bool = True

    # ---- Phase 3: contagion channel ----
    social_enabled: bool = True
    social_mode: SocialMode = "network"
    topology: Topology = "scale_free"
    n_neighbors: int = 2              # peer degree among non-hub nodes
    social_weight: float = 1.0       # GLOBAL gain on the per-agent social coupling
                                      # (effective weight = gain x persona susceptibility)
    # ---- influencer-megaphone ablation ----
    broadcast_mode: str = "all"      # all | exclude_influencer (mute) | only_influencer (solo mic)
    demote_influencer: bool = False  # True -> influencer is an ordinary node (no hub, no auto-seed)

    # ---- leverage + forced-liquidation (the market AMPLIFIER) ----
    # FROZEN-REFERENCE, PRICE-ONLY margin (V2). Each leveraged agent carries a
    # FIXED reference leveraged long (lev_ref_shares financed by lev_debt, set at
    # t0), marked to market on PRICE ALONE -> equity_ratio = 1 - (1-1/L)*p0/price.
    # When it drops below maintenance the reference long is force-liquidated ONCE,
    # dumping lev_ref_shares of sell pressure into the NEXT round's net flow. This
    # is a deleveraging CASCADE: price falls -> margin calls -> forced sells ->
    # price falls further -> more breaches. The leverage book is SEPARATE from the
    # agent's voluntary shares/cash (never mutates them), so margin is purely
    # price-driven (no voluntary-selling artifact, no short-balance interaction)
    # and never leaks into the LLM prompt — the agents are not told their margin
    # state; leverage only reaches them through the price tape it moves.
    leverage_enabled: bool = False
    leverage_ratio: float = 2.5       # CENTER leverage L of the cohort
    leverage_spread: float = 0.5      # L is staggered in [center-spread, center+spread]
                                      # across the cohort -> staggered breach prices
                                      # (heterogeneous leverage -> a real cascade, not
                                      #  one synchronized dump). With 0.25 maintenance:
                                      #  L 2.0->breach at -33%, 2.5->-20%, 3.0->-11%.
    maintenance_margin: float = 0.25  # force-liquidate the reference long below this
    leverage_fraction: float = 0.5    # fraction of LLM agents leveraged (fuel-first)
    digest_size: int = 4             # how many neighbor statements an agent sees
    seed_fraction: float = 0.34      # fraction of agents the headline reaches first

    # ---- Phase 4: validation ----
    reference_path: str = ""          # optional CSV: timestamp,price[,news]

    # ---- output ----
    out_dir: str = "outputs"

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: dict) -> "Config":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})
