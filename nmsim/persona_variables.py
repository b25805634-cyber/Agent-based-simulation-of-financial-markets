"""Frozen persona-variable registry, renderer, and deterministic samplers.

This module is additive.  It does not replace or modify the six personas in
``nmsim.prompts``.  The five legacy mappings below are coordinates for
comparison and sampling only; ``quant_arb`` deliberately has no coordinate.

Scientific definitions come from ``docs/PERSONA_VARIABLES.md`` v1.0.  In
particular, E1/E2 are inherited engine metadata and are not rendered.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from types import MappingProxyType
from typing import Iterable, Mapping


class PersonaVariableError(ValueError):
    """Raised when a persona vector or sampling region is invalid."""


@dataclass(frozen=True)
class PersonaLevel:
    """One frozen level in a rendered persona variable."""

    id: str
    fragment: str


@dataclass(frozen=True)
class PersonaVariable:
    """A rendered persona-variable definition."""

    id: str
    group_id: str
    group_name: str
    name: str
    levels: tuple[PersonaLevel, ...]

    @property
    def level_ids(self) -> tuple[str, ...]:
        return tuple(level.id for level in self.levels)

    def level(self, level_id: str) -> PersonaLevel:
        for level in self.levels:
            if level.id == level_id:
                return level
        raise PersonaVariableError(
            "unknown level {!r} for variable {}; expected one of {}".format(
                level_id,
                self.id,
                ", ".join(self.level_ids),
            )
        )


@dataclass(frozen=True)
class InheritedVariable:
    """Metadata for an inherited variable that never enters ``render``."""

    id: str
    name: str
    source: str
    rendered: bool = False


def _levels(*pairs: tuple[str, str]) -> tuple[PersonaLevel, ...]:
    return tuple(PersonaLevel(level_id, fragment) for level_id, fragment in pairs)


# Dict insertion order is part of the stable public API.  The fragments below
# are copied byte-for-byte from the frozen protocol and must not be paraphrased.
VARIABLE_REGISTRY: Mapping[str, PersonaVariable] = MappingProxyType(
    {
        "A1": PersonaVariable(
            "A1",
            "A",
            "资金与仓位",
            "可投资资产",
            _levels(
                ("a1_2w", "工作几年攒下的 2 万元积蓄"),
                ("a1_5w", "多年攒下的 5 万元积蓄"),
                ("a1_10w", "10 万元可投资资金"),
                ("a1_50w", "50 万元闲置资金"),
            ),
        ),
        "A2": PersonaVariable(
            "A2",
            "A",
            "资金与仓位",
            "仓位集中度",
            _levels(
                ("a2_10", "你把全部可投资资金中的 10% 投在了这只股票上"),
                ("a2_30", "你把全部可投资资金中的 30% 投在了这只股票上"),
                ("a2_50", "你把全部可投资资金中的 50% 投在了这只股票上"),
                ("a2_70", "你把全部可投资资金中的 70% 投在了这只股票上"),
                ("a2_90", "你把全部可投资资金中的 90% 投在了这只股票上"),
            ),
        ),
        "A3": PersonaVariable(
            "A3",
            "A",
            "资金与仓位",
            "浮亏深度",
            _levels(
                ("a3_0", "买入成本 100 元,现价 100 元,基本持平"),
                ("a3_m10", "买入成本 100 元,现价 90 元,浮亏 10%"),
                ("a3_m25", "买入成本 100 元,现价 75 元,浮亏 25%"),
                ("a3_m50", "买入成本 100 元,现价 50 元,浮亏 50%"),
            ),
        ),
        "A4": PersonaVariable(
            "A4",
            "A",
            "资金与仓位",
            "收入稳定性与现金流",
            _levels(
                ("a4_stable", "你有稳定的工资收入,近期没有大额支出计划"),
                ("a4_unstable", "你的收入不太稳定,时好时坏"),
                ("a4_needcash", "半年内你需要用到这笔钱(有明确的大额支出计划)"),
            ),
        ),
        "B1": PersonaVariable(
            "B1",
            "B",
            "信念结构",
            "买入依据类型",
            _levels(
                (
                    "b1_research",
                    "这只股票是你自己花了几周时间研究财报和行业后决定买入的",
                ),
                ("b1_trend", "这只股票之前一直在涨,你是顺势而为买入的"),
                (
                    "b1_peers",
                    "你身边好几位朋友同事都在买这只股票,都说好,你跟着买入的",
                ),
                ("b1_media", "你是在新闻和分析师一致看好中买入这只股票的"),
            ),
        ),
        "B2": PersonaVariable(
            "B2",
            "B",
            "信念结构",
            "信念深度",
            _levels(
                ("b2_1", "关于这只股票,你只听说过个大概"),
                ("b2_3", "你看过几篇关于这只股票的分析,大致了解"),
                (
                    "b2_5",
                    "你曾认真写下过买入这只股票的理由,也想过什么情况下自己可能是错的",
                ),
            ),
        ),
        "B3": PersonaVariable(
            "B3",
            "B",
            "信念结构",
            "信念可证伪性",
            _levels(
                (
                    "b3_clear",
                    "你心里有明确的标尺:如果出现什么情况,你就承认自己错了并卖出",
                ),
                (
                    "b3_vague",
                    "你没有具体想过,什么情况会让你改变对这只股票的看法",
                ),
            ),
        ),
        "C1": PersonaVariable(
            "C1",
            "C",
            "心理特质",
            "亏损容忍度",
            _levels(
                ("c1_5", "浮亏超过 5% 你就开始睡不好"),
                ("c1_15", "浮亏超过 15% 你就开始睡不好"),
                ("c1_30", "浮亏 30% 以内你都能承受"),
                ("c1_50", "浮亏一半你也能扛得住"),
            ),
        ),
        "C2": PersonaVariable(
            "C2",
            "C",
            "心理特质",
            "认错成本",
            _levels(
                (
                    "c2_low",
                    "在投资上承认错误对你不是难事,认错换股是常事",
                ),
                ("c2_mid", "承认错误会让你有些不舒服,但你通常能做到"),
                (
                    "c2_high",
                    "承认自己做错了决定让你非常难堪,这只股票又是你坚持要买的",
                ),
            ),
        ),
        "C3": PersonaVariable(
            "C3",
            "C",
            "心理特质",
            "持仓公开度",
            _levels(
                ("c3_private", "你买这只股票的事没人知道"),
                ("c3_family", "你买这只股票的事家里人知道"),
                (
                    "c3_public",
                    "你买这只股票的事朋友们都知道,还常有人问起",
                ),
            ),
        ),
        "C4": PersonaVariable(
            "C4",
            "C",
            "心理特质",
            "耐心与时间尺度",
            _levels(
                ("c4_days", "一只股票几天没动静你就会开始烦躁"),
                ("c4_months", "你愿意拿几个月看看"),
                ("c4_years", "你习惯按年持有"),
            ),
        ),
        "D1": PersonaVariable(
            "D1",
            "D",
            "注意力与信息行为",
            "盯盘频率",
            _levels(
                ("d1_intraday", "开盘时间你几乎一直看盘,一天看好多次"),
                ("d1_daily", "你每天收盘后看一次"),
                ("d1_weekly", "你一两个星期才看一次"),
            ),
        ),
        "D2": PersonaVariable(
            "D2",
            "D",
            "注意力与信息行为",
            "替代机会敏感度",
            _levels(
                (
                    "d2_low",
                    "你只关心自己手里这只股票,别的股票涨跌你不太在意",
                ),
                ("d2_mid", "你会留意别的股票涨得好不好"),
                (
                    "d2_high",
                    "你总在比较哪只股票涨得好,很怕错过别的机会",
                ),
            ),
        ),
        "D3": PersonaVariable(
            "D3",
            "D",
            "注意力与信息行为",
            "媒体信任度",
            _levels(
                ("d3_trust", "你大体相信新闻和专家的判断"),
                ("d3_half", "你对新闻半信半疑,会和自己的判断对照"),
                ("d3_self", "你只相信自己的研究"),
            ),
        ),
    }
)

VARIABLE_IDS: tuple[str, ...] = tuple(VARIABLE_REGISTRY)

INHERITED_VARIABLES: Mapping[str, InheritedVariable] = MappingProxyType(
    {
        "E1": InheritedVariable(
            "E1",
            "社交易感性",
            "persona.social_susceptibility",
        ),
        "E2": InheritedVariable(
            "E2",
            "表达欲/广播行为",
            "persona type: influencer_amplifier=高频广播,其余=低",
        ),
    }
)

REFERENCE_THETA: Mapping[str, str] = MappingProxyType(
    {
        "A1": "a1_10w",
        "A2": "a2_50",
        "A3": "a3_0",
        "A4": "a4_stable",
        "B1": "b1_trend",
        "B2": "b2_3",
        "B3": "b3_vague",
        "C1": "c1_15",
        "C2": "c2_mid",
        "C3": "c3_private",
        "C4": "c4_months",
        "D1": "d1_daily",
        "D2": "d2_mid",
        "D3": "d3_half",
    }
)


def _coordinate(**levels: str) -> Mapping[str, str]:
    if set(levels) != set(VARIABLE_IDS):
        raise RuntimeError("legacy persona coordinate must define all 14 variables")
    ordered: dict[str, str] = {}
    for variable_id in VARIABLE_IDS:
        level_id = levels[variable_id]
        VARIABLE_REGISTRY[variable_id].level(level_id)
        ordered[variable_id] = level_id
    return MappingProxyType(ordered)


# Coordinates only: importing this module does not modify PERSONAS or prompts.
LEGACY_PERSONA_COORDINATES: Mapping[str, Mapping[str, str] | None]
LEGACY_PERSONA_COORDINATES = MappingProxyType(
    {
        "retail_crowd": _coordinate(
            A1="a1_5w",
            A2="a2_70",
            A3="a3_m10",
            A4="a4_stable",
            B1="b1_peers",
            B2="b2_1",
            B3="b3_vague",
            C1="c1_15",
            C2="c2_mid",
            C3="c3_family",
            C4="c4_months",
            D1="d1_intraday",
            D2="d2_high",
            D3="d3_half",
        ),
        "fomo_momentum": _coordinate(
            A1="a1_10w",
            A2="a2_50",
            A3="a3_0",
            A4="a4_stable",
            B1="b1_trend",
            B2="b2_1",
            B3="b3_vague",
            C1="c1_5",
            C2="c2_low",
            C3="c3_private",
            C4="c4_days",
            D1="d1_intraday",
            D2="d2_high",
            D3="d3_trust",
        ),
        "value_institution": _coordinate(
            A1="a1_50w",
            A2="a2_30",
            A3="a3_0",
            A4="a4_stable",
            B1="b1_research",
            B2="b2_5",
            B3="b3_clear",
            C1="c1_50",
            C2="c2_low",
            C3="c3_private",
            C4="c4_years",
            D1="d1_daily",
            D2="d2_low",
            D3="d3_self",
        ),
        "contrarian_fund": _coordinate(
            A1="a1_50w",
            A2="a2_30",
            A3="a3_m25",
            A4="a4_stable",
            B1="b1_research",
            B2="b2_5",
            B3="b3_clear",
            C1="c1_30",
            C2="c2_low",
            C3="c3_private",
            C4="c4_months",
            D1="d1_daily",
            D2="d2_low",
            D3="d3_self",
        ),
        "influencer_amplifier": _coordinate(
            A1="a1_10w",
            A2="a2_30",
            A3="a3_0",
            A4="a4_stable",
            B1="b1_media",
            B2="b2_3",
            B3="b3_vague",
            C1="c1_15",
            C2="c2_high",
            C3="c3_public",
            C4="c4_days",
            D1="d1_intraday",
            D2="d2_mid",
            D3="d3_trust",
        ),
        "quant_arb": None,
    }
)

# Each pair is (semantically low, semantically high).  B1's categorical
# contrast is the frozen research-versus-peers prediction.  The expected sign
# below always means P(sell, high) - P(sell, low).
FLIP_ENDPOINTS: Mapping[str, tuple[str, str]] = MappingProxyType(
    {
        "A1": ("a1_2w", "a1_50w"),
        "A2": ("a2_10", "a2_90"),
        "A3": ("a3_0", "a3_m50"),
        "A4": ("a4_stable", "a4_needcash"),
        "B1": ("b1_research", "b1_peers"),
        "B2": ("b2_1", "b2_5"),
        "B3": ("b3_vague", "b3_clear"),
        "C1": ("c1_5", "c1_50"),
        "C2": ("c2_low", "c2_high"),
        "C3": ("c3_private", "c3_public"),
        "C4": ("c4_days", "c4_years"),
        "D1": ("d1_weekly", "d1_intraday"),
        "D2": ("d2_low", "d2_high"),
        "D3": ("d3_self", "d3_trust"),
    }
)

# +1/-1 predicts the sign of P(sell, high) - P(sell, low).  A1 is
# intentionally None: its preregistered direction is open and significance
# alone is evaluated.
EXPECTED_SELL_DIRECTIONS: Mapping[str, int | None] = MappingProxyType(
    {
        "A1": None,
        "A2": 1,
        "A3": -1,
        "A4": 1,
        "B1": 1,
        "B2": -1,
        "B3": 1,
        "C1": -1,
        "C2": -1,
        "C3": -1,
        "C4": -1,
        "D1": 1,
        "D2": 1,
        "D3": 1,
    }
)

# Compatibility alias for callers using the longer specification wording.
LOW_HIGH_ENDPOINTS = FLIP_ENDPOINTS
EXPECTED_DELTA_DIRECTIONS = EXPECTED_SELL_DIRECTIONS

FIXTURE_BY_VARIABLE: Mapping[str, str] = MappingProxyType(
    {
        "A1": "negative_news_price_unchanged",
        "A2": "negative_news_price_unchanged",
        "A3": "deep_loss_bad_news",
        "A4": "negative_news_price_unchanged",
        "B1": "belief_source_pair",
        "B2": "belief_source_pair",
        "B3": "negative_news_price_unchanged",
        "C1": "deep_loss_bad_news",
        "C2": "public_holding_friends_ask",
        "C3": "public_holding_friends_ask",
        "C4": "sideways_2y_hot_peers",
        "D1": "negative_news_price_unchanged",
        "D2": "sideways_2y_hot_peers",
        "D3": "negative_news_price_unchanged",
    }
)

FORBIDDEN_RENDER_TERMS: tuple[str, ...] = (
    "散户",
    "机构",
    "庄家",
    "价值投资者",
    "趋势交易者",
    "处置效应",
    "损失厌恶",
    "从众",
    "羊群",
    "锚定",
    "过度自信",
    "请表现出",
    "你应当",
    "你需要扮演",
)

_SEED_DOMAIN = b"nmsim-persona-variables-seed-v1\0"
_RENDER_PREFIX = "我的处境备忘：“"
_RENDER_SUFFIX = "”。"


def validate_theta(theta: Mapping[str, str]) -> dict[str, str]:
    """Validate and return a registry-ordered copy of a complete vector."""

    if not isinstance(theta, Mapping):
        raise PersonaVariableError("theta must be a mapping")
    supplied = set(theta)
    expected = set(VARIABLE_IDS)
    missing = sorted(expected - supplied)
    unknown = sorted(supplied - expected)
    if missing or unknown:
        details: list[str] = []
        if missing:
            details.append("missing variables: " + ", ".join(missing))
        if unknown:
            details.append("unknown variables: " + ", ".join(unknown))
        raise PersonaVariableError("invalid theta; " + "; ".join(details))

    validated: dict[str, str] = {}
    for variable_id in VARIABLE_IDS:
        level_id = theta[variable_id]
        if not isinstance(level_id, str):
            raise PersonaVariableError(
                "level for {} must be a string".format(variable_id)
            )
        VARIABLE_REGISTRY[variable_id].level(level_id)
        validated[variable_id] = level_id
    return validated


def _seed_part_bytes(part: object) -> bytes:
    if isinstance(part, bool):
        return b"bool:" + (b"1" if part else b"0")
    if isinstance(part, int):
        return b"int:" + str(part).encode("ascii")
    if isinstance(part, str):
        return b"str:" + part.encode("utf-8")
    raise TypeError("seed parts must be integers or strings")


def derive_seed(seed: int, *parts: object) -> int:
    """Derive a stable integer seed with SHA-256.

    No Python object hash participates, so the result is independent of
    ``PYTHONHASHSEED`` and process boundaries.
    """

    if isinstance(seed, bool) or not isinstance(seed, int):
        raise TypeError("seed must be an integer")
    digest = hashlib.sha256()
    digest.update(_SEED_DOMAIN)
    for part in (seed, *parts):
        encoded = _seed_part_bytes(part)
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return int.from_bytes(digest.digest(), "big", signed=False)


def _selected_fragment(variable_id: str, theta: Mapping[str, str]) -> str:
    return VARIABLE_REGISTRY[variable_id].level(theta[variable_id]).fragment


def chinese_character_count(text: str) -> int:
    """Count CJK unified ideographs for the protocol's 120–300 字 bound."""

    return sum(
        1
        for character in text
        if "\u3400" <= character <= "\u4dbf"
        or "\u4e00" <= character <= "\u9fff"
        or "\uf900" <= character <= "\ufaff"
    )


