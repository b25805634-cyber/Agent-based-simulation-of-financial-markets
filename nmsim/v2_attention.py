"""Versioned contracts for the V2 price/volume-only teacher protocol.

This module is deliberately independent from the legacy persona and prompt
machinery.  It defines a small, numeric, single-asset daily observation; a
strict teacher response boundary; deterministic engineering test doubles; and
replicate aggregation.  It performs no network or filesystem access.

The fake teachers in this module are test infrastructure.  Their output is not
evidence about human decisions and must never be reported as such.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import random
import re
import statistics
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


CONTRACT_VERSION = "v2-attention-teacher-contract/1.0.0"
STATE_SCHEMA_VERSION = "v2-attention-state/1.0.0"
PROMPT_SCHEMA_VERSION = "v2-attention-teacher-prompt/1.0.0"
RESPONSE_SCHEMA_VERSION = "v2-attention-teacher-response/1.0.0"
SOFT_TARGET_SCHEMA_VERSION = "v2-attention-soft-target/1.0.0"
STATE_DESIGN_VERSION = "v2-attention-coherent-latent-design/1.0.0"

TIME_SEMANTICS = (
    "one decision at each daily close t; the requested adjustment is submitted "
    "to the next daily batch t+1"
)

ACTION_ORDER: Tuple[str, ...] = ("buy", "hold", "sell")

# This order is part of the model-input contract.  Append-only evolution still
# requires a new STATE_SCHEMA_VERSION because a changed vector changes the
# scientific meaning of trained weights.
FEATURE_ORDER: Tuple[str, ...] = (
    "return_1d",
    "return_5d",
    "return_20d",
    "realized_vol_20d",
    "drawdown_20d",
    "volume_z",
    "position_fraction",
    "unrealized_return",
    "unrealized_return_mask",
    "days_since_trade_scaled",
    "days_since_trade_scaled_mask",
    "post_sale_return",
    "post_sale_return_mask",
    "log10_wealth",
)

# Friendly name used by dataset/training entrypoints.  It is the same frozen
# tuple, not a separately maintained ordering.
FEATURE_NAMES = FEATURE_ORDER

MARKET_FEATURES: Tuple[str, ...] = (
    "return_1d",
    "return_5d",
    "return_20d",
    "realized_vol_20d",
    "drawdown_20d",
    "volume_z",
)

ACCOUNT_FEATURES: Tuple[str, ...] = tuple(
    name for name in FEATURE_ORDER if name not in MARKET_FEATURES
)

MASK_FIELDS: Tuple[str, ...] = (
    "unrealized_return_mask",
    "days_since_trade_scaled_mask",
    "post_sale_return_mask",
)

MASKED_FEATURES: Tuple[Tuple[str, str], ...] = (
    ("unrealized_return", "unrealized_return_mask"),
    ("days_since_trade_scaled", "days_since_trade_scaled_mask"),
    ("post_sale_return", "post_sale_return_mask"),
)

CROSS_FIELD_CONSTRAINTS: Dict[str, str] = {
    "tape_drawdown": (
        "drawdown_20d <= min(0, return_1d, return_5d, return_20d), "
        "because the 20-session maximum includes every referenced close"
    ),
    "sale_history": (
        "post_sale_return_mask=1 implies days_since_trade_scaled_mask=1, "
        "because a recorded sale is a recorded prior trade"
    ),
}

# Closed design domain.  Inputs outside it fail rather than being silently
# clipped.  The return ranges are deliberately wider than a single-market
# price-limit convention because this protocol is not tied to one exchange.
FEATURE_RANGES: Dict[str, Tuple[float, float]] = {
    "return_1d": (-0.25, 0.25),
    "return_5d": (-0.75, 1.25),
    "return_20d": (-0.95, 4.00),
    "realized_vol_20d": (0.00, 1.50),
    "drawdown_20d": (-1.00, 0.00),
    "volume_z": (-6.00, 6.00),
    "position_fraction": (0.00, 1.00),
    "unrealized_return": (-0.95, 5.00),
    "unrealized_return_mask": (0.00, 1.00),
    "days_since_trade_scaled": (0.00, 1.00),
    "days_since_trade_scaled_mask": (0.00, 1.00),
    "post_sale_return": (-0.95, 5.00),
    "post_sale_return_mask": (0.00, 1.00),
    "log10_wealth": (3.00, 10.00),
}

# These units/formulas are part of the Teacher-visible scientific contract.
# They deliberately describe the numeric state rather than an investor type or
# a named behavioural mechanism.
FEATURE_SEMANTICS: Dict[str, str] = {
    "return_1d": "simple close-to-close return P_t/P_(t-1)-1 over one daily session",
    "return_5d": "simple close-to-close cumulative return P_t/P_(t-5)-1",
    "return_20d": "simple close-to-close cumulative return P_t/P_(t-20)-1",
    "realized_vol_20d": (
        "population standard deviation of the latest 20 simple daily returns, "
        "annualized by sqrt(252)"
    ),
    "drawdown_20d": "P_t/max(P_(t-20),...,P_t)-1",
    "volume_z": (
        "current share volume minus the prior-20-session mean, divided by the "
        "prior-20 population standard deviation; when that deviation is zero, "
        "an equal current volume maps to 0 and a different volume maps to the "
        "signed closed-domain boundary (-6 or +6)"
    ),
    "position_fraction": (
        "current security market value divided by cash plus security market value"
    ),
    "unrealized_return": "P_t/position_cost_basis_price-1 when its mask is 1",
    "unrealized_return_mask": "1 when a held position has a known cost basis, else 0",
    "days_since_trade_scaled": (
        "daily decision intervals since the last trade divided by 20 and capped at 1, "
        "when its mask is 1"
    ),
    "days_since_trade_scaled_mask": "1 when a prior trade time exists, else 0",
    "post_sale_return": "P_t/most_recent_sale_price-1 when its mask is 1",
    "post_sale_return_mask": "1 when a prior sale price exists, else 0",
    "log10_wealth": "base-10 logarithm of net account wealth measured in integer cents",
}

FAKE_TEST_TEACHER_KIND = "engineering_test_double_not_human_evidence"
FAKE_NULL_TEACHER_KIND = "constant_null_engineering_control"

# This list is a regression guard, not an exhaustive ontology.  Prompt text is
# scanned case-insensitively.  The production prompt below is intentionally
# plain and does not name a category of market participant or a behavioural
# finance mechanism.
FORBIDDEN_PROMPT_TERMS: Tuple[str, ...] = (
    "persona",
    "identity",
    "retail investor",
    "institutional investor",
    "market maker",
    "dealer",
    "speculator",
    "trader type",
    "loss aversion",
    "disposition effect",
    "prospect theory",
    "herding",
    "anchoring effect",
    "overconfidence",
    "behavioral finance",
    "behavioural finance",
    "散户",
    "机构",
    "庄家",
    "大户",
    "期货交易者",
    "价值投资者",
    "请表现出",
    "损失厌恶",
    "处置效应",
    "前景理论",
    "羊群效应",
    "锚定效应",
    "过度自信",
    "行为金融",
)

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_STATE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,191}$")
_MAX_REASONING_CHARS = 4000


class V2ContractError(ValueError):
    """Base class for a closed-boundary contract violation."""


class StateValidationError(V2ContractError):
    """The numeric state or its identity is outside the frozen contract."""


class TeacherResponseError(V2ContractError):
    """A teacher response cannot be admitted as a valid replicate."""


def _finite_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StateValidationError("{} must be a finite number".format(name))
    try:
        result = float(value)
    except (OverflowError, TypeError, ValueError) as error:
        raise StateValidationError("{} must be finite".format(name)) from error
    if not math.isfinite(result):
        raise StateValidationError("{} must be finite".format(name))
    # Canonicalise negative zero before hashing or vectorisation.
    return 0.0 if result == 0.0 else result


def _validate_identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise StateValidationError(
            "{} must match {}".format(name, _IDENTIFIER_RE.pattern)
        )
    return value


def _canonical_value(value: Any) -> Any:
    """Convert a supported value to a JSON-safe canonical tree.

    Objects must opt in through ``to_dict``.  In particular,
    :class:`ParsedTeacherResponse` intentionally does not opt in because doing
    so could accidentally serialize private rationale alongside public labels.
    """

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise V2ContractError("canonical JSON forbids non-finite floats")
        return 0.0 if value == 0.0 else value
    if isinstance(value, Mapping):
        converted: Dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise V2ContractError("canonical JSON mapping keys must be strings")
            converted[key] = _canonical_value(item)
        return converted
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _canonical_value(to_dict())
    raise V2ContractError(
        "unsupported canonical JSON value type: {}".format(type(value).__name__)
    )


def canonical_json(value: Any) -> str:
    """Return stable UTF-8 JSON with sorted keys and no NaN extensions."""

    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_hex(value: Any) -> str:
    """SHA-256 of :func:`canonical_json`, encoded as lower-case hex."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def derive_seed(*parts: Any, **kwargs: Any) -> int:
    """Derive a stable unsigned 64-bit integer seed from ordered components."""

    namespace = kwargs.pop("namespace", CONTRACT_VERSION)
    if kwargs:
        raise TypeError("unexpected keyword arguments: {}".format(sorted(kwargs)))
    digest = hashlib.sha256(
        canonical_json({"namespace": namespace, "parts": list(parts)}).encode("utf-8")
    ).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=False)


