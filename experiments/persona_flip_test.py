"""Managed low/high flip diagnostic for the frozen persona-variable protocol.

This entrypoint is a diagnostic, not a market simulation.  It renders one
frozen persona variable at its low and high endpoint while holding the other
variables at the reference vector, then measures response distributions over
K endpoint calls per arm.  Fake and Mock providers are orchestration-only null
controls.  OpenAI-compatible transport is fail-closed behind ``--live`` and
``--dry-run`` constructs no provider.

Public artifacts contain hashes, parsed decisions, and explicitly public takes.
Rendered personas, complete prompts, raw responses, errors, and private
rationales are retained only in the mode-0600 private JSONL artifact.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import sys
import time
from typing import Any, Mapping, Optional, Sequence, TextIO

from experiments import model_qualification as qualification
from nmsim import prompts
from nmsim.config import Config
from nmsim.llm import CostTracker, MockLLM
from nmsim.managed_cli import (
    BootstrapCLIError,
    ManagedCLIError,
    RaisingArgumentParser,
    bootstrap_cli,
    fail_cli,
)
from nmsim.persona_variables import (
    EXPECTED_DELTA_DIRECTIONS,
    FIXTURE_BY_VARIABLE,
    LOW_HIGH_ENDPOINTS,
    REFERENCE_THETA,
    VARIABLE_REGISTRY,
    derive_seed,
    flip_pair,
    render,
)
from nmsim.provider_capabilities import provider_capability_snapshot
from nmsim.run_context import ManagedRunContext


OUTPUT_SCHEMA_VERSION = "1.0"
STUDY_PLAN_SCHEMA_VERSION = "1.0"
PERSONA_FIXTURE_PATH = (
    Path(__file__).resolve().parents[1] / "qualification" / "persona_fixtures.json"
)
PERSONA_PROTOCOL_PATH = (
    Path(__file__).resolve().parents[1] / "docs" / "PERSONA_VARIABLES.md"
)
DEFAULT_OUTPUT_ROOT = "persona_flip_results"
COMMAND_IDENTITY = "python -m experiments.persona_flip_test"
DEFAULT_K = 30
DEFAULT_CONCURRENCY = 8
DEFAULT_TEMPERATURE = 0.3
MAX_TOKENS = 1024
REQUEST_TIMEOUT_SECONDS = 600.0
STUDY_SEED = 20260728
BOOTSTRAP_REPLICATES = 2000
BOOTSTRAP_CONFIDENCE = 0.95
P_SELL_EFFECT_THRESHOLD = 0.15
ALLOWED_PROVIDER_IDS = frozenset({"fake_test_provider", "mock", "openai"})
PUBLIC_PROVIDER_ERROR_TYPES = frozenset(
    {
        "APIConnectionError",
        "APITimeoutError",
        "AuthenticationError",
        "BadRequestError",
        "ConflictError",
        "ConnectionError",
        "InternalServerError",
        "NotFoundError",
        "OSError",
        "PermissionDeniedError",
        "RateLimitError",
        "RuntimeError",
        "TimeoutError",
        "UnprocessableEntityError",
    }
)
PUBLIC_PROVIDER_ERROR_CODES = frozenset(
    {
        "bad_request",
        "context_length_exceeded",
        "insufficient_quota",
        "invalid_api_key",
        "invalid_request_error",
        "model_not_found",
        "rate_limit_exceeded",
        "server_error",
        "timeout",
        "unprocessable_entity",
    }
)
EXPECTED_PERSONA_FIXTURE_BUNDLE_HASH = (
    "d6feb6459b7cc38bd7e8d9b1957ef5c1d693923b82ad124929f708356147e14b"
)
EXPECTED_PERSONA_PROTOCOL_SHA256 = (
    "96b46d1b6163f708fcf74ba6c9c68b2da0cdec6f538220ce1534fb8b503daf45"
)
EXPECTED_DEFAULT_STUDY_PLAN_HASH = (
    "9ad6af3c8ecba9e53ed10f2be117cdc16f8d2b203df6ed28f2e55dc2f604c4e7"
)


class PersonaFlipError(ValueError):
    """The frozen design or requested execution mode is invalid."""


class PersonaFlipProviderGuardError(ValueError):
    """Provider execution was requested without the required guard."""


@dataclass(frozen=True)
class EndpointCompletion:
    raw_response: str
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    total_tokens: Optional[int]
    token_count_source: str
    response_id: Optional[str] = None
    reported_model: Optional[str] = None


@dataclass(frozen=True)
class FlipArm:
    arm: str
    level_id: str
    theta: Mapping[str, str]
    theta_hash: str
    rendered_persona: str
    rendered_persona_hash: str
    logical_fixture_id: str
    concrete_fixture_id: str
    fixture: Mapping[str, Any]
    baseline_overrides: Mapping[str, str]


@dataclass(frozen=True)
class FlipCase:
    variable_id: str
    group: str
    label: str
    expected_delta_direction: Optional[int]
    low: FlipArm
    high: FlipArm


@dataclass(frozen=True)
class PersonaFlipStudyPlan:
    fixture_bundle: Mapping[str, Any]
    cases: tuple[FlipCase, ...]
    public_plan: Mapping[str, Any]
    study_plan_hash: str


@dataclass(frozen=True)
class FlipRequest:
    request_id: str
    pair_id: str
    attempt_order: int
    variable_id: str
    arm: str
    level_id: str
    repeat_index: int
    arm_spec: FlipArm


class FakeNullProvider:
    """Deterministic strict null whose decision ignores all prompt contents."""

    kind = "fake_test_provider"
    model = "persona-flip-strict-null-v1"

    def __init__(self) -> None:
        self.request_count = 0
        self.response_count = 0
        self.network_access = False
        self.batch_sizes: list[int] = []
        self.current_inflight = 0
        self.max_inflight = 0

    async def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float,
        null_key: str,
    ) -> EndpointCompletion:
        del temperature
        self.request_count += 1
        self.current_inflight += 1
        self.max_inflight = max(self.max_inflight, self.current_inflight)
        try:
            await asyncio.sleep(0)
            # The paired key excludes arm, theta, rendered persona, and concrete
            # fixture.  Low/high therefore receive byte-identical fake outcomes.
            digest = hashlib.sha256(null_key.encode("utf-8")).digest()
            action = ("sell", "hold", "buy")[digest[0] % 3]
            quantity = 0 if action == "hold" else 1 + digest[1] % 5
            sentiment = {"sell": -0.5, "hold": 0.0, "buy": 0.5}[action]
            raw = json.dumps(
                {
                    "action": action,
                    "quantity": quantity,
                    "limit_price": 100.0,
                    "sentiment": sentiment,
                    "public_take": "Strict persona-flip null control.",
                    "reasoning": "private strict-null orchestration rationale",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            self.response_count += 1
            input_tokens = max(1, (len(system) + len(user)) // 4)
            output_tokens = max(1, len(raw) // 4)
            return EndpointCompletion(
                raw_response=raw,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
                token_count_source="fake_exact_test_metadata",
                response_id="fake-{}".format(
                    hashlib.sha256(
                        (null_key + "\0" + raw).encode("utf-8")
                    ).hexdigest()[:16]
                ),
                reported_model=self.model,
            )
        finally:
            self.current_inflight -= 1

    async def aclose(self) -> None:
        return None


class MockNullProvider:
    """Existing in-process MockLLM exposed explicitly as a null control."""

    kind = "mock"
    model = "nmsim-mock-null-control"

    def __init__(self, seed: int) -> None:
        self._mock = MockLLM(seed=seed)
        self.request_count = 0
        self.response_count = 0
        self.network_access = False
        self.batch_sizes: list[int] = []

    async def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float,
        null_key: str,
    ) -> EndpointCompletion:
        del temperature, null_key
        self.request_count += 1
        await asyncio.sleep(0)
        # MockLLM does not read the real-prompt persona prose.  Its mock-only
        # parameters are deliberately absent, making this an orchestration
        # control rather than evidence about persona behavior.
        raw = self._mock.complete(system, user)
        self.response_count += 1
        return EndpointCompletion(
            raw_response=raw,
            input_tokens=None,
            output_tokens=None,
            total_tokens=None,
            token_count_source="mock_usage_unavailable",
            reported_model=self.model,
        )

    async def aclose(self) -> None:
        return None


class OpenAIFlipProvider:
    """Single-attempt OpenAI-compatible transport dedicated to this diagnostic."""

    kind = "openai"

    def __init__(self, *, cfg: Config, model: str, concurrency: int) -> None:
        import httpx
        from openai import AsyncOpenAI

        base_url = os.environ.get("OPENAI_BASE_URL") or cfg.openai_base_url
        api_key = os.environ.get("OPENAI_API_KEY") or "EMPTY"
        client = httpx.AsyncClient(
            trust_env=False,
            timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS, connect=10.0),
            limits=httpx.Limits(
                max_connections=concurrency,
                max_keepalive_connections=concurrency,
            ),
        )
        self._client = AsyncOpenAI(
            base_url=base_url,
            api_key=api_key,
            http_client=client,
            max_retries=0,
        )
        self.model = model
        self.request_count = 0
        self.response_count = 0
        self.network_access = False
        self.batch_sizes: list[int] = []

    async def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float,
        null_key: str,
    ) -> EndpointCompletion:
        del null_key
        self.request_count += 1
        self.network_access = True
        response = await self._client.chat.completions.create(
            model=self.model,
            max_tokens=MAX_TOKENS,
            temperature=float(temperature),
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        raw = response.choices[0].message.content or ""
        usage = getattr(response, "usage", None)
        input_tokens = _optional_int(getattr(usage, "prompt_tokens", None))
        output_tokens = _optional_int(getattr(usage, "completion_tokens", None))
        total_tokens = _optional_int(getattr(usage, "total_tokens", None))
        if total_tokens is None and input_tokens is not None and output_tokens is not None:
            total_tokens = input_tokens + output_tokens
        self.response_count += 1
        return EndpointCompletion(
            raw_response=raw,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            token_count_source=(
                "provider_usage" if usage is not None else "provider_usage_unavailable"
            ),
            response_id=(str(response.id) if getattr(response, "id", None) else None),
            reported_model=(
                str(response.model) if getattr(response, "model", None) else None
            ),
        )

    async def aclose(self) -> None:
        await self._client.close()


def _optional_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise PersonaFlipError("persona fixture bundle must be an object")
    return value


def load_persona_fixture_bundle(
    path: Path = PERSONA_FIXTURE_PATH,
) -> dict[str, Any]:
    """Load the standalone bundle and validate its independent hash contract."""

    bundle = _read_json(path)
    if (
        bundle.get("bundle_schema_version") != "1.0"
        or bundle.get("bundle_id") != "persona_fixtures"
        or bundle.get("bundle_version") != "1.0"
    ):
        raise PersonaFlipError("persona fixture bundle identity changed")
    payload = dict(bundle)
    expected_bundle_hash = payload.pop("bundle_hash", None)
    if qualification.stable_json_hash(payload) != expected_bundle_hash:
        raise PersonaFlipError("persona fixture bundle hash mismatch")
    if expected_bundle_hash != EXPECTED_PERSONA_FIXTURE_BUNDLE_HASH:
        raise PersonaFlipError(
            "frozen persona fixture bundle changed; bump the protocol after review"
        )

    fixtures = bundle.get("fixtures")
    if not isinstance(fixtures, list) or len(fixtures) != 5:
        raise PersonaFlipError("persona fixture bundle must contain five observations")
    qualification._validate_fixture_shapes(fixtures)
    fixture_ids = [str(fixture.get("fixture_id", "")) for fixture in fixtures]
    if len(set(fixture_ids)) != 5 or "" in fixture_ids:
        raise PersonaFlipError("persona fixture ids must be non-empty and unique")
    for fixture in fixtures:
        if qualification.fixture_input_hash(fixture) != fixture.get("input_hash"):
            raise PersonaFlipError(
                "persona fixture input hash mismatch: {}".format(
                    fixture.get("fixture_id")
                )
            )

    logical = bundle.get("logical_fixtures")
    if not isinstance(logical, list) or {
        str(row.get("logical_fixture_id"))
        for row in logical
        if isinstance(row, Mapping)
    } != {
        "sideways_2y_hot_peers",
        "deep_loss_bad_news",
        "public_holding_friends_ask",
        "belief_source_pair",
    }:
        raise PersonaFlipError("persona logical fixture catalogue changed")

    references = bundle.get("fixture_references")
    if not isinstance(references, list) or len(references) != 1:
        raise PersonaFlipError("persona generic fixture reference changed")
    reference = references[0]
    if (
        reference.get("access") != "read_only"
        or reference.get("source_path") != "qualification/observations.json"
        or reference.get("fixture_id") != "negative_news_price_unchanged"
    ):
        raise PersonaFlipError("persona generic fixture must remain a read-only reference")

    qualification_bundle = qualification.load_protocol_bundle()
    generic = next(
        fixture
        for fixture in qualification_bundle["observations"]["fixtures"]
        if fixture["fixture_id"] == "negative_news_price_unchanged"
    )
    if reference.get("input_hash") != generic["input_hash"]:
        raise PersonaFlipError("generic fixture reference hash mismatch")
    return bundle


def _direction_value(value: Any) -> Optional[int]:
    if value is None or value in {"open", "direction_open"}:
        return None
    if value in (1, "+", "positive", "increase", "higher"):
        return 1
    if value in (-1, "-", "negative", "decrease", "lower"):
        return -1
    raise PersonaFlipError("unsupported expected delta direction")


def _registry_attr(spec: Any, name: str) -> str:
    if isinstance(spec, Mapping):
        return str(spec[name])
    aliases = {"group": "group_id", "label": "name"}
    return str(getattr(spec, aliases.get(name, name)))


def _arm_record(arm: FlipArm) -> dict[str, Any]:
    return {
        "arm": arm.arm,
        "level_id": arm.level_id,
        "theta_hash": arm.theta_hash,
        "rendered_persona_sha256": arm.rendered_persona_hash,
        "logical_fixture_id": arm.logical_fixture_id,
        "concrete_fixture_id": arm.concrete_fixture_id,
        "fixture_input_hash": arm.fixture["input_hash"],
        "baseline_overrides": dict(arm.baseline_overrides),
    }


def load_study_plan(
    *,
    k: int = DEFAULT_K,
    temperature: float = DEFAULT_TEMPERATURE,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> PersonaFlipStudyPlan:
    """Validate frozen inputs and bind the exact low/high request design."""

    k = int(k)
    concurrency = int(concurrency)
    temperature = float(temperature)
    if k < 1:
        raise PersonaFlipError("--k must be positive")
    if concurrency < 1:
        raise PersonaFlipError("--concurrency must be positive")
    if not math.isfinite(temperature) or temperature < 0.0:
        raise PersonaFlipError("--temperature must be finite and non-negative")
    variable_ids = tuple(VARIABLE_REGISTRY)
    if len(variable_ids) != 14 or variable_ids != tuple(LOW_HIGH_ENDPOINTS):
        raise PersonaFlipError("persona variable endpoint registry changed")
    if set(variable_ids) != set(EXPECTED_DELTA_DIRECTIONS):
        raise PersonaFlipError("persona direction registry coverage changed")
    if set(variable_ids) != set(FIXTURE_BY_VARIABLE):
        raise PersonaFlipError("persona fixture mapping coverage changed")

    fixture_bundle = load_persona_fixture_bundle()
    standalone = {
        fixture["fixture_id"]: fixture for fixture in fixture_bundle["fixtures"]
    }
    qualification_bundle = qualification.load_protocol_bundle()
    generic = next(
        fixture
        for fixture in qualification_bundle["observations"]["fixtures"]
        if fixture["fixture_id"] == "negative_news_price_unchanged"
    )
    all_fixtures = {**standalone, generic["fixture_id"]: generic}

    cases: list[FlipCase] = []
    for variable_id in variable_ids:
        low_theta, high_theta = flip_pair(variable_id)
        low_level, high_level = LOW_HIGH_ENDPOINTS[variable_id]
        logical_fixture_id = str(FIXTURE_BY_VARIABLE[variable_id])
        low_concrete = logical_fixture_id
        high_concrete = logical_fixture_id
        low_override: dict[str, str] = {}
        high_override: dict[str, str] = {}
        if variable_id == "B1":
            logical_fixture_id = "belief_source_pair"
            low_concrete = "belief_source_research"
            high_concrete = "belief_source_peers"
        elif variable_id == "B2":
            logical_fixture_id = "belief_source_pair"
            low_concrete = high_concrete = "belief_source_research"
            # A single concrete fixture in both arms avoids source-arm
            # confounding.  B1 deliberately remains the frozen Reference
            # Human value (b1_trend), because §5 permits no non-flipped theta
            # override.
        if low_concrete not in all_fixtures or high_concrete not in all_fixtures:
            raise PersonaFlipError(
                "mapped persona fixture is absent: {}".format(logical_fixture_id)
            )
        render_seed = derive_seed(STUDY_SEED, "render", variable_id)

        def make_arm(
            arm: str,
            level_id: str,
            theta: Mapping[str, str],
            concrete_id: str,
            overrides: Mapping[str, str],
        ) -> FlipArm:
            rendered = render(theta, seed=render_seed)
            return FlipArm(
                arm=arm,
                level_id=str(level_id),
                theta=dict(theta),
                theta_hash=qualification.stable_json_hash(dict(theta)),
                rendered_persona=rendered,
                rendered_persona_hash=hashlib.sha256(
                    rendered.encode("utf-8")
                ).hexdigest(),
                logical_fixture_id=logical_fixture_id,
                concrete_fixture_id=concrete_id,
                fixture=all_fixtures[concrete_id],
                baseline_overrides=dict(overrides),
            )

        spec = VARIABLE_REGISTRY[variable_id]
        cases.append(
            FlipCase(
                variable_id=variable_id,
                group=_registry_attr(spec, "group"),
                label=_registry_attr(spec, "label"),
                expected_delta_direction=_direction_value(
                    EXPECTED_DELTA_DIRECTIONS[variable_id]
                ),
                low=make_arm(
                    "low", str(low_level), low_theta, low_concrete, low_override
                ),
                high=make_arm(
                    "high", str(high_level), high_theta, high_concrete, high_override
                ),
            )
        )

    registry_snapshot = {
        variable_id: {
            "low_level_id": str(LOW_HIGH_ENDPOINTS[variable_id][0]),
            "high_level_id": str(LOW_HIGH_ENDPOINTS[variable_id][1]),
            "expected_delta_direction": _direction_value(
                EXPECTED_DELTA_DIRECTIONS[variable_id]
            ),
            "logical_fixture_id": str(FIXTURE_BY_VARIABLE[variable_id]),
        }
        for variable_id in variable_ids
    }
    persona_protocol_sha256 = hashlib.sha256(
        PERSONA_PROTOCOL_PATH.read_bytes()
    ).hexdigest()
    if persona_protocol_sha256 != EXPECTED_PERSONA_PROTOCOL_SHA256:
        raise PersonaFlipError(
            "frozen persona variable protocol changed; bump the protocol after review"
        )
    public_plan: dict[str, Any] = {
        "study_plan_schema_version": STUDY_PLAN_SCHEMA_VERSION,
        "persona_protocol_version": "1.0",
        "persona_protocol_sha256": persona_protocol_sha256,
        "persona_fixture_bundle_hash": fixture_bundle["bundle_hash"],
        "qualification_protocol_hash": qualification_bundle["protocol_hash"],
        "qualification_fixture_set_hash": qualification_bundle["fixture_set_hash"],
        "reference_theta_hash": qualification.stable_json_hash(dict(REFERENCE_THETA)),
        "registry_snapshot_hash": qualification.stable_json_hash(registry_snapshot),
        "variable_count": len(cases),
        "variables": [
            {
                "variable_id": case.variable_id,
                "group": case.group,
                "label": case.label,
                "expected_delta_direction": case.expected_delta_direction,
                "direction_open": case.expected_delta_direction is None,
                "low": _arm_record(case.low),
                "high": _arm_record(case.high),
            }
            for case in cases
        ],
        "reference_policy": "all_non_flipped_variables_use_reference_theta",
        "b2_fixture_conditioning": {
            "logical_fixture_id": "belief_source_pair",
            "concrete_fixture_id_both_arms": "belief_source_research",
            "baseline_override_both_arms": {},
            "reason": "avoid_source_arm_confounding_while_preserving_reference_theta",
            "known_tension": (
                "the_concrete_fixture_mentions_research_while_reference_B1_is_trend"
            ),
        },
        "known_prompt_fixture_tensions": [
            {
                "variable_id": "A3",
                "arm": "low",
                "level_id": "a3_0",
                "concrete_fixture_id": "deep_loss_bad_news",
                "tension_code": "render_flat_but_fixture_deep_loss",
            },
            {
                "variable_id": "C3",
                "arm": "low",
                "level_id": "c3_private",
                "concrete_fixture_id": "public_holding_friends_ask",
                "tension_code": "render_private_but_fixture_public_recommendation",
            },
        ],
        "k_per_arm": k,
        "temperature": temperature,
        "concurrency": concurrency,
        "planned_requests": len(cases) * 2 * k,
        "bootstrap": {
            "method": "independent_arm_percentile",
            "replicates": BOOTSTRAP_REPLICATES,
            "confidence": BOOTSTRAP_CONFIDENCE,
            "seed_derivation": "integer_sha256_derive_seed",
        },
        "effectiveness_rule": {
            "primary_metric": "delta_p_sell_high_minus_low",
            "absolute_threshold": P_SELL_EFFECT_THRESHOLD,
            "ci_must_exclude_zero": True,
            "direction_must_match_preregistered_prediction": True,
            "A1_direction_exception": "sign_open_but_magnitude_and_ci_rules_unchanged",
            "classification_eligibility": (
                "both_arms_parse_valid_n_must_equal_planned_k"
            ),
            "incomplete_honest_n_result": "not_evaluable",
        },
        "metric_contract": {
            "delta": "high_arm_mean_minus_low_arm_mean",
            "p_sell": "sell decisions divided by parse-valid decisions",
            "sentiment": "mean parsed sentiment",
            "signed_order": "buy=+quantity, sell=-quantity, hold=0",
            "parse_failure_policy": (
                "retained_in_endpoint_response_honest_n_and_excluded_from_metrics"
            ),
        },
        "fake_mock_interpretation": (
            "orchestration_null_controls_only_not_real_endpoint_behavior"
        ),
        "is_market_simulation": False,
        "study_seed": STUDY_SEED,
    }
    study_plan_hash = qualification.stable_json_hash(public_plan)
    if (
        k == DEFAULT_K
        and temperature == DEFAULT_TEMPERATURE
        and concurrency == DEFAULT_CONCURRENCY
        and study_plan_hash != EXPECTED_DEFAULT_STUDY_PLAN_HASH
    ):
        raise PersonaFlipError(
            "frozen default persona flip study plan changed; bump the schema after review"
        )
    return PersonaFlipStudyPlan(
        fixture_bundle=fixture_bundle,
        cases=tuple(cases),
        public_plan=public_plan,
        study_plan_hash=study_plan_hash,
    )


def _request_id(payload: Mapping[str, Any]) -> str:
    return "persona-flip-{}".format(qualification.stable_json_hash(payload)[:24])


def build_requests(plan: PersonaFlipStudyPlan) -> list[FlipRequest]:
    requests: list[FlipRequest] = []
    order = 0
    k = int(plan.public_plan["k_per_arm"])
    for case in plan.cases:
        for repeat_index in range(k):
            pair_identity = {
                "study_plan_hash": plan.study_plan_hash,
                "variable_id": case.variable_id,
                "repeat_index": repeat_index,
            }
            pair_id = "persona-pair-{}".format(
                qualification.stable_json_hash(pair_identity)[:24]
            )
            for arm_name, arm_spec in (("low", case.low), ("high", case.high)):
                identity = {**pair_identity, "arm": arm_name}
                requests.append(
                    FlipRequest(
                        request_id=_request_id(identity),
                        pair_id=pair_id,
                        attempt_order=order,
                        variable_id=case.variable_id,
                        arm=arm_name,
                        level_id=arm_spec.level_id,
                        repeat_index=repeat_index,
                        arm_spec=arm_spec,
                    )
                )
                order += 1
    return requests


def _build_prompt(arm: FlipArm) -> tuple[str, str]:
    fixture = arm.fixture
    state = fixture["market_state"]
    social_feed = [
        (float(item["sentiment"]), str(item["public_take"]))
        for item in fixture["visible_social_feed"]
    ]
    return (
        prompts.build_system({"persona": arm.rendered_persona}),
        prompts.build_user(
            round_i=int(fixture["round"]),
            price=float(state["latest_price"]),
            recent=list(state["recent_prices"]),
            news=fixture["visible_news"],
            social_feed=social_feed or None,
            shares=int(fixture["shares"]),
            cash=float(fixture["cash"]),
            memory=list(fixture["memory"]),
        ),
    )


def _raw_provider_error_code(error: BaseException) -> Any:
    """Extract an adapter error code without deciding whether it is public-safe."""

    code = getattr(error, "code", None)
    if code is None and isinstance(getattr(error, "body", None), Mapping):
        body = error.body
        code = body.get("code")
        if code is None and isinstance(body.get("error"), Mapping):
            code = body["error"].get("code")
    return code


def _safe_provider_error(error: BaseException) -> dict[str, Any]:
    status_code = _optional_int(getattr(error, "status_code", None))
    raw_type = type(error).__name__
    raw_code = _raw_provider_error_code(error)
    code = str(raw_code) if raw_code is not None else None
    code_is_safe = bool(
        code is not None
        and re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", code)
        and code in PUBLIC_PROVIDER_ERROR_CODES
    )
    return {
        "provider_error_type": (
            raw_type if raw_type in PUBLIC_PROVIDER_ERROR_TYPES else "ProviderError"
        ),
        "provider_status_code": status_code,
        "provider_error_code": code if code_is_safe else None,
        "provider_error_code_redacted": bool(code is not None and not code_is_safe),
    }


def _case_for_request(request: FlipRequest) -> qualification.QualificationCase:
    identity = {
        "variable_id": request.variable_id,
        "arm": request.arm,
        "level_id": request.level_id,
        "theta_hash": request.arm_spec.theta_hash,
        "fixture_input_hash": request.arm_spec.fixture["input_hash"],
    }
    return qualification.QualificationCase(
        case_id="persona-case-{}".format(
            qualification.stable_json_hash(identity)[:24]
        ),
        request_order=request.attempt_order,
        persona_id="persona_variable_{}".format(request.variable_id.lower()),
        fixture_id=request.arm_spec.concrete_fixture_id,
        fixture=request.arm_spec.fixture,
    )


def _public_request_fields(request: FlipRequest) -> dict[str, Any]:
    arm = request.arm_spec
    return {
        "sample_schema_version": OUTPUT_SCHEMA_VERSION,
        "sample_id": request.request_id,
        "pair_id": request.pair_id,
        "attempt_order": request.attempt_order,
        "variable_id": request.variable_id,
        "arm": request.arm,
        "level_id": request.level_id,
        "repeat_index": request.repeat_index,
        "theta_hash": arm.theta_hash,
        "rendered_persona_sha256": arm.rendered_persona_hash,
        "logical_fixture_id": arm.logical_fixture_id,
        "concrete_fixture_id": arm.concrete_fixture_id,
        "fixture_input_hash": arm.fixture["input_hash"],
    }


async def _execute_one(
    manager: ManagedRunContext,
    provider: Any,
    tracker: CostTracker,
    request: FlipRequest,
    prompt: tuple[str, str],
    *,
    temperature: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    system, user = prompt
    base = _public_request_fields(request)
    prompt_hash = hashlib.sha256((system + "\0" + user).encode("utf-8")).hexdigest()
    manager.events.emit(
        "LLMRequestRecorded",
        agent_id="persona_variable_{}".format(request.variable_id.lower()),
        data={
            "request_id": request.request_id,
            "variable_id": request.variable_id,
            "arm": request.arm,
            "prompt_hash": prompt_hash,
            "run_kind": "persona_flip_test",
        },
        private_data={"system_prompt": system, "user_prompt": user},
    )
    started = time.perf_counter()
    try:
        completion = await provider.complete(
            system,
            user,
            temperature=temperature,
            null_key=request.pair_id,
        )
    except Exception as error:
        latency_ms = round((time.perf_counter() - started) * 1000.0, 3)
        safe_error = _safe_provider_error(error)
        public = {
            **base,
            "status": "transport_failed",
            "prompt_sha256": prompt_hash,
            "raw_response_sha256": None,
            "response_id_sha256": None,
            "reported_model": None,
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "token_count_source": "unavailable_transport_failure",
            "latency_ms": latency_ms,
            "parse_failed": None,
            "validation_failed": None,
            "action": None,
            "sentiment": None,
            "order": None,
            "public_take": None,
            **safe_error,
        }
        private = {
            **public,
            "theta": dict(request.arm_spec.theta),
            "rendered_persona": request.arm_spec.rendered_persona,
            "system_prompt": system,
            "user_prompt": user,
            "provider_error_detail": str(error),
            "provider_error_type_private": type(error).__name__,
            "provider_error_code_private": (
                str(_raw_provider_error_code(error))
                if _raw_provider_error_code(error) is not None
                else None
            ),
        }
        manager.events.emit(
            "PersonaFlipProbeFailed",
            agent_id="persona_variable_{}".format(request.variable_id.lower()),
            data={"request_id": request.request_id, **safe_error},
            private_data={
                "provider_error_detail": str(error),
                "provider_error_type_private": type(error).__name__,
                "provider_error_code_private": (
                    str(_raw_provider_error_code(error))
                    if _raw_provider_error_code(error) is not None
                    else None
                ),
            },
        )
        return public, private

    latency_ms = round((time.perf_counter() - started) * 1000.0, 3)
    raw = completion.raw_response
    raw_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    response_id_hash = (
        hashlib.sha256(completion.response_id.encode("utf-8")).hexdigest()
        if completion.response_id
        else None
    )
    if completion.input_tokens is not None and completion.output_tokens is not None:
        tracker.add(
            getattr(provider, "model", completion.reported_model or "unknown"),
            completion.input_tokens,
            completion.output_tokens,
        )
    manager.events.emit(
        "LLMResponseRecorded",
        agent_id="persona_variable_{}".format(request.variable_id.lower()),
        data={
            "request_id": request.request_id,
            "variable_id": request.variable_id,
            "arm": request.arm,
            "source": "provider",
            "response_hash": raw_hash,
            "input_tokens": completion.input_tokens,
            "output_tokens": completion.output_tokens,
            "total_tokens": completion.total_tokens,
        },
        private_data={"raw_response": raw},
    )
    evaluated, private_evaluation = qualification.evaluate_response(
        _case_for_request(request), raw
    )
    action = str(evaluated["action"])
    quantity = int(evaluated["quantity"])
    signed_quantity = (
        quantity if action == "buy" else -quantity if action == "sell" else 0
    )
    parse_failed = not bool(evaluated["parse_success"])
    public = {
        **base,
        "status": "completed",
        "prompt_sha256": prompt_hash,
        "raw_response_sha256": raw_hash,
        "response_id_sha256": response_id_hash,
        "reported_model": completion.reported_model,
        "input_tokens": completion.input_tokens,
        "output_tokens": completion.output_tokens,
        "total_tokens": completion.total_tokens,
        "token_count_source": completion.token_count_source,
        "latency_ms": latency_ms,
        "parse_failed": parse_failed,
        "validation_failed": bool(evaluated["validation_failure"]),
        "action": action,
        "sentiment": float(evaluated["sentiment"]),
        "order": {
            "side": action,
            "quantity": quantity,
            "signed_quantity": signed_quantity,
            "limit_price": float(evaluated["limit_price"]),
        },
        "public_take": str(evaluated["public_take"]),
        "provider_error_type": None,
        "provider_status_code": None,
        "provider_error_code": None,
        "provider_error_code_redacted": False,
    }
    private = {
        **public,
        "theta": dict(request.arm_spec.theta),
        "rendered_persona": request.arm_spec.rendered_persona,
        "system_prompt": system,
        "user_prompt": user,
        "raw_response": raw,
        "private_rationale": private_evaluation["parsed_decision"]["rationale"],
        "parsed_decision_private": dict(private_evaluation["parsed_decision"]),
    }
    manager.events.emit(
        "AgentDecisionParsed",
        agent_id="persona_variable_{}".format(request.variable_id.lower()),
        data={
            "request_id": request.request_id,
            "variable_id": request.variable_id,
            "arm": request.arm,
            "parse_status": "error" if parse_failed else "ok",
            "action": action,
            "quantity": quantity,
            "sentiment": public["sentiment"],
            "public_take": public["public_take"],
        },
        private_data={
            "private_rationale": private_evaluation["parsed_decision"]["rationale"]
        },
    )
    return public, private


async def _execute_block(
    manager: ManagedRunContext,
    provider: Any,
    tracker: CostTracker,
    requests: Sequence[FlipRequest],
    prompts_by_arm: Mapping[tuple[str, str], tuple[str, str]],
    *,
    temperature: float,
    concurrency: int,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    semaphore = asyncio.Semaphore(concurrency)

    async def bounded(request: FlipRequest):
        async with semaphore:
            return await _execute_one(
                manager,
                provider,
                tracker,
                request,
                prompts_by_arm[(request.variable_id, request.arm)],
                temperature=temperature,
            )

    provider.batch_sizes.append(len(requests))
    return list(await asyncio.gather(*(bounded(request) for request in requests)))


def _percentile(sorted_values: Sequence[float], probability: float) -> float:
    if not sorted_values:
        raise PersonaFlipError("percentile requires at least one value")
    position = (len(sorted_values) - 1) * float(probability)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return float(sorted_values[lower])
    weight = position - lower
    return (
        float(sorted_values[lower]) * (1.0 - weight)
        + float(sorted_values[upper]) * weight
    )


def bootstrap_delta_ci(
    low_values: Sequence[float],
    high_values: Sequence[float],
    *,
    seed: int,
    replicates: int = BOOTSTRAP_REPLICATES,
    confidence: float = BOOTSTRAP_CONFIDENCE,
) -> Optional[dict[str, Any]]:
    """Independent-arm deterministic percentile CI for high minus low."""

    low = [float(value) for value in low_values]
    high = [float(value) for value in high_values]
    if not low or not high:
        return None
    if replicates < 1 or not 0.0 < confidence < 1.0:
        raise PersonaFlipError("invalid bootstrap settings")
    if not all(math.isfinite(value) for value in low + high):
        raise PersonaFlipError("bootstrap values must be finite")
    rng = random.Random(int(seed))
    draws: list[float] = []
    for _ in range(int(replicates)):
        low_mean = math.fsum(rng.choice(low) for _ in range(len(low))) / len(low)
        high_mean = math.fsum(rng.choice(high) for _ in range(len(high))) / len(high)
        draws.append(high_mean - low_mean)
    draws.sort()
    tail = (1.0 - confidence) / 2.0
    return {
        "method": "independent_arm_percentile",
        "replicates": int(replicates),
        "confidence": float(confidence),
        "seed": int(seed),
        "low": round(_percentile(draws, tail), 12),
        "high": round(_percentile(draws, 1.0 - tail), 12),
    }


def _mean(values: Sequence[float]) -> Optional[float]:
    return math.fsum(values) / len(values) if values else None


def _metric_displacement(
    variable_id: str,
    metric: str,
    low_values: Sequence[float],
    high_values: Sequence[float],
) -> dict[str, Any]:
    low_mean = _mean(low_values)
    high_mean = _mean(high_values)
    delta = (
        None if low_mean is None or high_mean is None else high_mean - low_mean
    )
    ci = bootstrap_delta_ci(
        low_values,
        high_values,
        seed=derive_seed(STUDY_SEED, "bootstrap", variable_id, metric),
    )
    return {
        "low_mean": None if low_mean is None else round(low_mean, 12),
        "high_mean": None if high_mean is None else round(high_mean, 12),
        "delta_high_minus_low": None if delta is None else round(delta, 12),
        "bootstrap_ci_95": ci,
    }


def aggregate_results(
    rows: Sequence[Mapping[str, Any]],
    plan: PersonaFlipStudyPlan,
) -> dict[str, Any]:
    """Aggregate parse-valid response distributions with honest denominators."""

    results: list[dict[str, Any]] = []
    k = int(plan.public_plan["k_per_arm"])
    for case in plan.cases:
        arm_rows: dict[str, list[Mapping[str, Any]]] = {}
        parsed_rows: dict[str, list[Mapping[str, Any]]] = {}
        for arm in ("low", "high"):
            selected = [
                row
                for row in rows
                if row.get("variable_id") == case.variable_id
                and row.get("arm") == arm
            ]
            arm_rows[arm] = selected
            parsed_rows[arm] = [
                row
                for row in selected
                if row.get("status") == "completed"
                and row.get("parse_failed") is False
            ]

        def values(arm: str, metric: str) -> list[float]:
            if metric == "p_sell":
                return [
                    1.0 if row["action"] == "sell" else 0.0
                    for row in parsed_rows[arm]
                ]
            if metric == "sentiment":
                return [float(row["sentiment"]) for row in parsed_rows[arm]]
            return [float(row["order"]["signed_quantity"]) for row in parsed_rows[arm]]

        displacements = {
            metric: _metric_displacement(
                case.variable_id,
                metric,
                values("low", metric),
                values("high", metric),
            )
            for metric in ("p_sell", "sentiment", "signed_order")
        }
        primary = displacements["p_sell"]
        delta = primary["delta_high_minus_low"]
        ci = primary["bootstrap_ci_95"]
        magnitude_met = delta is not None and abs(float(delta)) >= P_SELL_EFFECT_THRESHOLD
        ci_excludes_zero = bool(
            ci is not None and (float(ci["low"]) > 0.0 or float(ci["high"]) < 0.0)
        )
        expected = case.expected_delta_direction
        direction_matches = bool(
            delta is not None
            and (
                expected is None
                or (expected > 0 and float(delta) > 0.0)
                or (expected < 0 and float(delta) < 0.0)
            )
        )
        counts = {}
        for arm in ("low", "high"):
            completed = [
                row for row in arm_rows[arm] if row.get("status") == "completed"
            ]
            counts[arm] = {
                "planned": k,
                "attempted": len(arm_rows[arm]),
                "endpoint_responses": len(completed),
                "parse_valid": len(parsed_rows[arm]),
                "transport_failures": len(arm_rows[arm]) - len(completed),
                "parse_failures": len(completed) - len(parsed_rows[arm]),
                "skipped": max(0, k - len(arm_rows[arm])),
            }
        classification_eligible = all(
            counts[arm]["parse_valid"] == k for arm in ("low", "high")
        )
        effective = bool(
            classification_eligible
            and magnitude_met
            and ci_excludes_zero
            and direction_matches
        )
        if not classification_eligible:
            preclassification = "not_evaluable"
            preclassification_zh = "不可判定"
        elif effective:
            preclassification = "effective"
            preclassification_zh = "有效"
        else:
            preclassification = "idle_candidate"
            preclassification_zh = "空转候选"
        results.append(
            {
                "variable_id": case.variable_id,
                "group": case.group,
                "label": case.label,
                "low_level_id": case.low.level_id,
                "high_level_id": case.high.level_id,
                "logical_fixture_id": case.low.logical_fixture_id,
                "low_concrete_fixture_id": case.low.concrete_fixture_id,
                "high_concrete_fixture_id": case.high.concrete_fixture_id,
                "honest_n_by_arm": counts,
                "displacements": displacements,
                "effectiveness": {
                    "threshold": P_SELL_EFFECT_THRESHOLD,
                    "expected_delta_direction": expected,
                    "direction_open": expected is None,
                    "magnitude_threshold_met": magnitude_met,
                    "ci_excludes_zero": ci_excludes_zero,
                    "direction_matches": direction_matches,
                    "classification_eligible": classification_eligible,
                    "classification_eligibility_rule": (
                        "both_arms_parse_valid_n_equals_planned_k"
                    ),
                    "preclassification": preclassification,
                    "preclassification_zh": preclassification_zh,
                },
            }
        )
    endpoint_responses = sum(
        row.get("status") == "completed" for row in rows
    )
    parsed_decisions = sum(
        row.get("status") == "completed" and row.get("parse_failed") is False
        for row in rows
    )
    return {
        "honest_n_endpoint_responses": endpoint_responses,
        "honest_n_parsed_decisions": parsed_decisions,
        "parse_failure_count": endpoint_responses - parsed_decisions,
        "variable_results": results,
    }


def _write_json_exclusive(path: Path, payload: Any, mode: int = 0o644) -> None:
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            json.dump(payload, stream, indent=2, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    os.chmod(path, mode)


def _open_jsonl_exclusive(path: Path, mode: int) -> TextIO:
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        stream = os.fdopen(descriptor, "w", encoding="utf-8")
        descriptor = -1
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    os.chmod(path, mode)
    return stream


def _append_jsonl(stream: TextIO, rows: Sequence[Mapping[str, Any]]) -> None:
    for row in rows:
        stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    stream.flush()
    os.fsync(stream.fileno())


def _completion_summary(
    rows: Sequence[Mapping[str, Any]], *, planned: int
) -> dict[str, int | str]:
    completed = [row for row in rows if row.get("status") == "completed"]
    parsed = [row for row in completed if row.get("parse_failed") is False]
    return {
        "unit": "endpoint_requests",
        "planned": planned,
        "attempted": len(rows),
        "completed": len(completed),
        "endpoint_responses": len(completed),
        "parse_valid": len(parsed),
        "transport_failures": len(rows) - len(completed),
        "parse_failures": len(completed) - len(parsed),
        "skipped": max(0, planned - len(rows)),
    }


def _adapter_capability(
    provider_id: str, *, endpoint: Optional[str], concurrency: int
) -> dict[str, Any]:
    base = provider_capability_snapshot(
        provider_id,
        endpoint=endpoint if provider_id == "openai" else None,
    )
    adapter = {
        "adapter_id": "persona_flip_{}_v1".format(provider_id),
        "provider_id": provider_id,
        "implementation_scope": "persona_flip_diagnostic",
        "external_network_expected": provider_id == "openai",
        "provider_retry_count": 0,
        "application_concurrency_limit": concurrency,
        "deterministic_claim": (
            "strict_paired_null_control"
            if provider_id == "fake_test_provider"
            else "orchestration_null_control"
            if provider_id == "mock"
            else "none"
        ),
    }
    snapshot = {
        "capability_schema_version": "1.0",
        "capability_scope": "persona_flip_dedicated_adapter",
        "adapter": adapter,
        "endpoint_identity": base["endpoint_identity"],
        "provider_registry_snapshot": base["provider"],
    }
    snapshot["capability_snapshot_sha256"] = qualification.stable_json_hash(snapshot)
    return snapshot


def _validate_execution_guard(args: argparse.Namespace) -> None:
    if args.provider not in ALLOWED_PROVIDER_IDS:
        raise PersonaFlipProviderGuardError("unreviewed persona-flip Provider")
    if args.dry_run:
        return
    if args.provider == "openai" and not args.live:
        raise PersonaFlipProviderGuardError(
            "OpenAI-compatible persona-flip execution requires explicit --live"
        )
    if args.provider != "openai" and args.live:
        raise PersonaFlipProviderGuardError(
            "--live is reserved for the OpenAI-compatible endpoint"
        )


def _resolved_model(provider_id: str, requested_model: str) -> str:
    if provider_id == "fake_test_provider":
        return FakeNullProvider.model
    if provider_id == "mock":
        return MockNullProvider.model
    return (
        str(requested_model or "").strip()
        or str(os.environ.get("LLM_MODEL") or "").strip()
        or Config().openai_model
    )


def _build_provider(
    provider_id: str, *, cfg: Config, model: str, concurrency: int
) -> Any:
    if provider_id == "fake_test_provider":
        return FakeNullProvider()
    if provider_id == "mock":
        return MockNullProvider(seed=STUDY_SEED)
    if provider_id == "openai":
        return OpenAIFlipProvider(cfg=cfg, model=model, concurrency=concurrency)
    raise PersonaFlipProviderGuardError("unreviewed persona-flip Provider")


def _initialise_manifest(
    manager: ManagedRunContext,
    *,
    args: argparse.Namespace,
    plan: PersonaFlipStudyPlan,
    model: str,
    capability: Mapping[str, Any],
) -> None:
    planned = int(plan.public_plan["planned_requests"])
    completion = manager.manifest["completion"]
    completion["simulation_runs"].update(
        {"planned": 0, "started": 0, "completed": 0, "failed": 0}
    )
    completion["rounds"].update(
        {"planned": 0, "started": 0, "completed": 0, "failed": 0, "skipped": 0}
    )
    completion["agent_decisions"].update(
        {
            "planned": planned,
            "attempted": 0,
            "completed": 0,
            "failed": 0,
            "skipped": planned,
        }
    )
    completion["llm_logical_requests"].update(
        {"planned": planned, "attempted": 0, "completed": 0, "failed": 0}
    )
    manager.manifest["persona_flip_test"] = {
        "output_schema_version": OUTPUT_SCHEMA_VERSION,
        "study_plan_schema_version": STUDY_PLAN_SCHEMA_VERSION,
        "study_plan_hash": plan.study_plan_hash,
        "study_plan": dict(plan.public_plan),
        "provider_requested": args.provider,
        "provider_resolved": args.provider,
        "model_requested": args.model or None,
        "model_resolved": model,
        "provider_capability_snapshot": dict(capability),
        "dry_run": bool(args.dry_run),
        "live": bool(args.live),
        "network_access": False,
        "endpoint_responses": _completion_summary([], planned=planned),
        "honest_n_endpoint_responses": 0,
        "honest_n_parsed_decisions": 0,
        "honest_n_runs": 0,
        "is_simulation_run": False,
    }
    manager.manifest["llm"]["provider_capability_snapshot"] = dict(capability)
    manager.manifest["honest_n_endpoint_responses"] = 0
    manager.manifest["honest_n_parsed_decisions"] = 0
    manager.manifest["honest_n_runs"] = 0
    manager._write()


def _checkpoint(
    manager: ManagedRunContext,
    *,
    provider: Any,
    tracker: CostTracker,
    plan: PersonaFlipStudyPlan,
    rows: Sequence[Mapping[str, Any]],
) -> None:
    completion = _completion_summary(
        rows, planned=int(plan.public_plan["planned_requests"])
    )
    manager.network_access = bool(getattr(provider, "network_access", False))
    manager.sync_llm_accounting(provider, tracker)
    manifest = manager.manifest["persona_flip_test"]
    manifest["network_access"] = manager.network_access
    manifest["endpoint_responses"] = dict(completion)
    manifest["honest_n_endpoint_responses"] = completion["endpoint_responses"]
    manifest["honest_n_parsed_decisions"] = completion["parse_valid"]
    manager.manifest["honest_n_endpoint_responses"] = completion["endpoint_responses"]
    manager.manifest["honest_n_parsed_decisions"] = completion["parse_valid"]
    manager.manifest["honest_n_runs"] = 0
    manager._write()


async def _run_requests(
    manager: ManagedRunContext,
    provider: Any,
    tracker: CostTracker,
    plan: PersonaFlipStudyPlan,
    public_stream: TextIO,
    private_stream: TextIO,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    prompts_by_arm = {
        (case.variable_id, arm.arm): _build_prompt(arm)
        for case in plan.cases
        for arm in (case.low, case.high)
    }
    requests = build_requests(plan)
    public_rows: list[dict[str, Any]] = []
    private_rows: list[dict[str, Any]] = []
    for case in plan.cases:
        block = [
            request for request in requests if request.variable_id == case.variable_id
        ]
        results = await _execute_block(
            manager,
            provider,
            tracker,
            block,
            prompts_by_arm,
            temperature=float(plan.public_plan["temperature"]),
            concurrency=int(plan.public_plan["concurrency"]),
        )
        block_public = [result[0] for result in results]
        block_private = [result[1] for result in results]
        public_rows.extend(block_public)
        private_rows.extend(block_private)
        _append_jsonl(private_stream, block_private)
        _append_jsonl(public_stream, block_public)
        _checkpoint(
            manager,
            provider=provider,
            tracker=tracker,
            plan=plan,
            rows=public_rows,
        )
        print(
            "persona flip variable={} completed={}/{}".format(
                case.variable_id,
                sum(row["status"] == "completed" for row in block_public),
                len(block_public),
            ),
            file=sys.stderr,
            flush=True,
        )
    public_rows.sort(key=lambda row: int(row["attempt_order"]))
    private_rows.sort(key=lambda row: int(row["attempt_order"]))
    return public_rows, private_rows


def build_summary(
    manager: ManagedRunContext,
    *,
    args: argparse.Namespace,
    plan: PersonaFlipStudyPlan,
    model: str,
    provider: Any,
    public_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    planned = int(plan.public_plan["planned_requests"])
    completion = _completion_summary(public_rows, planned=planned)
    aggregate = aggregate_results(public_rows, plan)
    return {
        "output_schema_version": OUTPUT_SCHEMA_VERSION,
        "run_id": manager.run_id,
        "run_kind": "persona_flip_test",
        "study_plan_hash": plan.study_plan_hash,
        "study_plan": dict(plan.public_plan),
        "provider": args.provider,
        "model_requested": args.model or None,
        "model_resolved": model,
        "live": bool(args.live),
        "dry_run": False,
        "network_access": bool(getattr(provider, "network_access", False)),
        "completion": completion,
        "honest_n_endpoint_responses": aggregate["honest_n_endpoint_responses"],
        "honest_n_parsed_decisions": aggregate["honest_n_parsed_decisions"],
        "honest_n_runs": 0,
        "parse_failure_count": aggregate["parse_failure_count"],
        "variable_results": aggregate["variable_results"],
        "interpretation": (
            "fake/mock results are orchestration null controls and are not "
            "evidence of real endpoint persona behavior"
        ),
        "artifacts": {
            "public_samples": "persona_flip_samples.jsonl",
            "private_records": "private_persona_flip_records.jsonl",
        },
    }


def _dry_run_summary(
    manager: ManagedRunContext,
    *,
    args: argparse.Namespace,
    plan: PersonaFlipStudyPlan,
    model: str,
) -> dict[str, Any]:
    return {
        "output_schema_version": OUTPUT_SCHEMA_VERSION,
        "run_id": manager.run_id,
        "run_kind": "persona_flip_test",
        "study_plan_hash": plan.study_plan_hash,
        "study_plan": dict(plan.public_plan),
        "provider": args.provider,
        "model_requested": args.model or None,
        "model_resolved": model,
        "live": False,
        "dry_run": True,
        "network_access": False,
        "provider_calls": 0,
        "planned_requests": plan.public_plan["planned_requests"],
        "honest_n_endpoint_responses": 0,
        "honest_n_parsed_decisions": 0,
        "honest_n_runs": 0,
    }


def run_study(
    manager: ManagedRunContext,
    *,
    args: argparse.Namespace,
    cfg: Config,
    plan: PersonaFlipStudyPlan,
    model: str,
) -> dict[str, Any]:
    provider = _build_provider(
        args.provider,
        cfg=cfg,
        model=model,
        concurrency=int(args.concurrency),
    )
    tracker = CostTracker()
    manager.active_llm = provider
    manager.tracker = tracker
    manager.llm_mode = "record"
    manager.register_llm_runtime(
        provider=args.provider,
        model=model,
        mode="persona_flip_test",
        cache_enabled=False,
        network_access=False,
        provider_calls=0,
        application_concurrency_limit=int(args.concurrency),
        provider_connection_limit=(
            int(args.concurrency) if args.provider == "openai" else 0
        ),
        temperature_grid=[float(args.temperature)],
        provider_retry_count=0,
        live=bool(args.live),
    )
    public_stream = _open_jsonl_exclusive(
        manager.run_dir / "persona_flip_samples.jsonl", 0o644
    )
    try:
        private_stream = _open_jsonl_exclusive(
            manager.run_dir / "private_persona_flip_records.jsonl", 0o600
        )
    except BaseException:
        public_stream.close()
        raise
    try:
        async def execute_and_close():
            try:
                return await _run_requests(
                    manager,
                    provider,
                    tracker,
                    plan,
                    public_stream,
                    private_stream,
                )
            finally:
                await provider.aclose()

        public_rows, private_rows = asyncio.run(execute_and_close())
    finally:
        private_stream.close()
        public_stream.close()

    manager.network_access = bool(getattr(provider, "network_access", False))
    manager.sync_llm_accounting(provider, tracker)
    summary = build_summary(
        manager,
        args=args,
        plan=plan,
        model=model,
        provider=provider,
        public_rows=public_rows,
    )
    manifest = manager.manifest["persona_flip_test"]
    manifest["network_access"] = summary["network_access"]
    manifest["endpoint_responses"] = dict(summary["completion"])
    manifest["honest_n_endpoint_responses"] = summary[
        "honest_n_endpoint_responses"
    ]
    manifest["honest_n_parsed_decisions"] = summary[
        "honest_n_parsed_decisions"
    ]
    manager.manifest["honest_n_endpoint_responses"] = summary[
        "honest_n_endpoint_responses"
    ]
    manager.manifest["honest_n_parsed_decisions"] = summary[
        "honest_n_parsed_decisions"
    ]
    manager.manifest["honest_n_runs"] = 0
    manager._write()
    return {
        "summary": summary,
        "public_rows": public_rows,
        "private_rows": private_rows,
    }


def build_argparser() -> argparse.ArgumentParser:
    parser = RaisingArgumentParser(allow_abbrev=False)
    parser.add_argument("--version", action="version", version="persona-flip-test 1.0")
    parser.add_argument(
        "--provider",
        choices=sorted(ALLOWED_PROVIDER_IDS),
        default="fake_test_provider",
    )
    parser.add_argument("--model", default="")
    parser.add_argument("--k", type=int, default=DEFAULT_K)
    parser.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE)
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--out", default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--run-id", default=None)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> None:
    args_list = list(sys.argv[1:] if argv is None else argv)
    try:
        bootstrap = bootstrap_cli(
            args_list,
            default_out=DEFAULT_OUTPUT_ROOT,
            command_identity=COMMAND_IDENTITY,
        )
    except BootstrapCLIError as error:
        print(
            "provenance_not_created_reason={}".format(type(error).__name__),
            file=sys.stderr,
        )
        raise SystemExit(2)

    try:
        args = build_argparser().parse_args(args_list)
        args.provider = str(args.provider).strip().lower()
        if args.dry_run and args.live:
            raise PersonaFlipError("--dry-run and --live are mutually exclusive")
        plan = load_study_plan(
            k=args.k,
            temperature=args.temperature,
            concurrency=args.concurrency,
        )
        model = _resolved_model(args.provider, args.model)
    except (ManagedCLIError, OSError, PersonaFlipError, ValueError) as error:
        fail_cli(bootstrap, error, failure_stage="config_validation")

    cfg = Config(
        provider=args.provider,
        model=model,
        seed=STUDY_SEED,
        n_llm_agents=14,
        n_noise_agents=0,
        temperature=float(args.temperature),
        max_tokens=MAX_TOKENS,
        cache_enabled=False,
        out_dir=args.out,
    )
    manager = ManagedRunContext.create(
        cfg,
        out_root=args.out,
        scenario_id="persona-flip:{}".format(STUDY_PLAN_SCHEMA_VERSION),
        run_id=args.run_id,
        worker_count=int(args.concurrency),
        batching={
            "k_per_arm": int(args.k),
            "arm_count": 2,
            "variable_count": len(plan.cases),
            "concurrency": int(args.concurrency),
        },
        input_paths={
            "persona_variables_protocol": PERSONA_PROTOCOL_PATH,
            "persona_fixtures": PERSONA_FIXTURE_PATH,
            "qualification_observations_read_only_reference": (
                qualification.OBSERVATIONS_PATH
            ),
        },
        command_identity=COMMAND_IDENTITY,
        run_kind="persona_flip_test",
        planned_simulation_runs=0,
    )
    try:
        with manager:
            manager.set_stage("provider_setup")
            capability = _adapter_capability(
                args.provider,
                endpoint=(
                    os.environ.get("OPENAI_BASE_URL") or cfg.openai_base_url
                    if args.provider == "openai"
                    else None
                ),
                concurrency=int(args.concurrency),
            )
            _initialise_manifest(
                manager,
                args=args,
                plan=plan,
                model=model,
                capability=capability,
            )
            _validate_execution_guard(args)
            manager.register_llm_runtime(
                provider=args.provider,
                model=model,
                mode=(
                    "persona_flip_test_dry_run"
                    if args.dry_run
                    else "persona_flip_test"
                ),
                cache_enabled=False,
                network_access=False,
                provider_calls=0,
                live=bool(args.live),
            )
            if args.dry_run:
                manager.set_stage("result_export")
                summary = _dry_run_summary(
                    manager,
                    args=args,
                    plan=plan,
                    model=model,
                )
                _write_json_exclusive(manager.run_dir / "dry_run_summary.json", summary)
                manager.finish()
                print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
                return

            payload = run_study(
                manager,
                args=args,
                cfg=cfg,
                plan=plan,
                model=model,
            )
            manager.set_stage("result_export")
            _write_json_exclusive(
                manager.run_dir / "persona_flip_summary.json",
                payload["summary"],
            )
            manager.finish()
            print(json.dumps(payload["summary"], ensure_ascii=False, sort_keys=True))
    except PersonaFlipProviderGuardError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2)
    except Exception as error:
        if args.provider == "openai":
            print(
                "persona flip run failed: {}".format(type(error).__name__),
                file=sys.stderr,
            )
            raise SystemExit(1) from None
        raise


if __name__ == "__main__":
    main()


__all__ = [
    "ALLOWED_PROVIDER_IDS",
    "BOOTSTRAP_CONFIDENCE",
    "BOOTSTRAP_REPLICATES",
    "DEFAULT_CONCURRENCY",
    "DEFAULT_K",
    "DEFAULT_TEMPERATURE",
    "EXPECTED_DEFAULT_STUDY_PLAN_HASH",
    "EXPECTED_PERSONA_FIXTURE_BUNDLE_HASH",
    "EXPECTED_PERSONA_PROTOCOL_SHA256",
    "FakeNullProvider",
    "FlipArm",
    "FlipCase",
    "FlipRequest",
    "MockNullProvider",
    "OUTPUT_SCHEMA_VERSION",
    "P_SELL_EFFECT_THRESHOLD",
    "PersonaFlipError",
    "PersonaFlipProviderGuardError",
    "PersonaFlipStudyPlan",
    "aggregate_results",
    "bootstrap_delta_ci",
    "build_argparser",
    "build_requests",
    "build_summary",
    "load_persona_fixture_bundle",
    "load_study_plan",
    "main",
    "run_study",
]
