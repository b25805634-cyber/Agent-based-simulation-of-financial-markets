"""Pure contracts for information-emphasis Teacher requests.

The scientific manipulation in this module is the *observable numeric field
set*.  No participant label, category name, or behavioural instruction is
shown to the Teacher.  Every latent state is rendered four ways with the same
account fields and the same output contract; only the allocation of twelve
observable market fields changes.

This module performs no filesystem, network, Provider, or run-context work.
The fake response helper is deterministic engineering infrastructure and is
not evidence about people or a real model endpoint.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import random
import re
from types import MappingProxyType
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple

from nmsim import v2_attention


CONTRACT_VERSION = "information-emphasis-contract/1.0.0"
LATENT_SCHEMA_VERSION = "information-emphasis-latent/1.0.0"
VIEW_SCHEMA_VERSION = "information-emphasis-view/1.0.0"
PROMPT_SCHEMA_VERSION = "limited-observation-teacher-prompt/1.0.0"
SAMPLE_SCHEMA_VERSION = "information-emphasis-sample/1.0.0"
PLAN_SCHEMA_VERSION = "information-emphasis-plan/1.0.0"
RESPONSE_SCHEMA_VERSION = v2_attention.RESPONSE_SCHEMA_VERSION
TIME_SEMANTICS = v2_attention.TIME_SEMANTICS

LATENT_STATE_COUNT = 50
PROFILE_COUNT = 4
PLAN_COUNT = LATENT_STATE_COUNT * PROFILE_COUNT
DEFAULT_DESIGN_SEED = 20260824
DEFAULT_STUDY_ID = "information-weight-pilot-v1"

P8: Tuple[str, ...] = (
    "return_1d",
    "return_5d",
    "return_20d",
    "realized_vol_20d",
    "drawdown_20d",
    "volume_z",
    "intraday_range_5d_mean",
    "turnover_change_5d",
)

F8: Tuple[str, ...] = (
    "earnings_yield_ttm",
    "revenue_growth_yoy",
    "debt_to_assets",
    "operating_margin_ttm",
    "return_on_assets_ttm",
    "operating_cashflow_to_assets",
    "book_to_market",
    "current_ratio",
)

N8: Tuple[str, ...] = (
    "signed_event_surprise",
    "affected_revenue_fraction",
    "age_20d_scaled",
    "official_source_mask",
    "confirmation_count_scaled",
    "scheduled_event_mask",
    "duration_20d_scaled",
    "source_disagreement",
)

# The existing V2 account contract contains exactly eight fields.  It remains
# fixed across all four views and is not part of the twelve-field allocation.
ACCOUNT8: Tuple[str, ...] = tuple(v2_attention.ACCOUNT_FEATURES)
if len(ACCOUNT8) != 8:  # fail closed if the imported contract ever changes
    raise RuntimeError("the imported V2 account contract is no longer eight fields")

EXTRA_FIELDS: Tuple[str, ...] = P8[6:] + F8 + N8
BINARY_FIELDS: Tuple[str, ...] = (
    "official_source_mask",
    "scheduled_event_mask",
)

PROFILE_IDS: Tuple[str, ...] = (
    "price_volume_8_2_2",
    "fundamental_2_8_2",
    "news_2_2_8",
    "balanced_4_4_4",
)

PROFILE_FIELDS: Mapping[str, Tuple[str, ...]] = MappingProxyType(
    {
        "price_volume_8_2_2": P8 + F8[:2] + N8[:2],
        "fundamental_2_8_2": P8[:2] + F8 + N8[:2],
        "news_2_2_8": P8[:2] + F8[:2] + N8,
        "balanced_4_4_4": P8[:4] + F8[:4] + N8[:4],
    }
)

# Closed domains are part of the scientific contract.  Values outside these
# domains fail; callers must not silently clip empirical inputs into them.
EXTRA_FIELD_RANGES: Mapping[str, Tuple[float, float]] = MappingProxyType(
    {
        "intraday_range_5d_mean": (0.0, 0.50),
        "turnover_change_5d": (-0.95, 5.0),
        "earnings_yield_ttm": (-1.0, 1.0),
        "revenue_growth_yoy": (-1.0, 5.0),
        "debt_to_assets": (0.0, 2.0),
        "operating_margin_ttm": (-2.0, 1.0),
        "return_on_assets_ttm": (-1.0, 1.0),
        "operating_cashflow_to_assets": (-1.0, 1.0),
        "book_to_market": (-2.0, 5.0),
        "current_ratio": (0.0, 10.0),
        "signed_event_surprise": (-1.0, 1.0),
        "affected_revenue_fraction": (0.0, 1.0),
        "age_20d_scaled": (0.0, 1.0),
        "official_source_mask": (0.0, 1.0),
        "confirmation_count_scaled": (0.0, 1.0),
        "scheduled_event_mask": (0.0, 1.0),
        "duration_20d_scaled": (0.0, 1.0),
        "source_disagreement": (0.0, 1.0),
    }
)

EXTRA_FIELD_SEMANTICS: Mapping[str, str] = MappingProxyType(
    {
        "intraday_range_5d_mean": (
            "mean over the latest five sessions of (daily high minus daily low) "
            "divided by the previous close"
        ),
        "turnover_change_5d": (
            "current share-turnover rate divided by the mean share-turnover rate "
            "over the preceding five sessions, minus 1"
        ),
        "earnings_yield_ttm": (
            "trailing-twelve-month net income divided by current market capitalization"
        ),
        "revenue_growth_yoy": (
            "trailing-twelve-month revenue divided by revenue for the preceding "
            "twelve-month period, minus 1"
        ),
        "debt_to_assets": "latest reported total liabilities divided by total assets",
        "operating_margin_ttm": (
            "trailing-twelve-month operating income divided by revenue"
        ),
        "return_on_assets_ttm": (
            "trailing-twelve-month net income divided by latest reported total assets"
        ),
        "operating_cashflow_to_assets": (
            "trailing-twelve-month operating cash flow divided by latest reported "
            "total assets"
        ),
        "book_to_market": (
            "latest reported common book equity divided by current market capitalization"
        ),
        "current_ratio": (
            "latest reported current assets divided by current liabilities"
        ),
        "signed_event_surprise": (
            "signed event surprise on a closed scale from -1 for maximally adverse "
            "to +1 for maximally favorable, with 0 neutral"
        ),
        "affected_revenue_fraction": (
            "estimated fraction of trailing revenue directly exposed to the event"
        ),
        "age_20d_scaled": (
            "daily decision intervals since first public release divided by 20 and "
            "capped at 1"
        ),
        "official_source_mask": (
            "1 when an official issuer or authority source is present, else 0"
        ),
        "confirmation_count_scaled": (
            "count of distinct confirming public sources divided by 10 and capped at 1"
        ),
        "scheduled_event_mask": (
            "1 when the event date was publicly scheduled in advance, else 0"
        ),
        "duration_20d_scaled": (
            "stated or observed event duration in daily intervals divided by 20 and "
            "capped at 1"
        ),
        "source_disagreement": (
            "degree of directional disagreement among public sources on a closed "
            "scale from 0 for none to 1 for maximal disagreement"
        ),
    }
)


def _merged_field_ranges() -> Dict[str, Tuple[float, float]]:
    result: Dict[str, Tuple[float, float]] = {}
    for name in P8[:6] + ACCOUNT8:
        result[name] = tuple(v2_attention.FEATURE_RANGES[name])
    result.update(EXTRA_FIELD_RANGES)
    return result


def _merged_field_semantics() -> Dict[str, str]:
    result: Dict[str, str] = {}
    for name in P8[:6] + ACCOUNT8:
        result[name] = v2_attention.FEATURE_SEMANTICS[name]
    result.update(EXTRA_FIELD_SEMANTICS)
    return result


FIELD_RANGES: Mapping[str, Tuple[float, float]] = MappingProxyType(
    _merged_field_ranges()
)
FIELD_SEMANTICS: Mapping[str, str] = MappingProxyType(_merged_field_semantics())

RESPONSE_FORMAT: Mapping[str, str] = MappingProxyType({"type": "json_object"})

SYSTEM_PROMPT = (
    "You produce one portfolio adjustment for one asset at a fixed daily "
    "decision point. Use only the supplied numeric market summaries and account "
    "state; do not infer external facts. Return exactly one JSON object with "
    "exactly these keys: action, intensity, reasoning. action must be buy, hold, "
    "or sell. For buy, intensity is the fraction of currently feasible cash to "
    "use. For sell, intensity is the fraction of currently held shares to "
    "release. For hold, intensity must be 0. reasoning must be a concise private "
    "diagnostic string. Do not add markdown or any text outside the JSON object."
)

# These are prompt leakage guards.  ASCII entries are matched as words;
# Chinese entries are matched as substrings.  Internal IDs may use these words,
# but no internal ID is ever projected into a request.
FORBIDDEN_PROMPT_TERMS: Tuple[str, ...] = tuple(
    dict.fromkeys(
        v2_attention.FORBIDDEN_PROMPT_TERMS
        + (
            "profile",
            "type",
            "weight",
            "price-volume trader",
            "fundamental trader",
            "news trader",
            "balanced trader",
            "量价交易者",
            "基本面交易者",
            "消息交易者",
        )
    )
)


class InformationContractError(ValueError):
    """A value violates the frozen information-emphasis contract."""


def _canonical_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InformationContractError("canonical JSON forbids non-finite floats")
        return 0.0 if value == 0.0 else value
    if isinstance(value, Mapping):
        result: Dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise InformationContractError(
                    "canonical JSON mapping keys must be strings"
                )
            result[key] = _canonical_value(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _canonical_value(to_dict())
    raise InformationContractError(
        "unsupported canonical JSON value: {}".format(type(value).__name__)
    )


def canonical_json(value: Any) -> str:
    """Return stable, compact UTF-8 JSON with no non-finite extensions."""

    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def stable_hash(value: Any) -> str:
    """Return lower-case SHA-256 over :func:`canonical_json`."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _finite_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InformationContractError("{} must be a finite number".format(name))
    result = float(value)
    if not math.isfinite(result):
        raise InformationContractError("{} must be finite".format(name))
    return 0.0 if result == 0.0 else result