def contract_descriptor() -> Dict[str, Any]:
    """Return the complete scientific contract descriptor used for hashing."""

    return {
        "contract_version": CONTRACT_VERSION,
        "state_design_version": STATE_DESIGN_VERSION,
        "state_schema_version": STATE_SCHEMA_VERSION,
        "prompt_schema_version": PROMPT_SCHEMA_VERSION,
        "response_schema_version": RESPONSE_SCHEMA_VERSION,
        "soft_target_schema_version": SOFT_TARGET_SCHEMA_VERSION,
        "time_semantics": TIME_SEMANTICS,
        "feature_names": list(FEATURE_NAMES),
        "feature_ranges": {
            name: list(FEATURE_RANGES[name]) for name in FEATURE_NAMES
        },
        "feature_semantics": {
            name: FEATURE_SEMANTICS[name] for name in FEATURE_NAMES
        },
        "masked_features": [list(pair) for pair in MASKED_FEATURES],
        "cross_field_constraints": dict(sorted(CROSS_FIELD_CONSTRAINTS.items())),
        "action_order": list(ACTION_ORDER),
    }


CONTRACT_HASH = sha256_hex(contract_descriptor())


@dataclass(frozen=True)
class V2AttentionState:
    """Frozen numeric observation used by both teacher and student."""

    return_1d: float
    return_5d: float
    return_20d: float
    realized_vol_20d: float
    drawdown_20d: float
    volume_z: float
    position_fraction: float
    unrealized_return: float
    unrealized_return_mask: int
    days_since_trade_scaled: float
    days_since_trade_scaled_mask: int
    post_sale_return: float
    post_sale_return_mask: int
    log10_wealth: float

    def __post_init__(self) -> None:
        for name in FEATURE_ORDER:
            raw_value = getattr(self, name)
            value = _finite_number(raw_value, name)
            low, high = FEATURE_RANGES[name]
            if value < low or value > high:
                raise StateValidationError(
                    "{}={} outside [{}, {}]".format(name, value, low, high)
                )
            if name in MASK_FIELDS:
                if value not in (0.0, 1.0):
                    raise StateValidationError("{} must be exactly 0 or 1".format(name))
                object.__setattr__(self, name, int(value))
            else:
                object.__setattr__(self, name, value)

        for value_name, mask_name in MASKED_FEATURES:
            if getattr(self, mask_name) == 0 and getattr(self, value_name) != 0.0:
                raise StateValidationError(
                    "{} must be the canonical 0.0 placeholder when {}=0".format(
                        value_name, mask_name
                    )
                )
        if self.position_fraction == 0.0 and self.unrealized_return_mask != 0:
            raise StateValidationError(
                "unrealized_return cannot be known when position_fraction=0"
            )
        if self.drawdown_20d > min(
            0.0, self.return_1d, self.return_5d, self.return_20d
        ) + 1e-12:
            raise StateValidationError(
                "drawdown_20d is inconsistent with the referenced close returns"
            )
        if (
            self.post_sale_return_mask == 1
            and self.days_since_trade_scaled_mask != 1
        ):
            raise StateValidationError(
                "post-sale history requires a known prior-trade time"
            )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "V2AttentionState":
        if not isinstance(value, Mapping):
            raise StateValidationError("state must be a mapping")
        if any(not isinstance(key, str) for key in value.keys()):
            raise StateValidationError("state field names must be strings")
        supplied = set(value.keys())
        expected = set(FEATURE_ORDER)
        if supplied != expected:
            missing = sorted(expected - supplied)
            extra = sorted(supplied - expected)
            raise StateValidationError(
                "state fields must match exactly; missing={}, extra={}".format(
                    missing, extra
                )
            )
        return cls(**{name: value[name] for name in FEATURE_ORDER})

    def to_dict(self) -> Dict[str, Any]:
        return {name: getattr(self, name) for name in FEATURE_ORDER}

    def to_feature_vector(self) -> Tuple[float, ...]:
        return tuple(float(getattr(self, name)) for name in FEATURE_ORDER)

    @property
    def scientific_state_hash(self) -> str:
        return sha256_hex(
            {
                "schema_version": STATE_SCHEMA_VERSION,
                "features": self.to_dict(),
            }
        )


