"""Managed measurement of OpenAI-compatible endpoint stochasticity.

The study uses six frozen persona/observation cases from the version-controlled
model-qualification matrix.  It measures repeated response variation without
running the market, changing an Agent, or feeding any response into the social
channel.  Real endpoint access is fail-closed behind ``--live``; ``--dry-run``
does not import or construct an endpoint Provider.

Raw responses and Prompts are retained only in the managed 0600 private
artifact.  Public rows contain hashes, explicitly public ``public_take`` text,
and parsed numeric/order fields, never private rationale.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time
from typing import Any, Mapping, Optional, Sequence, TextIO

from experiments import model_qualification as qualification
from nmsim.config import Config
from nmsim.llm import CostTracker
from nmsim.managed_cli import (
    BootstrapCLIError,
    ManagedCLIError,
    RaisingArgumentParser,
    bootstrap_cli,
    fail_cli,
)
from nmsim.provider_capabilities import provider_capability_snapshot
from nmsim.run_context import ManagedRunContext


OUTPUT_SCHEMA_VERSION = "1.0"
STUDY_PLAN_SCHEMA_VERSION = "1.0"
SELECTION_SCHEMA_VERSION = "1.0"
TEMPERATURES = (0.0, 0.3)
CONCURRENCY_LEVELS = (1, 8, 32)
REPEATS = 30
MAX_TOKENS = 1024
REQUEST_TIMEOUT_SECONDS = 600.0
STUDY_SEED = 20260722
SEED_PROBE_VALUE = 2026072203
SEED_PROBE_TEMPERATURE = 0.3
ALLOWED_PROVIDER_IDS = frozenset({"fake_test_provider", "openai"})
DEFAULT_OUTPUT_ROOT = "endpoint_stochasticity_results"
COMMAND_IDENTITY = "python -m experiments.endpoint_stochasticity"

# Six time-balanced waves keep every temperature/concurrency condition in each
# execution phase and rotate every condition through every within-wave
# position exactly once.  The first wave uses six repeats (36 requests per
# condition) so the concurrency=32 arm actually reaches its declared client
# limit; later waves complete the frozen K=30 without changing any denominator.
BLOCK_CONDITIONS = (
    (0.0, 1),
    (0.3, 8),
    (0.0, 32),
    (0.3, 1),
    (0.0, 8),
    (0.3, 32),
)
BLOCK_REPEAT_SPANS = (
    (0, 6),
    (6, 11),
    (11, 16),
    (16, 21),
    (21, 26),
    (26, 30),
)

# One case per production persona.  The observation choices cover news, price,
# social conflict/panic, a neutral placebo, and a deep discount.  The role
# labels are design metadata only; they never change the existing Persona.
SELECTED_CASE_SPECS: tuple[tuple[str, str, str], ...] = (
    ("retail_crowd", "negative_news_price_unchanged", "fuel"),
    ("fomo_momentum", "price_crash_no_news", "fuel"),
    ("value_institution", "deep_discount_to_fundamental", "dampener"),
    ("quant_arb", "neutral_placebo_news", "dampener"),
    ("influencer_amplifier", "conflicting_neighbor_views", "spark"),
    ("contrarian_fund", "unanimous_neighbor_panic", "dampener"),
)
EXPECTED_SELECTED_CASE_IDS = (
    "qcase-cc0dec3634c3c8bd87d254f8",
    "qcase-0587fd0f734b430d3ef77b41",
    "qcase-b1760dde034a888e00e388cb",
    "qcase-d05d85bfa6ecc87c74905e43",
    "qcase-ddaedf5354a0d12a03ba066b",
    "qcase-a5a5109b01a620d9dbc25f51",
)
EXPECTED_QUALIFICATION_SELECTION_HASH = (
    "db008c386d6eb5a9dccdd91d4c8e978c22523a969857187b1e3e56327488223c"
)
EXPECTED_STUDY_PLAN_HASH = (
    "d45392b4741a97006987325287cb76f9c1a450ecdb46cc0a220a65a2c69c993d"
)
SEED_PROBE_PERSONA_ID = "influencer_amplifier"
SEED_PROBE_FIXTURE_ID = "conflicting_neighbor_views"


class EndpointStudyError(ValueError):
    """The frozen design or a requested execution mode is invalid."""


class EndpointProviderGuardError(ValueError):
    """A Provider execution was requested without the required guard."""


@dataclass(frozen=True)
class EndpointStudyPlan:
    """Validated source cases plus the public, hash-bound study plan."""

    bundle: Mapping[str, Any]
    all_cases: tuple[qualification.QualificationCase, ...]
    selected_cases: tuple["SelectedEndpointCase", ...]
    public_plan: Mapping[str, Any]
    study_plan_hash: str


@dataclass(frozen=True)
class SelectedEndpointCase:
    """A qualification case plus its endpoint-study mechanism label."""

    case_id: str
    request_order: int
    persona_id: str
    fixture_id: str
    fixture: Mapping[str, Any]
    mechanism_role: str


@dataclass(frozen=True)
class EndpointCompletion:
    """One endpoint response with per-request usage evidence."""

    raw_response: str
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    total_tokens: Optional[int]
    token_count_source: str
    response_id: Optional[str] = None
    reported_model: Optional[str] = None


@dataclass(frozen=True)
class ProbeRequest:
    request_id: str
    attempt_order: int
    sample_kind: str
    case: qualification.QualificationCase
    temperature: float
    concurrency: int
    replicate: Optional[int]
    seed: Optional[int]
    seed_probe_index: Optional[int]
    schedule_block_index: Optional[int]
    wave_index: Optional[int]
    within_wave_position: Optional[int]


class FakeEndpointProvider:
    """Deterministic, asynchronous, no-I/O control for the complete grid."""

    kind = "fake_test_provider"
    model = "endpoint-stochasticity-fake-v1"

    def __init__(self, kind: str = "fake_test_provider") -> None:
        self.kind = kind
        self.request_count = 0
        self.response_count = 0
        self.network_access = False
        self.batch_sizes: list[int] = []
        self.calls: list[dict[str, Any]] = []
        self.current_inflight = 0
        self.max_inflight = 0

    async def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float,
        seed: Optional[int] = None,
    ) -> EndpointCompletion:
        self.request_count += 1
        self.current_inflight += 1
        self.max_inflight = max(self.max_inflight, self.current_inflight)
        self.calls.append(
            {"temperature": float(temperature), "seed": seed}
        )
        try:
            # Yield once so the bounded executor's concurrency path is exercised.
            await asyncio.sleep(0)
            identity = json.dumps(
                {
                    "system": system,
                    "user": user,
                    "temperature": float(temperature).hex(),
                    "seed": seed,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            digest = hashlib.sha256(identity.encode("utf-8")).digest()
            action = ("sell", "hold", "buy")[digest[0] % 3]
            quantity = 0 if action == "hold" else 1 + digest[1] % 5
            sentiment = {
                "sell": -0.5,
                "hold": 0.0,
                "buy": 0.5,
            }[action]
            raw = json.dumps(
                {
                    "action": action,
                    "quantity": quantity,
                    "limit_price": 100.0 + (digest[2] % 5),
                    "sentiment": sentiment,
                    "public_take": "Deterministic endpoint control response.",
                    "reasoning": "private deterministic endpoint-control rationale",
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
                    hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
                ),
                reported_model=self.model,
            )
        finally:
            self.current_inflight -= 1

    async def aclose(self) -> None:
        return None


class OpenAIEndpointProvider:
    """Single-attempt OpenAI-compatible diagnostic transport.

    This deliberately does not use :class:`nmsim.llm.OpenAILLM`: that
    production adapter retries parse failures and turns transport errors into
    fallback decisions, both of which would bias a noise-floor measurement.
    """

    kind = "openai"

    def __init__(self, *, cfg: Config, model: str) -> None:
        import httpx
        from openai import AsyncOpenAI

        base_url = os.environ.get("OPENAI_BASE_URL") or cfg.openai_base_url
        # EMPTY is the existing non-secret sentinel for endpoints with no auth.
        # Any real credential can enter only through the environment.
        api_key = os.environ.get("OPENAI_API_KEY") or "EMPTY"
        limit = max(CONCURRENCY_LEVELS)
        client = httpx.AsyncClient(
            trust_env=False,
            timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS, connect=10.0),
            limits=httpx.Limits(
                max_connections=limit,
                max_keepalive_connections=limit,
            ),
        )
        self._client = AsyncOpenAI(
            base_url=base_url,
            api_key=api_key,
            http_client=client,
            max_retries=0,
        )
        self.model = model
        self.max_tokens = MAX_TOKENS
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
        seed: Optional[int] = None,
    ) -> EndpointCompletion:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": float(temperature),
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if seed is not None:
            kwargs["seed"] = int(seed)
        self.request_count += 1
        self.network_access = True
        response = await self._client.chat.completions.create(**kwargs)
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
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _case_record(case: Any) -> dict[str, Any]:
    record = {
        "case_id": case.case_id,
        "request_order": case.request_order,
        "persona_id": case.persona_id,
        "fixture_id": case.fixture_id,
        "fixture_input_hash": case.fixture["input_hash"],
    }
    mechanism_role = getattr(case, "mechanism_role", None)
    if mechanism_role is not None:
        record["mechanism_role"] = str(mechanism_role)
    return record


def _block_schedule() -> list[dict[str, Any]]:
    """Return the frozen time-balanced execution schedule."""

    schedule: list[dict[str, Any]] = []
    condition_count = len(BLOCK_CONDITIONS)
    for wave_index, (repeat_start, repeat_stop) in enumerate(BLOCK_REPEAT_SPANS):
        for position in range(condition_count):
            temperature, concurrency = BLOCK_CONDITIONS[
                (wave_index + position) % condition_count
            ]
            schedule.append(
                {
                    "wave_index": wave_index,
                    "within_wave_position": position,
                    "repeat_start_inclusive": repeat_start,
                    "repeat_stop_exclusive": repeat_stop,
                    "temperature": temperature,
                    "concurrency_level": concurrency,
                }
            )
    return schedule


def _validated_block_schedule() -> list[dict[str, Any]]:
    schedule = _block_schedule()
    expected_conditions = {
        (temperature, concurrency)
        for temperature in TEMPERATURES
        for concurrency in CONCURRENCY_LEVELS
    }
    expected_positions = set(range(len(expected_conditions)))
    if len(schedule) != len(BLOCK_REPEAT_SPANS) * len(expected_conditions):
        raise EndpointStudyError("endpoint block schedule length changed")
    for wave_index in range(len(BLOCK_REPEAT_SPANS)):
        wave = [row for row in schedule if row["wave_index"] == wave_index]
        conditions = {
            (row["temperature"], row["concurrency_level"]) for row in wave
        }
        if conditions != expected_conditions:
            raise EndpointStudyError("endpoint block schedule condition coverage changed")
    for temperature, concurrency in expected_conditions:
        condition_rows = [
            row
            for row in schedule
            if row["temperature"] == temperature
            and row["concurrency_level"] == concurrency
        ]
        spans = sorted(
            (
                row["repeat_start_inclusive"],
                row["repeat_stop_exclusive"],
            )
            for row in condition_rows
        )
        positions = {row["within_wave_position"] for row in condition_rows}
        if spans != sorted(BLOCK_REPEAT_SPANS) or positions != expected_positions:
            raise EndpointStudyError("endpoint block schedule balance changed")
    return schedule


def _endpoint_adapter_capability_snapshot(
    provider_id: str,
    *,
    endpoint: Optional[str],
) -> dict[str, Any]:
    """Describe this diagnostic adapter, not a similarly named production one."""

    if provider_id == "fake_test_provider":
        adapter = {
            "adapter_id": "endpoint_stochasticity_fake_v1",
            "provider_id": provider_id,
            "implementation_scope": "endpoint_stochasticity_null_control",
            "transport_type": "in_process_async_test_double",
            "external_network_expected": False,
            "authentication_mode": "none",
            "supports_async": True,
            "supports_temperature_request": True,
            "temperature_behavior": "included_in_deterministic_response_identity",
            "sends_seed_request_field": True,
            "seed_semantics": "included_in_deterministic_response_identity",
            "supports_usage_metadata": True,
            "usage_metadata_behavior": "fake_exact_test_metadata",
            "supports_provider_response_id": True,
            "provider_response_id_behavior": "locally_generated_fake_identifier",
            "supports_cache": False,
            "supports_record_replay": False,
            "provider_retry_count": 0,
            "application_concurrency_limit": max(CONCURRENCY_LEVELS),
            "provider_connection_limit": 0,
            "http_trust_env": None,
            "deterministic_claim": "deterministic_for_identical_fake_inputs",
        }
        endpoint_identity = provider_capability_snapshot(
            "fake_test_provider"
        )["endpoint_identity"]
    elif provider_id == "openai":
        adapter = {
            "adapter_id": "endpoint_stochasticity_openai_compatible_v1",
            "provider_id": provider_id,
            "implementation_scope": "endpoint_stochasticity_dedicated_probe",
            "transport_type": "openai_compatible_async_sdk_http",
            "external_network_expected": True,
            "authentication_mode": "environment_only",
            "supports_async": True,
            "supports_temperature_request": True,
            "temperature_behavior": "sent_on_every_request",
            "sends_seed_request_field": True,
            "seed_semantics": "unknown_and_empirically_probed_not_assumed",
            "supports_usage_metadata": True,
            "usage_metadata_behavior": "provider_optional_without_local_estimate",
            "supports_provider_response_id": True,
            "provider_response_id_behavior": "optional_and_publicly_hashed",
            "supports_cache": False,
            "supports_record_replay": False,
            "provider_retry_count": 0,
            "application_concurrency_limit": max(CONCURRENCY_LEVELS),
            "provider_connection_limit": max(CONCURRENCY_LEVELS),
            "http_trust_env": False,
            "deterministic_claim": "none",
        }
        endpoint_identity = provider_capability_snapshot(
            "openai", endpoint=endpoint
        )["endpoint_identity"]
    else:
        raise EndpointProviderGuardError("unreviewed endpoint Provider")

    snapshot: dict[str, Any] = {
        "capability_schema_version": "1.0",
        "capability_scope": "endpoint_stochasticity_dedicated_adapter",
        "adapter": adapter,
        "endpoint_identity": endpoint_identity,
    }
    snapshot["capability_snapshot_sha256"] = qualification.stable_json_hash(
        snapshot
    )
    return snapshot


def load_study_plan() -> EndpointStudyPlan:
    """Load, validate and hash the frozen 48-to-6 design."""

    bundle = qualification.load_protocol_bundle()
    all_cases = tuple(qualification.build_cases(bundle))
    by_pair = {(case.persona_id, case.fixture_id): case for case in all_cases}
    source_selected: list[qualification.QualificationCase] = []
    role_by_case: dict[str, str] = {}
    for persona_id, fixture_id, role in SELECTED_CASE_SPECS:
        case = by_pair.get((persona_id, fixture_id))
        if case is None:
            raise EndpointStudyError(
                "frozen endpoint case is absent from qualification protocol"
            )
        source_selected.append(case)
        role_by_case[case.case_id] = role

    source_selected.sort(key=lambda case: case.request_order)
    actual_ids = tuple(case.case_id for case in source_selected)
    if actual_ids != EXPECTED_SELECTED_CASE_IDS:
        raise EndpointStudyError(
            "frozen endpoint case identities changed; review the study design"
        )
    selection = qualification.select_cases(
        bundle,
        all_cases,
        provider_id="fake_test_provider",
        case_ids=actual_ids,
        max_cases=len(actual_ids),
    )
    if selection.metadata["selection_hash"] != EXPECTED_QUALIFICATION_SELECTION_HASH:
        raise EndpointStudyError(
            "qualification selection identity changed; review the study design"
        )

    selected = [
        SelectedEndpointCase(
            case_id=case.case_id,
            request_order=case.request_order,
            persona_id=case.persona_id,
            fixture_id=case.fixture_id,
            fixture=case.fixture,
            mechanism_role=role_by_case[case.case_id],
        )
        for case in source_selected
    ]
    selected_records = [_case_record(case) for case in selected]
    role_coverage = dict(
        sorted(Counter(case.mechanism_role for case in selected).items())
    )
    seed_case = next(
        case
        for case in selected
        if case.persona_id == SEED_PROBE_PERSONA_ID
        and case.fixture_id == SEED_PROBE_FIXTURE_ID
    )
    planned_grid = len(selected) * len(TEMPERATURES) * len(CONCURRENCY_LEVELS) * REPEATS
    block_schedule = _validated_block_schedule()
    public_plan: dict[str, Any] = {
        "study_plan_schema_version": STUDY_PLAN_SCHEMA_VERSION,
        "selection_schema_version": SELECTION_SCHEMA_VERSION,
        "source_protocol_version": bundle["protocol"]["protocol_version"],
        "protocol_hash": bundle["protocol_hash"],
        "fixture_set_hash": bundle["fixture_set_hash"],
        "rubric_hash": bundle["rubric_hash"],
        "visibility_contract_hash": bundle["visibility_contract_hash"],
        "source_case_count": len(all_cases),
        "source_cases": [_case_record(case) for case in all_cases],
        "selection_hash": selection.metadata["selection_hash"],
        "selected_case_count": len(selected),
        "selected_cases": selected_records,
        "role_coverage": role_coverage,
        "required_role_coverage": ["fuel", "dampener", "spark"],
        "observed_role_coverage": sorted(set(role_by_case.values())),
        "temperatures": list(TEMPERATURES),
        "repeats": REPEATS,
        "concurrency_levels": list(CONCURRENCY_LEVELS),
        "block_schedule_design": (
            "six_time_balanced_waves_with_cyclic_condition_rotation"
        ),
        "block_schedule": block_schedule,
        "prompt_variant": "real_agent_prompt_v1",
        "max_tokens": MAX_TOKENS,
        "request_timeout_seconds": REQUEST_TIMEOUT_SECONDS,
        "provider_retry_count": 0,
        "planned_grid_requests": planned_grid,
        "seed_probe": {
            "case_id": seed_case.case_id,
            "persona_id": seed_case.persona_id,
            "fixture_id": seed_case.fixture_id,
            "temperature": SEED_PROBE_TEMPERATURE,
            "concurrency_level": 1,
            "seed": SEED_PROBE_VALUE,
            "request_count": 2,
            "interpretation": "two_call_acceptance_and_equality_probe_not_determinism_proof",
        },
        "planned_seed_probe_requests": 2,
        "planned_total_requests": planned_grid + 2,
        "metric_contract": {
            "byte_identity": "pairwise equality pooled only within identical-case cells",
            "sentiment_sigma": "pooled within-case sample standard deviation",
            "order_scalar": "signed quantity: buy=+quantity, sell=-quantity, hold=0",
            "order_sigma": "pooled within-case signed-order sample standard deviation",
            "parse_failure_policy": (
                "retained in response/byte honest-N; excluded from parsed sigma"
            ),
        },
        "study_seed": STUDY_SEED,
        "is_market_simulation": False,
    }
    study_plan_hash = qualification.stable_json_hash(public_plan)
    if study_plan_hash != EXPECTED_STUDY_PLAN_HASH:
        raise EndpointStudyError(
            "frozen endpoint study plan changed; bump the schema after review"
        )
    return EndpointStudyPlan(
        bundle=bundle,
        all_cases=all_cases,
        selected_cases=tuple(selected),
        public_plan=public_plan,
        study_plan_hash=study_plan_hash,
    )


def _request_id(payload: Mapping[str, Any]) -> str:
    return "endpoint-sample-{}".format(
        qualification.stable_json_hash(payload)[:24]
    )


def build_grid_requests(plan: EndpointStudyPlan) -> list[ProbeRequest]:
    requests: list[ProbeRequest] = []
    order = 0
    for block_index, block_spec in enumerate(plan.public_plan["block_schedule"]):
        temperature = float(block_spec["temperature"])
        concurrency = int(block_spec["concurrency_level"])
        repeat_start = int(block_spec["repeat_start_inclusive"])
        repeat_stop = int(block_spec["repeat_stop_exclusive"])
        for replicate in range(repeat_start, repeat_stop):
            for case in plan.selected_cases:
                identity = {
                    "study_plan_hash": plan.study_plan_hash,
                    "sample_kind": "grid",
                    "case_id": case.case_id,
                    "temperature": float(temperature).hex(),
                    "concurrency": concurrency,
                    "replicate": replicate,
                }
                requests.append(
                    ProbeRequest(
                        request_id=_request_id(identity),
                        attempt_order=order,
                        sample_kind="grid",
                        case=case,
                        temperature=temperature,
                        concurrency=concurrency,
                        replicate=replicate,
                        seed=None,
                        seed_probe_index=None,
                        schedule_block_index=block_index,
                        wave_index=int(block_spec["wave_index"]),
                        within_wave_position=int(
                            block_spec["within_wave_position"]
                        ),
                    )
                )
                order += 1
    return requests


def build_seed_probe_requests(
    plan: EndpointStudyPlan, *, start_order: int
) -> list[ProbeRequest]:
    case = next(
        case
        for case in plan.selected_cases
        if case.persona_id == SEED_PROBE_PERSONA_ID
        and case.fixture_id == SEED_PROBE_FIXTURE_ID
    )
    requests = []
    for index in range(2):
        identity = {
            "study_plan_hash": plan.study_plan_hash,
            "sample_kind": "seed_probe",
            "case_id": case.case_id,
            "temperature": float(SEED_PROBE_TEMPERATURE).hex(),
            "seed": SEED_PROBE_VALUE,
            "seed_probe_index": index,
        }
        requests.append(
            ProbeRequest(
                request_id=_request_id(identity),
                attempt_order=start_order + index,
                sample_kind="seed_probe",
                case=case,
                temperature=SEED_PROBE_TEMPERATURE,
                concurrency=1,
                replicate=None,
                seed=SEED_PROBE_VALUE,
                seed_probe_index=index,
                schedule_block_index=None,
                wave_index=None,
                within_wave_position=None,
            )
        )
    return requests


def _build_provider(provider_id: str, *, cfg: Config, model: str):
    if provider_id == "fake_test_provider":
        return FakeEndpointProvider(kind=provider_id)
    if provider_id == "openai":
        return OpenAIEndpointProvider(cfg=cfg, model=model)
    raise EndpointProviderGuardError("unreviewed endpoint Provider")


def _validate_execution_guard(args: argparse.Namespace) -> None:
    if args.provider not in ALLOWED_PROVIDER_IDS:
        raise EndpointProviderGuardError("unreviewed endpoint Provider")
    if args.dry_run:
        return
    if args.provider == "openai" and not args.live:
        raise EndpointProviderGuardError(
            "OpenAI-compatible endpoint execution requires explicit --live"
        )
    if args.provider != "openai" and args.live:
        raise EndpointProviderGuardError(
            "--live is reserved for the OpenAI-compatible endpoint"
        )


def _resolved_model(provider_id: str, requested_model: str) -> str:
    if provider_id == "fake_test_provider":
        return FakeEndpointProvider.model
    return (
        str(requested_model or "").strip()
        or str(os.environ.get("LLM_MODEL") or "").strip()
        or Config().openai_model
    )


def _initialise_manifest(
    manager: ManagedRunContext,
    *,
    args: argparse.Namespace,
    plan: EndpointStudyPlan,
    model: str,
    capability: Mapping[str, Any],
) -> None:
    planned = int(plan.public_plan["planned_total_requests"])
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
    manager.manifest["endpoint_stochasticity"] = {
        "output_schema_version": OUTPUT_SCHEMA_VERSION,
        "study_plan_schema_version": STUDY_PLAN_SCHEMA_VERSION,
        "study_plan_hash": plan.study_plan_hash,
        "study_plan": dict(plan.public_plan),
        "request_identity_contract": {
            "authoritative_grid_identity": "study_plan_hash plus per-sample identity",
            "managed_config_temperature_scope": (
                "bootstrap baseline only; the multi-temperature request grid is "
                "bound by study_plan_hash"
            ),
            "model_request_config_hash_is_not_standalone_study_identity": True,
        },
        "provider_requested": args.provider,
        "provider_resolved": args.provider,
        "model_requested": args.model or None,
        "model_resolved": model,
        "provider_capability_snapshot": dict(capability),
        "dry_run": bool(args.dry_run),
        "live": bool(args.live),
        "network_access": False,
        "endpoint_responses": {
            "unit": "endpoint_responses",
            "planned": planned,
            "attempted": 0,
            "completed": 0,
            "failed": 0,
            "skipped": planned,
        },
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


def _public_request_fields(request: ProbeRequest) -> dict[str, Any]:
    fields = {
        "sample_schema_version": OUTPUT_SCHEMA_VERSION,
        "sample_id": request.request_id,
        "attempt_order": request.attempt_order,
        "measurement_kind": request.sample_kind,
        "case_id": request.case.case_id,
        "persona_id": request.case.persona_id,
        "fixture_id": request.case.fixture_id,
        "fixture_input_hash": request.case.fixture["input_hash"],
        "mechanism_role": request.case.mechanism_role,
        "temperature": request.temperature,
        "concurrency_level": request.concurrency,
        "repeat_index": request.replicate,
        "schedule_block_index": request.schedule_block_index,
        "wave_index": request.wave_index,
        "within_wave_position": request.within_wave_position,
        "seed_parameter_sent": request.seed is not None,
        "seed_probe_index": request.seed_probe_index,
    }
    if request.seed is not None:
        fields["seed"] = request.seed
    return fields


def _safe_provider_error(error: BaseException) -> dict[str, Any]:
    status_code = _optional_int(getattr(error, "status_code", None))
    code = getattr(error, "code", None)
    if code is None:
        body = getattr(error, "body", None)
        if isinstance(body, Mapping):
            code = body.get("code")
            nested = body.get("error")
            if code is None and isinstance(nested, Mapping):
                code = nested.get("code")
    return {
        "provider_error_type": type(error).__name__,
        "provider_status_code": status_code,
        "provider_error_code": str(code) if code is not None else None,
    }


async def _execute_one(
    manager: ManagedRunContext,
    provider: Any,
    tracker: CostTracker,
    request: ProbeRequest,
    prompt: tuple[str, str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    system, user = prompt
    base = _public_request_fields(request)
    prompt_hash = hashlib.sha256((system + "\0" + user).encode("utf-8")).hexdigest()
    manager.events.emit(
        "LLMRequestRecorded",
        agent_id=request.case.persona_id,
        data={
            "request_id": request.request_id,
            "case_id": request.case.case_id,
            "sample_kind": request.sample_kind,
            "temperature": request.temperature,
            "concurrency": request.concurrency,
            "prompt_hash": prompt_hash,
            "run_kind": "endpoint_stochasticity",
        },
        private_data={"system_prompt": system, "user_prompt": user},
    )
    started = time.perf_counter()
    try:
        completion = await provider.complete(
            system,
            user,
            temperature=request.temperature,
            seed=request.seed,
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
            "sentiment": None,
            "order": None,
            "public_take": None,
            **safe_error,
        }
        private = {
            **public,
            "system_prompt": system,
            "user_prompt": user,
            "provider_error_detail": str(error),
        }
        manager.events.emit(
            "EndpointProbeFailed",
            agent_id=request.case.persona_id,
            data={
                "request_id": request.request_id,
                "case_id": request.case.case_id,
                **safe_error,
            },
            private_data={"provider_error_detail": str(error)},
        )
        return public, private

    latency_ms = round((time.perf_counter() - started) * 1000.0, 3)
    raw = completion.raw_response
    response_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
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
        agent_id=request.case.persona_id,
        data={
            "request_id": request.request_id,
            "case_id": request.case.case_id,
            "sample_kind": request.sample_kind,
            "source": "provider",
            "response_hash": response_hash,
            "input_tokens": completion.input_tokens,
            "output_tokens": completion.output_tokens,
            "total_tokens": completion.total_tokens,
        },
        private_data={"raw_response": raw},
    )
    evaluated, private_evaluation = qualification.evaluate_response(request.case, raw)
    action = str(evaluated["action"])
    quantity = int(evaluated["quantity"])
    signed_quantity = quantity if action == "buy" else -quantity if action == "sell" else 0
    order = {
        "side": action,
        "quantity": quantity,
        "signed_quantity": signed_quantity,
        "limit_price": float(evaluated["limit_price"]),
    }
    sentiment = float(evaluated["sentiment"])
    public_take = str(evaluated["public_take"])
    parse_failed = not bool(evaluated["parse_success"])
    public = {
        **base,
        "status": "completed",
        "prompt_sha256": prompt_hash,
        "raw_response_sha256": response_hash,
        "response_id_sha256": response_id_hash,
        "reported_model": completion.reported_model,
        "input_tokens": completion.input_tokens,
        "output_tokens": completion.output_tokens,
        "total_tokens": completion.total_tokens,
        "token_count_source": completion.token_count_source,
        "latency_ms": latency_ms,
        "parse_failed": parse_failed,
        "validation_failed": bool(evaluated["validation_failure"]),
        "sentiment": sentiment,
        "order": order,
        "public_take": public_take,
        "provider_error_type": None,
        "provider_status_code": None,
        "provider_error_code": None,
    }
    private = {
        **public,
        "system_prompt": system,
        "user_prompt": user,
        "raw_response": raw,
        "private_rationale": private_evaluation["parsed_decision"]["rationale"],
        "parsed_decision_private": dict(private_evaluation["parsed_decision"]),
    }
    manager.events.emit(
        "AgentDecisionParsed",
        agent_id=request.case.persona_id,
        data={
            "request_id": request.request_id,
            "case_id": request.case.case_id,
            "parse_status": "error" if parse_failed else "ok",
            "action": action,
            "quantity": quantity,
            "limit_price": order["limit_price"],
            "sentiment": sentiment,
            "public_take": public_take,
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
    requests: Sequence[ProbeRequest],
    prompts: Mapping[str, tuple[str, str]],
    *,
    concurrency: int,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    semaphore = asyncio.Semaphore(concurrency)

    async def bounded(request: ProbeRequest):
        async with semaphore:
            return await _execute_one(
                manager,
                provider,
                tracker,
                request,
                prompts[request.case.case_id],
            )

    provider.batch_sizes.append(len(requests))
    return list(await asyncio.gather(*(bounded(request) for request in requests)))


async def _run_requests(
    manager: ManagedRunContext,
    provider: Any,
    tracker: CostTracker,
    plan: EndpointStudyPlan,
    public_stream: TextIO,
    private_stream: TextIO,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    agents = qualification._agents_by_persona(STUDY_SEED)
    prompts = {
        case.case_id: qualification._build_prompt(
            case, agents[case.persona_id], "fake_test_provider"
        )
        for case in plan.selected_cases
    }
    public_rows: list[dict[str, Any]] = []
    private_rows: list[dict[str, Any]] = []
    grid = build_grid_requests(plan)
    for block_spec in plan.public_plan["block_schedule"]:
        temperature = float(block_spec["temperature"])
        concurrency = int(block_spec["concurrency_level"])
        repeat_start = int(block_spec["repeat_start_inclusive"])
        repeat_stop = int(block_spec["repeat_stop_exclusive"])
        block = [
            request
            for request in grid
            if request.temperature == temperature
            and request.concurrency == concurrency
            and request.replicate is not None
            and repeat_start <= request.replicate < repeat_stop
        ]
        results = await _execute_block(
            manager,
            provider,
            tracker,
            block,
            prompts,
            concurrency=concurrency,
        )
        block_public = [item[0] for item in results]
        block_private = [item[1] for item in results]
        public_rows.extend(block_public)
        private_rows.extend(block_private)
        _append_jsonl(private_stream, block_private)
        _append_jsonl(public_stream, block_public)
        _checkpoint_progress(manager, provider, tracker, plan, public_rows)
        completed = sum(row["status"] == "completed" for row in block_public)
        print(
            (
                "endpoint block wave={} position={} temp={} concurrency={} "
                "repeats=[{},{}) completed={}/{}"
            ).format(
                block_spec["wave_index"],
                block_spec["within_wave_position"],
                temperature,
                concurrency,
                repeat_start,
                repeat_stop,
                completed,
                len(block),
            ),
            file=sys.stderr,
            flush=True,
        )

    seed_requests = build_seed_probe_requests(plan, start_order=len(grid))
    seed_results = await _execute_block(
        manager,
        provider,
        tracker,
        seed_requests,
        prompts,
        concurrency=1,
    )
    seed_public = [item[0] for item in seed_results]
    seed_private = [item[1] for item in seed_results]
    public_rows.extend(seed_public)
    private_rows.extend(seed_private)
    _append_jsonl(private_stream, seed_private)
    _append_jsonl(public_stream, seed_public)
    _checkpoint_progress(manager, provider, tracker, plan, public_rows)
    public_rows.sort(key=lambda row: int(row["attempt_order"]))
    private_rows.sort(key=lambda row: int(row["attempt_order"]))
    return public_rows, private_rows


def _sample_variance(values: Sequence[float]) -> Optional[float]:
    if len(values) < 2:
        return None
    mean = math.fsum(values) / len(values)
    return math.fsum((value - mean) ** 2 for value in values) / (len(values) - 1)


def _pairwise_identity(hashes: Sequence[str]) -> tuple[int, int, Optional[float]]:
    denominator = len(hashes) * (len(hashes) - 1) // 2
    if denominator == 0:
        return 0, 0, None
    numerator = sum(count * (count - 1) // 2 for count in Counter(hashes).values())
    return numerator, denominator, numerator / denominator


def _round_optional(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(float(value), 12)


def aggregate_grid_results(
    rows: Sequence[Mapping[str, Any]], plan: EndpointStudyPlan
) -> dict[str, Any]:
    """Aggregate only within fixed case/temp/concurrency cells.

    Parsed sigma excludes parse failures.  Byte identity includes every
    transport-completed raw response, including an unparseable response.
    """

    grid_rows = [
        row
        for row in rows
        if row.get("measurement_kind", row.get("sample_kind")) == "grid"
    ]
    grouped: dict[tuple[float, int, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in grid_rows:
        grouped[(
            float(row["temperature"]),
            int(row.get("concurrency_level", row.get("concurrency"))),
            str(row["case_id"]),
        )].append(row)

    case_by_id = {case.case_id: case for case in plan.selected_cases}
    temperature_order = {value: index for index, value in enumerate(TEMPERATURES)}
    concurrency_order = {
        value: index for index, value in enumerate(CONCURRENCY_LEVELS)
    }
    ordered_keys = sorted(
        grouped,
        key=lambda key: (
            temperature_order.get(key[0], len(temperature_order)),
            concurrency_order.get(key[1], len(concurrency_order)),
            getattr(case_by_id.get(key[2]), "request_order", 10**9),
            key[2],
        ),
    )
    cell_summaries: list[dict[str, Any]] = []
    for temperature, concurrency, case_id in ordered_keys:
        case = case_by_id[case_id]
        cell = grouped[(temperature, concurrency, case_id)]
        completed = [
            row for row in cell if row.get("status", "completed") == "completed"
        ]
        parsed = [
            row
            for row in completed
            if row.get("parse_failed") is False
            and isinstance(row.get("order"), Mapping)
            and row.get("sentiment") is not None
        ]
        hashes = [
            str(row["raw_response_sha256"])
            for row in completed
            if row.get("raw_response_sha256") is not None
        ]
        equal_pairs, total_pairs, pairwise_rate = _pairwise_identity(hashes)
        sentiments = [float(row["sentiment"]) for row in parsed]
        signed_orders = [
            float(row["order"]["signed_quantity"]) for row in parsed
        ]
        quantities = [float(row["order"]["quantity"]) for row in parsed]
        limit_prices = [float(row["order"]["limit_price"]) for row in parsed]
        sentiment_variance = _sample_variance(sentiments)
        signed_order_variance = _sample_variance(signed_orders)
        cell_summaries.append(
            {
                "temperature": temperature,
                "concurrency_level": concurrency,
                "case_id": case.case_id,
                "persona_id": case.persona_id,
                "fixture_id": case.fixture_id,
                "mechanism_role": case.mechanism_role,
                "planned": REPEATS,
                "attempted": len(cell),
                "honest_n": len(completed),
                "parsed_n": len(parsed),
                "transport_failure_count": len(cell) - len(completed),
                "parse_failure_count": len(completed) - len(parsed),
                "skipped": max(0, REPEATS - len(cell)),
                "matching_response_pairs": equal_pairs,
                "eligible_response_pairs": total_pairs,
                "byte_identical_rate": _round_optional(pairwise_rate),
                "unique_raw_response_count": len(set(hashes)),
                "sentiment_variance": _round_optional(sentiment_variance),
                "sentiment_sigma": _round_optional(
                    math.sqrt(sentiment_variance)
                    if sentiment_variance is not None
                    else None
                ),
                "signed_order_variance": _round_optional(signed_order_variance),
                "signed_order_sigma": _round_optional(
                    math.sqrt(signed_order_variance)
                    if signed_order_variance is not None
                    else None
                ),
                "quantity_variance": _round_optional(_sample_variance(quantities)),
                "limit_price_variance": _round_optional(
                    _sample_variance(limit_prices)
                ),
                "action_distribution": dict(
                    sorted(Counter(str(row["order"]["side"]) for row in parsed).items())
                ),
            }
        )

    sigma_table: list[dict[str, Any]] = []
    observed_blocks = sorted(
        {(cell["temperature"], cell["concurrency_level"]) for cell in cell_summaries},
        key=lambda key: (
            temperature_order.get(key[0], len(temperature_order)),
            concurrency_order.get(key[1], len(concurrency_order)),
        ),
    )
    for temperature, concurrency in observed_blocks:
            cells = [
                cell
                for cell in cell_summaries
                if cell["temperature"] == temperature
                and cell["concurrency_level"] == concurrency
            ]
            sentiment_ss = math.fsum(
                (cell["parsed_n"] - 1) * cell["sentiment_variance"]
                for cell in cells
                if cell["parsed_n"] >= 2
                and cell["sentiment_variance"] is not None
            )
            order_ss = math.fsum(
                (cell["parsed_n"] - 1) * cell["signed_order_variance"]
                for cell in cells
                if cell["parsed_n"] >= 2
                and cell["signed_order_variance"] is not None
            )
            degrees_of_freedom = sum(
                cell["parsed_n"] - 1
                for cell in cells
                if cell["parsed_n"] >= 2
            )
            byte_equal_pairs = sum(cell["matching_response_pairs"] for cell in cells)
            byte_total_pairs = sum(cell["eligible_response_pairs"] for cell in cells)
            sentiment_variance = (
                sentiment_ss / degrees_of_freedom if degrees_of_freedom else None
            )
            signed_order_variance = (
                order_ss / degrees_of_freedom if degrees_of_freedom else None
            )
            sigma_table.append(
                {
                    "temperature": temperature,
                    "concurrency_level": concurrency,
                    "planned": len(cells) * REPEATS,
                    "attempted": sum(cell["attempted"] for cell in cells),
                    "honest_n": sum(cell["honest_n"] for cell in cells),
                    "parsed_n": sum(cell["parsed_n"] for cell in cells),
                    "transport_failure_count": sum(
                        cell["transport_failure_count"] for cell in cells
                    ),
                    "parse_failure_count": sum(
                        cell["parse_failure_count"] for cell in cells
                    ),
                    "matching_response_pairs": byte_equal_pairs,
                    "eligible_response_pairs": byte_total_pairs,
                    "byte_identical_rate": (
                        round(byte_equal_pairs / byte_total_pairs, 12)
                        if byte_total_pairs
                        else None
                    ),
                    "within_case_degrees_of_freedom": degrees_of_freedom,
                    "sentiment_variance": _round_optional(sentiment_variance),
                    "sentiment_sigma": (
                        round(math.sqrt(sentiment_variance), 12)
                        if sentiment_variance is not None
                        else None
                    ),
                    "signed_order_variance": _round_optional(signed_order_variance),
                    "signed_order_sigma": (
                        round(math.sqrt(signed_order_variance), 12)
                        if signed_order_variance is not None
                        else None
                    ),
                }
            )
    endpoint_responses = sum(
        row.get("status", "completed") == "completed" for row in grid_rows
    )
    parsed_decisions = sum(
        row.get("status", "completed") == "completed"
        and row.get("parse_failed") is False
        for row in grid_rows
    )
    return {
        "honest_n_endpoint_responses": endpoint_responses,
        "honest_n_parsed_decisions": parsed_decisions,
        "parse_failure_count": endpoint_responses - parsed_decisions,
        "cell_summaries": cell_summaries,
        "sigma_table": sigma_table,
    }


def aggregate_seed_probe(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    probe = sorted(
        (
            row
            for row in rows
            if row.get("measurement_kind", row.get("sample_kind")) == "seed_probe"
        ),
        key=lambda row: int(row["seed_probe_index"]),
    )
    completed = [
        row for row in probe if row.get("status", "completed") == "completed"
    ]
    rejected = any(
        row.get("provider_status_code") in {400, 422}
        for row in probe
        if row.get("status", "completed") != "completed"
    )
    if len(completed) == 2:
        parameter_status = "accepted"
    elif rejected:
        parameter_status = "rejected"
    else:
        parameter_status = "inconclusive_failure"
    hashes = [row.get("raw_response_sha256") for row in completed]
    seeds = [row.get("seed") for row in probe if row.get("seed") is not None]
    byte_identical = bool(hashes[0] == hashes[1]) if len(hashes) == 2 else None
    return {
        "planned": 2,
        "attempted": len(probe),
        "endpoint_responses": len(completed),
        "failed": len(probe) - len(completed),
        "request_count": len(probe),
        "seed": seeds[0] if seeds and len(set(seeds)) == 1 else None,
        "same_seed": len(seeds) == 2 and len(set(seeds)) == 1,
        "seed_parameter_status": parameter_status,
        "seed_parameter_accepted": parameter_status == "accepted",
        "two_response_byte_identical": byte_identical,
        "byte_identical": byte_identical,
        "raw_response_sha256": hashes,
        "evidence": [
            {
                "sample_id": row.get("sample_id", row.get("request_id")),
                "status": row.get("status"),
                "raw_response_sha256": row.get("raw_response_sha256"),
                "provider_status_code": row.get("provider_status_code"),
                "provider_error_code": row.get("provider_error_code"),
            }
            for row in probe
        ],
        "interpretation": (
            "acceptance and a two-call equality result do not prove that the "
            "endpoint honors seed semantics or is deterministic"
        ),
    }


def _completion_summary(
    rows: Sequence[Mapping[str, Any]], *, sample_kind: Optional[str], planned: int
) -> dict[str, int | str]:
    selected = [
        row
        for row in rows
        if sample_kind is None
        or row.get("measurement_kind", row.get("sample_kind")) == sample_kind
    ]
    responses = [
        row for row in selected if row.get("status", "completed") == "completed"
    ]
    parse_valid = [row for row in responses if row.get("parse_failed") is False]
    return {
        "unit": "endpoint_requests",
        "planned": planned,
        "attempted": len(selected),
        "completed": len(responses),
        "endpoint_responses": len(responses),
        "parse_valid": len(parse_valid),
        "transport_failures": len(selected) - len(responses),
        "parse_failures": len(responses) - len(parse_valid),
        "skipped": max(0, planned - len(selected)),
    }


def _checkpoint_progress(
    manager: ManagedRunContext,
    provider: Any,
    tracker: CostTracker,
    plan: EndpointStudyPlan,
    public_rows: Sequence[Mapping[str, Any]],
) -> None:
    """Persist endpoint honest-N after every completed schedule block."""

    planned = int(plan.public_plan["planned_total_requests"])
    completion = _completion_summary(
        public_rows,
        sample_kind=None,
        planned=planned,
    )
    network_access = bool(getattr(provider, "network_access", False))
    manager.network_access = network_access
    manager.sync_llm_accounting(provider, tracker)
    endpoint_manifest = manager.manifest["endpoint_stochasticity"]
    endpoint_manifest["network_access"] = network_access
    endpoint_manifest["endpoint_responses"] = dict(completion)
    endpoint_manifest["honest_n_endpoint_responses"] = completion[
        "endpoint_responses"
    ]
    endpoint_manifest["honest_n_parsed_decisions"] = completion["parse_valid"]
    manager.manifest["honest_n_endpoint_responses"] = completion[
        "endpoint_responses"
    ]
    manager.manifest["honest_n_parsed_decisions"] = completion["parse_valid"]
    manager.manifest["honest_n_runs"] = 0
    manager._write()


def build_summary(
    manager: ManagedRunContext,
    *,
    args: argparse.Namespace,
    plan: EndpointStudyPlan,
    model: str,
    provider: Any,
    public_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    grid_planned = int(plan.public_plan["planned_grid_requests"])
    seed_planned = int(plan.public_plan["planned_seed_probe_requests"])
    total_planned = int(plan.public_plan["planned_total_requests"])
    grid = _completion_summary(public_rows, sample_kind="grid", planned=grid_planned)
    seed = _completion_summary(
        public_rows, sample_kind="seed_probe", planned=seed_planned
    )
    total = _completion_summary(public_rows, sample_kind=None, planned=total_planned)
    aggregate = aggregate_grid_results(public_rows, plan)
    token_rows = [row for row in public_rows if row.get("status") == "completed"]
    return {
        "output_schema_version": OUTPUT_SCHEMA_VERSION,
        "run_id": manager.run_id,
        "run_kind": "endpoint_stochasticity",
        "study_plan_hash": plan.study_plan_hash,
        "study_plan": dict(plan.public_plan),
        "provider": args.provider,
        "model_requested": args.model or None,
        "model_resolved": model,
        "live": bool(args.live),
        "dry_run": False,
        "network_access": bool(getattr(provider, "network_access", False)),
        "completion": {
            "grid": grid,
            "seed_probe": seed,
            "total": total,
        },
        "honest_n_endpoint_responses": total["endpoint_responses"],
        "honest_n_parsed_decisions": total["parse_valid"],
        "honest_n_runs": 0,
        "token_usage": {
            "provider_usage_rows": sum(
                row.get("token_count_source") == "provider_usage"
                for row in token_rows
            ),
            "unavailable_rows": sum(row.get("total_tokens") is None for row in token_rows),
            "input_tokens": sum(
                int(row["input_tokens"])
                for row in token_rows
                if row.get("input_tokens") is not None
            ),
            "output_tokens": sum(
                int(row["output_tokens"])
                for row in token_rows
                if row.get("output_tokens") is not None
            ),
            "total_tokens": sum(
                int(row["total_tokens"])
                for row in token_rows
                if row.get("total_tokens") is not None
            ),
        },
        "sigma_table": aggregate["sigma_table"],
        "cell_summaries": aggregate["cell_summaries"],
        "parse_failure_count": total["parse_failures"],
        "grid_parse_failure_count": aggregate["parse_failure_count"],
        "seed_probe": aggregate_seed_probe(public_rows),
        "power_guidance": {
            "paired_seed_variance": (
                "Var(D_seed(K)) = tau_between^2 + sigma_on_within^2/K + "
                "sigma_off_within^2/K"
            ),
            "mean_standard_error": "SE(mean(D)) = sqrt(Var(D_seed(K))/N)",
            "interpretation": (
                "K reduces within-seed Provider noise; N addresses between-seed "
                "generalization. Response-level sigma is not market-outcome sigma."
            ),
        },
        "artifacts": {
            "endpoint_samples": "endpoint_samples.jsonl",
            "private_endpoint_records": "private_endpoint_records.jsonl",
        },
    }


def _write_json_exclusive(path: Path, payload: Any, mode: int = 0o644) -> None:
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            json.dump(payload, stream, indent=2, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
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


def _dry_run_summary(
    manager: ManagedRunContext,
    *,
    args: argparse.Namespace,
    plan: EndpointStudyPlan,
    model: str,
) -> dict[str, Any]:
    return {
        "output_schema_version": OUTPUT_SCHEMA_VERSION,
        "run_id": manager.run_id,
        "run_kind": "endpoint_stochasticity",
        "dry_run": True,
        "live": False,
        "provider": args.provider,
        "model_requested": args.model or None,
        "model_resolved": model,
        "study_plan_hash": plan.study_plan_hash,
        "study_plan": dict(plan.public_plan),
        "source_case_count": len(plan.all_cases),
        "selected_case_count": len(plan.selected_cases),
        "planned_grid_requests": plan.public_plan["planned_grid_requests"],
        "planned_seed_probe_requests": plan.public_plan[
            "planned_seed_probe_requests"
        ],
        "planned_total_requests": plan.public_plan["planned_total_requests"],
        "provider_calls": 0,
        "network_access": False,
        "honest_n_endpoint_responses": 0,
        "honest_n_parsed_decisions": 0,
        "honest_n_runs": 0,
    }


def run_study(
    manager: ManagedRunContext,
    *,
    args: argparse.Namespace,
    cfg: Config,
    plan: EndpointStudyPlan,
    model: str,
) -> dict[str, Any]:
    provider = _build_provider(args.provider, cfg=cfg, model=model)
    tracker = CostTracker()
    manager.active_llm = provider
    manager.tracker = tracker
    manager.llm_mode = "record"
    manager.register_llm_runtime(
        provider=args.provider,
        model=model,
        mode="endpoint_stochasticity",
        cache_enabled=False,
        network_access=False,
        provider_calls=0,
        application_concurrency_limit=max(CONCURRENCY_LEVELS),
        provider_connection_limit=(
            max(CONCURRENCY_LEVELS) if args.provider == "openai" else 0
        ),
        temperature_grid=list(TEMPERATURES),
        provider_retry_count=0,
        live=bool(args.live),
    )
    public_stream = _open_jsonl_exclusive(
        manager.run_dir / "endpoint_samples.jsonl", 0o644
    )
    try:
        private_stream = _open_jsonl_exclusive(
            manager.run_dir / "private_endpoint_records.jsonl", 0o600
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
    endpoint_manifest = manager.manifest["endpoint_stochasticity"]
    endpoint_manifest["network_access"] = summary["network_access"]
    endpoint_manifest["endpoint_responses"] = dict(
        summary["completion"]["total"]
    )
    endpoint_manifest["honest_n_endpoint_responses"] = summary[
        "honest_n_endpoint_responses"
    ]
    endpoint_manifest["honest_n_parsed_decisions"] = summary[
        "honest_n_parsed_decisions"
    ]
    endpoint_manifest["seed_probe"] = dict(summary["seed_probe"])
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
    parser.add_argument("--version", action="version", version="endpoint-stochasticity 1.0")
    parser.add_argument(
        "--provider",
        choices=sorted(ALLOWED_PROVIDER_IDS),
        default="fake_test_provider",
    )
    parser.add_argument("--model", default="")
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
            raise EndpointStudyError("--dry-run and --live are mutually exclusive")
        plan = load_study_plan()
        model = _resolved_model(args.provider, args.model)
    except (ManagedCLIError, OSError, EndpointStudyError, ValueError) as error:
        fail_cli(bootstrap, error, failure_stage="config_validation")

    cfg = Config(
        provider=args.provider,
        model=model,
        seed=STUDY_SEED,
        n_llm_agents=6,
        n_noise_agents=0,
        temperature=0.0,
        max_tokens=MAX_TOKENS,
        cache_enabled=False,
        out_dir=args.out,
    )
    manager = ManagedRunContext.create(
        cfg,
        out_root=args.out,
        scenario_id="endpoint-stochasticity:{}".format(STUDY_PLAN_SCHEMA_VERSION),
        run_id=args.run_id,
        worker_count=max(CONCURRENCY_LEVELS),
        batching={
            "temperature_grid": list(TEMPERATURES),
            "concurrency_levels": list(CONCURRENCY_LEVELS),
            "repeats_per_case_cell": REPEATS,
        },
        input_paths={
            "qualification_protocol": qualification.PROTOCOL_PATH,
            "qualification_observations": qualification.OBSERVATIONS_PATH,
            "qualification_rubric": qualification.RUBRIC_PATH,
            "qualification_visibility_contract": qualification.VISIBILITY_CONTRACT_PATH,
        },
        command_identity=COMMAND_IDENTITY,
        run_kind="endpoint_stochasticity",
        planned_simulation_runs=0,
    )
    try:
        with manager:
            manager.set_stage("provider_setup")
            try:
                _validate_execution_guard(args)
            except EndpointProviderGuardError:
                capability = _endpoint_adapter_capability_snapshot(
                    args.provider,
                    endpoint=(
                        os.environ.get("OPENAI_BASE_URL") or cfg.openai_base_url
                        if args.provider == "openai"
                        else None
                    ),
                )
                _initialise_manifest(
                    manager,
                    args=args,
                    plan=plan,
                    model=model,
                    capability=capability,
                )
                raise
            capability = _endpoint_adapter_capability_snapshot(
                args.provider,
                endpoint=(
                    os.environ.get("OPENAI_BASE_URL") or cfg.openai_base_url
                    if args.provider == "openai"
                    else None
                ),
            )
            _initialise_manifest(
                manager,
                args=args,
                plan=plan,
                model=model,
                capability=capability,
            )
            manager.register_llm_runtime(
                provider=args.provider,
                model=model,
                mode=("endpoint_stochasticity_dry_run" if args.dry_run else "endpoint_stochasticity"),
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

            manager.set_stage("provider_setup")
            payload = run_study(
                manager,
                args=args,
                cfg=cfg,
                plan=plan,
                model=model,
            )
            manager.set_stage("result_export")
            _write_json_exclusive(
                manager.run_dir / "endpoint_stochasticity_summary.json",
                payload["summary"],
            )
            manager.finish()
            print(json.dumps(payload["summary"], ensure_ascii=False, sort_keys=True))
    except EndpointProviderGuardError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2)
    except Exception as error:
        if args.provider == "openai":
            print(
                "endpoint stochasticity run failed: {}".format(type(error).__name__),
                file=sys.stderr,
            )
            raise SystemExit(1) from None
        raise


if __name__ == "__main__":
    main()


__all__ = [
    "ALLOWED_PROVIDER_IDS",
    "CONCURRENCY_LEVELS",
    "EndpointCompletion",
    "EndpointProviderGuardError",
    "EndpointStudyError",
    "EndpointStudyPlan",
    "EXPECTED_SELECTED_CASE_IDS",
    "EXPECTED_STUDY_PLAN_HASH",
    "FakeEndpointProvider",
    "MAX_TOKENS",
    "OUTPUT_SCHEMA_VERSION",
    "REPEATS",
    "SELECTED_CASE_SPECS",
    "SEED_PROBE_TEMPERATURE",
    "SEED_PROBE_VALUE",
    "STUDY_PLAN_SCHEMA_VERSION",
    "TEMPERATURES",
    "aggregate_grid_results",
    "aggregate_seed_probe",
    "build_argparser",
    "build_grid_requests",
    "build_seed_probe_requests",
    "build_summary",
    "load_study_plan",
    "main",
    "run_study",
]