def _validate_extra_fields(value: Mapping[str, Any]) -> Mapping[str, float]:
    if not isinstance(value, Mapping):
        raise InformationContractError("extra_fields must be a mapping")
    supplied = set(value)
    expected = set(EXTRA_FIELDS)
    if supplied != expected:
        raise InformationContractError(
            "extra fields must match exactly; missing={}, extra={}".format(
                sorted(expected - supplied), sorted(supplied - expected)
            )
        )
    result: Dict[str, float] = {}
    for name in EXTRA_FIELDS:
        number = _finite_number(value[name], name)
        low, high = EXTRA_FIELD_RANGES[name]
        if number < low or number > high:
            raise InformationContractError(
                "{}={} outside [{}, {}]".format(name, number, low, high)
            )
        if name in BINARY_FIELDS:
            if number not in (0.0, 1.0):
                raise InformationContractError("{} must be exactly 0 or 1".format(name))
            number = int(number)
        result[name] = number
    return MappingProxyType(result)


@dataclass(frozen=True)
class LatentState:
    """One complete state before a twelve-field observation is selected."""

    state_index: int
    base_observation: v2_attention.TeacherObservation
    extra_fields: Mapping[str, float]

    def __post_init__(self) -> None:
        if (
            isinstance(self.state_index, bool)
            or not isinstance(self.state_index, int)
            or self.state_index < 0
        ):
            raise InformationContractError("state_index must be non-negative integer")
        if not isinstance(self.base_observation, v2_attention.TeacherObservation):
            raise InformationContractError(
                "base_observation must be a V2 TeacherObservation"
            )
        object.__setattr__(
            self, "extra_fields", _validate_extra_fields(self.extra_fields)
        )

    @property
    def base_state(self) -> v2_attention.V2AttentionState:
        return self.base_observation.state

    @property
    def information_fields(self) -> Mapping[str, float]:
        values: Dict[str, float] = {
            name: float(getattr(self.base_state, name)) for name in P8[:6]
        }
        values.update(self.extra_fields)
        return MappingProxyType({name: values[name] for name in P8 + F8 + N8})

    @property
    def account_state(self) -> Mapping[str, float]:
        return MappingProxyType(
            {name: getattr(self.base_state, name) for name in ACCOUNT8}
        )

    @property
    def latent_hash(self) -> str:
        return stable_hash(
            {
                "schema_version": LATENT_SCHEMA_VERSION,
                "information_fields": self.information_fields,
                "account_state": self.account_state,
            }
        )

    @property
    def latent_id(self) -> str:
        return "latent/{:05d}-{}".format(self.state_index, self.latent_hash[:12])

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": LATENT_SCHEMA_VERSION,
            "state_index": self.state_index,
            "latent_id": self.latent_id,
            "latent_hash": self.latent_hash,
            "source_state_id": self.base_observation.state_id,
            "source_scientific_state_hash": self.base_state.scientific_state_hash,
            "information_fields": dict(self.information_fields),
            "account_state": dict(self.account_state),
        }


