"""Managed, provider-free analysis for the preregistered multi-event pilot.

The CLI accepts one finished official driver manifest and derives every input
from its registered plan, attempt ledger, selection, and summary artifacts.  It
never discovers child runs from filenames or directory globs.  Every selected
child crosses the central managed-child lifecycle/artifact gate before it can
contribute to the complete-case seed analysis.

The pure :func:`analyze_observations` path performs no filesystem, Provider, or
network operation and is used by the synthetic numerical tests.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import errno
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import stat
from statistics import mean, stdev
import subprocess
import sys
from typing import Any, Iterable, Mapping, Optional, Sequence

from nmsim import validation as V
from nmsim.config import Config
from nmsim.config_contract import (
    CONFIG_HASH_SCHEMA_VERSION,
    build_effective_config_contract,
)
from nmsim.managed_cli import (
    BootstrapCLIError,
    ManagedCLIError,
    RaisingArgumentParser,
    bootstrap_cli,
    fail_cli,
)
from nmsim.decision_contract import (
    DECISION_VALID,
    LEGACY_PARSE_INVALID,
    MULTI_EVENT_DECISION_RESPONSE_SCHEMA,
    PROVIDER_EXCEPTION_EXHAUSTED,
    PROVIDER_PARSE_EXHAUSTED,
    STRICT_SCHEMA_INVALID,
)
from nmsim.provenance import (
    SCIENTIFIC_RUNTIME_ENVIRONMENT_SCHEMA_VERSION,
    sha256_file,
)
from nmsim.provider_attempts import PROVIDER_ATTEMPT_SCHEMA, safe_reported_model
from nmsim.multi_event import (
    ATTEMPT_SERIES_SCHEMA_VERSION,
    MultiEventMaterial,
    MultiEventProtocolError,
    PROTOCOL_SCHEMA_VERSION,
    PROTOCOL_SCHEMA_VERSION_V2,
    ProtocolProfile,
    build_attempt_run_id,
    build_attempt_series_id,
    build_experiment_slot,
    canonical_multi_event_basename,
    get_protocol_profile,
    load_multi_event_material,
    load_protocol as load_frozen_protocol,
    reference_transform_identity,
    resample_reference_log_path,
)
from nmsim.result_reuse import (
    REUSE_REASON_CODES,
    ResultReuseError,
    ReusableRunCandidate,
    load_child_run_identity,
)
from nmsim.run_context import ManagedRunContext

from experiments.driver_utils import (
    assess_run_seed_reuse,
    expected_run_seed_identity,
)
from experiments.multi_event import build_multi_event_child_command
from experiments.run_seed import build_population


PROTOCOL_PATH = Path(__file__).with_name("multi_event_protocol.json")
WORKERS2_PROTOCOL_PATH = Path(__file__).with_name(
    "multi_event_protocol_workers2.json"
)
REPO_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_LIVE_OUT = REPO_ROOT / "results_multi_event"
SELECTION_SCHEMA_VERSION = "1.0"
CELL_IDENTITY_SCHEMA_VERSION = "1.0"
SUMMARY_SCHEMA_VERSION = "1.0"
SUMMARY_FILENAME = "multi_event_summary.json"
PLOT_FILENAME = "multi_event_three_panel.png"
ARMS = ("social_off", "social_on")
RESULT_ARTIFACT = "experiment_result.json"
RUN_SEED_COMMAND = "python -m experiments.run_seed"
DRIVER_COMMAND = "experiments.multi_event"
DRIVER_MANIFEST_FILENAME = "run_manifest.json"
DRIVER_PLAN_FILENAME = "multi_event_plan.json"
DRIVER_SELECTION_FILENAME = "multi_event_selection.json"
DRIVER_ATTEMPT_LEDGER_FILENAME = "multi_event_attempts.jsonl"
DRIVER_PRIVATE_ATTEMPT_LEDGER_FILENAME = "multi_event_attempts.private.jsonl"
DRIVER_SUMMARY_FILENAME = "driver_summary.json"
DRIVER_PRIVATE_FAILURES_FILENAME = "driver_failures.private.jsonl"
ATTEMPT_COORDINATION_LOCK_NAME = ".multi_event_attempts.lock"
_HASH_HEX = frozenset("0123456789abcdef")
DRIVER_PUBLIC_REASON_CODES = frozenset(
    {
        "identity_and_health_valid",
        "child_process_launched",
        "off_policy_slot_attempt_materialized",
        "attempt_in_flight",
        "attempt_materialization_indeterminate",
        "attempt_budget_exhausted",
        "endpoint_unreachable",
        "subprocess_interrupted",
        "subprocess_exception",
        "child_attempt_not_materialized",
        "child_identity_rejected",
        "subprocess_exit",
        "driver_job_exception",
        "child_run_not_started",
        "child_result_missing",
        "live_source_snapshot_rejected",
        "acceleration_canary_deferred",
        "acceleration_canary_attempt_limit_reached",
    }
)
ANALYZER_PUBLIC_REASON_CODES = (
    frozenset(
        {
            "manifest_unreadable",
            "declared_manifest_hash_mismatch",
            "canonical_expected_identity_invalid",
            "frozen_reference_content_hash_mismatch",
            "manifest_config_missing",
            "live_source_snapshot_mismatch",
            "result_artifact_unreadable",
            "declared_result_hash_mismatch",
            "result_cell_contract_mismatch",
            "health_bad_frac_exceeded",
        }
    )
    | frozenset(
        "declared_identity_mismatch:" + field
        for field in {
            "run_id",
            "command_identity",
            "config_hash_schema_version",
            "scientific_config_hash",
            "model_request_config_hash",
            "scientific_input_identity",
            "scenario_definition_hash",
            "population_identity",
            "requested_provider",
            "requested_model",
            "resolved_provider",
            "resolved_model",
            "endpoint_identity",
            "invalid_reported_model_alias_count",
            "scientific_runtime_environment",
            "scientific_runtime_environment_identity",
        }
    )
    | frozenset(
        "config_mismatch:" + field
        for field in {
            "broadcast_mode",
            "decision_response_schema",
            "demote_influencer",
            "digest_size",
            "fundamental_value",
            "initial_price",
            "kappa",
            "leverage_enabled",
            "leverage_fraction",
            "leverage_ratio",
            "leverage_spread",
            "maintenance_margin",
            "max_llm_agents",
            "n_llm_agents",
            "n_neighbors",
            "n_noise_agents",
            "n_rounds",
            "news_round",
            "news_text",
            "news_timeline",
            "population",
            "recent_window",
            "reference_path",
            "seed",
            "seed_fraction",
            "social_enabled",
            "social_mode",
            "social_weight",
            "topology",
            "temperature",
            "max_tokens",
            "cache_enabled",
            "provider_sdk_max_retries",
            "provider",
            "model",
            "cheap_model",
            "use_cheap_model",
            "openai_base_url",
            "openai_model",
        }
    )
)
ALLOWED_PUBLIC_REASON_CODES = (
    REUSE_REASON_CODES
    | DRIVER_PUBLIC_REASON_CODES
    | ANALYZER_PUBLIC_REASON_CODES
)
STRICT_DECISION_ERROR_CODES = frozenset(
    {
        "invalid_json_object",
        "missing_required_field",
        "invalid_action",
        "invalid_quantity",
        "quantity_action_mismatch",
        "invalid_limit_price",
        "invalid_sentiment",
        "blank_reasoning",
        "blank_public_take",
    }
)
HEALTH_TERMINAL_BUCKETS = frozenset(
    {
        "strict_schema_invalid",
        "legacy_parse_invalid",
        "provider_exception_exhausted",
        "provider_parse_exhausted",
        "valid_decisions",
    }
)


class MultiEventInputError(ValueError):
    """The explicit analysis selection does not satisfy its schema."""


class ApprovalRequiredError(ValueError):
    """Confirmatory qualitative claims have no preregistered approved thresholds."""


class AnalysisAttemptLock:
    """Hold an exclusive nonblocking study-root lock while reading evidence."""

    def __init__(self, child_root: Path) -> None:
        lexical_root = Path(
            os.path.abspath(os.path.expanduser(str(child_root)))
        )
        self.path = lexical_root / ATTEMPT_COORDINATION_LOCK_NAME
        self.fd: Optional[int] = None

    def __enter__(self) -> "AnalysisAttemptLock":
        try:
            lexical_info = self.path.lstat()
        except OSError as error:
            raise MultiEventInputError(
                "attempt coordination lock cannot be inspected safely"
            ) from error
        if (
            stat.S_ISLNK(lexical_info.st_mode)
            or not stat.S_ISREG(lexical_info.st_mode)
            or stat.S_IMODE(lexical_info.st_mode) != 0o600
        ):
            raise MultiEventInputError(
                "attempt coordination lock must be regular mode 0600"
            )
        flags = os.O_RDWR
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.path, flags)
        except OSError as error:
            raise MultiEventInputError(
                "attempt coordination lock cannot be opened safely"
            ) from error
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600
                or (info.st_dev, info.st_ino)
                != (lexical_info.st_dev, lexical_info.st_ino)
            ):
                raise MultiEventInputError(
                    "attempt coordination lock must be regular mode 0600"
                )
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                raise MultiEventInputError(
                    "attempt coordination lock is held by a parent or child"
                ) from error
            self.fd = fd
            return self
        except BaseException:
            os.close(fd)
            raise

    def close(self) -> None:
        if self.fd is None:
            return
        fd, self.fd = self.fd, None
        os.close(fd)

    def __exit__(self, exc_type, exc, traceback) -> bool:
        self.close()
        return False


@dataclass(frozen=True)
class EventInput:
    material: MultiEventMaterial
    event_id: str
    reference_csv: Path
    reference_csv_sha256: str
    news_timeline: Path
    news_timeline_sha256: str
    reference_prices: tuple[float, ...]
    reference_shock_idx: int
    transformed_reference_log_path: tuple[float, ...]
    reference_transform_sha256: str


@dataclass(frozen=True)
class ChildSelection:
    event_id: str
    arm: str
    seed: int
    repeat_idx: int
    manifest_path: Path
    manifest_sha256: str
    result_sha256: str
    expected_identity: Mapping[str, Any]

    @property
    def cell(self) -> tuple[str, str, int, int]:
        return (self.event_id, self.arm, self.seed, self.repeat_idx)


@dataclass(frozen=True)
class PreparedSelection:
    protocol_sha256: str
    selection_path: Path
    child_root: Path
    reference_root: Path
    events: Mapping[str, EventInput]
    execution_plan: Mapping[str, Any]
    study_model_identity: Mapping[str, Any]
    children: tuple[ChildSelection, ...]
    declared_missing_or_rejected: tuple[Mapping[str, Any], ...]
    catalog_inputs: tuple[Path, ...]
    input_paths: Mapping[str, Path]
    driver_manifest_path: Optional[Path] = None
    driver_run_id: Optional[str] = None
    output_root_policy: Optional[Mapping[str, Any]] = None
    source_snapshot: Optional[Mapping[str, Any]] = None
    protocol_profile: Optional[ProtocolProfile] = None
    execution_acceleration: Optional[Mapping[str, Any]] = None


@dataclass(frozen=True)
class Observation:
    """One accepted child replicate after identity and health validation."""

    event_id: str
    arm: str
    seed: int
    repeat_idx: int
    run_id: str
    drop_depth: float
    reported_drop_depth: float
    bad_frac: float
    norm_log_path: tuple[float, ...]
    rmse_logprice: Optional[float]
    dtw_norm: Optional[float]
    dtw_raw_full_norm: Optional[float]
    recovery: Optional[float]

    @property
    def cell(self) -> tuple[str, str, int, int]:
        return (self.event_id, self.arm, self.seed, self.repeat_idx)


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MultiEventInputError(f"{field} must be an object")
    return value


def _list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise MultiEventInputError(f"{field} must be a list")
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise MultiEventInputError(f"{field} must be a non-empty string")
    return value


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MultiEventInputError(f"{field} must be an integer")
    return value


def _finite(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MultiEventInputError(f"{field} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise MultiEventInputError(f"{field} must be finite")
    return number


def _sha256(value: Any, field: str) -> str:
    digest = _text(value, field)
    if len(digest) != 64 or any(char not in _HASH_HEX for char in digest):
        raise MultiEventInputError(f"{field} must be a lowercase SHA-256")
    return digest


def _validated_public_reason_code(value: Any, field: str) -> str:
    reason = _text(value, field)
    if reason not in ALLOWED_PUBLIC_REASON_CODES:
        raise MultiEventInputError(
            f"{field} is not a registered public-safe reason code"
        )
    return reason


def _stable_json_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validated_runtime_environment(
    raw: Any, digest: Any, field: str
) -> Mapping[str, Any]:
    environment = _mapping(raw, field)
    required = {
        "schema_version",
        "python_implementation",
        "python_version",
        "platform",
        "architecture",
        "dependencies",
    }
    dependencies = _mapping(
        environment.get("dependencies"), f"{field}.dependencies"
    )
    if (
        set(environment) != required
        or environment.get("schema_version")
        != SCIENTIFIC_RUNTIME_ENVIRONMENT_SCHEMA_VERSION
        or any(
            not isinstance(environment.get(name), str)
            or not environment.get(name)
            for name in (
                "python_implementation",
                "python_version",
                "platform",
                "architecture",
            )
        )
        or set(dependencies)
        != {"numpy", "matplotlib", "anthropic", "openai", "httpx"}
        or any(
            value is not None
            and (not isinstance(value, str) or not value)
            for value in dependencies.values()
        )
    ):
        raise MultiEventInputError(
            f"{field} does not satisfy scientific_runtime_environment_v1"
        )
    expected_digest = _sha256(digest, f"{field}_identity")
    if _stable_json_sha256(environment) != expected_digest:
        raise MultiEventInputError(f"{field} hash is inconsistent")
    return dict(environment)


def _validated_reported_aliases(raw: Any, field: str) -> list[str]:
    aliases = _list(raw, field)
    if aliases != sorted(set(aliases)) or any(
        safe_reported_model(alias) != alias for alias in aliases
    ):
        raise MultiEventInputError(
            f"{field} must be a sorted unique list of exact safe aliases"
        )
    return aliases


def launch_order_policy(
    profile: Optional[ProtocolProfile] = None,
) -> Mapping[str, Any]:
    """Return the selected profile's acquisition-order declaration."""

    base = {
        "schema_version": "multi_event_launch_order_v1",
        "block_order": "repeat_position_then_seed_position",
        "event_rotation": "(repeat_position+seed_position)%3",
        "arm_pairing": "both_arms_adjacent_per_event_seed_repeat",
        "social_on_first": (
            "(repeat_position+seed_position+canonical_event_position)%2==1"
        ),
        "expected_event_temporal_positions": "8_each_of_3_positions",
        "expected_arm_first_counts_per_event": {
            "social_on": 12,
            "social_off": 12,
        },
        "resume_policy": (
            "filter_ineligible_slots_without_reordering_remaining_jobs"
        ),
    }
    acceleration = (
        None if profile is None else profile.execution_acceleration
    )
    if not isinstance(acceleration, Mapping):
        return base
    return {
        **base,
        "schema_version": "multi_event_paired_workers2_launch_order_v1",
        "scheduler": acceleration["scheduler"],
        "pair_barrier": bool(acceleration["pair_barrier"]),
        "submission_unit": acceleration["submission_unit"],
        "between_pair_policy": acceleration["between_pair_policy"],
    }


def _read_json(path: Path, field: str) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise MultiEventInputError(f"{field} is not readable JSON") from error
    return _mapping(value, field)


def protocol_sha256(path: Path = PROTOCOL_PATH) -> str:
    return sha256_file(path)


def load_protocol(path: Path = PROTOCOL_PATH) -> Mapping[str, Any]:
    """Load one exact-byte allowlisted protocol contract."""

    protocol, digest = load_frozen_protocol(Path(path))
    profile = get_protocol_profile(protocol, digest)
    canonical = (
        REPO_ROOT / profile.canonical_protocol_relative_path
    ).resolve(strict=True)
    if Path(path).resolve(strict=True) != canonical:
        raise MultiEventProtocolError(
            "frozen protocol must use its canonical repository path"
        )
    return protocol


def _protocol_profile(
    protocol: Mapping[str, Any], protocol_path: Path
) -> ProtocolProfile:
    """Resolve and path-bind an exact frozen protocol profile."""

    path = Path(protocol_path).resolve(strict=True)
    digest = protocol_sha256(path)
    profile = get_protocol_profile(protocol, digest)
    expected = (
        REPO_ROOT / profile.canonical_protocol_relative_path
    ).resolve(strict=True)
    if path != expected:
        raise MultiEventInputError(
            "protocol path does not match its exact frozen profile"
        )
    return profile


def _validated_acceleration_stage(
    raw: Any,
    profile: ProtocolProfile,
    *,
    required: bool,
) -> Optional[Mapping[str, Any]]:
    """Validate the exact v2 canary/full stage identity; v1 has none."""

    acceleration = profile.execution_acceleration
    if acceleration is None:
        if raw is not None:
            raise MultiEventInputError(
                "workers=1 artifacts cannot declare execution_acceleration"
            )
        return None
    if raw is None:
        if required:
            raise MultiEventInputError(
                "workers=2 artifacts require execution_acceleration"
            )
        return None
    stage = _mapping(raw, "execution_acceleration")
    expected_keys = {
        "schema_version",
        "profile_id",
        "stage",
        "scheduler",
        "workers",
        "max_in_flight_children",
        "submitted_pair_limit",
        "submitted_slot_limit",
        "max_child_attempts_per_slot",
        "full_stage_approved",
        "deferred_reason_code",
    }
    if set(stage) != expected_keys:
        raise MultiEventInputError(
            "execution_acceleration does not have the exact stage fields"
        )
    stage_name = stage.get("stage")
    if stage_name == "canary":
        expected = {
            "schema_version": "multi_event_acceleration_stage_v1",
            "profile_id": acceleration["profile_id"],
            "stage": "canary",
            "scheduler": acceleration["scheduler"],
            "workers": profile.workers,
            "max_in_flight_children": acceleration[
                "max_in_flight_children"
            ],
            "submitted_pair_limit": acceleration["canary_pair_count"],
            "submitted_slot_limit": acceleration["canary_slot_count"],
            "max_child_attempts_per_slot": acceleration[
                "canary_max_child_attempts_per_slot"
            ],
            "full_stage_approved": False,
            "deferred_reason_code": "acceleration_canary_deferred",
        }
    elif stage_name == "full":
        full_slots = int(acceleration["full_stage_slot_count"])
        submitted_slots = stage.get("submitted_slot_limit")
        if (
            isinstance(submitted_slots, bool)
            or not isinstance(submitted_slots, int)
            or submitted_slots < 2
            or submitted_slots > full_slots
            or submitted_slots % 2
        ):
            raise MultiEventInputError(
                "full execution_acceleration submitted slots must be a "
                "positive complete-pair prefix within the frozen full grid"
            )
        expected = {
            "schema_version": "multi_event_acceleration_stage_v1",
            "profile_id": acceleration["profile_id"],
            "stage": "full",
            "scheduler": acceleration["scheduler"],
            "workers": profile.workers,
            "max_in_flight_children": acceleration[
                "max_in_flight_children"
            ],
            "submitted_pair_limit": submitted_slots // 2,
            "submitted_slot_limit": submitted_slots,
            "max_child_attempts_per_slot": acceleration[
                "full_stage_max_child_attempts_per_slot"
            ],
            "full_stage_approved": True,
            "deferred_reason_code": None,
        }
    else:
        raise MultiEventInputError(
            "execution_acceleration.stage must be canary or full"
        )
    if dict(stage) != expected:
        raise MultiEventInputError(
            "execution_acceleration differs from the frozen stage profile"
        )
    return dict(expected)


def _validate_acceleration_stage_extent(
    execution_acceleration: Optional[Mapping[str, Any]],
    execution_plan: Mapping[str, Any],
) -> None:
    """Bind a stage's submitted prefix to the selected live/mock grid."""

    if execution_acceleration is None:
        return
    planned_runs = execution_plan.get("planned_runs")
    if (
        isinstance(planned_runs, bool)
        or not isinstance(planned_runs, int)
        or planned_runs < 2
        or planned_runs % 2
    ):
        raise MultiEventInputError(
            "workers2 execution plan must contain complete arm pairs"
        )
    submitted_slots = int(execution_acceleration["submitted_slot_limit"])
    submitted_pairs = int(execution_acceleration["submitted_pair_limit"])
    if (
        submitted_slots > planned_runs
        or submitted_pairs * 2 != submitted_slots
        or (
            execution_acceleration["stage"] == "full"
            and submitted_slots != planned_runs
        )
    ):
        raise MultiEventInputError(
            "execution_acceleration extent differs from the selected grid"
        )


def _resolve_explicit(root: Path, value: Any, field: str) -> Path:
    relative = Path(_text(value, field))
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise MultiEventInputError(f"{field} must be a relative path below its explicit root")
    root_resolved = root.resolve(strict=True)
    try:
        resolved = (root_resolved / relative).resolve(strict=True)
        resolved.relative_to(root_resolved)
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as error:
        raise MultiEventInputError(f"{field} is missing or escapes its explicit root") from error
    if not resolved.is_file():
        raise MultiEventInputError(f"{field} must resolve to a regular file")
    return resolved


def _planned_values(protocol: Mapping[str, Any]) -> tuple[tuple[str, ...], tuple[int, ...], tuple[int, ...]]:
    design = _mapping(protocol["design"], "design")
    events = tuple(row["event_id"] for row in design["events"])
    return events, tuple(design["seeds"]), tuple(design["repeat_indices"])