def render(theta: Mapping[str, str], seed: int = 0) -> str:
    """Render a complete vector as a label-free Chinese situation narrative.

    Frozen level fragments often use ``你``.  Replacing that pronoun would
    violate the byte-frozen fragment rule, so the first-person ``我的处境备忘``
    heading frames the exact fragments as a self-addressed memo.  ``seed``
    deterministically orders the four rendered groups; fragments within a
    group retain registry order.
    """

    validated = validate_theta(theta)
    derive_seed(seed, "render-seed-validation")

    group_ids = tuple(dict.fromkeys(v.group_id for v in VARIABLE_REGISTRY.values()))
    group_ids = tuple(
        sorted(
            group_ids,
            key=lambda group_id: (
                derive_seed(seed, "render-group", group_id),
                group_id,
            ),
        )
    )
    fragments: list[str] = []
    for group_id in group_ids:
        fragments.extend(
            _selected_fragment(variable_id, validated)
            for variable_id in VARIABLE_IDS
            if VARIABLE_REGISTRY[variable_id].group_id == group_id
        )
    text = _RENDER_PREFIX + "；".join(fragments) + _RENDER_SUFFIX

    count = chinese_character_count(text)
    if not 120 <= count <= 300:
        raise PersonaVariableError(
            "rendered text must contain 120-300 Chinese characters; got {}".format(
                count
            )
        )
    for forbidden in FORBIDDEN_RENDER_TERMS:
        if forbidden in text:
            raise PersonaVariableError(
                "rendered text contains forbidden term {!r}".format(forbidden)
            )
    return text