@dataclass(frozen=True)
class InformationView:
    """Internal observation record; only ``to_prompt_payload`` is Provider-safe."""

    latent_state: LatentState
    profile_id: str
    visible_fields: Mapping[str, float]
    account_state: Mapping[str, float]

    def __post_init__(self) -> None:
        if not isinstance(self.latent_state, LatentState):
            raise InformationContractError("latent_state must be LatentState")
        if self.profile_id not in PROFILE_IDS:
            raise InformationContractError("unknown profile_id {!r}".format(self.profile_id))
        expected_fields = PROFILE_FIELDS[self.profile_id]
        if tuple(self.visible_fields.keys()) != expected_fields:
            raise InformationContractError(
                "visible fields must have the frozen profile order"
            )
        expected_values = self.latent_state.information_fields
        visible: Dict[str, float] = {}
        for name in expected_fields:
            number = _finite_number(self.visible_fields[name], name)
            if number != float(expected_values[name]):
                raise InformationContractError(
                    "visible field {} does not match latent state".format(name)
                )
            visible[name] = number
        if tuple(self.account_state.keys()) != ACCOUNT8:
            raise InformationContractError("account fields must have frozen order")
        expected_account = self.latent_state.account_state
        account: Dict[str, float] = {}
        for name in ACCOUNT8:
            number = _finite_number(self.account_state[name], name)
            if number != float(expected_account[name]):
                raise InformationContractError(
                    "account field {} does not match latent state".format(name)
                )
            account[name] = int(number) if name in v2_attention.MASK_FIELDS else number
        object.__setattr__(self, "visible_fields", MappingProxyType(visible))
        object.__setattr__(self, "account_state", MappingProxyType(account))

    @property
    def view_hash(self) -> str:
        return stable_hash(
            {
                "schema_version": VIEW_SCHEMA_VERSION,
                "latent_hash": self.latent_state.latent_hash,
                "profile_id": self.profile_id,
                "visible_fields": self.visible_fields,
                "account_state": self.account_state,
            }
        )

    def to_prompt_payload(self) -> Dict[str, Any]:
        """Return the identity-free request projection.

        The internal allocation ID, latent identity, view hash, and unavailable
        fields are deliberately absent.
        """

        names = tuple(self.visible_fields) + ACCOUNT8
        return {
            "prompt_schema_version": PROMPT_SCHEMA_VERSION,
            "response_schema_version": RESPONSE_SCHEMA_VERSION,
            "time_semantics": TIME_SEMANTICS,
            "observable_state": dict(self.visible_fields),
            "account_state": dict(self.account_state),
            "field_semantics": {name: FIELD_SEMANTICS[name] for name in names},
            "missing_value_rule": (
                "a mask of 0 means the paired value is unavailable and its numeric "
                "field is the required 0.0 placeholder"
            ),
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": VIEW_SCHEMA_VERSION,
            "latent_id": self.latent_state.latent_id,
            "latent_hash": self.latent_state.latent_hash,
            "profile_id": self.profile_id,
            "visible_fields": dict(self.visible_fields),
            "account_state": dict(self.account_state),
            "view_hash": self.view_hash,
        }