@dataclass(frozen=True)
class TeacherObservation:
    """A state plus stable study/family/state identity hierarchy."""

    study_id: str
    family_id: str
    state_id: str
    state: V2AttentionState

    def __post_init__(self) -> None:
        _validate_identifier(self.study_id, "study_id")
        _validate_identifier(self.family_id, "family_id")
        if not isinstance(self.state_id, str) or not _STATE_ID_RE.fullmatch(self.state_id):
            raise StateValidationError(
                "state_id must match {}".format(_STATE_ID_RE.pattern)
            )
        if not self.state_id.startswith(self.family_id + "/"):
            raise StateValidationError(
                "state_id must be a child of family_id using 'family_id/'"
            )
        if not isinstance(self.state, V2AttentionState):
            raise StateValidationError("state must be V2AttentionState")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "study_id": self.study_id,
            "family_id": self.family_id,
            "state_id": self.state_id,
            "state": self.state.to_dict(),
            "scientific_state_hash": self.state.scientific_state_hash,
        }


def make_observation(
    study_id: str,
    family_id: str,
    state_index: int,
    state: V2AttentionState,
) -> TeacherObservation:
    """Create a content-addressed child identity for a design state."""

    _validate_identifier(study_id, "study_id")
    _validate_identifier(family_id, "family_id")
    if isinstance(state_index, bool) or not isinstance(state_index, int) or state_index < 0:
        raise StateValidationError("state_index must be a non-negative integer")
    if not isinstance(state, V2AttentionState):
        raise StateValidationError("state must be V2AttentionState")
    identity_hash = sha256_hex(
        {
            "schema_version": STATE_SCHEMA_VERSION,
            "family_id": family_id,
            "state": state.to_dict(),
        }
    )
    state_id = "{}/{:05d}-{}".format(family_id, state_index, identity_hash[:12])
    return TeacherObservation(study_id, family_id, state_id, state)


def _base_state_values() -> Dict[str, Any]:
    return {
        "return_1d": 0.0,
        "return_5d": 0.0,
        "return_20d": 0.0,
        "realized_vol_20d": 0.15,
        "drawdown_20d": 0.0,
        "volume_z": 0.0,
        "position_fraction": 0.0,
        "unrealized_return": 0.0,
        "unrealized_return_mask": 0,
        "days_since_trade_scaled": 0.0,
        "days_since_trade_scaled_mask": 0,
        "post_sale_return": 0.0,
        "post_sale_return_mask": 0,
        "log10_wealth": 5.0,
    }


# Ordered, explicit anchors.  Each market summary is derived from an actual
# sequence of 20 simple daily returns rather than independently invented
# summary statistics.  Changing a tape, account value, or order changes state
# identities and therefore the scientific-config identity.
_ANCHOR_DEFINITIONS: Tuple[
    Tuple[str, Tuple[float, ...], Dict[str, Any]], ...
] = (
    ("anchor.neutral_cash", (0.0,) * 20, {}),
    (
        "anchor.cash_rally",
        (0.012,) * 15 + (0.020, 0.025, 0.030, 0.035, 0.040),
        {
            "volume_z": 1.5,
        },
    ),
    (
        "anchor.holder_rally",
        (0.011,) * 15 + (0.015, 0.020, 0.025, 0.030, 0.030),
        {
            "volume_z": 1.0,
            "position_fraction": 0.70,
            "unrealized_return": 0.18,
            "unrealized_return_mask": 1,
            "days_since_trade_scaled": 0.15,
            "days_since_trade_scaled_mask": 1,
        },
    ),
    (
        "anchor.holder_drawdown",
        (-0.012,) * 15 + (-0.020, -0.030, -0.040, -0.040, -0.050),
        {
            "volume_z": 2.0,
            "position_fraction": 0.80,
            "unrealized_return": -0.30,
            "unrealized_return_mask": 1,
            "days_since_trade_scaled": 0.35,
            "days_since_trade_scaled_mask": 1,
        },
    ),
    (
        "anchor.post_sale_rally",
        (0.008,) * 15 + (0.025, 0.030, 0.035, 0.040, 0.040),
        {
            "volume_z": 1.8,
            "days_since_trade_scaled": 0.10,
            "days_since_trade_scaled_mask": 1,
            "post_sale_return": 0.12,
            "post_sale_return_mask": 1,
        },
    ),
    (
        "anchor.fully_invested",
        (-0.010, 0.010) * 10,
        {
            "position_fraction": 1.0,
            "unrealized_return": 0.05,
            "unrealized_return_mask": 1,
            "days_since_trade_scaled": 0.20,
            "days_since_trade_scaled_mask": 1,
        },
    ),
    (
        "anchor.max_trade_age",
        (-0.006,) * 20,
        {
            "position_fraction": 0.40,
            "unrealized_return": -0.10,
            "unrealized_return_mask": 1,
            "days_since_trade_scaled": 1.0,
            "days_since_trade_scaled_mask": 1,
        },
    ),
    (
        "anchor.volume_dry_jump",
        (-0.020, 0.020) * 10,
        {"volume_z": 6.0, "position_fraction": 0.50},
    ),
    (
        "anchor.volume_dry_drop",
        (-0.010,) * 20,
        {"volume_z": -6.0, "position_fraction": 0.90},
    ),
)

ANCHOR_FAMILY_ORDER: Tuple[str, ...] = tuple(
    item[0] for item in _ANCHOR_DEFINITIONS
)


