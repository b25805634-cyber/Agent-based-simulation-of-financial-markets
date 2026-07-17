"""Offline-safe model qualification protocol with guarded Codex pilots.

This is a managed experiment entrypoint, but it is deliberately not a market
simulation.  It applies the existing Agent prompt builders and Decision parser
to a frozen matrix of observations.  ``MockLLM`` and an in-process
deterministic test double remain the default offline paths.  The experimental
``codex_exec`` path is available only behind explicit case-count and real-use
guards; dry-runs never construct it.

The protocol is diagnostic rather than normative: no fixture has one required
trading action, and behavioral output is reported as distributions and
relative signals instead of a single quality score.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Iterable, Mapping, Optional, Sequence

from nmsim.agents import Agent, make_agents
from nmsim.config import Config
from nmsim.llm import CostTracker, MockLLM, parse_order
from nmsim.managed_cli import (
    BootstrapCLIError,
    ManagedCLIError,
    RaisingArgumentParser,
    bootstrap_cli,
    fail_cli,
)
from nmsim.provider_capabilities import provider_capability_snapshot
from nmsim.run_context import ManagedRunContext


PROTOCOL_ROOT = Path(__file__).resolve().parents[1] / "qualification"
PROTOCOL_PATH = PROTOCOL_ROOT / "protocol.json"
OBSERVATIONS_PATH = PROTOCOL_ROOT / "observations.json"
RUBRIC_PATH = PROTOCOL_ROOT / "rubric.json"
VISIBILITY_CONTRACT_PATH = PROTOCOL_ROOT / "visibility_contract.json"
ALLOWED_PROVIDER_IDS = frozenset({"mock", "fake_test_provider", "codex_exec"})
QUALIFICATION_OUTPUT_SCHEMA_VERSION = "1.1"
QUALIFICATION_SELECTION_SCHEMA_VERSION = "1.0"
CODEX_DEFAULT_MAX_CASES = 1
QUALIFICATION_MAX_CASES = 48
_REQUIRED_DECISION_FIELDS = frozenset(
    {"quantity", "limit_price", "sentiment", "public_take"}
)
_FALLBACK_MARKERS = frozenset(
    {
        "parse-failed; holding",
        "api-error; holding",
        "parse-retries-exhausted; holding",
    }
)
_PUBLIC_CODEX_METADATA_FIELDS = frozenset(
    {
        "provider",
        "experimental",
        "codex_cli_version",
        "binary_identity",
        "requested_model",
        "reasoning_effort",
        "auth_mode",
        "auth_verified",
        "forced_login_method",
        "codex_wrapper_protocol_version",
        "wrapper_source_hash",
        "decision_schema_version",
        "decision_schema_hash",
        "execution_flags",
        "sandbox_mode",
        "ephemeral",
        "strict_config",
        "tool_surface_contract",
        "tool_surface_contract_hash",
        "control_mapping_schema_version",
        "control_mapping_policy_hash",
        "control_mapping_hash",
        "control_matrix",
        "tool_surface_verified",
        "capability_probe_method",
        "auth_probe_performed",
        "subprocess_started",
        "model_turn_subprocess_started",
        "real_use_ready",
        "real_use_readiness",
        "real_use_missing_requirements",
        "tool_access",
        "provider_transport_network_expected",
        "provider_transport_network_declared_or_observed",
        "agent_tool_network_enabled",
        "web_search_mode",
        "shell_tool_enabled",
        "unified_exec_enabled",
        "apps_enabled",
        "memories_enabled",
        "view_image_enabled",
        "history_persistence",
        "agent_reasoning_events_hidden",
        "show_raw_agent_reasoning",
        "personality",
        "production_system_prompt_hash",
        "production_user_prompt_hash",
        "final_combined_input_hash",
        "request_sequence",
        "batch_identity",
        "isolated_cwd_identity",
        "response_source",
        "process_exit_code",
        "latency_seconds",
        "timeout",
        "event_type_counts",
        "tool_use_violation_count",
        "tool_calls_observed",
        "reasoning_event_count",
        "effective_config_anomaly",
        "usage",
        "reported_model",
        "actual_model_verification",
        "final_response_hash",
        "status",
        "error_code",
    }
)


class QualificationProtocolError(ValueError):
    """A version-controlled qualification input violates its contract."""


class QualificationProviderGuardError(ValueError):
    """Qualification rejected an unconfirmed/unreviewed provider before I/O."""


@dataclass(frozen=True)
class QualificationCase:
    case_id: str
    request_order: int
    persona_id: str
    fixture_id: str
    fixture: Mapping[str, Any]


@dataclass(frozen=True)
class QualificationSelection:
    """Stable, public-safe identity for one qualification case subset."""

    cases: tuple[QualificationCase, ...]
    metadata: Mapping[str, Any]


class FakeTestProvider:
    """Qualification-only deterministic response source with no I/O surface."""

    kind = "fake_test_provider"
    model = "fixture-defined-test-double-v1"

    def __init__(self, seed: int = 0) -> None:
        self.seed = int(seed)
        self.calls = 0

    def complete(self, system: str, user: str) -> str:
        self.calls += 1
        digest = hashlib.sha256(
            (str(self.seed) + "\0" + system + "\0" + user).encode("utf-8")
        ).digest()
        action = ("sell", "hold", "buy")[digest[0] % 3]
        quantity = 0 if action == "hold" else 1 + digest[1] % 7
        sentiment = {-1: -0.5, 0: 0.0, 1: 0.5}[
            {"sell": -1, "hold": 0, "buy": 1}[action]
        ]
        return json.dumps(
            {
                "action": action,
                "quantity": quantity,
                "limit_price": 90.0 + digest[2] / 10.0,
                "sentiment": sentiment,
                "public_take": "Deterministic qualification test response.",
                "reasoning": "private deterministic test-double explanation",
            },
            sort_keys=True,
        )


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def stable_json_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def fixture_input_hash(fixture: Mapping[str, Any]) -> str:
    payload = dict(fixture)
    payload.pop("input_hash", None)
    return stable_json_hash(payload)


def fixture_set_hash(fixtures: Iterable[Mapping[str, Any]]) -> str:
    """Hash fixture contents independently of input list order."""

    ordered = sorted(
        (dict(fixture) for fixture in fixtures),
        key=lambda fixture: str(fixture.get("fixture_id", "")),
    )
    return stable_json_hash(
        {
            "fixture_set_hash_schema_version": "1.0",
            "fixtures": ordered,
        }
    )


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise QualificationProtocolError("qualification document must be an object")
    return value


_FIXTURE_FIELD_PATHS = frozenset(
    {
        "fixture_id",
        "protocol_version",
        "round",
        "market_state.latest_price",
        "market_state.recent_prices",
        "visible_news",
        "visible_social_feed.sentiment",
        "visible_social_feed.public_take",
        "cash",
        "shares",
        "memory",
        "fundamental_value",
        "invisible_fields",
        "input_hash",
    }
)
_VISIBILITY_STATUSES = frozenset({"always", "conditional", "never", "mode_dependent"})


def _visibility_fields(contract: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    rows = contract.get("fields")
    if not isinstance(rows, list):
        raise QualificationProtocolError("visibility contract fields must be a list")
    indexed: dict[str, Mapping[str, Any]] = {}
    required = {
        "field",
        "present_in_fixture",
        "visible_to_model",
        "visibility_by_prompt_mode",
        "visible_to_evaluator",
        "private_or_public",
        "allowed_for_scoring",
        "rationale",
    }
    for row in rows:
        if not isinstance(row, Mapping) or not required.issubset(row):
            raise QualificationProtocolError(
                "visibility contract field is missing required metadata"
            )
        name = str(row["field"])
        if not name or name in indexed:
            raise QualificationProtocolError(
                "visibility contract field names must be non-empty and unique"
            )
        if row["visible_to_model"] not in _VISIBILITY_STATUSES:
            raise QualificationProtocolError(
                "invalid model visibility for field {}".format(name)
            )
        if row["present_in_fixture"] is not True or not isinstance(
            row["visible_to_evaluator"], bool
        ):
            raise QualificationProtocolError(
                "invalid fixture/evaluator visibility metadata for field {}".format(
                    name
                )
            )
        by_mode = row["visibility_by_prompt_mode"]
        if not isinstance(by_mode, Mapping) or set(by_mode) != {"real", "mock"}:
            raise QualificationProtocolError(
                "prompt-mode visibility must cover real and mock for field {}".format(
                    name
                )
            )
        if not str(row["private_or_public"]).strip() or not str(
            row["rationale"]
        ).strip():
            raise QualificationProtocolError(
                "visibility contract field {} requires classification and rationale".format(
                    name
                )
            )
        if not isinstance(row["allowed_for_scoring"], list):
            raise QualificationProtocolError(
                "allowed_for_scoring must be a list for field {}".format(name)
            )
        indexed[name] = row
    if set(indexed) != _FIXTURE_FIELD_PATHS:
        missing = sorted(_FIXTURE_FIELD_PATHS - set(indexed))
        extra = sorted(set(indexed) - _FIXTURE_FIELD_PATHS)
        raise QualificationProtocolError(
            "visibility contract fixture fields differ: missing={} extra={}".format(
                missing, extra
            )
        )
    return indexed


def _validate_rubric_visibility(
    rubric: Mapping[str, Any], contract: Mapping[str, Any]
) -> None:
    fields_by_name = _visibility_fields(contract)
    non_fixture_rows = contract.get("non_fixture_prompt_inputs")
    if not isinstance(non_fixture_rows, list):
        raise QualificationProtocolError(
            "visibility contract non_fixture_prompt_inputs must be a list"
        )
    non_fixture = {
        str(row.get("field")): row
        for row in non_fixture_rows
        if isinstance(row, Mapping) and row.get("field")
    }
    all_inputs = {**fields_by_name, **non_fixture}
    for section_name in ("engineering_metrics", "behavioral_diagnostics"):
        section = rubric.get(section_name)
        if not isinstance(section, Mapping):
            raise QualificationProtocolError(
                "rubric {} must be an object".format(section_name)
            )
        prefix = "engineering" if section_name == "engineering_metrics" else "behavioral"
        for metric_name, raw_spec in section.items():
            if not isinstance(raw_spec, Mapping):
                raise QualificationProtocolError(
                    "rubric metric {}.{} lacks dependency metadata".format(
                        prefix, metric_name
                    )
                )
            model_dependencies = raw_spec.get("model_input_dependencies")
            evaluator_dependencies = raw_spec.get("evaluator_dependencies")
            if not isinstance(model_dependencies, list) or not isinstance(
                evaluator_dependencies, list
            ):
                raise QualificationProtocolError(
                    "rubric metric {}.{} must declare model and evaluator dependencies".format(
                        prefix, metric_name
                    )
                )
            metric_id = "{}.{}".format(prefix, metric_name)
            is_not_scored = raw_spec.get("mode") == "not_scored"
            if is_not_scored and not str(raw_spec.get("reason", "")).strip():
                raise QualificationProtocolError(
                    "not_scored metric {} requires a reason".format(metric_id)
                )
            resolved_dependencies = []
            for dependency in model_dependencies:
                field = all_inputs.get(str(dependency))
                if field is None:
                    raise QualificationProtocolError(
                        "rubric metric {} references unknown model input {}".format(
                            metric_id, dependency
                        )
                    )
                resolved_dependencies.append((dependency, field))
            for dependency, field in resolved_dependencies:
                visibility = field.get("visible_to_model")
                if not is_not_scored and visibility in {"never", "mode_dependent"}:
                    raise QualificationProtocolError(
                        "rubric metric {} depends on model-invisible field {}".format(
                            metric_id, dependency
                        )
                    )
            for dependency, field in resolved_dependencies:
                allowed = field.get("allowed_for_scoring", [])
                if not is_not_scored and metric_id not in allowed:
                    raise QualificationProtocolError(
                        "rubric metric {} is not allowed to score field {}".format(
                            metric_id, dependency
                        )
                    )

    fundamental = rubric["behavioral_diagnostics"].get("fundamental_anchor_score", {})
    if (
        fundamental.get("mode") != "not_scored"
        or fundamental.get("reason") != "fundamental_anchor_not_visible"
    ):
        raise QualificationProtocolError(
            "fundamental_anchor_score must remain not_scored while the real prompt hides fundamental_value"
        )


def _validate_fixture_shapes(fixtures: Sequence[Mapping[str, Any]]) -> None:
    expected_top = {
        "fixture_id",
        "protocol_version",
        "round",
        "market_state",
        "visible_news",
        "visible_social_feed",
        "cash",
        "shares",
        "memory",
        "fundamental_value",
        "invisible_fields",
        "input_hash",
    }
    for fixture in fixtures:
        if set(fixture) != expected_top:
            raise QualificationProtocolError(
                "fixture fields differ from visibility contract: {}".format(
                    fixture.get("fixture_id")
                )
            )
        state = fixture.get("market_state")
        if not isinstance(state, Mapping) or set(state) != {
            "latest_price",
            "recent_prices",
        }:
            raise QualificationProtocolError(
                "fixture market_state differs from visibility contract: {}".format(
                    fixture.get("fixture_id")
                )
            )
        feed = fixture.get("visible_social_feed")
        if not isinstance(feed, list) or any(
            not isinstance(item, Mapping)
            or set(item) != {"sentiment", "public_take"}
            for item in feed
        ):
            raise QualificationProtocolError(
                "fixture social feed differs from visibility contract: {}".format(
                    fixture.get("fixture_id")
                )
            )


def load_protocol_bundle(
    *,
    protocol_path: Path = PROTOCOL_PATH,
    observations_path: Path = OBSERVATIONS_PATH,
    rubric_path: Path = RUBRIC_PATH,
    visibility_contract_path: Path = VISIBILITY_CONTRACT_PATH,
) -> dict[str, Any]:
    protocol = _read_json(protocol_path)
    observations = _read_json(observations_path)
    rubric = _read_json(rubric_path)
    visibility_contract = _read_json(visibility_contract_path)
    version = str(protocol.get("protocol_version", ""))
    observation_version = str(
        protocol.get("observation_protocol_version", version)
    )
    if not version or observations.get("protocol_version") != observation_version:
        raise QualificationProtocolError("observation protocol version mismatch")
    if rubric.get("protocol_version") != version:
        raise QualificationProtocolError("rubric protocol version mismatch")
    if visibility_contract.get("protocol_version") != version:
        raise QualificationProtocolError("visibility contract protocol version mismatch")
    if visibility_contract.get("observation_protocol_version") != observation_version:
        raise QualificationProtocolError(
            "visibility contract observation protocol version mismatch"
        )
    if visibility_contract.get("visibility_contract_version") != protocol.get(
        "visibility_contract_version"
    ) or rubric.get("visibility_contract_version") != protocol.get(
        "visibility_contract_version"
    ):
        raise QualificationProtocolError("visibility contract version mismatch")
    fixtures = observations.get("fixtures")
    if not isinstance(fixtures, list):
        raise QualificationProtocolError("observations.fixtures must be a list")
    _validate_fixture_shapes(fixtures)
    fixture_ids = [str(fixture.get("fixture_id", "")) for fixture in fixtures]
    if not fixture_ids or len(fixture_ids) != len(set(fixture_ids)):
        raise QualificationProtocolError("fixture ids must be non-empty and unique")
    if sorted(fixture_ids) != sorted(protocol.get("fixture_ids", [])):
        raise QualificationProtocolError("protocol and observation fixture ids differ")
    for fixture in fixtures:
        if fixture.get("protocol_version") != observation_version:
            raise QualificationProtocolError(
                "fixture protocol version mismatch: {}".format(
                    fixture.get("fixture_id")
                )
            )
        actual = fixture_input_hash(fixture)
        if fixture.get("input_hash") != actual:
            raise QualificationProtocolError(
                "fixture input hash mismatch: {}".format(fixture.get("fixture_id"))
            )
        forbidden = {
            "future_price",
            "future_prices",
            "expected_answer",
            "evaluation_rubric",
            "private_rationale",
        }
        if forbidden.intersection(fixture):
            raise QualificationProtocolError(
                "fixture contains a forbidden visible field: {}".format(
                    fixture.get("fixture_id")
                )
            )
    persona_ids = [str(value) for value in protocol.get("persona_ids", [])]
    if len(persona_ids) != 6 or len(set(persona_ids)) != 6:
        raise QualificationProtocolError("protocol must identify exactly six personas")
    if len(fixtures) != 8:
        raise QualificationProtocolError("protocol must contain exactly eight fixtures")
    expected = int(protocol.get("expected_case_count", -1))
    if expected != len(persona_ids) * len(fixtures) or expected != 48:
        raise QualificationProtocolError("protocol must define exactly 48 cases")
    if rubric.get("frozen_before_external_provider_calls") is not True:
        raise QualificationProtocolError("rubric must be frozen before external calls")
    _validate_rubric_visibility(rubric, visibility_contract)
    return {
        "protocol": protocol,
        "observations": observations,
        "rubric": rubric,
        "visibility_contract": visibility_contract,
        "protocol_hash": stable_json_hash(protocol),
        "fixture_set_hash": fixture_set_hash(fixtures),
        "rubric_hash": stable_json_hash(rubric),
        "visibility_contract_hash": stable_json_hash(visibility_contract),
    }


def build_cases(bundle: Mapping[str, Any]) -> list[QualificationCase]:
    protocol = bundle["protocol"]
    fixtures_by_id = {
        fixture["fixture_id"]: fixture
        for fixture in bundle["observations"]["fixtures"]
    }
    cases: list[QualificationCase] = []
    for persona_id in protocol["persona_ids"]:
        for fixture_id in protocol["fixture_ids"]:
            fixture = fixtures_by_id[fixture_id]
            identity = {
                "case_identity_schema_version": protocol[
                    "case_identity_schema_version"
                ],
                "protocol_version": protocol["protocol_version"],
                "persona_id": persona_id,
                "fixture_id": fixture_id,
                "fixture_input_hash": fixture["input_hash"],
            }
            cases.append(
                QualificationCase(
                    case_id="qcase-{}".format(stable_json_hash(identity)[:24]),
                    request_order=len(cases),
                    persona_id=persona_id,
                    fixture_id=fixture_id,
                    fixture=fixture,
                )
            )
    if len(cases) != 48 or len({case.case_id for case in cases}) != 48:
        raise QualificationProtocolError("qualification case identities are not 48 unique values")
    return cases


def _normalise_selector(values: Optional[Sequence[str]]) -> tuple[str, ...]:
    """Return a deterministic selector set without accepting empty ids."""

    normalised = tuple(sorted({str(value).strip() for value in (values or ())}))
    if "" in normalised:
        raise QualificationProtocolError("qualification selector ids must be non-empty")
    return normalised


def select_cases(
    bundle: Mapping[str, Any],
    cases: Sequence[QualificationCase],
    *,
    provider_id: str,
    case_ids: Optional[Sequence[str]] = None,
    fixture_ids: Optional[Sequence[str]] = None,
    persona_ids: Optional[Sequence[str]] = None,
    max_cases: Optional[int] = None,
) -> QualificationSelection:
    """Select a stable qualification subset and bind its exact identity.

    Selector order and duplicate selector flags do not affect the result.  Case
    order always follows the frozen protocol request order.  ``codex_exec`` is
    the only provider whose omitted ``max_cases`` is narrowed: it defaults to
    one case; existing Mock/Fake invocations continue to select all 48.
    """

    provider = str(provider_id).strip().lower()
    requested_case_ids = _normalise_selector(case_ids)
    requested_fixture_ids = _normalise_selector(fixture_ids)
    requested_persona_ids = _normalise_selector(persona_ids)
    ordered_cases = tuple(sorted(cases, key=lambda item: item.request_order))
    if len(ordered_cases) != QUALIFICATION_MAX_CASES:
        raise QualificationProtocolError(
            "qualification protocol must expose exactly {} cases before selection".format(
                QUALIFICATION_MAX_CASES
            )
        )

    known = {
        "case_id": {case.case_id for case in ordered_cases},
        "fixture_id": {case.fixture_id for case in ordered_cases},
        "persona_id": {case.persona_id for case in ordered_cases},
    }
    requested_by_field = {
        "case_id": requested_case_ids,
        "fixture_id": requested_fixture_ids,
        "persona_id": requested_persona_ids,
    }
    for field, requested in requested_by_field.items():
        unknown = sorted(set(requested) - known[field])
        if unknown:
            raise QualificationProtocolError(
                "unknown qualification {} selector(s): {}".format(
                    field, ", ".join(unknown)
                )
            )

    if max_cases is None:
        effective_max_cases = (
            CODEX_DEFAULT_MAX_CASES
            if provider == "codex_exec"
            else QUALIFICATION_MAX_CASES
        )
        max_cases_defaulted = True
    else:
        effective_max_cases = int(max_cases)
        max_cases_defaulted = False
    if effective_max_cases < 1 or effective_max_cases > QUALIFICATION_MAX_CASES:
        raise QualificationProtocolError(
            "--max-cases must be between 1 and {}".format(QUALIFICATION_MAX_CASES)
        )

    filtered = [
        case
        for case in ordered_cases
        if (not requested_case_ids or case.case_id in requested_case_ids)
        and (not requested_fixture_ids or case.fixture_id in requested_fixture_ids)
        and (not requested_persona_ids or case.persona_id in requested_persona_ids)
    ]
    selected = tuple(filtered[:effective_max_cases])
    if not selected:
        raise QualificationProtocolError(
            "qualification selectors produced an empty case set"
        )

    def in_order(attribute: str) -> list[str]:
        return list(dict.fromkeys(str(getattr(case, attribute)) for case in selected))

    identity = {
        "selection_schema_version": QUALIFICATION_SELECTION_SCHEMA_VERSION,
        "protocol_version": bundle["protocol"]["protocol_version"],
        "protocol_hash": bundle["protocol_hash"],
        "fixture_set_hash": bundle["fixture_set_hash"],
        "rubric_hash": bundle["rubric_hash"],
        "visibility_contract_hash": bundle["visibility_contract_hash"],
        "selected_cases": [
            {
                "case_id": case.case_id,
                "request_order": case.request_order,
                "persona_id": case.persona_id,
                "fixture_id": case.fixture_id,
                "fixture_input_hash": case.fixture["input_hash"],
            }
            for case in selected
        ],
    }
    full_case_ids = [case.case_id for case in ordered_cases]
    selected_case_ids = [case.case_id for case in selected]
    metadata = {
        "selection_schema_version": QUALIFICATION_SELECTION_SCHEMA_VERSION,
        "selection_hash": stable_json_hash(identity),
        "protocol_hash": bundle["protocol_hash"],
        "fixture_set_hash": bundle["fixture_set_hash"],
        "rubric_hash": bundle["rubric_hash"],
        "visibility_contract_hash": bundle["visibility_contract_hash"],
        "requested": {
            "case_ids": list(requested_case_ids),
            "fixture_ids": list(requested_fixture_ids),
            "persona_ids": list(requested_persona_ids),
            "max_cases": max_cases,
        },
        "effective_max_cases": effective_max_cases,
        "max_cases_defaulted": max_cases_defaulted,
        "full_protocol_case_count": len(ordered_cases),
        "selected_case_count": len(selected),
        "selected_case_ids": selected_case_ids,
        "selected_fixture_ids": in_order("fixture_id"),
        "selected_persona_ids": in_order("persona_id"),
        "is_full_qualification": selected_case_ids == full_case_ids,
        "qualification_scope": (
            "complete_protocol"
            if selected_case_ids == full_case_ids
            else "subset_pilot"
        ),
    }
    return QualificationSelection(cases=selected, metadata=metadata)


def _validate_provider_execution_guard(
    args: argparse.Namespace, selection: QualificationSelection
) -> None:
    """Fail before provider construction unless real Codex use is explicit."""

    if args.provider not in ALLOWED_PROVIDER_IDS:
        raise QualificationProviderGuardError(
            "qualification forbids external provider or unreviewed provider {!r}; "
            "allowed providers are codex_exec, fake_test_provider and mock".format(
                args.provider
            )
        )
    if int(args.workers) != 1:
        raise QualificationProviderGuardError(
            "model qualification requires --workers 1"
        )
    if args.provider != "codex_exec":
        return
    if args.dry_run:
        return
    if not str(args.model or "").strip():
        raise QualificationProviderGuardError(
            "codex_exec qualification requires an explicit --model"
        )
    if not str(args.reasoning_effort or "").strip():
        raise QualificationProviderGuardError(
            "codex_exec qualification requires an explicit --reasoning-effort"
        )
    if not args.confirm_real_codex_usage:
        raise QualificationProviderGuardError(
            "codex_exec requires --confirm-real-codex-usage"
        )
    if args.max_cases is None:
        raise QualificationProviderGuardError(
            "real codex_exec qualification requires an explicit --max-cases"
        )
    effective_max = int(selection.metadata["effective_max_cases"])
    confirmation = args.confirm_case_count
    if effective_max > 1 and confirmation != effective_max:
        raise QualificationProviderGuardError(
            "codex_exec --max-cases greater than 1 requires matching "
            "--confirm-case-count"
        )
    if confirmation is not None and int(confirmation) != effective_max:
        raise QualificationProviderGuardError(
            "--confirm-case-count must equal the effective --max-cases value"
        )


def _agents_by_persona(seed: int) -> dict[str, Agent]:
    cfg = Config(provider="mock", seed=seed, n_llm_agents=6, n_noise_agents=0)
    agents = make_agents(cfg)
    return {agent.persona_id: agent for agent in agents if agent.is_llm}


def _build_prompt(
    case: QualificationCase, agent: Agent, provider_id: str
) -> tuple[str, str]:
    fixture = case.fixture
    state = fixture["market_state"]
    social_feed = [
        (float(item["sentiment"]), str(item["public_take"]))
        for item in fixture["visible_social_feed"]
    ]
    social_mean = (
        sum(sentiment for sentiment, _text in social_feed) / len(social_feed)
        if social_feed
        else 0.0
    )
    agent.cash = float(fixture["cash"])
    agent.shares = int(fixture["shares"])
    agent.memory = list(fixture["memory"])
    return agent.build_prompt(
        "mock" if provider_id == "mock" else "real",
        int(fixture["round"]),
        float(state["latest_price"]),
        list(state["recent_prices"]),
        float(fixture["fundamental_value"]),
        fixture["visible_news"],
        social_feed=social_feed,
        social_mean=social_mean,
        effective_weight=agent.social_weight if social_feed else 0.0,
    )


def _json_object(raw: str) -> Optional[dict[str, Any]]:
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        return None
    try:
        value = json.loads(match.group(0))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _is_finite_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def evaluate_response(case: QualificationCase, raw: str) -> tuple[dict, dict]:
    """Return public-safe case data and a private record.

    The existing ``parse_order`` result is used unchanged.  Raw-field checks
    are parallel qualification diagnostics and never alter the parsed Decision.
    """

    fixture = case.fixture
    price = float(fixture["market_state"]["latest_price"])
    raw_object = _json_object(raw)
    parsed = parse_order(raw, price)
    raw_action = None if raw_object is None else raw_object.get(
        "action", raw_object.get("side")
    )
    raw_quantity = None if raw_object is None else raw_object.get("quantity")
    raw_sentiment = None if raw_object is None else raw_object.get("sentiment")
    raw_limit = None if raw_object is None else raw_object.get("limit_price")
    raw_public = None if raw_object is None else raw_object.get("public_take")
    raw_private = "" if raw_object is None else str(
        raw_object.get("reasoning") or raw_object.get("rationale") or ""
    )

    required_present = bool(
        raw_object is not None
        and _REQUIRED_DECISION_FIELDS.issubset(raw_object)
        and ("action" in raw_object or "side" in raw_object)
        and ("reasoning" in raw_object or "rationale" in raw_object)
    )
    invalid_action = raw_action not in {"buy", "sell", "hold"}
    invalid_quantity = (
        isinstance(raw_quantity, bool)
        or not isinstance(raw_quantity, int)
        or raw_quantity < 0
    )
    invalid_sentiment = (
        not _is_finite_number(raw_sentiment)
        or float(raw_sentiment) < -1.0
        or float(raw_sentiment) > 1.0
    )
    invalid_limit_price = not _is_finite_number(raw_limit)
    missing_public_take = not isinstance(raw_public, str) or not raw_public.strip()
    parse_success = parsed["rationale"] != "parse-failed; holding"
    fallback = parsed["rationale"] in _FALLBACK_MARKERS

    constraint_violations = []
    if parsed["side"] == "buy" and (
        parsed["quantity"] * parsed["limit_price"] > float(fixture["cash"])
    ):
        constraint_violations.append("insufficient_cash")
    if parsed["side"] == "sell" and parsed["quantity"] > int(fixture["shares"]):
        constraint_violations.append("insufficient_inventory")

    public_private_leakage = bool(
        raw_private.strip()
        and parsed["public_take"].strip()
        and raw_private.strip() == parsed["public_take"].strip()
    )
    side = parsed["side"]
    sentiment = float(parsed["sentiment"])
    sentiment_action_consistent = (
        side == "buy" and sentiment >= 0.0
        or side == "sell" and sentiment <= 0.0
        or side == "hold" and abs(sentiment) <= 0.25
    )
    validation_failure = any(
        (
            invalid_action,
            invalid_quantity,
            invalid_sentiment,
            invalid_limit_price,
            missing_public_take,
        )
    )
    public = {
        "case_id": case.case_id,
        "request_order": case.request_order,
        "persona_id": case.persona_id,
        "fixture_id": case.fixture_id,
        "fixture_input_hash": fixture["input_hash"],
        "status": "completed",
        "schema_success": required_present,
        "parse_success": parse_success,
        "validation_failure": validation_failure,
        "fallback": fallback,
        "action": side,
        "quantity": int(parsed["quantity"]),
        "limit_price": float(parsed["limit_price"]),
        "sentiment": sentiment,
        "public_take": parsed["public_take"],
        "private_text_present": bool(raw_private),
        "private_text_sha256": (
            hashlib.sha256(raw_private.encode("utf-8")).hexdigest()
            if raw_private
            else None
        ),
        "diagnostic_flags": {
            "invalid_action": invalid_action,
            "invalid_quantity": invalid_quantity,
            "invalid_sentiment": invalid_sentiment,
            "invalid_limit_price": invalid_limit_price,
            "missing_public_take": missing_public_take,
            "public_private_leakage": public_private_leakage,
            "constraint_violations": constraint_violations,
            "sentiment_action_consistent": sentiment_action_consistent,
        },
    }
    private = {
        "case_id": case.case_id,
        "persona_id": case.persona_id,
        "fixture_id": case.fixture_id,
        "raw_response": raw,
        "parsed_decision": dict(parsed),
    }
    return public, private


def _rate(numerator: int, denominator: int) -> Optional[float]:
    return round(numerator / denominator, 6) if denominator else None


def aggregate_results(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    completed = [result for result in results if result.get("status") == "completed"]
    total = len(completed)
    action_value = {"sell": -1, "hold": 0, "buy": 1}
    by_persona_actions: dict[str, Counter] = defaultdict(Counter)
    by_fixture_actions: dict[str, Counter] = defaultdict(Counter)
    persona_vectors: dict[str, list[str]] = defaultdict(list)
    sentiments_by_fixture_persona: dict[str, dict[str, float]] = defaultdict(dict)
    actions_by_fixture_persona: dict[str, dict[str, str]] = defaultdict(dict)
    for result in completed:
        persona = str(result["persona_id"])
        fixture = str(result["fixture_id"])
        action = str(result["action"])
        by_persona_actions[persona][action] += 1
        by_fixture_actions[fixture][action] += 1
        persona_vectors[persona].append(action)
        sentiments_by_fixture_persona[fixture][persona] = float(result["sentiment"])
        actions_by_fixture_persona[fixture][persona] = action

    def count_flag(flag: str) -> int:
        return sum(bool(result["diagnostic_flags"].get(flag)) for result in completed)

    schema_success = sum(bool(result["schema_success"]) for result in completed)
    parse_success = sum(bool(result["parse_success"]) for result in completed)
    validation_failures = sum(bool(result["validation_failure"]) for result in completed)
    fallbacks = sum(bool(result["fallback"]) for result in completed)
    constraint_count = sum(
        len(result["diagnostic_flags"]["constraint_violations"])
        for result in completed
    )
    consistent = count_flag("sentiment_action_consistent")
    persona_tendency = {}
    for persona, distribution in sorted(by_persona_actions.items()):
        denominator = sum(distribution.values())
        persona_tendency[persona] = round(
            sum(action_value[action] * count for action, count in distribution.items())
            / denominator,
            6,
        )

    unique_vectors = {tuple(vector) for vector in persona_vectors.values()}
    raw_distributions = {
        "global": dict(Counter(str(result["action"]) for result in completed)),
        "by_persona": {
            key: dict(value) for key, value in sorted(by_persona_actions.items())
        },
        "by_fixture": {
            key: dict(value) for key, value in sorted(by_fixture_actions.items())
        },
        "persona_action_vectors": {
            key: value for key, value in sorted(persona_vectors.items())
        },
    }
    panic = sentiments_by_fixture_persona.get("unanimous_neighbor_panic", {})
    discount = sentiments_by_fixture_persona.get("deep_discount_to_fundamental", {})
    discount_actions = actions_by_fixture_persona.get(
        "deep_discount_to_fundamental", {}
    )
    return {
        "engineering": {
            "schema_success_rate": _rate(schema_success, total),
            "parse_success_rate": _rate(parse_success, total),
            "validation_failure_rate": _rate(validation_failures, total),
            "fallback_rate": _rate(fallbacks, total),
            "public_private_leakage_count": count_flag("public_private_leakage"),
            "invalid_action_count": count_flag("invalid_action"),
            "invalid_quantity_count": count_flag("invalid_quantity"),
            "constraint_violation_count": constraint_count,
            "missing_public_take_count": count_flag("missing_public_take"),
            "provider_failure_count": sum(
                result.get("status") == "provider_failed" for result in results
            ),
            "honest_n_cases": total,
        },
        "behavioral_diagnostics": {
            "sentiment_action_consistency": _rate(consistent, total),
            "persona_tendency_score": persona_tendency,
            "social_response_score": {
                fixture: sentiments_by_fixture_persona.get(fixture, {})
                for fixture in (
                    "unanimous_neighbor_panic",
                    "conflicting_neighbor_views",
                )
            },
            "news_response_score": {
                fixture: sentiments_by_fixture_persona.get(fixture, {})
                for fixture in (
                    "negative_news_price_unchanged",
                    "neutral_placebo_news",
                )
            },
            "price_response_score": {
                fixture: sentiments_by_fixture_persona.get(fixture, {})
                for fixture in (
                    "price_crash_no_news",
                    "deep_discount_to_fundamental",
                )
            },
            "contrarian_response_score": {
                "contrarian_fund": panic.get("contrarian_fund"),
                "fomo_momentum": panic.get("fomo_momentum"),
                "interpretation": "relative_diagnostic",
            },
            "fundamental_anchor_score": {
                "status": "not_scored",
                "reason": "fundamental_anchor_not_visible",
                "score": None,
                "raw_evidence": {
                    persona: {
                        "sentiment": discount.get(persona),
                        "action": discount_actions.get(persona),
                    }
                    for persona in ("value_institution", "retail_crowd")
                },
                "interpretation": "raw_distribution_only",
            },
            "persona_distinctiveness": {
                "unique_action_vectors": len(unique_vectors),
                "persona_count": len(persona_vectors),
                "ratio": _rate(len(unique_vectors), len(persona_vectors)),
            },
            "same_model_persona_collapse": bool(persona_vectors)
            and len(unique_vectors) == 1,
            "raw_action_distributions": raw_distributions,
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


def _write_jsonl_exclusive(
    path: Path, rows: Iterable[Mapping[str, Any]], mode: int
) -> None:
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            for row in rows:
                stream.write(
                    json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
                )
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    os.chmod(path, mode)


def _safe_codex_metadata(value: Any) -> Optional[dict[str, Any]]:
    """Whitelist public-safe Codex metadata; never copy arbitrary adapter state."""

    if not isinstance(value, Mapping):
        return None
    return {
        key: value[key]
        for key in sorted(_PUBLIC_CODEX_METADATA_FIELDS)
        if key in value
    }


def _codex_static_identity(
    model: str, reasoning_effort: Optional[str]
) -> dict[str, Any]:
    """Build wrapper/schema/binary identity without probing auth or spawning Codex."""

    from nmsim.codex_exec import (
        codex_binary_from_environment,
        codex_static_adapter_identity,
    )

    return _safe_codex_metadata(
        codex_static_adapter_identity(
            binary=codex_binary_from_environment(),
            model=model or None,
            reasoning_effort=reasoning_effort or None,
        )
    ) or {}


def _case_provider_identity(metadata: Optional[Mapping[str, Any]]) -> Optional[dict]:
    """Return the intentionally small identity/error projection for public rows."""

    if not metadata:
        return None
    keys = (
        "final_response_hash",
        "reported_model",
        "actual_model_verification",
        "tool_use_violation_count",
        "tool_calls_observed",
        "error_code",
    )
    return {key: metadata.get(key) for key in keys if key in metadata}


def _build_provider(
    provider_id: str,
    seed: int,
    model: str = "",
    reasoning_effort: str = "",
):
    if provider_id == "mock":
        return MockLLM(seed=seed)
    if provider_id == "fake_test_provider":
        return FakeTestProvider(seed=seed)
    if provider_id == "codex_exec":
        # Deliberately lazy: --dry-run and rejected guard paths must neither
        # import nor construct the external CLI adapter.
        from nmsim.codex_exec import CodexExecLLM, codex_binary_from_environment

        return CodexExecLLM(
            model=model or "",
            reasoning_effort=reasoning_effort or "",
            binary=codex_binary_from_environment(),
        )
    raise QualificationProviderGuardError(
        "qualification forbids external or unreviewed provider {!r}".format(
            provider_id
        )
    )


def _initialise_manifest(
    manager: ManagedRunContext,
    *,
    args: argparse.Namespace,
    bundle: Mapping[str, Any],
    cases: Sequence[QualificationCase],
    selection: QualificationSelection,
    capability_snapshot: Optional[Mapping[str, Any]],
) -> None:
    resolved_model = (
        "fixture-defined-test-double-v1"
        if args.provider == "fake_test_provider"
        else "mock"
        if args.provider == "mock"
        else args.model or None
    )
    completion = manager.manifest["completion"]
    completion["simulation_runs"].update(
        {"planned": 0, "started": 0, "completed": 0, "failed": 0}
    )
    completion["rounds"].update(
        {"planned": 0, "started": 0, "completed": 0, "failed": 0, "skipped": 0}
    )
    completion["agent_decisions"].update(
        {
            "planned": len(cases),
            "attempted": 0,
            "completed": 0,
            "failed": 0,
            "skipped": len(cases),
        }
    )
    completion["llm_logical_requests"].update(
        {"planned": len(cases), "attempted": 0, "completed": 0, "failed": 0}
    )
    manager.manifest["qualification"] = {
        "output_schema_version": QUALIFICATION_OUTPUT_SCHEMA_VERSION,
        "protocol_version": bundle["protocol"]["protocol_version"],
        "protocol_hash": bundle["protocol_hash"],
        "observation_protocol_version": bundle["observations"]["protocol_version"],
        "fixture_set_hash": bundle["fixture_set_hash"],
        "rubric_version": bundle["rubric"]["rubric_version"],
        "rubric_hash": bundle["rubric_hash"],
        "visibility_contract_version": bundle["visibility_contract"][
            "visibility_contract_version"
        ],
        "visibility_contract_hash": bundle["visibility_contract_hash"],
        "persona_ids": list(bundle["protocol"]["persona_ids"]),
        "persona_count": len(bundle["protocol"]["persona_ids"]),
        "fixture_ids": list(bundle["protocol"]["fixture_ids"]),
        "fixture_count": len(bundle["observations"]["fixtures"]),
        "case_count": len(cases),
        "case_identity_hashes": [case.case_id for case in cases],
        "selection": dict(selection.metadata),
        "selection_hash": selection.metadata["selection_hash"],
        "qualification_scope": selection.metadata["qualification_scope"],
        "selected_persona_count": len(selection.metadata["selected_persona_ids"]),
        "selected_fixture_count": len(selection.metadata["selected_fixture_ids"]),
        "provider_requested": args.provider,
        "provider_resolved": args.provider if args.provider in ALLOWED_PROVIDER_IDS else None,
        "model_requested": args.model or None,
        "reasoning_effort_requested": args.reasoning_effort or None,
        "model_resolved": resolved_model,
        "provider_capability_snapshot": dict(capability_snapshot or {}),
        "provider_static_identity": {},
        "provider_call_metadata": [],
        "dry_run": bool(args.dry_run),
        "real_codex_usage_confirmed": bool(
            args.provider == "codex_exec" and args.confirm_real_codex_usage
        ),
        "workers": int(args.workers),
        "network_access": False,
        "provider_transport_network_expected": args.provider == "codex_exec",
        "provider_transport_network_declared_or_observed": (
            "declared_expected" if args.provider == "codex_exec" else "not_expected"
        ),
        "agent_tool_network_enabled": False,
        "tool_calls_observed": 0,
        "qualification_cases": {
            "unit": "qualification_cases",
            "planned": len(cases),
            "attempted": 0,
            "completed": 0,
            "failed": 0,
            "skipped": len(cases),
        },
        "honest_n_cases": 0,
        "is_simulation_run": False,
    }
    manager.manifest["honest_n_cases"] = 0
    manager.manifest["honest_n_runs"] = 0
    manager._write()


def _dry_run_summary(
    manager: ManagedRunContext,
    args: argparse.Namespace,
    bundle: Mapping[str, Any],
    cases: Sequence[QualificationCase],
    selection: QualificationSelection,
) -> dict[str, Any]:
    capability_snapshot = manager.manifest["qualification"].get(
        "provider_capability_snapshot", {}
    )
    capability = capability_snapshot.get("provider", capability_snapshot)
    return {
        "run_id": manager.run_id,
        "run_kind": "model_qualification",
        "dry_run": True,
        "case_count": len(cases),
        "persona_count": len(selection.metadata["selected_persona_ids"]),
        "fixture_count": len(selection.metadata["selected_fixture_ids"]),
        "full_protocol_case_count": selection.metadata["full_protocol_case_count"],
        "selection": dict(selection.metadata),
        "selection_hash": selection.metadata["selection_hash"],
        "qualification_scope": selection.metadata["qualification_scope"],
        "protocol_version": bundle["protocol"]["protocol_version"],
        "protocol_hash": bundle["protocol_hash"],
        "observation_protocol_version": bundle["observations"]["protocol_version"],
        "fixture_set_hash": bundle["fixture_set_hash"],
        "rubric_hash": bundle["rubric_hash"],
        "visibility_contract_version": bundle["visibility_contract"][
            "visibility_contract_version"
        ],
        "visibility_contract_hash": bundle["visibility_contract_hash"],
        "provider": args.provider,
        "model_requested": args.model or None,
        "reasoning_effort_requested": args.reasoning_effort or None,
        "model_resolved": manager.manifest["qualification"]["model_resolved"],
        "provider_static_identity": manager.manifest["qualification"].get(
            "provider_static_identity", {}
        ),
        "network_required": bool(capability.get("external_network_expected", False)),
        "estimated_logical_requests": len(cases),
        "provider_calls": 0,
        "network_access": False,
        "provider_transport_network_expected": args.provider == "codex_exec",
        "provider_transport_network_declared_or_observed": (
            "declared_expected" if args.provider == "codex_exec" else "not_expected"
        ),
        "agent_tool_network_enabled": False,
        "tool_calls_observed": 0,
        "output_directory": str(manager.run_dir),
    }


def run_qualification(
    manager: ManagedRunContext,
    *,
    provider_id: str,
    model: str,
    seed: int,
    bundle: Mapping[str, Any],
    cases: Sequence[QualificationCase],
    reasoning_effort: str = "",
) -> dict[str, Any]:
    external_network_expected = provider_id == "codex_exec"
    # Capability expectation and observed access are distinct.  Capability
    # probing/auth setup is not a model request; the adapter flips its observed
    # flag only when it actually starts an exec turn.
    manager.network_access = False
    provider = _build_provider(
        provider_id, seed, model, reasoning_effort=reasoning_effort
    )
    tracker = CostTracker()
    manager.active_llm = provider
    manager.tracker = tracker
    manager.llm_mode = "record"
    resolved_model = getattr(provider, "model", model or "mock")
    manager.manifest["qualification"]["model_resolved"] = resolved_model
    manager.manifest["qualification"]["network_access"] = False
    manager.manifest["qualification"][
        "provider_transport_network_declared_or_observed"
    ] = "declared_expected" if external_network_expected else "not_expected"
    manager.manifest["qualification"]["agent_tool_network_enabled"] = False
    identity_snapshot = getattr(provider, "identity_snapshot", None)
    if callable(identity_snapshot):
        manager.manifest["qualification"]["provider_runtime_identity"] = (
            _safe_codex_metadata(identity_snapshot()) or {}
        )
    manager.register_llm_runtime(
        provider=provider_id,
        model=resolved_model,
        mode="model_qualification",
        cache_enabled=False,
        network_access=False,
        provider_calls=0,
    )
    agents = _agents_by_persona(seed)
    public_results: list[dict[str, Any]] = []
    private_results: list[dict[str, Any]] = []
    qualification = manager.manifest["qualification"]["qualification_cases"]

    for case in cases:
        qualification["attempted"] += 1
        qualification["skipped"] = max(
            0, qualification["planned"] - qualification["attempted"]
        )
        system, user = _build_prompt(case, agents[case.persona_id], provider_id)
        set_request_identity = getattr(provider, "set_request_identity", None)
        if callable(set_request_identity):
            set_request_identity(run_id=manager.run_id, agent_id=case.persona_id)
        prompt_hash = hashlib.sha256(
            (system + "\0" + user).encode("utf-8")
        ).hexdigest()
        manager.events.emit(
            "LLMRequestRecorded",
            agent_id=case.persona_id,
            data={
                "case_id": case.case_id,
                "fixture_id": case.fixture_id,
                "request_order": case.request_order,
                "prompt_hash": prompt_hash,
                "run_kind": "model_qualification",
            },
            private_data={"system_prompt": system, "user_prompt": user},
        )
        started = time.perf_counter()
        try:
            raw = provider.complete(system, user)
        except Exception as error:
            elapsed_ms = round((time.perf_counter() - started) * 1000.0, 3)
            qualification["failed"] += 1
            provider_metadata = _safe_codex_metadata(
                getattr(provider, "last_call_metadata", None)
            )
            if provider_metadata is not None:
                manager.manifest["qualification"]["provider_call_metadata"].append(
                    provider_metadata
                )
            failed = {
                "case_id": case.case_id,
                "request_order": case.request_order,
                "persona_id": case.persona_id,
                "fixture_id": case.fixture_id,
                "status": "provider_failed",
                "provider_error_type": type(error).__name__,
                "provider_error_code": getattr(error, "code", None),
                "provider_identity": _case_provider_identity(provider_metadata),
                "latency_ms": elapsed_ms,
            }
            public_results.append(failed)
            private_results.append(
                {
                    **failed,
                    "provider_error_detail": str(error),
                    "provider_call_metadata": provider_metadata,
                }
            )
            manager.events.emit(
                "QualificationCaseFailed",
                agent_id=case.persona_id,
                data={
                    "case_id": case.case_id,
                    "fixture_id": case.fixture_id,
                    "provider_error_type": type(error).__name__,
                    "provider_error_code": getattr(error, "code", None),
                    "provider_call_metadata": provider_metadata,
                },
                private_data={"provider_error_detail": str(error)},
            )
            if provider_id == "codex_exec":
                # Codex is an agentic CLI.  Any adapter failure (especially a
                # tool event) invalidates the managed pilot rather than being
                # aggregated into an apparently successful qualification.
                qualification["skipped"] = max(
                    0,
                    qualification["planned"]
                    - qualification["completed"]
                    - qualification["failed"],
                )
                manager.manifest["qualification"]["honest_n_cases"] = (
                    qualification["completed"]
                )
                manager.manifest["qualification"]["failure"] = dict(failed)
                manager.manifest["honest_n_cases"] = qualification["completed"]
                manager.manifest["honest_n_runs"] = 0
                manager.sync_llm_accounting(provider, tracker)
                manager.manifest["qualification"]["network_access"] = bool(
                    getattr(provider, "network_access", False)
                )
                manager.manifest["qualification"][
                    "provider_transport_network_declared_or_observed"
                ] = (
                    "process_started_network_not_observed"
                    if getattr(provider, "model_turn_process_started", False)
                    else "declared_expected"
                    if external_network_expected
                    else "not_expected"
                )
                manager.manifest["qualification"]["tool_calls_observed"] = sum(
                    int(item.get("tool_calls_observed", 0))
                    for item in manager.manifest["qualification"][
                        "provider_call_metadata"
                    ]
                )
                manager._write()
                raise
            continue
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 3)
        response_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        provider_metadata = _safe_codex_metadata(
            getattr(provider, "last_call_metadata", None)
        )
        if provider_metadata is not None:
            manager.manifest["qualification"]["provider_call_metadata"].append(
                provider_metadata
            )
        manager.events.emit(
            "LLMResponseRecorded",
            agent_id=case.persona_id,
            data={
                "case_id": case.case_id,
                "fixture_id": case.fixture_id,
                "source": "provider",
                "response_hash": response_hash,
                "provider_call_metadata": provider_metadata,
            },
            private_data={"raw_response": raw},
        )
        public, private = evaluate_response(case, raw)
        public.update(
            {
                "prompt_hash": prompt_hash,
                "prompt_variant": (
                    "mock_agent_prompt_v1"
                    if provider_id == "mock"
                    else "real_agent_prompt_v1"
                ),
                "source_fixture_hash": case.fixture["input_hash"],
                "visibility_contract_hash": bundle["visibility_contract_hash"],
                "provider_identity": _case_provider_identity(provider_metadata),
            }
        )
        public["latency_ms"] = elapsed_ms
        private.update(
            {
                "system_prompt": system,
                "user_prompt": user,
                "prompt_hash": prompt_hash,
                "response_hash": response_hash,
                "latency_ms": elapsed_ms,
                "provider_call_metadata": provider_metadata,
            }
        )
        public_results.append(public)
        private_results.append(private)
        manager.events.emit(
            "AgentDecisionParsed",
            agent_id=case.persona_id,
            data={
                "case_id": case.case_id,
                "fixture_id": case.fixture_id,
                "parse_status": "ok" if public["parse_success"] else "error",
                "action": public["action"],
                "quantity": public["quantity"],
                "limit_price": public["limit_price"],
                "sentiment": public["sentiment"],
                "public_take": public["public_take"],
                "provider_call_metadata": provider_metadata,
            },
            private_data={"private_rationale": private["parsed_decision"]["rationale"]},
        )
        qualification["completed"] += 1

    qualification["skipped"] = max(
        0,
        qualification["planned"]
        - qualification["completed"]
        - qualification["failed"],
    )
    manager.manifest["qualification"]["honest_n_cases"] = qualification["completed"]
    manager.manifest["honest_n_cases"] = qualification["completed"]
    manager.manifest["honest_n_runs"] = 0
    manager.sync_llm_accounting(provider, tracker)
    actual_network_access = bool(getattr(provider, "network_access", False))
    manager.manifest["qualification"]["network_access"] = actual_network_access
    manager.manifest["qualification"][
        "provider_transport_network_declared_or_observed"
    ] = (
        "process_started_network_not_observed"
        if getattr(provider, "model_turn_process_started", False)
        else "declared_expected"
        if external_network_expected
        else "not_expected"
    )
    manager.manifest["qualification"]["tool_calls_observed"] = sum(
        int(item.get("tool_calls_observed", 0))
        for item in manager.manifest["qualification"]["provider_call_metadata"]
    )
    aggregate = aggregate_results(public_results)
    aggregate["engineering"]["provider_failure_count"] = qualification["failed"]
    summary = {
        "output_schema_version": QUALIFICATION_OUTPUT_SCHEMA_VERSION,
        "run_id": manager.run_id,
        "run_kind": "model_qualification",
        "protocol_version": bundle["protocol"]["protocol_version"],
        "protocol_hash": bundle["protocol_hash"],
        "observation_protocol_version": bundle["observations"]["protocol_version"],
        "fixture_set_hash": bundle["fixture_set_hash"],
        "rubric_version": bundle["rubric"]["rubric_version"],
        "rubric_hash": bundle["rubric_hash"],
        "visibility_contract_version": bundle["visibility_contract"][
            "visibility_contract_version"
        ],
        "visibility_contract_hash": bundle["visibility_contract_hash"],
        "selection": dict(manager.manifest["qualification"]["selection"]),
        "selection_hash": manager.manifest["qualification"]["selection_hash"],
        "qualification_scope": manager.manifest["qualification"][
            "qualification_scope"
        ],
        "provider": provider_id,
        "model_requested": manager.manifest["qualification"]["model_requested"],
        "reasoning_effort_requested": manager.manifest["qualification"][
            "reasoning_effort_requested"
        ],
        "model_resolved": manager.manifest["qualification"]["model_resolved"],
        "provider_capability_snapshot": manager.manifest["qualification"][
            "provider_capability_snapshot"
        ],
        "completion": {
            "qualification_cases": dict(qualification),
            "llm_logical_requests": dict(
                manager.manifest["completion"]["llm_logical_requests"]
            ),
            "agent_decisions": dict(
                manager.manifest["completion"]["agent_decisions"]
            ),
            "provider_calls": dict(manager.manifest["completion"]["provider_calls"]),
            "response_sources": dict(
                manager.manifest["completion"]["response_sources"]
            ),
            "simulation_runs": dict(
                manager.manifest["completion"]["simulation_runs"]
            ),
            "rounds": dict(manager.manifest["completion"]["rounds"]),
        },
        "honest_n_cases": qualification["completed"],
        "honest_n_runs": 0,
        "network_access": actual_network_access,
        "external_network_expected": external_network_expected,
        "provider_transport_network_expected": external_network_expected,
        "provider_transport_network_declared_or_observed": manager.manifest[
            "qualification"
        ]["provider_transport_network_declared_or_observed"],
        "agent_tool_network_enabled": False,
        "tool_calls_observed": manager.manifest["qualification"][
            "tool_calls_observed"
        ],
        "token_usage": (
            dict(getattr(provider, "usage_totals"))
            if isinstance(getattr(provider, "usage_totals", None), Mapping)
            else None
        ),
        "metrics": aggregate,
        "case_results_file": "case_results.jsonl",
        "private_case_records_file": "private_case_records.jsonl",
    }
    return {
        "summary": summary,
        "public_results": public_results,
        "private_results": private_results,
    }


def build_argparser() -> argparse.ArgumentParser:
    parser = RaisingArgumentParser(allow_abbrev=False)
    parser.add_argument("--version", action="version", version="model-qualification 1.1")
    parser.add_argument("--provider", default="mock")
    parser.add_argument("--model", default="")
    parser.add_argument("--reasoning-effort", default="")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--case-id", action="append", default=[])
    parser.add_argument("--fixture-id", action="append", default=[])
    parser.add_argument("--persona-id", action="append", default=[])
    parser.add_argument("--max-cases", type=int, default=None)
    parser.add_argument("--confirm-real-codex-usage", action="store_true")
    parser.add_argument("--confirm-case-count", type=int, default=None)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--out", default="qualification_results")
    parser.add_argument("--run-id", default=None)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> None:
    args_list = list(sys.argv[1:] if argv is None else argv)
    try:
        bootstrap = bootstrap_cli(
            args_list,
            default_out="qualification_results",
            command_identity="python -m experiments.model_qualification",
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
        if args.provider == "codex_exec":
            from nmsim.codex_exec import (
                validate_codex_model_identity,
                validate_codex_reasoning_effort,
            )

            if str(args.model or "").strip():
                args.model = validate_codex_model_identity(args.model)
            if str(args.reasoning_effort or "").strip():
                args.reasoning_effort = validate_codex_reasoning_effort(
                    args.reasoning_effort
                )
        bundle = load_protocol_bundle()
        all_cases = build_cases(bundle)
        selection = select_cases(
            bundle,
            all_cases,
            provider_id=args.provider,
            case_ids=args.case_id,
            fixture_ids=args.fixture_id,
            persona_ids=args.persona_id,
            max_cases=args.max_cases,
        )
        cases = list(selection.cases)
    except (ManagedCLIError, OSError, QualificationProtocolError, ValueError) as error:
        fail_cli(bootstrap, error, failure_stage="config_validation")

    cfg = Config(
        provider=args.provider,
        model=args.model,
        seed=args.seed,
        n_llm_agents=6,
        n_noise_agents=0,
        cache_enabled=False,
        out_dir=args.out,
    )
    if args.provider == "codex_exec":
        cfg.codex_reasoning_effort = args.reasoning_effort or None
    manager = ManagedRunContext.create(
        cfg,
        out_root=args.out,
        scenario_id="model-qualification:{}".format(
            bundle["protocol"]["protocol_version"]
        ),
        run_id=args.run_id,
        input_paths={
            "qualification_protocol": PROTOCOL_PATH,
            "qualification_observations": OBSERVATIONS_PATH,
            "qualification_rubric": RUBRIC_PATH,
            "qualification_visibility_contract": VISIBILITY_CONTRACT_PATH,
        },
        command_identity="python -m experiments.model_qualification",
        run_kind="model_qualification",
        planned_simulation_runs=0,
    )
    try:
        with manager:
            manager.set_stage("provider_setup")
            try:
                _validate_provider_execution_guard(args, selection)
            except QualificationProviderGuardError:
                _initialise_manifest(
                    manager,
                    args=args,
                    bundle=bundle,
                    cases=cases,
                    selection=selection,
                    capability_snapshot=None,
                )
                raise
            capability = provider_capability_snapshot(
                args.provider,
                model=args.model or None,
                reasoning_effort=args.reasoning_effort or None,
            )
            _initialise_manifest(
                manager,
                args=args,
                bundle=bundle,
                cases=cases,
                selection=selection,
                capability_snapshot=capability,
            )
            if args.provider == "codex_exec":
                manager.manifest["qualification"]["provider_static_identity"] = (
                    _codex_static_identity(args.model, args.reasoning_effort or None)
                )
                manager._write()
            preliminary_model = (
                args.model
                or (
                    "fixture-defined-test-double-v1"
                    if args.provider == "fake_test_provider"
                    else "mock"
                    if args.provider == "mock"
                    else None
                )
            )
            manager.register_llm_runtime(
                provider=args.provider,
                model=preliminary_model,
                mode="qualification_dry_run" if args.dry_run else "model_qualification",
                cache_enabled=False,
                network_access=False,
                provider_calls=0,
            )
            manager.set_stage("result_export" if args.dry_run else "provider_setup")
            if args.dry_run:
                summary = _dry_run_summary(
                    manager, args, bundle, cases, selection
                )
                _write_json_exclusive(manager.run_dir / "dry_run_summary.json", summary)
                manager.finish()
                print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
                return

            payload = run_qualification(
                manager,
                provider_id=args.provider,
                model=args.model,
                reasoning_effort=args.reasoning_effort,
                seed=args.seed,
                bundle=bundle,
                cases=cases,
            )
            manager.set_stage("result_export")
            _write_jsonl_exclusive(
                manager.run_dir / "case_results.jsonl",
                payload["public_results"],
                0o644,
            )
            _write_jsonl_exclusive(
                manager.run_dir / "private_case_records.jsonl",
                payload["private_results"],
                0o600,
            )
            _write_json_exclusive(
                manager.run_dir / "qualification_summary.json",
                payload["summary"],
            )
            manager.finish()
            print(json.dumps(payload["summary"], ensure_ascii=False, sort_keys=True))
    except QualificationProviderGuardError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2)
    except Exception as error:
        if args.provider == "codex_exec":
            code = getattr(error, "code", type(error).__name__)
            print("codex_exec qualification failed: {}".format(code), file=sys.stderr)
            raise SystemExit(1) from None
        raise


if __name__ == "__main__":
    main()


__all__ = [
    "ALLOWED_PROVIDER_IDS",
    "FakeTestProvider",
    "OBSERVATIONS_PATH",
    "PROTOCOL_PATH",
    "QUALIFICATION_OUTPUT_SCHEMA_VERSION",
    "QUALIFICATION_SELECTION_SCHEMA_VERSION",
    "QualificationCase",
    "QualificationSelection",
    "QualificationProtocolError",
    "QualificationProviderGuardError",
    "RUBRIC_PATH",
    "VISIBILITY_CONTRACT_PATH",
    "aggregate_results",
    "build_argparser",
    "build_cases",
    "evaluate_response",
    "fixture_input_hash",
    "fixture_set_hash",
    "load_protocol_bundle",
    "main",
    "run_qualification",
    "select_cases",
    "stable_json_hash",
]
