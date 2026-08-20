"""Managed V2 Teacher -> Student -> conserving-market engineering pipeline.

The default provider is an offline, deterministic test double.  A real
OpenAI-compatible endpoint is fail-closed behind ``--provider openai --live``
and an exact request-count confirmation.  This module never calls the legacy
Persona market or changes its Config/schema semantics.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import asdict, dataclass, is_dataclass
import hashlib
import html
import json
import math
import os
from pathlib import Path
import random
import re
import statistics
import sys
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from nmsim.config import Config
from nmsim.managed_cli import (
    BootstrapCLIError,
    ManagedCLIError,
    RaisingArgumentParser,
    bootstrap_cli,
    fail_cli,
)
from nmsim.provider_attempts import safe_reported_model, sha256_text
from nmsim.run_context import ManagedRunContext


COMMAND_IDENTITY = "experiments.v2_attention_market"
DEFAULT_OUTPUT_ROOT = "results_v2_attention_market"
RUN_KIND = "v2_attention_market"
OUTPUT_SCHEMA_VERSION = "v2_attention_market_result/0.1"
PROTOCOL_VERSION = "v2_attention_market/0.1"
MODEL_REQUEST_SCHEMA_VERSION = "v2_teacher_request/0.1"
FINISH_AUDIT_REQUEST_SCHEMA_VERSION = "v2_teacher_request/0.2"
SAMPLE_IDENTITY_SCHEMA_VERSION = "v2_teacher_request/0.1"
EXECUTION_SCHEMA_VERSION = "v2_attention_execution/0.1"
TRANSPORT_AUDIT_EXECUTION_SCHEMA_VERSION = "v2_attention_execution/0.2"
FULL_CONFIG_SCHEMA_VERSION = "v2_attention_full_effective_config/0.1"
ALLOWED_PROVIDERS = frozenset({"fake_test_teacher", "fake_null_teacher", "openai"})
MINIMAX_M27_JOINT54X3_PILOT = "minimax_m27_joint54x3_v1"
MINIMAX_M27_REQUEST_HIGGSAI_REPORTED_JOINT54X3_PILOT = (
    "minimax_m27_request_higgsai_reported_joint54x3_v2"
)
MINIMAX_M27_HIGGSAI_FINISH_AUDIT_JOINT54X3_PILOT = (
    "minimax_m27_higgsai_finish_audit_joint54x3_v3"
)
MINIMAX_M27_HIGGSAI_FINISH_AUDIT_EXTERNAL_JOINT54X3_PILOT = (
    "minimax_m27_higgsai_finish_audit_external_joint54x3_v4"
)
MINIMAX_M27_HIGGSAI_LONG_TIMEOUT_JOINT54X3_PILOT = (
    "minimax_m27_higgsai_finish_audit_long_timeout_joint54x3_v5"
)
MINIMAX_M27_HIGGSAI_TIMEOUT600_OUTPUT16384_JOINT54X3_PILOT = (
    "minimax_m27_higgsai_finish_audit_timeout600_output16384_joint54x3_v6"
)
DEFAULT_REQUEST_TIMEOUT_SECONDS = 120.0
V5_REQUEST_TIMEOUT_SECONDS = 600.0
PROVIDER_CONNECT_TIMEOUT_SECONDS = 10.0
PILOT_PROFILES = frozenset(
    {
        MINIMAX_M27_JOINT54X3_PILOT,
        MINIMAX_M27_REQUEST_HIGGSAI_REPORTED_JOINT54X3_PILOT,
        MINIMAX_M27_HIGGSAI_FINISH_AUDIT_JOINT54X3_PILOT,
        MINIMAX_M27_HIGGSAI_FINISH_AUDIT_EXTERNAL_JOINT54X3_PILOT,
        MINIMAX_M27_HIGGSAI_LONG_TIMEOUT_JOINT54X3_PILOT,
        MINIMAX_M27_HIGGSAI_TIMEOUT600_OUTPUT16384_JOINT54X3_PILOT,
    }
)
SPLIT_FRACTIONS = (0.70, 0.15, 0.15)
TAPE_DECLINE_UPPER_EXCLUSIVE = -0.10
TAPE_RISE_LOWER_EXCLUSIVE = 0.10
POSITION_CASH_UPPER_EXCLUSIVE = 0.20
POSITION_INVESTED_LOWER_EXCLUSIVE = 0.80
JOINT_SPLIT_REQUIRED_STRATA = 9
JOINT_SPLIT_MIN_FAMILIES_PER_STRATUM = 5
_PUBLIC_MODEL_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,255}$")
_PUBLIC_FINISH_REASONS = frozenset(
    {"stop", "length", "content_filter", "tool_calls", "function_call"}
)
_SECRET_ENDPOINT_QUERY_KEY_RE = re.compile(
    r"(?:api[_-]?key|access[_-]?token|auth(?:orization)?|bearer|credential|password|secret|signature|sig|token)",
    re.IGNORECASE,
)


class V2ProtocolError(ValueError):
    """The V2 protocol, state design, or artifact contract is invalid."""


class V2ProviderGuardError(ValueError):
    """A real Provider was requested without the exact explicit guard."""


class V2TeacherGateError(RuntimeError):
    """A frozen live-pilot Teacher acceptance condition failed."""


def pilot_profile_descriptor(profile_id: Optional[str]) -> Optional[dict[str, Any]]:
    """Return the frozen, opt-in live-pilot contract without endpoint secrets."""

    if profile_id is None:
        return None
    if profile_id not in PILOT_PROFILES:
        raise V2ProviderGuardError("unknown --pilot-profile")
    higgs_alias_profile = profile_id in {
        MINIMAX_M27_REQUEST_HIGGSAI_REPORTED_JOINT54X3_PILOT,
        MINIMAX_M27_HIGGSAI_FINISH_AUDIT_JOINT54X3_PILOT,
        MINIMAX_M27_HIGGSAI_FINISH_AUDIT_EXTERNAL_JOINT54X3_PILOT,
        MINIMAX_M27_HIGGSAI_LONG_TIMEOUT_JOINT54X3_PILOT,
        MINIMAX_M27_HIGGSAI_TIMEOUT600_OUTPUT16384_JOINT54X3_PILOT,
    }
    finish_audit_profile = profile_id in {
        MINIMAX_M27_HIGGSAI_FINISH_AUDIT_JOINT54X3_PILOT,
        MINIMAX_M27_HIGGSAI_FINISH_AUDIT_EXTERNAL_JOINT54X3_PILOT,
        MINIMAX_M27_HIGGSAI_LONG_TIMEOUT_JOINT54X3_PILOT,
        MINIMAX_M27_HIGGSAI_TIMEOUT600_OUTPUT16384_JOINT54X3_PILOT,
    }
    external_execution_successor_profile = profile_id in {
        MINIMAX_M27_HIGGSAI_FINISH_AUDIT_EXTERNAL_JOINT54X3_PILOT,
        MINIMAX_M27_HIGGSAI_LONG_TIMEOUT_JOINT54X3_PILOT,
        MINIMAX_M27_HIGGSAI_TIMEOUT600_OUTPUT16384_JOINT54X3_PILOT,
    }
    long_timeout_execution_successor_profile = profile_id in {
        MINIMAX_M27_HIGGSAI_LONG_TIMEOUT_JOINT54X3_PILOT,
        MINIMAX_M27_HIGGSAI_TIMEOUT600_OUTPUT16384_JOINT54X3_PILOT,
    }
    output_budget_successor_profile = (
        profile_id == MINIMAX_M27_HIGGSAI_TIMEOUT600_OUTPUT16384_JOINT54X3_PILOT
    )
    required_reported_model = (
        "HiggsAI"
        if higgs_alias_profile
        else "MiniMax-M2.7"
    )
    descriptor = {
        "schema_version": (
            "v2_teacher_pilot_profile/0.6"
            if output_budget_successor_profile
            else (
                "v2_teacher_pilot_profile/0.5"
                if long_timeout_execution_successor_profile
                else (
                    "v2_teacher_pilot_profile/0.4"
                    if external_execution_successor_profile
                    else (
                        "v2_teacher_pilot_profile/0.3"
                        if finish_audit_profile
                        else (
                            "v2_teacher_pilot_profile/0.2"
                            if higgs_alias_profile
                            else "v2_teacher_pilot_profile/0.1"
                        )
                    )
                )
            )
        ),
        "profile_id": profile_id,
        "purpose": "exploratory_endpoint_teacher_not_human_ground_truth",
        "provider": "openai",
        "model_requested": "MiniMax-M2.7",
        "required_reported_model": required_reported_model,
        "endpoint_identity_sha256": "66e21f44b31bae951b37de32684b004d81c0821d956eb3351432770f11aad0c1",
        "states": 54,
        "replicates_per_state": 3,
        "planned_requests": 162,
        "seed": 20260811,
        "temperature": 0.0,
        "max_tokens": (
            16384
            if output_budget_successor_profile
            else (4096 if finish_audit_profile else 1024)
        ),
        "workers": 1,
        "training_epochs": 400,
        "market_agents": 48,
        "market_rounds": 60,
        "market_seeds": 3,
        "state_design_hash": "26f02f06fb9cefb8dd16da029864fe9687fb9bd724ab5389f6a42ab9231c59f8",
        "planned_split_hash": "8953691100c66949fee911b65d1c557bda037228c4d81b2d3e8e9594f5daeca6",
        "planned_split_counts": {
            "train_rows": 36,
            "validation_rows": 9,
            "test_rows": 9,
            "train_families": 36,
            "validation_families": 9,
            "test_families": 9,
        },
        "planned_sample_order_hash": "ea45fd623d56aa88cd55100231b6d59f83ae1ee093411d6339e3cdfe670d1ac2",
        "canary_sample_id": "82029f81cfbb9c227637d03ee98303e5fabd05eef2681a76c25b98a8eff197a5",
        "transport_release_policy": {
            "first_planned_sample_is_canary": True,
            "strict_sequential": True,
            "fail_fast_after_any_resolved_failure": True,
            "provider_retry_count": 0,
        },
        "teacher_acceptance_gate": {
            "all_planned_samples_must_resolve_valid": True,
            "required_valid_replicates_per_state": 3,
            "required_unique_reported_models": [required_reported_model],
            "student_and_market_forbidden_on_failure": True,
            "selective_supplement_forbidden": True,
            "partial_run_merge_forbidden": True,
        },
    }
    if higgs_alias_profile:
        descriptor["model_identity_semantics"] = {
            "requested_and_reported_are_distinct_fields": True,
            "reported_value_source": "provider_sdk_response_model_field",
            "reported_alias_is_underlying_serving_weights_identity_claim": False,
            "gateway_mapping_operator_confirmed_on": "2026-08-12",
        }
        if finish_audit_profile:
            descriptor["predecessor_failed_runs"] = [
                {
                    "run_id": "v2-teacher-pilot-live-20260812-a1",
                    "status": "failed",
                    "attempted": 1,
                    "valid": 0,
                    "skipped": 161,
                    "run_manifest_sha256": "8e9fde17ed2c71267c2c45cc110c6c951690269d997f84fbf080e5e7e9881c7d",
                    "reuse_supplement_or_merge_forbidden": True,
                },
                {
                    "run_id": "v2-teacher-pilot-live-20260812-a2",
                    "status": "failed",
                    "attempted": 4,
                    "valid": 3,
                    "skipped": 158,
                    "failure_code": "provider_response_shape_invalid",
                    "run_manifest_sha256": "1dc925131c31ed24e85df891c9b6bcbb6a879082e0db6ef2f38b02c5007dc6c8",
                    "reuse_supplement_or_merge_forbidden": True,
                },
            ]
            if external_execution_successor_profile:
                descriptor["predecessor_failed_runs"].append(
                    {
                        "run_id": "v2-teacher-pilot-live-20260813-a3",
                        "status": "failed",
                        "attempted": 1,
                        "responses": 0,
                        "valid": 0,
                        "skipped": 161,
                        "failure_code": "provider_exception",
                        "run_manifest_sha256": "ede68ce4d7c069feb2424884cf62f0f0d92546c56638c5e5550cb56f6ee326cf",
                        "reuse_supplement_or_merge_forbidden": True,
                    }
                )
            if long_timeout_execution_successor_profile:
                descriptor["predecessor_failed_runs"].append(
                    {
                        "run_id": "v2-teacher-pilot-live-20260813-a4",
                        "status": "failed",
                        "attempted": 6,
                        "responses": 5,
                        "valid": 5,
                        "skipped": 156,
                        "failure_code": "provider_exception",
                        "provider_error_type": "APITimeoutError",
                        "run_manifest_sha256": "86c1deb28f85c2f49e9fd36c410c951f3fa27e8d8fa48b94cf3febf86d1f888e",
                        "reuse_supplement_or_merge_forbidden": True,
                    }
                )
            if output_budget_successor_profile:
                descriptor["predecessor_failed_runs"].append(
                    {
                        "run_id": "v2-teacher-pilot-live-20260813-a5",
                        "status": "failed",
                        "attempted": 7,
                        "responses": 7,
                        "valid": 6,
                        "honest_n": 6,
                        "skipped": 155,
                        "failure_code": "provider_response_shape_invalid",
                        "finish_reason": "length",
                        "output_tokens": 4096,
                        "student_runs": 0,
                        "market_runs": 0,
                        "run_manifest_sha256": "76e79010a55a2c57766d52b147f686b4c0854c9abaa7cadc3abe468adf96b16a",
                        "reuse_supplement_or_merge_forbidden": True,
                    }
                )
            descriptor["response_termination_contract"] = {
                "finish_reason_source": "provider_sdk_choice_finish_reason",
                "required_finish_reason": "stop",
                "missing_finish_reason_is_failure": True,
                "length_finish_reason_is_failure": True,
                "reasoning_content_is_never_a_decision_source": True,
            }
            if external_execution_successor_profile:
                descriptor["external_network_required"] = True
                if output_budget_successor_profile:
                    descriptor["successor_scope"] = (
                        "output_budget_model_request_only"
                    )
                    descriptor["httpx_phase_inactivity_timeout_seconds"] = 600
                    descriptor["hard_request_deadline_seconds"] = 600
                    descriptor["connect_timeout_seconds"] = 10
                    descriptor["engineering_change_from_v5"] = {
                        "max_tokens": {"from": 4096, "to": 16384},
                        "motivation": (
                            "a5 request 7 resolved with finish_reason=length at "
                            "output_tokens=4096 and therefore failed the exact-stop "
                            "response-termination gate"
                        ),
                        "model_request_change_scope": ["max_tokens"],
                        "model_request_changed": True,
                        "execution_contract_inherited_from_v5": True,
                        "httpx_phase_inactivity_timeout_changed": False,
                        "hard_request_deadline_changed": False,
                        "connect_timeout_changed": False,
                        "teacher_prompt_changed": False,
                        "state_design_changed": False,
                        "sample_identity_changed": False,
                        "sample_order_changed": False,
                        "finish_reason_contract_changed": False,
                        "provider_retry_count_changed": False,
                        "student_changed": False,
                        "market_changed": False,
                    }
                    descriptor["required_run_ids"] = {
                        "dry_run": "v2-teacher-pilot-v6-dry-20260820-a1",
                        "live": "v2-teacher-pilot-live-20260820-a6",
                    }
                elif long_timeout_execution_successor_profile:
                    descriptor["successor_scope"] = "execution_only"
                    descriptor["httpx_phase_inactivity_timeout_seconds"] = 600
                    descriptor["hard_request_deadline_seconds"] = 600
                    descriptor["connect_timeout_seconds"] = 10
                    descriptor["engineering_change_from_v4"] = {
                        "httpx_phase_inactivity_timeout_seconds": {
                            "from": 120,
                            "to": 600,
                        },
                        "hard_request_deadline_seconds": {
                            "from": None,
                            "to": 600,
                        },
                        "motivation": "a4 request 6 produced APITimeoutError after the frozen 120-second client timeout",
                        "model_request_changed": False,
                        "teacher_prompt_changed": False,
                        "state_design_changed": False,
                        "sample_identity_changed": False,
                        "finish_reason_contract_changed": False,
                        "provider_retry_count_changed": False,
                    }
                    descriptor["required_run_ids"] = {
                        "dry_run": "v2-teacher-pilot-v5-dry-20260813-a1",
                        "live": "v2-teacher-pilot-live-20260813-a5",
                    }
                else:
                    descriptor["successor_scope"] = "execution_only"
                    descriptor["engineering_change_from_v3"] = {
                        "motivation": "a3 failed before any provider response because the endpoint was unreachable from its execution environment",
                        "model_request_changed": False,
                        "teacher_prompt_changed": False,
                        "state_design_changed": False,
                        "sample_identity_changed": False,
                        "finish_reason_contract_changed": False,
                    }
                    descriptor["required_run_ids"] = {
                        "dry_run": "v2-teacher-pilot-v4-dry-20260813-a1",
                        "live": "v2-teacher-pilot-live-20260813-a4",
                    }
            else:
                descriptor["engineering_change_from_v2"] = {
                    "max_tokens": {"from": 1024, "to": 4096},
                    "motivation": "a2 response 4 reached the 1024-token output cap without returning string content, consistent with output-budget exhaustion",
                    "teacher_prompt_changed": False,
                    "state_design_changed": False,
                }
                descriptor["required_run_ids"] = {
                    "dry_run": "v2-teacher-pilot-v3-dry-20260813-a1",
                    "live": "v2-teacher-pilot-live-20260813-a3",
                }
        else:
            descriptor["predecessor_failed_run"] = {
                "run_id": "v2-teacher-pilot-live-20260812-a1",
                "status": "failed",
                "attempted": 1,
                "valid": 0,
                "skipped": 161,
                "run_manifest_sha256": "8e9fde17ed2c71267c2c45cc110c6c951690269d997f84fbf080e5e7e9881c7d",
                "reuse_supplement_or_merge_forbidden": True,
            }
            descriptor["required_run_ids"] = {
                "dry_run": "v2-teacher-pilot-v2-dry-20260812-a1",
                "live": "v2-teacher-pilot-live-20260812-a2",
            }
    return descriptor


def _teacher_request_schema_version(profile_id: Optional[str]) -> str:
    if profile_id in {
        MINIMAX_M27_HIGGSAI_FINISH_AUDIT_JOINT54X3_PILOT,
        MINIMAX_M27_HIGGSAI_FINISH_AUDIT_EXTERNAL_JOINT54X3_PILOT,
        MINIMAX_M27_HIGGSAI_LONG_TIMEOUT_JOINT54X3_PILOT,
        MINIMAX_M27_HIGGSAI_TIMEOUT600_OUTPUT16384_JOINT54X3_PILOT,
    }:
        return FINISH_AUDIT_REQUEST_SCHEMA_VERSION
    return MODEL_REQUEST_SCHEMA_VERSION


def _request_timeout_seconds(profile_id: Optional[str]) -> float:
    if profile_id in {
        MINIMAX_M27_HIGGSAI_LONG_TIMEOUT_JOINT54X3_PILOT,
        MINIMAX_M27_HIGGSAI_TIMEOUT600_OUTPUT16384_JOINT54X3_PILOT,
    }:
        return V5_REQUEST_TIMEOUT_SECONDS
    return DEFAULT_REQUEST_TIMEOUT_SECONDS


def _hard_request_deadline_seconds(profile_id: Optional[str]) -> Optional[float]:
    if profile_id in {
        MINIMAX_M27_HIGGSAI_LONG_TIMEOUT_JOINT54X3_PILOT,
        MINIMAX_M27_HIGGSAI_TIMEOUT600_OUTPUT16384_JOINT54X3_PILOT,
    }:
        return V5_REQUEST_TIMEOUT_SECONDS
    return None


def _execution_schema_version(profile_id: Optional[str]) -> str:
    if profile_id in {
        MINIMAX_M27_HIGGSAI_LONG_TIMEOUT_JOINT54X3_PILOT,
        MINIMAX_M27_HIGGSAI_TIMEOUT600_OUTPUT16384_JOINT54X3_PILOT,
    }:
        return TRANSPORT_AUDIT_EXECUTION_SCHEMA_VERSION
    return EXECUTION_SCHEMA_VERSION


def split_regime_descriptor() -> dict[str, Any]:
    """Return the frozen split-classification and allocation semantics."""

    return {
        "schema_version": "v2-split-regime-classification/0.1",
        "tape_regimes": {
            "decline": {
                "field": "return_20d",
                "operator": "<",
                "threshold": TAPE_DECLINE_UPPER_EXCLUSIVE,
            },
            "flat": {
                "field": "return_20d",
                "lower_operator": ">=",
                "lower_threshold": TAPE_DECLINE_UPPER_EXCLUSIVE,
                "upper_operator": "<=",
                "upper_threshold": TAPE_RISE_LOWER_EXCLUSIVE,
            },
            "rise": {
                "field": "return_20d",
                "operator": ">",
                "threshold": TAPE_RISE_LOWER_EXCLUSIVE,
            },
        },
        "position_regimes": {
            "cash": {
                "field": "position_fraction",
                "operator": "<",
                "threshold": POSITION_CASH_UPPER_EXCLUSIVE,
            },
            "mixed": {
                "field": "position_fraction",
                "lower_operator": ">=",
                "lower_threshold": POSITION_CASH_UPPER_EXCLUSIVE,
                "upper_operator": "<=",
                "upper_threshold": POSITION_INVESTED_LOWER_EXCLUSIVE,
            },
            "invested": {
                "field": "position_fraction",
                "operator": ">",
                "threshold": POSITION_INVESTED_LOWER_EXCLUSIVE,
            },
        },
        "joint_stratification_eligibility": {
            "required_exact_cell_count": JOINT_SPLIT_REQUIRED_STRATA,
            "minimum_families_per_cell": JOINT_SPLIT_MIN_FAMILIES_PER_STRATUM,
        },
        "small_design_fallback": "return_20d_tape_regime_only",
        "allocation": {
            "partitions": ["train", "validation", "test"],
            "fractions": list(SPLIT_FRACTIONS),
            "integer_rule": "largest_remainder_with_partition_index_tie_break",
            "unit": "family_id",
        },
        "confirmatory_coverage_acceptance_threshold_frozen": False,
    }


@dataclass(frozen=True)
class TeacherCompletion:
    raw_response: Optional[str]
    reported_model: Optional[str]
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    response_id: Optional[str]
    reported_model_raw: Optional[str] = None
    error_type: Optional[str] = None
    error_detail: Optional[str] = None
    # Additive v3 fields remain after every pre-v3 field so older positional
    # construction cannot silently bind an error as termination provenance.
    finish_reason: Optional[str] = None
    finish_reason_raw: Optional[str] = None
    provider_sdk_response_json: Optional[str] = None


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite value is not valid canonical JSON")
        return value
    raise TypeError("unsupported canonical JSON value: {}".format(type(value).__name__))


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def stable_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)


def _open_exclusive(path: Path, mode: int):
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        stream = os.fdopen(descriptor, "w", encoding="utf-8")
    except BaseException:
        os.close(descriptor)
        raise
    os.chmod(path, mode)
    return stream


def _write_json_exclusive(path: Path, value: Any, mode: int = 0o644) -> None:
    with _open_exclusive(path, mode) as stream:
        json.dump(
            _jsonable(value),
            stream,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _write_text_exclusive(path: Path, value: str, mode: int = 0o644) -> None:
    with _open_exclusive(path, mode) as stream:
        stream.write(value)
        if value and not value.endswith("\n"):
            stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _write_jsonl_row(stream: Any, row: Mapping[str, Any]) -> None:
    stream.write(
        json.dumps(
            _jsonable(row),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    )
    stream.flush()


def _optional_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _safe_public_model_alias(value: Any) -> Optional[str]:
    """Return a strict, credential-aware public Provider model alias."""

    alias = safe_reported_model(value)
    if alias is None or _PUBLIC_MODEL_ALIAS_RE.fullmatch(alias) is None:
        return None
    secret_candidates = [os.environ.get("OPENAI_API_KEY")]
    endpoint = os.environ.get("OPENAI_BASE_URL")
    if endpoint:
        try:
            parsed = urlsplit(endpoint)
            secret_candidates.extend([parsed.username, parsed.password])
            secret_candidates.extend(
                item for _, item in parse_qsl(parsed.query, keep_blank_values=True)
            )
            secret_candidates.append(parsed.fragment)
            secret_candidates.extend(
                item for _, item in parse_qsl(parsed.fragment, keep_blank_values=True)
            )
        except (TypeError, ValueError):
            pass
    for candidate in secret_candidates:
        if not isinstance(candidate, str) or not candidate or candidate == "EMPTY":
            continue
        if alias == candidate or (len(candidate) >= 4 and candidate in alias):
            return None
    return alias


def _safe_public_finish_reason(value: Any) -> Optional[str]:
    """Expose only the SDK's finite, non-secret Chat Completion reasons."""

    if not isinstance(value, str) or value not in _PUBLIC_FINISH_REASONS:
        return None
    return value