def _market_summary_from_daily_returns(
    daily_returns: Sequence[float],
) -> Dict[str, float]:
    """Derive the six visible tape fields from one coherent 21-close path."""

    if len(daily_returns) != 20:
        raise StateValidationError("a coherent market tape requires 20 daily returns")
    returns: List[float] = []
    closes = [1.0]
    for index, raw in enumerate(daily_returns):
        value = _finite_number(raw, "daily_return[{}]".format(index))
        if value <= -1.0:
            raise StateValidationError("daily returns must preserve a positive close")
        returns.append(value)
        closes.append(closes[-1] * (1.0 + value))
    last = closes[-1]
    summary = {
        "return_1d": returns[-1],
        "return_5d": last / closes[-6] - 1.0,
        "return_20d": last / closes[0] - 1.0,
        "realized_vol_20d": statistics.pstdev(returns) * math.sqrt(252.0),
        "drawdown_20d": last / max(closes) - 1.0,
    }
    for name, value in summary.items():
        low, high = FEATURE_RANGES[name]
        if value < low - 1e-12 or value > high + 1e-12:
            raise StateValidationError(
                "coherent tape generated {} outside its state domain".format(name)
            )
        summary[name] = min(high, max(low, value))
    return summary


def design_anchor_states(study_id: str) -> Tuple[TeacherObservation, ...]:
    """Return the frozen anchor design in contract order."""

    _validate_identifier(study_id, "study_id")
    observations: List[TeacherObservation] = []
    for family_id, daily_returns, changes in _ANCHOR_DEFINITIONS:
        values = _base_state_values()
        values.update(_market_summary_from_daily_returns(daily_returns))
        values.update(changes)
        state = V2AttentionState.from_mapping(values)
        observations.append(make_observation(study_id, family_id, 0, state))
    return tuple(observations)


def _unit_interval(*parts: Any) -> float:
    # The midpoint makes both endpoints impossible and avoids a zero draw from
    # turning a lower-bound log-like feature into a special case.
    return (derive_seed(*parts, namespace="v2-lhs-unit/1") + 0.5) / float(2 ** 64)


def _stable_permutation(size: int, *parts: Any) -> List[int]:
    return sorted(
        range(size),
        key=lambda index: (
            derive_seed(*parts, index, namespace="v2-lhs-permutation/1"),
            index,
        ),
    )


def _lhs_values(
    size: int,
    low: float,
    high: float,
    *parts: Any
) -> List[float]:
    ranks = _stable_permutation(size, *parts)
    values: List[float] = []
    for row_index, rank in enumerate(ranks):
        jitter = _unit_interval(*parts, row_index, "jitter")
        values.append(low + ((rank + jitter) / float(size)) * (high - low))
    return values


_TAPE_REGIMES: Tuple[str, ...] = ("decline", "flat", "rise")
_POSITION_REGIMES: Tuple[str, ...] = ("cash", "mixed", "invested")
_JOINT_DESIGN_STRATA: Tuple[Tuple[str, str], ...] = tuple(
    (tape, position)
    for tape in _TAPE_REGIMES
    for position in _POSITION_REGIMES
)
_RETURN_20D_DESIGN_RANGES: Dict[str, Tuple[float, float]] = {
    "decline": (-0.80, -0.12),
    "flat": (-0.08, 0.08),
    "rise": (0.12, 2.50),
}
_POSITION_DESIGN_RANGES: Dict[str, Tuple[float, float]] = {
    "cash": (0.005, 0.195),
    "mixed": (0.205, 0.795),
    "invested": (0.805, 0.995),
}


def state_design_descriptor() -> Dict[str, Any]:
    """Return the explicit coherent-state generator contract."""

    return {
        "schema_version": STATE_DESIGN_VERSION,
        "anchor_family_order": list(ANCHOR_FAMILY_ORDER),
        "market_summary_source": "derived_from_20_simple_daily_returns",
        "lhs_strata": [list(item) for item in _JOINT_DESIGN_STRATA],
        "return_1d_sampling": "global_latin_hypercube_over_closed_feature_range",
        "return_20d_ranges_by_tape_regime": {
            key: list(value)
            for key, value in sorted(_RETURN_20D_DESIGN_RANGES.items())
        },
        "position_ranges_by_regime": {
            key: list(value)
            for key, value in sorted(_POSITION_DESIGN_RANGES.items())
        },
        "history_categories": [
            "no_known_prior_trade",
            "known_prior_trade_without_known_sale",
            "known_prior_sale",
        ],
        "cross_field_constraints": dict(sorted(CROSS_FIELD_CONSTRAINTS.items())),
        "reachable_boundary_anchors": {
            "position_fraction": [0.0, 1.0],
            "days_since_trade_scaled": [0.0, 1.0],
            "volume_z": [-6.0, 6.0],
        },
    }


def _balanced_joint_strata(
    size: int, design_seed: int, family_id: str
) -> List[Tuple[str, str]]:
    row_order = _stable_permutation(
        size, design_seed, family_id, "joint-design-stratum"
    )
    assignments: List[Optional[Tuple[str, str]]] = [None] * size
    for rank, row_index in enumerate(row_order):
        assignments[row_index] = _JOINT_DESIGN_STRATA[
            rank % len(_JOINT_DESIGN_STRATA)
        ]
    return [
        item
        for item in assignments
        if item is not None
    ]


def _stratum_lhs_values(
    assignments: Sequence[Tuple[str, str]],
    *,
    stratum_index: int,
    ranges: Mapping[str, Tuple[float, float]],
    design_seed: int,
    family_id: str,
    label: str,
) -> List[float]:
    values = [0.0] * len(assignments)
    for stratum in sorted(ranges):
        indices = [
            index
            for index, assignment in enumerate(assignments)
            if assignment[stratum_index] == stratum
        ]
        low, high = ranges[stratum]
        sampled = _lhs_values(
            len(indices), low, high, design_seed, family_id, label, stratum
        )
        for index, value in zip(indices, sampled):
            values[index] = value
    return values


def _coherent_daily_returns(
    return_1d: float,
    return_20d: float,
    amplitude: float,
    phase: float,
) -> Tuple[float, ...]:
    """Construct 20 daily returns with exact one- and twenty-day returns."""

    prior_log_total = math.log1p(return_20d) - math.log1p(return_1d)
    base = prior_log_total / 19.0
    raw_noise = [
        math.sin(2.0 * math.pi * (index / 19.0 + phase))
        + 0.5 * math.sin(4.0 * math.pi * (index / 19.0 + phase))
        for index in range(19)
    ]
    mean_noise = statistics.fmean(raw_noise)
    centered = [value - mean_noise for value in raw_noise]
    maximum = max(abs(value) for value in centered)
    scale = 0.0 if maximum == 0.0 else amplitude / maximum
    prior = tuple(math.expm1(base + value * scale) for value in centered)
    return prior + (return_1d,)


