"""Managed 10,000-request successor to the information-weight Teacher pilot.

The frozen design contains 2,500 common latent states.  Each latent state is
rendered through the same four label-free information-allocation views used by
the 200-request pilot.  Groups are released in latent-state order; the four
views within one group may execute concurrently, with a hard limit of four.

This entrypoint is Teacher-only.  It trains no Student and runs no market.
Dry-run never constructs a Provider.  Public artifacts contain exact prompts,
visible observations, and parsed public decisions.  Raw responses, rationale,
SDK payloads, response identifiers, and provider errors are mode 0600.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping, Optional, Sequence, TextIO

from experiments import information_weight_teacher as pilot
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


PROTOCOL_VERSION = "information_weight_teacher_scale/1.0"
OUTPUT_SCHEMA_VERSION = "information_weight_teacher_scale_output/1.0"
SAMPLE_SCHEMA_VERSION = "information_weight_teacher_scale_sample/1.0"
PRIVATE_RECORD_SCHEMA_VERSION = "information_weight_teacher_scale_private_record/1.0"
IDENTITY_SCHEMA_VERSION = "information_weight_teacher_scale_identity/1.0"
LATENT_DESIGN_SCHEMA_VERSION = "information_weight_scale_latent_design/1.0"
SAMPLE_PLAN_SCHEMA_VERSION = "information_weight_scale_sample_plan/1.0"
COMMAND_IDENTITY = "python -m experiments.information_weight_teacher_scale"
RUN_KIND = "information_weight_teacher_scale"
DEFAULT_OUTPUT_ROOT = "results_information_weight_scale_10k"

FROZEN_STUDY_ID = "information-weight-scale-10k-v1"
FROZEN_SEED = 20260825
FROZEN_LATENT_STATE_COUNT = 2500
FROZEN_VIEW_COUNT = 4
FROZEN_REQUEST_COUNT = 10000
FROZEN_GROUP_COUNT = 2500
FROZEN_GROUP_SIZE = 4
FROZEN_WORKERS = 4
FROZEN_REPLICATES_PER_STATE_VIEW = 1
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

DRY_RUN_ID = "information-weight-scale-10k-v1-dry-20260825-a1"
FAKE_RUN_ID = "information-weight-scale-10k-v1-fake-20260825-a1"
LIVE_RUN_ID = "information-weight-scale-10k-live-20260825-a1"
ALLOWED_PROVIDERS = frozenset({"fake_test_teacher", "openai"})
SCIENTIFIC_LIMITATIONS = (
    "K=1 does not identify within-state-view endpoint variance; profile differences "
    "combine observation-allocation sensitivity with Provider stochasticity",
    "added price, fundamental, and news fields use independent marginal Latin "
    "hypercubes rather than a fitted coherent joint company-event distribution",
    "balanced_4_4_4 is an information-rich comparison arm, not a null or sham",
    "four concurrent views share time and endpoint load and may have correlated "
    "execution noise",
    "the 2,500-state successor is not nested in the 50-state pilot and must not be "
    "stitched to the pilot or reported as N=10,200",
    "8/2/2 and related profiles operationalize visible field allocation, not a "
    "continuous cognitive attention weight or a validated human trader type",
    "requested MiniMax-M2.7 and reported HiggsAI are distinct identities; the alias "
    "does not independently verify serving weights",
)
IDENTITY_HASH_KEYS = (
    "state_design_hash",
    "sample_plan_hash",
    "scientific_config_hash",
    "model_request_config_hash",
    "execution_config_hash",
    "full_effective_config_hash",
)


class InformationWeightScaleProtocolError(ValueError):
    """The frozen 2,500-by-four successor contract was violated."""


class InformationWeightScaleProviderGuardError(ValueError):
    """Provider access was requested outside the frozen live guard."""


# These pure/credential-safe helpers keep the already reviewed serialization
# and privacy boundary while this successor uses independent versioned schemas.
_jsonable = pilot._jsonable
_stable_hash = pilot._stable_hash
_object_dict = pilot._object_dict
_sample_id = pilot._sample_id
_latent_id = pilot._latent_id
_profile_id = pilot._profile_id
_open_exclusive = pilot._open_exclusive
_write_json_exclusive = pilot._write_json_exclusive
_append_jsonl = pilot._append_jsonl
_safe_private = pilot._safe_private
_safe_finish_reason = pilot._safe_finish_reason
_safe_public_model_alias = pilot._safe_public_model_alias
_public_decision = pilot._public_decision
_private_rationale = pilot._private_rationale
_endpoint_identity = pilot._endpoint_identity


class FakeInformationWeightScaleTeacher:
    """Deterministic offline contract control; no network and no science claim."""

    kind = "fake_test_teacher"
    model = "information-weight-scale-fake-teacher-v1"

    def __init__(self) -> None:
        self.request_count = 0
        self.response_count = 0
        self.application_attempt_count = 0
        self.provider_exception_attempts = 0
        self.retries_scheduled = 0
        self.logical_requests_with_retry = 0
        self.network_access = False
        self.batch_sizes: list[int] = []
        self.scale_global_application_attempt_audits: list[dict[str, Any]] = []

    def complete_group(
        self,
        group: Sequence[Any],
        *,
        group_base: int,
        before_attempt: Any,
        on_application_attempt: Any,
        on_completion: Any,
    ) -> None:
        from nmsim import information_weight_scale as domain

        self.batch_sizes.append(len(group))
        for local_index, sample in enumerate(group):
            global_index = group_base + local_index
            before_attempt(global_index)
            self.request_count += 1
            self.application_attempt_count += 1
            audit = {
                "logical_request_index": global_index,
                "callback_local_index": local_index,
                "callback_global_index": global_index,
                "application_attempt_index": 1,
                "application_max_attempts": 1,
                "status": "response_received",
                "provider_error_type": None,
                "provider_error_detail": None,
                "retryable": False,
                "retry_scheduled": False,
                "retry_delay_seconds": None,
            }
            self.scale_global_application_attempt_audits.append(dict(audit))
            on_application_attempt(global_index, audit)
            raw = domain.fake_teacher_response(sample)
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
            on_completion(global_index, completion)

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
        raise InformationWeightScaleProviderGuardError(
            "--dry-run and --live are mutually exclusive"
        )
    if args.provider == "openai":
        if not args.dry_run and not args.live:
            raise InformationWeightScaleProviderGuardError(
                "--provider openai requires --live"
            )
        if args.model != FROZEN_MODEL:
            raise InformationWeightScaleProviderGuardError(
                "--provider openai requires frozen --model {}".format(FROZEN_MODEL)
            )
        if not args.dry_run and args.confirm_request_count != FROZEN_REQUEST_COUNT:
            raise InformationWeightScaleProviderGuardError(
                "live acquisition requires --confirm-request-count {}".format(
                    FROZEN_REQUEST_COUNT
                )
            )
    else:
        if args.live:
            raise InformationWeightScaleProviderGuardError(
                "--live is valid only with --provider openai"
            )
        if str(args.model).strip():
            raise InformationWeightScaleProviderGuardError(
                "--model is not configurable for the offline Fake Teacher"
            )
        if args.confirm_request_count is not None:
            raise InformationWeightScaleProviderGuardError(
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
        raise InformationWeightScaleProtocolError(
            "this frozen execution mode requires --run-id {}".format(required_run_id)
        )


def _build_provider(args: argparse.Namespace) -> Any:
    if args.dry_run:
        raise InformationWeightScaleProviderGuardError(
            "dry-run must not construct a Provider"
        )
    if args.provider == "fake_test_teacher":
        return FakeInformationWeightScaleTeacher()
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


def build_information_weight_scale_identities(
    args: argparse.Namespace,
    latent_states: Sequence[Any],
    plan: Sequence[Any],
) -> dict[str, Any]:
    from nmsim import information_weight_scale as domain

    state_design_hash = _stable_hash([_object_dict(state) for state in latent_states])
    sample_plan_hash = domain.plan_hash(plan)
    endpoint_identity = _endpoint_identity() if args.provider == "openai" else None
    scientific_payload = {
        "schema_version": "information_weight_scale_scientific_config/1.0",
        "study_id": FROZEN_STUDY_ID,
        "seed": FROZEN_SEED,
        "latent_state_count": FROZEN_LATENT_STATE_COUNT,
        "profile_ids": list(domain.PROFILE_IDS),
        "information_view_contract": domain.contract_descriptor(),
        "view_count": FROZEN_VIEW_COUNT,
        "planned_samples": FROZEN_REQUEST_COUNT,
        "contract_hash": domain.CONTRACT_HASH,
        "state_design_hash": state_design_hash,
        "sample_plan_hash": sample_plan_hash,
        "teacher_only": True,
        "student_enabled": False,
        "market_enabled": False,
    }
    request_payload = {
        "schema_version": "information_weight_scale_model_request_config/1.0",
        "provider": args.provider,
        "model_requested": (
            FROZEN_MODEL
            if args.provider == "openai"
            else FakeInformationWeightScaleTeacher.model
        ),
        "temperature": FROZEN_TEMPERATURE,
        "max_tokens": FROZEN_MAX_TOKENS,
        "top_p": FROZEN_TOP_P,
        "top_k": FROZEN_TOP_K,
        "response_format": dict(FROZEN_RESPONSE_FORMAT),
        "system_prompt_sha256": sha256_text(domain.SYSTEM_PROMPT),
        "required_reported_model": (
            REQUIRED_REPORTED_MODEL if args.provider == "openai" else None
        ),
        "response_termination_contract": (
            {
                "required_finish_reason": REQUIRED_FINISH_REASON,
                "missing_or_other_finish_reason": "fail_closed",
            }
            if args.provider == "openai"
            else None
        ),
        # The credential-free route identity belongs to the model request:
        # changing the serving route can change the effective weights even
        # when every JSON generation field is byte-identical.
        "endpoint_identity": endpoint_identity,
    }
    output_path = Path(args.out).expanduser()
    if not output_path.is_absolute():
        output_path = (Path.cwd() / output_path).resolve()
    execution_payload = {
        "schema_version": "information_weight_scale_execution_config/1.0",
        "provider": args.provider,
        "live": bool(args.live),
        "dry_run": bool(args.dry_run),
        "run_id": args.run_id,
        "workers": FROZEN_WORKERS,
        "latent_group_count": FROZEN_GROUP_COUNT,
        "views_per_group": FROZEN_GROUP_SIZE,
        "group_release": "strict_latent_order",
        "within_group_transport": "bounded_concurrent",
        "callback_index_space": "global_0_based_request_index",
        "continue_across_logical_failures": True,
        "phase_inactivity_timeout_seconds": FROZEN_PHASE_TIMEOUT_SECONDS,
        "hard_request_deadline_seconds": FROZEN_HARD_DEADLINE_SECONDS,
        "connect_timeout_seconds": FROZEN_CONNECT_TIMEOUT_SECONDS,
        "sdk_retry_count": FROZEN_SDK_RETRY_COUNT,
        "application_max_attempts": FROZEN_APPLICATION_MAX_ATTEMPTS,
        "application_retry_delays_seconds": list(FROZEN_RETRY_DELAYS_SECONDS),
        "output_root_identity_sha256": _stable_hash(
            {"resolved_path": str(output_path)}
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
        "full_effective_config_hash": _stable_hash(full_payload),
    }


def _identity_hashes(identities: Mapping[str, Any]) -> dict[str, str]:
    return {key: str(identities[key]) for key in IDENTITY_HASH_KEYS}


def _plan_public_row(sample: Any, index: int) -> dict[str, Any]:
    return {
        "request_index": index,
        "latent_group_index": index // FROZEN_GROUP_SIZE,
        "within_group_index": index % FROZEN_GROUP_SIZE,
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


def _validate_frozen_plan(latent_states: Sequence[Any], plan: Sequence[Any]) -> None:
    from nmsim import information_weight_scale as domain

    if len(latent_states) != FROZEN_LATENT_STATE_COUNT:
        raise InformationWeightScaleProtocolError(
            "frozen scale design must contain exactly 2500 latent states"
        )
    if len(plan) != FROZEN_REQUEST_COUNT:
        raise InformationWeightScaleProtocolError(
            "frozen scale sample plan must contain exactly 10000 requests"
        )
    if len({_sample_id(sample) for sample in plan}) != FROZEN_REQUEST_COUNT:
        raise InformationWeightScaleProtocolError("sample ids must be unique")
    all_profiles = set(domain.PROFILE_IDS)
    for group_index in range(FROZEN_GROUP_COUNT):
        base = group_index * FROZEN_GROUP_SIZE
        group = plan[base : base + FROZEN_GROUP_SIZE]
        latent_ids = {_latent_id(sample.view) for sample in group}
        profiles = {_profile_id(sample.view) for sample in group}
        if len(latent_ids) != 1 or profiles != all_profiles:
            raise InformationWeightScaleProtocolError(
                "each contiguous latent group must contain all four views exactly once"
            )


def _initialise_manifest(
    manager: ManagedRunContext,
    *,
    args: argparse.Namespace,
    identities: Mapping[str, Any],
) -> None:
    planned = {
        "latent_states": FROZEN_LATENT_STATE_COUNT,
        "latent_groups": FROZEN_GROUP_COUNT,
        "views_per_latent_state": FROZEN_VIEW_COUNT,
        "logical_requests": FROZEN_REQUEST_COUNT,
        "group_release": "strict_latent_order",
        "within_group_transport": "bounded_concurrent",
        "worker_limit": FROZEN_WORKERS,
        "replicates_per_state_view": FROZEN_REPLICATES_PER_STATE_VIEW,
    }
    honest = {
        "logical_requests": 0,
        "physical_provider_attempts": 0,
        "raw_responses": 0,
        "parsed_decisions": 0,
        "resolved_logical_requests": 0,
        "complete_paired_latent_states": 0,
        "resolved_latent_groups": 0,
    }
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
        "planned": planned,
        "identities": _identity_hashes(identities),
        "honest_n": honest,
        "privacy": {
            "public_visible_observations_and_exact_prompts": True,
            "raw_response_and_private_rationale_artifact_mode": "0600",
            "private_rationale_in_public_artifacts": False,
        },
        "observation_allocation_semantics": (
            "field-count composition under a fixed twelve-field information budget; "
            "not an explicit attention coefficient or trader identity"
        ),
        "synthetic_design_boundary": (
            "P6/A8 reuse the coherent V2 state core; added P2/F8/N8 fields are "
            "independent marginal Latin-hypercube draws, so cross-domain combinations "
            "may be economically unusual and are not population weighted"
        ),
        "scientific_limitations": list(SCIENTIFIC_LIMITATIONS),
    }
    manager.manifest["information_weight_scale_config_identities"] = {
        "schema_version": IDENTITY_SCHEMA_VERSION,
        "scientific_config": identities["scientific_config"],
        "model_request_config": identities["model_request_config"],
        "execution_config": identities["execution_config"],
        "full_effective_config": identities["full_effective_config"],
        **_identity_hashes(identities),
    }
    manager.manifest.update(_identity_hashes(identities))
    manager.manifest["completion"]["llm_logical_requests"].update(
        {"planned": FROZEN_REQUEST_COUNT, "attempted": 0, "completed": 0, "failed": 0}
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
        "planned_latent_groups": FROZEN_GROUP_COUNT,
        "planned_views_per_latent_state": FROZEN_VIEW_COUNT,
        "planned_logical_requests": FROZEN_REQUEST_COUNT,
        "workers": FROZEN_WORKERS,
        "replicates_per_state_view": FROZEN_REPLICATES_PER_STATE_VIEW,
        "group_release": "strict_latent_order",
        "within_group_transport": "bounded_concurrent",
        "provider_constructed": False,
        "provider_calls": 0,
        "network_access": False,
        "honest_n": {
            "logical_requests": 0,
            "physical_provider_attempts": 0,
            "raw_responses": 0,
            "parsed_decisions": 0,
            "resolved_logical_requests": 0,
            "complete_paired_latent_states": 0,
            "resolved_latent_groups": 0,
        },
        "scientific_limitations": list(SCIENTIFIC_LIMITATIONS),
        **_identity_hashes(identities),
    }


def _honest_n_snapshot(
    manager: ManagedRunContext,
    provider: Any,
    public_rows: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    logical = max(
        int(getattr(provider, "request_count", 0)),
        len(public_rows),
        int(
            manager.manifest["completion"]["llm_logical_requests"].get(
                "attempted", 0
            )
        ),
    )
    complete_profiles: dict[str, set[str]] = defaultdict(set)
    resolved_profiles: dict[str, set[str]] = defaultdict(set)
    for row in public_rows:
        latent_id = str(row["latent_state_id"])
        resolved_profiles[latent_id].add(str(row["profile_id"]))
        if row.get("status") == "valid":
            complete_profiles[latent_id].add(str(row["profile_id"]))
    return {
        "logical_requests": logical,
        "physical_provider_attempts": int(
            getattr(provider, "application_attempt_count", 0)
        ),
        "raw_responses": sum(row.get("response_hash") is not None for row in public_rows),
        "parsed_decisions": sum(row.get("status") == "valid" for row in public_rows),
        "resolved_logical_requests": len(public_rows),
        "complete_paired_latent_states": sum(
            len(profiles) == FROZEN_VIEW_COUNT
            for profiles in complete_profiles.values()
        ),
        "resolved_latent_groups": sum(
            len(profiles) == FROZEN_VIEW_COUNT
            for profiles in resolved_profiles.values()
        ),
    }


def _provider_attempt_snapshot(
    provider: Any,
    public_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    audits_value = getattr(provider, "scale_global_application_attempt_audits", ())
    audits = (
        [dict(item) for item in audits_value if isinstance(item, Mapping)]
        if isinstance(audits_value, (list, tuple))
        else []
    )
    responses_received = sum(
        item.get("status") == "response_received" for item in audits
    )
    provider_exceptions = sum(
        item.get("status") == "provider_exception" for item in audits
    )
    retries_scheduled = sum(bool(item.get("retry_scheduled")) for item in audits)
    retried_logical_indices = {
        int(item["logical_request_index"])
        for item in audits
        if isinstance(item.get("logical_request_index"), int)
        and (
            int(item.get("application_attempt_index") or 0) > 1
            or bool(item.get("retry_scheduled"))
        )
    }
    exhausted_logical_indices = {
        int(item["logical_request_index"])
        for item in audits
        if isinstance(item.get("logical_request_index"), int)
        and item.get("status") == "provider_exception"
        and not bool(item.get("retry_scheduled"))
    }
    physical = int(getattr(provider, "application_attempt_count", 0))
    recognized = responses_received + provider_exceptions
    reported_models = sorted(
        {
            str(row["reported_model"])
            for row in public_rows
            if isinstance(row.get("reported_model"), str) and row.get("reported_model")
        }
    )
    finish_reason_counts = Counter(
        str(row["finish_reason"])
        for row in public_rows
        if isinstance(row.get("finish_reason"), str)
    )
    logical_attempted = max(int(getattr(provider, "request_count", 0)), len(public_rows))
    return {
        "attempted": physical,
        "succeeded": responses_received,
        "failed": provider_exceptions,
        "unresolved_physical_attempts": max(0, physical - recognized),
        "responses_received": responses_received,
        "parse_failed_responses": sum(
            row.get("failure_code") == "teacher_response_invalid" for row in public_rows
        ),
        "provider_exceptions": provider_exceptions,
        "retries_scheduled": retries_scheduled,
        # Derived from global, not per-batch, indices.  Reusing the Provider's
        # local 0..3 callback indices would undercount retries across groups.
        "logical_requests_with_retry": len(retried_logical_indices),
        "exhausted_logical_requests": len(exhausted_logical_indices),
        "unresolved_logical_requests": max(0, logical_attempted - len(public_rows)),
        "reported_models": reported_models[:16],
        "reported_models_truncated": len(reported_models) > 16,
        "invalid_reported_model_alias_count": sum(
            bool(row.get("reported_model_alias_invalid")) for row in public_rows
        ),
        "missing_reported_model_count": sum(
            bool(row.get("reported_model_missing")) for row in public_rows
        ),
        "reported_model_mismatch_count": sum(
            row.get("failure_code") == "reported_model_mismatch" for row in public_rows
        ),
        "finish_reason_counts": dict(sorted(finish_reason_counts.items())),
        "invalid_finish_reason_count": sum(
            bool(row.get("finish_reason_invalid")) for row in public_rows
        ),
        "missing_finish_reason_count": sum(
            bool(row.get("finish_reason_missing")) for row in public_rows
        ),
        "callback_index_space": "global_0_based_request_index",
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
    parsed = honest["parsed_decisions"]
    resolved = honest["resolved_logical_requests"]
    attempts["unresolved_logical_requests"] = max(0, logical - resolved)
    parser_attempted = sum(bool(row.get("parser_attempted")) for row in public_rows)
    manager.network_access = bool(getattr(provider, "network_access", False))
    completion = manager.manifest["completion"]
    completion["llm_logical_requests"].update(
        {
            "planned": FROZEN_REQUEST_COUNT,
            "attempted": logical,
            "completed": resolved,
            "failed": max(0, logical - resolved),
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
    completion["response_sources"].update(
        {
            "provider": attempts["responses_received"],
            "cache": 0,
            "replay": 0,
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
    manager.manifest["honest_n_information_weight_teacher_scale"] = dict(honest)
    manager.register_llm_runtime(
        provider_calls=physical,
        provider_calls_succeeded=attempts["succeeded"],
        provider_calls_failed=attempts["failed"],
        provider_calls_unresolved=attempts["unresolved_physical_attempts"],
        logical_requests=logical,
        recorded_responses=attempts["responses_received"],
        response_sources=dict(completion["response_sources"]),
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


def _install_physical_attempt_accounting(
    manager: ManagedRunContext,
    provider: Any,
    public_rows: Sequence[Mapping[str, Any]],
) -> None:
    """Keep partial physical/logical evidence correct on interruption."""

    original_sync = manager.sync_llm_accounting

    def sync_with_scale_attempts(llm: Any = None, tracker: Any = None) -> None:
        original_sync(llm, tracker)
        _apply_accounting_snapshot(manager, llm or provider, public_rows)

    manager.sync_llm_accounting = sync_with_scale_attempts


def _sync_accounting(
    manager: ManagedRunContext,
    provider: Any,
    public_rows: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    manager.sync_llm_accounting(provider)
    return dict(manager.manifest[RUN_KIND]["honest_n"])


async def _execute_openai_group_batches(
    provider: Any,
    plan: Sequence[Any],
    *,
    before_attempt: Any,
    on_application_attempt: Any,
    on_completion: Any,
    after_group: Any = None,
    group_size: int = FROZEN_GROUP_SIZE,
) -> None:
    """Release sequential groups while mapping every callback to global indices.

    This is deliberately injectable so a small no-network provider can verify
    the same production scheduler, cross-batch retry accounting, completion
    reordering, and group barrier without constructing a 10,000-call run.
    """

    if isinstance(group_size, bool) or not isinstance(group_size, int) or group_size < 1:
        raise ValueError("group_size must be a positive integer")
    if len(plan) % group_size:
        raise InformationWeightScaleProtocolError(
            "plan length must be exactly divisible by group_size"
        )
    provider.scale_global_application_attempt_audits = []
    group_count = len(plan) // group_size
    for group_index in range(group_count):
        base = group_index * group_size
        group = plan[base : base + group_size]
        completed: set[int] = set()

        def group_before(local_index: int) -> None:
            if local_index < 0 or local_index >= group_size:
                raise RuntimeError("Provider callback local index outside group")
            before_attempt(base + local_index)

        def group_attempt(local_index: int, audit: Mapping[str, Any]) -> None:
            if local_index < 0 or local_index >= group_size:
                raise RuntimeError("Provider attempt callback local index outside group")
            global_index = base + local_index
            transformed = {
                **dict(audit),
                "logical_request_index": global_index,
                "callback_local_index": local_index,
                "callback_global_index": global_index,
            }
            # The reused Provider records local indices because every
            # complete_many call is one group.  Normalize that private copy
            # immediately and separately retain an authoritative global ledger.
            internal = getattr(provider, "application_attempt_audits", None)
            if isinstance(internal, list) and internal:
                internal[-1]["logical_request_index"] = global_index
                internal[-1]["callback_local_index"] = local_index
                internal[-1]["callback_global_index"] = global_index
            provider.scale_global_application_attempt_audits.append(dict(transformed))
            on_application_attempt(global_index, transformed)

        def group_completion(
            local_index: int, completion: TeacherCompletion
        ) -> None:
            if local_index < 0 or local_index >= group_size:
                raise RuntimeError("Provider completion callback local index outside group")
            global_index = base + local_index
            if global_index in completed:
                raise RuntimeError("duplicate completion callback index")
            completed.add(global_index)
            # Persist immediately in callback arrival order.  The global index
            # makes order explicit, while immediate persistence preserves any
            # already received raw response if a sibling request is interrupted.
            on_completion(global_index, completion)

        await provider.complete_many(
            [(sample.prompt.system, sample.prompt.user) for sample in group],
            before_attempt=group_before,
            on_application_attempt=group_attempt,
            on_completion=group_completion,
            strict_sequential=False,
        )
        expected = set(range(base, base + group_size))
        if completed != expected:
            raise RuntimeError("group completion callbacks violated global index plan")
        if after_group is not None:
            after_group(group_index, base)


def run_teacher_acquisition(
    manager: ManagedRunContext,
    *,
    args: argparse.Namespace,
    plan: Sequence[Any],
    identities: Mapping[str, Any],
) -> dict[str, Any]:
    from nmsim import information_weight_scale as domain

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
        strict_sequential=False,
        batching_strategy="strict_group_order_four_view_bounded_concurrent",
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
        manager.run_dir / "information_weight_scale_samples.jsonl", 0o644
    )
    try:
        private_stream = _open_exclusive(
            manager.run_dir / "private_information_weight_scale_records.jsonl", 0o600
        )
    except BaseException:
        public_stream.close()
        raise

    def before_attempt(global_index: int) -> None:
        if global_index < 0 or global_index >= len(plan):
            raise RuntimeError("global Teacher request index is outside immutable plan")
        sample = plan[global_index]
        manager.events.emit(
            "LLMRequestRecorded",
            agent_id=_latent_id(sample.view),
            data={
                "run_kind": RUN_KIND,
                "request_index": global_index,
                "latent_group_index": global_index // FROZEN_GROUP_SIZE,
                "within_group_index": global_index % FROZEN_GROUP_SIZE,
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

    def on_application_attempt(
        global_index: int, audit: Mapping[str, Any]
    ) -> None:
        sample = plan[global_index]
        private_error = _safe_private(manager, audit.get("provider_error_detail"))
        public_audit = {
            "run_kind": RUN_KIND,
            "sample_id": _sample_id(sample),
            "logical_request_index": global_index,
            "callback_global_index": global_index,
            "latent_group_index": global_index // FROZEN_GROUP_SIZE,
            "within_group_index": global_index % FROZEN_GROUP_SIZE,
            "callback_local_index": audit.get("callback_local_index"),
            "application_attempt_index": audit.get("application_attempt_index"),
            "application_max_attempts": audit.get("application_max_attempts"),
            "status": audit.get("status"),
            "retryable": bool(audit.get("retryable")),
            "retry_scheduled": bool(audit.get("retry_scheduled")),
            "retry_delay_seconds": audit.get("retry_delay_seconds"),
        }
        manager.events.emit(
            "InformationWeightScaleProviderApplicationAttemptRecorded",
            agent_id=_latent_id(sample.view),
            data=public_audit,
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
                "latent_state_id": _latent_id(sample.view),
                "profile_id": _profile_id(sample.view),
                **public_audit,
                "provider_error_type": audit.get("provider_error_type"),
                "provider_error_detail": private_error,
            },
        )

    def record_completion(global_index: int, completion: TeacherCompletion) -> None:
        nonlocal input_tokens, output_tokens
        if (
            global_index in processed
            or global_index < 0
            or global_index >= len(plan)
        ):
            raise RuntimeError("Teacher completion index violated immutable global plan")
        sample = plan[global_index]
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
            or (args.provider == "openai" and finish_reason != REQUIRED_FINISH_REASON)
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
                    "request_index": global_index,
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
                    "provider_response_id": _safe_private(manager, completion.response_id),
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
                    parsed = domain.parse_teacher_response(raw, sample.view)
                except Exception as error:
                    failure_code = "teacher_response_invalid"
                    private_error = _safe_private(
                        manager, "{}: {}".format(type(error).__name__, error)
                    )

        decision = _public_decision(parsed) if parsed is not None else None
        private_rationale = _private_rationale(parsed) if parsed is not None else None
        status = "valid" if parsed is not None else "failed"
        public_row = {
            "schema_version": SAMPLE_SCHEMA_VERSION,
            **_plan_public_row(sample, global_index),
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
        processed.add(global_index)
        public_rows.append(public_row)
        manager.events.emit(
            "InformationWeightScaleTeacherSampleValidated",
            agent_id=_latent_id(sample.view),
            data={
                "request_index": global_index,
                "latent_group_index": global_index // FROZEN_GROUP_SIZE,
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

    async def execute_openai_groups() -> None:
        try:
            await _execute_openai_group_batches(
                provider,
                plan,
                before_attempt=before_attempt,
                on_application_attempt=on_application_attempt,
                on_completion=record_completion,
                after_group=lambda _group_index, _base: _sync_accounting(
                    manager, provider, public_rows
                ),
            )
        finally:
            await provider.aclose()

    try:
        if args.provider == "openai":
            asyncio.run(execute_openai_groups())
        else:
            for group_index in range(FROZEN_GROUP_COUNT):
                base = group_index * FROZEN_GROUP_SIZE
                provider.complete_group(
                    plan[base : base + FROZEN_GROUP_SIZE],
                    group_base=base,
                    before_attempt=before_attempt,
                    on_application_attempt=on_application_attempt,
                    on_completion=record_completion,
                )
                _sync_accounting(manager, provider, public_rows)
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
    for profile_id in domain.PROFILE_IDS:
        rows = [row for row in public_rows if row["profile_id"] == profile_id]
        valid_decisions = [
            row["decision"]
            for row in rows
            if row.get("status") == "valid" and isinstance(row.get("decision"), Mapping)
        ]
        action_counts = Counter(str(item.get("action")) for item in valid_decisions)
        signed_values = [
            {"sell": -1.0, "hold": 0.0, "buy": 1.0}.get(str(item.get("action")))
            for item in valid_decisions
        ]
        signed_values = [value for value in signed_values if value is not None]
        intensities = [
            float(item["intensity"])
            for item in valid_decisions
            if isinstance(item.get("intensity"), (int, float))
            and not isinstance(item.get("intensity"), bool)
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
        "planned_latent_groups": FROZEN_GROUP_COUNT,
        "planned_views_per_latent_state": FROZEN_VIEW_COUNT,
        "planned_logical_requests": FROZEN_REQUEST_COUNT,
        "workers": FROZEN_WORKERS,
        "replicates_per_state_view": FROZEN_REPLICATES_PER_STATE_VIEW,
        "group_release": "strict_latent_order",
        "within_group_transport": "bounded_concurrent",
        "continue_across_logical_failures": True,
        "callback_index_space": "global_0_based_request_index",
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
            "public_samples": "information_weight_scale_samples.jsonl",
            "private_records": "private_information_weight_scale_records.jsonl",
            "private_records_mode": "0600",
        },
        "scientific_claim_status": (
            "engineering_fake_control"
            if args.provider != "openai"
            else "exploratory_endpoint_teacher_diagnostic_not_human_ground_truth"
        ),
        "scientific_semantics_change": (
            "additive 10,000-request Teacher-only scale successor; no existing "
            "prompt, Persona, Student, market, clearing, financing, CLI, or result "
            "schema changed"
        ),
        "scientific_limitations": list(SCIENTIFIC_LIMITATIONS),
        **_identity_hashes(identities),
    }
    manager.manifest[RUN_KIND]["summary"] = {
        "honest_n": dict(honest_n),
        "failure_counts": dict(sorted(failure_counts.items())),
        "scientific_claim_status": summary["scientific_claim_status"],
        "scientific_limitations": list(SCIENTIFIC_LIMITATIONS),
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
        from nmsim import information_weight_scale as domain

        latent_states = domain.generate_latent_states(
            count=FROZEN_LATENT_STATE_COUNT,
            seed=FROZEN_SEED,
            study_id=FROZEN_STUDY_ID,
        )
        plan = domain.build_sample_plan(
            states=latent_states,
            seed=FROZEN_SEED,
            study_id=FROZEN_STUDY_ID,
        )
        _validate_frozen_plan(latent_states, plan)
        identities = build_information_weight_scale_identities(
            args, latent_states, plan
        )
    except (
        ManagedCLIError,
        InformationWeightScaleProtocolError,
        InformationWeightScaleProviderGuardError,
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
        # This Config exists only to establish the shared ManagedRunContext.
        # The dedicated Provider reads its reviewed route from explicit live
        # environment variables.  Do not leak the legacy package default route
        # or key into this study's public manifest.
        openai_base_url="",
        openai_api_key="",
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
            "latent_groups": FROZEN_GROUP_COUNT,
            "views_per_state": FROZEN_VIEW_COUNT,
            "logical_requests": FROZEN_REQUEST_COUNT,
            "strategy": "strict_group_order_four_view_bounded_concurrent",
            "callback_index_space": "global_0_based_request_index",
            "continue_across_logical_failures": True,
        },
        input_paths={
            "information_weight_scale_protocol": repo_root
            / "docs/INFORMATION_WEIGHT_TEACHER_SCALE10K.md",
            "information_weight_scale_contract_source": repo_root
            / "nmsim/information_weight_scale.py",
            "information_weight_scale_entrypoint_source": Path(__file__).resolve(),
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
                "teacher_contract_hash": domain.CONTRACT_HASH,
                "scientific_config_hash": identities["scientific_config_hash"],
                "model_request_config_hash": identities[
                    "model_request_config_hash"
                ],
            },
        },
    )
    try:
        with manager:
            _initialise_manifest(manager, args=args, identities=identities)
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
                summary = _dry_summary(manager, args=args, identities=identities)
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
                    "schema_version": LATENT_DESIGN_SCHEMA_VERSION,
                    "study_id": FROZEN_STUDY_ID,
                    "seed": FROZEN_SEED,
                    "state_design_hash": identities["state_design_hash"],
                    "latent_states": [_object_dict(state) for state in latent_states],
                },
            )
            _write_json_exclusive(
                manager.run_dir / "sample_plan.json",
                {
                    "schema_version": SAMPLE_PLAN_SCHEMA_VERSION,
                    "sample_plan_hash": identities["sample_plan_hash"],
                    "planned_latent_groups": FROZEN_GROUP_COUNT,
                    "planned_logical_requests": FROZEN_REQUEST_COUNT,
                    "group_release": "strict_latent_order",
                    "within_group_transport": "bounded_concurrent",
                    "callback_index_space": "global_0_based_request_index",
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
                manager.run_dir / "information_weight_scale_teacher_summary.json",
                summary,
            )
            manager.finish()
            print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    except InformationWeightScaleProviderGuardError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2)
    except Exception as error:
        if args.provider == "openai":
            print(
                "information-weight scale Teacher run failed: {}".format(
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
    "FROZEN_GROUP_COUNT",
    "FROZEN_GROUP_SIZE",
    "FROZEN_LATENT_STATE_COUNT",
    "FROZEN_MODEL",
    "FROZEN_REQUEST_COUNT",
    "FROZEN_SEED",
    "FROZEN_VIEW_COUNT",
    "FROZEN_WORKERS",
    "FakeInformationWeightScaleTeacher",
    "InformationWeightScaleProtocolError",
    "InformationWeightScaleProviderGuardError",
    "LIVE_RUN_ID",
    "OUTPUT_SCHEMA_VERSION",
    "PROTOCOL_VERSION",
    "REQUIRED_FINISH_REASON",
    "REQUIRED_REPORTED_MODEL",
    "build_argparser",
    "build_information_weight_scale_identities",
    "main",
    "run_teacher_acquisition",
]