def _choose_level(seed: int, variable_id: str, candidate_ids: tuple[str, ...]) -> str:
    if not candidate_ids:
        raise PersonaVariableError(
            "sampling region for {} must contain at least one level".format(variable_id)
        )
    index = derive_seed(seed, "sample-level", variable_id) % len(candidate_ids)
    return candidate_ids[index]


def sample_uniform(seed: int) -> dict[str, str]:
    """Uniformly sample every variable from all of its frozen levels."""

    derive_seed(seed, "uniform-seed-validation")
    return {
        variable_id: _choose_level(
            derive_seed(seed, "uniform"),
            variable_id,
            VARIABLE_REGISTRY[variable_id].level_ids,
        )
        for variable_id in VARIABLE_IDS
    }


def _normalise_candidates(
    variable_id: str,
    requested: str | Iterable[str],
) -> tuple[str, ...]:
    if isinstance(requested, str):
        requested_ids = (requested,)
    else:
        try:
            requested_ids = tuple(requested)
        except TypeError as exc:
            raise PersonaVariableError(
                "sampling region for {} must be a level id or iterable of level ids".format(
                    variable_id
                )
            ) from exc
    if not requested_ids:
        raise PersonaVariableError(
            "sampling region for {} must contain at least one level".format(variable_id)
        )
    if any(not isinstance(level_id, str) for level_id in requested_ids):
        raise PersonaVariableError(
            "sampling region for {} contains a non-string level".format(variable_id)
        )

    requested_set = set(requested_ids)
    variable = VARIABLE_REGISTRY[variable_id]
    unknown = sorted(requested_set - set(variable.level_ids))
    if unknown:
        raise PersonaVariableError(
            "sampling region for {} has unknown levels: {}".format(
                variable_id,
                ", ".join(unknown),
            )
        )
    # Registry order, rather than caller container order, freezes the sampling
    # result even if a caller supplies a set.
    return tuple(
        level_id for level_id in variable.level_ids if level_id in requested_set
    )


