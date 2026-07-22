"""Central configuration for the Narrative Market Sim.

All knobs live here so experiments are reproducible from a single object.
Secrets (API keys) are NEVER stored here - they come from environment variables
read inside llm.py. Everything is seeded.
"""
from __future__ import annotations
from collections.abc import Mapping
from dataclasses import dataclass, asdict
from typing import Any, Literal
import json

Topology = Literal["fully_connected", "random", "scale_free"]
SocialMode = Literal["feed", "network"]


@dataclass(frozen=True)
class NewsTimelineEntry:
    """One normalized public scenario event delivered during a simulation.

    The deliberately small schema prevents source metadata and agent-private
    rationale from entering the scientific Config or the social feed.  Source
    citations remain registered input artifacts; only public text is eligible
    for prompt delivery.
    """

    event_id: str
    round: int
    public_text: str


_NEWS_TIMELINE_KEYS = frozenset({"event_id", "round", "public_text"})


def normalize_news_timeline(
    value: Any,
    *,
    n_rounds: int,
) -> tuple[NewsTimelineEntry, ...]:
    """Validate and freeze an optional public-news timeline.

    Events are ordered by delivery round with Python's stable sort, preserving
    the caller's order for events mapped to the same round.  Empty input is the
    exact legacy/null mechanism.
    """

    if value in (None, (), []):
        return ()
    if isinstance(value, (str, bytes, Mapping)):
        raise ValueError("news_timeline must be a sequence of event objects")
    try:
        raw_entries = tuple(value)
    except TypeError as error:
        raise ValueError("news_timeline must be a sequence of event objects") from error

    normalized: list[NewsTimelineEntry] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(raw_entries):
        if isinstance(raw, NewsTimelineEntry):
            item = raw
        else:
            if not isinstance(raw, Mapping):
                raise ValueError(
                    "news_timeline entry {} must be an object".format(index)
                )
            keys = frozenset(str(key) for key in raw.keys())
            if keys != _NEWS_TIMELINE_KEYS:
                raise ValueError(
                    "news_timeline entry {} must contain exactly {}".format(
                        index, sorted(_NEWS_TIMELINE_KEYS)
                    )
                )
            event_id = raw["event_id"]
            public_text = raw["public_text"]
            if not isinstance(event_id, str):
                raise ValueError("news_timeline event_id must be a string")
            if not isinstance(public_text, str):
                raise ValueError("news_timeline public_text must be a string")
            round_value = raw["round"]
            if isinstance(round_value, bool) or not isinstance(round_value, int):
                raise ValueError("news_timeline round must be an integer")
            item = NewsTimelineEntry(event_id, round_value, public_text)

        if not isinstance(item.event_id, str):
            raise ValueError("news_timeline event_id must be a string")
        if not isinstance(item.public_text, str):
            raise ValueError("news_timeline public_text must be a string")
        item = NewsTimelineEntry(item.event_id, item.round, item.public_text)
        if not item.event_id.strip():
            raise ValueError("news_timeline event_id must be nonblank")
        if item.event_id in seen_ids:
            raise ValueError(
                "news_timeline event_id must be unique: {!r}".format(item.event_id)
            )
        if isinstance(item.round, bool) or not isinstance(item.round, int):
            raise ValueError("news_timeline round must be an integer")
        if not 1 <= item.round <= int(n_rounds):
            raise ValueError("news_timeline round must be in 1..n_rounds")
        if not item.public_text.strip():
            raise ValueError("news_timeline public_text must be nonblank")
        seen_ids.add(item.event_id)
        normalized.append(item)
    return tuple(sorted(normalized, key=lambda entry: entry.round))


@dataclass
class Config:
    # ---- reproducibility ----
    seed: int = 7

    # ---- market / timeline ----
    n_rounds: int = 24
    news_round: int = 12
    news_text: str = ("BREAKING: the company missed Q2 earnings by a wide margin "
                       "and cut full-year guidance sharply.")
    # Explicit opt-in evolving public-news mechanism.  The empty tuple is the
    # legacy/null path and leaves news_text/news_round behavior byte-for-byte
    # unchanged.  Non-empty values are immutable normalized entries.
    news_timeline: tuple[NewsTimelineEntry, ...] = ()
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
    # ``None`` preserves the historical coercive parser.  Formal multi-event
    # runs opt into the versioned strict response contract before live data.
    decision_response_schema: str | None = None
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
    # ``None`` preserves each SDK's legacy default.  Multi-event freezes zero
    # so only application-visible retries can occur.
    provider_sdk_max_retries: int | None = None

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

    def __post_init__(self) -> None:
        from .decision_contract import SUPPORTED_DECISION_RESPONSE_SCHEMAS

        if (
            self.decision_response_schema is not None
            and self.decision_response_schema
            not in SUPPORTED_DECISION_RESPONSE_SCHEMAS
        ):
            raise ValueError("unsupported decision_response_schema")
        if (
            self.provider_sdk_max_retries is not None
            and (
                isinstance(self.provider_sdk_max_retries, bool)
                or not isinstance(self.provider_sdk_max_retries, int)
                or self.provider_sdk_max_retries < 0
            )
        ):
            raise ValueError("provider_sdk_max_retries must be an integer >= 0")
        self.news_timeline = normalize_news_timeline(
            self.news_timeline, n_rounds=self.n_rounds
        )

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: dict) -> "Config":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})