def _validated_execution_plan(
    raw: Any,
    protocol: Mapping[str, Any],
    profile: Optional[ProtocolProfile] = None,
) -> Mapping[str, Any]:
    plan = _mapping(raw, "execution_plan")
    if set(plan) != {
        "protocol_adherence",
        "execution_mode",
        "seeds",
        "repeat_indices",
        "planned_runs",
        "override_reason",
        "launch_order_policy",
    }:
        raise MultiEventInputError(
            "execution_plan does not have the exact frozen fields"
        )
    adherence = plan.get("protocol_adherence")
    if not isinstance(adherence, bool):
        raise MultiEventInputError("execution_plan.protocol_adherence must be boolean")
    mode = _text(plan.get("execution_mode"), "execution_plan.execution_mode")
    if mode not in {"mock", "openai_live"}:
        raise MultiEventInputError("execution_plan.execution_mode is unsupported")
    seeds = [_integer(value, "execution_plan.seeds[]") for value in _list(
        plan.get("seeds"), "execution_plan.seeds"
    )]
    repeats = [_integer(value, "execution_plan.repeat_indices[]") for value in _list(
        plan.get("repeat_indices"), "execution_plan.repeat_indices"
    )]
    if not seeds or len(seeds) != len(set(seeds)) or seeds != sorted(seeds):
        raise MultiEventInputError("execution_plan.seeds must be non-empty sorted unique")
    if not repeats or len(repeats) != len(set(repeats)) or repeats != sorted(repeats):
        raise MultiEventInputError(
            "execution_plan.repeat_indices must be non-empty sorted unique"
        )
    _events, frozen_seeds, frozen_repeats = _planned_values(protocol)
    if adherence:
        if mode != "openai_live":
            raise MultiEventInputError(
                "only openai_live execution may claim protocol adherence"
            )
        if seeds != list(frozen_seeds) or repeats != list(frozen_repeats):
            raise MultiEventInputError("protocol-adherent execution must use frozen N/K")
        if plan.get("override_reason") is not None:
            raise MultiEventInputError("protocol-adherent execution has no override_reason")
    else:
        if mode != "mock":
            raise MultiEventInputError("only mock execution may use a non-adherent test grid")
        if not set(seeds).issubset(frozen_seeds) or not set(repeats).issubset(
            frozen_repeats
        ):
            raise MultiEventInputError("mock override must be a subset of frozen seeds/K")
        _text(plan.get("override_reason"), "execution_plan.override_reason")
    planned_runs = len(_events) * len(ARMS) * len(seeds) * len(repeats)
    if plan.get("planned_runs") != planned_runs:
        raise MultiEventInputError("execution_plan.planned_runs is inconsistent")
    expected_launch_policy = launch_order_policy(profile)
    if plan.get("launch_order_policy") != expected_launch_policy:
        raise MultiEventInputError(
            "execution_plan.launch_order_policy differs from the frozen policy"
        )
    return {
        "protocol_adherence": adherence,
        "execution_mode": mode,
        "seeds": seeds,
        "repeat_indices": repeats,
        "planned_runs": planned_runs,
        "override_reason": plan.get("override_reason"),
        "launch_order_policy": dict(expected_launch_policy),
    }


def _planned_cells(
    protocol: Mapping[str, Any], execution_plan: Optional[Mapping[str, Any]] = None
) -> set[tuple[str, str, int, int]]:
    events, frozen_seeds, frozen_repeats = _planned_values(protocol)
    seeds = tuple(execution_plan["seeds"]) if execution_plan else frozen_seeds
    repeats = (
        tuple(execution_plan["repeat_indices"])
        if execution_plan
        else frozen_repeats
    )
    return {
        (event_id, arm, seed, repeat_idx)
        for event_id in events
        for arm in ARMS
        for seed in seeds
        for repeat_idx in repeats
    }


def _counterbalanced_cells(
    protocol: Mapping[str, Any], execution_plan: Mapping[str, Any]
) -> list[tuple[str, str, int, int]]:
    """Reconstruct the exact launch sequence without trusting the driver."""

    event_ids, _frozen_seeds, _frozen_repeats = _planned_values(protocol)
    order: list[tuple[str, str, int, int]] = []
    canonical_positions = {
        event_id: index for index, event_id in enumerate(event_ids)
    }
    for repeat_position, repeat_idx in enumerate(
        execution_plan["repeat_indices"]
    ):
        for seed_position, seed in enumerate(execution_plan["seeds"]):
            rotation = (repeat_position + seed_position) % len(event_ids)
            rotated_events = [
                event_ids[(rotation + offset) % len(event_ids)]
                for offset in range(len(event_ids))
            ]
            for event_id in rotated_events:
                event_position = canonical_positions[event_id]
                social_on_first = (
                    repeat_position + seed_position + event_position
                ) % 2 == 1
                arms = (
                    ("social_on", "social_off")
                    if social_on_first
                    else ("social_off", "social_on")
                )
                order.extend(
                    (event_id, arm, seed, repeat_idx) for arm in arms
                )
    return order


def _selection_slot(raw: Any, field: str) -> tuple[str, str, int, int]:
    item = _mapping(raw, field)
    return (
        _text(item.get("event_id"), f"{field}.event_id"),
        _text(item.get("arm"), f"{field}.arm"),
        _integer(item.get("seed"), f"{field}.seed"),
        _integer(item.get("repeat_idx"), f"{field}.repeat_idx"),
    )


def _validate_selection_partition(
    accepted_children: Sequence[Any],
    missing_or_rejected: Sequence[Any],
    protocol: Mapping[str, Any],
    execution_plan: Mapping[str, Any],
) -> None:
    """Require an explicit, disjoint accounting of every execution-plan slot."""

    planned = _planned_cells(protocol, execution_plan)
    accepted_cells = [_selection_slot(item, "children[]") for item in accepted_children]
    absent_cells = [
        _selection_slot(item, "missing_or_rejected_slots[]")
        for item in missing_or_rejected
    ]
    all_cells = accepted_cells + absent_cells
    if len(all_cells) != len(set(all_cells)):
        raise MultiEventInputError("selection slot partition contains duplicates")
    if set(all_cells) != planned:
        missing = len(planned - set(all_cells))
        extra = len(set(all_cells) - planned)
        raise MultiEventInputError(
            "selection must partition every execution-plan slot; "
            f"missing={missing}, extra={extra}"
        )
    max_attempts = int(
        protocol["acceptance_and_execution"]["max_child_attempts"]
    )
    all_attempt_run_ids: list[str] = []
    for raw in accepted_children:
        item = _mapping(raw, "children[]")
        attempts = _list(item.get("attempt_run_ids"), "children[].attempt_run_ids")
        if (
            not attempts
            or len(attempts) > max_attempts
            or any(not isinstance(run_id, str) or not run_id for run_id in attempts)
            or len(attempts) != len(set(attempts))
        ):
            raise MultiEventInputError(
                "accepted child attempt_run_ids must be non-empty, unique, and bounded"
            )
        identity = _mapping(item.get("identity"), "children[].identity")
        accepted_run_id = _text(
            item.get("accepted_run_id"), "children[].accepted_run_id"
        )
        if (
            accepted_run_id != identity.get("run_id")
            or accepted_run_id not in attempts
        ):
            raise MultiEventInputError(
                "accepted_run_id must equal identity.run_id and occur in attempt_run_ids"
            )
        all_attempt_run_ids.extend(attempts)
    for raw in missing_or_rejected:
        item = _mapping(raw, "missing_or_rejected_slots[]")
        if item.get("status") not in {"missing", "rejected"}:
            raise MultiEventInputError(
                "missing_or_rejected_slots[].status must be missing or rejected"
            )
        reasons = _list(item.get("reason_codes"), "missing_or_rejected_slots[].reason_codes")
        if not reasons:
            raise MultiEventInputError(
                "missing/rejected planned slots require non-empty public reason_codes"
            )
        for index, reason in enumerate(reasons):
            _validated_public_reason_code(
                reason,
                f"missing_or_rejected_slots[].reason_codes[{index}]",
            )
        attempts = item.get("attempt_run_ids", [])
        if (
            not isinstance(attempts, list)
            or len(attempts) > max_attempts
            or any(not isinstance(run_id, str) or not run_id for run_id in attempts)
            or len(attempts) != len(set(attempts))
        ):
            raise MultiEventInputError(
                "missing/rejected attempt_run_ids must be unique and bounded"
            )
        if item["status"] == "missing" and attempts:
            raise MultiEventInputError("a missing slot cannot list attempted run ids")
        if item["status"] == "rejected" and not attempts:
            raise MultiEventInputError("a rejected slot must list an attempted run id")
        all_attempt_run_ids.extend(attempts)
    if len(all_attempt_run_ids) != len(set(all_attempt_run_ids)):
        raise MultiEventInputError(
            "one managed attempt run_id cannot be assigned to multiple slots"
        )


def _validate_acceleration_selection_partition(
    accepted_children: Sequence[Any],
    missing_or_rejected: Sequence[Any],
    *,
    protocol: Mapping[str, Any],
    execution_plan: Mapping[str, Any],
    execution_acceleration: Optional[Mapping[str, Any]],
) -> None:
    """Bind canary/full selection cells to the exact submitted pair prefix."""

    if execution_acceleration is None:
        return
    _validate_acceleration_stage_extent(
        execution_acceleration, execution_plan
    )
    ordered = _counterbalanced_cells(protocol, execution_plan)
    submitted_limit = int(
        execution_acceleration["submitted_slot_limit"]
    )
    submitted = set(ordered[:submitted_limit])
    deferred = set(ordered[submitted_limit:])
    accepted_by_cell = {
        _selection_slot(item, "children[]"): _mapping(item, "children[]")
        for item in accepted_children
    }
    absent_by_cell = {
        _selection_slot(item, "missing_or_rejected_slots[]"): _mapping(
            item, "missing_or_rejected_slots[]"
        )
        for item in missing_or_rejected
    }
    if execution_acceleration["stage"] == "canary":
        if set(accepted_by_cell) & deferred:
            raise MultiEventInputError(
                "canary selection accepted a slot outside its first pair"
            )
        for cell in deferred:
            item = absent_by_cell.get(cell)
            if (
                item is None
                or item.get("status") != "missing"
                or item.get("reason_codes")
                != ["acceleration_canary_deferred"]
                or item.get("attempt_run_ids", []) != []
            ):
                raise MultiEventInputError(
                    "canary deferred selection slot differs from the frozen policy"
                )
        for cell in submitted:
            item = accepted_by_cell.get(cell) or absent_by_cell.get(cell)
            if (
                item is None
                or item.get("reason_codes")
                == ["acceleration_canary_deferred"]
            ):
                raise MultiEventInputError(
                    "canary must account for both submitted first-pair slots"
                )
    elif any(
        _mapping(item, "missing_or_rejected_slots[]").get("reason_codes")
        == ["acceleration_canary_deferred"]
        for item in missing_or_rejected
    ):
        raise MultiEventInputError(
            "full workers2 selection cannot defer canary-only slots"
        )


def build_selection_document(
    *,
    protocol: Mapping[str, Any],
    protocol_sha256: str,
    execution_plan: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    catalog_inputs: Sequence[Mapping[str, Any]],
    study_model_identity: Mapping[str, Any],
    planned_slots: Sequence[Mapping[str, Any]],
    accepted_children: Sequence[Mapping[str, Any]],
    rejected_slots: Sequence[Mapping[str, Any]],
    execution_acceleration: Optional[Mapping[str, Any]] = None,
) -> Mapping[str, Any]:
    """Pure builder for the driver's immutable post-run selection artifact.

    The caller supplies already-final child and file hashes.  This helper does
    not discover paths or write a file; it guarantees that accepted children
    plus missing/rejected entries form an exact partition of the declared
    execution plan.  A protocol-adherent live plan is the frozen 144-cell
    grid; a non-adherent mock plan is an explicitly smaller engineering grid.
    """

    _sha256(protocol_sha256, "protocol_sha256")
    profile: Optional[ProtocolProfile]
    try:
        profile = get_protocol_profile(protocol, protocol_sha256)
    except MultiEventProtocolError:
        # Preserve the historical pure-builder tests that use a placeholder
        # digest. Production callers are revalidated against the source bytes
        # in prepare_selection/prepare_driver_selection.
        if (
            protocol.get("schema_version") == PROTOCOL_SCHEMA_VERSION
            and execution_acceleration is None
        ):
            profile = None
        else:
            raise
    validated_stage = (
        None
        if profile is None
        else _validated_acceleration_stage(
            execution_acceleration,
            profile,
            required=profile.execution_acceleration is not None,
        )
    )
    validated_plan = _validated_execution_plan(
        execution_plan, protocol, profile
    )
    planned_cells = [_selection_slot(item, "planned_slots[]") for item in planned_slots]
    expected_cells = _planned_cells(protocol, validated_plan)
    if len(planned_cells) != len(set(planned_cells)) or set(planned_cells) != expected_cells:
        raise MultiEventInputError(
            "planned_slots must name each execution-plan cell exactly once"
        )
    _validate_selection_partition(
        accepted_children, rejected_slots, protocol, validated_plan
    )
    _validate_acceleration_selection_partition(
        accepted_children,
        rejected_slots,
        protocol=protocol,
        execution_plan=validated_plan,
        execution_acceleration=validated_stage,
    )
    document = {
        "schema_version": SELECTION_SCHEMA_VERSION,
        "protocol_sha256": protocol_sha256,
        "execution_plan": dict(validated_plan),
        "events": [dict(item) for item in events],
        "catalog_inputs": [dict(item) for item in catalog_inputs],
        "study_model_identity": dict(study_model_identity),
        "children": [dict(item) for item in accepted_children],
        "missing_or_rejected_slots": [
            dict(item) for item in rejected_slots
        ],
    }
    if validated_stage is not None:
        document["execution_acceleration"] = dict(validated_stage)
    return document


def prepare_selection(
    selection_path: Path,
    *,
    protocol: Mapping[str, Any],
    protocol_path: Path,
    child_root: Path,
    reference_root: Path,
) -> PreparedSelection:
    """Validate one immutable selection against shared frozen materials."""

    selection_path = Path(selection_path).resolve(strict=True)
    protocol_path = Path(protocol_path).resolve(strict=True)
    child_root = Path(child_root).resolve(strict=True)
    reference_root = Path(reference_root).resolve(strict=True)
    selection = _read_json(selection_path, "analysis selection")
    profile = _protocol_profile(protocol, protocol_path)
    expected_selection_fields = {
        "schema_version",
        "protocol_sha256",
        "execution_plan",
        "events",
        "catalog_inputs",
        "study_model_identity",
        "children",
        "missing_or_rejected_slots",
    }
    if profile.execution_acceleration is not None:
        expected_selection_fields.add("execution_acceleration")
    if set(selection) != expected_selection_fields:
        raise MultiEventInputError(
            "analysis selection does not have the exact profile fields"
        )
    if selection.get("schema_version") != SELECTION_SCHEMA_VERSION:
        raise MultiEventInputError("unsupported analysis selection schema_version")
    expected_protocol_hash = protocol_sha256(protocol_path)
    if (
        _sha256(selection.get("protocol_sha256"), "protocol_sha256")
        != expected_protocol_hash
    ):
        raise MultiEventInputError("analysis selection protocol_sha256 mismatch")

    execution_acceleration = _validated_acceleration_stage(
        selection.get("execution_acceleration"),
        profile,
        required=profile.execution_acceleration is not None,
    )
    execution_plan = _validated_execution_plan(
        selection.get("execution_plan"), protocol, profile
    )
    planned_events, _frozen_seeds, _frozen_repeats = _planned_values(protocol)
    planned_seeds = tuple(execution_plan["seeds"])
    planned_repeats = tuple(execution_plan["repeat_indices"])
    protocol_event_rows = {
        row["event_id"]: row for row in protocol["design"]["events"]
    }
    input_paths: dict[str, Path] = {
        "protocol": protocol_path,
        "analysis_selection": selection_path,
    }

    raw_catalog_inputs = _list(selection.get("catalog_inputs"), "catalog_inputs")
    if len(raw_catalog_inputs) != 1:
        raise MultiEventInputError(
            "catalog_inputs must contain exactly the authoritative v1 catalog"
        )
    catalog_item = _mapping(raw_catalog_inputs[0], "catalog_inputs[]")
    catalog_contract = protocol["reference_data_catalog"]
    if catalog_item.get("path") != catalog_contract["path"]:
        raise MultiEventInputError(
            "catalog_inputs[].path is not the authoritative protocol catalog"
        )
    catalog_digest = _sha256(
        catalog_item.get("sha256"), "catalog_inputs[].sha256"
    )
    if catalog_digest != catalog_contract["sha256"]:
        raise MultiEventInputError(
            "catalog input SHA-256 differs from the frozen protocol source bytes"
        )
    catalog_path = _resolve_explicit(
        reference_root, catalog_item.get("path"), "catalog_inputs[].path"
    )
    if sha256_file(catalog_path) != catalog_digest:
        raise MultiEventInputError("catalog input SHA-256 mismatch")
    input_paths["catalog_000"] = catalog_path

    raw_events = _list(selection.get("events"), "events")
    if len(raw_events) != len(planned_events):
        raise MultiEventInputError(
            "events must name each preregistered event exactly once"
        )
    events: dict[str, EventInput] = {}
    for raw in raw_events:
        item = _mapping(raw, "events[]")
        event_id = _text(item.get("event_id"), "events[].event_id")
        if event_id not in planned_events or event_id in events:
            raise MultiEventInputError(
                "events must name each preregistered event exactly once"
            )
        expected_event = protocol_event_rows[event_id]
        reference = _mapping(item.get("reference_csv"), "events[].reference_csv")
        timeline = _mapping(item.get("news_timeline"), "events[].news_timeline")
        if reference.get("path") != expected_event["reference_csv"]:
            raise MultiEventInputError(
                "reference_csv.path does not match the preregistered event source"
            )
        if timeline.get("path") != expected_event["news_timeline"]:
            raise MultiEventInputError(
                "news_timeline.path does not match the preregistered event source"
            )
        reference_hash = _sha256(reference.get("sha256"), "reference_csv.sha256")
        timeline_hash = _sha256(timeline.get("sha256"), "news_timeline.sha256")
        if reference_hash != expected_event["reference_csv_sha256"]:
            raise MultiEventInputError(
                "reference_csv.sha256 differs from the frozen protocol source bytes"
            )
        if timeline_hash != expected_event["news_timeline_sha256"]:
            raise MultiEventInputError(
                "news_timeline.sha256 differs from the frozen protocol source bytes"
            )
        reference_path = _resolve_explicit(
            reference_root, reference.get("path"), "reference_csv.path"
        )
        timeline_path = _resolve_explicit(
            reference_root, timeline.get("path"), "news_timeline.path"
        )
        try:
            material = load_multi_event_material(
                event_id=event_id,
                reference_csv=reference_path,
                news_timeline_jsonl=timeline_path,
                protocol_path=protocol_path,
                catalog_path=catalog_path,
            )
        except MultiEventProtocolError as error:
            raise MultiEventInputError(
                "event source does not satisfy the shared frozen material contract"
            ) from error
        if (
            material.protocol_hash != expected_protocol_hash
            or material.catalog_hash != catalog_digest
            or material.reference_hash != reference_hash
            or material.timeline_hash != timeline_hash
        ):
            raise MultiEventInputError("shared event material identity mismatch")
        transformed = _mapping(
            item.get("transformed_reference"), "events[].transformed_reference"
        )
        if transformed.get("schema_version") != "1.0":
            raise MultiEventInputError(
                "unsupported transformed_reference schema_version"
            )
        listed_transform = tuple(
            _finite(value, "transformed_reference.norm_log_path[]")
            for value in _list(
                transformed.get("norm_log_path"),
                "transformed_reference.norm_log_path",
            )
        )
        shared_transform = tuple(material.transformed.norm_log_path)
        if listed_transform != shared_transform:
            raise MultiEventInputError(
                "transformed_reference does not equal the shared frozen transform"
            )
        transform_hash = _sha256(
            transformed.get("sha256"), "transformed_reference.sha256"
        )
        if transform_hash != material.reference_transform_sha256:
            raise MultiEventInputError("transformed_reference SHA-256 mismatch")
        try:
            prices, shock_idx = V.load_reference(str(reference_path))
        except (OSError, ValueError, KeyError, IndexError) as error:
            raise MultiEventInputError("reference_csv is not loadable") from error
        if not prices or not all(
            math.isfinite(float(value)) and float(value) > 0 for value in prices
        ):
            raise MultiEventInputError(
                "reference_csv contains no valid positive path"
            )
        events[event_id] = EventInput(
            material=material,
            event_id=event_id,
            reference_csv=reference_path,
            reference_csv_sha256=reference_hash,
            news_timeline=timeline_path,
            news_timeline_sha256=timeline_hash,
            reference_prices=tuple(float(value) for value in prices),
            reference_shock_idx=int(shock_idx),
            transformed_reference_log_path=shared_transform,
            reference_transform_sha256=transform_hash,
        )
        input_paths[f"reference_{event_id}"] = reference_path
        input_paths[f"timeline_{event_id}"] = timeline_path
    if tuple(events) != tuple(planned_events):
        raise MultiEventInputError(
            "events must retain the frozen protocol event order"
        )

    study_model = _mapping(
        selection.get("study_model_identity"), "study_model_identity"
    )
    expected_study_keys = set(
        protocol["analysis_input_contract"][
            "required_study_model_identity"
        ]
    ) | {"scientific_runtime_environment"}
    if set(study_model) != expected_study_keys:
        raise MultiEventInputError("study_model_identity has unexpected fields")
    execution_mode = _text(
        study_model.get("execution_mode"),
        "study_model_identity.execution_mode",
    )
    if execution_mode != execution_plan["execution_mode"]:
        raise MultiEventInputError(
            "study_model_identity execution_mode differs from execution_plan"
        )
    study_model_fields = (
        "model_request_config_hash",
        "requested_provider",
        "resolved_provider",
        "resolved_model",
        "endpoint_identity",
    )
    for field in study_model_fields:
        if field.endswith("_hash") or field == "endpoint_identity":
            _sha256(study_model.get(field), f"study_model_identity.{field}")
        else:
            _text(study_model.get(field), f"study_model_identity.{field}")
    requested_model = study_model.get("requested_model")
    if execution_mode == "openai_live":
        _text(requested_model, "study_model_identity.requested_model")
    elif requested_model is not None and not isinstance(requested_model, str):
        raise MultiEventInputError(
            "mock study_model_identity.requested_model must be null or a string"
        )
    if execution_mode == "mock" and (
        study_model.get("requested_provider") != "mock"
        or study_model.get("resolved_provider") != "mock"
    ):
        raise MultiEventInputError(
            "mock execution must retain mock provider identity"
        )
    if (
        execution_mode == "openai_live"
        and study_model.get("requested_provider") != "openai"
    ):
        raise MultiEventInputError(
            "openai_live execution must request provider=openai"
        )
    study_reported_aliases = _validated_reported_aliases(
        study_model.get("reported_model_aliases"),
        "study_model_identity.reported_model_aliases",
    )
    invalid_alias_count = study_model.get(
        "invalid_reported_model_alias_count"
    )
    if (
        isinstance(invalid_alias_count, bool)
        or not isinstance(invalid_alias_count, int)
        or invalid_alias_count != 0
    ):
        raise MultiEventInputError(
            "study invalid_reported_model_alias_count must be exact integer zero"
        )
    study_runtime_identity = _sha256(
        study_model.get("scientific_runtime_environment_identity"),
        "study_model_identity.scientific_runtime_environment_identity",
    )
    study_runtime = _validated_runtime_environment(
        study_model.get("scientific_runtime_environment"),
        study_runtime_identity,
        "study_model_identity.scientific_runtime_environment",
    )
    if execution_mode == "mock" and study_reported_aliases:
        raise MultiEventInputError(
            "mock execution must not fabricate endpoint-reported model aliases"
        )

    raw_children = _list(selection.get("children"), "children")
    raw_missing = _list(
        selection.get("missing_or_rejected_slots"),
        "missing_or_rejected_slots",
    )
    _validate_selection_partition(
        raw_children, raw_missing, protocol, execution_plan
    )
    _validate_acceleration_selection_partition(
        raw_children,
        raw_missing,
        protocol=protocol,
        execution_plan=execution_plan,
        execution_acceleration=execution_acceleration,
    )
    declared_missing: list[Mapping[str, Any]] = []
    for raw in raw_missing:
        item = _mapping(raw, "missing_or_rejected_slots[]")
        event_id, arm, seed, repeat_idx = _selection_slot(
            item, "missing_or_rejected_slots[]"
        )
        declared_missing.append(
            {
                "event_id": event_id,
                "arm": arm,
                "seed": seed,
                "repeat_idx": repeat_idx,
                "run_id": None,
                "slot_status": item["status"],
                "attempt_run_ids": list(item.get("attempt_run_ids", [])),
                "reason_codes": list(item["reason_codes"]),
            }
        )

    required_identity = set(
        protocol["analysis_input_contract"]["required_child_identity"]
    )
    frozen_population_identity = expected_population_identity(protocol)
    children: list[ChildSelection] = []
    seen_cells: set[tuple[str, str, int, int]] = set()
    seen_run_ids: set[str] = set()
    child_reported_aliases: set[str] = set()
    runs_root = child_root / "runs"
    for index, raw in enumerate(raw_children):
        item = _mapping(raw, "children[]")
        event_id, arm, seed, repeat_idx = _selection_slot(item, "children[]")
        cell = (event_id, arm, seed, repeat_idx)
        if (
            event_id not in planned_events
            or arm not in ARMS
            or seed not in planned_seeds
            or repeat_idx not in planned_repeats
            or cell in seen_cells
        ):
            raise MultiEventInputError(
                "child cell is duplicated or outside the execution grid"
            )
        seen_cells.add(cell)
        manifest_path = _resolve_explicit(
            child_root, item.get("manifest_path"), "children[].manifest_path"
        )
        if (
            manifest_path.name != DRIVER_MANIFEST_FILENAME
            or manifest_path.parent.parent != runs_root
        ):
            raise MultiEventInputError(
                "child manifest_path must be runs/<run_id>/run_manifest.json"
            )
        manifest_hash = _sha256(
            item.get("manifest_sha256"), "manifest_sha256"
        )
        result = _mapping(
            item.get("result_artifact"), "children[].result_artifact"
        )
        if result.get("path") != RESULT_ARTIFACT:
            raise MultiEventInputError(
                "result_artifact.path must be experiment_result.json"
            )
        result_hash = _sha256(
            result.get("sha256"), "result_artifact.sha256"
        )
        identity = _mapping(item.get("identity"), "children[].identity")
        if set(identity) != required_identity:
            raise MultiEventInputError(
                "child identity does not have the exact frozen fields"
            )
        run_id = _text(identity.get("run_id"), "children[].identity.run_id")
        if run_id in seen_run_ids or manifest_path.parent.name != run_id:
            raise MultiEventInputError(
                "child run identity is duplicated or differs from its path"
            )
        seen_run_ids.add(run_id)
        if identity.get("command_identity") != RUN_SEED_COMMAND:
            raise MultiEventInputError(
                "child command_identity must be experiments.run_seed"
            )
        if (
            identity.get("config_hash_schema_version")
            != CONFIG_HASH_SCHEMA_VERSION
        ):
            raise MultiEventInputError(
                "child config hash schema is not supported"
            )
        for field in {
            "scientific_config_hash",
            "model_request_config_hash",
            "scientific_input_identity",
            "scenario_definition_hash",
            "population_identity",
            "endpoint_identity",
        }:
            _sha256(identity.get(field), f"children[].identity.{field}")
        if identity.get("population_identity") != frozen_population_identity:
            raise MultiEventInputError(
                "child population_identity does not match the frozen cast"
            )
        for field in (
            "requested_provider",
            "resolved_provider",
            "resolved_model",
        ):
            _text(identity.get(field), f"children[].identity.{field}")
        child_requested_model = identity.get("requested_model")
        if execution_mode == "openai_live":
            _text(
                child_requested_model,
                "children[].identity.requested_model",
            )
        elif child_requested_model is not None and not isinstance(
            child_requested_model, str
        ):
            raise MultiEventInputError(
                "mock child requested_model must be null or a string"
            )
        reported_aliases = _validated_reported_aliases(
            identity.get("reported_model_aliases"),
            "children[].identity.reported_model_aliases",
        )
        child_invalid_alias_count = identity.get(
            "invalid_reported_model_alias_count"
        )
        if (
            isinstance(child_invalid_alias_count, bool)
            or not isinstance(child_invalid_alias_count, int)
            or child_invalid_alias_count != 0
        ):
            raise MultiEventInputError(
                "child invalid_reported_model_alias_count must be exact integer zero"
            )
        child_runtime_identity = _sha256(
            identity.get("scientific_runtime_environment_identity"),
            "children[].identity.scientific_runtime_environment_identity",
        )
        child_runtime = _validated_runtime_environment(
            identity.get("scientific_runtime_environment"),
            child_runtime_identity,
            "children[].identity.scientific_runtime_environment",
        )
        if (
            child_runtime_identity != study_runtime_identity
            or child_runtime != study_runtime
        ):
            raise MultiEventInputError(
                "child scientific runtime environment differs from the study"
            )
        if execution_mode == "mock" and reported_aliases:
            raise MultiEventInputError(
                "mock child must have reported_model_aliases=[]"
            )
        if execution_mode == "openai_live" and len(reported_aliases) != 1:
            raise MultiEventInputError(
                "each openai_live child must report exactly one model alias"
            )
        child_reported_aliases.update(reported_aliases)
        for field in study_model_fields:
            if identity.get(field) != study_model.get(field):
                raise MultiEventInputError(
                    f"child identity does not match study identity: {field}"
                )
        if identity.get("requested_model") != requested_model:
            raise MultiEventInputError(
                "child identity does not match study requested_model"
            )
        if child_invalid_alias_count != invalid_alias_count:
            raise MultiEventInputError(
                "child invalid-alias evidence differs from the study"
            )
        children.append(
            ChildSelection(
                event_id=event_id,
                arm=arm,
                seed=seed,
                repeat_idx=repeat_idx,
                manifest_path=manifest_path,
                manifest_sha256=manifest_hash,
                result_sha256=result_hash,
                expected_identity=dict(identity),
            )
        )
        input_paths[f"child_manifest_{index:03d}"] = manifest_path
        input_paths[f"child_result_{index:03d}"] = (
            manifest_path.parent / RESULT_ARTIFACT
        )

    if sorted(child_reported_aliases) != study_reported_aliases:
        raise MultiEventInputError(
            "study reported_model_aliases must equal the accepted-child union"
        )
    return PreparedSelection(
        protocol_profile=profile,
        protocol_sha256=expected_protocol_hash,
        selection_path=selection_path,
        child_root=child_root,
        reference_root=reference_root,
        events=events,
        execution_plan=execution_plan,
        study_model_identity=dict(study_model),
        children=tuple(children),
        declared_missing_or_rejected=tuple(declared_missing),
        catalog_inputs=(catalog_path,),
        input_paths=input_paths,
        execution_acceleration=execution_acceleration,
    )