def sample_region(
    region: Mapping[str, str | Iterable[str]],
    seed: int,
) -> dict[str, str]:
    """Sample from caller-declared per-variable level sets.

    Constrained dimensions draw uniformly from the listed frozen levels;
    omitted dimensions draw uniformly from all levels.  This is generic region
    machinery only: the protocol does not preregister named population regions.
    """

    if not isinstance(region, Mapping):
        raise PersonaVariableError("region must be a mapping")
    derive_seed(seed, "region-seed-validation")
    unknown_variables = sorted(set(region) - set(VARIABLE_IDS))
    if unknown_variables:
        raise PersonaVariableError(
            "sampling region has unknown variables: "
            + ", ".join(unknown_variables)
        )

    region_seed = derive_seed(seed, "region")
    sampled: dict[str, str] = {}
    for variable_id in VARIABLE_IDS:
        requested = region.get(
            variable_id,
            VARIABLE_REGISTRY[variable_id].level_ids,
        )
        candidates = _normalise_candidates(variable_id, requested)
        sampled[variable_id] = _choose_level(
            region_seed,
            variable_id,
            candidates,
        )
    return sampled


def flip_pair(
    variable_id: str,
    baseline: Mapping[str, str] = REFERENCE_THETA,
) -> tuple[dict[str, str], dict[str, str]]:
    """Return low/high complete vectors for one preregistered flip."""

    if variable_id not in VARIABLE_REGISTRY:
        raise PersonaVariableError("unknown variable {!r}".format(variable_id))
    low_level, high_level = FLIP_ENDPOINTS[variable_id]
    low = validate_theta(baseline)
    high = dict(low)
    low[variable_id] = low_level
    high[variable_id] = high_level
    return low, high