def _v2_endpoint_identity(endpoint: Optional[str]) -> dict[str, Any]:
    """Build a V2-local route identity without hashing credential values.

    This intentionally does not alter the versioned provider-capability or V1
    provenance contracts. Query keys remain part of route identity, while all
    values, userinfo, and fragments are omitted before hashing.
    """

    text_value = str(endpoint or "")
    if not text_value:
        return {
            "schema_version": "v2_endpoint_identity/0.1",
            "configured": False,
            "scheme": None,
            "endpoint_identity_sha256": None,
            "userinfo_redacted": False,
            "sensitive_query_redacted": False,
            "query_values_omitted": False,
            "fragment_omitted": False,
            "malformed_endpoint_redacted": False,
            "path_credential_redacted": False,
        }
    try:
        parsed = urlsplit(text_value)
        host = parsed.netloc.rsplit("@", 1)[-1]
        query_items: list[tuple[str, str]] = []
        sensitive_query = False
        query_values_omitted = False
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            sensitive_query = sensitive_query or bool(
                _SECRET_ENDPOINT_QUERY_KEY_RE.search(key)
                or value.strip().lower().startswith("bearer ")
            )
            query_values_omitted = query_values_omitted or bool(value)
            query_items.append((key, "<value-omitted>" if value else ""))
        query_items.sort()
        normalized_path = parsed.path
        path_credential_redacted = False
        configured_key = os.environ.get("OPENAI_API_KEY")
        if configured_key and configured_key != "EMPTY" and configured_key in normalized_path:
            normalized_path = normalized_path.replace(
                configured_key, "<credential-omitted>"
            )
            path_credential_redacted = True
        normalized = urlunsplit(
            (
                parsed.scheme.lower(),
                host.lower(),
                normalized_path,
                urlencode(query_items),
                "",
            )
        )
        scheme = parsed.scheme.lower() or None
        userinfo_redacted = "@" in parsed.netloc
        fragment_omitted = bool(parsed.fragment)
        malformed = False
    except (TypeError, ValueError):
        normalized = "<malformed-v2-endpoint>"
        scheme = None
        userinfo_redacted = "@" in text_value
        sensitive_query = bool(_SECRET_ENDPOINT_QUERY_KEY_RE.search(text_value))
        query_values_omitted = "?" in text_value
        fragment_omitted = "#" in text_value
        malformed = True
        path_credential_redacted = False
    return {
        "schema_version": "v2_endpoint_identity/0.1",
        "configured": True,
        "scheme": scheme,
        "endpoint_identity_sha256": hashlib.sha256(
            normalized.encode("utf-8")
        ).hexdigest(),
        "userinfo_redacted": userinfo_redacted,
        "sensitive_query_redacted": sensitive_query,
        "query_values_omitted": query_values_omitted,
        "fragment_omitted": fragment_omitted,
        "malformed_endpoint_redacted": malformed,
        "path_credential_redacted": path_credential_redacted,
    }


class OpenAITeacherProvider:
    """Single-attempt concurrent OpenAI-compatible transport with no fallback."""

    kind = "openai"

    def __init__(
        self,
        *,
        model: str,
        temperature: float,
        max_tokens: int,
        workers: int,
        request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        hard_request_deadline_seconds: Optional[float] = None,
    ) -> None:
        import httpx
        from openai import AsyncOpenAI

        timeout_seconds = float(request_timeout_seconds)
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0.0:
            raise V2ProviderGuardError(
                "request_timeout_seconds must be finite and positive"
            )
        hard_deadline_seconds = (
            None
            if hard_request_deadline_seconds is None
            else float(hard_request_deadline_seconds)
        )
        if hard_deadline_seconds is not None and (
            not math.isfinite(hard_deadline_seconds)
            or hard_deadline_seconds <= 0.0
        ):
            raise V2ProviderGuardError(
                "hard_request_deadline_seconds must be finite and positive"
            )
        base_url = os.environ.get("OPENAI_BASE_URL")
        if not base_url:
            raise V2ProviderGuardError("OPENAI_BASE_URL is required for --provider openai")
        api_key = os.environ.get("OPENAI_API_KEY") or "EMPTY"
        self._http = httpx.AsyncClient(
            trust_env=False,
            timeout=httpx.Timeout(
                timeout_seconds,
                connect=PROVIDER_CONNECT_TIMEOUT_SECONDS,
            ),
            limits=httpx.Limits(
                max_connections=workers,
                max_keepalive_connections=workers,
            ),
        )
        self._client = AsyncOpenAI(
            base_url=base_url,
            api_key=api_key,
            http_client=self._http,
            max_retries=0,
        )
        self.model = model
        self.temperature = float(temperature)
        self.max_tokens = int(max_tokens)
        self.workers = int(workers)
        self.request_timeout_seconds = timeout_seconds
        self.httpx_phase_inactivity_timeout_seconds = timeout_seconds
        self.hard_request_deadline_seconds = hard_deadline_seconds
        self.connect_timeout_seconds = PROVIDER_CONNECT_TIMEOUT_SECONDS
        self.provider_retry_count = 0
        self.network_access = False
        self.request_count = 0
        self.response_count = 0
        self.batch_sizes: list[int] = []

    async def _one(
        self,
        index: int,
        system: str,
        user: str,
        semaphore: asyncio.Semaphore,
        before_attempt: Optional[Callable[[int], None]],
    ) -> tuple[int, TeacherCompletion]:
        async with semaphore:
            if before_attempt is not None:
                before_attempt(index)
            self.request_count += 1
            self.network_access = True
            try:
                request = self._client.chat.completions.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    temperature=self.temperature,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                )
                hard_deadline = getattr(
                    self, "hard_request_deadline_seconds", None
                )
                if hard_deadline is None:
                    response = await request
                else:
                    response = await asyncio.wait_for(
                        request,
                        timeout=hard_deadline,
                    )
            except Exception as error:
                return index, TeacherCompletion(
                    raw_response=None,
                    reported_model=None,
                    input_tokens=None,
                    output_tokens=None,
                    response_id=None,
                    error_type=type(error).__name__,
                    error_detail="{}: {}".format(type(error).__name__, error),
                )
            self.response_count += 1
            provider_sdk_response_json = None
            dump_response = getattr(response, "model_dump_json", None)
            if callable(dump_response):
                try:
                    dumped = dump_response()
                    if isinstance(dumped, str):
                        provider_sdk_response_json = dumped
                except Exception:
                    provider_sdk_response_json = None
            usage = getattr(response, "usage", None)
            raw_reported_model = (
                str(response.model) if getattr(response, "model", None) else None
            )
            reported_model = _safe_public_model_alias(raw_reported_model)
            input_tokens = _optional_int(getattr(usage, "prompt_tokens", None))
            output_tokens = _optional_int(
                getattr(usage, "completion_tokens", None)
            )
            response_id = (
                str(response.id) if getattr(response, "id", None) else None
            )
            raw_finish_reason = None
            finish_reason = None
            try:
                choices = getattr(response, "choices", None)
                if not choices:
                    raise V2ProtocolError("provider response has no choice")
                choice = choices[0]
                raw_finish_reason = getattr(choice, "finish_reason", None)
                finish_reason = _safe_public_finish_reason(raw_finish_reason)
                message = getattr(choice, "message", None)
                if message is None:
                    raise V2ProtocolError("provider choice has no message")
                content = getattr(message, "content", None)
                if not isinstance(content, str):
                    raise V2ProtocolError(
                        "provider message content must be a string"
                    )
            except Exception as error:
                return index, TeacherCompletion(
                    raw_response=None,
                    reported_model=reported_model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    response_id=response_id,
                    reported_model_raw=raw_reported_model,
                    finish_reason=finish_reason,
                    finish_reason_raw=(
                        str(raw_finish_reason)
                        if raw_finish_reason is not None
                        else None
                    ),
                    provider_sdk_response_json=provider_sdk_response_json,
                    error_type="ProviderResponseShapeError",
                    error_detail="ProviderResponseShapeError: {}".format(error),
                )
            return index, TeacherCompletion(
                raw_response=content,
                reported_model=reported_model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                response_id=response_id,
                reported_model_raw=raw_reported_model,
                finish_reason=finish_reason,
                finish_reason_raw=(
                    str(raw_finish_reason)
                    if raw_finish_reason is not None
                    else None
                ),
                provider_sdk_response_json=provider_sdk_response_json,
            )

    async def complete_many(
        self,
        prompts: Sequence[tuple[str, str]],
        *,
        before_attempt: Optional[Callable[[int], None]] = None,
        on_completion: Optional[Callable[[int, TeacherCompletion], None]] = None,
        strict_sequential: bool = False,
    ) -> list[TeacherCompletion]:
        self.batch_sizes.append(len(prompts))
        semaphore = asyncio.Semaphore(self.workers)

        async def observed(
            index: int, system: str, user: str
        ) -> tuple[int, TeacherCompletion]:
            pair = await self._one(
                index, system, user, semaphore, before_attempt
            )
            if on_completion is not None:
                on_completion(pair[0], pair[1])
            return pair

        if strict_sequential:
            if self.workers != 1:
                raise V2ProviderGuardError(
                    "strict sequential Teacher transport requires --workers 1"
                )
            pairs = []
            for index, (system, user) in enumerate(prompts):
                pairs.append(await observed(index, system, user))
        else:
            pairs = await asyncio.gather(
                *(
                    observed(index, system, user)
                    for index, (system, user) in enumerate(prompts)
                )
            )
        pairs.sort(key=lambda pair: pair[0])
        return [completion for _, completion in pairs]

    async def aclose(self) -> None:
        await self._client.close()