def _registered_driver_artifacts(
    manifest: Mapping[str, Any], run_dir: Path
) -> Mapping[str, Path]:
    """Re-hash every artifact registered by a finished driver parent."""

    artifacts: dict[str, Path] = {}
    for raw in _list(manifest.get("results"), "driver manifest results"):
        item = _mapping(raw, "driver manifest results[]")
        relative = Path(_text(item.get("path"), "driver result path"))
        if (
            relative.is_absolute()
            or not relative.parts
            or ".." in relative.parts
            or item.get("inside_run_directory") is not True
        ):
            raise MultiEventInputError(
                "driver result artifact must be a relative in-run path"
            )
        key = relative.as_posix()
        if key in artifacts:
            raise MultiEventInputError("driver result artifact path is duplicated")
        unresolved_path = run_dir / relative
        if unresolved_path.is_symlink():
            raise MultiEventInputError(
                "registered driver artifact cannot be a symlink"
            )
        try:
            path = unresolved_path.resolve(strict=True)
            path.relative_to(run_dir)
        except (FileNotFoundError, OSError, RuntimeError, ValueError) as error:
            raise MultiEventInputError(
                "driver result artifact is missing or escapes its run"
            ) from error
        if (
            not path.is_file()
            or item.get("exists") is not True
            or item.get("kind") != "file"
            or item.get("error") is not None
        ):
            raise MultiEventInputError("driver result artifact is not a valid file")
        expected_hash = _sha256(item.get("sha256"), "driver result sha256")
        expected_size = _integer(item.get("size_bytes"), "driver result size_bytes")
        try:
            actual_size = path.stat().st_size
        except OSError as error:
            raise MultiEventInputError("driver result artifact cannot be stated") from error
        if actual_size != expected_size or sha256_file(path) != expected_hash:
            raise MultiEventInputError("registered driver artifact integrity mismatch")
        if key in {
            DRIVER_PRIVATE_ATTEMPT_LEDGER_FILENAME,
            DRIVER_PRIVATE_FAILURES_FILENAME,
        } and stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise MultiEventInputError(
                "registered private driver artifact must have mode 0600"
            )
        artifacts[key] = path
    required = {
        DRIVER_PLAN_FILENAME,
        DRIVER_SELECTION_FILENAME,
        DRIVER_ATTEMPT_LEDGER_FILENAME,
        DRIVER_PRIVATE_ATTEMPT_LEDGER_FILENAME,
        DRIVER_SUMMARY_FILENAME,
        DRIVER_PRIVATE_FAILURES_FILENAME,
    }
    if not required <= set(artifacts):
        raise MultiEventInputError(
            "driver parent did not register every required analysis artifact"
        )
    return artifacts