def fixture_for_variable(variable_id: str) -> str:
    """Return the frozen fixture assignment for a flip variable."""

    try:
        return FIXTURE_BY_VARIABLE[variable_id]
    except KeyError as exc:
        raise PersonaVariableError("unknown variable {!r}".format(variable_id)) from exc


def inherited_values_for_persona(persona_id: str) -> dict[str, float | str]:
    """Derive E1/E2 from an existing persona without changing its definition."""

    # Keep the dependency read-only and local: persona_variables remains an
    # additive representation layer, while prompts.py stays the authority for
    # the six existing personas and their inherited engine attributes.
    from nmsim.prompts import PERSONAS_BY_ID

    try:
        existing = PERSONAS_BY_ID[str(persona_id)]
    except KeyError as exc:
        raise PersonaVariableError(
            "unknown existing persona {!r}".format(persona_id)
        ) from exc
    return {
        "E1": float(existing["social_susceptibility"]),
        "E2": "high" if existing["id"] == "influencer_amplifier" else "low",
    }


def _assert_registry_integrity() -> None:
    if len(VARIABLE_REGISTRY) != 14:
        raise RuntimeError("persona-variable registry must contain exactly 14 variables")
    validate_theta(REFERENCE_THETA)
    for variable_id, endpoints in FLIP_ENDPOINTS.items():
        variable = VARIABLE_REGISTRY[variable_id]
        for endpoint in endpoints:
            variable.level(endpoint)
    for variable in VARIABLE_REGISTRY.values():
        for level in variable.levels:
            for forbidden in FORBIDDEN_RENDER_TERMS:
                if forbidden in level.fragment:
                    raise RuntimeError(
                        "frozen fragment {} contains forbidden term {!r}".format(
                            level.id,
                            forbidden,
                        )
                    )


_assert_registry_integrity()


__all__ = [
    "EXPECTED_DELTA_DIRECTIONS",
    "EXPECTED_SELL_DIRECTIONS",
    "FIXTURE_BY_VARIABLE",
    "FLIP_ENDPOINTS",
    "FORBIDDEN_RENDER_TERMS",
    "INHERITED_VARIABLES",
    "LEGACY_PERSONA_COORDINATES",
    "LOW_HIGH_ENDPOINTS",
    "PersonaLevel",
    "PersonaVariable",
    "PersonaVariableError",
    "REFERENCE_THETA",
    "VARIABLE_IDS",
    "VARIABLE_REGISTRY",
    "chinese_character_count",
    "derive_seed",
    "fixture_for_variable",
    "flip_pair",
    "inherited_values_for_persona",
    "render",
    "sample_region",
    "sample_uniform",
    "validate_theta",
]