def design_latin_hypercube_states(
    study_id: str,
    n_states: int,
    design_seed: int,
    family_id: str = "lhs",
) -> Tuple[TeacherObservation, ...]:
    """Generate deterministic coherent states from stratified latent variables.

    The visible tape summaries are derived from an actual 21-close path.  A
    balanced latent tape-regime x position-regime grid prevents the independent
    summary combinations that made the earlier engineering draft physically
    impossible.  Exact reachable boundaries remain explicit anchors.
    """

    _validate_identifier(study_id, "study_id")
    _validate_identifier(family_id, "family_id")
    if isinstance(n_states, bool) or not isinstance(n_states, int) or n_states <= 0:
        raise StateValidationError("n_states must be a positive integer")
    if isinstance(design_seed, bool) or not isinstance(design_seed, int):
        raise StateValidationError("design_seed must be an integer")

    assignments = _balanced_joint_strata(n_states, design_seed, family_id)
    return_1d = _lhs_values(
        n_states,
        *FEATURE_RANGES["return_1d"],
        design_seed,
        family_id,
        "return_1d",
    )
    return_20d = _stratum_lhs_values(
        assignments,
        stratum_index=0,
        ranges=_RETURN_20D_DESIGN_RANGES,
        design_seed=design_seed,
        family_id=family_id,
        label="return_20d",
    )
    position_fraction = _stratum_lhs_values(
        assignments,
        stratum_index=1,
        ranges=_POSITION_DESIGN_RANGES,
        design_seed=design_seed,
        family_id=family_id,
        label="position_fraction",
    )
    amplitudes = _lhs_values(
        n_states, 0.0, 0.045, design_seed, family_id, "path-amplitude"
    )
    sampled: Dict[str, List[float]] = {}
    for feature_name in (
        "volume_z",
        "unrealized_return",
        "days_since_trade_scaled",
        "post_sale_return",
        "log10_wealth",
    ):
        sampled[feature_name] = _lhs_values(
            n_states,
            *FEATURE_RANGES[feature_name],
            design_seed,
            family_id,
            feature_name,
        )

    unrealized_ranks = _stable_permutation(
        n_states, design_seed, family_id, "unrealized-history"
    )
    unrealized_masks = [
        1 if rank >= n_states // 2 else 0 for rank in unrealized_ranks
    ]
    history_order = _stable_permutation(
        n_states, design_seed, family_id, "trade-sale-history"
    )
    history_categories = [0] * n_states
    for rank, row_index in enumerate(history_order):
        history_categories[row_index] = rank % 3

    observations: List[TeacherObservation] = []
    for row_index in range(n_states):
        path = _coherent_daily_returns(
            return_1d[row_index],
            return_20d[row_index],
            amplitudes[row_index],
            _unit_interval(design_seed, family_id, row_index, "path-phase"),
        )
        values: Dict[str, Any] = _base_state_values()
        values.update(_market_summary_from_daily_returns(path))
        values.update(
            {
                "volume_z": sampled["volume_z"][row_index],
                "position_fraction": position_fraction[row_index],
                "unrealized_return_mask": unrealized_masks[row_index],
                "days_since_trade_scaled_mask": int(
                    history_categories[row_index] >= 1
                ),
                "post_sale_return_mask": int(history_categories[row_index] == 2),
                "log10_wealth": sampled["log10_wealth"][row_index],
            }
        )
        for value_name, mask_name in MASKED_FEATURES:
            values[value_name] = (
                sampled[value_name][row_index] if values[mask_name] else 0.0
            )
        state = V2AttentionState.from_mapping(values)
        # A family is the indivisible split group.  V1 has no trajectory or
        # counterfactual linkage between different LHS rows, so grouping every
        # row under a single ``lhs`` family would make train/validation/test
        # separation impossible.  Replicates of this row still share the same
        # family and state identity.
        row_family_id = "{}.{:05d}".format(family_id, row_index)
        observations.append(make_observation(study_id, row_family_id, 0, state))
    return tuple(observations)


def generate_state_design(
    count: int,
    seed: int,
    study_id: str = "v2-study",
) -> Tuple[TeacherObservation, ...]:
    """Return exactly ``count`` states: frozen anchors, then LHS coverage.

    The minimum is the number of frozen anchors.  This avoids a seemingly valid
    design silently omitting one of its named edge/contrast states.
    """

    if isinstance(count, bool) or not isinstance(count, int):
        raise StateValidationError("count must be an integer")
    minimum = len(_ANCHOR_DEFINITIONS)
    if count < minimum:
        raise StateValidationError(
            "count must be at least {} to include every frozen anchor".format(minimum)
        )
    anchors = design_anchor_states(study_id)
    remainder = count - minimum
    if remainder == 0:
        return anchors
    lhs = design_latin_hypercube_states(
        study_id=study_id,
        n_states=remainder,
        design_seed=seed,
        family_id="lhs",
    )
    return anchors + lhs


@dataclass(frozen=True)
class TeacherPrompt:
    """Exact two-message prompt.  It contains no run identity or private text."""

    system: str
    user: str

    def to_dict(self) -> Dict[str, str]:
        return {"system": self.system, "user": self.user}

    def to_messages(self) -> List[Dict[str, str]]:
        return [
            {"role": "system", "content": self.system},
            {"role": "user", "content": self.user},
        ]

    @property
    def prompt_hash(self) -> str:
        return sha256_hex(
            {"schema_version": PROMPT_SCHEMA_VERSION, "messages": self.to_messages()}
        )


_TEACHER_SYSTEM_PROMPT = """You produce one portfolio adjustment for one asset at a fixed daily decision point. Use only the supplied price/volume summaries and account state; do not infer external facts. Return exactly one JSON object with exactly these keys: action, intensity, reasoning. action must be buy, hold, or sell. For buy, intensity is the fraction of currently feasible cash to use. For sell, intensity is the fraction of currently held shares to release. For hold, intensity must be 0. reasoning must be a concise private diagnostic string. Do not add markdown or any text outside the JSON object."""