@dataclass(frozen=True)
class InformationPrompt:
    """Exact two-message request with a content hash."""

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
        return stable_hash(
            {"schema_version": PROMPT_SCHEMA_VERSION, "messages": self.to_messages()}
        )


@dataclass(frozen=True)
class PlannedSample:
    """One of the frozen 200 requests in exact release order."""

    order_index: int
    group_index: int
    within_group_index: int
    view: InformationView
    prompt: InformationPrompt

    def __post_init__(self) -> None:
        for name in ("order_index", "group_index", "within_group_index"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise InformationContractError("{} must be non-negative integer".format(name))
        if self.within_group_index >= PROFILE_COUNT:
            raise InformationContractError("within_group_index outside frozen group")
        if not isinstance(self.view, InformationView):
            raise InformationContractError("view must be InformationView")
        if not isinstance(self.prompt, InformationPrompt):
            raise InformationContractError("prompt must be InformationPrompt")
        if self.prompt != render_teacher_prompt(self.view):
            raise InformationContractError("prompt does not match view")

    @property
    def sample_hash(self) -> str:
        return stable_hash(
            {
                "schema_version": SAMPLE_SCHEMA_VERSION,
                "order_index": self.order_index,
                "group_index": self.group_index,
                "within_group_index": self.within_group_index,
                "latent_hash": self.view.latent_state.latent_hash,
                "view_hash": self.view.view_hash,
                "prompt_hash": self.prompt.prompt_hash,
            }
        )

    @property
    def sample_id(self) -> str:
        return self.sample_hash

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": SAMPLE_SCHEMA_VERSION,
            "order_index": self.order_index,
            "group_index": self.group_index,
            "within_group_index": self.within_group_index,
            "sample_id": self.sample_id,
            "sample_hash": self.sample_hash,
            "latent_id": self.view.latent_state.latent_id,
            "latent_hash": self.view.latent_state.latent_hash,
            "profile_id": self.view.profile_id,
            "view_hash": self.view.view_hash,
            "prompt_hash": self.prompt.prompt_hash,
            "visible_fields": list(self.view.visible_fields),
        }