class FakeTeacherProvider:
    """Offline provider-shaped adapter for the two explicit engineering fakes."""

    kind = "fake_test_teacher"

    def __init__(self, provider_id: str) -> None:
        if provider_id not in {"fake_test_teacher", "fake_null_teacher"}:
            raise ValueError("unknown fake Teacher")
        self.kind = provider_id
        self.model = (
            "structured-engineering-test-double-v1"
            if provider_id == "fake_test_teacher"
            else "constant-null-engineering-control-v1"
        )
        self.request_count = 0
        self.response_count = 0
        self.network_access = False
        self.batch_sizes: list[int] = []

    def complete_plan(
        self,
        sample_plan: Sequence[Mapping[str, Any]],
        *,
        before_attempt: Optional[Callable[[int], None]] = None,
        on_completion: Optional[Callable[[int, TeacherCompletion], None]] = None,
    ) -> list[TeacherCompletion]:
        from nmsim import v2_attention

        function = (
            v2_attention.fake_test_teacher
            if self.kind == "fake_test_teacher"
            else v2_attention.fake_null_teacher
        )
        self.batch_sizes.append(len(sample_plan))
        completions: list[TeacherCompletion] = []
        for index, item in enumerate(sample_plan):
            if before_attempt is not None:
                before_attempt(index)
            self.request_count += 1
            raw = function(item["observation"], item["replicate_index"])
            self.response_count += 1
            completion = TeacherCompletion(
                raw_response=raw,
                reported_model=self.model,
                input_tokens=None,
                output_tokens=None,
                response_id="fake-{}".format(
                    hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
                ),
                reported_model_raw=self.model,
            )
            completions.append(completion)
            if on_completion is not None:
                on_completion(index, completion)
        return completions


def build_argparser() -> argparse.ArgumentParser:
    parser = RaisingArgumentParser(allow_abbrev=False)
    parser.add_argument("--version", action="version", version=PROTOCOL_VERSION)
    parser.add_argument("--provider", choices=sorted(ALLOWED_PROVIDERS), default="fake_test_teacher")
    parser.add_argument("--model", default="")
    parser.add_argument("--temperature", type=float, default=0.3)
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--states", type=int, default=96)
    parser.add_argument("--replicates", type=int, default=5)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--seed", type=int, default=20260811)
    parser.add_argument("--training-epochs", type=int, default=400)
    parser.add_argument("--market-agents", type=int, default=48)
    parser.add_argument("--market-rounds", type=int, default=60)
    parser.add_argument("--market-seeds", type=int, default=3)
    parser.add_argument("--pilot-profile", choices=sorted(PILOT_PROFILES), default=None)
    parser.add_argument("--confirm-request-count", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--out", default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--run-id", default=None)
    return parser


def _validate_args(args: argparse.Namespace) -> int:
    if args.dry_run and args.live:
        raise V2ProtocolError("--dry-run and --live are mutually exclusive")
    if args.provider == "openai":
        if not args.live and not args.dry_run:
            raise V2ProviderGuardError("--provider openai requires --live")
        if not str(args.model).strip():
            raise V2ProviderGuardError("--provider openai requires an explicit --model")
        model_text = str(args.model)
        if (
            model_text != model_text.strip()
            or _PUBLIC_MODEL_ALIAS_RE.fullmatch(model_text) is None
            or _safe_public_model_alias(model_text) != model_text
        ):
            raise V2ProviderGuardError(
                "--model must be a public credential-free provider alias"
            )
    elif args.live:
        raise V2ProviderGuardError("--live is valid only with --provider openai")
    else:
        if str(args.model).strip():
            raise V2ProviderGuardError(
                "--model is not configurable for an offline Fake Teacher"
            )
        if args.temperature != 0.3 or args.max_tokens != 256:
            raise V2ProviderGuardError(
                "--temperature and --max-tokens are fixed for offline Fake Teachers"
            )
    numeric_minimums = {
        "--seed": (args.seed, 0),
        "--states": (args.states, 12),
        "--replicates": (args.replicates, 1),
        "--workers": (args.workers, 1),
        "--max-tokens": (args.max_tokens, 1),
        "--training-epochs": (args.training_epochs, 1),
        "--market-agents": (args.market_agents, 4),
        "--market-rounds": (args.market_rounds, 1),
        "--market-seeds": (args.market_seeds, 1),
    }
    for option, (value, minimum) in numeric_minimums.items():
        if isinstance(value, bool) or int(value) < minimum:
            raise V2ProtocolError("{} must be >= {}".format(option, minimum))
    if not math.isfinite(args.temperature) or not 0.0 <= args.temperature <= 2.0:
        raise V2ProtocolError("--temperature must be finite and in [0,2]")
    planned_requests = int(args.states) * int(args.replicates)
    pilot = pilot_profile_descriptor(args.pilot_profile)
    if pilot is not None:
        actual = {
            "provider": args.provider,
            "model_requested": args.model,
            "states": args.states,
            "replicates_per_state": args.replicates,
            "planned_requests": planned_requests,
            "seed": args.seed,
            "temperature": args.temperature,
            "max_tokens": args.max_tokens,
            "workers": args.workers,
            "training_epochs": args.training_epochs,
            "market_agents": args.market_agents,
            "market_rounds": args.market_rounds,
            "market_seeds": args.market_seeds,
            "httpx_phase_inactivity_timeout_seconds": _request_timeout_seconds(
                args.pilot_profile
            ),
            "hard_request_deadline_seconds": _hard_request_deadline_seconds(
                args.pilot_profile
            ),
            "connect_timeout_seconds": PROVIDER_CONNECT_TIMEOUT_SECONDS,
        }
        mismatches = [
            key for key, expected in pilot.items()
            if key in actual and actual[key] != expected
        ]
        if mismatches:
            raise V2ProviderGuardError(
                "--pilot-profile settings do not match frozen fields: {}".format(
                    ", ".join(sorted(mismatches))
                )
            )
        endpoint_identity = _v2_endpoint_identity(
            os.environ.get("OPENAI_BASE_URL")
        )["endpoint_identity_sha256"]
        if endpoint_identity != pilot["endpoint_identity_sha256"]:
            raise V2ProviderGuardError(
                "--pilot-profile endpoint identity does not match the frozen endpoint"
            )
        required_run_ids = pilot.get("required_run_ids")
        if required_run_ids is not None:
            required_run_id = required_run_ids[
                "live" if args.live else "dry_run"
            ]
            if args.run_id != required_run_id:
                raise V2ProviderGuardError(
                    "--pilot-profile requires the frozen {} run id".format(
                        "live" if args.live else "dry-run"
                    )
                )
    if args.provider == "openai" and args.live:
        if args.confirm_request_count != planned_requests:
            raise V2ProviderGuardError(
                "--confirm-request-count must equal the exact planned request count"
            )
    elif args.confirm_request_count is not None:
        raise V2ProviderGuardError(
            "--confirm-request-count is accepted only for a live OpenAI run"
        )
    return planned_requests


def _build_openai_provider(args: argparse.Namespace) -> OpenAITeacherProvider:
    return OpenAITeacherProvider(
        model=args.model,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        workers=args.workers,
        request_timeout_seconds=_request_timeout_seconds(args.pilot_profile),
        hard_request_deadline_seconds=_hard_request_deadline_seconds(
            args.pilot_profile
        ),
    )


# The pipeline and report functions are kept below the strict transport and CLI
# boundary so importing this module never imports a Provider SDK or performs I/O.


def _fmt(value: Any, digits: int = 4) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return ("{:.%df}" % digits).format(value)
    return str(value)


def _sparkline_svg(values: Sequence[float], *, width: int = 320, height: int = 72) -> str:
    finite = [float(value) for value in values if math.isfinite(float(value))]
    if not finite:
        return '<svg viewBox="0 0 320 72" role="img" aria-label="no price data"></svg>'
    low, high = min(finite), max(finite)
    spread = high - low or 1.0
    count = max(1, len(finite) - 1)
    points = []
    for index, value in enumerate(finite):
        x = 4.0 + (width - 8.0) * index / count
        y = 4.0 + (height - 8.0) * (high - value) / spread
        points.append("{:.2f},{:.2f}".format(x, y))
    return (
        '<svg viewBox="0 0 {w} {h}" role="img" aria-label="price path">'
        '<polyline fill="none" stroke="currentColor" stroke-width="2" '
        'points="{points}"/></svg>'
    ).format(w=width, h=height, points=" ".join(points))


