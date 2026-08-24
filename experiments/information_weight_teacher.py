"""Managed Teacher-only pilot for weighted information views.

The frozen design presents four differently weighted observation views of each
of 50 common latent market/account states.  The prompts contain no trader-type
label: a view is defined only by the allocation of visible numeric fields among
price/volume, fundamental, and news information.  This entrypoint acquires 200
Teacher decisions; it deliberately trains no Student and runs no market.

Real OpenAI-compatible access is fail-closed behind ``--live`` and an exact
request-count confirmation.  Dry-run never constructs a Provider.  Exact
prompts and visible observations are public research inputs, while raw model
responses, private rationale, SDK payloads, response identifiers, and error
details remain in a mode-0600 artifact.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
from dataclasses import asdict, is_dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import sys
from typing import Any, Mapping, Optional, Sequence, TextIO

from experiments.v2_attention_market import OpenAITeacherProvider, TeacherCompletion
from nmsim.config import Config
from nmsim.managed_cli import (
    BootstrapCLIError,
    ManagedCLIError,
    RaisingArgumentParser,
    bootstrap_cli,
    fail_cli,
)
from nmsim.provider_attempts import sha256_text
from nmsim.run_context import ManagedRunContext


PROTOCOL_VERSION = "information_weight_teacher/0.1"
OUTPUT_SCHEMA_VERSION = "information_weight_teacher_output/0.1"
SAMPLE_SCHEMA_VERSION = "information_weight_teacher_sample/0.1"
PRIVATE_RECORD_SCHEMA_VERSION = "information_weight_teacher_private_record/0.1"
IDENTITY_SCHEMA_VERSION = "information_weight_teacher_identity/0.1"
COMMAND_IDENTITY = "python -m experiments.information_weight_teacher"
RUN_KIND = "information_weight_teacher"
DEFAULT_OUTPUT_ROOT = "results_information_weight_pilot"

FROZEN_STUDY_ID = "information-weight-pilot-v1"
FROZEN_SEED = 20260824
FROZEN_LATENT_STATE_COUNT = 50
FROZEN_VIEW_COUNT = 4
FROZEN_REQUEST_COUNT = 200
FROZEN_WORKERS = 1
FROZEN_MODEL = "MiniMax-M2.7"
REQUIRED_REPORTED_MODEL = "HiggsAI"
REQUIRED_FINISH_REASON = "stop"
FROZEN_TEMPERATURE = 1.0
FROZEN_MAX_TOKENS = 190000
FROZEN_TOP_P = 0.95
FROZEN_TOP_K = 40
FROZEN_RESPONSE_FORMAT = {"type": "json_object"}
FROZEN_PHASE_TIMEOUT_SECONDS = 7200.0
FROZEN_HARD_DEADLINE_SECONDS = 7200.0
FROZEN_CONNECT_TIMEOUT_SECONDS = 10.0
FROZEN_APPLICATION_MAX_ATTEMPTS = 5
FROZEN_RETRY_DELAYS_SECONDS = (10.0, 30.0, 60.0, 120.0)
FROZEN_SDK_RETRY_COUNT = 0

DRY_RUN_ID = "information-weight-pilot-v1-dry-20260824-a2"
FAKE_RUN_ID = "information-weight-pilot-v1-fake-20260824-a1"
LIVE_RUN_ID = "information-weight-pilot-live-20260824-a1"
ALLOWED_PROVIDERS = frozenset({"fake_test_teacher", "openai"})
IDENTITY_HASH_KEYS = (
    "state_design_hash",
    "sample_plan_hash",
    "scientific_config_hash",
    "model_request_config_hash",
    "execution_config_hash",
    "full_effective_config_hash",
)


class InformationWeightProtocolError(ValueError):
    """The frozen 50-by-four study contract was violated."""


class InformationWeightProviderGuardError(ValueError):
    """A Provider execution was requested without the frozen live guard."""


def _jsonable(value: Any) -> Any:
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _jsonable(to_dict())
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite value is not valid canonical JSON")
        return value
    raise TypeError("unsupported JSON value: {}".format(type(value).__name__))


def _stable_hash(value: Any) -> str:
    payload = json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _object_dict(value: Any) -> dict[str, Any]:
    converted = _jsonable(value)
    if not isinstance(converted, dict):
        raise InformationWeightProtocolError(
            "versioned information-weight object must project to an object"
        )
    return converted


def _sample_id(sample: Any) -> str:
    for name in ("sample_hash", "sample_id"):
        value = getattr(sample, name, None)
        if isinstance(value, str) and value:
            return value
    return _stable_hash(_object_dict(sample))


def _latent_id(value: Any) -> str:
    for name in ("latent_state_id", "state_id", "latent_id"):
        item = getattr(value, name, None)
        if isinstance(item, str) and item:
            return item
    projected = _object_dict(value)
    for name in ("latent_state_id", "state_id", "latent_id"):
        item = projected.get(name)
        if isinstance(item, str) and item:
            return item
    raise InformationWeightProtocolError("latent state has no stable identifier")


def _profile_id(view: Any) -> str:
    value = getattr(view, "profile_id", None)
    if isinstance(value, str) and value:
        return value
    projected = _object_dict(view)
    value = projected.get("profile_id")
    if isinstance(value, str) and value:
        return value
    raise InformationWeightProtocolError("information view has no profile_id")


def _open_exclusive(path: Path, mode: int) -> TextIO:
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


def _write_json_exclusive(path: Path, payload: Any, mode: int = 0o644) -> None:
    with _open_exclusive(path, mode) as stream:
        json.dump(
            _jsonable(payload),
            stream,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _append_jsonl(stream: TextIO, row: Mapping[str, Any]) -> None:
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
    os.fsync(stream.fileno())


def _safe_private(manager: ManagedRunContext, value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        return manager._manager._sanitize_text(value, max_length=None)
    if isinstance(value, Mapping):
        return {str(key): _safe_private(manager, item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_safe_private(manager, item) for item in value]
    return value


def _safe_finish_reason(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value in {
        "stop", "length", "content_filter", "tool_calls", "function_call"
    } else None


def _safe_public_model_alias(value: Any) -> Optional[str]:
    # Keep the same credential-aware public alias boundary as the reused
    # OpenAI Teacher transport.
    from experiments.v2_attention_market import _safe_public_model_alias

    return _safe_public_model_alias(value)


def _public_decision(parsed: Any) -> dict[str, Any]:
    public_record = getattr(parsed, "public_record", None)
    if callable(public_record):
        result = public_record()
    else:
        result = _object_dict(parsed)
        for private_key in ("reasoning", "private_rationale", "rationale"):
            result.pop(private_key, None)
    if not isinstance(result, Mapping):
        raise InformationWeightProtocolError("parsed decision has no public projection")
    return dict(result)


def _private_rationale(parsed: Any) -> Optional[str]:
    for name in ("private_rationale", "reasoning", "rationale"):
        value = getattr(parsed, name, None)
        if isinstance(value, str):
            return value
    return None


class FakeInformationWeightTeacher:
    """Offline contract control; it performs no I/O and is not scientific evidence."""

    kind = "fake_test_teacher"
    model = "information-weight-fake-teacher-v1"

    def __init__(self) -> None:
        self.request_count = 0
        self.response_count = 0
        self.application_attempt_count = 0
        self.provider_exception_attempts = 0
        self.retries_scheduled = 0
        self.logical_requests_with_retry = 0
        self.network_access = False
        self.batch_sizes: list[int] = []
        self.application_attempt_audits: list[dict[str, Any]] = []

    def complete_plan(
        self,
        plan: Sequence[Any],
        *,
        before_attempt: Any,
        on_application_attempt: Any,
        on_completion: Any,
    ) -> list[TeacherCompletion]:
        from nmsim import information_weight

        self.batch_sizes.append(len(plan))
        completions: list[TeacherCompletion] = []
        for index, sample in enumerate(plan):
            before_attempt(index)
            self.request_count += 1
            self.application_attempt_count += 1
            audit = {
                    "logical_request_index": index,
                    "application_attempt_index": 1,
                    "application_max_attempts": 1,
                    "status": "response_received",
                    "provider_error_type": None,
                    "provider_error_detail": None,
                    "retryable": False,
                    "retry_scheduled": False,
                    "retry_delay_seconds": None,
                }
            self.application_attempt_audits.append(dict(audit))
            on_application_attempt(index, audit)
            raw = information_weight.fake_teacher_response(sample)
            self.response_count += 1
            completion = TeacherCompletion(
                raw_response=raw,
                reported_model=self.model,
                input_tokens=None,
                output_tokens=None,
                response_id="fake-{}".format(sha256_text(raw)[:16]),
                reported_model_raw=self.model,
                finish_reason="stop",
                finish_reason_raw="stop",
                application_attempt_count=1,
                technical_retry_count=0,
            )
            completions.append(completion)
            on_completion(index, completion)
        return completions

    async def aclose(self) -> None:
        return None


def build_argparser() -> argparse.ArgumentParser:
    parser = RaisingArgumentParser(allow_abbrev=False)
    parser.add_argument("--version", action="version", version=PROTOCOL_VERSION)
    parser.add_argument(
        "--provider", choices=sorted(ALLOWED_PROVIDERS), default="fake_test_teacher"
    )
    parser.add_argument("--model", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--confirm-request-count", type=int, default=None)
    parser.add_argument("--out", default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--run-id", default=None)
    return parser


def _validate_args(args: argparse.Namespace) -> None:
    if args.dry_run and args.live:
        raise InformationWeightProviderGuardError(
            "--dry-run and --live are mutually exclusive"
        )
    if args.provider == "openai":
        if not args.dry_run and not args.live:
            raise InformationWeightProviderGuardError(
                "--provider openai requires --live"
            )
        if args.model != FROZEN_MODEL:
            raise InformationWeightProviderGuardError(
                "--provider openai requires frozen --model {}".format(FROZEN_MODEL)
            )
        if not args.dry_run and args.confirm_request_count != FROZEN_REQUEST_COUNT:
            raise InformationWeightProviderGuardError(
                "live acquisition requires --confirm-request-count {}".format(
                    FROZEN_REQUEST_COUNT
                )
            )
    else:
        if args.live:
            raise InformationWeightProviderGuardError(
                "--live is valid only with --provider openai"
            )
        if str(args.model).strip():
            raise InformationWeightProviderGuardError(
                "--model is not configurable for the offline Fake Teacher"
            )
        if args.confirm_request_count is not None:
            raise InformationWeightProviderGuardError(
                "--confirm-request-count is valid only for OpenAI live acquisition"
            )

    required_run_id = (
        DRY_RUN_ID
        if args.dry_run
        else LIVE_RUN_ID
        if args.provider == "openai"
        else FAKE_RUN_ID
    )
    if args.run_id is None:
        args.run_id = required_run_id
    elif args.run_id != required_run_id:
        raise InformationWeightProtocolError(
            "this frozen execution mode requires --run-id {}".format(required_run_id)
        )


def _build_provider(args: argparse.Namespace) -> Any:
    if args.dry_run:
        raise InformationWeightProviderGuardError(
            "dry-run must not construct a Provider"
        )
    if args.provider == "fake_test_teacher":
        return FakeInformationWeightTeacher()
    return OpenAITeacherProvider(
        model=FROZEN_MODEL,
        temperature=FROZEN_TEMPERATURE,
        max_tokens=FROZEN_MAX_TOKENS,
        workers=FROZEN_WORKERS,
        top_p=FROZEN_TOP_P,
        top_k=FROZEN_TOP_K,
        response_format=FROZEN_RESPONSE_FORMAT,
        application_max_attempts=FROZEN_APPLICATION_MAX_ATTEMPTS,
        retry_delays_seconds=FROZEN_RETRY_DELAYS_SECONDS,
        request_timeout_seconds=FROZEN_PHASE_TIMEOUT_SECONDS,
        hard_request_deadline_seconds=FROZEN_HARD_DEADLINE_SECONDS,
    )


def _endpoint_identity() -> dict[str, Any]:
    # Reuse the already reviewed credential-free route projection without
    # publishing or hashing raw endpoint values in this additive study.
    from experiments.v2_attention_market import _v2_endpoint_identity

    return _v2_endpoint_identity(os.environ.get("OPENAI_BASE_URL"))


def build_information_weight_identities(
    args: argparse.Namespace,
    latent_states: Sequence[Any],
    plan: Sequence[Any],
) -> dict[str, Any]:
    from nmsim import information_weight

    latent_projection = [_object_dict(state) for state in latent_states]
    state_design_hash = _stable_hash(latent_projection)
    sample_plan_hash = information_weight.plan_hash(plan)
    scientific_payload = {
        "schema_version": "information_weight_scientific_config/0.1",
        "study_id": FROZEN_STUDY_ID,
        "seed": FROZEN_SEED,
        "latent_state_count": FROZEN_LATENT_STATE_COUNT,
        "profile_ids": list(information_weight.PROFILE_IDS),
        "information_view_contract": information_weight.contract_descriptor(),
        "view_count": FROZEN_VIEW_COUNT,
        "planned_samples": FROZEN_REQUEST_COUNT,
        "contract_hash": information_weight.CONTRACT_HASH,
        "state_design_hash": state_design_hash,
        "sample_plan_hash": sample_plan_hash,
        "teacher_only": True,
        "student_enabled": False,
        "market_enabled": False,
    }
    request_payload = {
        "schema_version": "information_weight_model_request_config/0.1",
        "provider": args.provider,
        "model_requested": (
            FROZEN_MODEL
            if args.provider == "openai"
            else FakeInformationWeightTeacher.model
        ),
        "temperature": FROZEN_TEMPERATURE,
        "max_tokens": FROZEN_MAX_TOKENS,
        "top_p": FROZEN_TOP_P,
        "top_k": FROZEN_TOP_K,
        "response_format": dict(FROZEN_RESPONSE_FORMAT),
        "system_prompt_sha256": sha256_text(information_weight.SYSTEM_PROMPT),
    }
    execution_payload = {
        "schema_version": "information_weight_execution_config/0.1",
        "provider": args.provider,
        "live": bool(args.live),
        "dry_run": bool(args.dry_run),
        "run_id": args.run_id,
        "workers": FROZEN_WORKERS,
        "strict_sequential": True,
        "phase_inactivity_timeout_seconds": FROZEN_PHASE_TIMEOUT_SECONDS,
        "hard_request_deadline_seconds": FROZEN_HARD_DEADLINE_SECONDS,
        "connect_timeout_seconds": FROZEN_CONNECT_TIMEOUT_SECONDS,
        "sdk_retry_count": FROZEN_SDK_RETRY_COUNT,
        "application_max_attempts": FROZEN_APPLICATION_MAX_ATTEMPTS,
        "application_retry_delays_seconds": list(FROZEN_RETRY_DELAYS_SECONDS),
        "required_reported_model": REQUIRED_REPORTED_MODEL,
        "required_finish_reason": REQUIRED_FINISH_REASON,
        "endpoint_identity": (
            _endpoint_identity() if args.provider == "openai" else None
        ),
        "output_root_identity_sha256": _stable_hash(
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
        "command_identity": COMMAND_IDENTITY,
    }
    scientific_hash = _stable_hash(scientific_payload)
    request_hash = _stable_hash(request_payload)
    execution_hash = _stable_hash(execution_payload)
    full_payload = {
        "schema_version": IDENTITY_SCHEMA_VERSION,
        "scientific_config_hash": scientific_hash,
        "model_request_config_hash": request_hash,
        "execution_config_hash": execution_hash,
    }
    full_hash = _stable_hash(full_payload)
    return {
        "scientific_config": scientific_payload,
        "model_request_config": request_payload,
        "execution_config": execution_payload,
        "full_effective_config": full_payload,
        "state_design_hash": state_design_hash,
        "sample_plan_hash": sample_plan_hash,
        "scientific_config_hash": scientific_hash,
        "model_request_config_hash": request_hash,
        "execution_config_hash": execution_hash,
        "full_effective_config_hash": full_hash,
    }


def _identity_hashes(identities: Mapping[str, Any]) -> dict[str, str]:
    return {key: str(identities[key]) for key in IDENTITY_HASH_KEYS}


def _plan_public_row(sample: Any, index: int) -> dict[str, Any]:
    return {
        "request_index": index,
        "sample_id": _sample_id(sample),
        "latent_state_id": _latent_id(sample.view),
        "profile_id": _profile_id(sample.view),
        "visible_observation": _object_dict(sample.view),
        "prompt": {
            "system": sample.prompt.system,
            "user": sample.prompt.user,
            "prompt_hash": sample.prompt.prompt_hash,
        },
    }


def _initialise_manifest(
    manager: ManagedRunContext,
    *,
    args: argparse.Namespace,
    identities: Mapping[str, Any],
) -> None:
    manager.manifest[RUN_KIND] = {
        "protocol_version": PROTOCOL_VERSION,
        "output_schema_version": OUTPUT_SCHEMA_VERSION,
        "diagnostic_only": True,
        "teacher_only": True,
        "student_enabled": False,
        "market_enabled": False,
        "provider": args.provider,
        "model_requested": FROZEN_MODEL if args.provider == "openai" else None,
        "live": bool(args.live),
        "dry_run": bool(args.dry_run),
        "network_access": False,
        "planned": {
            "latent_states": FROZEN_LATENT_STATE_COUNT,
            "views_per_latent_state": FROZEN_VIEW_COUNT,
            "logical_requests": FROZEN_REQUEST_COUNT,
            "strict_sequential": True,
        },
        "identities": _identity_hashes(identities),
        "honest_n": {
            "logical_requests": 0,
            "physical_provider_attempts": 0,
            "raw_responses": 0,
            "parsed_decisions": 0,
            "complete_paired_latent_states": 0,
        },
        "privacy": {
            "public_visible_observations_and_exact_prompts": True,
            "raw_response_and_private_rationale_artifact_mode": "0600",
            "private_rationale_in_public_artifacts": False,
        },
    }
    manager.manifest["information_weight_config_identities"] = {
        "schema_version": IDENTITY_SCHEMA_VERSION,
        "scientific_config": identities["scientific_config"],
        "model_request_config": identities["model_request_config"],
        "execution_config": identities["execution_config"],
        "full_effective_config": identities["full_effective_config"],
        **_identity_hashes(identities),
    }
    manager.manifest.update(_identity_hashes(identities))
    manager.manifest["completion"]["llm_logical_requests"].update(
        {
            "planned": FROZEN_REQUEST_COUNT,
            "attempted": 0,
            "completed": 0,
            "failed": 0,
        }
    )
    manager.manifest["completion"]["agent_decisions"].update(
        {
            "planned": FROZEN_REQUEST_COUNT,
            "attempted": 0,
            "completed": 0,
            "failed": 0,
            "skipped": FROZEN_REQUEST_COUNT,
        }
    )
    manager._write()


def _dry_summary(
    manager: ManagedRunContext,
    *,
    args: argparse.Namespace,
    identities: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "output_schema_version": OUTPUT_SCHEMA_VERSION,
        "run_id": manager.run_id,
        "run_kind": RUN_KIND,
        "diagnostic_only": True,
        "teacher_only": True,
        "student_enabled": False,
        "market_enabled": False,
        "dry_run": True,
        "live": False,
        "provider": args.provider,
        "planned_latent_states": FROZEN_LATENT_STATE_COUNT,
        "planned_views_per_latent_state": FROZEN_VIEW_COUNT,
        "planned_logical_requests": FROZEN_REQUEST_COUNT,
        "provider_constructed": False,
        "provider_calls": 0,
        "network_access": False,
        "honest_n": {
            "logical_requests": 0,
            "physical_provider_attempts": 0,
            "raw_responses": 0,
            "parsed_decisions": 0,
            "complete_paired_latent_states": 0,
        },
        **_identity_hashes(identities),
    }


def _honest_n_snapshot(
    manager: ManagedRunContext,
    provider: Any,
    public_rows: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    manifest_logical = int(
        manager.manifest["completion"]["llm_logical_requests"].get(
            "attempted", 0
        )
    )
    logical = max(
        manifest_logical,
        int(getattr(provider, "request_count", 0)),
        len(public_rows),
    )
    physical = int(getattr(provider, "application_attempt_count", 0))
    raw = sum(row.get("response_hash") is not None for row in public_rows)
    parsed = sum(row.get("status") == "valid" for row in public_rows)
    complete_profiles: dict[str, set[str]] = defaultdict(set)
    for row in public_rows:
        if row.get("status") == "valid":
            complete_profiles[str(row["latent_state_id"])].add(
                str(row["profile_id"])
            )
    paired = sum(len(profiles) == FROZEN_VIEW_COUNT for profiles in complete_profiles.values())
    return {
        "logical_requests": logical,
        "physical_provider_attempts": physical,
        "raw_responses": raw,
        "parsed_decisions": parsed,
        "complete_paired_latent_states": paired,
    }


def _provider_attempt_snapshot(
    provider: Any,
    public_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    physical = int(getattr(provider, "application_attempt_count", 0))
    audits_value = getattr(provider, "application_attempt_audits", ())
    audits = [
        item for item in audits_value if isinstance(item, Mapping)
    ] if isinstance(audits_value, (list, tuple)) else []
    if audits:
        responses_received = sum(
            item.get("status") == "response_received" for item in audits
        )
        provider_exceptions = sum(
            item.get("status") == "provider_exception" for item in audits
        )
        retries_scheduled = sum(
            bool(item.get("retry_scheduled")) for item in audits
        )
        retried_logical_indices = {
            int(item["logical_request_index"])
            for item in audits
            if isinstance(item.get("logical_request_index"), int)
            and (
                int(item.get("application_attempt_index") or 0) > 1
                or bool(item.get("retry_scheduled"))
            )
        }
        logical_requests_with_retry = len(retried_logical_indices)
        exhausted_logical_indices = {
            int(item["logical_request_index"])
            for item in audits
            if isinstance(item.get("logical_request_index"), int)
            and item.get("status") == "provider_exception"
            and not bool(item.get("retry_scheduled"))
        }
    else:
        # These are direct observed counters, not ``attempted - failures``.
        # A cancelled in-flight attempt is therefore never inferred successful.
        responses_received = int(getattr(provider, "response_count", 0))
        provider_exceptions = int(
            getattr(provider, "provider_exception_attempts", 0)
        )
        retries_scheduled = int(getattr(provider, "retries_scheduled", 0))
        logical_requests_with_retry = int(
            getattr(provider, "logical_requests_with_retry", 0)
        )
        exhausted_logical_indices = {
            int(row["request_index"])
            for row in public_rows
            if row.get("failure_code") == "provider_exception"
        }
    recognized = responses_received + provider_exceptions
    unresolved_physical = max(0, physical - recognized)
    reported_models = sorted(
        {
            str(row["reported_model"])
            for row in public_rows
            if isinstance(row.get("reported_model"), str)
            and row.get("reported_model")
        }
    )
    finish_reason_counts = Counter(
        str(row["finish_reason"])
        for row in public_rows
        if isinstance(row.get("finish_reason"), str)
    )
    resolved_logical = len(public_rows)
    logical_attempted = max(
        int(getattr(provider, "request_count", 0)), resolved_logical
    )
    return {
        "attempted": physical,
        "succeeded": responses_received,
        "failed": provider_exceptions,
        "unresolved_physical_attempts": unresolved_physical,
        "responses_received": responses_received,
        "parse_failed_responses": sum(
            row.get("failure_code") == "teacher_response_invalid"
            for row in public_rows
        ),
        "provider_exceptions": provider_exceptions,
        "retries_scheduled": retries_scheduled,
        "logical_requests_with_retry": logical_requests_with_retry,
        "exhausted_logical_requests": len(exhausted_logical_indices),
        "unresolved_logical_requests": max(
            0, logical_attempted - resolved_logical
        ),
        "reported_models": reported_models[:16],
        "reported_models_truncated": len(reported_models) > 16,
        "invalid_reported_model_alias_count": sum(
            bool(row.get("reported_model_alias_invalid"))
            for row in public_rows
        ),
        "missing_reported_model_count": sum(
            bool(row.get("reported_model_missing")) for row in public_rows
        ),
        "reported_model_mismatch_count": sum(
            row.get("failure_code") == "reported_model_mismatch"
            for row in public_rows
        ),
        "finish_reason_counts": dict(sorted(finish_reason_counts.items())),
        "invalid_finish_reason_count": sum(
            bool(row.get("finish_reason_invalid"))
            for row in public_rows
        ),
        "missing_finish_reason_count": sum(
            bool(row.get("finish_reason_missing"))
            for row in public_rows
        ),
    }


def _apply_accounting_snapshot(
    manager: ManagedRunContext,
    provider: Any,
    public_rows: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    honest = _honest_n_snapshot(manager, provider, public_rows)
    attempts = _provider_attempt_snapshot(provider, public_rows)
    logical = honest["logical_requests"]
    physical = honest["physical_provider_attempts"]
    raw = honest["raw_responses"]
    parsed = honest["parsed_decisions"]
    attempts["unresolved_logical_requests"] = max(
        0, logical - len(public_rows)
    )
    parser_attempted = sum(bool(row.get("parser_attempted")) for row in public_rows)
    manager.network_access = bool(getattr(provider, "network_access", False))
    completion = manager.manifest["completion"]
    completion["llm_logical_requests"].update(
        {
            "planned": FROZEN_REQUEST_COUNT,
            "attempted": logical,
            "completed": len(public_rows),
            "failed": max(0, logical - len(public_rows)),
        }
    )
    completion["provider_calls"].update(
        {
            "unit": "physical_provider_attempts_after_cache_and_replay",
            "coverage": "application adapter attempts; SDK retries disabled",
            "attempted": physical,
            "succeeded": attempts["succeeded"],
            "failed": attempts["failed"],
            "unresolved": attempts["unresolved_physical_attempts"],
        }
    )
    completion["agent_decisions"].update(
        {
            "planned": FROZEN_REQUEST_COUNT,
            "attempted": logical,
            "completed": parsed,
            "failed": max(0, logical - parsed),
            "skipped": max(0, FROZEN_REQUEST_COUNT - logical),
        }
    )
    completion["parsing"].update(
        {
            "attempted": parser_attempted,
            "succeeded": parsed,
            "failed": max(0, parser_attempted - parsed),
            "fallbacks": 0,
        }
    )
    completion["application_provider_attempts"].update(attempts)
    manager.manifest[RUN_KIND]["network_access"] = manager.network_access
    manager.manifest[RUN_KIND]["honest_n"] = dict(honest)
    manager.manifest["honest_n_information_weight_teacher"] = dict(honest)
    manager.register_llm_runtime(
        provider_calls=physical,
        provider_calls_succeeded=attempts["succeeded"],
        provider_calls_failed=attempts["failed"],
        provider_calls_unresolved=attempts["unresolved_physical_attempts"],
        logical_requests=logical,
        network_access=manager.network_access,
        application_attempt_count=physical,
        retries_scheduled=attempts["retries_scheduled"],
        provider_exception_attempts=attempts["provider_exceptions"],
        unresolved_physical_attempts=attempts["unresolved_physical_attempts"],
        unresolved_logical_requests=attempts["unresolved_logical_requests"],
        sdk_retry_count=FROZEN_SDK_RETRY_COUNT,
    )
    manager._refresh_derived()
    manager._write()
    return honest


def _sync_accounting(
    manager: ManagedRunContext,
    provider: Any,
    public_rows: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    manager.sync_llm_accounting(provider)
    return dict(manager.manifest[RUN_KIND]["honest_n"])


def _install_physical_attempt_accounting(
    manager: ManagedRunContext,
    provider: Any,
    public_rows: Sequence[Mapping[str, Any]],
) -> None:
    """Refresh partial live evidence during normal or exceptional finalization."""

    original_sync = manager.sync_llm_accounting

    def sync_with_physical_attempts(llm: Any = None, tracker: Any = None) -> None:
        original_sync(llm, tracker)
        active = llm or provider
        _apply_accounting_snapshot(manager, active, public_rows)

    manager.sync_llm_accounting = sync_with_physical_attempts


def run_teacher_acquisition(
    manager: ManagedRunContext,
    *,
    args: argparse.Namespace,
    plan: Sequence[Any],
    identities: Mapping[str, Any],
) -> dict[str, Any]:
    from nmsim import information_weight

    provider = _build_provider(args)
    manager.active_llm = provider
    manager.llm_mode = "record"
    manager.network_access = False
    public_rows: list[dict[str, Any]] = []
    _install_physical_attempt_accounting(manager, provider, public_rows)
    manager.register_llm_runtime(
        provider=args.provider,
        model=(FROZEN_MODEL if args.provider == "openai" else provider.model),
        mode=RUN_KIND,
        cache_enabled=False,
        network_access=False,
        provider_calls=0,
        live=bool(args.live),
        application_concurrency_limit=FROZEN_WORKERS,
        provider_connection_limit=(FROZEN_WORKERS if args.provider == "openai" else 0),
        strict_sequential=True,
        generation_sampling={
            "temperature": FROZEN_TEMPERATURE,
            "max_tokens": FROZEN_MAX_TOKENS,
            "top_p": FROZEN_TOP_P,
            "top_k": FROZEN_TOP_K,
        },
        response_format=dict(FROZEN_RESPONSE_FORMAT),
        httpx_phase_inactivity_timeout_seconds=FROZEN_PHASE_TIMEOUT_SECONDS,
        hard_request_deadline_seconds=FROZEN_HARD_DEADLINE_SECONDS,
        connect_timeout_seconds=FROZEN_CONNECT_TIMEOUT_SECONDS,
        provider_retry_count=FROZEN_SDK_RETRY_COUNT,
        application_max_attempts=(
            FROZEN_APPLICATION_MAX_ATTEMPTS if args.provider == "openai" else 1
        ),
        application_retry_delays_seconds=(
            list(FROZEN_RETRY_DELAYS_SECONDS) if args.provider == "openai" else []
        ),
    )

    processed: set[int] = set()
    input_tokens = 0
    output_tokens = 0
    public_stream = _open_exclusive(
        manager.run_dir / "information_weight_samples.jsonl", 0o644
    )
    try:
        private_stream = _open_exclusive(
            manager.run_dir / "private_information_weight_records.jsonl", 0o600
        )
    except BaseException:
        public_stream.close()
        raise

    def before_attempt(index: int) -> None:
        sample = plan[index]
        manager.events.emit(
            "LLMRequestRecorded",
            agent_id=_latent_id(sample.view),
            data={
                "run_kind": RUN_KIND,
                "request_index": index,
                "sample_id": _sample_id(sample),
                "latent_state_id": _latent_id(sample.view),
                "profile_id": _profile_id(sample.view),
                "prompt_hash": sample.prompt.prompt_hash,
            },
            private_data={
                "system_prompt": sample.prompt.system,
                "user_prompt": sample.prompt.user,
            },
        )

    def on_application_attempt(index: int, audit: Mapping[str, Any]) -> None:
        sample = plan[index]
        private_error = _safe_private(manager, audit.get("provider_error_detail"))
        manager.events.emit(
            "InformationWeightProviderApplicationAttemptRecorded",
            agent_id=_latent_id(sample.view),
            data={
                "run_kind": RUN_KIND,
                "sample_id": _sample_id(sample),
                "logical_request_index": index,
                "application_attempt_index": audit.get("application_attempt_index"),
                "application_max_attempts": audit.get("application_max_attempts"),
                "status": audit.get("status"),
                "retryable": bool(audit.get("retryable")),
                "retry_scheduled": bool(audit.get("retry_scheduled")),
                "retry_delay_seconds": audit.get("retry_delay_seconds"),
            },
            private_data={
                "provider_error_type": audit.get("provider_error_type"),
                "provider_error_detail": private_error,
            },
        )
        _append_jsonl(
            private_stream,
            {
                "schema_version": PRIVATE_RECORD_SCHEMA_VERSION,
                "record_type": "application_attempt",
                "sample_id": _sample_id(sample),
                "latent_state_id": _latent_id(sample.view),
                "profile_id": _profile_id(sample.view),
                "logical_request_index": index,
                "application_attempt_index": audit.get("application_attempt_index"),
                "application_max_attempts": audit.get("application_max_attempts"),
                "status": audit.get("status"),
                "retryable": bool(audit.get("retryable")),
                "retry_scheduled": bool(audit.get("retry_scheduled")),
                "retry_delay_seconds": audit.get("retry_delay_seconds"),
                "provider_error_type": audit.get("provider_error_type"),
                "provider_error_detail": private_error,
            },
        )

    def on_completion(index: int, completion: TeacherCompletion) -> None:
        nonlocal input_tokens, output_tokens
        if index in processed or index < 0 or index >= len(plan):
            raise RuntimeError("Teacher completion index violated immutable plan")
        sample = plan[index]
        raw = completion.raw_response
        response_hash = sha256_text(raw) if raw is not None else None
        raw_model = completion.reported_model_raw or completion.reported_model
        reported_model = _safe_public_model_alias(raw_model)
        raw_finish = completion.finish_reason_raw or completion.finish_reason
        finish_reason = _safe_finish_reason(raw_finish)
        reported_model_missing = raw_model is None
        reported_model_alias_invalid = raw_model is not None and reported_model is None
        finish_reason_missing = raw_finish is None
        finish_reason_invalid = raw_finish is not None and (
            finish_reason is None
            or (
                args.provider == "openai"
                and finish_reason != REQUIRED_FINISH_REASON
            )
        )
        input_tokens += completion.input_tokens or 0
        output_tokens += completion.output_tokens or 0

        failure_code: Optional[str] = None
        private_error: Optional[str] = None
        parsed: Any = None
        parser_attempted = False
        if raw is None:
            failure_code = (
                "provider_response_shape_invalid"
                if completion.error_type == "ProviderResponseShapeError"
                else "provider_exception"
            )
            private_error = _safe_private(manager, completion.error_detail)
        else:
            manager.events.emit(
                "LLMResponseRecorded",
                agent_id=_latent_id(sample.view),
                data={
                    "run_kind": RUN_KIND,
                    "sample_id": _sample_id(sample),
                    "source": "record",
                    "response_hash": response_hash,
                    "reported_model": reported_model,
                    "response_id_sha256": (
                        sha256_text(completion.response_id)
                        if completion.response_id is not None
                        else None
                    ),
                },
                private_data={
                    "raw_response": raw,
                    "provider_response_id": _safe_private(
                        manager, completion.response_id
                    ),
                    "provider_reported_model_raw": _safe_private(manager, raw_model),
                },
            )
            if args.provider == "openai" and reported_model != REQUIRED_REPORTED_MODEL:
                failure_code = "reported_model_mismatch"
                private_error = "reported model did not match frozen HiggsAI alias"
            elif args.provider == "openai" and finish_reason != REQUIRED_FINISH_REASON:
                failure_code = "finish_reason_invalid"
                private_error = "finish reason did not match frozen stop contract"
            else:
                try:
                    parser_attempted = True
                    parsed = information_weight.parse_teacher_response(raw, sample.view)
                except Exception as error:
                    failure_code = "teacher_response_invalid"
                    private_error = _safe_private(
                        manager, "{}: {}".format(type(error).__name__, error)
                    )

        decision = _public_decision(parsed) if parsed is not None else None
        private_rationale = _private_rationale(parsed) if parsed is not None else None
        status = "valid" if parsed is not None else "failed"
        base = _plan_public_row(sample, index)
        public_row = {
            "schema_version": SAMPLE_SCHEMA_VERSION,
            **base,
            "provider": args.provider,
            "model_requested": FROZEN_MODEL if args.provider == "openai" else None,
            "reported_model": reported_model,
            "reported_model_missing": reported_model_missing,
            "reported_model_alias_invalid": reported_model_alias_invalid,
            "finish_reason": finish_reason,
            "finish_reason_missing": finish_reason_missing,
            "finish_reason_invalid": finish_reason_invalid,
            "generation_sampling": {
                "temperature": FROZEN_TEMPERATURE,
                "max_tokens": FROZEN_MAX_TOKENS,
                "top_p": FROZEN_TOP_P,
                "top_k": FROZEN_TOP_K,
            },
            "response_format": dict(FROZEN_RESPONSE_FORMAT),
            "response_hash": response_hash,
            "parser_attempted": parser_attempted,
            "status": status,
            "decision": decision,
            "failure_code": failure_code,
            "input_tokens": completion.input_tokens,
            "output_tokens": completion.output_tokens,
            "application_attempt_count": int(completion.application_attempt_count),
            "technical_retry_count": int(completion.technical_retry_count),
        }
        private_row = {
            "schema_version": PRIVATE_RECORD_SCHEMA_VERSION,
            "record_type": "logical_completion",
            **public_row,
            "raw_response": _safe_private(manager, raw),
            "private_rationale": _safe_private(manager, private_rationale),
            "provider_error_type": completion.error_type,
            "provider_error_detail": private_error,
            "provider_response_id": _safe_private(manager, completion.response_id),
            "provider_response_id_sha256": (
                sha256_text(completion.response_id)
                if completion.response_id is not None
                else None
            ),
            "provider_reported_model_raw": _safe_private(manager, raw_model),
            "provider_reported_model_raw_sha256": (
                sha256_text(raw_model) if raw_model is not None else None
            ),
            "provider_finish_reason_raw": _safe_private(manager, raw_finish),
            "provider_finish_reason_raw_sha256": (
                sha256_text(raw_finish) if raw_finish is not None else None
            ),
            "provider_sdk_response_json": _safe_private(
                manager, completion.provider_sdk_response_json
            ),
            "provider_sdk_response_json_sha256": (
                sha256_text(completion.provider_sdk_response_json)
                if completion.provider_sdk_response_json is not None
                else None
            ),
        }
        _append_jsonl(private_stream, private_row)
        _append_jsonl(public_stream, public_row)
        processed.add(index)
        public_rows.append(public_row)
        manager.events.emit(
            "InformationWeightTeacherSampleValidated",
            agent_id=_latent_id(sample.view),
            data={
                "sample_id": _sample_id(sample),
                "profile_id": _profile_id(sample.view),
                "status": status,
                "failure_code": failure_code,
            },
            private_data={
                "private_rationale": _safe_private(manager, private_rationale),
                "validation_error": private_error,
            },
        )
        _sync_accounting(manager, provider, public_rows)

    try:
        if args.provider == "openai":

            async def execute_and_close() -> list[TeacherCompletion]:
                try:
                    return await provider.complete_many(
                        [(sample.prompt.system, sample.prompt.user) for sample in plan],
                        before_attempt=before_attempt,
                        on_application_attempt=on_application_attempt,
                        on_completion=on_completion,
                        strict_sequential=True,
                    )
                finally:
                    await provider.aclose()

            asyncio.run(execute_and_close())
        else:
            provider.complete_plan(
                plan,
                before_attempt=before_attempt,
                on_application_attempt=on_application_attempt,
                on_completion=on_completion,
            )
    finally:
        private_stream.close()
        public_stream.close()

    honest_n = _sync_accounting(manager, provider, public_rows)
    failure_counts = Counter(
        str(row["failure_code"])
        for row in public_rows
        if row.get("failure_code") is not None
    )
    profile_counts: dict[str, dict[str, Any]] = {}
    for profile_id in information_weight.PROFILE_IDS:
        rows = [row for row in public_rows if row["profile_id"] == profile_id]
        valid_decisions = [
            row["decision"]
            for row in rows
            if row.get("status") == "valid" and isinstance(row.get("decision"), Mapping)
        ]
        action_counts = Counter(
            str(decision.get("action")) for decision in valid_decisions
        )
        signed_values = [
            {"sell": -1.0, "hold": 0.0, "buy": 1.0}.get(
                str(decision.get("action"))
            )
            for decision in valid_decisions
        ]
        signed_values = [value for value in signed_values if value is not None]
        intensities = [
            float(decision["intensity"])
            for decision in valid_decisions
            if isinstance(decision.get("intensity"), (int, float))
            and not isinstance(decision.get("intensity"), bool)
        ]
        profile_counts[profile_id] = {
            "planned": FROZEN_LATENT_STATE_COUNT,
            "resolved": len(rows),
            "valid": sum(row["status"] == "valid" for row in rows),
            "failed": sum(row["status"] != "valid" for row in rows),
            "action_counts": {
                action: int(action_counts.get(action, 0))
                for action in ("buy", "hold", "sell")
            },
            "mean_signed_action": (
                sum(signed_values) / len(signed_values) if signed_values else None
            ),
            "mean_intensity": (
                sum(intensities) / len(intensities) if intensities else None
            ),
        }
    summary = {
        "output_schema_version": OUTPUT_SCHEMA_VERSION,
        "run_id": manager.run_id,
        "run_kind": RUN_KIND,
        "diagnostic_only": True,
        "teacher_only": True,
        "student_enabled": False,
        "market_enabled": False,
        "provider": args.provider,
        "model_requested": FROZEN_MODEL if args.provider == "openai" else None,
        "required_reported_model": (
            REQUIRED_REPORTED_MODEL if args.provider == "openai" else None
        ),
        "required_finish_reason": (
            REQUIRED_FINISH_REASON if args.provider == "openai" else None
        ),
        "live": bool(args.live),
        "dry_run": False,
        "network_access": bool(getattr(provider, "network_access", False)),
        "planned_latent_states": FROZEN_LATENT_STATE_COUNT,
        "planned_views_per_latent_state": FROZEN_VIEW_COUNT,
        "planned_logical_requests": FROZEN_REQUEST_COUNT,
        "strict_sequential": True,
        "honest_n": honest_n,
        "profile_completion": profile_counts,
        "failure_counts": dict(sorted(failure_counts.items())),
        "token_usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "source": "provider_usage_when_available",
        },
        "artifacts": {
            "latent_state_design": "latent_state_design.json",
            "sample_plan": "sample_plan.json",
            "public_samples": "information_weight_samples.jsonl",
            "private_records": "private_information_weight_records.jsonl",
            "private_records_mode": "0600",
        },
        "scientific_claim_status": (
            "engineering_fake_control"
            if args.provider != "openai"
            else "exploratory_endpoint_teacher_diagnostic_not_human_ground_truth"
        ),
        "scientific_semantics_change": (
            "additive Teacher-only information-view diagnostic; no existing prompt, "
            "Persona, Student, market, clearing, financing, CLI, or result schema changed"
        ),
        **_identity_hashes(identities),
    }
    manager.manifest[RUN_KIND]["summary"] = {
        "honest_n": dict(honest_n),
        "failure_counts": dict(sorted(failure_counts.items())),
        "scientific_claim_status": summary["scientific_claim_status"],
    }
    manager._write()
    return summary


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
        _validate_args(args)
        from nmsim import information_weight

        latent_states = information_weight.generate_latent_states(
            count=FROZEN_LATENT_STATE_COUNT,
            seed=FROZEN_SEED,
            study_id=FROZEN_STUDY_ID,
        )
        plan = information_weight.build_sample_plan(
            states=latent_states,
            seed=FROZEN_SEED,
            study_id=FROZEN_STUDY_ID,
        )
        if len(latent_states) != FROZEN_LATENT_STATE_COUNT:
            raise InformationWeightProtocolError(
                "frozen design must contain exactly 50 latent states"
            )
        if len(plan) != FROZEN_REQUEST_COUNT:
            raise InformationWeightProtocolError(
                "frozen sample plan must contain exactly 200 requests"
            )
        per_latent = Counter(_latent_id(sample.view) for sample in plan)
        profiles = Counter(_profile_id(sample.view) for sample in plan)
        if set(per_latent.values()) != {FROZEN_VIEW_COUNT}:
            raise InformationWeightProtocolError(
                "each latent state must have exactly four information views"
            )
        if set(profiles) != set(information_weight.PROFILE_IDS) or set(
            profiles.values()
        ) != {FROZEN_LATENT_STATE_COUNT}:
            raise InformationWeightProtocolError(
                "each frozen information profile must cover all 50 latent states"
            )
        identities = build_information_weight_identities(args, latent_states, plan)
    except (
        ManagedCLIError,
        InformationWeightProtocolError,
        InformationWeightProviderGuardError,
        OSError,
        ValueError,
    ) as error:
        fail_cli(bootstrap, error, failure_stage="config_validation")

    cfg = Config(
        provider="mock",
        model="",
        seed=FROZEN_SEED,
        n_rounds=0,
        news_round=0,
        n_llm_agents=0,
        n_noise_agents=0,
        cache_enabled=False,
        temperature=FROZEN_TEMPERATURE,
        max_tokens=FROZEN_MAX_TOKENS,
        out_dir=args.out,
    )
    manager = ManagedRunContext.create(
        cfg,
        out_root=args.out,
        scenario_id="{}:{}".format(
            FROZEN_STUDY_ID, identities["scientific_config_hash"][:16]
        ),
        run_id=args.run_id,
        worker_count=FROZEN_WORKERS,
        batching={
            "latent_states": FROZEN_LATENT_STATE_COUNT,
            "views_per_state": FROZEN_VIEW_COUNT,
            "logical_requests": FROZEN_REQUEST_COUNT,
            "strategy": "strict_sequential_continue_across_logical_failures",
        },
        input_paths={
            "information_weight_contract_source": repo_root
            / "nmsim/information_weight.py",
            "information_weight_entrypoint_source": Path(__file__).resolve(),
        },
        repo_root=repo_root,
        command_identity=COMMAND_IDENTITY,
        run_kind=RUN_KIND,
        planned_simulation_runs=0,
        research_profile={
            "profile_id": FROZEN_STUDY_ID,
            "persona_contract": {
                "applicable": False,
                "reason": (
                    "information weighting is induced by numeric observation "
                    "allocation; no Persona or trader-type label is supplied"
                ),
            },
            "prompt_contract": {
                "schema_version": PROTOCOL_VERSION,
                "teacher_contract_hash": information_weight.CONTRACT_HASH,
                "scientific_config_hash": identities["scientific_config_hash"],
                "model_request_config_hash": identities[
                    "model_request_config_hash"
                ],
            },
        },
    )
    try:
        with manager:
            _initialise_manifest(
                manager, args=args, identities=identities
            )
            manager.register_llm_runtime(
                provider=args.provider,
                model=(FROZEN_MODEL if args.provider == "openai" else None),
                mode=(RUN_KIND + "_dry_run" if args.dry_run else RUN_KIND),
                cache_enabled=False,
                network_access=False,
                provider_calls=0,
                live=bool(args.live),
            )
            if args.dry_run:
                manager.set_stage("result_export")
                summary = _dry_summary(
                    manager, args=args, identities=identities
                )
                _write_json_exclusive(
                    manager.run_dir / "dry_run_summary.json", summary
                )
                manager.finish()
                print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
                return

            manager.set_stage("provider_setup")
            _write_json_exclusive(
                manager.run_dir / "latent_state_design.json",
                {
                    "schema_version": "information_weight_latent_design/0.1",
                    "study_id": FROZEN_STUDY_ID,
                    "seed": FROZEN_SEED,
                    "state_design_hash": identities["state_design_hash"],
                    "latent_states": [_object_dict(state) for state in latent_states],
                },
            )
            _write_json_exclusive(
                manager.run_dir / "sample_plan.json",
                {
                    "schema_version": "information_weight_sample_plan/0.1",
                    "sample_plan_hash": identities["sample_plan_hash"],
                    "planned_logical_requests": FROZEN_REQUEST_COUNT,
                    "strict_sequential": True,
                    "samples": [
                        _plan_public_row(sample, index)
                        for index, sample in enumerate(plan)
                    ],
                },
            )
            summary = run_teacher_acquisition(
                manager,
                args=args,
                plan=plan,
                identities=identities,
            )
            manager.set_stage("result_export")
            _write_json_exclusive(
                manager.run_dir / "information_weight_teacher_summary.json",
                summary,
            )
            manager.finish()
            print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    except InformationWeightProviderGuardError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2)
    except Exception as error:
        if args.provider == "openai":
            print(
                "information-weight Teacher run failed: {}".format(
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
    "DRY_RUN_ID",
    "FAKE_RUN_ID",
    "FROZEN_LATENT_STATE_COUNT",
    "FROZEN_MODEL",
    "FROZEN_REQUEST_COUNT",
    "FROZEN_SEED",
    "FROZEN_VIEW_COUNT",
    "FakeInformationWeightTeacher",
    "InformationWeightProtocolError",
    "InformationWeightProviderGuardError",
    "LIVE_RUN_ID",
    "OUTPUT_SCHEMA_VERSION",
    "PROTOCOL_VERSION",
    "REQUIRED_FINISH_REASON",
    "REQUIRED_REPORTED_MODEL",
    "build_argparser",
    "build_information_weight_identities",
    "main",
    "run_teacher_acquisition",
]