def _unit_interval(*parts: Any) -> float:
    seed = v2_attention.derive_seed(
        *parts, namespace="information-emphasis-lhs-unit/1"
    )
    return (seed + 0.5) / float(2 ** 64)


def _stable_permutation(size: int, *parts: Any) -> List[int]:
    return sorted(
        range(size),
        key=lambda index: (
            v2_attention.derive_seed(
                *parts, index, namespace="information-emphasis-lhs-permutation/1"
            ),
            index,
        ),
    )


def _lhs_values(size: int, low: float, high: float, *parts: Any) -> List[float]:
    ranks = _stable_permutation(size, *parts)
    return [
        low
        + ((rank + _unit_interval(*parts, row_index, "jitter")) / float(size))
        * (high - low)
        for row_index, rank in enumerate(ranks)
    ]


def generate_latent_states(
    count: int = LATENT_STATE_COUNT,
    seed: int = DEFAULT_DESIGN_SEED,
    study_id: str = DEFAULT_STUDY_ID,
) -> Tuple[LatentState, ...]:
    """Generate the frozen 50-state design without I/O.

    Existing coherent V2 states supply the first six market fields and all
    eight account fields.  Every added continuous field receives its own
    deterministic Latin hypercube.  The two binary fields are generated by an
    LHS draw followed by the frozen ``>= 0.5`` threshold.
    """

    if count != LATENT_STATE_COUNT or isinstance(count, bool):
        raise InformationContractError(
            "count must be exactly {}".format(LATENT_STATE_COUNT)
        )
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise InformationContractError("seed must be an integer")
    base = v2_attention.generate_state_design(
        count=count,
        seed=seed,
        study_id=study_id,
    )
    columns: Dict[str, List[float]] = {}
    for name in EXTRA_FIELDS:
        low, high = EXTRA_FIELD_RANGES[name]
        values = _lhs_values(count, low, high, seed, study_id, name)
        if name in BINARY_FIELDS:
            values = [1 if value >= 0.5 else 0 for value in values]
        columns[name] = values
    return tuple(
        LatentState(
            state_index=index,
            base_observation=observation,
            extra_fields={name: columns[name][index] for name in EXTRA_FIELDS},
        )
        for index, observation in enumerate(base)
    )