def render_markdown_report(summary: Mapping[str, Any]) -> str:
    teacher = summary.get("teacher", {})
    split = summary.get("dataset", {}).get("split_counts", {})
    planned_split_coverage = summary.get("dataset", {}).get(
        "planned_design_partition_regime_coverage", {}
    )
    eligible_split_coverage = summary.get("dataset", {}).get(
        "training_eligible_partition_regime_coverage", {}
    )
    disagreement = summary.get("dataset", {}).get("teacher_disagreement", {})
    evaluations = summary.get("student", {}).get("evaluations", {})
    baseline = summary.get("student", {}).get(
        "held_out_baseline_comparison", {}
    )
    market_rows = summary.get("market", {}).get("cell_summaries", [])
    market_ood = summary.get("market", {}).get("market_vs_train_ood") or {}
    all_market_ood = market_ood.get("all_cells", {})
    lines = [
        "# V2 limited-attention behavior-distillation market — run report",
        "",
        "> This is an engineering report. The built-in Fake Teacher is not a real endpoint or human-behavior result.",
        "",
        "## Run identity",
        "",
        "- Run id: `{}`".format(summary.get("run_id")),
        "- Provider: `{}`".format(summary.get("provider")),
        "- Requested model: `{}`".format(summary.get("model_requested")),
        "- Provider/SDK-reported model aliases: `{}`".format(
            summary.get("reported_models", [])
        ),
        "- Underlying serving weights independently verified: `{}`".format(
            summary.get("model_identity", {}).get(
                "underlying_serving_weights_independently_verified", False
            )
        ),
        "- Protocol: `{}`".format(summary.get("protocol_version")),
        "- `v2_scientific_config_hash`: `{}`".format(
            summary.get("v2_scientific_config_hash")
        ),
        "- `v2_model_request_config_hash`: `{}`".format(
            summary.get("v2_model_request_config_hash")
        ),
        "- `v2_execution_config_hash`: `{}`".format(
            summary.get("v2_execution_config_hash")
        ),
        "- `v2_full_effective_config_hash`: `{}`".format(
            summary.get("v2_full_effective_config_hash")
        ),
        "",
        "## Teacher and dataset",
        "",
        "| Planned | Attempted | Raw responses | Valid | Failed |",
        "|---:|---:|---:|---:|---:|",
        "| {} | {} | {} | {} | {} |".format(
            teacher.get("planned"), teacher.get("attempted"),
            teacher.get("raw_responses"), teacher.get("valid"),
            teacher.get("failed"),
        ),
        "",
        "Grouped examples: train `{}`, validation `{}`, frozen test `{}`. Related states and all repeated samples remain in one split.".format(
            split.get("train", 0), split.get("validation", 0), split.get("test", 0)
        ),
        "Split strategy: `{}`; all partitions cover all nine planned tape x position strata: `{}`; all partitions retain all nine among training-eligible states: `{}`.".format(
            planned_split_coverage.get("split_stratification_unit"),
            planned_split_coverage.get("all_partitions_cover_all_design_strata"),
            eligible_split_coverage.get("all_partitions_cover_all_design_strata"),
        ),
        "Teacher disagreement: mean action Gini `{}` across `{}` states with at least two valid replicates; mean conditional buy/sell intensity variance `{}`.".format(
            _fmt(disagreement.get("mean_action_gini")),
            disagreement.get("states_with_at_least_two_valid_replicates", 0),
            _fmt(
                disagreement.get(
                    "mean_conditional_buy_sell_intensity_variance"
                )
            ),
        ),
        "",
        "## Student evaluation",
        "",
        "| Model | Test cross entropy | Test Brier | Test accuracy | Buy/sell intensity MAE |",
        "|---|---:|---:|---:|---:|",
    ]
    for model_name in ("prior", "linear", "mlp"):
        metrics = evaluations.get(model_name, {}).get("test", {})
        lines.append(
            "| {} | {} | {} | {} | {} |".format(
                model_name,
                _fmt(metrics.get("action_cross_entropy")),
                _fmt(metrics.get("action_brier")),
                _fmt(metrics.get("action_accuracy")),
                _fmt(metrics.get("conditional_intensity_mae")),
            )
        )
    lines.extend(
        [
            "",
            "These held-out metrics measure agreement with this run's Teacher, not agreement with people.",
            "Fixed-MLP diagnostic: `deployed_model_failed_to_beat_a_baseline={}`. This test-set comparison is descriptive and was not used for model selection.".format(
                baseline.get("deployed_model_failed_to_beat_a_baseline")
            ),
            "",
            "## Budget x behavior market diagnostic",
            "",
            "| Cell | Completed seeds | Final return | Max run-up | Max drawdown | Turnover | Locked days | Credit used | Ending debt | Outside train range |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in market_rows:
        lines.append(
            "| {} | {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
                row.get("cell"), row.get("completed_seeds"),
                _fmt(row.get("mean_final_return")),
                _fmt(row.get("mean_max_runup")),
                _fmt(row.get("mean_max_drawdown")),
                _fmt(row.get("mean_turnover_shares"), 2),
                _fmt(row.get("mean_locked_rounds"), 2),
                _fmt(row.get("mean_credit_used_cents"), 2),
                _fmt(row.get("mean_ending_debt_cents"), 2),
                _fmt(
                    (row.get("market_vs_train_ood") or {})
                    .get("diagnostics", {})
                    .get("outside_train_range_row_fraction")
                ),
            )
        )
    lines.extend(
        [
            "",
            "Across all cells, `{}` of `{}` effective agent-round states (`{}`) were outside at least one train-only rectangular feature range. This is an extrapolation diagnostic, not an invalid-state count; joint/manifold support and distribution shift are not assessed.".format(
                all_market_ood.get("outside_train_range_rows", 0),
                all_market_ood.get("n", 0),
                _fmt(all_market_ood.get("outside_train_range_row_fraction")),
            ),
            "",
            "## Scientific interpretation",
            "",
            "- The V1 Persona/social/leverage market is unchanged; V2 is an additive isolated path.",
            "- Private Teacher rationale never enters public artifacts, Student features, or the market.",
            "- Cash and shares are conserved on every settled V2 round; relaxed budget uses an explicit balanced credit ledger.",
            "- Credit charges no interest or fees and has no automatic principal repayment inside the finite horizon; terminal debt remains outstanding.",
            "- No fundamental-value series is specified, so the report does not call a run-up a bubble.",
            "- Human labels, real endpoint qualification, preregistered acceptance thresholds, and a live confirmatory run remain pending.",
            "",
        ]
    )
    return "\n".join(lines)


def render_html_report(summary: Mapping[str, Any]) -> str:
    teacher = summary.get("teacher", {})
    evaluations = summary.get("student", {}).get("evaluations", {})
    disagreement = summary.get("dataset", {}).get("teacher_disagreement", {})
    baseline = summary.get("student", {}).get(
        "held_out_baseline_comparison", {}
    )
    market_rows = summary.get("market", {}).get("cell_summaries", [])
    market_ood = summary.get("market", {}).get("market_vs_train_ood") or {}
    all_market_ood = market_ood.get("all_cells", {})
    cards = []
    for row in market_rows:
        price_path = row.get("representative_price_path", [])
        cards.append(
            """
            <article class="cell">
              <header><h3>{cell}</h3><span>{seeds} completed seeds</span></header>
              <div class="spark">{spark}</div>
              <dl>
                <div><dt>Final return</dt><dd>{ret}</dd></div>
                <div><dt>Max drawdown</dt><dd>{dd}</dd></div>
                <div><dt>Turnover</dt><dd>{turnover}</dd></div>
                <div><dt>Credit used</dt><dd>{credit}</dd></div>
                <div><dt>Ending debt</dt><dd>{debt}</dd></div>
                <div><dt>Outside train range</dt><dd>{ood}</dd></div>
              </dl>
            </article>
            """.format(
                cell=html.escape(str(row.get("cell"))),
                seeds=html.escape(str(row.get("completed_seeds"))),
                spark=_sparkline_svg(price_path),
                ret=html.escape(_fmt(row.get("mean_final_return"))),
                dd=html.escape(_fmt(row.get("mean_max_drawdown"))),
                turnover=html.escape(_fmt(row.get("mean_turnover_shares"), 2)),
                credit=html.escape(_fmt(row.get("mean_credit_used_cents"), 2)),
                debt=html.escape(_fmt(row.get("mean_ending_debt_cents"), 2)),
                ood=html.escape(
                    _fmt(
                        (row.get("market_vs_train_ood") or {})
                        .get("diagnostics", {})
                        .get("outside_train_range_row_fraction")
                    )
                ),
            )
        )
    metric_rows = []
    for model_name in ("prior", "linear", "mlp"):
        metrics = evaluations.get(model_name, {}).get("test", {})
        metric_rows.append(
            "<tr><th>{}</th><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                html.escape(model_name),
                html.escape(_fmt(metrics.get("action_cross_entropy"))),
                html.escape(_fmt(metrics.get("action_brier"))),
                html.escape(_fmt(metrics.get("action_accuracy"))),
                html.escape(_fmt(metrics.get("conditional_intensity_mae"))),
            )
        )
    serialized = json.dumps(
        _jsonable(summary), ensure_ascii=False, sort_keys=True
    ).replace("<", "\\u003c")
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>V2 behavior-distillation market report</title>
<style>
:root {{ color-scheme: light; --ink:#18201c; --muted:#64706a; --paper:#f4f1e9; --card:#fffdf7; --accent:#136f63; --line:#d8d3c7; }}
* {{ box-sizing:border-box }} body {{ margin:0; background:var(--paper); color:var(--ink); font:15px/1.55 ui-sans-serif,system-ui,-apple-system,sans-serif }}
main {{ max-width:1120px; margin:auto; padding:44px 24px 72px }} h1 {{ max-width:780px; font:700 42px/1.05 ui-serif,Georgia,serif; letter-spacing:-.02em }} h2 {{ margin-top:42px; font:700 25px/1.2 ui-serif,Georgia,serif }}
.flag {{ display:inline-block; padding:7px 11px; border:1px solid #b97821; background:#fff3cf; border-radius:999px; color:#70440b; font-weight:650 }}
.lede {{ max-width:820px; color:var(--muted); font-size:17px }} .stats {{ display:grid; grid-template-columns:repeat(5,1fr); gap:10px; margin:28px 0 }}
.stat,.cell,.identity {{ background:var(--card); border:1px solid var(--line); border-radius:14px; padding:16px }} .stat b {{ display:block; font-size:25px }} .stat span, dt, header span {{ color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.06em }}
table {{ width:100%; border-collapse:collapse; background:var(--card); border:1px solid var(--line) }} th,td {{ padding:11px 13px; border-bottom:1px solid var(--line); text-align:right }} th:first-child,td:first-child {{ text-align:left }}
.grid {{ display:grid; grid-template-columns:repeat(2,1fr); gap:14px }} .cell header {{ display:flex; justify-content:space-between; gap:12px; align-items:baseline }} .cell h3 {{ margin:0 }} .spark {{ color:var(--accent); margin:10px 0 }} .spark svg {{ width:100%; height:72px }}
dl {{ display:grid; grid-template-columns:1fr 1fr; gap:8px; margin:0 }} dl div {{ border-top:1px solid var(--line); padding-top:8px }} dd {{ margin:2px 0 0; font-weight:700 }}
.identity {{ overflow-wrap:anywhere }} code {{ color:var(--accent) }} .notes li {{ margin:.55em 0 }}
@media(max-width:760px) {{ .stats {{grid-template-columns:1fr 1fr}} .grid {{grid-template-columns:1fr}} h1 {{font-size:34px}} }}
</style></head><body><main>
<span class="flag">Engineering evidence only · no human-validity claim</span>
<h1>Limited-attention Teacher → Student → conserving market</h1>
<p class="lede">An end-to-end audit of the V2 schemas, private Teacher boundary, grouped distillation, and the paired budget × behavior market controls.</p>
<section class="stats">
  <div class="stat"><b>{planned}</b><span>planned Teacher calls</span></div>
  <div class="stat"><b>{valid}</b><span>honest valid N</span></div>
  <div class="stat"><b>{failed}</b><span>failed samples</span></div>
  <div class="stat"><b>{states}</b><span>state examples</span></div>
  <div class="stat"><b>{runs}</b><span>market runs</span></div>
</section>
<h2>Student held-out agreement</h2>
<table><thead><tr><th>Model</th><th>Cross entropy</th><th>Brier</th><th>Accuracy</th><th>Intensity MAE</th></tr></thead><tbody>{metric_rows}</tbody></table>
<p><strong>Teacher disagreement:</strong> mean action Gini {action_gini} across {disagreement_n} states; mean conditional intensity variance {intensity_variance}.</p>
<p><strong>Fixed-MLP baseline diagnostic:</strong> deployed model failed to beat a baseline = {baseline_failed}. This is descriptive and was not used for model selection.</p>
<h2>Budget × behavior diagnostic</h2><div class="grid">{cards}</div>
<p><strong>Market-vs-train OOD:</strong> {ood_rows} of {ood_n} effective agent-round states ({ood_fraction}) exceeded at least one train-only rectangular feature range. This reports marginal extrapolation; it does not mark those states invalid, and joint/manifold support and distribution shift are not assessed.</p>
<h2>Endpoint model identity</h2><div class="identity">
<p><strong>Requested model</strong><br><code>{model_requested}</code></p>
<p><strong>Provider/SDK-reported aliases</strong><br><code>{reported_models}</code></p>
<p><strong>Underlying serving weights independently verified</strong><br>{weights_verified}</p>
<p>Requested and reported values are separate provenance fields. A gateway alias is not an independently verified claim about underlying weights.</p>
</div>
<h2>Named V2 identities</h2><div class="identity">
<p><code>v2_scientific_config_hash</code><br>{science}</p>
<p><code>v2_model_request_config_hash</code><br>{request}</p>
<p><code>v2_execution_config_hash</code><br>{execution}</p>
<p><code>v2_full_effective_config_hash</code><br>{full}</p></div>
<h2>Interpretation boundary</h2><ul class="notes">
<li>The bundled Fake Teacher is an engineering fixture, not an endpoint or a person.</li>
<li>Private rationale is not a public sample, feature, social signal, or market input.</li>
<li>All successful settlements conserve integer cash and shares; credit has an explicit balanced counter-account.</li>
<li>Credit has no interest, fees, or automatic principal repayment during the finite horizon; terminal debt remains outstanding.</li>
<li>Without an external fundamental-value series, run-up and reversal are descriptive and are not labelled a bubble.</li>
<li>Human comparison, real endpoint qualification, frozen acceptance thresholds, and live confirmatory sampling remain pending.</li>
</ul>
<details><summary>Machine-readable summary embedded in this report</summary><pre id="raw"></pre></details>
</main><script id="summary-data" type="application/json">{serialized}</script><script>const s=JSON.parse(document.getElementById("summary-data").textContent);document.getElementById("raw").textContent=JSON.stringify(s,null,2);</script></body></html>
""".format(
        planned=html.escape(str(teacher.get("planned", 0))),
        valid=html.escape(str(teacher.get("valid", 0))),
        failed=html.escape(str(teacher.get("failed", 0))),
        states=html.escape(str(summary.get("dataset", {}).get("aggregated_examples", 0))),
        runs=html.escape(str(summary.get("market", {}).get("honest_n_market_runs", 0))),
        metric_rows="".join(metric_rows),
        action_gini=html.escape(_fmt(disagreement.get("mean_action_gini"))),
        disagreement_n=html.escape(
            str(disagreement.get("states_with_at_least_two_valid_replicates", 0))
        ),
        intensity_variance=html.escape(
            _fmt(
                disagreement.get(
                    "mean_conditional_buy_sell_intensity_variance"
                )
            )
        ),
        baseline_failed=html.escape(
            str(baseline.get("deployed_model_failed_to_beat_a_baseline"))
        ),
        cards="".join(cards),
        ood_rows=html.escape(
            str(all_market_ood.get("outside_train_range_rows", 0))
        ),
        ood_n=html.escape(str(all_market_ood.get("n", 0))),
        ood_fraction=html.escape(
            _fmt(all_market_ood.get("outside_train_range_row_fraction"))
        ),
        model_requested=html.escape(str(summary.get("model_requested"))),
        reported_models=html.escape(str(summary.get("reported_models", []))),
        weights_verified=html.escape(
            str(
                summary.get("model_identity", {}).get(
                    "underlying_serving_weights_independently_verified", False
                )
            )
        ),
        science=html.escape(str(summary.get("v2_scientific_config_hash"))),
        request=html.escape(str(summary.get("v2_model_request_config_hash"))),
        execution=html.escape(str(summary.get("v2_execution_config_hash"))),
        full=html.escape(str(summary.get("v2_full_effective_config_hash"))),
        serialized=serialized,
    )


def _sample_plan(observations: Sequence[Any], replicates: int) -> list[dict[str, Any]]:
    from nmsim import v2_attention

    plan: list[dict[str, Any]] = []
    for observation in observations:
        prompt = v2_attention.render_teacher_prompt(observation)
        for replicate_index in range(replicates):
            sample_id = v2_attention.sha256_hex(
                {
                    # Sample identity remains frozen across the v1/v2/v3/v4
                    # pilot succession.  The v3/v4 request/row projection uses
                    # 0.2, but that additive provenance change must not reorder
                    # or otherwise redefine the 162 planned samples.
                    "schema_version": SAMPLE_IDENTITY_SCHEMA_VERSION,
                    "state_id": observation.state_id,
                    "prompt_hash": prompt.prompt_hash,
                    "replicate_index": replicate_index,
                }
            )
            plan.append(
                {
                    "sample_id": sample_id,
                    "observation": observation,
                    "replicate_index": replicate_index,
                    "prompt": prompt,
                }
            )
    return plan


def _safe_private_detail(manager: ManagedRunContext, value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    return manager._manager._sanitize_text(value, max_length=None)


def _teacher_gate_result(
    *,
    args: argparse.Namespace,
    plan: Sequence[Mapping[str, Any]],
    public_rows: Sequence[Mapping[str, Any]],
    reported_models: Sequence[str],
    attempted_samples: Optional[int] = None,
) -> dict[str, Any]:
    """Evaluate the frozen pilot gate without weakening honest-N semantics."""

    pilot = pilot_profile_descriptor(args.pilot_profile)
    if pilot is None:
        return {"enabled": False, "status": "not_applicable"}
    valid_rows = [row for row in public_rows if row.get("status") == "valid"]
    counts = Counter(str(row["state_id"]) for row in valid_rows)
    required_per_state = int(args.replicates)
    expected_state_ids = sorted(
        {str(item["observation"].state_id) for item in plan}
    )
    reason_codes: list[str] = []
    attempted = len(public_rows) if attempted_samples is None else int(attempted_samples)
    if attempted != len(plan):
        reason_codes.append("not_all_planned_samples_attempted")
    if len(public_rows) != len(plan):
        reason_codes.append("not_all_planned_samples_resolved")
    if len(valid_rows) != len(plan):
        reason_codes.append("not_all_planned_samples_valid")
    if any(
        row.get("model_requested") != pilot["model_requested"]
        for row in public_rows
    ):
        reason_codes.append("requested_model_identity_not_exact_match")
    if any(
        row.get("reported_model") != pilot["required_reported_model"]
        for row in public_rows
    ):
        reason_codes.append("reported_model_row_identity_not_exact_match")
    termination_contract = pilot.get("response_termination_contract")
    if termination_contract is not None and any(
        row.get("finish_reason")
        != termination_contract["required_finish_reason"]
        for row in public_rows
    ):
        reason_codes.append("finish_reason_not_exact_match")
    if any(counts[state_id] != required_per_state for state_id in expected_state_ids):
        reason_codes.append("state_replicate_coverage_incomplete")
    if sorted(set(reported_models)) != [pilot["required_reported_model"]]:
        reason_codes.append("reported_model_identity_not_unique_exact_match")
    return {
        "enabled": True,
        "profile_id": args.pilot_profile,
        "status": "passed" if not reason_codes else "failed",
        "reason_codes": reason_codes,
        "canary_sample_id": plan[0]["sample_id"],
        "canary_policy": "first_planned_sample_then_strict_sequential_release",
        "planned_samples": len(plan),
        "attempted_samples": attempted,
        "resolved_samples": len(public_rows),
        "valid_samples": len(valid_rows),
        "planned_states": len(expected_state_ids),
        "states_with_exact_required_replicates": sum(
            counts[state_id] == required_per_state
            for state_id in expected_state_ids
        ),
        "required_valid_replicates_per_state": required_per_state,
        "required_requested_model": pilot["model_requested"],
        "required_reported_model": pilot["required_reported_model"],
        "required_finish_reason": (
            termination_contract["required_finish_reason"]
            if termination_contract is not None
            else None
        ),
        "reported_models": sorted(set(reported_models)),
        "student_and_market_released": not reason_codes,
    }


def run_teacher_phase(
    manager: ManagedRunContext,
    *,
    args: argparse.Namespace,
    observations: Sequence[Any],
) -> dict[str, Any]:
    """Execute and durably project each Teacher completion as it resolves."""

    from nmsim import v2_attention

    plan = _sample_plan(observations, args.replicates)
    pilot = pilot_profile_descriptor(args.pilot_profile)
    request_schema_version = _teacher_request_schema_version(
        args.pilot_profile
    )
    if args.provider == "openai":
        provider: Any = _build_openai_provider(args)
        runtime_details = {
            "provider": "openai",
            "model": args.model,
            "live": True,
            "application_concurrency_limit": args.workers,
            "provider_connection_limit": args.workers,
        }
        if args.pilot_profile in {
            MINIMAX_M27_HIGGSAI_LONG_TIMEOUT_JOINT54X3_PILOT,
            MINIMAX_M27_HIGGSAI_TIMEOUT600_OUTPUT16384_JOINT54X3_PILOT,
        }:
            runtime_details.update(
                {
                    "httpx_phase_inactivity_timeout_seconds": (
                        _request_timeout_seconds(args.pilot_profile)
                    ),
                    "hard_request_deadline_seconds": (
                        _hard_request_deadline_seconds(args.pilot_profile)
                    ),
                    "connect_timeout_seconds": PROVIDER_CONNECT_TIMEOUT_SECONDS,
                    "provider_retry_count": 0,
                }
            )
        else:
            runtime_details.update(
                {
                    "request_timeout_seconds": _request_timeout_seconds(
                        args.pilot_profile
                    ),
                    "connect_timeout_seconds": PROVIDER_CONNECT_TIMEOUT_SECONDS,
                    "provider_retry_count": 0,
                }
            )
    else:
        provider = FakeTeacherProvider(args.provider)
        runtime_details = {
            "provider": args.provider,
            "model": provider.model,
            "live": False,
            "application_concurrency_limit": 1,
            "provider_connection_limit": 0,
        }
    manager.active_llm = provider
    manager.llm_mode = "record"
    manager.network_access = False
    manager.register_llm_runtime(
        mode=RUN_KIND,
        cache_enabled=False,
        network_access=False,
        **runtime_details,
    )

    public_rows: list[dict[str, Any]] = []
    private_rows: list[dict[str, Any]] = []
    replicate_results: list[Any] = []
    processed_indices: set[int] = set()
    reported_models: set[str] = set()
    input_tokens = 0
    output_tokens = 0
    invalid_reported_model_alias_count = 0
    invalid_finish_reason_count = 0
    missing_finish_reason_count = 0
    provider_exception_count = 0
    finish_reason_counts: Counter[str] = Counter()
    teacher_completion: dict[str, Any] = {
        "unit": "teacher_samples",
        "planned": len(plan),
        "attempted": 0,
        "resolved": 0,
        "raw_responses": 0,
        "valid": 0,
        "failed": 0,
        "failure_counts": {},
        "unresolved_attempts": 0,
        "skipped": len(plan),
        "honest_n_teacher_samples": 0,
    }
    manager.manifest["v2_attention_market"]["teacher_samples"] = dict(
        teacher_completion
    )
    if pilot is not None:
        manager.manifest["v2_attention_market"]["teacher_acceptance_gate"] = {
            "enabled": True,
            "profile_id": args.pilot_profile,
            "status": "pending",
            "reason_codes": [],
            "canary_sample_id": plan[0]["sample_id"],
            "canary_status": "pending",
            "student_and_market_released": False,
        }
    manager._write()

    public_stream = _open_exclusive(
        manager.run_dir / "teacher_samples.jsonl", 0o644
    )
    try:
        private_stream = _open_exclusive(
            manager.run_dir / "private_teacher_records.jsonl", 0o600
        )
    except BaseException:
        public_stream.close()
        raise

    def record_attempt(index: int) -> None:
        item = plan[index]
        observation = item["observation"]
        prompt = item["prompt"]
        manager.events.emit(
            "LLMRequestRecorded",
            agent_id=observation.state_id,
            data={
                "run_kind": RUN_KIND,
                "sample_id": item["sample_id"],
                "state_id": observation.state_id,
                "family_id": observation.family_id,
                "replicate_index": item["replicate_index"],
                "prompt_hash": prompt.prompt_hash,
                "request_schema_version": request_schema_version,
            },
            private_data={
                "system_prompt": prompt.system,
                "user_prompt": prompt.user,
            },
        )

    def refresh_accounting() -> None:
        valid = sum(row["status"] == "valid" for row in public_rows)
        resolved_failures = len(public_rows) - valid
        attempted = int(getattr(provider, "request_count", 0))
        resolved = len(public_rows)
        unresolved = max(0, attempted - resolved)
        raw_responses = sum(
            row["response_hash"] is not None for row in public_rows
        )
        teacher_completion.update(
            {
                "attempted": attempted,
                "resolved": resolved,
                "raw_responses": raw_responses,
                "valid": valid,
                "failed": resolved_failures + unresolved,
                "unresolved_attempts": unresolved,
                "skipped": max(0, len(plan) - attempted),
                "honest_n_teacher_samples": valid,
            }
        )
        manager.network_access = bool(getattr(provider, "network_access", False))
        manager.manifest["v2_attention_market"]["network_access"] = (
            manager.network_access
        )
        manager.sync_llm_accounting(provider)
        completion_accounting = manager.manifest["completion"]
        if args.provider == "openai":
            completion_accounting["provider_calls"].update(
                {
                    "attempted": attempted,
                    "succeeded": max(0, attempted - provider_exception_count),
                    "failed": provider_exception_count,
                }
            )
        completion_accounting["agent_decisions"].update(
            {
                "planned": len(plan),
                "attempted": attempted,
                "completed": valid,
                "failed": resolved_failures + unresolved,
                "skipped": max(0, len(plan) - attempted),
            }
        )
        parse_failures = sum(
            row["failure_code"] == "teacher_response_invalid"
            for row in public_rows
        )
        parse_attempts = sum(
            row["response_hash"] is not None
            and row["failure_code"]
            not in {"reported_model_mismatch", "finish_reason_invalid"}
            for row in public_rows
        )
        provider_exceptions = sum(
            row["failure_code"] == "provider_exception" for row in public_rows
        )
        failure_counts: dict[str, int] = {}
        for row in public_rows:
            code = row["failure_code"]
            if code is not None:
                failure_counts[code] = failure_counts.get(code, 0) + 1
        if unresolved:
            failure_counts["unresolved_after_interruption"] = unresolved
        teacher_completion["failure_counts"] = dict(sorted(failure_counts.items()))
        completion_accounting["parsing"].update(
            {
                "attempted": parse_attempts,
                "succeeded": valid,
                "failed": parse_failures,
                "fallbacks": 0,
            }
        )
        if args.provider == "openai":
            visible_attempts = completion_accounting[
                "application_provider_attempts"
            ]
            visible_attempts.update(
                {
                    "attempted": attempted,
                    "responses_received": int(
                        getattr(provider, "response_count", 0)
                    ),
                    "parse_failed_responses": parse_failures,
                    "provider_exceptions": provider_exceptions,
                    "retries_scheduled": 0,
                    "logical_requests_with_retry": 0,
                    "exhausted_logical_requests": resolved_failures + unresolved,
                    "reported_models": sorted(reported_models)[:16],
                    "reported_models_truncated": len(reported_models) > 16,
                    "invalid_reported_model_alias_count": (
                        invalid_reported_model_alias_count
                    ),
                }
            )
            if request_schema_version == FINISH_AUDIT_REQUEST_SCHEMA_VERSION:
                visible_attempts.update(
                    {
                        "finish_reason_counts": dict(
                            sorted(finish_reason_counts.items())
                        ),
                        "invalid_finish_reason_count": (
                            invalid_finish_reason_count
                        ),
                        "missing_finish_reason_count": (
                            missing_finish_reason_count
                        ),
                    }
                )
        manager.manifest["v2_attention_market"]["teacher_samples"] = dict(
            teacher_completion
        )
        manager.manifest["honest_n_teacher_samples"] = valid
        manager.manifest["v2_attention_market"]["reported_models"] = sorted(
            reported_models
        )
        manager.manifest["v2_attention_market"]["token_totals"] = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "source": "provider_usage_when_available",
        }
        manager._refresh_derived()
        manager._write()

    def record_completion(index: int, completion: TeacherCompletion) -> None:
        nonlocal input_tokens, output_tokens, invalid_reported_model_alias_count
        nonlocal invalid_finish_reason_count
        nonlocal missing_finish_reason_count
        nonlocal provider_exception_count
        if index in processed_indices or index < 0 or index >= len(plan):
            raise RuntimeError("Teacher completion index violated immutable plan")
        item = plan[index]
        observation = item["observation"]
        prompt = item["prompt"]
        raw_hash = (
            hashlib.sha256(completion.raw_response.encode("utf-8")).hexdigest()
            if completion.raw_response is not None
            else None
        )
        private_sdk_response_json = _safe_private_detail(
            manager, completion.provider_sdk_response_json
        )
        raw_reported_model = (
            completion.reported_model_raw
            if completion.reported_model_raw is not None
            else completion.reported_model
        )
        private_reported_model = _safe_private_detail(manager, raw_reported_model)
        private_response_id = _safe_private_detail(manager, completion.response_id)
        public_reported_model = _safe_public_model_alias(raw_reported_model)
        raw_finish_reason = (
            completion.finish_reason_raw
            if completion.finish_reason_raw is not None
            else completion.finish_reason
        )
        private_finish_reason = _safe_private_detail(manager, raw_finish_reason)
        public_finish_reason = _safe_public_finish_reason(raw_finish_reason)
        if public_finish_reason is not None:
            finish_reason_counts[public_finish_reason] += 1
        elif raw_finish_reason is not None:
            invalid_finish_reason_count += 1
        elif pilot is not None and "response_termination_contract" in pilot:
            missing_finish_reason_count += 1
        if public_reported_model:
            reported_models.add(public_reported_model)
        elif raw_reported_model is not None:
            invalid_reported_model_alias_count += 1
        input_tokens += completion.input_tokens or 0
        output_tokens += completion.output_tokens or 0
        parsed = None
        failure_code = None
        private_error = None
        if completion.raw_response is None:
            if completion.error_type != "ProviderResponseShapeError":
                provider_exception_count += 1
            failure_code = (
                "provider_response_shape_invalid"
                if completion.error_type == "ProviderResponseShapeError"
                else "provider_exception"
            )
            private_error = _safe_private_detail(manager, completion.error_detail)
        else:
            manager.events.emit(
                "LLMResponseRecorded",
                agent_id=observation.state_id,
                data={
                    "run_kind": RUN_KIND,
                    "sample_id": item["sample_id"],
                    "source": "record",
                    "response_hash": raw_hash,
                    "reported_model": public_reported_model,
                    "response_id_sha256": (
                        sha256_text(completion.response_id)
                        if completion.response_id is not None
                        else None
                    ),
                },
                private_data={
                    "raw_response": completion.raw_response,
                    "provider_response_id": private_response_id,
                    "provider_reported_model_raw": private_reported_model,
                },
            )
            if (
                pilot is not None
                and public_reported_model != pilot["required_reported_model"]
            ):
                failure_code = "reported_model_mismatch"
                private_error = "reported model did not match frozen pilot alias"
            elif (
                pilot is not None
                and "response_termination_contract" in pilot
                and public_finish_reason
                != pilot["response_termination_contract"][
                    "required_finish_reason"
                ]
            ):
                failure_code = "finish_reason_invalid"
                private_error = (
                    "finish reason did not match frozen pilot termination contract"
                )
            else:
                try:
                    parsed = v2_attention.parse_teacher_response(
                        completion.raw_response, observation.state
                    )
                except v2_attention.TeacherResponseError as error:
                    failure_code = "teacher_response_invalid"
                    private_error = _safe_private_detail(
                        manager, "{}: {}".format(type(error).__name__, error)
                    )

        if parsed is not None:
            replicate = v2_attention.TeacherReplicateResult.success(
                observation, item["replicate_index"], parsed
            )
            public_decision = parsed.public_record()
            private_rationale = parsed.private_rationale
            status = "valid"
        else:
            replicate = v2_attention.TeacherReplicateResult.failure(
                observation,
                item["replicate_index"],
                failure_code or "unknown_failure",
            )
            public_decision = None
            private_rationale = None
            status = "failed"
        public_row = {
            "schema_version": request_schema_version,
            "sample_id": item["sample_id"],
            "provider": args.provider,
            "model_requested": args.model or None,
            "reported_model": public_reported_model,
            "family_id": observation.family_id,
            "state_id": observation.state_id,
            "scientific_state_hash": observation.state.scientific_state_hash,
            "replicate_index": item["replicate_index"],
            "prompt_hash": prompt.prompt_hash,
            "response_hash": raw_hash,
            "status": status,
            "decision": public_decision,
            "failure_code": failure_code,
            "input_tokens": completion.input_tokens,
            "output_tokens": completion.output_tokens,
        }
        if request_schema_version == FINISH_AUDIT_REQUEST_SCHEMA_VERSION:
            public_row["finish_reason"] = public_finish_reason
        private_row = {
            **public_row,
            "system_prompt": prompt.system,
            "user_prompt": prompt.user,
            "raw_response": completion.raw_response,
            "private_rationale": private_rationale,
            "provider_error_type": completion.error_type,
            "provider_error_detail": private_error,
            "provider_response_id": private_response_id,
            "provider_response_id_sha256": (
                sha256_text(completion.response_id)
                if completion.response_id is not None
                else None
            ),
            "provider_reported_model_raw": private_reported_model,
            "provider_reported_model_raw_sha256": (
                sha256_text(raw_reported_model)
                if raw_reported_model is not None
                else None
            ),
        }
        if request_schema_version == FINISH_AUDIT_REQUEST_SCHEMA_VERSION:
            private_row.update(
                {
                    "provider_finish_reason_raw": private_finish_reason,
                    "provider_finish_reason_raw_sha256": (
                        sha256_text(raw_finish_reason)
                        if raw_finish_reason is not None
                        else None
                    ),
                    "provider_sdk_response_json": private_sdk_response_json,
                    "provider_sdk_response_json_sha256": (
                        sha256_text(completion.provider_sdk_response_json)
                        if completion.provider_sdk_response_json is not None
                        else None
                    ),
                }
            )
        _write_jsonl_row(private_stream, private_row)
        os.fsync(private_stream.fileno())
        _write_jsonl_row(public_stream, public_row)
        os.fsync(public_stream.fileno())
        processed_indices.add(index)
        public_rows.append(public_row)
        private_rows.append(private_row)
        replicate_results.append(replicate)
        manager.events.emit(
            "V2TeacherSampleValidated",
            agent_id=observation.state_id,
            data={
                "sample_id": item["sample_id"],
                "status": status,
                "failure_code": failure_code,
                "response_schema_version": v2_attention.RESPONSE_SCHEMA_VERSION,
            },
            private_data={
                "private_rationale": private_rationale,
                "validation_error": private_error,
            },
        )
        refresh_accounting()
        if pilot is not None:
            gate = manager.manifest["v2_attention_market"][
                "teacher_acceptance_gate"
            ]
            if index == 0:
                gate["canary_status"] = "passed" if status == "valid" else "failed"
                manager._write()
            if status != "valid":
                gate.update(
                    {
                        "status": "failed",
                        "reason_codes": [failure_code or "unknown_failure"],
                        "student_and_market_released": False,
                    }
                )
                manager._write()
                raise V2TeacherGateError(
                    "frozen Teacher pilot stopped after a resolved sample failure"
                )

    try:
        if args.provider == "openai":

            async def execute_and_close() -> list[TeacherCompletion]:
                try:
                    call_kwargs = {
                        "before_attempt": record_attempt,
                        "on_completion": record_completion,
                    }
                    if pilot is not None:
                        call_kwargs["strict_sequential"] = True
                    return await provider.complete_many(
                        [
                            (item["prompt"].system, item["prompt"].user)
                            for item in plan
                        ],
                        **call_kwargs,
                    )
                finally:
                    await provider.aclose()

            completions = asyncio.run(execute_and_close())
        else:
            completions = provider.complete_plan(
                plan,
                before_attempt=record_attempt,
                on_completion=record_completion,
            )
        if len(completions) != len(plan) or len(processed_indices) != len(plan):
            raise RuntimeError(
                "Teacher completion count did not match the immutable plan"
            )
        gate = _teacher_gate_result(
            args=args,
            plan=plan,
            public_rows=public_rows,
            reported_models=sorted(reported_models),
            attempted_samples=int(getattr(provider, "request_count", 0)),
        )
        if pilot is not None:
            gate["canary_status"] = "passed"
            manager.manifest["v2_attention_market"][
                "teacher_acceptance_gate"
            ] = gate
            manager._write()
            if gate["status"] != "passed":
                raise V2TeacherGateError(
                    "frozen Teacher acceptance gate rejected downstream execution"
                )
    finally:
        try:
            refresh_accounting()
        finally:
            private_stream.close()
            public_stream.close()

    return {
        "public_rows": public_rows,
        "private_rows": private_rows,
        "replicate_results": replicate_results,
        "completion": teacher_completion,
        "model_resolved": (
            sorted(reported_models)[0]
            if len(reported_models) == 1
            else provider.model
        ),
        "reported_models": sorted(reported_models),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


def _state_tape_regime(state: Any) -> str:
    if state.return_20d < TAPE_DECLINE_UPPER_EXCLUSIVE:
        tape = "decline"
    elif state.return_20d > TAPE_RISE_LOWER_EXCLUSIVE:
        tape = "rise"
    else:
        tape = "flat"
    return tape


def _state_diagnostic_stratum(state: Any) -> str:
    tape = _state_tape_regime(state)
    if state.position_fraction < POSITION_CASH_UPPER_EXCLUSIVE:
        position = "cash"
    elif state.position_fraction > POSITION_INVESTED_LOWER_EXCLUSIVE:
        position = "invested"
    else:
        position = "mixed"
    return "{}:{}".format(tape, position)


def _state_stratum(state: Any) -> str:
    return _state_diagnostic_stratum(state)


def _split_regime_coverage(
    observations: Sequence[Any],
    family_assignments: Mapping[str, str],
    family_strata: Optional[Mapping[str, str]] = None,
) -> dict[str, Any]:
    partitions = ("train", "validation", "test")
    tape_regimes = ("decline", "flat", "rise")
    counts = {
        partition: {
            "tape_regimes": {regime: 0 for regime in tape_regimes},
            "tape_x_position": {},
        }
        for partition in partitions
    }
    for observation in observations:
        partition = family_assignments[observation.family_id]
        tape = _state_tape_regime(observation.state)
        diagnostic = _state_diagnostic_stratum(observation.state)
        counts[partition]["tape_regimes"][tape] += 1
        matrix = counts[partition]["tape_x_position"]
        matrix[diagnostic] = matrix.get(diagnostic, 0) + 1
    for partition in partitions:
        counts[partition]["tape_x_position"] = dict(
            sorted(counts[partition]["tape_x_position"].items())
        )
    missing = [
        "{}:{}".format(partition, regime)
        for partition in partitions
        for regime in tape_regimes
        if counts[partition]["tape_regimes"][regime] == 0
    ]
    design_strata = tuple(
        "{}:{}".format(tape, position)
        for tape in tape_regimes
        for position in ("cash", "mixed", "invested")
    )
    missing_design_strata = [
        "{}:{}".format(partition, stratum)
        for partition in partitions
        for stratum in design_strata
        if counts[partition]["tape_x_position"].get(stratum, 0) == 0
    ]
    joint_split = bool(family_strata) and all(
        ":" in value for value in family_strata.values()
    )
    return {
        "split_stratification_unit": (
            "return_20d_tape_regime_x_position_fraction_regime"
            if joint_split
            else "return_20d_tape_regime_small_design_fallback"
        ),
        "partition_counts": counts,
        "all_partitions_cover_all_tape_regimes": not missing,
        "missing_partition_tape_regimes": missing,
        "all_partitions_cover_all_design_strata": not missing_design_strata,
        "missing_partition_design_strata": missing_design_strata,
        "confirmatory_coverage_threshold_frozen": False,
    }


def _group_split_seed(seed: int) -> int:
    from nmsim import v2_attention

    return v2_attention.derive_seed(
        seed, "student-group-split", namespace=PROTOCOL_VERSION
    ) % (2 ** 31)


def preflight_group_split(observations: Sequence[Any], seed: int) -> Any:
    """Prove all three family partitions exist before any Teacher call."""

    from nmsim import v2_distillation

    joint_counts: dict[str, int] = {}
    for observation in observations:
        stratum = _state_diagnostic_stratum(observation.state)
        joint_counts[stratum] = joint_counts.get(stratum, 0) + 1
    use_joint_strata = (
        len(joint_counts) == JOINT_SPLIT_REQUIRED_STRATA
        and min(joint_counts.values()) >= JOINT_SPLIT_MIN_FAMILIES_PER_STRATUM
    )
    placeholders = [
        {
            "family_id": observation.family_id,
            "state_id": observation.state_id,
            "features": list(observation.state.to_feature_vector()),
            "target_probs": [0.0, 1.0, 0.0],
            "intensity_targets": [0.0, 0.0],
            "intensity_weights": [0.0, 0.0],
            "stratum": (
                _state_diagnostic_stratum(observation.state)
                if use_joint_strata
                else _state_tape_regime(observation.state)
            ),
        }
        for observation in observations
    ]
    split = v2_distillation.deterministic_group_split(
        placeholders,
        seed=_group_split_seed(seed),
        fractions=SPLIT_FRACTIONS,
        stratum_key="stratum",
    )
    if not split.train or not split.validation or not split.test:
        counts = split.to_dict()["counts"]
        raise V2ProtocolError(
            "state design cannot form non-empty train/validation/test family groups: "
            "train={}, validation={}, test={}".format(
                counts["train_rows"],
                counts["validation_rows"],
                counts["test_rows"],
            )
        )
    coverage = _split_regime_coverage(
        observations, split.family_assignments, split.family_strata
    )
    if not coverage["all_partitions_cover_all_tape_regimes"]:
        raise V2ProtocolError(
            "state design does not cover every tape regime in every partition: {}".format(
                coverage["missing_partition_tape_regimes"]
            )
        )
    return split


def build_training_examples(
    observations: Sequence[Any],
    targets: Sequence[Any],
    *,
    family_strata: Optional[Mapping[str, str]] = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Project aggregate Teacher targets to the numeric Student contract."""

    from nmsim import v2_attention

    by_state = {observation.state_id: observation for observation in observations}
    training_examples: list[dict[str, Any]] = []
    public_dataset: list[dict[str, Any]] = []
    for target in targets:
        observation = by_state.get(target.state_id)
        if observation is None or observation.family_id != target.family_id:
            raise V2ProtocolError("aggregate target does not match the state design")
        target_dict = target.to_dict()
        stratum = (
            family_strata[observation.family_id]
            if family_strata is not None
            else _state_stratum(observation.state)
        )
        dataset_row = {
            "observation": observation.to_dict(),
            "soft_target": target_dict,
            "stratum": stratum,
        }
        public_dataset.append(dataset_row)
        if target.valid_n == 0:
            continue
        probabilities = [
            target.action_probabilities[action]
            for action in v2_attention.ACTION_ORDER
        ]
        if any(value is None for value in probabilities):
            raise V2ProtocolError("non-empty target has missing action probabilities")
        buy_stats = target.conditional_intensity["buy"]
        sell_stats = target.conditional_intensity["sell"]
        training_examples.append(
            {
                "family_id": target.family_id,
                "state_id": target.state_id,
                "features": list(observation.state.to_feature_vector()),
                "target_probs": [float(value) for value in probabilities],
                "intensity_targets": [
                    0.0 if buy_stats.mean is None else buy_stats.mean,
                    0.0 if sell_stats.mean is None else sell_stats.mean,
                ],
                "intensity_weights": [
                    float(target.action_probabilities["buy"] or 0.0),
                    float(target.action_probabilities["sell"] or 0.0),
                ],
                "stratum": stratum,
            }
        )
    if not training_examples:
        raise V2ProtocolError("no valid Teacher samples are available for distillation")
    return training_examples, public_dataset


def run_distillation_phase(
    manager: ManagedRunContext,
    *,
    args: argparse.Namespace,
    observations: Sequence[Any],
    replicate_results: Sequence[Any],
) -> dict[str, Any]:
    from nmsim import v2_attention, v2_distillation

    targets = v2_attention.aggregate_teacher_samples(replicate_results)
    planned_split = preflight_group_split(observations, args.seed)
    examples, public_dataset = build_training_examples(
        observations,
        targets,
        family_strata=planned_split.family_strata,
    )
    action_ginis = [
        1.0
        - sum(
            float(target.action_probabilities[action] or 0.0) ** 2
            for action in v2_attention.ACTION_ORDER
        )
        for target in targets
        if target.valid_n >= 2
    ]
    conditional_variances = [
        float(stats.variance)
        for target in targets
        for action, stats in target.conditional_intensity.items()
        if action in {"buy", "sell"}
        and stats.n >= 2
        and stats.variance is not None
    ]
    teacher_disagreement = {
        "action_metric": "gini_impurity_1_minus_sum_p_squared",
        "states_with_at_least_two_valid_replicates": len(action_ginis),
        "mean_action_gini": (
            statistics.fmean(action_ginis) if action_ginis else None
        ),
        "conditional_intensity_variance_entries": len(conditional_variances),
        "mean_conditional_buy_sell_intensity_variance": (
            statistics.fmean(conditional_variances)
            if conditional_variances
            else None
        ),
    }
    split = v2_distillation.apply_frozen_family_assignments(
        examples,
        family_assignments=planned_split.family_assignments,
        family_strata=planned_split.family_strata,
        seed=planned_split.seed,
        fractions=planned_split.fractions,
        stratum_key="stratum",
    )
    if not split.train or not split.validation or not split.test:
        raise V2ProtocolError(
            "group split produced an empty train/validation/test partition"
        )
    standardizer = v2_distillation.Standardizer.fit(split.train)
    prior = v2_distillation.ActionPrior.fit(split.train)
    linear = v2_distillation.LinearSoftmaxStudent(
        len(v2_attention.FEATURE_ORDER),
        seed=v2_attention.derive_seed(
            args.seed, "linear-student", namespace=PROTOCOL_VERSION
        ) % (2 ** 31),
    )
    linear_history = linear.fit(
        split.train,
        standardizer,
        epochs=args.training_epochs,
        learning_rate=0.02,
        intensity_loss_weight=1.0,
        l2=1e-4,
    )["loss"]
    mlp = v2_distillation.TanhMLPStudent(
        len(v2_attention.FEATURE_ORDER),
        hidden_dim=16,
        seed=v2_attention.derive_seed(
            args.seed, "mlp-student", namespace=PROTOCOL_VERSION
        ) % (2 ** 31),
    )
    mlp_history = mlp.fit(
        split.train,
        standardizer,
        epochs=args.training_epochs,
        learning_rate=0.02,
        intensity_loss_weight=1.0,
        l2=1e-4,
    )["loss"]
    models = {"prior": prior, "linear": linear, "mlp": mlp}
    evaluations: dict[str, Any] = {}
    for model_name, model in models.items():
        evaluations[model_name] = {
            "train": v2_distillation.evaluate_model(model, split.train),
            "validation": v2_distillation.evaluate_model(model, split.validation),
            "test": v2_distillation.evaluate_model(model, split.test),
        }
    test_cross_entropies = {
        name: values["test"]["action_cross_entropy"]
        for name, values in evaluations.items()
    }
    baseline_comparison = {
        "metric": "frozen_test_action_cross_entropy_descriptive_only",
        "test_cross_entropy": test_cross_entropies,
        "mlp_beats_prior": (
            test_cross_entropies["mlp"] < test_cross_entropies["prior"]
        ),
        "mlp_beats_linear": (
            test_cross_entropies["mlp"] < test_cross_entropies["linear"]
        ),
        "deployed_model_failed_to_beat_a_baseline": (
            test_cross_entropies["mlp"]
            >= min(test_cross_entropies["prior"], test_cross_entropies["linear"])
        ),
        "used_for_model_selection": False,
    }
    reference = v2_distillation.build_ood_reference(
        split.train, standardizer=standardizer
    )
    ood_reference_hash = stable_hash(reference.to_dict())
    ood = {
        "reference": reference.to_dict(),
        "reference_hash": ood_reference_hash,
        "fit_partition": "train_only",
        "support_geometry": "per_feature_train_min_max_rectangle",
        "joint_support_assessed": False,
        "validation": v2_distillation.ood_diagnostics(
            split.validation,
            reference,
            z_threshold=v2_distillation.OOD_Z_THRESHOLD,
        ),
        "test": v2_distillation.ood_diagnostics(
            split.test,
            reference,
            z_threshold=v2_distillation.OOD_Z_THRESHOLD,
        ),
    }
    split_manifest = split.to_dict()
    split_manifest["planned_design_partition_regime_coverage"] = (
        _split_regime_coverage(
            observations, split.family_assignments, split.family_strata
        )
    )
    eligible_state_ids = {example["state_id"] for example in examples}
    eligible_observations = [
        observation
        for observation in observations
        if observation.state_id in eligible_state_ids
    ]
    split_manifest["training_eligible_partition_regime_coverage"] = (
        _split_regime_coverage(
            eligible_observations,
            split.family_assignments,
            split.family_strata,
        )
    )
    split_manifest["planned_split_hash"] = stable_hash(planned_split.to_dict())
    split_manifest["frozen_family_assignment_hash"] = stable_hash(
        planned_split.family_assignments
    )
    split_manifest["split_hash"] = stable_hash(split_manifest)
    dataset_hash = stable_hash(
        {
            "schema_version": "v2_teacher_aggregated_dataset/0.1",
            "rows": sorted(
                public_dataset,
                key=lambda row: row["observation"]["state_id"],
            ),
        }
    )
    training_projection_hash = v2_distillation.canonical_observations_hash(
        examples, stratum_key="stratum"
    )
    preprocessing = {
        **standardizer.to_dict(),
        "feature_order": list(v2_attention.FEATURE_ORDER),
        "fit_partition": "train_only",
        "training_projection_hash": training_projection_hash,
    }
    evaluation_payload = {
        "schema_version": "v2_student_evaluation/0.1",
        "teacher_kind": (
            "engineering_fake_not_human_evidence"
            if args.provider != "openai"
            else "real_endpoint_teacher_not_human_ground_truth"
        ),
        "dataset_hash": dataset_hash,
        "training_projection_hash": training_projection_hash,
        "split_hash": split_manifest["split_hash"],
        "teacher_disagreement": teacher_disagreement,
        "held_out_baseline_comparison": baseline_comparison,
        "evaluations": evaluations,
        "training_trace": {
            "trace_semantics": "pre_update_full_batch_objective_at_each_epoch",
            "linear": {
                "first_pre_update_objective": linear_history[0],
                "last_pre_update_objective": linear_history[-1],
                "epochs": len(linear_history),
            },
            "mlp": {
                "first_pre_update_objective": mlp_history[0],
                "last_pre_update_objective": mlp_history[-1],
                "epochs": len(mlp_history),
            },
        },
        "deployed_model": "mlp_fixed_by_protocol",
        "interpretation": "agreement with this Teacher only; not human validity",
    }
    _write_json_exclusive(
        manager.run_dir / "aggregated_dataset.json",
        {
            "schema_version": "v2_teacher_aggregated_dataset/0.1",
            "dataset_hash": dataset_hash,
            "training_projection_hash": training_projection_hash,
            "rows": public_dataset,
        },
    )
    _write_json_exclusive(manager.run_dir / "split_manifest.json", split_manifest)
    _write_json_exclusive(manager.run_dir / "preprocessing.json", preprocessing)
    prior_path = manager.run_dir / "action_prior.json"
    linear_path = manager.run_dir / "linear_student.json"
    mlp_path = manager.run_dir / "mlp_student.json"
    _write_json_exclusive(prior_path, prior.to_dict())
    _write_json_exclusive(linear_path, linear.to_dict())
    _write_json_exclusive(mlp_path, mlp.to_dict())
    model_envelope = {
        "schema_version": "v2_student_model_envelope/0.1",
        "feature_order": list(v2_attention.FEATURE_ORDER),
        "state_contract_hash": v2_attention.CONTRACT_HASH,
        "dataset_hash": dataset_hash,
        "training_projection_hash": training_projection_hash,
        "split_hash": split_manifest["split_hash"],
        "ood_reference_hash": ood_reference_hash,
        "deployed_model": "mlp_fixed_by_protocol",
        "models": {
            "prior": {
                "path": "action_prior.json",
                "model_semantic_hash": stable_hash(prior.to_dict()),
                "artifact_sha256": _file_sha256(prior_path),
            },
            "linear": {
                "path": "linear_student.json",
                "model_semantic_hash": linear.model_hash(),
                "artifact_sha256": _file_sha256(linear_path),
            },
            "mlp": {
                "path": "mlp_student.json",
                "model_semantic_hash": mlp.model_hash(),
                "artifact_sha256": _file_sha256(mlp_path),
            },
        },
    }
    model_envelope["model_envelope_hash"] = stable_hash(model_envelope)
    model_envelope_path = manager.run_dir / "student_model_envelope.json"
    _write_json_exclusive(model_envelope_path, model_envelope)
    model_envelope_artifact_sha256 = _file_sha256(model_envelope_path)
    _write_json_exclusive(
        manager.run_dir / "student_evaluation.json", evaluation_payload
    )
    _write_json_exclusive(manager.run_dir / "ood_diagnostics.json", ood)
    dataset_completion = {
        "unit": "aggregated_state_examples",
        "planned_states": len(observations),
        "aggregated_states": len(targets),
        "training_eligible_states": len(examples),
        "states_without_valid_response": len(targets) - len(examples),
        "honest_n_aggregated_examples": len(examples),
    }
    manager.manifest["v2_attention_market"]["dataset"] = {
        **dataset_completion,
        "dataset_hash": dataset_hash,
        "training_projection_hash": training_projection_hash,
        "split_hash": split_manifest["split_hash"],
    }
    manager.manifest["v2_attention_market"]["student"] = {
        "deployed_model": "mlp",
        "prior_hash": stable_hash(prior.to_dict()),
        "linear_model_hash": linear.model_hash(),
        "mlp_model_hash": mlp.model_hash(),
        "model_envelope_hash": model_envelope["model_envelope_hash"],
        "model_envelope_artifact_sha256": model_envelope_artifact_sha256,
        "ood_reference_hash": ood_reference_hash,
    }
    manager.manifest["honest_n_aggregated_examples"] = len(examples)
    manager._write()
    return {
        "targets": targets,
        "examples": examples,
        "public_dataset": public_dataset,
        "split": split,
        "split_manifest": split_manifest,
        "standardizer": standardizer,
        "prior": prior,
        "linear": linear,
        "mlp": mlp,
        "evaluations": evaluations,
        "teacher_disagreement": teacher_disagreement,
        "baseline_comparison": baseline_comparison,
        "model_envelope": model_envelope,
        "model_envelope_artifact_sha256": model_envelope_artifact_sha256,
        "ood_reference": reference,
        "ood_reference_hash": ood_reference_hash,
        "ood": ood,
        "dataset_hash": dataset_hash,
        "training_projection_hash": training_projection_hash,
        "completion": dataset_completion,
    }


def _source_fingerprint(
    repo_root: Path,
    *,
    relative_paths: Sequence[str],
    schema_version: str,
    role: str,
) -> dict[str, Any]:
    files: dict[str, str] = {}
    for relative in relative_paths:
        path = repo_root / relative
        if not path.is_file():
            raise V2ProtocolError(
                "missing V2 {} component: {}".format(role, relative)
            )
        files[relative] = _file_sha256(path)
    return {
        "schema_version": schema_version,
        "files": files,
        "sha256": stable_hash(
            {"schema_version": schema_version, "files": files}
        ),
    }


def _component_fingerprint(repo_root: Path) -> dict[str, Any]:
    return _source_fingerprint(
        repo_root,
        relative_paths=(
            "nmsim/v2_attention.py",
            "nmsim/v2_distillation.py",
            "nmsim/v2_market.py",
            "nmsim/v2_market_experiment.py",
            "experiments/v2_attention_market.py",
        ),
        schema_version="v2_scientific_component_fingerprint/0.1",
        role="scientific",
    )


def _execution_component_fingerprint(repo_root: Path) -> dict[str, Any]:
    return _source_fingerprint(
        repo_root,
        relative_paths=(
            "experiments/v2_attention_market.py",
            "nmsim/config.py",
            "nmsim/entrypoints.py",
            "nmsim/events.py",
            "nmsim/managed_cli.py",
            "nmsim/provider_attempts.py",
            "nmsim/provenance.py",
            "nmsim/run_context.py",
        ),
        schema_version="v2_execution_component_fingerprint/0.1",
        role="execution",
    )


def build_v2_identities(
    args: argparse.Namespace,
    observations: Sequence[Any],
    *,
    repo_root: Path,
) -> dict[str, Any]:
    from nmsim import v2_attention, v2_distillation
    from nmsim.v2_market_experiment import market_experiment_descriptor

    state_design = [observation.to_dict() for observation in observations]
    sample_plan = _sample_plan(observations, args.replicates)
    pilot = pilot_profile_descriptor(args.pilot_profile)
    planned_split = preflight_group_split(observations, args.seed)
    planned_split_manifest = planned_split.to_dict()
    planned_split_hash = stable_hash(planned_split_manifest)
    frozen_family_assignment_hash = stable_hash(
        planned_split.family_assignments
    )
    component_fingerprint = _component_fingerprint(repo_root)
    execution_component_fingerprint = _execution_component_fingerprint(repo_root)
    if pilot is not None:
        frozen_checks = {
            "state_design_hash": stable_hash(state_design),
            "planned_split_hash": planned_split_hash,
            "planned_sample_order_hash": stable_hash(
                [item["sample_id"] for item in sample_plan]
            ),
            "canary_sample_id": sample_plan[0]["sample_id"],
        }
        mismatches = [
            key
            for key, actual in frozen_checks.items()
            if actual != pilot[key]
        ]
        if planned_split_manifest["counts"] != pilot["planned_split_counts"]:
            mismatches.append("planned_split_counts")
        coverage = _split_regime_coverage(
            observations,
            planned_split.family_assignments,
            planned_split.family_strata,
        )
        if (
            coverage["split_stratification_unit"]
            != "return_20d_tape_regime_x_position_fraction_regime"
            or not coverage["all_partitions_cover_all_design_strata"]
        ):
            mismatches.append("planned_joint_stratum_coverage")
        if mismatches:
            raise V2ProviderGuardError(
                "--pilot-profile generated design does not match frozen fields: {}".format(
                    ", ".join(sorted(set(mismatches)))
                )
            )
    scientific = {
        "schema_version": PROTOCOL_VERSION,
        "identity": "v2_scientific_config",
        "teacher_contract": v2_attention.contract_descriptor(),
        "teacher_contract_hash": v2_attention.CONTRACT_HASH,
        "state_design_contract": v2_attention.state_design_descriptor(),
        "state_design_count": len(observations),
        "state_design_hash": stable_hash(state_design),
        "state_design_seed": args.seed,
        "teacher_sampling": {
            "replicates_per_state": args.replicates,
            "planned_requests": len(observations) * args.replicates,
            "cache_enabled": False,
            "replicate_identity_derivation": "sha256 content identity",
            "fake_test_teacher_rng": "sha256 integer sub-seed",
            "real_provider_request_seed": None,
            "real_provider_seed_support": "unsupported_and_not_sent",
            "pilot_profile_id": args.pilot_profile,
            "teacher_acceptance_gate": (
                pilot["teacher_acceptance_gate"] if pilot is not None else None
            ),
        },
        "group_split": {
            "fractions": list(SPLIT_FRACTIONS),
            "unit": "family_id",
            "classification_and_allocation_contract": split_regime_descriptor(),
            "stratification": _split_regime_coverage(
                observations,
                planned_split.family_assignments,
                planned_split.family_strata,
            )["split_stratification_unit"],
            "seed_derivation": "sha256 integer sub-seed",
            "planned_split_hash": planned_split_hash,
            "frozen_family_assignment_hash": frozen_family_assignment_hash,
            "planned_counts": planned_split_manifest["counts"],
            "planned_design_partition_regime_coverage": _split_regime_coverage(
                observations,
                planned_split.family_assignments,
                planned_split.family_strata,
            ),
        },
        "ood_diagnostics": v2_distillation.ood_diagnostic_descriptor(),
        "student": {
            "feature_order": list(v2_attention.FEATURE_ORDER),
            "action_order": list(v2_attention.ACTION_ORDER),
            "baselines": ["action_prior", "linear_softmax"],
            "deployed_architecture": "one_hidden_tanh_16_softmax3_sigmoid2",
            "training_epochs": args.training_epochs,
            "optimizer": "full_batch_adam",
            "learning_rate": 0.02,
            "intensity_loss_weight": 1.0,
            "l2": 1e-4,
        },
        "market": {
            **market_experiment_descriptor(),
            "n_agents": args.market_agents,
            "rounds": args.market_rounds,
            "seeds": args.market_seeds,
            "paired_master_seed": args.seed,
        },
        "v2_scientific_component_fingerprint": component_fingerprint,
    }
    endpoint = os.environ.get("OPENAI_BASE_URL") if args.provider == "openai" else None
    endpoint_identity = _v2_endpoint_identity(endpoint)
    model_request = {
        "schema_version": _teacher_request_schema_version(
            args.pilot_profile
        ),
        "identity": "v2_model_request_config",
        "provider": args.provider,
        "model_requested": args.model or None,
        "temperature": args.temperature,
        "max_tokens": args.max_tokens,
        "cache_enabled": False,
        "state_count": args.states,
        "replicates_per_state": args.replicates,
        "planned_requests": args.states * args.replicates,
        "endpoint_identity": endpoint_identity,
        "endpoint_identity_sha256": endpoint_identity[
            "endpoint_identity_sha256"
        ],
        "provider_retry_count": 0,
        "request_seed": None,
        "request_seed_support": "unsupported_and_not_sent",
        "pilot_profile_id": args.pilot_profile,
        "required_reported_model": (
            pilot["required_reported_model"] if pilot is not None else None
        ),
        "planned_sample_order_hash": stable_hash(
            [item["sample_id"] for item in sample_plan]
        ),
        "canary_sample_id": (
            sample_plan[0]["sample_id"] if pilot is not None else None
        ),
    }
    if pilot is not None and pilot.get("response_termination_contract") is not None:
        model_request["response_termination_contract"] = pilot[
            "response_termination_contract"
        ]
    execution = {
        "schema_version": _execution_schema_version(args.pilot_profile),
        "identity": "v2_execution_config",
        "worker_count": args.workers,
        "dry_run": bool(args.dry_run),
        "live": bool(args.live),
        "output_root_identity_sha256": stable_hash(
            {
                "resolved_path": str(
                    (
                        Path(args.out).expanduser()
                        if Path(args.out).expanduser().is_absolute()
                        else (Path.cwd() / Path(args.out).expanduser()).resolve()
                    )
                )
            }
        ),
        "output_root_is_execution_only": True,
        "caller_run_id": args.run_id,
        "pilot_profile_id": args.pilot_profile,
        "strict_sequential_teacher_transport": pilot is not None,
        "first_planned_sample_is_canary": pilot is not None,
        "fail_fast_after_any_resolved_teacher_failure": pilot is not None,
        "v2_execution_component_fingerprint": execution_component_fingerprint,
    }
    if args.pilot_profile in {
        MINIMAX_M27_HIGGSAI_LONG_TIMEOUT_JOINT54X3_PILOT,
        MINIMAX_M27_HIGGSAI_TIMEOUT600_OUTPUT16384_JOINT54X3_PILOT,
    }:
        execution.update(
            {
                "httpx_phase_inactivity_timeout_seconds": _request_timeout_seconds(
                    args.pilot_profile
                ),
                "hard_request_deadline_seconds": _hard_request_deadline_seconds(
                    args.pilot_profile
                ),
                "connect_timeout_seconds": PROVIDER_CONNECT_TIMEOUT_SECONDS,
                "provider_retry_count": 0,
            }
        )
    scientific_hash = stable_hash(scientific)
    request_hash = stable_hash(model_request)
    execution_hash = stable_hash(execution)
    full = {
        "schema_version": FULL_CONFIG_SCHEMA_VERSION,
        "identity": "v2_full_effective_config",
        "v2_scientific_config_hash": scientific_hash,
        "v2_model_request_config_hash": request_hash,
        "v2_execution_config_hash": execution_hash,
    }
    return {
        "scientific_config": scientific,
        "model_request_config": model_request,
        "execution_config": execution,
        "v2_scientific_config_hash": scientific_hash,
        "v2_model_request_config_hash": request_hash,
        "v2_execution_config_hash": execution_hash,
        "v2_full_effective_config_hash": stable_hash(full),
        "full_effective_config": full,
        "state_design_hash": scientific["state_design_hash"],
        "component_fingerprint": component_fingerprint,
        "execution_component_fingerprint": execution_component_fingerprint,
    }


def _research_profile(identities: Mapping[str, Any]) -> dict[str, Any]:
    scientific = identities["scientific_config"]
    return {
        "profile_id": PROTOCOL_VERSION,
        "persona_contract": {
            "applicable": False,
            "reason": "V2 has no identity label or legacy Persona input",
        },
        "prompt_contract": {
            "schema_version": scientific["teacher_contract"]["prompt_schema_version"],
            "teacher_contract_hash": scientific["teacher_contract_hash"],
            "source_sha256": identities["component_fingerprint"]["files"][
                "nmsim/v2_attention.py"
            ],
        },
    }


def _initialise_manifest(
    manager: ManagedRunContext,
    *,
    args: argparse.Namespace,
    identities: Mapping[str, Any],
    planned_requests: int,
) -> None:
    pilot = pilot_profile_descriptor(args.pilot_profile)
    completion = manager.manifest["completion"]
    planned_market_runs = 0 if args.dry_run else 4 * args.market_seeds
    completion["simulation_runs"].update(
        {
            "planned": planned_market_runs,
            "started": 0,
            "completed": 0,
            "failed": 0,
        }
    )
    planned_market_rounds = (
        0 if args.dry_run else planned_market_runs * args.market_rounds
    )
    completion["rounds"].update(
        {
            "planned": planned_market_rounds,
            "started": 0,
            "completed": 0,
            "failed": 0,
            "skipped": planned_market_rounds,
        }
    )
    completion["agent_decisions"].update(
        {
            "planned": planned_requests,
            "attempted": 0,
            "completed": 0,
            "failed": 0,
            "skipped": planned_requests,
        }
    )
    completion["llm_logical_requests"].update(
        {
            "planned": planned_requests,
            "attempted": 0,
            "completed": 0,
            "failed": 0,
        }
    )
    manager.manifest["v2_attention_market"] = {
        "output_schema_version": OUTPUT_SCHEMA_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "dry_run": bool(args.dry_run),
        "live": bool(args.live),
        "provider_requested": args.provider,
        "model_requested": args.model or None,
        "network_access": False,
        "teacher_is_human_evidence": False,
        "fake_teacher": args.provider != "openai",
        "pilot_profile": pilot,
        "legacy_config_scope": "managed_lifecycle_infrastructure_only",
        "legacy_scientific_fingerprint_scope": "V1_compatibility_metadata_only",
        "v2_scientific_config_hash": identities["v2_scientific_config_hash"],
        "v2_model_request_config_hash": identities["v2_model_request_config_hash"],
        "v2_execution_config_hash": identities["v2_execution_config_hash"],
        "v2_full_effective_config_hash": identities[
            "v2_full_effective_config_hash"
        ],
        "v2_scientific_component_fingerprint": identities[
            "component_fingerprint"
        ],
        "v2_execution_component_fingerprint": identities[
            "execution_component_fingerprint"
        ],
        "state_design_hash": identities["state_design_hash"],
        "plan": {
            "states": args.states,
            "replicates_per_state": args.replicates,
            "planned_teacher_requests": planned_requests,
            "market_agents": args.market_agents,
            "market_rounds_per_run": args.market_rounds,
            "market_seeds": args.market_seeds,
            "planned_market_runs": 4 * args.market_seeds,
        },
        "scientific_claim_status": (
            "plan_only_no_teacher_or_market_result"
            if args.dry_run
            else (
                "engineering_fake_only"
                if args.provider != "openai"
                else (
                    "exploratory_endpoint_pilot_not_human_ground_truth"
                    if args.pilot_profile is not None
                    else "endpoint_teacher_only_not_human_ground_truth"
                )
            )
        ),
    }
    if pilot is not None:
        manager.manifest["v2_attention_market"]["teacher_acceptance_gate"] = {
            "enabled": True,
            "profile_id": args.pilot_profile,
            "status": "plan_only" if args.dry_run else "pending",
            "reason_codes": [],
            "canary_status": "not_attempted" if args.dry_run else "pending",
            "student_and_market_released": False,
        }
    manager.manifest["v2_config_identities"] = {
        key: identities[key]
        for key in (
            "scientific_config",
            "model_request_config",
            "execution_config",
            "full_effective_config",
            "v2_scientific_config_hash",
            "v2_model_request_config_hash",
            "v2_execution_config_hash",
            "v2_full_effective_config_hash",
        )
    }
    manager.manifest["honest_n_teacher_samples"] = 0
    manager.manifest["honest_n_aggregated_examples"] = 0
    manager.manifest["honest_n_market_runs"] = 0
    manager._write()


def run_market_phase(
    manager: ManagedRunContext,
    *,
    args: argparse.Namespace,
    student: Any,
    model_lineage: Mapping[str, Any],
    ood_reference: Any,
) -> dict[str, Any]:
    from nmsim.v2_market_experiment import run_budget_behavior_2x2

    planned = 4 * args.market_seeds
    planned_rounds = planned * args.market_rounds
    started_keys: list[tuple[str, int]] = []
    run_catalog: list[dict[str, Any]] = []
    completed_runs: list[dict[str, Any]] = []
    round_streams: dict[tuple[str, int], Any] = {}
    started_round_keys: set[tuple[str, int, int]] = set()
    completed_round_keys: set[tuple[str, int, int]] = set()
    terminal_failure = False

    def cell_accounting(failed_runs: int) -> dict[str, Any]:
        cells: dict[str, Any] = {}
        for financing in ("finite", "credit"):
            for behavior in ("distilled", "momentum"):
                cell = "{}_{}".format(financing, behavior)
                started = sum(key[0] == cell for key in started_keys)
                completed = sum(row["cell"] == cell for row in run_catalog)
                failed = max(0, started - completed) if failed_runs else 0
                cells[cell] = {
                    "planned_runs": args.market_seeds,
                    "started_runs": started,
                    "completed_runs": completed,
                    "failed_runs": failed,
                    "honest_n_runs": completed,
                }
        return cells

    def refresh_market_accounting(*, terminal: bool) -> None:
        started = len(started_keys)
        completed = len(run_catalog)
        failed = max(0, started - completed) if terminal else 0
        started_rounds = len(started_round_keys)
        completed_rounds = len(completed_round_keys)
        failed_rounds = max(0, started_rounds - completed_rounds) if terminal else 0
        completed_run_keys = {
            (str(row["cell"]), int(row["seed_index"])) for row in run_catalog
        }
        failed_run_keys = (
            set(started_keys) - completed_run_keys if terminal else set()
        )
        unstarted_round_slots_in_failed_runs = sum(
            max(
                0,
                args.market_rounds
                - sum(
                    round_key[:2] == run_key
                    for round_key in started_round_keys
                ),
            )
            for run_key in failed_run_keys
        )
        manager.manifest["completion"]["rounds"].update(
            {
                "planned": planned_rounds,
                "started": started_rounds,
                "completed": completed_rounds,
                "failed": failed_rounds,
                "skipped": max(0, planned_rounds - started_rounds),
            }
        )
        manager.set_experiment_completion(
            planned_runs=planned,
            started_runs=started,
            completed_runs=completed,
            failed_runs=failed,
            cells=cell_accounting(failed),
        )
        manager.manifest["simulation_computation_completed"] = completed == planned
        manager.manifest["honest_n_market_runs"] = completed
        manager.manifest["v2_attention_market"]["market"] = {
            "planned_runs": planned,
            "started_runs": started,
            "completed_runs": completed,
            "failed_runs": failed,
            "honest_n_market_runs": completed,
            "run_catalog": list(run_catalog),
            "rounds": {
                "unit": "durably_persisted_settled_market_rounds",
                "planned": planned_rounds,
                "started": started_rounds,
                "completed": completed_rounds,
                "failed_after_start": failed_rounds,
                "unstarted_round_slots_in_failed_runs": (
                    unstarted_round_slots_in_failed_runs
                ),
            },
            "all_persisted_round_conservation_checks_passed": (
                all(
                    all(
                        all(round_row["conservation"].values())
                        for round_row in run["rounds"]
                    )
                    for run in completed_runs
                )
                if completed_runs
                else None
            ),
        }
        manager._write()

    def on_run_started(cell: str, seed_index: int) -> None:
        key = (cell, seed_index)
        if key in started_keys:
            raise RuntimeError("duplicate V2 market run start")
        round_stream = _open_exclusive(
            manager.run_dir
            / "market_rounds_{}_seed_{:03d}.jsonl".format(cell, seed_index),
            0o644,
        )
        round_streams[key] = round_stream
        started_keys.append(key)
        manager.events.emit(
            "V2MarketRunStarted",
            data={"cell": cell, "seed_index": seed_index},
        )
        refresh_market_accounting(terminal=False)

    def on_round_started(cell: str, seed_index: int, round_index: int) -> None:
        run_key = (cell, seed_index)
        key = (cell, seed_index, round_index)
        if run_key not in round_streams or key in started_round_keys:
            raise RuntimeError("invalid or duplicate V2 market round start")
        started_round_keys.add(key)
        manager.events.emit(
            "V2MarketRoundStarted",
            data={
                "cell": cell,
                "seed_index": seed_index,
                "round_index": round_index,
            },
        )
        refresh_market_accounting(terminal=False)

    def on_round_completed(
        cell: str, seed_index: int, round_ledger: dict[str, Any]
    ) -> None:
        round_index = int(round_ledger["round_index"])
        run_key = (cell, seed_index)
        key = (cell, seed_index, round_index)
        if key not in started_round_keys or key in completed_round_keys:
            raise RuntimeError("invalid or duplicate V2 market round completion")
        stream = round_streams[run_key]
        _write_jsonl_row(
            stream,
            {
                "schema_version": "v2_durable_market_round/0.1",
                "model_lineage": dict(model_lineage),
                "round": round_ledger,
            },
        )
        os.fsync(stream.fileno())
        completed_round_keys.add(key)
        manager.events.emit(
            "V2MarketRoundCompleted",
            data={
                "cell": cell,
                "seed_index": seed_index,
                "round_index": round_index,
                "round_ledger_hash": stable_hash(round_ledger),
                "all_conservation_checks_passed": all(
                    round_ledger["conservation"].values()
                ),
            },
        )
        refresh_market_accounting(terminal=False)

    def on_run_completed(run: dict[str, Any]) -> None:
        run_key = (run["cell"], run["seed_index"])
        round_streams.pop(run_key).close()
        filename = "market_{}_seed_{:03d}.json".format(
            run["cell"], run["seed_index"]
        )
        managed_run = {**run, "model_lineage": dict(model_lineage)}
        _write_json_exclusive(manager.run_dir / filename, managed_run)
        run_record = {
            "cell": run["cell"],
            "seed_index": run["seed_index"],
            "run_seed": run["run_seed"],
            "path": filename,
            "ledger_hash": stable_hash(managed_run),
            "metrics": run["metrics"],
        }
        run_catalog.append(run_record)
        completed_runs.append(run)
        manager.events.emit(
            "V2MarketRunCompleted",
            data={
                "cell": run["cell"],
                "seed_index": run["seed_index"],
                "ledger_hash": run_record["ledger_hash"],
                "rounds_completed": len(run["rounds"]),
                "all_conservation_checks_passed": all(
                    all(round_row["conservation"].values())
                    for round_row in run["rounds"]
                ),
            },
        )
        refresh_market_accounting(terminal=False)

    refresh_market_accounting(terminal=False)
    try:
        payload = run_budget_behavior_2x2(
            student,
            n_agents=args.market_agents,
            rounds=args.market_rounds,
            seeds=args.market_seeds,
            master_seed=args.seed,
            ood_reference=ood_reference,
            on_run_started=on_run_started,
            on_run_completed=on_run_completed,
            on_round_started=on_round_started,
            on_round_completed=on_round_completed,
        )
    except BaseException:
        terminal_failure = True
        raise
    finally:
        for stream in list(round_streams.values()):
            stream.close()
        round_streams.clear()
        refresh_market_accounting(terminal=terminal_failure)

    market_index = {
        "schema_version": payload["schema_version"],
        "descriptor": payload["descriptor"],
        "paired_design": payload["paired_design"],
        "model_lineage": dict(model_lineage),
        "planned_runs": payload["planned_runs"],
        "honest_n_market_runs": payload["honest_n_market_runs"],
        "run_catalog": run_catalog,
        "cell_summaries": payload["cell_summaries"],
        "market_vs_train_ood": payload["market_vs_train_ood"],
    }
    market_index["market_index_hash"] = stable_hash(market_index)
    _write_json_exclusive(manager.run_dir / "market_2x2_summary.json", market_index)
    completed = payload["honest_n_market_runs"]
    refresh_market_accounting(terminal=False)
    manager.manifest["v2_attention_market"]["market"].update(
        {
            "market_index_hash": market_index["market_index_hash"],
            "market_vs_train_ood": payload["market_vs_train_ood"],
            "all_round_conservation_checks_passed": all(
                all(
                    all(round_row["conservation"].values())
                    for round_row in run["rounds"]
                )
                for run in payload["runs"]
            ),
        }
    )
    manager._write()
    return {
        **market_index,
        "runs": payload["runs"],
    }


def _dry_run_summary(
    manager: ManagedRunContext,
    *,
    args: argparse.Namespace,
    identities: Mapping[str, Any],
    planned_requests: int,
) -> dict[str, Any]:
    return {
        "output_schema_version": OUTPUT_SCHEMA_VERSION,
        "run_id": manager.run_id,
        "run_kind": RUN_KIND,
        "protocol_version": PROTOCOL_VERSION,
        "dry_run": True,
        "live": False,
        "provider": args.provider,
        "model_requested": args.model or None,
        "pilot_profile": pilot_profile_descriptor(args.pilot_profile),
        "planned_states": args.states,
        "planned_replicates_per_state": args.replicates,
        "planned_teacher_requests": planned_requests,
        "planned_market_runs": 4 * args.market_seeds,
        "provider_calls": 0,
        "network_access": False,
        "honest_n_teacher_samples": 0,
        "honest_n_aggregated_examples": 0,
        "honest_n_market_runs": 0,
        "v2_scientific_config_hash": identities["v2_scientific_config_hash"],
        "v2_model_request_config_hash": identities[
            "v2_model_request_config_hash"
        ],
        "v2_execution_config_hash": identities["v2_execution_config_hash"],
        "v2_full_effective_config_hash": identities[
            "v2_full_effective_config_hash"
        ],
        "state_design_hash": identities["state_design_hash"],
        "scientific_claim_status": "plan_only_no_teacher_or_market_result",
    }


def _full_summary(
    manager: ManagedRunContext,
    *,
    args: argparse.Namespace,
    identities: Mapping[str, Any],
    teacher: Mapping[str, Any],
    distillation: Mapping[str, Any],
    market: Mapping[str, Any],
) -> dict[str, Any]:
    split_counts = distillation["split_manifest"]["counts"]
    return {
        "output_schema_version": OUTPUT_SCHEMA_VERSION,
        "run_id": manager.run_id,
        "run_kind": RUN_KIND,
        "protocol_version": PROTOCOL_VERSION,
        "dry_run": False,
        "live": bool(args.live),
        "provider": args.provider,
        "model_requested": args.model or None,
        "model_resolved": teacher["model_resolved"],
        "reported_models": teacher["reported_models"],
        "model_identity": {
            "model_requested": args.model or None,
            "provider_sdk_reported_aliases": teacher["reported_models"],
            "requested_and_reported_are_distinct_fields": True,
            "underlying_serving_weights_independently_verified": False,
            "legacy_model_resolved_field_semantics": (
                "single safe provider-reported alias when unique; otherwise "
                "requested provider model; retained for schema compatibility "
                "and not an underlying-weights identity claim"
            ),
        },
        "pilot_profile": pilot_profile_descriptor(args.pilot_profile),
        "teacher_acceptance_gate": manager.manifest["v2_attention_market"].get(
            "teacher_acceptance_gate",
            {"enabled": False, "status": "not_applicable"},
        ),
        "network_access": bool(manager.network_access),
        "v2_scientific_config_hash": identities["v2_scientific_config_hash"],
        "v2_model_request_config_hash": identities[
            "v2_model_request_config_hash"
        ],
        "v2_execution_config_hash": identities["v2_execution_config_hash"],
        "v2_full_effective_config_hash": identities[
            "v2_full_effective_config_hash"
        ],
        "v2_scientific_component_fingerprint": identities[
            "component_fingerprint"
        ],
        "v2_execution_component_fingerprint": identities[
            "execution_component_fingerprint"
        ],
        "teacher": {
            **teacher["completion"],
            "reported_models": teacher["reported_models"],
            "input_tokens": teacher["input_tokens"],
            "output_tokens": teacher["output_tokens"],
            "cache_enabled": False,
            "real_provider_determinism_claimed": False,
        },
        "dataset": {
            "dataset_hash": distillation["dataset_hash"],
            "training_projection_hash": distillation[
                "training_projection_hash"
            ],
            "split_hash": distillation["split_manifest"]["split_hash"],
            "aggregated_examples": len(distillation["examples"]),
            "split_counts": {
                "train": split_counts["train_rows"],
                "validation": split_counts["validation_rows"],
                "test": split_counts["test_rows"],
            },
            "group_unit": "family_id",
            "replicate_leakage": False,
            "planned_design_partition_regime_coverage": distillation[
                "split_manifest"
            ]["planned_design_partition_regime_coverage"],
            "training_eligible_partition_regime_coverage": distillation[
                "split_manifest"
            ]["training_eligible_partition_regime_coverage"],
            "teacher_disagreement": distillation["teacher_disagreement"],
        },
        "student": {
            "deployed_model": "mlp_fixed_by_protocol",
            "prior_hash": stable_hash(distillation["prior"].to_dict()),
            "linear_model_hash": distillation["linear"].model_hash(),
            "mlp_model_hash": distillation["mlp"].model_hash(),
            "model_envelope_hash": distillation["model_envelope"][
                "model_envelope_hash"
            ],
            "model_envelope_artifact_sha256": distillation[
                "model_envelope_artifact_sha256"
            ],
            "model_artifacts": distillation["model_envelope"]["models"],
            "evaluations": distillation["evaluations"],
            "held_out_baseline_comparison": distillation[
                "baseline_comparison"
            ],
            "ood": distillation["ood"],
            "interpretation": (
                "Teacher agreement only; fixed MLP is reported even when it "
                "fails to beat a baseline and is not evidence of human validity"
            ),
        },
        "market": {
            "planned_runs": market["planned_runs"],
            "honest_n_market_runs": market["honest_n_market_runs"],
            "market_index_hash": market["market_index_hash"],
            "paired_design": market["paired_design"],
            "cell_summaries": market["cell_summaries"],
            "market_vs_train_ood": market["market_vs_train_ood"],
            "all_round_conservation_checks_passed": all(
                all(
                    all(round_row["conservation"].values())
                    for round_row in run["rounds"]
                )
                for run in market["runs"]
            ),
        },
        "privacy_boundary": {
            "private_teacher_artifact_mode": "0600",
            "private_rationale_in_public_samples": False,
            "private_rationale_in_student_features": False,
            "private_rationale_in_market": False,
        },
        "scientific_semantics_change": (
            "additive isolated V2 protocol only; V1 Persona, prompt, Config, "
            "clearing, CLI, result, and replay semantics are unchanged"
        ),
        "scientific_claim_status": (
            "engineering_fake_only"
            if args.provider != "openai"
            else (
                "exploratory_endpoint_pilot_not_human_ground_truth"
                if args.pilot_profile is not None
                else "endpoint_teacher_only_not_human_ground_truth"
            )
        ),
        "remaining_validation": [
            "human comparison labels",
            "real Teacher qualification",
            "preregistered acceptance thresholds",
            "reviewed live sample size and confirmatory execution",
        ],
    }


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

    repo_root = Path(__file__).resolve().parents[1]
    try:
        args = build_argparser().parse_args(args_list)
        args.provider = str(args.provider).strip().lower()
        planned_requests = _validate_args(args)
        from nmsim import v2_attention

        observations = v2_attention.generate_state_design(
            args.states, args.seed, study_id="v2-attention-market"
        )
        identities = build_v2_identities(
            args, observations, repo_root=repo_root
        )
    except (
        ManagedCLIError,
        OSError,
        V2ProtocolError,
        V2ProviderGuardError,
        ValueError,
    ) as error:
        fail_cli(bootstrap, error, failure_stage="config_validation")

    cfg = Config(
        provider="mock",
        model="",
        seed=args.seed,
        n_rounds=0,
        news_round=0,
        n_llm_agents=0,
        n_noise_agents=0,
        cache_enabled=False,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        out_dir=args.out,
    )
    input_paths = {
        "v2_protocol_document": repo_root / "docs/V2_ATTENTION_DISTILLATION.md",
        "v2_teacher_contract_source": repo_root / "nmsim/v2_attention.py",
        "v2_distillation_source": repo_root / "nmsim/v2_distillation.py",
        "v2_market_source": repo_root / "nmsim/v2_market.py",
        "v2_market_experiment_source": repo_root / "nmsim/v2_market_experiment.py",
        "v2_managed_entrypoint_source": Path(__file__).resolve(),
    }
    if args.pilot_profile is not None:
        input_paths["v2_teacher_pilot_protocol"] = (
            repo_root
            / (
                "docs/V2_TEACHER_PILOT_V6.md"
                if args.pilot_profile
                == MINIMAX_M27_HIGGSAI_TIMEOUT600_OUTPUT16384_JOINT54X3_PILOT
                else (
                    "docs/V2_TEACHER_PILOT_V5.md"
                    if args.pilot_profile
                    == MINIMAX_M27_HIGGSAI_LONG_TIMEOUT_JOINT54X3_PILOT
                    else (
                        "docs/V2_TEACHER_PILOT_V4.md"
                        if args.pilot_profile
                        == MINIMAX_M27_HIGGSAI_FINISH_AUDIT_EXTERNAL_JOINT54X3_PILOT
                        else (
                            "docs/V2_TEACHER_PILOT_V3.md"
                            if args.pilot_profile
                            == MINIMAX_M27_HIGGSAI_FINISH_AUDIT_JOINT54X3_PILOT
                            else (
                                "docs/V2_TEACHER_PILOT_V2.md"
                                if args.pilot_profile
                                == MINIMAX_M27_REQUEST_HIGGSAI_REPORTED_JOINT54X3_PILOT
                                else "docs/V2_TEACHER_PILOT.md"
                            )
                        )
                    )
                )
            )
        )
    manager = ManagedRunContext.create(
        cfg,
        out_root=args.out,
        scenario_id="{}:{}".format(
            PROTOCOL_VERSION, identities["v2_scientific_config_hash"][:16]
        ),
        run_id=args.run_id,
        worker_count=args.workers,
        batching={
            "teacher": {
                "strategy": (
                    "first_planned_canary_then_strict_sequential_fail_fast"
                    if args.pilot_profile is not None
                    else "bounded_concurrent_exact_replicates"
                ),
                "workers": args.workers if args.provider == "openai" else 1,
                "cache_enabled": False,
                "pilot_profile_id": args.pilot_profile,
            },
            "market": {
                "cells": 4,
                "seeds": args.market_seeds,
                "synchronous_daily_batch": True,
            },
        },
        input_paths=input_paths,
        repo_root=repo_root,
        command_identity=COMMAND_IDENTITY,
        run_kind=RUN_KIND,
        planned_simulation_runs=(0 if args.dry_run else 4 * args.market_seeds),
        research_profile=_research_profile(identities),
    )
    try:
        with manager:
            _initialise_manifest(
                manager,
                args=args,
                identities=identities,
                planned_requests=planned_requests,
            )
            manager.register_llm_runtime(
                provider=args.provider,
                model=(args.model or None),
                mode=("v2_attention_dry_run" if args.dry_run else RUN_KIND),
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
                    identities=identities,
                    planned_requests=planned_requests,
                )
                _write_json_exclusive(
                    manager.run_dir / "dry_run_summary.json", summary
                )
                manager.finish()
                print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
                return

            manager.set_stage("provider_setup")
            _write_json_exclusive(
                manager.run_dir / "state_design.json",
                {
                    "schema_version": "v2_state_design/0.1",
                    "state_design_hash": identities["state_design_hash"],
                    "states": [observation.to_dict() for observation in observations],
                },
            )
            teacher = run_teacher_phase(
                manager, args=args, observations=observations
            )
            manager.set_stage("simulation")
            distillation = run_distillation_phase(
                manager,
                args=args,
                observations=observations,
                replicate_results=teacher["replicate_results"],
            )
            market = run_market_phase(
                manager,
                args=args,
                student=distillation["mlp"],
                model_lineage={
                    "schema_version": "v2_market_model_lineage/0.1",
                    "feature_order": list(v2_attention.FEATURE_ORDER),
                    "state_contract_hash": v2_attention.CONTRACT_HASH,
                    "dataset_hash": distillation["dataset_hash"],
                    "training_projection_hash": distillation[
                        "training_projection_hash"
                    ],
                    "deployed_model_hash": distillation["mlp"].model_hash(),
                    "deployed_model_artifact_sha256": distillation[
                        "model_envelope"
                    ]["models"]["mlp"]["artifact_sha256"],
                    "student_model_envelope_hash": distillation[
                        "model_envelope"
                    ]["model_envelope_hash"],
                    "student_model_envelope_artifact_sha256": distillation[
                        "model_envelope_artifact_sha256"
                    ],
                    "ood_reference_hash": distillation[
                        "ood_reference_hash"
                    ],
                },
                ood_reference=distillation["ood_reference"],
            )
            manager.set_stage("result_export")
            summary = _full_summary(
                manager,
                args=args,
                identities=identities,
                teacher=teacher,
                distillation=distillation,
                market=market,
            )
            manager.manifest["v2_attention_market"]["network_access"] = summary[
                "network_access"
            ]
            manager.manifest["v2_attention_market"]["scientific_claim_status"] = (
                summary["scientific_claim_status"]
            )
            manager._write()
            _write_json_exclusive(
                manager.run_dir / "v2_attention_market_summary.json", summary
            )
            _write_text_exclusive(
                manager.run_dir / "v2_attention_market_report.md",
                render_markdown_report(summary),
            )
            _write_text_exclusive(
                manager.run_dir / "v2_attention_market_report.html",
                render_html_report(summary),
            )
            manager.finish()
            print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    except V2ProviderGuardError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2)
    except Exception as error:
        if args.provider == "openai":
            print(
                "V2 Teacher/Student/market run failed: {}".format(
                    type(error).__name__
                ),
                file=sys.stderr,
            )
            raise SystemExit(1) from None
        raise


if __name__ == "__main__":
    main()


__all__ = [
    "ALLOWED_PROVIDERS",
    "COMMAND_IDENTITY",
    "DEFAULT_OUTPUT_ROOT",
    "FakeTeacherProvider",
    "OpenAITeacherProvider",
    "OUTPUT_SCHEMA_VERSION",
    "MINIMAX_M27_JOINT54X3_PILOT",
    "MINIMAX_M27_REQUEST_HIGGSAI_REPORTED_JOINT54X3_PILOT",
    "MINIMAX_M27_HIGGSAI_FINISH_AUDIT_JOINT54X3_PILOT",
    "MINIMAX_M27_HIGGSAI_FINISH_AUDIT_EXTERNAL_JOINT54X3_PILOT",
    "MINIMAX_M27_HIGGSAI_LONG_TIMEOUT_JOINT54X3_PILOT",
    "MINIMAX_M27_HIGGSAI_TIMEOUT600_OUTPUT16384_JOINT54X3_PILOT",
    "PILOT_PROFILES",
    "PROTOCOL_VERSION",
    "TeacherCompletion",
    "V2ProtocolError",
    "V2ProviderGuardError",
    "V2TeacherGateError",
    "build_argparser",
    "build_training_examples",
    "build_v2_identities",
    "main",
    "pilot_profile_descriptor",
    "render_html_report",
    "render_markdown_report",
    "run_distillation_phase",
    "run_market_phase",
    "run_teacher_phase",
    "stable_hash",
]