def render_teacher_prompt(observation: TeacherObservation) -> TeacherPrompt:
    """Render the frozen teacher request without exposing design identities."""

    if not isinstance(observation, TeacherObservation):
        raise StateValidationError("observation must be TeacherObservation")
    state = observation.state
    payload = {
        "prompt_schema_version": PROMPT_SCHEMA_VERSION,
        "response_schema_version": RESPONSE_SCHEMA_VERSION,
        "time_semantics": TIME_SEMANTICS,
        "market_price_volume_summary": {
            name: getattr(state, name) for name in MARKET_FEATURES
        },
        "account_state": {name: getattr(state, name) for name in ACCOUNT_FEATURES},
        "feature_semantics": {
            name: FEATURE_SEMANTICS[name] for name in FEATURE_ORDER
        },
        "missing_value_rule": (
            "a mask of 0 means the paired value is unavailable and its numeric "
            "field is the required 0.0 placeholder"
        ),
    }
    prompt = TeacherPrompt(
        system=_TEACHER_SYSTEM_PROMPT,
        user=canonical_json(payload),
    )
    joined = (prompt.system + "\n" + prompt.user).casefold()
    violations = [term for term in FORBIDDEN_PROMPT_TERMS if term.casefold() in joined]
    if violations:
        raise V2ContractError(
            "teacher prompt contains forbidden terms: {}".format(violations)
        )
    return prompt


def build_teacher_prompt(
    state_or_observation: Any,
) -> TeacherPrompt:
    """Entrypoint-friendly adapter accepting a state or full observation."""

    if isinstance(state_or_observation, TeacherObservation):
        observation = state_or_observation
    elif isinstance(state_or_observation, V2AttentionState):
        observation = make_observation(
            study_id="prompt-render",
            family_id="prompt",
            state_index=0,
            state=state_or_observation,
        )
    else:
        raise StateValidationError(
            "build_teacher_prompt expects V2AttentionState or TeacherObservation"
        )
    return render_teacher_prompt(observation)


@dataclass(frozen=True)
class PublicTeacherDecision:
    """Public, training-safe projection of a parsed response."""

    action: str
    intensity: float

    def __post_init__(self) -> None:
        if self.action not in ACTION_ORDER:
            raise TeacherResponseError("invalid action {!r}".format(self.action))
        if isinstance(self.intensity, bool) or not isinstance(self.intensity, (int, float)):
            raise TeacherResponseError("intensity must be a finite number")
        try:
            intensity = float(self.intensity)
        except (OverflowError, TypeError, ValueError) as error:
            raise TeacherResponseError("intensity must be finite") from error
        if not math.isfinite(intensity) or not 0.0 <= intensity <= 1.0:
            raise TeacherResponseError("intensity must be in [0, 1]")
        if self.action == "hold" and intensity != 0.0:
            raise TeacherResponseError("hold intensity must be exactly 0")
        if self.action != "hold" and intensity <= 0.0:
            raise TeacherResponseError("buy/sell intensity must be greater than 0")
        object.__setattr__(self, "intensity", 0.0 if intensity == 0.0 else intensity)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "response_schema_version": RESPONSE_SCHEMA_VERSION,
            "action": self.action,
            "intensity": self.intensity,
        }


@dataclass(frozen=True)
class ParsedTeacherResponse:
    """Parsed boundary with private rationale structurally separated.

    There is intentionally no ``to_dict`` method.  Call ``public_record`` for
    training artifacts and route ``private_rationale`` only to private storage.
    """

    public: PublicTeacherDecision
    private_rationale: str

    def public_record(self) -> Dict[str, Any]:
        return self.public.to_dict()

    def private_record(self) -> Dict[str, str]:
        return {
            "response_schema_version": RESPONSE_SCHEMA_VERSION,
            "reasoning": self.private_rationale,
        }