def render_information_view(
    latent_state: LatentState,
    profile_id: str,
) -> InformationView:
    """Select one frozen twelve-field view from a complete latent state."""

    if not isinstance(latent_state, LatentState):
        raise InformationContractError("latent_state must be LatentState")
    if profile_id not in PROFILE_IDS:
        raise InformationContractError("unknown profile_id {!r}".format(profile_id))
    all_fields = latent_state.information_fields
    return InformationView(
        latent_state=latent_state,
        profile_id=profile_id,
        visible_fields={name: all_fields[name] for name in PROFILE_FIELDS[profile_id]},
        account_state=dict(latent_state.account_state),
    )


def _prompt_violations(text: str) -> Tuple[str, ...]:
    folded = text.casefold()
    violations: List[str] = []
    for term in FORBIDDEN_PROMPT_TERMS:
        folded_term = term.casefold()
        if re.fullmatch(r"[a-z0-9 -]+", folded_term):
            pattern = r"(?<![a-z0-9]){}(?![a-z0-9])".format(
                re.escape(folded_term)
            )
            matched = re.search(pattern, folded) is not None
        else:
            matched = folded_term in folded
        if matched:
            violations.append(term)
    return tuple(violations)


def render_teacher_prompt(view: InformationView) -> InformationPrompt:
    """Render a request that contains no internal allocation or identity label."""

    if not isinstance(view, InformationView):
        raise InformationContractError("view must be InformationView")
    prompt = InformationPrompt(
        system=SYSTEM_PROMPT,
        user=canonical_json(view.to_prompt_payload()),
    )
    violations = _prompt_violations(prompt.system + "\n" + prompt.user)
    if violations:
        raise InformationContractError(
            "Teacher prompt contains forbidden terms: {}".format(list(violations))
        )
    return prompt


def build_sample_plan(
    states: Optional[Sequence[LatentState]] = None,
    *,
    seed: int = DEFAULT_DESIGN_SEED,
    study_id: str = DEFAULT_STUDY_ID,
) -> Tuple[PlannedSample, ...]:
    """Return 50 four-view groups (200 requests) in cyclic balanced order."""

    latent_states = (
        tuple(states)
        if states is not None
        else generate_latent_states(seed=seed, study_id=study_id)
    )
    if len(latent_states) != LATENT_STATE_COUNT:
        raise InformationContractError(
            "sample plan requires exactly {} latent states".format(
                LATENT_STATE_COUNT
            )
        )
    if any(not isinstance(item, LatentState) for item in latent_states):
        raise InformationContractError("states must contain only LatentState values")
    samples: List[PlannedSample] = []
    for group_index, latent_state in enumerate(latent_states):
        rotation = group_index % PROFILE_COUNT
        group_order = PROFILE_IDS[rotation:] + PROFILE_IDS[:rotation]
        for within_group_index, profile_id in enumerate(group_order):
            view = render_information_view(latent_state, profile_id)
            prompt = render_teacher_prompt(view)
            samples.append(
                PlannedSample(
                    order_index=len(samples),
                    group_index=group_index,
                    within_group_index=within_group_index,
                    view=view,
                    prompt=prompt,
                )
            )
    if len(samples) != PLAN_COUNT:
        raise AssertionError("internal sample-plan cardinality error")
    return tuple(samples)