def _read_jsonl_objects(path: Path, field: str) -> list[Mapping[str, Any]]:
    records: list[Mapping[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    raise MultiEventInputError(
                        f"{field} contains a blank record at line {line_number}"
                    )
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as error:
                    raise MultiEventInputError(
                        f"{field} contains invalid JSON at line {line_number}"
                    ) from error
                records.append(_mapping(value, f"{field} line {line_number}"))
    except (OSError, UnicodeError) as error:
        raise MultiEventInputError(f"{field} is unreadable") from error
    return records


def _provider_for_execution_mode(mode: str) -> str:
    if mode == "mock":
        return "mock"
    if mode == "openai_live":
        return "openai"
    raise MultiEventInputError("unsupported execution mode")


def _expected_output_root_policy(
    mode: str,
    child_root: Path,
    profile: Optional[ProtocolProfile] = None,
) -> Mapping[str, Any]:
    live = mode == "openai_live"
    canonical_relative_root = (
        "results_multi_event"
        if profile is None
        else profile.canonical_live_output_relative_path
    )
    return {
        "schema_version": "multi_event_output_root_v1",
        "effective_root": str(child_root),
        "canonical_repo_relative_root": canonical_relative_root,
        "live_canonical_root_enforced": live,
        "alternate_root_allowed": not live,
        "symlink_or_rebinding_allowed": False if live else None,
        "attempt_cap_scope": (
            "single_canonical_study_root"
            if live
            else "non_live_engineering_root"
        ),
    }


def _validated_output_root_policy(
    raw: Any,
    *,
    mode: str,
    child_root: Path,
    profile: Optional[ProtocolProfile] = None,
) -> Mapping[str, Any]:
    policy = _mapping(raw, "output_root_policy")
    expected = _expected_output_root_policy(
        mode, child_root, profile
    )
    if dict(policy) != expected:
        raise MultiEventInputError(
            "driver output_root_policy differs from the canonical policy"
        )
    canonical_live_root = (
        CANONICAL_LIVE_OUT
        if profile is None
        else REPO_ROOT / profile.canonical_live_output_relative_path
    )
    if mode == "openai_live" and child_root != canonical_live_root:
        raise MultiEventInputError(
            "openai_live analysis requires the selected profile's canonical root"
        )
    return dict(expected)


def _validated_source_snapshot(
    raw: Any,
    *,
    mode: str,
    protocol_path: Path,
    profile: Optional[ProtocolProfile] = None,
    expected_identities: Optional[Iterable[Any]] = None,
) -> Mapping[str, Any]:
    snapshot = _mapping(raw, "source_snapshot")
    if mode != "openai_live":
        expected = {
            "schema_version": "multi_event_source_snapshot_v1",
            "execution_mode": mode,
            "live_snapshot_enforced": False,
            "live_eligibility_claim": False,
            "policy": "explicit_non_live_no_preregistered_source_snapshot_claim",
            "head_commit": None,
            "protocol_last_change_commit": None,
            "scientific_component_fingerprint": None,
        }
        if dict(snapshot) != expected:
            raise MultiEventInputError(
                "non-live source_snapshot differs from the explicit null policy"
            )
        return expected

    required = {
        "schema_version",
        "execution_mode",
        "live_snapshot_enforced",
        "live_eligibility_claim",
        "policy",
        "repository_clean",
        "head_commit",
        "protocol_last_change_commit",
        "protocol_repo_relative_path",
        "scientific_component_fingerprint",
    }
    if set(snapshot) != required:
        raise MultiEventInputError(
            "live source_snapshot does not have the exact v1 fields"
        )
    head = _text(snapshot.get("head_commit"), "source_snapshot.head_commit")
    protocol_commit = _text(
        snapshot.get("protocol_last_change_commit"),
        "source_snapshot.protocol_last_change_commit",
    )
    fingerprint = _sha256(
        snapshot.get("scientific_component_fingerprint"),
        "source_snapshot.scientific_component_fingerprint",
    )
    canonical_relative_path = (
        "experiments/multi_event_protocol.json"
        if profile is None
        else profile.canonical_protocol_relative_path
    )
    canonical_protocol_path = (
        PROTOCOL_PATH
        if profile is None
        else REPO_ROOT / canonical_relative_path
    )
    if (
        snapshot.get("schema_version") != "multi_event_source_snapshot_v1"
        or snapshot.get("execution_mode") != "openai_live"
        or snapshot.get("live_snapshot_enforced") is not True
        or snapshot.get("live_eligibility_claim") is not True
        or snapshot.get("repository_clean") is not True
        or snapshot.get("policy")
        != "clean_head_equals_canonical_protocol_last_change_commit"
        or head != protocol_commit
        or protocol_path.resolve(strict=True)
        != canonical_protocol_path.resolve(strict=True)
        or snapshot.get("protocol_repo_relative_path")
        != canonical_relative_path
    ):
        raise MultiEventInputError(
            "live source_snapshot is not the clean protocol-owning snapshot"
        )
    if len(head) != 40 or any(char not in _HASH_HEX for char in head):
        raise MultiEventInputError("live source_snapshot commit is not a Git SHA")
    git_values: list[str] = []
    for arguments in (
        ("rev-parse", "HEAD"),
        (
            "log",
            "-1",
            "--format=%H",
            "--",
            canonical_relative_path,
        ),
        ("status", "--porcelain=v1", "--untracked-files=all"),
    ):
        process = subprocess.run(
            ["git", "-C", str(REPO_ROOT), *arguments],
            capture_output=True,
            text=True,
            check=False,
        )
        if process.returncode != 0:
            raise MultiEventInputError(
                "analyzer cannot independently establish current Git identity"
            )
        git_values.append(process.stdout.strip())
    current_head, current_protocol_commit, current_status = git_values
    if (
        current_head != head
        or current_protocol_commit != head
        or current_status
    ):
        raise MultiEventInputError(
            "current analyzer checkout is not the clean protocol-owning snapshot"
        )
    if expected_identities is not None and any(
        identity.git_commit != head
        or identity.scientific_component_fingerprint != fingerprint
        for identity in expected_identities
    ):
        raise MultiEventInputError(
            "live source_snapshot differs from reconstructed child source identity"
        )
    return dict(snapshot)


def _canonical_child_command(
    event: EventInput,
    *,
    arm: str,
    seed: int,
    repeat_idx: int,
    execution_mode: str,
    child_root: Path,
) -> tuple[str, ...]:
    """Rebuild a base child command without trusting a parent command."""

    return build_multi_event_child_command(
        material=event.material,
        arm=arm,
        seed=seed,
        repeat_idx=repeat_idx,
        provider=_provider_for_execution_mode(execution_mode),
        out_root=child_root,
    )


def _driver_plan_cells(
    plan: Mapping[str, Any],
    *,
    protocol: Mapping[str, Any],
    protocol_path: Path,
    execution_plan: Mapping[str, Any],
    events: Mapping[str, EventInput],
    child_root: Path,
    profile: Optional[ProtocolProfile] = None,
    execution_acceleration: Optional[Mapping[str, Any]] = None,
) -> tuple[
    Mapping[tuple[str, str, int, int], Mapping[str, Any]],
    set[tuple[str, str, int, int]],
    Mapping[tuple[str, str, int, int], Any],
]:
    """Independently reconstruct every frozen plan slot and child identity."""

    expected_protocol_hash = protocol_sha256(protocol_path)
    expected_plan_fields = {
        "schema_version",
        "protocol_id",
        "protocol_sha256",
        "pre_run_plan",
        "dry_run",
        "execution_plan",
        "provider_request",
        "output_root_policy",
        "source_snapshot",
        "health_and_retry",
        "hash_types",
        "reference_transform",
        "inputs",
        "planned_complete_seed_pairs",
        "honest_n_complete_seed_pairs",
        "jobs",
    }
    if profile is not None and profile.execution_acceleration is not None:
        expected_plan_fields.add("execution_acceleration")
    if set(plan) != expected_plan_fields:
        raise MultiEventInputError("driver plan has unexpected top-level fields")
    listed_stage = (
        None
        if profile is None
        else _validated_acceleration_stage(
            plan.get("execution_acceleration"),
            profile,
            required=profile.execution_acceleration is not None,
        )
    )
    if listed_stage != execution_acceleration:
        raise MultiEventInputError(
            "driver plan execution_acceleration differs from selection"
        )
    if (
        plan.get("schema_version") != "multi_event_plan_v1"
        or plan.get("protocol_id") != protocol["protocol_id"]
        or plan.get("protocol_sha256") != expected_protocol_hash
    ):
        raise MultiEventInputError("driver plan protocol identity mismatch")
    if plan.get("dry_run") is not False or plan.get("pre_run_plan") is not True:
        raise MultiEventInputError(
            "analysis requires a non-dry-run precommitted plan"
        )
    listed_plan = _validated_execution_plan(
        plan.get("execution_plan"), protocol, profile
    )
    if dict(listed_plan) != dict(execution_plan):
        raise MultiEventInputError(
            "driver plan and selection execution plans disagree"
        )
    provider = _provider_for_execution_mode(execution_plan["execution_mode"])
    provider_request = _mapping(
        plan.get("provider_request"), "driver plan provider_request"
    )
    if set(provider_request) != {
        "provider",
        "model",
        "temperature",
        "cache_enabled",
        "provider_sdk_max_retries",
        "workers",
        "network_access",
        "parent_network_scope",
    }:
        raise MultiEventInputError(
            "driver plan provider_request has unexpected fields"
        )
    expected_model = (
        protocol["effective_config_freeze"]["model_request"]["model"]
        if provider == "openai"
        else None
    )
    workers = _integer(
        provider_request.get("workers"),
        "driver plan provider_request.workers",
    )
    if workers < 1 or (
        execution_plan["protocol_adherence"]
        and workers
        != (
            profile.workers
            if profile is not None
            else protocol["acceptance_and_execution"]["workers"]
        )
    ):
        raise MultiEventInputError("driver plan worker count is invalid")
    if (
        provider_request.get("provider") != provider
        or provider_request.get("model") != expected_model
        or provider_request.get("temperature")
        != protocol["acceptance_and_execution"]["temperature"]
        or provider_request.get("cache_enabled")
        is not protocol["acceptance_and_execution"]["cache_enabled"]
        or provider_request.get("provider_sdk_max_retries")
        != protocol["effective_config_freeze"]["model_request"][
            "provider_sdk_max_retries"
        ]
        or provider_request.get("provider_sdk_max_retries") != 0
        or provider_request.get("network_access")
        is not (execution_plan["execution_mode"] == "openai_live")
        or provider_request.get("parent_network_scope")
        != (
            "connectivity_probe_only_children_own_provider_calls"
            if execution_plan["execution_mode"] == "openai_live"
            else "none"
        )
    ):
        raise MultiEventInputError("driver plan provider request mismatch")
    _validated_output_root_policy(
        plan.get("output_root_policy"),
        mode=execution_plan["execution_mode"],
        child_root=child_root,
        profile=profile,
    )
    health_retry = _mapping(
        plan.get("health_and_retry"), "driver plan health_and_retry"
    )
    expected_health_retry = {
        "max_bad_frac": protocol["acceptance_and_execution"][
            "health_bad_frac_max"
        ],
        "max_child_attempts": protocol["acceptance_and_execution"][
            "max_child_attempts"
        ],
        "technical_retry_identity": (
            "technical_retry_idx; excluded from repeat_idx/slot"
        ),
        "reported_model_gate": (
            "openai_live requires non-truncated exactly-one alias and "
            "invalid_reported_model_alias_count=0 per child; mock=[] and zero"
        ),
        "coordination": (
            "one output-root advisory lock inherited by launched children; "
            "ACTIVE attempts never advance"
        ),
    }
    if dict(health_retry) != expected_health_retry:
        raise MultiEventInputError("driver plan health/retry policy mismatch")
    expected_hash_types = [
        "scientific_config_hash",
        "model_request_config_hash",
        "execution_config_hash",
        "full_effective_config_hash",
        "scientific_input_identity",
        "scenario_definition_hash",
        "multi_event_slot_v1.slot_id",
        "scientific_runtime_environment_identity",
    ]
    if plan.get("hash_types") != expected_hash_types:
        raise MultiEventInputError("driver plan hash-type declaration mismatch")
    if plan.get("reference_transform") != protocol["reference_phase_transform"]:
        raise MultiEventInputError("driver plan reference transform mismatch")
    expected_inputs = [
        {
            "event_id": event.material.event_id,
            "reference_csv_sha256": event.material.reference_hash,
            "news_timeline_sha256": event.material.timeline_hash,
            "event_definition_sha256": event.material.event_definition_hash,
            "reference_transform_sha256": (
                event.material.reference_transform_sha256
            ),
        }
        for event in events.values()
    ]
    if plan.get("inputs") != expected_inputs:
        raise MultiEventInputError("driver plan material inputs mismatch")
    expected_complete_pairs = len(events) * len(execution_plan["seeds"])
    if (
        plan.get("planned_complete_seed_pairs") != expected_complete_pairs
        or plan.get("honest_n_complete_seed_pairs") != 0
    ):
        raise MultiEventInputError("driver pre-run honest-N fields mismatch")

    jobs: dict[
        tuple[str, str, int, int], Mapping[str, Any]
    ] = {}
    expected_identities: dict[tuple[str, str, int, int], Any] = {}
    max_attempts = int(
        protocol["acceptance_and_execution"]["max_child_attempts"]
    )
    expected_order = _counterbalanced_cells(protocol, execution_plan)
    raw_jobs = _list(plan.get("jobs"), "driver plan jobs")
    if len(raw_jobs) != len(expected_order):
        raise MultiEventInputError(
            "driver plan jobs do not equal the execution grid"
        )
    for launch_ordinal, (raw, expected_cell) in enumerate(
        zip(raw_jobs, expected_order), start=1
    ):
        job = _mapping(raw, "driver plan jobs[]")
        expected_job_fields = {
            "launch_ordinal",
            "event_id",
            "arm",
            "seed",
            "repeat_idx",
            "slot",
            "basename",
            "attempt_series_id",
            "allowed_attempt_run_ids",
            "child_command",
            "scientific_config_hash",
            "model_request_config_hash",
            "scientific_input_identity",
            "scenario_definition_hash",
            "scientific_runtime_environment",
            "scientific_runtime_environment_identity",
        }
        if execution_acceleration is not None:
            expected_job_fields.update(
                {"pair_ordinal", "within_pair_ordinal"}
            )
        if set(job) != expected_job_fields:
            raise MultiEventInputError(
                "driver plan job does not have the exact frozen fields"
            )
        cell = _selection_slot(job, "driver plan jobs[]")
        if (
            cell != expected_cell
            or cell in jobs
            or job.get("launch_ordinal") != launch_ordinal
            or (
                execution_acceleration is not None
                and (
                    job.get("pair_ordinal")
                    != (launch_ordinal + 1) // 2
                    or job.get("within_pair_ordinal")
                    != (1 if launch_ordinal % 2 else 2)
                )
            )
        ):
            raise MultiEventInputError(
                "driver plan launch ordinal/order differs from the counterbalanced grid"
            )
        event_id, arm, seed, repeat_idx = cell
        event = events[event_id]
        try:
            slot = build_experiment_slot(
                protocol_hash=expected_protocol_hash,
                event_id=event_id,
                social_arm=arm,
                seed=seed,
                repeat_idx=repeat_idx,
            )
            command = _canonical_child_command(
                event,
                arm=arm,
                seed=seed,
                repeat_idx=repeat_idx,
                execution_mode=execution_plan["execution_mode"],
                child_root=child_root,
            )
            expected = expected_run_seed_identity(command)
            series_id = build_attempt_series_id(slot, expected)
            allowed = [
                build_attempt_run_id(slot, series_id, index)
                for index in range(1, max_attempts + 1)
            ]
        except (MultiEventProtocolError, OSError, TypeError, ValueError) as error:
            raise MultiEventInputError(
                "canonical driver child identity could not be reconstructed"
            ) from error
        if (
            job.get("slot") != slot
            or job.get("basename") != canonical_multi_event_basename(slot)
            or job.get("child_command") != list(command)
            or job.get("attempt_series_id") != series_id
            or job.get("allowed_attempt_run_ids") != allowed
        ):
            raise MultiEventInputError(
                "driver plan job differs from its canonical command/attempt identity"
            )
        for field in (
            "scientific_config_hash",
            "model_request_config_hash",
            "scientific_input_identity",
            "scenario_definition_hash",
            "scientific_runtime_environment_identity",
        ):
            if job.get(field) != getattr(expected, field):
                raise MultiEventInputError(
                    f"driver plan job expected identity mismatch: {field}"
                )
        expected_runtime = expected.scientific_runtime_environment
        listed_runtime = _validated_runtime_environment(
            job.get("scientific_runtime_environment"),
            job.get("scientific_runtime_environment_identity"),
            "driver plan jobs[].scientific_runtime_environment",
        )
        if listed_runtime != expected_runtime:
            raise MultiEventInputError(
                "driver plan job runtime environment differs from reconstructed identity"
            )
        jobs[cell] = job
        expected_identities[cell] = expected

    planned = _planned_cells(protocol, execution_plan)
    if set(jobs) != planned:
        raise MultiEventInputError(
            "driver plan jobs do not equal the execution grid"
        )
    all_allowed = [
        run_id
        for job in jobs.values()
        for run_id in job["allowed_attempt_run_ids"]
    ]
    if len(all_allowed) != len(set(all_allowed)):
        raise MultiEventInputError(
            "attempt run IDs are not unique across plan cells"
        )
    _validated_source_snapshot(
        plan.get("source_snapshot"),
        mode=execution_plan["execution_mode"],
        protocol_path=protocol_path,
        profile=profile,
        expected_identities=expected_identities.values(),
    )
    return jobs, planned, expected_identities


def _validate_driver_attempt_ledger(
    records: Sequence[Mapping[str, Any]],
    *,
    jobs: Mapping[tuple[str, str, int, int], Mapping[str, Any]],
    selection: Mapping[str, Any],
    execution_acceleration: Optional[Mapping[str, Any]] = None,
) -> None:
    """Bind every durable launch/terminal transition to the selected prefix."""

    selected_by_cell: dict[
        tuple[str, str, int, int], Mapping[str, Any]
    ] = {}
    for field in ("children", "missing_or_rejected_slots"):
        for raw in _list(selection.get(field), field):
            item = _mapping(raw, f"{field}[]")
            cell = _selection_slot(item, f"{field}[]")
            if cell in selected_by_cell:
                raise MultiEventInputError(
                    "selection contains a duplicate ledger cell"
                )
            selected_by_cell[cell] = item
    if set(selected_by_cell) != set(jobs):
        raise MultiEventInputError(
            "selection/plan cells disagree before ledger validation"
        )

    ledger_keys = {
        "schema_version",
        "event_id",
        "arm",
        "seed",
        "repeat_idx",
        "slot_id",
        "source",
        "technical_retry_idx",
        "run_id",
        "status",
        "reason_code",
    }
    by_run_id: dict[str, list[Mapping[str, Any]]] = {}
    records_by_cell: dict[
        tuple[str, str, int, int], list[Mapping[str, Any]]
    ] = {cell: [] for cell in jobs}
    for raw in records:
        record = _mapping(raw, "driver attempt ledger[]")
        if set(record) != ledger_keys or record.get("schema_version") != "1.0":
            raise MultiEventInputError(
                "attempt ledger record does not have the exact v1 schema"
            )
        cell = _selection_slot(record, "driver attempt ledger[]")
        if cell not in jobs:
            raise MultiEventInputError(
                "attempt ledger contains an unplanned cell"
            )
        if record.get("slot_id") != jobs[cell]["slot"]["slot_id"]:
            raise MultiEventInputError(
                "attempt ledger slot_id differs from the frozen plan"
            )
        source = _text(record.get("source"), "attempt ledger source")
        status = _text(record.get("status"), "attempt ledger status")
        reason = _validated_public_reason_code(
            record.get("reason_code"), "attempt ledger reason_code"
        )
        if source not in {
            "executed",
            "resumed_attempt",
            "driver",
            "acceleration_stage",
        }:
            raise MultiEventInputError("attempt ledger source is unsupported")
        if status not in {
            "launched",
            "accepted",
            "rejected",
            "not_launched",
        }:
            raise MultiEventInputError("attempt ledger status is unsupported")
        retry_idx = record.get("technical_retry_idx")
        if retry_idx is not None:
            retry_idx = _integer(
                retry_idx, "attempt ledger technical_retry_idx"
            )
            if not 1 <= retry_idx <= len(
                jobs[cell]["allowed_attempt_run_ids"]
            ):
                raise MultiEventInputError(
                    "attempt ledger retry index is outside the frozen budget"
                )
        run_id = record.get("run_id")
        if run_id is None:
            valid_no_run = (
                status == "not_launched"
                and source == "executed"
                and retry_idx is not None
            ) or (
                status == "rejected"
                and source == "driver"
            ) or (
                status == "not_launched"
                and source == "acceleration_stage"
                and retry_idx is None
                and reason == "acceleration_canary_deferred"
            )
            if not valid_no_run:
                raise MultiEventInputError(
                    "run-id-free ledger record has invalid transition semantics"
                )
        else:
            run_id = _text(run_id, "attempt ledger run_id")
            if retry_idx is None:
                raise MultiEventInputError(
                    "attempt ledger run_id requires technical_retry_idx"
                )
            allowed = jobs[cell]["allowed_attempt_run_ids"]
            if run_id != allowed[retry_idx - 1]:
                raise MultiEventInputError(
                    "attempt ledger run_id is not the indexed frozen plan ID"
                )
            if status == "not_launched":
                raise MultiEventInputError(
                    "not_launched ledger record cannot name a run_id"
                )
            if source == "resumed_attempt" and status == "launched":
                raise MultiEventInputError(
                    "resumed attempts record only terminal validation"
                )
            if source == "driver" and status != "rejected":
                raise MultiEventInputError(
                    "driver-source attempt transition must be rejected"
                )
            by_run_id.setdefault(run_id, []).append(record)
        records_by_cell[cell].append(record)
        del reason

    for cell, job in jobs.items():
        cell_records = records_by_cell[cell]
        if not cell_records:
            raise MultiEventInputError(
                "finished driver has a plan cell with no durable outcome"
            )
        selected = selected_by_cell[cell]
        selected_attempts = list(selected.get("attempt_run_ids", []))
        stage_attempt_limit = (
            len(job["allowed_attempt_run_ids"])
            if execution_acceleration is None
            else int(
                execution_acceleration[
                    "max_child_attempts_per_slot"
                ]
            )
        )
        if len(selected_attempts) > stage_attempt_limit:
            raise MultiEventInputError(
                "selection attempt prefix exceeds the execution-stage cap"
            )
        allowed = list(job["allowed_attempt_run_ids"])
        if selected_attempts != allowed[: len(selected_attempts)]:
            raise MultiEventInputError(
                "selection attempt_run_ids are not the plan's contiguous prefix"
            )
        ledger_attempts: list[str] = []
        for record in cell_records:
            run_id = record.get("run_id")
            if run_id is not None and run_id not in ledger_attempts:
                ledger_attempts.append(str(run_id))
        if ledger_attempts != selected_attempts:
            raise MultiEventInputError(
                "selection attempt prefix disagrees with durable attempt ledger"
            )
        no_run_positions = [
            index
            for index, record in enumerate(cell_records)
            if record.get("run_id") is None
        ]
        if (
            len(no_run_positions) > 1
            or no_run_positions
            and no_run_positions[0] != len(cell_records) - 1
        ):
            raise MultiEventInputError(
                "run-id-free terminal failure must be the final cell record"
            )

        accepted_run_ids: list[str] = []
        for index, run_id in enumerate(selected_attempts, 1):
            run_records = by_run_id.get(run_id, [])
            statuses = [str(record["status"]) for record in run_records]
            sources = [str(record["source"]) for record in run_records]
            retries = [
                record.get("technical_retry_idx")
                for record in run_records
            ]
            if any(value != index for value in retries):
                raise MultiEventInputError(
                    "attempt ledger retry index is not a contiguous prefix"
                )
            resumed_terminal_only = (
                len(statuses) == 1
                and statuses[0] in {"accepted", "rejected"}
                and sources[0] == "resumed_attempt"
            )
            launched_terminal = (
                len(statuses) == 2
                and statuses[0] == "launched"
                and statuses[1] in {"accepted", "rejected"}
                and sources[0] == "executed"
                and sources[1] in {"executed", "driver"}
            )
            if not (resumed_terminal_only or launched_terminal):
                raise MultiEventInputError(
                    "each attempt requires one valid durable terminal transition"
                )
            if statuses[-1] == "accepted":
                accepted_run_ids.append(run_id)
        if len(accepted_run_ids) > 1:
            raise MultiEventInputError(
                "one plan cell has multiple accepted attempts"
            )
        if accepted_run_ids and accepted_run_ids[0] != selected_attempts[-1]:
            raise MultiEventInputError(
                "attempts continue after an accepted child"
            )
        if "accepted_run_id" in selected:
            if accepted_run_ids != [selected.get("accepted_run_id")]:
                raise MultiEventInputError(
                    "selection accepted child disagrees with attempt ledger"
                )
        else:
            if accepted_run_ids:
                raise MultiEventInputError(
                    "selection omits a child accepted by the durable ledger"
                )
            status = selected.get("status")
            if status == "missing" and selected_attempts:
                raise MultiEventInputError(
                    "missing selection cell has attempts"
                )
            if status == "rejected" and not selected_attempts:
                raise MultiEventInputError(
                    "rejected selection cell has no attempts"
                )

    if execution_acceleration is not None:
        ordered_cells = list(jobs)
        submitted_limit = int(
            execution_acceleration["submitted_slot_limit"]
        )
        submitted = set(ordered_cells[:submitted_limit])
        deferred = set(ordered_cells[submitted_limit:])
        if execution_acceleration["stage"] == "canary":
            for cell in submitted:
                if any(
                    record.get("source") == "acceleration_stage"
                    for record in records_by_cell[cell]
                ):
                    raise MultiEventInputError(
                        "canary submitted pair cannot be marked deferred"
                    )
            for cell in deferred:
                selected = selected_by_cell[cell]
                cell_records = records_by_cell[cell]
                if (
                    selected.get("status") != "missing"
                    or selected.get("attempt_run_ids") != []
                    or selected.get("reason_codes")
                    != ["acceleration_canary_deferred"]
                    or len(cell_records) != 1
                    or cell_records[0].get("source")
                    != "acceleration_stage"
                    or cell_records[0].get("status") != "not_launched"
                    or cell_records[0].get("reason_code")
                    != "acceleration_canary_deferred"
                    or cell_records[0].get("technical_retry_idx") is not None
                    or cell_records[0].get("run_id") is not None
                ):
                    raise MultiEventInputError(
                        "canary deferred slot differs from the frozen stage policy"
                    )
        elif any(
            record.get("source") == "acceleration_stage"
            or record.get("reason_code") == "acceleration_canary_deferred"
            for record in records
        ):
            raise MultiEventInputError(
                "full workers2 stage cannot contain canary-deferred outcomes"
            )


def _validate_private_attempt_ledger(
    public_records: Sequence[Mapping[str, Any]],
    private_records: Sequence[Mapping[str, Any]],
) -> None:
    """Require each private record to be an exact public-record projection."""

    public_keys = {
        "schema_version",
        "event_id",
        "arm",
        "seed",
        "repeat_idx",
        "slot_id",
        "source",
        "technical_retry_idx",
        "run_id",
        "status",
        "reason_code",
    }
    cursor = 0
    for raw in private_records:
        record = _mapping(raw, "private driver attempt ledger[]")
        if set(record) != public_keys | {"private"} or not isinstance(
            record.get("private"), Mapping
        ):
            raise MultiEventInputError(
                "private attempt record must be exact public identity plus private mapping"
            )
        projection = {key: record[key] for key in public_keys}
        while cursor < len(public_records) and public_records[cursor] != projection:
            cursor += 1
        if cursor >= len(public_records):
            raise MultiEventInputError(
                "private attempt ledger is not an ordered subset of the public ledger"
            )
        cursor += 1


def _validate_attempt_lock_artifact(child_root: Path) -> None:
    lock_path = child_root / ATTEMPT_COORDINATION_LOCK_NAME
    try:
        info = lock_path.lstat()
    except OSError as error:
        raise MultiEventInputError(
            "attempt coordination lock artifact is missing"
        ) from error
    if (
        lock_path.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise MultiEventInputError(
            "attempt coordination lock must be regular, non-symlink, mode 0600"
        )


def _validate_live_foreign_attempt_cap(
    *,
    child_root: Path,
    jobs: Mapping[tuple[str, str, int, int], Mapping[str, Any]],
    selection: Mapping[str, Any],
    execution_mode: str,
) -> None:
    """Audit same-slot materializations without using them as input selectors."""

    if execution_mode != "openai_live":
        return
    runs_root = child_root / "runs"
    try:
        info = runs_root.lstat()
    except OSError as error:
        raise MultiEventInputError("canonical live runs root is missing") from error
    if runs_root.is_symlink() or not stat.S_ISDIR(info.st_mode):
        raise MultiEventInputError(
            "canonical live runs root must be a non-symlink directory"
        )
    try:
        materialized_names = [entry.name for entry in os.scandir(runs_root)]
    except OSError as error:
        raise MultiEventInputError(
            "canonical live runs root cannot be audited"
        ) from error
    selected_by_cell: dict[
        tuple[str, str, int, int], Mapping[str, Any]
    ] = {}
    for field in ("children", "missing_or_rejected_slots"):
        for raw in _list(selection.get(field), field):
            item = _mapping(raw, f"{field}[]")
            selected_by_cell[_selection_slot(item, f"{field}[]")] = item
    if set(selected_by_cell) != set(jobs):
        raise MultiEventInputError(
            "live attempt-cap audit selection differs from the plan"
        )
    for cell, job in jobs.items():
        slot_id = job["slot"]["slot_id"]
        prefix = f"me-{slot_id}-"
        pattern = re.compile(
            rf"^{re.escape(prefix)}[0-9a-f]{{64}}-ta[1-9][0-9]*$"
        )
        allowed = set(job["allowed_attempt_run_ids"])
        foreign = sorted(
            name
            for name in materialized_names
            if pattern.fullmatch(name) and name not in allowed
        )
        if foreign:
            raise MultiEventInputError(
                "canonical live root contains a foreign-series same-slot attempt"
            )
        materialized = {
            name for name in materialized_names if pattern.fullmatch(name)
        }
        declared = set(selected_by_cell[cell].get("attempt_run_ids", []))
        if materialized != declared:
            raise MultiEventInputError(
                "live same-slot materializations differ from the declared attempt prefix"
            )
        for run_id in materialized:
            attempt_dir = runs_root / run_id
            manifest_path = attempt_dir / DRIVER_MANIFEST_FILENAME
            if (
                attempt_dir.is_symlink()
                or not attempt_dir.is_dir()
                or manifest_path.is_symlink()
                or not manifest_path.is_file()
            ):
                raise MultiEventInputError(
                    "live attempt-prefix materialization is not a regular managed run"
                )
            attempt_manifest = _read_json(
                manifest_path, "live attempt-prefix manifest"
            )
            attempt_managed = _mapping(
                attempt_manifest.get("managed_context"),
                "live attempt-prefix managed_context",
            )
            if (
                attempt_manifest.get("status"),
                attempt_managed.get("state"),
            ) not in {
                ("finished", "FINISHED"),
                ("failed", "FAILED"),
            }:
                raise MultiEventInputError(
                    "live attempt-prefix contains an ACTIVE or indeterminate attempt"
                )


def prepare_driver_selection(
    driver_manifest_path: Path,
    *,
    protocol: Mapping[str, Any],
    protocol_path: Path,
    child_root: Path,
    reference_root: Path,
) -> PreparedSelection:
    """Derive analysis input only from one terminal registered driver parent."""

    lexical_child_root = Path(
        os.path.abspath(os.path.expanduser(str(child_root)))
    )
    child_root_was_symlink = lexical_child_root.is_symlink()
    child_root = lexical_child_root.resolve(strict=True)
    protocol_path = Path(protocol_path).resolve(strict=True)
    profile = _protocol_profile(protocol, protocol_path)
    reference_root = Path(reference_root).resolve(strict=True)
    supplied = Path(driver_manifest_path)
    supplied = supplied if supplied.is_absolute() else child_root / supplied
    if supplied.is_symlink():
        raise MultiEventInputError("driver manifest trust anchor cannot be a symlink")
    try:
        manifest_path = supplied.resolve(strict=True)
        manifest_path.relative_to(child_root)
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as error:
        raise MultiEventInputError("driver manifest is missing or outside child-root") from error
    run_dir = manifest_path.parent
    if (
        manifest_path.name != DRIVER_MANIFEST_FILENAME
        or run_dir.parent.name != "runs"
        or run_dir.parent.parent != child_root
        or run_dir.name == ""
    ):
        raise MultiEventInputError(
            "driver manifest must be child-root/runs/<run_id>/run_manifest.json"
        )
    manifest = _read_json(manifest_path, "driver parent manifest")
    managed = _mapping(manifest.get("managed_context"), "managed_context")
    if (
        manifest.get("run_id") != run_dir.name
        or manifest.get("status") != "finished"
        or manifest.get("managed_run_completed") is not True
        or manifest.get("outputs_complete") is not True
        or manifest.get("failure_stage") is not None
        or managed.get("state") != "FINISHED"
        or managed.get("run_kind") != "experiment_driver"
        or managed.get("command_identity") != DRIVER_COMMAND
        or managed.get("full_validation_completed") is not True
    ):
        raise MultiEventInputError("driver parent is not a terminal multi-event run")
    expected_protocol_hash = protocol_sha256(protocol_path)
    parent_identity = _mapping(
        manifest.get("multi_event_driver"), "multi_event_driver"
    )
    parent_mode = parent_identity.get("execution_mode")
    expected_parent_fields = {
        "schema_version",
        "attempt_series_schema_version",
        "protocol_id",
        "protocol_sha256",
        "execution_mode",
        "protocol_adherence",
        "network_access",
        "network_scope",
        "output_root_policy",
        "source_snapshot",
        "attempt_coordination",
    }
    if profile.execution_acceleration is not None:
        expected_parent_fields.add("execution_acceleration")
    if (
        set(parent_identity)
        != expected_parent_fields
        or parent_identity.get("schema_version") != "1.0"
        or parent_identity.get("attempt_series_schema_version")
        != ATTEMPT_SERIES_SCHEMA_VERSION
        or parent_identity.get("protocol_id") != protocol["protocol_id"]
        or parent_identity.get("protocol_sha256") != expected_protocol_hash
        or parent_mode not in {"mock", "openai_live"}
    ):
        raise MultiEventInputError("driver parent protocol identity mismatch")
    execution_acceleration = _validated_acceleration_stage(
        parent_identity.get("execution_acceleration"),
        profile,
        required=profile.execution_acceleration is not None,
    )
    live = parent_mode == "openai_live"
    canonical_live_root = (
        REPO_ROOT / profile.canonical_live_output_relative_path
    )
    if live and (
        lexical_child_root != canonical_live_root
        or child_root_was_symlink
        or child_root != canonical_live_root
    ):
        raise MultiEventInputError(
            "openai_live parent must use the lexical non-symlink canonical root"
        )
    output_root_policy = _validated_output_root_policy(
        parent_identity.get("output_root_policy"),
        mode=str(parent_mode),
        child_root=child_root,
        profile=profile,
    )
    source_snapshot = _validated_source_snapshot(
        parent_identity.get("source_snapshot"),
        mode=str(parent_mode),
        protocol_path=protocol_path,
        profile=profile,
    )
    expected_coordination = {
        "schema_version": "multi_event_attempt_lock_v1",
        "path": ATTEMPT_COORDINATION_LOCK_NAME,
        "scope": "entire_output_root_all_attempt_series",
        "child_descriptor_inheritance": True,
        "stale_recovery": "kernel_release_after_last_inherited_fd_closes",
        "live_slot_cap_guard": (
            "all preserved canonical me-{slot_id}-*-ta* materializations; "
            "foreign series fail closed"
        ),
    }
    if (
        parent_identity.get("network_access") is not live
        or parent_identity.get("network_scope")
        != (
            "connectivity_probe_only_children_own_provider_calls"
            if live
            else "none"
        )
        or parent_identity.get("attempt_coordination")
        != expected_coordination
    ):
        raise MultiEventInputError(
            "driver parent network/attempt coordination identity mismatch"
        )
    _validate_attempt_lock_artifact(child_root)
    parent_llm = _mapping(manifest.get("llm"), "driver parent llm")
    parent_runtime = _mapping(
        parent_llm.get("runtime"), "driver parent llm.runtime"
    )
    expected_network_scope = (
        "connectivity_probe_only; LLM provider calls occur in managed children"
        if live
        else "none; mock children perform no network access"
    )
    if (
        parent_llm.get("mode")
        != ("connectivity_probe_only" if live else "offline_driver")
        or parent_llm.get("resolved_provider") != "none"
        or parent_llm.get("resolved_model") != "none"
        or parent_llm.get("cache_enabled") is not False
        or set(parent_runtime)
        != {
            "network_access",
            "network_scope",
            "provider_calls_owned_by",
            "provider_calls",
            "provider_calls_succeeded",
            "provider_calls_failed",
            "response_sources",
        }
        or parent_runtime.get("network_access") is not live
        or parent_runtime.get("network_scope") != expected_network_scope
        or parent_runtime.get("provider_calls_owned_by") != "managed_children"
        or any(
            parent_runtime.get(field) != 0
            for field in (
                "provider_calls",
                "provider_calls_succeeded",
                "provider_calls_failed",
            )
        )
        or parent_runtime.get("response_sources")
        != {"provider": 0, "cache": 0, "replay": 0}
    ):
        raise MultiEventInputError("driver parent LLM network metadata mismatch")
    if live:
        parent_git = _mapping(manifest.get("git"), "driver parent git")
        if (
            parent_git.get("commit") != source_snapshot["head_commit"]
            or parent_git.get("dirty") is not False
            or manifest.get("scientific_component_fingerprint")
            != source_snapshot["scientific_component_fingerprint"]
        ):
            raise MultiEventInputError(
                "driver parent source identity differs from source_snapshot"
            )
    expected_inputs: dict[str, tuple[Path, str]] = {
        "protocol": (protocol_path, expected_protocol_hash),
        "catalog": (
            _resolve_explicit(
                reference_root,
                protocol["reference_data_catalog"]["path"],
                "protocol reference-data catalog",
            ),
            protocol["reference_data_catalog"]["sha256"],
        ),
    }
    for index, event in enumerate(protocol["design"]["events"]):
        expected_inputs[f"reference_{index:02d}"] = (
            _resolve_explicit(
                reference_root,
                event["reference_csv"],
                "protocol event reference",
            ),
            event["reference_csv_sha256"],
        )
        expected_inputs[f"timeline_{index:02d}"] = (
            _resolve_explicit(
                reference_root,
                event["news_timeline"],
                "protocol event timeline",
            ),
            event["news_timeline_sha256"],
        )
    parent_inputs: dict[str, Mapping[str, Any]] = {}
    for raw in _list(manifest.get("inputs"), "driver manifest inputs"):
        item = _mapping(raw, "driver manifest inputs[]")
        label = _text(item.get("label"), "driver manifest input label")
        if label in parent_inputs:
            raise MultiEventInputError("driver manifest input label is duplicated")
        parent_inputs[label] = item
    if set(parent_inputs) != set(expected_inputs):
        raise MultiEventInputError("driver parent scientific input labels mismatch")
    for label, (expected_path, expected_hash) in expected_inputs.items():
        item = parent_inputs[label]
        try:
            listed_path = Path(_text(item.get("path"), "driver input path")).resolve(
                strict=True
            )
        except (FileNotFoundError, OSError, RuntimeError, ValueError) as error:
            raise MultiEventInputError(
                "driver parent scientific input path is invalid"
            ) from error
        if (
            listed_path != expected_path
            or item.get("exists") is not True
            or item.get("kind") != "file"
            or item.get("error") is not None
            or item.get("sha256") != expected_hash
            or sha256_file(listed_path) != expected_hash
        ):
            raise MultiEventInputError(
                "driver parent scientific input identity mismatch"
            )

    artifacts = _registered_driver_artifacts(manifest, run_dir)
    selection_path = artifacts[DRIVER_SELECTION_FILENAME]
    plan = _read_json(artifacts[DRIVER_PLAN_FILENAME], "driver plan")
    selection = _read_json(selection_path, "driver selection")
    summary = _read_json(artifacts[DRIVER_SUMMARY_FILENAME], "driver summary")
    if (
        plan.get("output_root_policy") != output_root_policy
        or plan.get("source_snapshot") != source_snapshot
    ):
        raise MultiEventInputError(
            "driver parent and plan root/source policies disagree"
        )
    technical_ledger = _mapping(
        manifest.get("technical_attempt_ledger"),
        "technical_attempt_ledger",
    )
    expected_technical_ledger_fields = {
            "schema_version",
            "durability",
            "public_path",
            "private_path",
            "max_child_attempts_per_series",
            "public_records",
            "private_records",
    }
    if execution_acceleration is not None:
        expected_technical_ledger_fields.add(
            "stage_max_child_attempts_per_slot"
        )
    if (
        set(technical_ledger) != expected_technical_ledger_fields
        or technical_ledger.get("schema_version") != "1.0"
        or technical_ledger.get("durability")
        != "append_flush_fsync_per_record"
        or technical_ledger.get("public_path")
        != DRIVER_ATTEMPT_LEDGER_FILENAME
        or technical_ledger.get("private_path")
        != DRIVER_PRIVATE_ATTEMPT_LEDGER_FILENAME
        or technical_ledger.get("max_child_attempts_per_series")
        != protocol["acceptance_and_execution"]["max_child_attempts"]
        or isinstance(technical_ledger.get("public_records"), bool)
        or not isinstance(technical_ledger.get("public_records"), int)
        or technical_ledger.get("public_records") < 0
        or isinstance(technical_ledger.get("private_records"), bool)
        or not isinstance(technical_ledger.get("private_records"), int)
        or technical_ledger.get("private_records") < 0
        or (
            execution_acceleration is not None
            and technical_ledger.get(
                "stage_max_child_attempts_per_slot"
            )
            != execution_acceleration[
                "max_child_attempts_per_slot"
            ]
        )
    ):
        raise MultiEventInputError(
            "driver parent technical-attempt ledger contract mismatch"
        )
    execution_plan = _validated_execution_plan(
        selection.get("execution_plan"), protocol, profile
    )
    if (
        parent_identity.get("execution_mode") != execution_plan["execution_mode"]
        or parent_identity.get("protocol_adherence")
        is not execution_plan["protocol_adherence"]
    ):
        raise MultiEventInputError("driver parent execution identity mismatch")
    _validate_selection_partition(
        _list(selection.get("children"), "children"),
        _list(selection.get("missing_or_rejected_slots"), "missing_or_rejected_slots"),
        protocol,
        execution_plan,
    )
    prepared = prepare_selection(
        selection_path,
        protocol=protocol,
        protocol_path=protocol_path,
        child_root=child_root,
        reference_root=reference_root,
    )
    if prepared.execution_acceleration != execution_acceleration:
        raise MultiEventInputError(
            "parent and selection execution_acceleration disagree"
        )
    jobs, planned, expected_identities = _driver_plan_cells(
        plan,
        protocol=protocol,
        protocol_path=protocol_path,
        execution_plan=execution_plan,
        events=prepared.events,
        child_root=child_root,
        profile=profile,
        execution_acceleration=execution_acceleration,
    )
    _validate_live_foreign_attempt_cap(
        child_root=child_root,
        jobs=jobs,
        selection=selection,
        execution_mode=execution_plan["execution_mode"],
    )
    first_expected = expected_identities[next(iter(jobs))]
    expected_study_identity = {
        "model_request_config_hash": first_expected.model_request_config_hash,
        "requested_provider": first_expected.requested_provider,
        "requested_model": first_expected.requested_model,
        "resolved_provider": first_expected.resolved_provider,
        "resolved_model": first_expected.resolved_model,
        "endpoint_identity": first_expected.endpoint_identity,
        "scientific_runtime_environment": (
            first_expected.scientific_runtime_environment
        ),
        "scientific_runtime_environment_identity": (
            first_expected.scientific_runtime_environment_identity
        ),
        "invalid_reported_model_alias_count": 0,
    }
    study_identity = _mapping(
        selection.get("study_model_identity"), "study_model_identity"
    )
    if any(
        study_identity.get(field) != value
        for field, value in expected_study_identity.items()
    ):
        raise MultiEventInputError(
            "selection study identity differs from canonical plan identity"
        )
    ledger = _read_jsonl_objects(
        artifacts[DRIVER_ATTEMPT_LEDGER_FILENAME], "driver attempt ledger"
    )
    private_ledger = _read_jsonl_objects(
        artifacts[DRIVER_PRIVATE_ATTEMPT_LEDGER_FILENAME],
        "private driver attempt ledger",
    )
    _read_jsonl_objects(
        artifacts[DRIVER_PRIVATE_FAILURES_FILENAME],
        "private driver failures",
    )
    if (
        technical_ledger.get("public_records") != len(ledger)
        or technical_ledger.get("private_records") != len(private_ledger)
    ):
        raise MultiEventInputError(
            "technical-attempt ledger declared counts disagree with files"
        )
    _validate_private_attempt_ledger(ledger, private_ledger)
    _validate_driver_attempt_ledger(
        ledger,
        jobs=jobs,
        selection=selection,
        execution_acceleration=execution_acceleration,
    )

    accepted_count = len(selection["children"])
    failed_count = len(selection["missing_or_rejected_slots"])
    accepted_cells = {
        _selection_slot(item, "children[]") for item in selection["children"]
    }
    complete_pairs_by_event: dict[str, int] = {}
    for event_id in _planned_values(protocol)[0]:
        complete_pairs_by_event[event_id] = sum(
            1
            for seed in execution_plan["seeds"]
            if {
                (event_id, arm, seed, repeat_idx)
                for arm in ARMS
                for repeat_idx in execution_plan["repeat_indices"]
            }
            <= accepted_cells
        )
    complete_pairs = sum(complete_pairs_by_event.values())
    reported_aliases = study_identity.get("reported_model_aliases")
    homogeneous_alias_pooling = bool(
        execution_plan["execution_mode"] == "openai_live"
        and isinstance(reported_aliases, list)
        and len(reported_aliases) == 1
    )
    if (
        summary.get("schema_version") != "1.0"
        or summary.get("run_id") != manifest["run_id"]
        or summary.get("driver") != DRIVER_COMMAND
        or summary.get("planned_runs") != len(planned)
        or summary.get("completed_runs") != accepted_count
        or summary.get("failed_runs") != failed_count
        or summary.get("honest_n_runs") != accepted_count
        or summary.get("multi_event_protocol_sha256") != expected_protocol_hash
        or summary.get("attempt_series_schema_version")
        != ATTEMPT_SERIES_SCHEMA_VERSION
        or summary.get("multi_event_plan") != DRIVER_PLAN_FILENAME
        or summary.get("multi_event_selection") != DRIVER_SELECTION_FILENAME
        or summary.get("multi_event_public_attempt_ledger")
        != DRIVER_ATTEMPT_LEDGER_FILENAME
        or summary.get("selection_accepted_children") != accepted_count
        or summary.get("selection_rejected_or_missing_slots") != failed_count
        or summary.get("honest_n_complete_seed_pairs") != complete_pairs
        or summary.get("honest_n_complete_seed_pairs_by_event")
        != complete_pairs_by_event
        or summary.get("incomplete") is not bool(failed_count)
        or summary.get("reported_model_aliases") != reported_aliases
        or summary.get("invalid_reported_model_alias_count") != 0
        or summary.get("underlying_model_identity_verified") is not False
        or summary.get("model_specific_inference_allowed") is not False
        or summary.get("reported_alias_homogeneous_pooling_allowed")
        is not homogeneous_alias_pooling
        or summary.get("pooling_scope")
        != (
            "single_endpoint_reported_alias_not_underlying_model_proof"
            if homogeneous_alias_pooling
            else "endpoint_mixture_or_mock_not_model_specific"
        )
        or (
            profile.execution_acceleration is None
            and "execution_acceleration" in summary
        )
        or (
            profile.execution_acceleration is not None
            and summary.get("execution_acceleration")
            != execution_acceleration
        )
    ):
        raise MultiEventInputError("driver summary disagrees with registered selection")
    experiment_completion = _mapping(
        manifest.get("experiment_completion"), "experiment_completion"
    )
    completion_fields = (
        "planned_runs",
        "started_runs",
        "completed_runs",
        "failed_runs",
        "honest_n_runs",
    )
    if any(
        experiment_completion.get(field) != summary.get(field)
        for field in completion_fields
    ):
        raise MultiEventInputError(
            "driver manifest completion disagrees with selection"
        )
    anchored_inputs = dict(prepared.input_paths)
    anchored_inputs.update(
        {
            "driver_parent_manifest": manifest_path,
            "driver_plan": artifacts[DRIVER_PLAN_FILENAME],
            "driver_attempt_ledger": artifacts[DRIVER_ATTEMPT_LEDGER_FILENAME],
            "driver_private_attempt_ledger": artifacts[
                DRIVER_PRIVATE_ATTEMPT_LEDGER_FILENAME
            ],
            "driver_summary": artifacts[DRIVER_SUMMARY_FILENAME],
            "driver_private_failures": artifacts[
                DRIVER_PRIVATE_FAILURES_FILENAME
            ],
        }
    )
    return replace(
        prepared,
        input_paths=anchored_inputs,
        driver_manifest_path=manifest_path,
        driver_run_id=str(manifest["run_id"]),
        output_root_policy=output_root_policy,
        source_snapshot=source_snapshot,
        execution_acceleration=execution_acceleration,
    )


def _child_actual_identity(child: Any) -> Mapping[str, Any]:
    return {
        "run_id": child.run_id,
        "command_identity": child.command_identity,
        "config_hash_schema_version": child.config_hash_schema_version,
        "scientific_config_hash": child.scientific_config_hash,
        "model_request_config_hash": child.model_request_config_hash,
        "scientific_input_identity": child.scientific_input_identity,
        "scenario_definition_hash": child.scenario_definition_hash,
        "population_identity": child.population_identity,
        "requested_provider": child.requested_provider,
        "requested_model": child.requested_model,
        "resolved_provider": child.resolved_provider,
        "resolved_model": child.resolved_model,
        "endpoint_identity": child.endpoint_identity,
        "invalid_reported_model_alias_count": (
            child.invalid_reported_model_alias_count
        ),
        "scientific_runtime_environment": (
            child.scientific_runtime_environment
        ),
        "scientific_runtime_environment_identity": (
            child.scientific_runtime_environment_identity
        ),
    }


def _config_mismatches(
    manifest: Mapping[str, Any],
    protocol: Mapping[str, Any],
    selection: ChildSelection,
    event: EventInput,
    *,
    execution_mode: str,
) -> list[str]:
    config = manifest.get("config")
    if not isinstance(config, Mapping):
        return ["manifest_config_missing"]
    scientific = protocol["effective_config_freeze"]["scientific"]
    mismatches: list[str] = []
    factors = {
        "seed": selection.seed,
        "social_enabled": selection.arm == "social_on",
    }
    expected_timeline = [
        {
            "event_id": item.event_id,
            "round": item.round,
            "public_text": item.public_text,
        }
        for item in event.material.transformed.news_timeline
    ]
    for field, expected in scientific.items():
        wanted = factors.get(field, expected)
        if field == "news_timeline":
            wanted = expected_timeline
        elif field == "reference_path":
            wanted = str(event.reference_csv)
        actual = config.get(field)
        if field == "population":
            # The manifest writer canonicalizes mapping keys.  Executable
            # insertion order is bound independently by population_identity's
            # effective_cast and the canonical ExpectedRunIdentity gate.
            if not isinstance(actual, Mapping) or actual != wanted:
                mismatches.append(f"config_mismatch:{field}")
        elif actual != wanted:
            mismatches.append(f"config_mismatch:{field}")
    model = protocol["effective_config_freeze"]["model_request"]
    strict_model_fields = [
        "temperature",
        "max_tokens",
        "cache_enabled",
        "provider_sdk_max_retries",
    ]
    if execution_mode == "openai_live":
        strict_model_fields.extend(
            [
                "provider", "model", "cheap_model", "use_cheap_model",
                "openai_base_url", "openai_model",
            ]
        )
    elif config.get("provider") != "mock":
        mismatches.append("config_mismatch:provider")
    for field in strict_model_fields:
        if config.get(field) != model[field]:
            mismatches.append(f"config_mismatch:{field}")
    return mismatches


def expected_population_identity(protocol: Mapping[str, Any]) -> str:
    """Recompute population identity from the frozen executable cast."""

    scientific = protocol["effective_config_freeze"]["scientific"]
    cfg = Config()
    for field in (
        "max_llm_agents",
        "n_llm_agents",
        "n_noise_agents",
        "population",
    ):
        setattr(cfg, field, scientific[field])
    summary = build_effective_config_contract(cfg)["scientific_config_summary"]
    payload = {
        field: summary[field]
        for field in (
            "max_llm_agents",
            "n_llm_agents",
            "n_noise_agents",
            "population",
        )
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _reported_model_aliases_from_manifest(
    manifest: Mapping[str, Any], *, execution_mode: str
) -> list[str]:
    """Recompute public reported-model evidence from application attempts."""

    completion = manifest.get("completion")
    attempts = (
        completion.get("application_provider_attempts")
        if isinstance(completion, Mapping)
        else None
    )
    if execution_mode == "mock" and attempts is None:
        return []
    if not isinstance(attempts, Mapping):
        raise MultiEventInputError("application_provider_attempts evidence is missing")
    if attempts.get("reported_models_truncated") is not False:
        raise MultiEventInputError("application_provider_attempts evidence is truncated")
    invalid_count = attempts.get("invalid_reported_model_alias_count")
    if (
        isinstance(invalid_count, bool)
        or not isinstance(invalid_count, int)
        or invalid_count != 0
    ):
        raise MultiEventInputError(
            "application_provider_attempts contains invalid alias evidence"
        )
    reported = attempts.get("reported_models")
    if not isinstance(reported, list) or any(
        safe_reported_model(model) != model for model in reported
    ):
        raise MultiEventInputError("application_provider_attempts.reported_models is invalid")
    aliases = sorted(set(reported))
    if execution_mode == "mock" and aliases:
        raise MultiEventInputError("mock attempts cannot report endpoint model aliases")
    return aliases


def validate_reported_model_alias_binding(
    manifest: Mapping[str, Any],
    *,
    selected_aliases: Sequence[str],
    result_aliases: Sequence[str],
    result_invalid_alias_count: Any,
    execution_mode: str,
) -> list[str]:
    """Bind manifest attempt evidence, selection identity, and result projection."""

    manifest_aliases = _reported_model_aliases_from_manifest(
        manifest, execution_mode=execution_mode
    )
    selected = list(selected_aliases)
    result = list(result_aliases)
    if (
        manifest_aliases != selected
        or result != selected
        or isinstance(result_invalid_alias_count, bool)
        or not isinstance(result_invalid_alias_count, int)
        or result_invalid_alias_count != 0
    ):
        raise MultiEventInputError(
            "reported model aliases disagree across attempts, selection, and result"
        )
    return manifest_aliases


def model_identity_interpretation(
    study_model_identity: Mapping[str, Any]
) -> Mapping[str, Any]:
    aliases = list(study_model_identity.get("reported_model_aliases", []))
    mode = study_model_identity.get("execution_mode")
    homogeneous_reported_alias = bool(
        mode == "openai_live" and len(aliases) == 1
    )
    return {
        "reported_model_aliases": aliases,
        "invalid_reported_model_alias_count": study_model_identity.get(
            "invalid_reported_model_alias_count"
        ),
        "scientific_runtime_environment": study_model_identity.get(
            "scientific_runtime_environment"
        ),
        "scientific_runtime_environment_identity": study_model_identity.get(
            "scientific_runtime_environment_identity"
        ),
        "underlying_model_identity_verified": False,
        "model_specific_inference_allowed": False,
        "reported_alias_homogeneous_pooling_allowed": homogeneous_reported_alias,
        "attribution_scope": (
            "endpoint_condition_with_homogeneous_self_reported_alias"
            if homogeneous_reported_alias
            else "endpoint_alias_mixture_or_mock_descriptive_only"
        ),
        "policy": (
            "reported_model is endpoint self-report and never verifies underlying "
            "weights; homogeneous live aliases permit only alias-stratified endpoint "
            "pooling, while mixed, absent, or mock aliases prohibit that pooling"
        ),
        "provider_retry_semantics": {
            "provider_sdk_max_retries": 0,
            "observable_attempt_scope": "visible_adapter_loop_attempts_only",
            "unobservable_scope": "transport_proxy_and_server_internal_behavior",
        },
    }


def _trajectory_metrics(
    norm_log_path: Sequence[float], event: EventInput
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Recompute supplementary distances through existing validation functions."""

    if not norm_log_path:
        return None, None, None
    simulated_prices = [math.exp(value) for value in norm_log_path]
    transformed_prices = [
        math.exp(value) for value in event.transformed_reference_log_path
    ]
    rmse, _horizon = V.logprice_rmse(
        simulated_prices,
        0,
        transformed_prices,
        0,
    )
    raw_reference_log = V.norm_log_path(
        list(event.reference_prices), event.reference_shock_idx
    )
    _dtw, dtw_norm = V.dtw_distance(
        list(norm_log_path), list(event.transformed_reference_log_path)
    )
    _raw_dtw, raw_dtw_norm = V.dtw_distance(
        list(norm_log_path), raw_reference_log
    )
    return (
        float(rmse) if math.isfinite(rmse) else None,
        float(dtw_norm) if math.isfinite(dtw_norm) else None,
        float(raw_dtw_norm) if math.isfinite(raw_dtw_norm) else None,
    )


def recompute_drop_depth_from_path(
    norm_log_path: Sequence[float],
    *,
    reported_rounded: Optional[float] = None,
    compatibility_tolerance: float = 5.1e-5,
) -> float:
    """Return continuous path depth and audit the rounded compatibility field."""

    if (
        len(norm_log_path) != 25
        or abs(float(norm_log_path[0])) > 1e-9
        or not all(math.isfinite(float(value)) for value in norm_log_path)
    ):
        raise MultiEventInputError("norm_log_path must be finite t0..t24 with t0=0")
    recomputed = min(math.exp(float(value)) for value in norm_log_path) - 1.0
    if reported_rounded is not None:
        reported = _finite(reported_rounded, "metrics.drop_depth")
        if abs(recomputed - reported) > compatibility_tolerance:
            raise MultiEventInputError(
                "metrics.drop_depth differs from independently recomputed path depth"
            )
    return recomputed


def _rejection(selection: ChildSelection, reasons: Iterable[str]) -> Mapping[str, Any]:
    public_reasons = [
        _validated_public_reason_code(reason, "analysis rejection reason_code")
        for reason in dict.fromkeys(reasons)
    ]
    return {
        "event_id": selection.event_id,
        "arm": selection.arm,
        "seed": selection.seed,
        "repeat_idx": selection.repeat_idx,
        "run_id": selection.expected_identity.get("run_id"),
        "reason_codes": public_reasons,
    }


def _validated_application_attempt_evidence(
    manifest: Mapping[str, Any], events_path: Path, *, execution_mode: str
) -> Mapping[str, Any]:
    """Recompute public decision health and visible adapter-attempt evidence."""

    completion = _mapping(manifest.get("completion"), "completion")
    attempts = _mapping(
        completion.get("application_provider_attempts"),
        "completion.application_provider_attempts",
    )
    expected_keys = {
        "unit",
        "attempted",
        "responses_received",
        "parse_failed_responses",
        "provider_exceptions",
        "retries_scheduled",
        "logical_requests_with_retry",
        "exhausted_logical_requests",
        "reported_models",
        "reported_models_truncated",
        "invalid_reported_model_alias_count",
        "coverage",
    }
    count_fields = {
        "attempted",
        "responses_received",
        "parse_failed_responses",
        "provider_exceptions",
        "retries_scheduled",
        "logical_requests_with_retry",
        "exhausted_logical_requests",
        "invalid_reported_model_alias_count",
    }
    aliases = _validated_reported_aliases(
        attempts.get("reported_models"),
        "completion.application_provider_attempts.reported_models",
    )
    if (
        set(attempts) != expected_keys
        or attempts.get("unit") != "visible_adapter_loop_attempts"
        or attempts.get("coverage")
        != (
            "OpenAI/Anthropic application retry loops only; excludes SDK, "
            "transport, proxy, and server-internal retries"
        )
        or attempts.get("reported_models_truncated") is not False
        or any(
            isinstance(attempts.get(field), bool)
            or not isinstance(attempts.get(field), int)
            or attempts.get(field) < 0
            for field in count_fields
        )
        or attempts.get("invalid_reported_model_alias_count") != 0
        or (execution_mode == "mock" and aliases)
        or (execution_mode == "openai_live" and len(aliases) != 1)
    ):
        raise MultiEventInputError(
            "application provider-attempt evidence has an invalid exact schema"
        )

    observed = {field: 0 for field in count_fields}
    observed_aliases: set[str] = set()
    terminal_counts = {field: 0 for field in HEALTH_TERMINAL_BUCKETS}
    provider_exhaustion_counts = {
        PROVIDER_EXCEPTION_EXHAUSTED: 0,
        PROVIDER_PARSE_EXHAUSTED: 0,
    }
    expected_run_id = _text(manifest.get("run_id"), "child manifest run_id")
    expected_direction = "side" if execution_mode == "mock" else "action"
    decision_data_keys = {
        "persona_id",
        "action",
        "quantity",
        "limit_price",
        "sentiment",
        "public_take",
        "parse_status",
        "decision_response_schema",
        "decision_response_direction_field",
        "strict_schema_valid",
        "strict_schema_error_code",
        "terminal_status",
        "raw_response_sha256",
    }
    provider_data_keys = {
        "provider_attempt_schema",
        "logical_sequence",
        "batch_sequence",
        "batch_index",
        "batch_size",
        "round",
        "agent",
        "persona",
        "attempt_index",
        "max_attempts",
        "provider",
        "model",
        "reported_model",
        "reported_model_alias_invalid",
        "original_prompt_hash",
        "attempted_prompt_hash",
        "trigger",
        "outcome",
        "response_hash",
        "latency_ms",
        "prompt_tokens",
        "completion_tokens",
        "will_retry",
    }
    provider_sequences: dict[int, list[Mapping[str, Any]]] = {}
    decision_cells: set[tuple[int, str]] = set()
    for record in _read_jsonl_objects(events_path, "child public events"):
        event_type = record.get("type")
        if event_type not in {
            "AgentDecisionParsed",
            "LLMProviderAttemptObserved",
        }:
            continue
        if (
            record.get("schema_version") != "1.0"
            or record.get("run_id") != expected_run_id
        ):
            raise MultiEventInputError(
                "public machine-evidence event has an invalid run envelope"
            )
        event_round = _integer(record.get("round"), "public event round")
        event_agent = _text(record.get("agent_id"), "public event agent_id")
        if event_round < 1:
            raise MultiEventInputError("public event round must be positive")
        data = _mapping(record.get("data"), "provider-attempt event data")

        if event_type == "AgentDecisionParsed":
            decision_cell = (event_round, event_agent)
            if decision_cell in decision_cells:
                raise MultiEventInputError(
                    "AgentDecisionParsed repeats one round/agent decision cell"
                )
            decision_cells.add(decision_cell)
            if set(data) != decision_data_keys:
                raise MultiEventInputError(
                    "AgentDecisionParsed does not have the exact public schema"
                )
            terminal_status = data.get("terminal_status")
            strict_valid = data.get("strict_schema_valid")
            strict_error = data.get("strict_schema_error_code")
            parse_status = data.get("parse_status")
            if (
                data.get("decision_response_schema")
                != MULTI_EVENT_DECISION_RESPONSE_SCHEMA
                or data.get("decision_response_direction_field")
                != expected_direction
                or _text(data.get("persona_id"), "decision persona_id") == ""
                or _sha256(
                    data.get("raw_response_sha256"),
                    "decision raw_response_sha256",
                )
                == ""
                or terminal_status == LEGACY_PARSE_INVALID
                or terminal_status
                not in {
                    DECISION_VALID,
                    STRICT_SCHEMA_INVALID,
                    PROVIDER_EXCEPTION_EXHAUSTED,
                    PROVIDER_PARSE_EXHAUSTED,
                }
            ):
                raise MultiEventInputError(
                    "AgentDecisionParsed strict-schema identity is inconsistent"
                )
            if terminal_status == DECISION_VALID:
                machine_relation_valid = (
                    strict_valid is True
                    and strict_error is None
                    and parse_status == "parsed"
                )
            else:
                machine_relation_valid = (
                    strict_valid is False
                    and strict_error in STRICT_DECISION_ERROR_CODES
                    and parse_status == "error"
                )
                if terminal_status in {
                    PROVIDER_EXCEPTION_EXHAUSTED,
                    PROVIDER_PARSE_EXHAUSTED,
                }:
                    machine_relation_valid = (
                        machine_relation_valid
                        and strict_error == "missing_required_field"
                    )
            if not machine_relation_valid:
                raise MultiEventInputError(
                    "AgentDecisionParsed terminal, strict-valid, and parse fields disagree"
                )
            bucket = (
                "valid_decisions"
                if terminal_status == DECISION_VALID
                else terminal_status
            )
            terminal_counts[bucket] += 1
            continue

        if execution_mode != "openai_live":
            raise MultiEventInputError(
                "mock child has a public Provider-attempt event"
            )
        if (
            set(data) != provider_data_keys
            or data.get("provider_attempt_schema") != PROVIDER_ATTEMPT_SCHEMA
        ):
            raise MultiEventInputError(
                "provider-attempt event schema is unsupported"
            )
        logical_sequence = _integer(
            data.get("logical_sequence"),
            "provider-attempt event logical_sequence",
        )
        batch_sequence = _integer(
            data.get("batch_sequence"),
            "provider-attempt event batch_sequence",
        )
        batch_index = _integer(
            data.get("batch_index"), "provider-attempt event batch_index"
        )
        batch_size = _integer(
            data.get("batch_size"), "provider-attempt event batch_size"
        )
        attempt_index = _integer(
            data.get("attempt_index"), "provider-attempt event attempt_index"
        )
        max_attempts = _integer(
            data.get("max_attempts"), "provider-attempt event max_attempts"
        )
        if (
            logical_sequence < 1
            or batch_sequence < 1
            or batch_size < 1
            or not 0 <= batch_index < batch_size
            or not 1 <= attempt_index <= max_attempts
            or max_attempts != 3
            or data.get("round") != event_round
            or data.get("agent") != event_agent
            or not isinstance(data.get("persona"), str)
            or not data.get("persona")
            or data.get("provider") != "openai"
            or not isinstance(data.get("model"), str)
            or not data.get("model")
        ):
            raise MultiEventInputError(
                "provider-attempt event context is inconsistent"
            )
        _sha256(
            data.get("original_prompt_hash"),
            "provider-attempt original_prompt_hash",
        )
        _sha256(
            data.get("attempted_prompt_hash"),
            "provider-attempt attempted_prompt_hash",
        )
        latency_ms = _finite(
            data.get("latency_ms"), "provider-attempt latency_ms"
        )
        if latency_ms < 0:
            raise MultiEventInputError(
                "provider-attempt latency_ms must be nonnegative"
            )
        for token_field in ("prompt_tokens", "completion_tokens"):
            token_count = data.get(token_field)
            if token_count is not None and (
                isinstance(token_count, bool)
                or not isinstance(token_count, int)
                or token_count < 0
            ):
                raise MultiEventInputError(
                    f"provider-attempt {token_field} must be null or nonnegative integer"
                )
        trigger = data.get("trigger")
        if trigger not in {"initial", "parse_failure", "provider_exception"}:
            raise MultiEventInputError(
                "provider-attempt event trigger is unsupported"
            )
        observed["attempted"] += 1
        outcome = data.get("outcome")
        if outcome in {"response_parseable", "response_parse_failed"}:
            observed["responses_received"] += 1
            _sha256(
                data.get("response_hash"),
                "provider-attempt response_hash",
            )
        if outcome == "response_parse_failed":
            observed["parse_failed_responses"] += 1
        elif outcome == "provider_exception":
            observed["provider_exceptions"] += 1
            if any(
                data.get(field) is not None
                for field in (
                    "response_hash",
                    "prompt_tokens",
                    "completion_tokens",
                    "reported_model",
                )
            ):
                raise MultiEventInputError(
                    "provider-exception event exposes impossible response evidence"
                )
        elif outcome != "response_parseable":
            raise MultiEventInputError(
                "provider-attempt event has an unsupported outcome"
            )
        will_retry = data.get("will_retry")
        if not isinstance(will_retry, bool):
            raise MultiEventInputError(
                "provider-attempt event will_retry must be boolean"
            )
        if will_retry:
            observed["retries_scheduled"] += 1
        alias = data.get("reported_model")
        invalid_alias = data.get("reported_model_alias_invalid")
        if not isinstance(invalid_alias, bool):
            raise MultiEventInputError(
                "provider-attempt invalid-alias flag must be boolean"
            )
        if invalid_alias or (alias is not None and safe_reported_model(alias) is None):
            observed["invalid_reported_model_alias_count"] += 1
        if alias is not None:
            if safe_reported_model(alias) != alias:
                if not invalid_alias:
                    raise MultiEventInputError(
                        "unsafe provider-attempt alias lacks invalid evidence"
                    )
            else:
                observed_aliases.add(alias)
        if invalid_alias and alias is not None:
            raise MultiEventInputError(
                "invalid provider alias must be redacted from public evidence"
            )
        provider_sequences.setdefault(logical_sequence, []).append(data)

    if execution_mode == "openai_live" and set(provider_sequences) != set(
        range(1, len(provider_sequences) + 1)
    ):
        raise MultiEventInputError(
            "provider-attempt logical sequences are not a contiguous run prefix"
        )
    stable_context_fields = (
        "batch_sequence",
        "batch_index",
        "batch_size",
        "round",
        "agent",
        "persona",
        "original_prompt_hash",
        "provider",
        "model",
        "max_attempts",
    )
    provider_cells: set[tuple[int, str]] = set()
    for logical_sequence, sequence in provider_sequences.items():
        attempt_indices = [item.get("attempt_index") for item in sequence]
        if attempt_indices != list(range(1, len(sequence) + 1)):
            raise MultiEventInputError(
                "provider-attempt indices are not contiguous within a logical request"
            )
        first = sequence[0]
        provider_cells.add((int(first["round"]), str(first["agent"])))
        if any(
            any(item.get(field) != first.get(field) for field in stable_context_fields)
            for item in sequence[1:]
        ):
            raise MultiEventInputError(
                "provider-attempt context changes within a logical request"
            )
        if sequence[0].get("trigger") != "initial":
            raise MultiEventInputError(
                "provider-attempt sequence must begin with the initial trigger"
            )
        for previous, current in zip(sequence, sequence[1:]):
            expected_trigger = {
                "response_parse_failed": "parse_failure",
                "provider_exception": "provider_exception",
            }.get(previous.get("outcome"))
            if (
                previous.get("will_retry") is not True
                or expected_trigger is None
                or current.get("trigger") != expected_trigger
            ):
                raise MultiEventInputError(
                    "provider-attempt retry transition is inconsistent"
                )
        final = sequence[-1]
        if final.get("will_retry") is not False:
            raise MultiEventInputError(
                "provider-attempt sequence lacks one non-retrying terminal event"
            )
        if any(item.get("outcome") == "response_parseable" for item in sequence[:-1]):
            raise MultiEventInputError(
                "provider-attempt sequence continues after a parseable response"
            )
        observed["logical_requests_with_retry"] += int(len(sequence) >= 2)
        final_outcome = final.get("outcome")
        if final_outcome in {"response_parse_failed", "provider_exception"}:
            if len(sequence) != 3:
                raise MultiEventInputError(
                    "provider-attempt failure exhausted before max_attempts"
                )
            observed["exhausted_logical_requests"] += 1
            bucket = (
                PROVIDER_PARSE_EXHAUSTED
                if final_outcome == "response_parse_failed"
                else PROVIDER_EXCEPTION_EXHAUSTED
            )
            provider_exhaustion_counts[bucket] += 1

    if any(attempts[field] != observed[field] for field in count_fields) or aliases != sorted(
        observed_aliases
    ):
        raise MultiEventInputError(
            "application provider-attempt completion disagrees with public events"
        )
    decision_count = sum(terminal_counts.values())
    if execution_mode == "openai_live" and (
        len(provider_sequences) != decision_count
        or provider_cells != decision_cells
    ):
        raise MultiEventInputError(
            "visible Provider logical requests do not close against public decision cells"
        )
    return {
        "application_provider_attempts": dict(attempts),
        "terminal_status_counts": terminal_counts,
        "provider_exhaustion_counts": provider_exhaustion_counts,
    }


def _validated_multi_event_health(
    result: Mapping[str, Any],
    manifest: Mapping[str, Any],
    *,
    terminal_status_counts: Mapping[str, Any],
    provider_exhaustion_counts: Mapping[str, Any],
) -> float:
    health = _mapping(result.get("health"), "health")
    if set(health) != {
        "bad_orders",
        "total_llm_orders",
        "bad_frac",
        "schema_version",
        "decision_response_schema",
        "failure_union",
        "failure_union_counts",
    }:
        raise MultiEventInputError("health does not have the exact multi-event schema")
    bad_orders = _integer(health.get("bad_orders"), "health.bad_orders")
    total_orders = _integer(
        health.get("total_llm_orders"), "health.total_llm_orders"
    )
    union = _mapping(health.get("failure_union_counts"), "health.failure_union_counts")
    count_keys = HEALTH_TERMINAL_BUCKETS
    if (
        health.get("schema_version") != "multi_event_health_v1"
        or health.get("decision_response_schema")
        != MULTI_EVENT_DECISION_RESPONSE_SCHEMA
        or health.get("failure_union") != "exact_terminal_decision_status"
        or set(union) != count_keys
        or any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            for value in union.values()
        )
        or total_orders <= 0
        or bad_orders < 0
        or bad_orders > total_orders
        or union.get("legacy_parse_invalid") != 0
        or sum(union.values()) != total_orders
        or sum(union[key] for key in count_keys - {"valid_decisions"})
        != bad_orders
        or dict(union) != dict(terminal_status_counts)
    ):
        raise MultiEventInputError("health terminal-status union is inconsistent")
    completion = _mapping(manifest.get("completion"), "completion")
    decisions = _mapping(
        completion.get("agent_decisions"), "completion.agent_decisions"
    )
    parsing = _mapping(completion.get("parsing"), "completion.parsing")
    attempts = _mapping(
        completion.get("application_provider_attempts"),
        "completion.application_provider_attempts",
    )
    if (
        decisions.get("completed") != total_orders
        or parsing.get("failed") != bad_orders
        or union["provider_exception_exhausted"]
        + union["provider_parse_exhausted"]
        != attempts.get("exhausted_logical_requests")
        or union["provider_exception_exhausted"]
        != provider_exhaustion_counts.get(PROVIDER_EXCEPTION_EXHAUSTED)
        or union["provider_parse_exhausted"]
        != provider_exhaustion_counts.get(PROVIDER_PARSE_EXHAUSTED)
    ):
        raise MultiEventInputError(
            "health terminal counts do not close against completion"
        )
    bad_frac = _finite(health.get("bad_frac"), "health.bad_frac")
    raw_bad_frac = bad_orders / total_orders
    if bad_frac != round(raw_bad_frac, 4):
        raise MultiEventInputError(
            "health.bad_frac does not match the exact unrounded counts"
        )
    return raw_bad_frac


def validate_selected_children(
    prepared: PreparedSelection,
    protocol: Mapping[str, Any],
) -> tuple[list[Observation], list[Mapping[str, Any]]]:
    """Load exact managed children, returning accepted observations and rejections."""

    observations: list[Observation] = []
    rejections: list[Mapping[str, Any]] = []
    health_max = float(protocol["acceptance_and_execution"]["health_bad_frac_max"])
    for selected in prepared.children:
        reasons: list[str] = []
        try:
            actual_manifest_hash = sha256_file(selected.manifest_path)
        except OSError:
            rejections.append(_rejection(selected, ["manifest_unreadable"]))
            continue
        if actual_manifest_hash != selected.manifest_sha256:
            rejections.append(_rejection(selected, ["declared_manifest_hash_mismatch"]))
            continue
        event = prepared.events[selected.event_id]
        try:
            canonical_command = _canonical_child_command(
                event,
                arm=selected.arm,
                seed=selected.seed,
                repeat_idx=selected.repeat_idx,
                execution_mode=prepared.execution_plan["execution_mode"],
                child_root=prepared.child_root,
            )
            expected = expected_run_seed_identity(canonical_command)
        except (MultiEventProtocolError, OSError, TypeError, ValueError):
            rejections.append(
                _rejection(selected, ["canonical_expected_identity_invalid"])
            )
            continue
        candidate = ReusableRunCandidate(selected.manifest_path, prepared.child_root)
        try:
            child, _compatibility = load_child_run_identity(candidate)
        except ResultReuseError as error:
            rejections.append(_rejection(selected, [error.reason_code]))
            continue
        decision = assess_run_seed_reuse(
            candidate_path=selected.manifest_path,
            allowed_result_root=prepared.child_root,
            child_command=canonical_command,
            max_bad_frac=health_max,
        )
        if decision is None:
            reasons.append("manifest_missing")
        elif not decision.reusable:
            reasons.extend(decision.reason_codes)
        if child.reference_path_content_hash != event.reference_csv_sha256:
            reasons.append("frozen_reference_content_hash_mismatch")
        actual_identity = _child_actual_identity(child)
        for field, expected_value in selected.expected_identity.items():
            if field == "reported_model_aliases":
                continue
            if actual_identity.get(field) != expected_value:
                reasons.append(f"declared_identity_mismatch:{field}")
        try:
            raw_manifest = _read_json(selected.manifest_path, "child manifest")
        except MultiEventInputError:
            reasons.append("manifest_invalid")
            raw_manifest = {}
        if prepared.execution_plan["execution_mode"] == "openai_live":
            snapshot = prepared.source_snapshot
            git = raw_manifest.get("git")
            if (
                not isinstance(snapshot, Mapping)
                or not isinstance(git, Mapping)
                or git.get("dirty") is not False
                or child.git_dirty is not False
                or child.git_commit != snapshot.get("head_commit")
                or child.scientific_component_fingerprint
                != snapshot.get("scientific_component_fingerprint")
            ):
                reasons.append("live_source_snapshot_mismatch")
        reasons.extend(
            _config_mismatches(
                raw_manifest,
                protocol,
                selected,
                event,
                execution_mode=prepared.execution_plan["execution_mode"],
            )
        )
        result_path = selected.manifest_path.parent / RESULT_ARTIFACT
        try:
            result_hash = sha256_file(result_path)
        except OSError:
            reasons.append("result_artifact_unreadable")
            result_hash = None
        if result_hash != selected.result_sha256:
            reasons.append("declared_result_hash_mismatch")
        if reasons:
            rejections.append(_rejection(selected, reasons))
            continue
        try:
            result = _read_json(result_path, "experiment_result.json")
            identity = _mapping(result.get("multi_event_identity"), "multi_event_identity")
            expected_cell_identity = {
                "schema_version": CELL_IDENTITY_SCHEMA_VERSION,
                "protocol_sha256": prepared.protocol_sha256,
                "event_id": selected.event_id,
                "arm": selected.arm,
                "seed": selected.seed,
                "repeat_idx": selected.repeat_idx,
                "reference_csv_sha256": event.reference_csv_sha256,
                "news_timeline_sha256": event.news_timeline_sha256,
                "reference_transform_sha256": event.reference_transform_sha256,
            }
            if dict(identity) != expected_cell_identity:
                raise MultiEventInputError("multi_event_identity mismatch")
            if result.get("run_id") != child.run_id or result.get("seed") != selected.seed:
                raise MultiEventInputError("result lifecycle identity mismatch")
            reported_aliases = _list(
                result.get("reported_model_aliases"), "reported_model_aliases"
            )
            public_machine_evidence = _validated_application_attempt_evidence(
                raw_manifest,
                selected.manifest_path.parent / "events.jsonl",
                execution_mode=prepared.execution_plan["execution_mode"],
            )
            validate_reported_model_alias_binding(
                raw_manifest,
                selected_aliases=selected.expected_identity[
                    "reported_model_aliases"
                ],
                result_aliases=reported_aliases,
                result_invalid_alias_count=result.get(
                    "invalid_reported_model_alias_count"
                ),
                execution_mode=prepared.execution_plan["execution_mode"],
            )
            condition = _mapping(result.get("condition"), "condition")
            if condition.get("social_enabled") is not (selected.arm == "social_on"):
                raise MultiEventInputError("result social arm mismatch")
            if condition.get("temperature") != 0.3 or condition.get("cache_enabled") is not False:
                raise MultiEventInputError("result model-request condition mismatch")
            metrics = _mapping(result.get("metrics"), "metrics")
            reported_drop_depth = _finite(
                metrics.get("drop_depth"), "metrics.drop_depth"
            )
            raw_bad_frac = _validated_multi_event_health(
                result,
                raw_manifest,
                terminal_status_counts=public_machine_evidence[
                    "terminal_status_counts"
                ],
                provider_exhaustion_counts=public_machine_evidence[
                    "provider_exhaustion_counts"
                ],
            )
            raw_path = _list(result.get("norm_log_path"), "norm_log_path")
            norm_log_path = tuple(
                _finite(value, "norm_log_path[]") for value in raw_path
            )
            if len(norm_log_path) != 25:
                raise MultiEventInputError("norm_log_path must contain exactly t0..t24")
            if abs(norm_log_path[0]) > 1e-9:
                raise MultiEventInputError("norm_log_path t0 must be normalized to zero")
            recomputed_drop_depth = recompute_drop_depth_from_path(
                norm_log_path, reported_rounded=reported_drop_depth
            )
            recovery_raw = metrics.get("recovery")
            recovery = (
                None
                if recovery_raw is None
                else _finite(recovery_raw, "metrics.recovery")
            )
        except MultiEventInputError:
            rejections.append(_rejection(selected, ["result_cell_contract_mismatch"]))
            continue
        if raw_bad_frac > health_max:
            rejections.append(_rejection(selected, ["health_bad_frac_exceeded"]))
            continue
        rmse, dtw_norm, dtw_raw_full_norm = _trajectory_metrics(norm_log_path, event)
        observations.append(
            Observation(
                selected.event_id,
                selected.arm,
                selected.seed,
                selected.repeat_idx,
                child.run_id,
                recomputed_drop_depth,
                reported_drop_depth,
                raw_bad_frac,
                norm_log_path,
                rmse,
                dtw_norm,
                dtw_raw_full_norm,
                recovery,
            )
        )
    return observations, rejections


def _sample_variance(values: Sequence[float]) -> Optional[float]:
    return stdev(values) ** 2 if len(values) > 1 else None


def _distribution(values: Iterable[Optional[float]]) -> Mapping[str, Any]:
    clean = [float(value) for value in values if value is not None and math.isfinite(value)]
    if not clean:
        return {"n": 0, "mean": None, "sample_std": None, "min": None, "max": None}
    return {
        "n": len(clean),
        "mean": mean(clean),
        "sample_std": stdev(clean) if len(clean) > 1 else 0.0,
        "min": min(clean),
        "max": max(clean),
    }


def cohen_d_arm_means(
    social_on: Sequence[float], social_off: Sequence[float]
) -> Optional[float]:
    """n-weighted pooled-sample-SD Cohen d on seed-level arm means."""

    if not social_on or not social_off:
        return None
    n_on, n_off = len(social_on), len(social_off)
    var_on = _sample_variance(social_on) or 0.0
    var_off = _sample_variance(social_off) or 0.0
    degrees = n_on + n_off - 2
    if degrees <= 0:
        return None
    pooled_var = ((n_on - 1) * var_on + (n_off - 1) * var_off) / degrees
    if pooled_var <= 1e-18:
        return None
    return (mean(social_on) - mean(social_off)) / math.sqrt(pooled_var)


def paired_cohen_dz(effects: Sequence[float]) -> Optional[float]:
    if len(effects) < 2:
        return None
    denominator = stdev(effects)
    if denominator <= 1e-9:
        return None
    return mean(effects) / denominator


def _percentile(sorted_values: Sequence[float], probability: float) -> float:
    if not sorted_values:
        raise ValueError("percentile requires at least one value")
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = max(0.0, min(1.0, probability)) * (len(sorted_values) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return sorted_values[lower]
    fraction = position - lower
    return sorted_values[lower] * (1.0 - fraction) + sorted_values[upper] * fraction


def clustered_bootstrap_ci(
    cluster_values: Mapping[int, Sequence[float]],
    *,
    B: int,
    seed: int,
    alpha: float,
) -> Mapping[str, Any]:
    """Percentile CI from complete seed clusters, deterministic for fixed inputs."""

    ids = sorted(cluster_values)
    if not ids:
        return {"estimate": None, "ci_low": None, "ci_high": None, "clusters": 0}
    cluster_means = {
        key: mean([float(value) for value in cluster_values[key]]) for key in ids
    }
    estimate = mean(cluster_means.values())
    rng = random.Random(int(seed))
    replicates = []
    for _ in range(int(B)):
        drawn = [ids[rng.randrange(len(ids))] for _ in ids]
        replicates.append(mean(cluster_means[key] for key in drawn))
    replicates.sort()
    return {
        "estimate": estimate,
        "ci_low": _percentile(replicates, alpha / 2.0),
        "ci_high": _percentile(replicates, 1.0 - alpha / 2.0),
        "clusters": len(ids),
    }


def balanced_variance_components(
    values: Mapping[int, Mapping[str, Sequence[float]]], *, K: int
) -> Mapping[str, Any]:
    """Balanced two-arm method-of-moments decomposition at seed level."""

    if not values:
        return {
            "complete_seeds": 0,
            "within_repeat_variance": {arm: None for arm in ARMS},
            "repeat_noise_contribution": None,
            "observed_seed_effect_variance": None,
            "between_seed_effect_variance": None,
            "repeat_noise_fraction": None,
        }
    within: dict[str, float] = {}
    for arm in ARMS:
        variances = []
        for by_arm in values.values():
            arm_values = list(by_arm[arm])
            if len(arm_values) != K:
                raise ValueError("variance components require a balanced K")
            variances.append(stdev(arm_values) ** 2 if K > 1 else 0.0)
        within[arm] = mean(variances)
    effects = [
        mean(by_arm["social_on"]) - mean(by_arm["social_off"])
        for by_arm in values.values()
    ]
    observed = _sample_variance(effects)
    repeat_contribution = (within["social_on"] + within["social_off"]) / K
    between = None if observed is None else max(0.0, observed - repeat_contribution)
    fraction = (
        repeat_contribution / observed
        if observed is not None and observed > 1e-18
        else None
    )
    return {
        "complete_seeds": len(values),
        "within_repeat_variance": within,
        "repeat_noise_contribution": repeat_contribution,
        "observed_seed_effect_variance": observed,
        "between_seed_effect_variance": between,
        "repeat_noise_fraction": fraction,
    }


def _mean_path(paths: Sequence[Sequence[float]]) -> list[float]:
    if not paths:
        return []
    lengths = {len(path) for path in paths}
    if len(lengths) != 1:
        raise ValueError("trajectory paths must have equal length; truncation is forbidden")
    length = next(iter(lengths))
    return [mean(path[index] for path in paths) for index in range(length)]


def _trajectory_envelope(
    seed_paths: Sequence[Sequence[float]], *, lower_q: float, upper_q: float
) -> Mapping[str, Any]:
    if not seed_paths:
        return {
            "unit": "seed_mean_over_K_complete_case_paths",
            "seed_count": 0,
            "lower_quantile": lower_q,
            "upper_quantile": upper_q,
            "mean": [],
            "lower": [],
            "upper": [],
        }
    center = _mean_path(seed_paths)
    lower = []
    upper = []
    for index in range(len(center)):
        values = sorted(float(path[index]) for path in seed_paths)
        lower.append(_percentile(values, lower_q))
        upper.append(_percentile(values, upper_q))
    return {
        "unit": "seed_mean_over_K_complete_case_paths",
        "seed_count": len(seed_paths),
        "lower_quantile": lower_q,
        "upper_quantile": upper_q,
        "mean": center,
        "lower": lower,
        "upper": upper,
    }


def _qualitative_categories(
    *,
    drop_depth: Optional[float],
    norm_log_path: Sequence[float],
    recovery_fraction: Optional[float],
    thresholds: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Apply only the preregistered inclusive boundaries, with finite fail-close."""

    finite = (
        drop_depth is not None
        and math.isfinite(float(drop_depth))
        and bool(norm_log_path)
        and all(math.isfinite(float(value)) for value in norm_log_path)
    )
    if not finite:
        return {
            "finite": False,
            "crash": None,
            "positive_jump": None,
            "full_recovery": None,
            "values": {
                "drop_depth": None,
                "peak_gain": None,
                "terminal_normalized_price": None,
                "recovery_fraction": recovery_fraction,
            },
        }
    normalized = [math.exp(float(value)) for value in norm_log_path]
    peak_gain = max(normalized) - 1.0
    terminal = normalized[-1]
    crash = float(drop_depth) <= float(thresholds["crash_threshold"]["value"])
    positive_jump = peak_gain >= float(
        thresholds["positive_jump_threshold"]["value"]
    )
    full_recovery = bool(
        crash
        and terminal >= float(thresholds["full_recovery_threshold"]["value"])
    )
    return {
        "finite": True,
        "crash": crash,
        "positive_jump": positive_jump,
        "full_recovery": full_recovery,
        "values": {
            "drop_depth": float(drop_depth),
            "peak_gain": peak_gain,
            "terminal_normalized_price": terminal,
            "recovery_fraction": recovery_fraction,
        },
    }


def _ordering_assessment(
    event_depths: Mapping[str, Optional[float]],
    thresholds: Mapping[str, Any],
) -> Mapping[str, Any]:
    order = list(thresholds["reference_depth_order_most_to_least_negative"])
    margin_required = float(thresholds["minimum_adjacent_ordering_margin"])
    finite = all(
        event_depths.get(event_id) is not None
        and math.isfinite(float(event_depths[event_id]))
        for event_id in order
    )
    comparisons = []
    if finite:
        for more_negative, less_negative in zip(order[:-1], order[1:]):
            margin = float(event_depths[less_negative]) - float(
                event_depths[more_negative]
            )
            comparisons.append(
                {
                    "more_negative": more_negative,
                    "less_negative": less_negative,
                    "observed_margin": margin,
                    "required_inclusive_margin": margin_required,
                    "passes": margin >= margin_required,
                }
            )
    return {
        "finite": finite,
        "order": order,
        "comparisons": comparisons,
        "passes": bool(finite and all(item["passes"] for item in comparisons)),
    }


def analyze_observations(
    observations: Sequence[Observation],
    *,
    protocol: Mapping[str, Any],
    rejections: Sequence[Mapping[str, Any]] = (),
    reference_log_paths: Optional[Mapping[str, Sequence[float]]] = None,
    execution_plan: Optional[Mapping[str, Any]] = None,
    protocol_profile: Optional[ProtocolProfile] = None,
    execution_acceleration: Optional[Mapping[str, Any]] = None,
) -> Mapping[str, Any]:
    """Pure event-complete aggregation with a stricter cross-event intersection."""

    if protocol_profile is None:
        if protocol.get("schema_version") == PROTOCOL_SCHEMA_VERSION_V2:
            raise MultiEventInputError(
                "workers2 analysis requires its exact protocol profile"
            )
    else:
        try:
            expected_profile = get_protocol_profile(
                protocol, protocol_profile.protocol_sha256
            )
        except MultiEventProtocolError as error:
            raise MultiEventInputError(
                "analysis protocol profile does not match the protocol"
            ) from error
        if protocol_profile != expected_profile:
            raise MultiEventInputError(
                "analysis protocol profile does not match the protocol"
            )

    design = protocol["design"]
    event_ids = [row["event_id"] for row in design["events"]]
    effective_plan = (
        _validated_execution_plan(
            execution_plan, protocol, protocol_profile
        )
        if execution_plan is not None
        else {
            "protocol_adherence": True,
            "execution_mode": "openai_live",
            "seeds": list(design["seeds"]),
            "repeat_indices": list(design["repeat_indices"]),
            "planned_runs": int(design["planned_runs"]),
            "override_reason": None,
            "launch_order_policy": dict(
                launch_order_policy(protocol_profile)
            ),
        }
    )
    if protocol_profile is not None:
        execution_acceleration = _validated_acceleration_stage(
            execution_acceleration,
            protocol_profile,
            required=(
                protocol_profile.execution_acceleration is not None
            ),
        )
        _validate_acceleration_stage_extent(
            execution_acceleration, effective_plan
        )
    seeds = list(effective_plan["seeds"])
    repeats = list(effective_plan["repeat_indices"])
    K = len(repeats)
    bootstrap = protocol["bootstrap"]
    thresholds = protocol["qualitative_thresholds"]
    by_cell: dict[tuple[str, str, int, int], Observation] = {}
    for observation in observations:
        cell = observation.cell
        if cell in by_cell:
            raise ValueError(f"duplicate accepted observation: {cell}")
        if (
            observation.event_id not in event_ids
            or observation.arm not in ARMS
            or observation.seed not in seeds
            or observation.repeat_idx not in repeats
        ):
            raise ValueError(f"accepted observation outside protocol: {cell}")
        if (
            len(observation.norm_log_path) != 25
            or abs(observation.norm_log_path[0]) > 1e-9
            or not all(math.isfinite(value) for value in observation.norm_log_path)
        ):
            raise ValueError(f"accepted observation has invalid 25-point path: {cell}")
        by_cell[cell] = observation

    rejected_by_cell = {
        (
            item.get("event_id"), item.get("arm"), item.get("seed"),
            item.get("repeat_idx"),
        ): list(item.get("reason_codes", []))
        for item in rejections
    }
    complete_by_event: dict[str, list[int]] = {event_id: [] for event_id in event_ids}
    exclusions: list[Mapping[str, Any]] = []
    for event_id in event_ids:
        for seed_id in seeds:
            missing = []
            for arm in ARMS:
                for repeat_idx in repeats:
                    cell = (event_id, arm, seed_id, repeat_idx)
                    if cell not in by_cell:
                        missing.append(
                            {
                                "arm": arm,
                                "repeat_idx": repeat_idx,
                                "reason_codes": rejected_by_cell.get(
                                    cell, ["not_listed_or_missing"]
                                ),
                            }
                        )
            if missing:
                exclusions.append(
                    {"event_id": event_id, "seed": seed_id, "excluded_cells": missing}
                )
            else:
                complete_by_event[event_id].append(seed_id)
    cross_event_complete = sorted(
        set(seeds).intersection(*(set(values) for values in complete_by_event.values()))
    )

    event_results: dict[str, Any] = {}
    event_seed_arm_means: dict[str, dict[int, dict[str, float]]] = {}
    for event_id in event_ids:
        complete_seeds = complete_by_event[event_id]
        balanced: dict[int, dict[str, list[float]]] = {}
        seed_rows = []
        event_seed_arm_means[event_id] = {}
        for seed_id in complete_seeds:
            arm_values = {
                arm: [
                    by_cell[(event_id, arm, seed_id, repeat_idx)].drop_depth
                    for repeat_idx in repeats
                ]
                for arm in ARMS
            }
            balanced[seed_id] = arm_values
            arm_means = {arm: mean(values) for arm, values in arm_values.items()}
            event_seed_arm_means[event_id][seed_id] = arm_means
            seed_rows.append(
                {
                    "seed": seed_id,
                    "arm_means": arm_means,
                    "effect_social_on_minus_off": (
                        arm_means["social_on"] - arm_means["social_off"]
                    ),
                }
            )
        effects = [row["effect_social_on_minus_off"] for row in seed_rows]
        on_means = [row["arm_means"]["social_on"] for row in seed_rows]
        off_means = [row["arm_means"]["social_off"] for row in seed_rows]
        event_bootstrap = clustered_bootstrap_ci(
            {row["seed"]: [row["effect_social_on_minus_off"]] for row in seed_rows},
            B=int(bootstrap["B"]),
            seed=int(bootstrap["seed"]),
            alpha=float(bootstrap["alpha"]),
        )
        cell_distributions: dict[str, Any] = {}
        trajectories: dict[str, list[float]] = {}
        trajectory_envelopes: dict[str, Mapping[str, Any]] = {}
        qualitative_by_arm: dict[str, Any] = {}
        for arm in ARMS:
            complete_selected = [
                by_cell[(event_id, arm, seed_id, repeat_idx)]
                for seed_id in complete_seeds
                for repeat_idx in repeats
            ]
            all_selected = [
                observation
                for observation in by_cell.values()
                if observation.event_id == event_id and observation.arm == arm
            ]

            def distributions(selected: Sequence[Observation]) -> Mapping[str, Any]:
                return {
                    "drop_depth_recomputed_from_norm_log_path": _distribution(
                        item.drop_depth for item in selected
                    ),
                    "drop_depth_reported_rounded_compatibility": _distribution(
                        item.reported_drop_depth for item in selected
                    ),
                    "rmse_logprice_transformed_25": _distribution(
                        item.rmse_logprice for item in selected
                    ),
                    "dtw_norm_transformed_25": _distribution(
                        item.dtw_norm for item in selected
                    ),
                    "dtw_norm_raw_full_episode": _distribution(
                        item.dtw_raw_full_norm for item in selected
                    ),
                    "recovery_fraction": _distribution(
                        item.recovery for item in selected
                    ),
                    "bad_frac": _distribution(item.bad_frac for item in selected),
                }

            cell_distributions[arm] = {
                "all_identity_health_accepted": distributions(all_selected),
                "primary_complete_case": distributions(complete_selected),
            }
            seed_mean_paths = [
                _mean_path(
                    [
                        by_cell[(event_id, arm, seed_id, repeat_idx)].norm_log_path
                        for repeat_idx in repeats
                    ]
                )
                for seed_id in complete_seeds
            ]
            envelope_protocol = protocol["trajectory_envelope"]
            trajectory_envelopes[arm] = _trajectory_envelope(
                seed_mean_paths,
                lower_q=float(envelope_protocol["pointwise_lower_quantile"]),
                upper_q=float(envelope_protocol["pointwise_upper_quantile"]),
            )
            trajectories[arm] = list(trajectory_envelopes[arm]["mean"])
            trajectory_drop = (
                min(math.exp(value) for value in trajectories[arm]) - 1.0
                if trajectories[arm]
                else None
            )
            primary_distribution = cell_distributions[arm]["primary_complete_case"]
            recovery_mean = primary_distribution["recovery_fraction"]["mean"]
            categories = _qualitative_categories(
                drop_depth=trajectory_drop,
                norm_log_path=trajectories[arm],
                recovery_fraction=recovery_mean,
                thresholds=thresholds,
            )
            target = thresholds["event_targets"][event_id]
            categories = dict(categories)
            categories["trajectory_derived_drop_depth"] = trajectory_drop
            categories["distributional_child_drop_depth_mean"] = (
                primary_distribution["drop_depth_recomputed_from_norm_log_path"][
                    "mean"
                ]
            )
            categories["target"] = dict(target)
            categories["target_match"] = bool(
                categories["finite"]
                and all(categories[key] is expected for key, expected in target.items())
            )
            qualitative_by_arm[arm] = categories

        reference_path = list((reference_log_paths or {}).get(event_id, []))
        reference_drop = (
            min(math.exp(value) for value in reference_path) - 1.0
            if reference_path
            else None
        )
        reference_categories = _qualitative_categories(
            drop_depth=reference_drop,
            norm_log_path=reference_path,
            recovery_fraction=None,
            thresholds=thresholds,
        )
        event_results[event_id] = {
            "complete_seed_count": len(complete_seeds),
            "complete_seed_ids": complete_seeds,
            "seed_arm_means": seed_rows,
            "effect": event_bootstrap,
            "cohen_d_arm_means": cohen_d_arm_means(on_means, off_means),
            "paired_cohen_dz": paired_cohen_dz(effects),
            "variance_components": balanced_variance_components(balanced, K=K),
            "cell_distributions": cell_distributions,
            "mean_norm_log_trajectory": trajectories,
            "trajectory_envelope": trajectory_envelopes,
            "reference_norm_log_path": reference_path,
            "reference_categories": reference_categories,
            "qualitative_by_arm": qualitative_by_arm,
        }

    pooled_effects: dict[int, list[float]] = {}
    pooled_seed_arm_means: dict[str, list[float]] = {arm: [] for arm in ARMS}
    for seed_id in cross_event_complete:
        pooled_effects[seed_id] = [
            event_seed_arm_means[event_id][seed_id]["social_on"]
            - event_seed_arm_means[event_id][seed_id]["social_off"]
            for event_id in event_ids
        ]
        for arm in ARMS:
            pooled_seed_arm_means[arm].append(
                mean(event_seed_arm_means[event_id][seed_id][arm] for event_id in event_ids)
            )
    pooled = clustered_bootstrap_ci(
        pooled_effects,
        B=int(bootstrap["B"]),
        seed=int(bootstrap["seed"]),
        alpha=float(bootstrap["alpha"]),
    )
    pooled["cohen_d_arm_means"] = cohen_d_arm_means(
        pooled_seed_arm_means["social_on"], pooled_seed_arm_means["social_off"]
    )
    pooled["paired_cohen_dz"] = paired_cohen_dz(
        [mean(pooled_effects[seed_id]) for seed_id in cross_event_complete]
    )

    arm_assessments = {}
    for arm in ARMS:
        ordering = _ordering_assessment(
            {
                event_id: event_results[event_id]["qualitative_by_arm"][arm][
                    "values"
                ]["drop_depth"]
                for event_id in event_ids
            },
            thresholds,
        )
        event_matches = {
            event_id: event_results[event_id]["qualitative_by_arm"][arm][
                "target_match"
            ]
            for event_id in event_ids
        }
        arm_assessments[arm] = {
            "event_target_matches": event_matches,
            "depth_ordering": ordering,
            "passes_all_preregistered_criteria": bool(
                all(event_matches.values()) and ordering["passes"]
            ),
        }

    complete_event_seed_pairs = sum(len(values) for values in complete_by_event.values())
    declared_absent = sum(
        1 for item in rejections if item.get("slot_status") in {"missing", "rejected"}
    )
    selected_child_rejections = len(rejections) - declared_absent
    frozen_planned_runs = int(design["planned_runs"])
    frozen_complete_pairs = len(event_ids) * len(seeds)
    full_acquisition_stage = bool(
        execution_acceleration is None
        or execution_acceleration["stage"] == "full"
    )
    complete_frozen_grid = bool(
        int(effective_plan["planned_runs"]) == frozen_planned_runs
        and len(observations) == frozen_planned_runs
        and not rejections
        and complete_event_seed_pairs == frozen_complete_pairs
        and all(
            len(complete_by_event[event_id]) == len(seeds)
            for event_id in event_ids
        )
        and len(cross_event_complete) == len(seeds)
        and full_acquisition_stage
    )
    protocol_adherent_realism = bool(
        effective_plan["protocol_adherence"]
        and effective_plan["execution_mode"] == "openai_live"
        and thresholds["thresholds_approved"] is True
        and complete_frozen_grid
    )
    protocol_adherent_but_incomplete = bool(
        effective_plan["protocol_adherence"]
        and effective_plan["execution_mode"] == "openai_live"
        and not complete_frozen_grid
    )
    result = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "protocol_id": protocol["protocol_id"],
        "study_status": protocol["study_status"],
        "confirmatory": False,
        "design": {
            "planned_runs": int(effective_plan["planned_runs"]),
            "events": event_ids,
            "arms": list(ARMS),
            "N": len(seeds),
            "K": K,
            "execution_plan": dict(effective_plan),
            "primary_unit": design["primary_unit"],
            "primary_outcome": design["primary_outcome"],
            "primary_realism_criterion": design["primary_realism_criterion"],
            "primary_social_estimand": design["primary_social_estimand"],
        },
        "acquisition_order": {
            "schema_version": "multi_event_acquisition_interpretation_v1",
            "counterbalanced": True,
            "launch_order_policy": dict(
                effective_plan["launch_order_policy"]
            ),
            "interpretation": (
                "adjacent arm pairs, rotated event temporal positions, and "
                "balanced arm-first parity reduce acquisition-time confounding"
            ),
            "resume_semantics": (
                "ineligible slots are filtered without reordering remaining jobs"
            ),
        },
        "honest_n": {
            "unit": "event_seed_complete_cases; seed_clusters for cross-event aggregate",
            "planned_event_seed_pairs": len(event_ids) * len(seeds),
            "planned_slots_accounted": len(observations) + len(rejections),
            "listed_managed_children": len(observations) + selected_child_rejections,
            "identity_and_health_accepted_children": len(observations),
            "selected_child_rejections": selected_child_rejections,
            "declared_missing_or_rejected_slots": declared_absent,
            "complete_case_children": complete_event_seed_pairs * len(ARMS) * K,
            "complete_event_seed_pairs": complete_event_seed_pairs,
            "complete_seed_ids_by_event": complete_by_event,
            "cross_event_complete_seed_clusters": len(cross_event_complete),
            "cross_event_complete_seed_ids": cross_event_complete,
            "excluded_event_seed_pairs": len(exclusions),
        },
        "exclusions": exclusions,
        "child_rejections": list(rejections),
        "primary_social_estimand": {
            "event_effects": {
                event_id: event_results[event_id]["effect"] for event_id in event_ids
            },
            "cross_event_complete_seed_effect": pooled,
        },
        "events": event_results,
        "bootstrap": {
            "method": bootstrap["method"],
            "B": int(bootstrap["B"]),
            "seed": int(bootstrap["seed"]),
            "alpha": float(bootstrap["alpha"]),
            "cluster": "seed",
            "interpretation": {
                "confirmatory": False,
                "interval_granularity": (
                    "percentile support is discrete because at most eight seed "
                    "clusters are resampled"
                ),
                "low_power_risk": (
                    "N=8 and K=3 are a variance-components pilot; intervals are "
                    "not significance proof and may be unstable or wide"
                ),
            },
        },
        "qualitative_claims": {
            "status": thresholds["status"],
            "thresholds_approved": bool(thresholds["thresholds_approved"]),
            "approval_record": thresholds.get("approval_record"),
            "confirmatory_claims_allowed": False,
            "finite_value_policy": thresholds["finite_value_policy"],
            "assessment_by_arm": arm_assessments,
            "primary_realism_assessment_social_on": arm_assessments["social_on"],
            "protocol_adherent_realism_claim_allowed": protocol_adherent_realism,
            "preregistered_realism_claim_eligible": protocol_adherent_realism,
            "realism_assessment_status": (
                "protocol_adherent_live_pilot_descriptive_not_confirmatory"
                if protocol_adherent_realism
                else "protocol_adherent_live_pilot_incomplete_descriptive_not_claim_eligible"
                if protocol_adherent_but_incomplete
                else "engineering_only_nonadherent_or_mock_not_claim_eligible"
            ),
            "no_curve_fit": protocol["reference_phase_transform"]["no_curve_fit"],
        },
        "scientific_semantics_change": protocol["scientific_semantics_change"],
    }
    if execution_acceleration is not None:
        canary = execution_acceleration["stage"] == "canary"
        submitted_slots = int(
            execution_acceleration["submitted_slot_limit"]
        )
        result["execution_acceleration"] = dict(
            execution_acceleration
        )
        result["acquisition_profile"] = {
            "schema_version": "multi_event_acquisition_profile_v1",
            "profile_id": execution_acceleration["profile_id"],
            "workers": execution_acceleration["workers"],
            "stage": execution_acceleration["stage"],
            "scheduler": execution_acceleration["scheduler"],
            "pair_barrier": True,
            "submitted_slots": submitted_slots,
            "deferred_slots": (
                int(effective_plan["planned_runs"]) - submitted_slots
                if canary
                else 0
            ),
            "complete_grid_claim_allowed": bool(
                complete_frozen_grid and not canary
            ),
            "workers1_workers2_pooling_allowed": False,
            "scientific_n_k_contribution_to_workers1": 0,
            "interpretation": (
                "incomplete_canary_descriptive_only"
                if canary
                else "full_workers2_descriptive_nonconfirmatory"
            ),
        }
        result["qualitative_claims"][
            "protocol_adherent_realism_claim_allowed"
        ] = bool(
            result["qualitative_claims"][
                "protocol_adherent_realism_claim_allowed"
            ]
            and not canary
        )
        result["qualitative_claims"][
            "preregistered_realism_claim_eligible"
        ] = bool(
            result["qualitative_claims"][
                "preregistered_realism_claim_eligible"
            ]
            and not canary
        )
        if canary:
            result["qualitative_claims"][
                "realism_assessment_status"
            ] = (
                "workers2_canary_incomplete_descriptive_not_claim_eligible"
            )
    return result


def create_three_panel_figure(summary: Mapping[str, Any]):
    """Create the required effect/trajectory/variance three-panel figure."""

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    events = list(summary["events"])
    fig, axes = plt.subplots(1, 3, figsize=(17, 5.5))

    effect_ax = axes[0]
    labels = []
    for index, event_id in enumerate(events):
        event = summary["events"][event_id]
        rows = event["seed_arm_means"]
        effects = [row["effect_social_on_minus_off"] for row in rows]
        for point_index, value in enumerate(effects):
            offset = (point_index - (len(effects) - 1) / 2.0) * 0.025
            effect_ax.scatter(index + offset, value, color="#376996", s=22, alpha=0.7)
        estimate = event["effect"]["estimate"]
        low = event["effect"]["ci_low"]
        high = event["effect"]["ci_high"]
        if estimate is not None:
            effect_ax.errorbar(
                [index], [estimate],
                yerr=[[estimate - low], [high - estimate]],
                color="#B23A48", marker="D", capsize=4, lw=2,
            )
        labels.append(event_id.replace("_v1", ""))
    effect_ax.axhline(0.0, color="#777", lw=1)
    effect_ax.set_xticks(range(len(labels)), labels, rotation=25, ha="right")
    effect_ax.set_ylabel("drop_depth effect (social_on - social_off)")
    effect_ax.set_title("Paired seed effects and 95% cluster CI")
    effect_ax.grid(alpha=0.25, axis="y")

    trajectory_ax = axes[1]
    colors = {"social_off": "#666666", "social_on": "#1B7F79"}
    for event_index, event_id in enumerate(events):
        event = summary["events"][event_id]
        for arm in ARMS:
            path = event["mean_norm_log_trajectory"][arm]
            if path:
                envelope = event["trajectory_envelope"][arm]
                trajectory_ax.fill_between(
                    range(len(path)),
                    envelope["lower"],
                    envelope["upper"],
                    color=colors[arm],
                    alpha=0.06 + event_index * 0.025,
                )
                trajectory_ax.plot(
                    range(len(path)), path,
                    color=colors[arm],
                    alpha=0.45 + event_index * 0.2,
                    linestyle="-" if arm == "social_on" else "--",
                    label=f"{event_id}: {arm}",
                )
        reference = event.get("reference_norm_log_path", [])
        if reference:
            trajectory_ax.plot(
                range(len(reference)), reference, color="#B23A48", alpha=0.35,
                linestyle=":", label=f"{event_id}: reference",
            )
    trajectory_ax.axhline(0.0, color="#999", lw=0.8)
    trajectory_ax.set_xlabel("shock-aligned step")
    trajectory_ax.set_ylabel("normalized log price")
    trajectory_ax.set_title("Complete-case arm means vs references")
    trajectory_ax.grid(alpha=0.25)
    trajectory_ax.legend(fontsize=6, loc="best")

    variance_ax = axes[2]
    within_values = []
    between_values = []
    for event_id in events:
        component = summary["events"][event_id]["variance_components"]
        within_values.append(component["repeat_noise_contribution"] or 0.0)
        between_values.append(component["between_seed_effect_variance"] or 0.0)
    x = list(range(len(events)))
    variance_ax.bar(x, within_values, label="repeat-noise contribution", color="#E09F3E")
    variance_ax.bar(
        x, between_values, bottom=within_values,
        label="between-seed effect variance", color="#335C67",
    )
    variance_ax.set_xticks(x, labels, rotation=25, ha="right")
    variance_ax.set_ylabel("variance")
    variance_ax.set_title("Balanced variance components")
    variance_ax.grid(alpha=0.25, axis="y")
    variance_ax.legend(fontsize=7)

    fig.tight_layout()
    return fig


def _reference_log_paths(events: Mapping[str, EventInput]) -> Mapping[str, Sequence[float]]:
    return {
        event_id: list(event.transformed_reference_log_path)
        for event_id, event in events.items()
    }


def _write_outputs(
    summary: Mapping[str, Any], run_dir: Path
) -> tuple[Path, Path]:
    summary_path = run_dir / SUMMARY_FILENAME
    with summary_path.open("x", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, ensure_ascii=False, sort_keys=True, allow_nan=False)
        handle.write("\n")
    figure = create_three_panel_figure(summary)
    plot_path = run_dir / PLOT_FILENAME
    figure.savefig(plot_path, dpi=140)
    import matplotlib.pyplot as plt

    plt.close(figure)
    return summary_path, plot_path


def build_argparser() -> RaisingArgumentParser:
    parser = RaisingArgumentParser(allow_abbrev=False)
    parser.add_argument(
        "--driver-manifest",
        required=True,
        help=(
            "finished experiments.multi_event run_manifest.json below child-root; "
            "standalone selection JSON is intentionally ineligible"
        ),
    )
    parser.add_argument("--child-root", required=True)
    parser.add_argument("--reference-root", default=".")
    parser.add_argument("--protocol", default=str(PROTOCOL_PATH))
    parser.add_argument("--out", default="results_multi_event_analysis")
    parser.add_argument("--run-id", default=None)
    parser.add_argument(
        "--require-confirmatory",
        action="store_true",
        help="fail unless qualitative numeric thresholds have prior recorded approval",
    )
    return parser


def _execute_managed_analysis(
    args: Any,
    prepared: PreparedSelection,
    protocol: Mapping[str, Any],
) -> tuple[Mapping[str, Any], Path]:
    """Create, populate, and finish one provenance-complete analysis run."""

    managed = ManagedRunContext.create_driver(
        out_root=Path(args.out),
        command_identity="experiments.aggregate_multi_event",
        planned_runs=0,
        run_id=args.run_id,
        worker_count=1,
        input_paths=prepared.input_paths,
    )
    for descriptor in managed.manifest.get("inputs", []):
        label = str(descriptor.get("label", ""))
        descriptor["provenance_class"] = (
            "frozen_analysis_protocol"
            if label == "protocol"
            else "explicit_analysis_selection"
            if label == "analysis_selection"
            else "scientific_reference_input"
            if label.startswith(("reference_", "timeline_", "catalog_"))
            else "identity_validated_managed_child_input"
        )
    managed.manifest["managed_context"]["run_kind"] = "analysis"
    managed.manifest["analysis_selection"] = {
        "schema_version": SELECTION_SCHEMA_VERSION,
        "protocol_sha256": prepared.protocol_sha256,
        "listed_children": len(prepared.children),
        "declared_missing_or_rejected_slots": len(
            prepared.declared_missing_or_rejected
        ),
        "planned_slots_accounted": (
            len(prepared.children) + len(prepared.declared_missing_or_rejected)
        ),
        "execution_plan": dict(prepared.execution_plan),
        "selection_mode": "finished_driver_registered_artifacts_no_glob",
        "trust_anchor": "finished_registered_experiments.multi_event_parent",
        "driver_run_id": prepared.driver_run_id,
        "driver_manifest_sha256": sha256_file(prepared.driver_manifest_path),
        "study_model_identity": dict(prepared.study_model_identity),
        "output_root_policy": (
            None
            if prepared.output_root_policy is None
            else dict(prepared.output_root_policy)
        ),
        "source_snapshot": (
            None
            if prepared.source_snapshot is None
            else dict(prepared.source_snapshot)
        ),
        "protocol_profile": (
            None
            if prepared.protocol_profile is None
            else {
                "schema_version": prepared.protocol_profile.schema_version,
                "protocol_id": prepared.protocol_profile.protocol_id,
                "protocol_sha256": (
                    prepared.protocol_profile.protocol_sha256
                ),
                "canonical_protocol_relative_path": (
                    prepared.protocol_profile
                    .canonical_protocol_relative_path
                ),
                "canonical_live_output_relative_path": (
                    prepared.protocol_profile
                    .canonical_live_output_relative_path
                ),
                "workers": prepared.protocol_profile.workers,
            }
        ),
        "execution_acceleration": (
            None
            if prepared.execution_acceleration is None
            else dict(prepared.execution_acceleration)
        ),
    }
    managed.manifest.write_atomic()
    with managed:
        managed.set_stage("result_export")
        analysis = {
            "schema_version": "1.0",
            "unit": "analysis_attempts",
            "planned": 1,
            "started": 1,
            "completed": 0,
            "failed": 0,
            "input_files": len(prepared.input_paths),
        }
        managed.manifest["analysis_completion"] = analysis
        managed.manifest.write_atomic()
        try:
            observations, runtime_rejections = validate_selected_children(
                prepared, protocol
            )
            rejections = [
                *prepared.declared_missing_or_rejected,
                *runtime_rejections,
            ]
            summary = analyze_observations(
                observations,
                protocol=protocol,
                rejections=rejections,
                reference_log_paths=_reference_log_paths(prepared.events),
                execution_plan=prepared.execution_plan,
                protocol_profile=prepared.protocol_profile,
                execution_acceleration=(
                    prepared.execution_acceleration
                ),
            )
            summary = dict(summary)
            summary["protocol_sha256"] = prepared.protocol_sha256
            summary["selection_manifest_sha256"] = sha256_file(
                prepared.selection_path
            )
            summary["driver_parent_run_id"] = prepared.driver_run_id
            summary["driver_parent_manifest_sha256"] = sha256_file(
                prepared.driver_manifest_path
            )
            summary["study_model_identity"] = dict(
                prepared.study_model_identity
            )
            summary["output_root_policy"] = (
                None
                if prepared.output_root_policy is None
                else dict(prepared.output_root_policy)
            )
            summary["source_snapshot"] = (
                None
                if prepared.source_snapshot is None
                else dict(prepared.source_snapshot)
            )
            summary["model_identity_interpretation"] = (
                model_identity_interpretation(prepared.study_model_identity)
            )
            _write_outputs(summary, managed.run_dir)
        except BaseException:
            analysis["failed"] = 1
            managed.manifest.write_atomic()
            raise
        analysis["completed"] = 1
        managed.manifest["analysis_honest_n"] = summary["honest_n"]
        managed.manifest.write_atomic()
        managed.finish(legacy_filenames=(SUMMARY_FILENAME, PLOT_FILENAME))
    return summary, managed.run_dir


def main(argv: Optional[Sequence[str]] = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    analysis_lock: Optional[AnalysisAttemptLock] = None
    try:
        bootstrap = bootstrap_cli(
            argv,
            default_out="results_multi_event_analysis",
            command_identity="python -m experiments.aggregate_multi_event",
        )
    except BootstrapCLIError as error:
        print(
            "provenance_not_created_reason={}".format(type(error).__name__),
            file=sys.stderr,
        )
        raise SystemExit(2)
    try:
        args = build_argparser().parse_args(argv)
        protocol_path = Path(args.protocol).resolve(strict=True)
        protocol = load_protocol(protocol_path)
        thresholds = protocol["qualitative_thresholds"]
        if args.require_confirmatory and (
            protocol.get("confirmatory") is not True
            or not thresholds["thresholds_approved"]
            or not thresholds.get("approval_record")
        ):
            raise ApprovalRequiredError(
                "the preregistered variance-components pilot is not confirmatory"
            )
        analysis_lock = AnalysisAttemptLock(Path(args.child_root))
        analysis_lock.__enter__()
        prepared = prepare_driver_selection(
            Path(args.driver_manifest),
            protocol=protocol,
            protocol_path=protocol_path,
            child_root=Path(args.child_root),
            reference_root=Path(args.reference_root),
        )
    except (ManagedCLIError, MultiEventInputError, MultiEventProtocolError,
            ApprovalRequiredError, OSError, ValueError) as error:
        if analysis_lock is not None:
            analysis_lock.close()
        fail_cli(bootstrap, error, failure_stage="config_validation")
    except BaseException:
        if analysis_lock is not None:
            analysis_lock.close()
        raise

    try:
        summary, analysis_run_dir = _execute_managed_analysis(
            args, prepared, protocol
        )
    finally:
        if analysis_lock is not None:
            analysis_lock.close()
    print(
        "multi-event analysis complete: cross_event_complete_seed_clusters={} -> {}".format(
            summary["honest_n"]["cross_event_complete_seed_clusters"],
            analysis_run_dir,
        )
    )


if __name__ == "__main__":
    main()
