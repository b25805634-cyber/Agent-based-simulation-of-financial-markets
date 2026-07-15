"""Offline-safe model qualification protocol for Phase 1.2A.

This is a managed experiment entrypoint, but it is deliberately not a market
simulation.  It applies the existing Agent prompt builders and Decision parser
to a frozen matrix of observations.  Phase 1.2A permits only ``MockLLM`` and an
in-process deterministic test double; every external provider is rejected
before provider construction.

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
ALLOWED_PROVIDER_IDS = frozenset({"mock", "fake_test_provider"})
QUALIFICATION_OUTPUT_SCHEMA_VERSION = "1.0"
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


class QualificationProtocolError(ValueError):
    """A version-controlled qualification input violates its contract."""


class QualificationProviderGuardError(ValueError):
    """Phase 1.2A rejected an external or unreviewed provider before I/O."""


@dataclass(frozen=True)
class QualificationCase:
    case_id: str
    request_order: int
    persona_id: str
    fixture_id: str
    fixture: Mapping[str, Any]


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


def load_protocol_bundle(
    *,
    protocol_path: Path = PROTOCOL_PATH,
    observations_path: Path = OBSERVATIONS_PATH,
    rubric_path: Path = RUBRIC_PATH,
) -> dict[str, Any]:
    protocol = _read_json(protocol_path)
    observations = _read_json(observations_path)
    rubric = _read_json(rubric_path)
    version = str(protocol.get("protocol_version", ""))
    if not version or observations.get("protocol_version") != version:
        raise QualificationProtocolError("observation protocol version mismatch")
    if rubric.get("protocol_version") != version:
        raise QualificationProtocolError("rubric protocol version mismatch")
    fixtures = observations.get("fixtures")
    if not isinstance(fixtures, list):
        raise QualificationProtocolError("observations.fixtures must be a list")
    fixture_ids = [str(fixture.get("fixture_id", "")) for fixture in fixtures]
    if not fixture_ids or len(fixture_ids) != len(set(fixture_ids)):
        raise QualificationProtocolError("fixture ids must be non-empty and unique")
    if sorted(fixture_ids) != sorted(protocol.get("fixture_ids", [])):
        raise QualificationProtocolError("protocol and observation fixture ids differ")
    for fixture in fixtures:
        if fixture.get("protocol_version") != version:
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
    return {
        "protocol": protocol,
        "observations": observations,
        "rubric": rubric,
        "protocol_hash": stable_json_hash(protocol),
        "fixture_set_hash": fixture_set_hash(fixtures),
        "rubric_hash": stable_json_hash(rubric),
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
    for result in completed:
        persona = str(result["persona_id"])
        fixture = str(result["fixture_id"])
        action = str(result["action"])
        by_persona_actions[persona][action] += 1
        by_fixture_actions[fixture][action] += 1
        persona_vectors[persona].append(action)
        sentiments_by_fixture_persona[fixture][persona] = float(result["sentiment"])

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
                "value_institution": discount.get("value_institution"),
                "retail_crowd": discount.get("retail_crowd"),
                "interpretation": "relative_diagnostic",
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


def _build_provider(provider_id: str, seed: int):
    if provider_id == "mock":
        return MockLLM(seed=seed)
    if provider_id == "fake_test_provider":
        return FakeTestProvider(seed=seed)
    raise QualificationProviderGuardError(
        "Phase 1.2A qualification forbids external provider {!r}".format(provider_id)
    )


def _initialise_manifest(
    manager: ManagedRunContext,
    *,
    args: argparse.Namespace,
    bundle: Mapping[str, Any],
    cases: Sequence[QualificationCase],
    capability_snapshot: Optional[Mapping[str, Any]],
) -> None:
    resolved_model = (
        "fixture-defined-test-double-v1"
        if args.provider == "fake_test_provider"
        else "mock"
        if args.provider == "mock"
        else None
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
        "fixture_set_hash": bundle["fixture_set_hash"],
        "rubric_version": bundle["rubric"]["rubric_version"],
        "rubric_hash": bundle["rubric_hash"],
        "persona_ids": list(bundle["protocol"]["persona_ids"]),
        "persona_count": len(bundle["protocol"]["persona_ids"]),
        "fixture_ids": list(bundle["protocol"]["fixture_ids"]),
        "fixture_count": len(bundle["observations"]["fixtures"]),
        "case_count": len(cases),
        "case_identity_hashes": [case.case_id for case in cases],
        "provider_requested": args.provider,
        "provider_resolved": args.provider if args.provider in ALLOWED_PROVIDER_IDS else None,
        "model_requested": args.model or None,
        "model_resolved": resolved_model,
        "provider_capability_snapshot": dict(capability_snapshot or {}),
        "dry_run": bool(args.dry_run),
        "network_access": False,
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
) -> dict[str, Any]:
    return {
        "run_id": manager.run_id,
        "run_kind": "model_qualification",
        "dry_run": True,
        "case_count": len(cases),
        "persona_count": len(bundle["protocol"]["persona_ids"]),
        "fixture_count": len(bundle["observations"]["fixtures"]),
        "protocol_version": bundle["protocol"]["protocol_version"],
        "protocol_hash": bundle["protocol_hash"],
        "fixture_set_hash": bundle["fixture_set_hash"],
        "rubric_hash": bundle["rubric_hash"],
        "provider": args.provider,
        "model_requested": args.model or None,
        "model_resolved": manager.manifest["qualification"]["model_resolved"],
        "network_required": False,
        "estimated_logical_requests": len(cases),
        "provider_calls": 0,
        "network_access": False,
        "output_directory": str(manager.run_dir),
    }


def run_qualification(
    manager: ManagedRunContext,
    *,
    provider_id: str,
    seed: int,
    bundle: Mapping[str, Any],
    cases: Sequence[QualificationCase],
) -> dict[str, Any]:
    provider = _build_provider(provider_id, seed)
    tracker = CostTracker()
    manager.active_llm = provider
    manager.tracker = tracker
    manager.llm_mode = "record"
    manager.network_access = False
    manager.register_llm_runtime(
        provider=provider_id,
        model=getattr(provider, "model", "mock"),
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
            failed = {
                "case_id": case.case_id,
                "request_order": case.request_order,
                "persona_id": case.persona_id,
                "fixture_id": case.fixture_id,
                "status": "provider_failed",
                "provider_error_type": type(error).__name__,
                "latency_ms": elapsed_ms,
            }
            public_results.append(failed)
            private_results.append({**failed, "provider_error_detail": str(error)})
            manager.events.emit(
                "QualificationCaseFailed",
                agent_id=case.persona_id,
                data={
                    "case_id": case.case_id,
                    "fixture_id": case.fixture_id,
                    "provider_error_type": type(error).__name__,
                },
                private_data={"provider_error_detail": str(error)},
            )
            continue
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 3)
        response_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        manager.events.emit(
            "LLMResponseRecorded",
            agent_id=case.persona_id,
            data={
                "case_id": case.case_id,
                "fixture_id": case.fixture_id,
                "source": "provider",
                "response_hash": response_hash,
            },
            private_data={"raw_response": raw},
        )
        public, private = evaluate_response(case, raw)
        public["latency_ms"] = elapsed_ms
        private.update(
            {
                "system_prompt": system,
                "user_prompt": user,
                "prompt_hash": prompt_hash,
                "response_hash": response_hash,
                "latency_ms": elapsed_ms,
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
    aggregate = aggregate_results(public_results)
    aggregate["engineering"]["provider_failure_count"] = qualification["failed"]
    summary = {
        "output_schema_version": QUALIFICATION_OUTPUT_SCHEMA_VERSION,
        "run_id": manager.run_id,
        "run_kind": "model_qualification",
        "protocol_version": bundle["protocol"]["protocol_version"],
        "protocol_hash": bundle["protocol_hash"],
        "fixture_set_hash": bundle["fixture_set_hash"],
        "rubric_version": bundle["rubric"]["rubric_version"],
        "rubric_hash": bundle["rubric_hash"],
        "provider": provider_id,
        "model_requested": manager.manifest["qualification"]["model_requested"],
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
        "network_access": False,
        "token_usage": None,
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
    parser.add_argument("--version", action="version", version="model-qualification 1.0")
    parser.add_argument("--provider", default="mock")
    parser.add_argument("--model", default="")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--dry-run", action="store_true")
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
        bundle = load_protocol_bundle()
        cases = build_cases(bundle)
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
        },
        command_identity="python -m experiments.model_qualification",
        run_kind="model_qualification",
        planned_simulation_runs=0,
    )
    try:
        with manager:
            manager.set_stage("provider_setup")
            if args.provider not in ALLOWED_PROVIDER_IDS:
                _initialise_manifest(
                    manager,
                    args=args,
                    bundle=bundle,
                    cases=cases,
                    capability_snapshot=None,
                )
                raise QualificationProviderGuardError(
                    "Phase 1.2A qualification forbids external provider {!r}; "
                    "allowed providers are fake_test_provider and mock".format(
                        args.provider
                    )
                )
            capability = provider_capability_snapshot(args.provider)
            _initialise_manifest(
                manager,
                args=args,
                bundle=bundle,
                cases=cases,
                capability_snapshot=capability,
            )
            manager.register_llm_runtime(
                provider=args.provider,
                model=args.model or (
                    "fixture-defined-test-double-v1"
                    if args.provider == "fake_test_provider"
                    else "mock"
                ),
                mode="qualification_dry_run" if args.dry_run else "model_qualification",
                cache_enabled=False,
                network_access=False,
                provider_calls=0,
            )
            manager.set_stage("result_export" if args.dry_run else "provider_setup")
            if args.dry_run:
                summary = _dry_run_summary(manager, args, bundle, cases)
                _write_json_exclusive(manager.run_dir / "dry_run_summary.json", summary)
                manager.finish()
                print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
                return

            payload = run_qualification(
                manager,
                provider_id=args.provider,
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


if __name__ == "__main__":
    main()


__all__ = [
    "ALLOWED_PROVIDER_IDS",
    "FakeTestProvider",
    "OBSERVATIONS_PATH",
    "PROTOCOL_PATH",
    "QUALIFICATION_OUTPUT_SCHEMA_VERSION",
    "QualificationCase",
    "QualificationProtocolError",
    "QualificationProviderGuardError",
    "RUBRIC_PATH",
    "aggregate_results",
    "build_argparser",
    "build_cases",
    "evaluate_response",
    "fixture_input_hash",
    "fixture_set_hash",
    "load_protocol_bundle",
    "main",
    "run_qualification",
    "stable_json_hash",
]