def plan_hash(samples: Sequence[PlannedSample]) -> str:
    """Hash exact sample identities in exact release order."""

    if len(samples) != PLAN_COUNT:
        raise InformationContractError(
            "plan hash requires exactly {} samples".format(PLAN_COUNT)
        )
    if any(not isinstance(item, PlannedSample) for item in samples):
        raise InformationContractError("plan contains a non-PlannedSample value")
    for index, sample in enumerate(samples):
        if sample.order_index != index:
            raise InformationContractError("plan order_index sequence is not canonical")
    return stable_hash(
        {
            "schema_version": PLAN_SCHEMA_VERSION,
            "sample_hashes": [item.sample_hash for item in samples],
        }
    )


hash_sample_plan = plan_hash


def parse_teacher_response(
    raw_response: str,
    state_or_view: Any,
) -> v2_attention.ParsedTeacherResponse:
    """Apply the existing strict parser and feasibility checks."""

    if isinstance(state_or_view, PlannedSample):
        state = state_or_view.view.latent_state.base_state
    elif isinstance(state_or_view, InformationView):
        state = state_or_view.latent_state.base_state
    elif isinstance(state_or_view, LatentState):
        state = state_or_view.base_state
    elif isinstance(state_or_view, v2_attention.V2AttentionState):
        state = state_or_view
    else:
        raise InformationContractError(
            "state_or_view must be PlannedSample, InformationView, LatentState, "
            "or V2AttentionState"
        )
    return v2_attention.parse_teacher_response(raw_response, state)


def _fake_target_view(value: Any) -> InformationView:
    if isinstance(value, PlannedSample):
        return value.view
    if isinstance(value, InformationView):
        return value
    raise InformationContractError(
        "fake_teacher_response expects PlannedSample or InformationView"
    )


def fake_teacher_response(value: Any, replicate_index: int = 0) -> str:
    """Return deterministic feasible JSON for dry-run and contract tests only."""

    view = _fake_target_view(value)
    if (
        isinstance(replicate_index, bool)
        or not isinstance(replicate_index, int)
        or replicate_index < 0
    ):
        raise InformationContractError(
            "replicate_index must be non-negative integer"
        )
    seed = v2_attention.derive_seed(
        view.view_hash,
        replicate_index,
        namespace="information-emphasis-fake-teacher/1",
    )
    rng = random.Random(seed)
    normalized: List[float] = []
    for name, value_item in view.visible_fields.items():
        low, high = FIELD_RANGES[name]
        if name in BINARY_FIELDS:
            normalized.append(float(value_item) - 0.5)
        elif high > low:
            normalized.append((float(value_item) - low) / (high - low) - 0.5)
    signal = (sum(normalized) / float(len(normalized))) + rng.uniform(-0.18, 0.18)
    position_fraction = view.latent_state.base_state.position_fraction
    if signal > 0.06 and position_fraction < 1.0:
        action = "buy"
    elif signal < -0.06 and position_fraction > 0.0:
        action = "sell"
    else:
        action = "hold"
    intensity = (
        0.0
        if action == "hold"
        else min(1.0, max(0.05, abs(signal) + 0.10))
    )
    raw = canonical_json(
        {
            "action": action,
            "intensity": intensity,
            "reasoning": (
                "Deterministic engineering test-double response; not empirical "
                "evidence about people."
            ),
        }
    )
    # Keep the fake helper closed under the same strict feasibility boundary.
    parse_teacher_response(raw, view)
    return raw


fake_provider_response = fake_teacher_response