def _strict_object_pairs(pairs: Iterable[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise TeacherResponseError("duplicate JSON key {!r}".format(key))
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise TeacherResponseError("non-finite JSON constant {!r}".format(value))


def parse_teacher_response(
    raw_response: str,
    state: V2AttentionState,
) -> ParsedTeacherResponse:
    """Strictly parse one response, failing closed on every violation.

    A caller must record any raised :class:`TeacherResponseError` as a failed
    attempt.  It is scientifically invalid to convert that failure to hold.
    """

    if not isinstance(raw_response, str):
        raise TeacherResponseError("raw_response must be a string")
    if not isinstance(state, V2AttentionState):
        raise TeacherResponseError("state must be V2AttentionState")
    try:
        value = json.loads(
            raw_response,
            object_pairs_hook=_strict_object_pairs,
            parse_constant=_reject_json_constant,
        )
    except TeacherResponseError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise TeacherResponseError("invalid JSON: {}".format(exc)) from exc
    if not isinstance(value, dict):
        raise TeacherResponseError("response root must be a JSON object")
    expected = {"action", "intensity", "reasoning"}
    supplied = set(value.keys())
    if supplied != expected:
        raise TeacherResponseError(
            "response fields must match exactly; missing={}, extra={}".format(
                sorted(expected - supplied), sorted(supplied - expected)
            )
        )
    action = value["action"]
    if not isinstance(action, str) or action not in ACTION_ORDER:
        raise TeacherResponseError("action must be exactly buy, hold, or sell")
    intensity_value = value["intensity"]
    if isinstance(intensity_value, bool) or not isinstance(
        intensity_value, (int, float)
    ):
        raise TeacherResponseError("intensity must be a finite JSON number")
    try:
        intensity = float(intensity_value)
    except (OverflowError, TypeError, ValueError) as error:
        raise TeacherResponseError("intensity must be a finite JSON number") from error
    if not math.isfinite(intensity) or not 0.0 <= intensity <= 1.0:
        raise TeacherResponseError("intensity must be in [0, 1]")
    if action == "hold" and intensity != 0.0:
        raise TeacherResponseError("hold intensity must be exactly 0")
    if action != "hold" and intensity <= 0.0:
        raise TeacherResponseError("buy/sell intensity must be greater than 0")
    if action == "sell" and state.position_fraction == 0.0:
        raise TeacherResponseError("sell is infeasible with no held shares")
    if action == "buy" and state.position_fraction == 1.0:
        raise TeacherResponseError("buy is infeasible with no feasible cash")
    reasoning = value["reasoning"]
    if not isinstance(reasoning, str) or not reasoning.strip():
        raise TeacherResponseError("reasoning must be a non-empty string")
    if len(reasoning) > _MAX_REASONING_CHARS:
        raise TeacherResponseError(
            "reasoning exceeds {} characters".format(_MAX_REASONING_CHARS)
        )
    return ParsedTeacherResponse(
        public=PublicTeacherDecision(action=action, intensity=intensity),
        private_rationale=reasoning,
    )


def fake_test_teacher(
    observation: TeacherObservation,
    replicate_index: int,
) -> str:
    """Return deterministic, structured fake output for engineering tests.

    The mapping below is intentionally synthetic and includes replicate noise.
    It does not encode or estimate human behaviour.  The seed hierarchy binds
    every draw to study, family, state, and replicate identities.
    """

    if not isinstance(observation, TeacherObservation):
        raise StateValidationError("observation must be TeacherObservation")
    if (
        isinstance(replicate_index, bool)
        or not isinstance(replicate_index, int)
        or replicate_index < 0
    ):
        raise StateValidationError("replicate_index must be a non-negative integer")
    seed = derive_seed(
        FAKE_TEST_TEACHER_KIND,
        observation.study_id,
        observation.family_id,
        observation.state_id,
        replicate_index,
        namespace="v2-fake-test-teacher/1",
    )
    rng = random.Random(seed)
    state = observation.state
    # Scales are only engineering fixtures chosen to exercise all branches.
    signal = (
        0.28 * (state.return_1d / 0.25)
        + 0.30 * (state.return_5d / 1.25)
        + 0.22 * (state.return_20d / 4.0)
        + 0.08 * (state.volume_z / 6.0)
        + 0.12 * (state.drawdown_20d / 1.0)
        - 0.10 * state.position_fraction
        + rng.gauss(0.0, 0.20)
    )
    if signal > 0.10 and state.position_fraction < 1.0:
        action = "buy"
    elif signal < -0.10 and state.position_fraction > 0.0:
        action = "sell"
    else:
        action = "hold"
    if action == "hold":
        intensity = 0.0
    else:
        intensity = min(1.0, max(0.05, abs(signal) + rng.uniform(0.02, 0.18)))
    return canonical_json(
        {
            "action": action,
            "intensity": intensity,
            "reasoning": (
                "Synthetic engineering test-double response; it is not empirical "
                "evidence about people."
            ),
        }
    )


def fake_null_teacher(
    observation: TeacherObservation,
    replicate_index: int,
) -> str:
    """Constant hold control for null-path and leakage tests."""

    if not isinstance(observation, TeacherObservation):
        raise StateValidationError("observation must be TeacherObservation")
    if (
        isinstance(replicate_index, bool)
        or not isinstance(replicate_index, int)
        or replicate_index < 0
    ):
        raise StateValidationError("replicate_index must be a non-negative integer")
    return canonical_json(
        {
            "action": "hold",
            "intensity": 0.0,
            "reasoning": "Constant null engineering control.",
        }
    )


# Explicit provider-style alias used by the managed experiment adapter.  It is
# still an engineering fake and its kind marker must be carried into manifests.
fake_provider_response = fake_test_teacher


@dataclass(frozen=True)
class TeacherReplicateResult:
    """Public outcome of one attempted replicate (success XOR failure)."""

    family_id: str
    state_id: str
    replicate_index: int
    decision: Optional[PublicTeacherDecision]
    failure_code: Optional[str]

    def __post_init__(self) -> None:
        _validate_identifier(self.family_id, "family_id")
        if (
            not isinstance(self.state_id, str)
            or not _STATE_ID_RE.fullmatch(self.state_id)
            or not self.state_id.startswith(self.family_id + "/")
        ):
            raise StateValidationError("replicate state_id must be a family child")
        if (
            isinstance(self.replicate_index, bool)
            or not isinstance(self.replicate_index, int)
            or self.replicate_index < 0
        ):
            raise StateValidationError("replicate_index must be a non-negative integer")
        success = self.decision is not None
        failure = self.failure_code is not None
        if success == failure:
            raise V2ContractError(
                "replicate must contain exactly one of decision or failure_code"
            )
        if success and not isinstance(self.decision, PublicTeacherDecision):
            raise V2ContractError("decision must be PublicTeacherDecision")
        if failure and (
            not isinstance(self.failure_code, str)
            or not self.failure_code
            or len(self.failure_code) > 128
        ):
            raise V2ContractError("failure_code must be a short non-empty string")

    @classmethod
    def success(
        cls,
        observation: TeacherObservation,
        replicate_index: int,
        parsed: ParsedTeacherResponse,
    ) -> "TeacherReplicateResult":
        if not isinstance(observation, TeacherObservation):
            raise V2ContractError("observation must be TeacherObservation")
        if not isinstance(parsed, ParsedTeacherResponse):
            raise V2ContractError("parsed must be ParsedTeacherResponse")
        return cls(
            family_id=observation.family_id,
            state_id=observation.state_id,
            replicate_index=replicate_index,
            decision=parsed.public,
            failure_code=None,
        )

    @classmethod
    def failure(
        cls,
        observation: TeacherObservation,
        replicate_index: int,
        failure_code: str,
    ) -> "TeacherReplicateResult":
        if not isinstance(observation, TeacherObservation):
            raise V2ContractError("observation must be TeacherObservation")
        return cls(
            family_id=observation.family_id,
            state_id=observation.state_id,
            replicate_index=replicate_index,
            decision=None,
            failure_code=failure_code,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "family_id": self.family_id,
            "state_id": self.state_id,
            "replicate_index": self.replicate_index,
            "status": "valid" if self.decision is not None else "failed",
            "decision": None if self.decision is None else self.decision.to_dict(),
            "failure_code": self.failure_code,
        }


@dataclass(frozen=True)
class ConditionalIntensityStats:
    n: int
    mean: Optional[float]
    variance: Optional[float]

    def to_dict(self) -> Dict[str, Any]:
        return {"n": self.n, "mean": self.mean, "variance": self.variance}


@dataclass(frozen=True)
class SoftTeacherTarget:
    """Empirical target over valid replicates, with honest-N accounting."""

    family_id: str
    state_id: str
    attempted_n: int
    valid_n: int
    failed_n: int
    action_probabilities: Dict[str, Optional[float]]
    conditional_intensity: Dict[str, ConditionalIntensityStats]
    failure_counts: Dict[str, int]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": SOFT_TARGET_SCHEMA_VERSION,
            "family_id": self.family_id,
            "state_id": self.state_id,
            "attempted_n": self.attempted_n,
            "valid_n": self.valid_n,
            "failed_n": self.failed_n,
            "action_probabilities": {
                action: self.action_probabilities[action] for action in ACTION_ORDER
            },
            "conditional_intensity": {
                action: self.conditional_intensity[action].to_dict()
                for action in ACTION_ORDER
            },
            "failure_counts": dict(sorted(self.failure_counts.items())),
        }


def _population_stats(values: Sequence[float]) -> ConditionalIntensityStats:
    if not values:
        return ConditionalIntensityStats(n=0, mean=None, variance=None)
    mean = math.fsum(values) / len(values)
    variance = math.fsum((value - mean) ** 2 for value in values) / len(values)
    return ConditionalIntensityStats(n=len(values), mean=mean, variance=variance)


def aggregate_replicates(
    observation: TeacherObservation,
    results: Sequence[TeacherReplicateResult],
) -> SoftTeacherTarget:
    """Aggregate valid labels without treating failures as hold observations."""

    if not isinstance(observation, TeacherObservation):
        raise StateValidationError("observation must be TeacherObservation")
    for result in results:
        if not isinstance(result, TeacherReplicateResult):
            raise V2ContractError("all results must be TeacherReplicateResult")
    ordered = sorted(results, key=lambda result: result.replicate_index)
    seen = set()
    for result in ordered:
        if result.family_id != observation.family_id or result.state_id != observation.state_id:
            raise V2ContractError("replicate identity does not match observation")
        if result.replicate_index in seen:
            raise V2ContractError("duplicate replicate_index")
        seen.add(result.replicate_index)

    action_intensities: Dict[str, List[float]] = {
        action: [] for action in ACTION_ORDER
    }
    failure_counts: Dict[str, int] = {}
    for result in ordered:
        if result.decision is not None:
            action_intensities[result.decision.action].append(result.decision.intensity)
        else:
            assert result.failure_code is not None
            failure_counts[result.failure_code] = (
                failure_counts.get(result.failure_code, 0) + 1
            )
    attempted_n = len(ordered)
    valid_n = sum(len(values) for values in action_intensities.values())
    failed_n = attempted_n - valid_n
    if valid_n:
        probabilities: Dict[str, Optional[float]] = {
            action: len(action_intensities[action]) / float(valid_n)
            for action in ACTION_ORDER
        }
    else:
        probabilities = {action: None for action in ACTION_ORDER}
    return SoftTeacherTarget(
        family_id=observation.family_id,
        state_id=observation.state_id,
        attempted_n=attempted_n,
        valid_n=valid_n,
        failed_n=failed_n,
        action_probabilities=probabilities,
        conditional_intensity={
            action: _population_stats(action_intensities[action])
            for action in ACTION_ORDER
        },
        failure_counts=failure_counts,
    )


def aggregate_teacher_samples(
    rows: Sequence[TeacherReplicateResult],
) -> Tuple[SoftTeacherTarget, ...]:
    """Group public replicate rows and return targets in stable identity order."""

    groups: Dict[Tuple[str, str], List[TeacherReplicateResult]] = {}
    for row in rows:
        if not isinstance(row, TeacherReplicateResult):
            raise V2ContractError("all rows must be TeacherReplicateResult")
        groups.setdefault((row.family_id, row.state_id), []).append(row)
    targets: List[SoftTeacherTarget] = []
    for family_id, state_id in sorted(groups):
        # Aggregation only needs identity, not state values.  Reuse the same
        # honest-N computation without inventing a synthetic numeric state.
        ordered = sorted(groups[(family_id, state_id)], key=lambda row: row.replicate_index)
        seen = set()
        action_intensities: Dict[str, List[float]] = {
            action: [] for action in ACTION_ORDER
        }
        failure_counts: Dict[str, int] = {}
        for row in ordered:
            if row.replicate_index in seen:
                raise V2ContractError(
                    "duplicate replicate_index for {}/{}".format(family_id, state_id)
                )
            seen.add(row.replicate_index)
            if row.decision is None:
                assert row.failure_code is not None
                failure_counts[row.failure_code] = failure_counts.get(row.failure_code, 0) + 1
            else:
                action_intensities[row.decision.action].append(row.decision.intensity)
        attempted_n = len(ordered)
        valid_n = sum(len(values) for values in action_intensities.values())
        probabilities: Dict[str, Optional[float]]
        if valid_n:
            probabilities = {
                action: len(action_intensities[action]) / float(valid_n)
                for action in ACTION_ORDER
            }
        else:
            probabilities = {action: None for action in ACTION_ORDER}
        targets.append(
            SoftTeacherTarget(
                family_id=family_id,
                state_id=state_id,
                attempted_n=attempted_n,
                valid_n=valid_n,
                failed_n=attempted_n - valid_n,
                action_probabilities=probabilities,
                conditional_intensity={
                    action: _population_stats(action_intensities[action])
                    for action in ACTION_ORDER
                },
                failure_counts=failure_counts,
            )
        )
    return tuple(targets)


__all__ = [
    "ACCOUNT_FEATURES",
    "ACTION_ORDER",
    "ANCHOR_FAMILY_ORDER",
    "CONTRACT_VERSION",
    "CONTRACT_HASH",
    "CROSS_FIELD_CONSTRAINTS",
    "ConditionalIntensityStats",
    "FAKE_NULL_TEACHER_KIND",
    "FAKE_TEST_TEACHER_KIND",
    "FEATURE_ORDER",
    "FEATURE_NAMES",
    "FEATURE_RANGES",
    "FEATURE_SEMANTICS",
    "FORBIDDEN_PROMPT_TERMS",
    "MARKET_FEATURES",
    "PROMPT_SCHEMA_VERSION",
    "ParsedTeacherResponse",
    "PublicTeacherDecision",
    "RESPONSE_SCHEMA_VERSION",
    "SOFT_TARGET_SCHEMA_VERSION",
    "STATE_SCHEMA_VERSION",
    "STATE_DESIGN_VERSION",
    "SoftTeacherTarget",
    "StateValidationError",
    "TIME_SEMANTICS",
    "TeacherObservation",
    "TeacherPrompt",
    "TeacherReplicateResult",
    "TeacherResponseError",
    "V2AttentionState",
    "V2ContractError",
    "aggregate_replicates",
    "aggregate_teacher_samples",
    "build_teacher_prompt",
    "canonical_json",
    "derive_seed",
    "contract_descriptor",
    "design_anchor_states",
    "design_latin_hypercube_states",
    "fake_null_teacher",
    "fake_provider_response",
    "fake_test_teacher",
    "make_observation",
    "generate_state_design",
    "parse_teacher_response",
    "render_teacher_prompt",
    "sha256_hex",
    "state_design_descriptor",
]