def contract_descriptor() -> Dict[str, Any]:
    """Return the complete frozen contract projection used for hashing."""

    return {
        "contract_version": CONTRACT_VERSION,
        "latent_schema_version": LATENT_SCHEMA_VERSION,
        "view_schema_version": VIEW_SCHEMA_VERSION,
        "prompt_schema_version": PROMPT_SCHEMA_VERSION,
        "sample_schema_version": SAMPLE_SCHEMA_VERSION,
        "plan_schema_version": PLAN_SCHEMA_VERSION,
        "response_schema_version": RESPONSE_SCHEMA_VERSION,
        "base_v2_contract_hash": v2_attention.CONTRACT_HASH,
        "base_v2_state_design": v2_attention.state_design_descriptor(),
        "time_semantics": TIME_SEMANTICS,
        "latent_state_count": LATENT_STATE_COUNT,
        "profile_count": PROFILE_COUNT,
        "plan_count": PLAN_COUNT,
        "default_design_seed": DEFAULT_DESIGN_SEED,
        "default_study_id": DEFAULT_STUDY_ID,
        "p8": list(P8),
        "f8": list(F8),
        "n8": list(N8),
        "account8": list(ACCOUNT8),
        "extra_field_ranges": {
            name: list(EXTRA_FIELD_RANGES[name]) for name in EXTRA_FIELDS
        },
        "extra_field_semantics": {
            name: EXTRA_FIELD_SEMANTICS[name] for name in EXTRA_FIELDS
        },
        "binary_field_rule": {
            "fields": list(BINARY_FIELDS),
            "sampling": "independent_deterministic_latin_hypercube",
            "threshold": ">=0.5 maps to 1; below 0.5 maps to 0",
        },
        "profiles": {
            profile_id: list(PROFILE_FIELDS[profile_id])
            for profile_id in PROFILE_IDS
        },
        "group_order_rule": "cyclic left rotation by latent group index modulo 4",
        "system_prompt": SYSTEM_PROMPT,
        "response_format": dict(RESPONSE_FORMAT),
        "forbidden_prompt_terms": list(FORBIDDEN_PROMPT_TERMS),
    }


CONTRACT_HASH = stable_hash(contract_descriptor())


def plan_summary(samples: Sequence[PlannedSample]) -> Dict[str, Any]:
    """Return a public, secret-free summary for dry-run reporting."""

    counts = {profile_id: 0 for profile_id in PROFILE_IDS}
    slot_counts = {
        str(slot): {profile_id: 0 for profile_id in PROFILE_IDS}
        for slot in range(PROFILE_COUNT)
    }
    for sample in samples:
        counts[sample.view.profile_id] += 1
        slot_counts[str(sample.within_group_index)][sample.view.profile_id] += 1
    return {
        "contract_version": CONTRACT_VERSION,
        "contract_hash": CONTRACT_HASH,
        "latent_state_count": len({item.view.latent_state.latent_hash for item in samples}),
        "sample_count": len(samples),
        "profile_counts": counts,
        "slot_counts": slot_counts,
        "plan_hash": plan_hash(samples),
        "canary_sample_id": samples[0].sample_id if samples else None,
        "fake_outputs_are_human_evidence": False,
    }


__all__ = [
    "ACCOUNT8",
    "BINARY_FIELDS",
    "CONTRACT_HASH",
    "CONTRACT_VERSION",
    "DEFAULT_DESIGN_SEED",
    "DEFAULT_STUDY_ID",
    "EXTRA_FIELDS",
    "EXTRA_FIELD_RANGES",
    "EXTRA_FIELD_SEMANTICS",
    "F8",
    "FIELD_RANGES",
    "FIELD_SEMANTICS",
    "FORBIDDEN_PROMPT_TERMS",
    "InformationContractError",
    "InformationPrompt",
    "InformationView",
    "LATENT_SCHEMA_VERSION",
    "LATENT_STATE_COUNT",
    "LatentState",
    "N8",
    "P8",
    "PLAN_COUNT",
    "PLAN_SCHEMA_VERSION",
    "PROFILE_COUNT",
    "PROFILE_FIELDS",
    "PROFILE_IDS",
    "PROMPT_SCHEMA_VERSION",
    "PlannedSample",
    "RESPONSE_FORMAT",
    "RESPONSE_SCHEMA_VERSION",
    "SAMPLE_SCHEMA_VERSION",
    "SYSTEM_PROMPT",
    "TIME_SEMANTICS",
    "VIEW_SCHEMA_VERSION",
    "build_sample_plan",
    "canonical_json",
    "contract_descriptor",
    "fake_provider_response",
    "fake_teacher_response",
    "generate_latent_states",
    "hash_sample_plan",
    "parse_teacher_response",
    "plan_hash",
    "plan_summary",
    "render_information_view",
    "render_teacher_prompt",
    "stable_hash",
]
