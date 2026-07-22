"""Managed, provider-free analysis for the preregistered multi-event pilot.

The CLI accepts one explicit selection manifest.  It never discovers child
runs from filenames or directory globs.  Every selected child crosses the
central managed-child lifecycle/artifact gate before it can contribute to the
complete-case seed analysis.

The pure :func:`analyze_observations` path performs no filesystem, Provider, or
network operation and is used by the synthetic numerical tests.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import math
from pathlib import Path
import random
from statistics import mean, stdev
import sys
from typing import Any, Iterable, Mapping, Optional, Sequence

from nmsim import validation as V
from nmsim.config import Config
from nmsim.config_contract import (
    CONFIG_FIELD_RULES,
    CONFIG_HASH_SCHEMA_VERSION,
    SCIENTIFIC,
    build_effective_config_contract,
)
from nmsim.managed_cli import (
    BootstrapCLIError,
    ManagedCLIError,
    RaisingArgumentParser,
    bootstrap_cli,
    fail_cli,
)
from nmsim.provenance import sha256_file
from nmsim.result_reuse import (
    ExpectedRunIdentity,
    ResultReuseError,
    ReusableRunCandidate,
    load_child_run_identity,
    validate_child_run_reuse,
)
from nmsim.run_context import ManagedRunContext

from experiments.run_seed import build_population


PROTOCOL_PATH = Path(__file__).with_name("multi_event_protocol.json")
PROTOCOL_SCHEMA_VERSION = "multi_event_protocol_v1"
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
DRIVER_SUMMARY_FILENAME = "driver_summary.json"
_HASH_HEX = frozenset("0123456789abcdef")


class MultiEventProtocolError(ValueError):
    """The frozen analysis protocol is invalid or no longer executable."""


class MultiEventInputError(ValueError):
    """The explicit analysis selection does not satisfy its schema."""


class ApprovalRequiredError(ValueError):
    """Confirmatory qualitative claims have no preregistered approved thresholds."""


@dataclass(frozen=True)
class EventInput:
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


def _read_json(path: Path, field: str) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise MultiEventInputError(f"{field} is not readable JSON") from error
    return _mapping(value, field)


def protocol_sha256(path: Path = PROTOCOL_PATH) -> str:
    return sha256_file(path)


def load_protocol(path: Path = PROTOCOL_PATH) -> Mapping[str, Any]:
    """Load and fail-closed validate the machine-readable frozen protocol."""

    protocol = _read_json(Path(path), "protocol")
    if protocol.get("schema_version") != PROTOCOL_SCHEMA_VERSION:
        raise MultiEventProtocolError("unsupported protocol schema_version")
    if (
        protocol.get("study_status") != "preregistered_variance_components_pilot"
        or protocol.get("confirmatory") is not False
    ):
        raise MultiEventProtocolError("study must remain a non-confirmatory pilot")
    design = _mapping(protocol.get("design"), "design")
    event_rows = _list(design.get("events"), "design.events")
    event_ids = [_text(_mapping(row, "event").get("event_id"), "event_id") for row in event_rows]
    if len(event_ids) != 3 or len(set(event_ids)) != 3:
        raise MultiEventProtocolError("the frozen design must contain three unique events")
    arms = _mapping(design.get("arms"), "design.arms")
    if arms != {"social_off": False, "social_on": True}:
        raise MultiEventProtocolError("the frozen social arms changed")
    seeds = _list(design.get("seeds"), "design.seeds")
    repeats = _list(design.get("repeat_indices"), "design.repeat_indices")
    if seeds != [11, 13, 17, 19, 23, 29, 31, 37] or repeats != [1, 2, 3]:
        raise MultiEventProtocolError("the frozen N=8 K=3 grid changed")
    if design.get("N") != 8 or design.get("K") != 3:
        raise MultiEventProtocolError("N or K does not match the frozen grid")
    planned = len(event_ids) * len(arms) * len(seeds) * len(repeats)
    if design.get("planned_runs") != planned or planned != 144:
        raise MultiEventProtocolError("planned_runs must equal 144")
    if design.get("primary_unit") != "event_seed_complete_across_both_arms_and_all_repeats":
        raise MultiEventProtocolError("primary complete-case unit changed")
    expected_sources = {
        "meta_2022_02_crash_v1": (
            "nmsim/reference_data/v1/meta_2022_02_crash.csv",
            "5f0a39c4cff4cc70d732cea1518266b9502ddf7e806e4e7127f252394f09319a",
            "nmsim/reference_data/v1/meta_2022_02_crash_news_timeline.jsonl",
            "27a0a9794e3b86b6b841617cf62fb2255e3e497f9f8c51cdd0a709254503b9b2",
        ),
        "spy_2020_03_covid_v_recovery_v1": (
            "nmsim/reference_data/v1/spy_2020_03_covid_v_recovery.csv",
            "6b4615af3eda402803852143d29a307a9c8090114bb9d6891451dba0e8fcb24d",
            "nmsim/reference_data/v1/spy_2020_03_covid_v_recovery_news_timeline.jsonl",
            "2b2cf2bce5d5c0fb1f939bc3068d68e35424e44d2923adf86ddb437ceace70aa",
        ),
        "meta_2023_02_efficiency_jump_v1": (
            "nmsim/reference_data/v1/meta_2023_02_efficiency_jump.csv",
            "5f419bbd078ef61f3819d8a14dca593cc1fe7e4e319843254327b81689d47a88",
            "nmsim/reference_data/v1/meta_2023_02_efficiency_jump_news_timeline.jsonl",
            "4c3e6a63fa280f0bbd41d00d90eaedbdbcc4367b4b6aad3218ac29ee73922706",
        ),
    }
    if set(event_ids) != set(expected_sources):
        raise MultiEventProtocolError("frozen event ids changed")
    for row in event_rows:
        event = _mapping(row, "design.events[]")
        if (
            event.get("reference_csv"),
            event.get("reference_csv_sha256"),
            event.get("news_timeline"),
            event.get("news_timeline_sha256"),
        ) != expected_sources[event["event_id"]]:
            raise MultiEventProtocolError("frozen event source path changed")
    catalog = _mapping(protocol.get("reference_data_catalog"), "reference_data_catalog")
    if catalog != {
        "path": "nmsim/reference_data/v1/catalog.json",
        "sha256": "02dad9ff1d9c6c2aaf1ab9ad10665649680ad2fab00358e4d5baa70da3752166",
        "schema_version": "reference_data_catalog_v1",
        "data_version": "v1",
        "binding_policy": (
            "the selection must contain exactly this catalog input; every event_id, "
            "reference_csv, and news_timeline path must match both design.events and "
            "the unique catalog dataset before content hashes are accepted"
        ),
    }:
        raise MultiEventProtocolError("authoritative reference-data catalog changed")

    acceptance = _mapping(
        protocol.get("acceptance_and_execution"), "acceptance_and_execution"
    )
    expected_acceptance = {
        "health_bad_frac_max": 0.15,
        "max_child_attempts": 5,
        "workers": 1,
        "cache_enabled": False,
        "temperature": 0.3,
    }
    for key, expected in expected_acceptance.items():
        if acceptance.get(key) != expected:
            raise MultiEventProtocolError(f"frozen acceptance field changed: {key}")

    bootstrap = _mapping(protocol.get("bootstrap"), "bootstrap")
    for key, expected in {
        "B": 10000,
        "seed": 20260722,
        "alpha": 0.05,
        "cluster": "seed",
    }.items():
        if bootstrap.get(key) != expected:
            raise MultiEventProtocolError(f"frozen bootstrap field changed: {key}")

    transform = _mapping(
        protocol.get("reference_phase_transform"), "reference_phase_transform"
    )
    if (
        transform.get("schema_version") != "1.0"
        or transform.get("transform_id")
        != "event_phase_normalized_log_linear_25_v1"
        or transform.get("target_points") != 25
        or transform.get("method")
        != "linear_interpolation_in_normalized_log_price_over_full_post_t0_horizon"
        or "no outcome-dependent" not in str(transform.get("no_curve_fit", ""))
    ):
        raise MultiEventProtocolError("reference phase transform changed")
    envelope = _mapping(protocol.get("trajectory_envelope"), "trajectory_envelope")
    if (
        envelope.get("unit")
        != "event_arm_seed_mean_over_K_complete_case_paths"
        or envelope.get("pointwise_lower_quantile") != 0.1
        or envelope.get("pointwise_upper_quantile") != 0.9
        or envelope.get("interpolation") != "linear_type7"
    ):
        raise MultiEventProtocolError("trajectory envelope definition changed")

    thresholds = _mapping(
        protocol.get("qualitative_thresholds"), "qualitative_thresholds"
    )
    approved = thresholds.get("thresholds_approved") is True
    if approved:
        if thresholds.get("status") != "preregistered_approved" or not isinstance(
            thresholds.get("approval_record"), Mapping
        ):
            raise MultiEventProtocolError("approved thresholds require an approval record")
        exact_thresholds = {
            "crash_threshold": {
                "metric": "trajectory_drop_depth_min_exp_mean_norm_log_path_minus_one",
                "operator": "less_than_or_equal",
                "value": -0.15,
                "inclusive": True,
            },
            "positive_jump_threshold": {
                "metric": "peak_normalized_price_minus_one_over_t0_to_t24",
                "operator": "greater_than_or_equal",
                "value": 0.1,
                "inclusive": True,
            },
        }
        for field, expected in exact_thresholds.items():
            if thresholds.get(field) != expected:
                raise MultiEventProtocolError(f"approved qualitative field changed: {field}")
        recovery = thresholds.get("full_recovery_threshold")
        if not isinstance(recovery, Mapping) or any(
            recovery.get(key) != expected
            for key, expected in {
                "requires_crash": True,
                "metric": "terminal_normalized_price_at_t24",
                "operator": "greater_than_or_equal",
                "value": 0.95,
                "inclusive": True,
            }.items()
        ):
            raise MultiEventProtocolError("approved recovery threshold changed")
        if thresholds.get("minimum_adjacent_ordering_margin") != 0.01:
            raise MultiEventProtocolError("approved ordering margin changed")
        if thresholds.get("reference_depth_order_most_to_least_negative") != event_ids:
            raise MultiEventProtocolError("approved reference depth order changed")
        targets = thresholds.get("event_targets")
        if not isinstance(targets, Mapping) or set(targets) != set(event_ids):
            raise MultiEventProtocolError("approved event target set changed")
    else:
        if thresholds.get("status") != "approval_required":
            raise MultiEventProtocolError("unapproved thresholds must fail closed")
        numeric_fields = (
            "crash_threshold",
            "positive_jump_threshold",
            "full_recovery_threshold",
            "minimum_adjacent_ordering_margin",
        )
        if any(thresholds.get(field) is not None for field in numeric_fields):
            raise MultiEventProtocolError("unapproved qualitative thresholds must be null")
        if thresholds.get("approval_record") is not None:
            raise MultiEventProtocolError("unapproved protocol cannot have an approval record")

    freeze = _mapping(protocol.get("effective_config_freeze"), "effective_config_freeze")
    if freeze.get("config_hash_schema_version") != CONFIG_HASH_SCHEMA_VERSION:
        raise MultiEventProtocolError("config hash schema_version changed")
    scientific = _mapping(freeze.get("scientific"), "effective_config_freeze.scientific")
    classified = {
        name for name, rule in CONFIG_FIELD_RULES.items() if rule.category == SCIENTIFIC
    }
    # ``news_timeline`` lands with the dependent Issue #1 core tranche.  Keeping
    # it in this pre-rebase protocol makes the integration requirement explicit;
    # after rebase it must be classified SCIENTIFIC by config_contract.
    pending_core_scientific = {"news_timeline"}
    expected_scientific = classified | pending_core_scientific
    if set(scientific) != expected_scientific:
        missing = sorted(expected_scientific - set(scientific))
        extra = sorted(set(scientific) - expected_scientific)
        raise MultiEventProtocolError(
            f"scientific Config coverage changed; missing={missing}, extra={extra}"
        )
    defaults = Config()
    factor_fields = {
        "seed", "population", "social_enabled", "reference_path", "news_timeline"
    }
    overrides = {"seed_fraction": 2.0 / 30.0, "news_round": 1, "news_text": ""}
    for field in sorted(classified - factor_fields):
        expected = overrides.get(field, getattr(defaults, field))
        if scientific.get(field) != expected:
            raise MultiEventProtocolError(f"frozen scientific Config changed: {field}")
    if scientific["seed"] != {"factor": "design.seeds", "values": seeds}:
        raise MultiEventProtocolError("seed factor does not match the design")
    if scientific["social_enabled"] != {
        "factor": "design.arms",
        "values": {"social_off": False, "social_on": True},
    }:
        raise MultiEventProtocolError("social_enabled factor does not match the arms")
    expected_population = build_population(0.5, 30)
    if scientific["population"] != expected_population or list(
        scientific["population"]
    ) != list(expected_population):
        raise MultiEventProtocolError("population no longer matches build_population(0.5,30)")
    for event_bound in ("news_timeline", "reference_path"):
        if not isinstance(scientific[event_bound], Mapping):
            raise MultiEventProtocolError(f"{event_bound} must remain event-bound")

    model_request = _mapping(freeze.get("model_request"), "model_request")
    expected_model = {
        "provider": "openai",
        "model": "MiniMax-M2.7",
        "cheap_model": "",
        "use_cheap_model": False,
        "openai_base_url": Config().openai_base_url,
        "openai_model": "MiniMax-M2.7",
        "temperature": 0.3,
        "max_tokens": 1024,
        "cache_enabled": False,
    }
    for key, expected in expected_model.items():
        if model_request.get(key) != expected:
            raise MultiEventProtocolError(f"frozen model request changed: {key}")
    return protocol


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


def _verify_file_hash(path: Path, expected: str, field: str) -> None:
    try:
        actual = sha256_file(path)
    except OSError as error:
        raise MultiEventInputError(f"{field} could not be hashed") from error
    if actual != expected:
        raise MultiEventInputError(f"{field} SHA-256 mismatch")


def resample_reference_log_path(
    prices: Sequence[float], shock_idx: int, *, target_points: int = 25
) -> tuple[float, ...]:
    """Apply the frozen, event-independent full-horizon phase transform.

    There is no fitted parameter: every post-t0 source point contributes through
    a fixed linear coordinate map to exactly ``target_points`` normalized-log
    values.
    """

    if target_points < 2:
        raise ValueError("target_points must be at least two")
    raw = V.norm_log_path(list(prices), int(shock_idx))
    if not raw or not all(math.isfinite(float(value)) for value in raw):
        raise ValueError("reference path must contain finite post-t0 values")
    if len(raw) == 1:
        return tuple(float(raw[0]) for _ in range(target_points))
    output = []
    for target_index in range(target_points):
        coordinate = target_index * (len(raw) - 1) / (target_points - 1)
        lower = int(math.floor(coordinate))
        upper = int(math.ceil(coordinate))
        fraction = coordinate - lower
        output.append(
            float(raw[lower]) * (1.0 - fraction) + float(raw[upper]) * fraction
        )
    return tuple(output)


def reference_transform_identity(
    event_id: str,
    reference_csv_sha256: str,
    transformed_log_path: Sequence[float],
) -> str:
    """Hash the exact transformed path and its source/method identity."""

    payload = {
        "schema_version": "1.0",
        "transform_id": "event_phase_normalized_log_linear_25_v1",
        "event_id": str(event_id),
        "source_reference_csv_sha256": str(reference_csv_sha256),
        "method": "linear_interpolation_in_normalized_log_price_over_full_post_t0_horizon",
        "target_points": 25,
        "norm_log_path": [float(value) for value in transformed_log_path],
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _planned_values(protocol: Mapping[str, Any]) -> tuple[tuple[str, ...], tuple[int, ...], tuple[int, ...]]:
    design = _mapping(protocol["design"], "design")
    events = tuple(row["event_id"] for row in design["events"])
    return events, tuple(design["seeds"]), tuple(design["repeat_indices"])


def _validated_execution_plan(
    raw: Any, protocol: Mapping[str, Any]
) -> Mapping[str, Any]:
    plan = _mapping(raw, "execution_plan")
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
    return {
        "protocol_adherence": adherence,
        "execution_mode": mode,
        "seeds": seeds,
        "repeat_indices": repeats,
        "planned_runs": planned_runs,
        "override_reason": plan.get("override_reason"),
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
        if not reasons or any(not isinstance(reason, str) or not reason for reason in reasons):
            raise MultiEventInputError(
                "missing/rejected planned slots require non-empty public reason_codes"
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
) -> Mapping[str, Any]:
    """Pure builder for the driver's immutable post-run selection artifact.

    The caller supplies already-final child and file hashes.  This helper does
    not discover paths or write a file; it guarantees that accepted children
    plus missing/rejected entries form an exact partition of the declared
    execution plan.  A protocol-adherent live plan is the frozen 144-cell
    grid; a non-adherent mock plan is an explicitly smaller engineering grid.
    """

    _sha256(protocol_sha256, "protocol_sha256")
    validated_plan = _validated_execution_plan(execution_plan, protocol)
    planned_cells = [_selection_slot(item, "planned_slots[]") for item in planned_slots]
    expected_cells = _planned_cells(protocol, validated_plan)
    if len(planned_cells) != len(set(planned_cells)) or set(planned_cells) != expected_cells:
        raise MultiEventInputError(
            "planned_slots must name each execution-plan cell exactly once"
        )
    _validate_selection_partition(
        accepted_children, rejected_slots, protocol, validated_plan
    )
    return {
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


def prepare_selection(
    selection_path: Path,
    *,
    protocol: Mapping[str, Any],
    protocol_path: Path,
    child_root: Path,
    reference_root: Path,
) -> PreparedSelection:
    """Validate structural selection identity without loading child results."""

    selection_path = Path(selection_path).resolve(strict=True)
    selection = _read_json(selection_path, "input manifest")
    if selection.get("schema_version") != SELECTION_SCHEMA_VERSION:
        raise MultiEventInputError("unsupported input manifest schema_version")
    expected_protocol_hash = protocol_sha256(protocol_path)
    if _sha256(selection.get("protocol_sha256"), "protocol_sha256") != expected_protocol_hash:
        raise MultiEventInputError("input manifest protocol_sha256 mismatch")

    execution_plan = _validated_execution_plan(
        selection.get("execution_plan"), protocol
    )
    planned_events, _frozen_seeds, _frozen_repeats = _planned_values(protocol)
    protocol_event_rows = {
        row["event_id"]: row for row in protocol["design"]["events"]
    }
    planned_seeds = tuple(execution_plan["seeds"])
    planned_repeats = tuple(execution_plan["repeat_indices"])
    raw_events = _list(selection.get("events"), "events")
    events: dict[str, EventInput] = {}
    input_paths: dict[str, Path] = {
        "protocol": Path(protocol_path).resolve(strict=True),
        "analysis_selection": selection_path,
    }
    for raw in raw_events:
        item = _mapping(raw, "event")
        event_id = _text(item.get("event_id"), "events[].event_id")
        if event_id not in planned_events or event_id in events:
            raise MultiEventInputError("events must name each preregistered event exactly once")
        reference = _mapping(item.get("reference_csv"), "events[].reference_csv")
        timeline = _mapping(item.get("news_timeline"), "events[].news_timeline")
        expected_event = protocol_event_rows[event_id]
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
            Path(reference_root), reference.get("path"), "reference_csv.path"
        )
        timeline_path = _resolve_explicit(
            Path(reference_root), timeline.get("path"), "news_timeline.path"
        )
        _verify_file_hash(reference_path, reference_hash, "reference_csv")
        _verify_file_hash(timeline_path, timeline_hash, "news_timeline")
        try:
            prices, shock_idx = V.load_reference(str(reference_path))
        except (OSError, ValueError, KeyError, IndexError) as error:
            raise MultiEventInputError("reference_csv is not loadable") from error
        if not prices or not all(math.isfinite(float(value)) and float(value) > 0 for value in prices):
            raise MultiEventInputError("reference_csv contains no valid positive path")
        transformed = _mapping(
            item.get("transformed_reference"), "events[].transformed_reference"
        )
        if transformed.get("schema_version") != "1.0":
            raise MultiEventInputError("unsupported transformed_reference schema_version")
        listed_transform = tuple(
            _finite(value, "transformed_reference.norm_log_path[]")
            for value in _list(
                transformed.get("norm_log_path"),
                "transformed_reference.norm_log_path",
            )
        )
        recomputed_transform = resample_reference_log_path(
            [float(value) for value in prices], int(shock_idx), target_points=25
        )
        if listed_transform != recomputed_transform:
            raise MultiEventInputError(
                "transformed_reference does not equal the frozen no-curve-fit transform"
            )
        transform_hash = _sha256(
            transformed.get("sha256"), "transformed_reference.sha256"
        )
        expected_transform_hash = reference_transform_identity(
            event_id, reference_hash, recomputed_transform
        )
        if transform_hash != expected_transform_hash:
            raise MultiEventInputError("transformed_reference SHA-256 mismatch")
        events[event_id] = EventInput(
            event_id,
            reference_path,
            reference_hash,
            timeline_path,
            timeline_hash,
            tuple(float(value) for value in prices),
            int(shock_idx),
            recomputed_transform,
            transform_hash,
        )
        input_paths[f"reference_{event_id}"] = reference_path
        input_paths[f"timeline_{event_id}"] = timeline_path
    if set(events) != set(planned_events):
        raise MultiEventInputError("events do not cover the preregistered event set")

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
    catalog_path = _resolve_explicit(
        Path(reference_root), catalog_item.get("path"), "catalog_inputs[].path"
    )
    catalog_digest = _sha256(
        catalog_item.get("sha256"), "catalog_inputs[].sha256"
    )
    if catalog_digest != catalog_contract["sha256"]:
        raise MultiEventInputError(
            "catalog input SHA-256 differs from the frozen protocol source bytes"
        )
    _verify_file_hash(catalog_path, catalog_digest, "catalog input")
    catalog_data = _read_json(catalog_path, "reference data catalog")
    if (
        catalog_data.get("schema_version") != catalog_contract["schema_version"]
        or catalog_data.get("data_version") != catalog_contract["data_version"]
    ):
        raise MultiEventInputError("authoritative catalog schema/data_version mismatch")
    raw_datasets = _list(catalog_data.get("datasets"), "catalog.datasets")
    datasets: dict[str, Mapping[str, Any]] = {}
    for raw_dataset in raw_datasets:
        dataset = _mapping(raw_dataset, "catalog.datasets[]")
        dataset_id = _text(dataset.get("dataset_id"), "catalog.dataset_id")
        if dataset_id in datasets:
            raise MultiEventInputError("catalog contains duplicate dataset_id")
        datasets[dataset_id] = dataset
    if set(datasets) != set(planned_events):
        raise MultiEventInputError("catalog dataset set differs from protocol events")
    for event_id, protocol_event in protocol_event_rows.items():
        dataset = datasets[event_id]
        if (
            dataset.get("reference_csv") != Path(protocol_event["reference_csv"]).name
            or dataset.get("news_timeline_jsonl")
            != Path(protocol_event["news_timeline"]).name
        ):
            raise MultiEventInputError(
                "catalog dataset paths differ from the preregistered event sources"
            )
    catalog_inputs = [catalog_path]
    input_paths["catalog_000"] = catalog_path

    study_model = _mapping(selection.get("study_model_identity"), "study_model_identity")
    execution_mode = _text(
        study_model.get("execution_mode"), "study_model_identity.execution_mode"
    )
    if execution_mode not in {"mock", "openai_live"}:
        raise MultiEventInputError(
            "study_model_identity.execution_mode must be mock or openai_live"
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
        raise MultiEventInputError("mock execution must retain mock provider identity")
    if execution_mode == "openai_live" and study_model.get("requested_provider") != "openai":
        raise MultiEventInputError("openai_live execution must request provider=openai")
    study_reported_aliases = _list(
        study_model.get("reported_model_aliases"),
        "study_model_identity.reported_model_aliases",
    )
    if study_reported_aliases != sorted(set(study_reported_aliases)) or any(
        not isinstance(alias, str) or not alias for alias in study_reported_aliases
    ):
        raise MultiEventInputError(
            "study reported_model_aliases must be a sorted unique string list"
        )
    if execution_mode == "mock" and study_reported_aliases:
        raise MultiEventInputError(
            "mock execution must not fabricate endpoint-reported model aliases"
        )

    raw_children = _list(selection.get("children"), "children")
    raw_missing = _list(
        selection.get("missing_or_rejected_slots"), "missing_or_rejected_slots"
    )
    _validate_selection_partition(
        raw_children, raw_missing, protocol, execution_plan
    )
    declared_missing: list[Mapping[str, Any]] = []
    for raw in raw_missing:
        item = _mapping(raw, "missing_or_rejected_slots[]")
        event_id, arm, seed, repeat_idx = _selection_slot(
            item, "missing_or_rejected_slots[]"
        )
        attempts = item.get("attempt_run_ids", [])
        if not isinstance(attempts, list) or any(
            not isinstance(run_id, str) or not run_id for run_id in attempts
        ):
            raise MultiEventInputError("attempt_run_ids must be a list of run ids")
        declared_missing.append(
            {
                "event_id": event_id,
                "arm": arm,
                "seed": seed,
                "repeat_idx": repeat_idx,
                "run_id": None,
                "slot_status": item["status"],
                "attempt_run_ids": list(attempts),
                "reason_codes": list(item["reason_codes"]),
            }
        )

    required_identity = tuple(protocol["analysis_input_contract"]["required_child_identity"])
    frozen_population_identity = expected_population_identity(protocol)
    children: list[ChildSelection] = []
    seen_cells: set[tuple[str, str, int, int]] = set()
    seen_run_ids: set[str] = set()
    child_reported_aliases: set[str] = set()
    for index, raw in enumerate(raw_children):
        item = _mapping(raw, "children[]")
        event_id = _text(item.get("event_id"), "children[].event_id")
        arm = _text(item.get("arm"), "children[].arm")
        seed = _integer(item.get("seed"), "children[].seed")
        repeat_idx = _integer(item.get("repeat_idx"), "children[].repeat_idx")
        cell = (event_id, arm, seed, repeat_idx)
        if (
            event_id not in planned_events
            or arm not in ARMS
            or seed not in planned_seeds
            or repeat_idx not in planned_repeats
        ):
            raise MultiEventInputError("child names a cell outside the preregistered grid")
        if cell in seen_cells:
            raise MultiEventInputError("duplicate planned cell in children")
        seen_cells.add(cell)

        manifest_path = _resolve_explicit(
            Path(child_root), item.get("manifest_path"), "children[].manifest_path"
        )
        if manifest_path.name != "run_manifest.json":
            raise MultiEventInputError("child manifest_path must name run_manifest.json")
        manifest_hash = _sha256(item.get("manifest_sha256"), "manifest_sha256")
        result = _mapping(item.get("result_artifact"), "children[].result_artifact")
        if result.get("path") != RESULT_ARTIFACT:
            raise MultiEventInputError("result_artifact.path must be experiment_result.json")
        result_hash = _sha256(result.get("sha256"), "result_artifact.sha256")
        identity = _mapping(item.get("identity"), "children[].identity")
        if set(required_identity) - set(identity):
            raise MultiEventInputError("child identity is missing required fields")
        run_id = _text(identity.get("run_id"), "children[].identity.run_id")
        if run_id in seen_run_ids:
            raise MultiEventInputError("one managed run cannot fill multiple planned cells")
        seen_run_ids.add(run_id)
        if identity.get("command_identity") != RUN_SEED_COMMAND:
            raise MultiEventInputError("child command_identity must be experiments.run_seed")
        if identity.get("config_hash_schema_version") != CONFIG_HASH_SCHEMA_VERSION:
            raise MultiEventInputError("child config hash schema is not supported")
        hash_fields = {
            "scientific_config_hash",
            "model_request_config_hash",
            "scientific_input_identity",
            "scenario_definition_hash",
            "population_identity",
            "endpoint_identity",
        }
        for field in hash_fields:
            _sha256(identity.get(field), f"children[].identity.{field}")
        if identity.get("population_identity") != frozen_population_identity:
            raise MultiEventInputError(
                "child population_identity does not match the frozen executable cast"
            )
        for field in (
            "requested_provider",
            "resolved_provider",
            "resolved_model",
        ):
            _text(identity.get(field), f"children[].identity.{field}")
        child_requested_model = identity.get("requested_model")
        if execution_mode == "openai_live":
            _text(child_requested_model, "children[].identity.requested_model")
        elif child_requested_model is not None and not isinstance(
            child_requested_model, str
        ):
            raise MultiEventInputError(
                "mock child requested_model must be null or a string"
            )
        reported_aliases = _list(
            identity.get("reported_model_aliases"),
            "children[].identity.reported_model_aliases",
        )
        if reported_aliases != sorted(set(reported_aliases)) or any(
            not isinstance(alias, str) or not alias for alias in reported_aliases
        ):
            raise MultiEventInputError(
                "child reported_model_aliases must be a sorted unique string list"
            )
        if execution_mode == "mock" and reported_aliases:
            raise MultiEventInputError("mock child must have reported_model_aliases=[]")
        if execution_mode == "openai_live" and len(reported_aliases) != 1:
            raise MultiEventInputError(
                "each openai_live child must have exactly one reported model alias"
            )
        child_reported_aliases.update(reported_aliases)
        for field in study_model_fields:
            if identity.get(field) != study_model.get(field):
                raise MultiEventInputError(
                    f"child identity does not match study_model_identity: {field}"
                )
        if identity.get("requested_model") != study_model.get("requested_model"):
            raise MultiEventInputError(
                "child identity does not match study requested_model"
            )
        child = ChildSelection(
            event_id,
            arm,
            seed,
            repeat_idx,
            manifest_path,
            manifest_hash,
            result_hash,
            dict(identity),
        )
        children.append(child)
        input_paths[f"child_manifest_{index:03d}"] = manifest_path
        input_paths[f"child_result_{index:03d}"] = manifest_path.parent / RESULT_ARTIFACT

    if sorted(child_reported_aliases) != study_reported_aliases:
        raise MultiEventInputError(
            "study reported_model_aliases must equal the accepted-child union"
        )

    return PreparedSelection(
        expected_protocol_hash,
        selection_path,
        Path(child_root).resolve(strict=True),
        Path(reference_root).resolve(strict=True),
        events,
        execution_plan,
        dict(study_model),
        tuple(children),
        tuple(declared_missing),
        tuple(catalog_inputs),
        input_paths,
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
        try:
            path = (run_dir / relative).resolve(strict=True)
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
        artifacts[key] = path
    required = {
        DRIVER_PLAN_FILENAME,
        DRIVER_SELECTION_FILENAME,
        DRIVER_ATTEMPT_LEDGER_FILENAME,
        DRIVER_SUMMARY_FILENAME,
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


def _driver_plan_cells(
    plan: Mapping[str, Any],
    *,
    protocol: Mapping[str, Any],
    execution_plan: Mapping[str, Any],
) -> tuple[Mapping[tuple[str, str, int, int], Mapping[str, Any]], set[tuple[str, str, int, int]]]:
    if plan.get("schema_version") != "multi_event_plan_v1":
        raise MultiEventInputError("driver plan schema_version is unsupported")
    if plan.get("protocol_sha256") != protocol_sha256():
        raise MultiEventInputError("driver plan protocol SHA-256 mismatch")
    if plan.get("dry_run") is not False or plan.get("pre_run_plan") is not True:
        raise MultiEventInputError("analysis requires a non-dry-run precommitted plan")
    listed_plan = _validated_execution_plan(plan.get("execution_plan"), protocol)
    if dict(listed_plan) != dict(execution_plan):
        raise MultiEventInputError("driver plan and selection execution plans disagree")
    jobs: dict[tuple[str, str, int, int], Mapping[str, Any]] = {}
    max_attempts = int(protocol["acceptance_and_execution"]["max_child_attempts"])
    for raw in _list(plan.get("jobs"), "driver plan jobs"):
        job = _mapping(raw, "driver plan jobs[]")
        cell = _selection_slot(job, "driver plan jobs[]")
        if cell in jobs:
            raise MultiEventInputError("driver plan contains duplicate cells")
        allowed = _list(
            job.get("allowed_attempt_run_ids"),
            "driver plan jobs[].allowed_attempt_run_ids",
        )
        if (
            len(allowed) != max_attempts
            or any(not isinstance(run_id, str) or not run_id for run_id in allowed)
            or len(allowed) != len(set(allowed))
        ):
            raise MultiEventInputError(
                "driver plan must freeze exactly five unique attempt run IDs per cell"
            )
        _text(job.get("attempt_series_id"), "driver plan jobs[].attempt_series_id")
        jobs[cell] = job
    planned = _planned_cells(protocol, execution_plan)
    if set(jobs) != planned:
        raise MultiEventInputError("driver plan jobs do not equal the execution grid")
    all_allowed = [
        run_id for job in jobs.values() for run_id in job["allowed_attempt_run_ids"]
    ]
    if len(all_allowed) != len(set(all_allowed)):
        raise MultiEventInputError("attempt run IDs are not unique across plan cells")
    return jobs, planned


def _validate_driver_attempt_ledger(
    records: Sequence[Mapping[str, Any]],
    *,
    jobs: Mapping[tuple[str, str, int, int], Mapping[str, Any]],
    selection: Mapping[str, Any],
) -> None:
    """Bind durable launch/terminal records to the selected ordered prefixes."""

    selected_by_cell: dict[tuple[str, str, int, int], Mapping[str, Any]] = {}
    for field in ("children", "missing_or_rejected_slots"):
        for raw in _list(selection.get(field), field):
            item = _mapping(raw, f"{field}[]")
            selected_by_cell[_selection_slot(item, f"{field}[]")] = item
    by_run_id: dict[str, list[Mapping[str, Any]]] = {}
    records_by_cell: dict[
        tuple[str, str, int, int], list[Mapping[str, Any]]
    ] = {cell: [] for cell in jobs}
    for raw in records:
        record = _mapping(raw, "driver attempt ledger[]")
        cell = _selection_slot(record, "driver attempt ledger[]")
        if cell not in jobs:
            raise MultiEventInputError("attempt ledger contains an unplanned cell")
        status = _text(record.get("status"), "attempt ledger status")
        if status not in {"launched", "accepted", "rejected", "not_launched"}:
            raise MultiEventInputError("attempt ledger status is unsupported")
        run_id = record.get("run_id")
        if run_id is None:
            if status != "not_launched":
                raise MultiEventInputError("only not_launched may omit run_id")
        else:
            run_id = _text(run_id, "attempt ledger run_id")
            if run_id not in jobs[cell]["allowed_attempt_run_ids"]:
                raise MultiEventInputError("attempt ledger run_id is not frozen by plan")
            by_run_id.setdefault(run_id, []).append(record)
        records_by_cell[cell].append(record)

    for cell, job in jobs.items():
        selected = selected_by_cell[cell]
        selected_attempts = list(selected.get("attempt_run_ids", []))
        allowed = list(job["allowed_attempt_run_ids"])
        if selected_attempts != allowed[: len(selected_attempts)]:
            raise MultiEventInputError(
                "selection attempt_run_ids are not the plan's contiguous prefix"
            )
        ledger_attempts = [
            run_id for run_id in allowed if run_id in by_run_id
        ]
        if ledger_attempts != selected_attempts:
            raise MultiEventInputError(
                "selection attempt prefix disagrees with durable attempt ledger"
            )
        accepted_run_ids: list[str] = []
        for run_id in selected_attempts:
            run_records = by_run_id[run_id]
            statuses = [str(record["status"]) for record in run_records]
            terminal = [status for status in statuses if status in {"accepted", "rejected"}]
            resumed_terminal_only = bool(
                len(statuses) == 1
                and terminal
                and run_records[0].get("source") == "resumed_attempt"
            )
            if not resumed_terminal_only and (
                statuses[0] != "launched"
                or len(statuses) != 2
                or len(terminal) != 1
            ):
                raise MultiEventInputError(
                    "each launched attempt requires one durable terminal transition"
                )
            if terminal == ["accepted"]:
                accepted_run_ids.append(run_id)
        if len(accepted_run_ids) > 1:
            raise MultiEventInputError("one plan cell has multiple accepted attempts")
        if accepted_run_ids and accepted_run_ids[0] != selected_attempts[-1]:
            raise MultiEventInputError("attempts continue after an accepted child")
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
                raise MultiEventInputError("missing selection cell has attempts")
            if status == "rejected" and not selected_attempts:
                raise MultiEventInputError("rejected selection cell has no attempts")


def prepare_driver_selection(
    driver_manifest_path: Path,
    *,
    protocol: Mapping[str, Any],
    protocol_path: Path,
    child_root: Path,
    reference_root: Path,
) -> PreparedSelection:
    """Derive analysis input only from one terminal registered driver parent."""

    child_root = Path(child_root).resolve(strict=True)
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
    if parent_identity.get("protocol_sha256") != expected_protocol_hash:
        raise MultiEventInputError("driver parent protocol SHA-256 mismatch")
    expected_input_hashes = {
        "protocol": expected_protocol_hash,
        "catalog": protocol["reference_data_catalog"]["sha256"],
    }
    for index, event in enumerate(protocol["design"]["events"]):
        expected_input_hashes[f"reference_{index:02d}"] = event[
            "reference_csv_sha256"
        ]
        expected_input_hashes[f"timeline_{index:02d}"] = event[
            "news_timeline_sha256"
        ]
    parent_inputs: dict[str, Mapping[str, Any]] = {}
    for raw in _list(manifest.get("inputs"), "driver manifest inputs"):
        item = _mapping(raw, "driver manifest inputs[]")
        label = _text(item.get("label"), "driver manifest input label")
        if label in parent_inputs:
            raise MultiEventInputError("driver manifest input label is duplicated")
        parent_inputs[label] = item
    if set(parent_inputs) != set(expected_input_hashes):
        raise MultiEventInputError("driver parent scientific input labels mismatch")
    for label, expected_hash in expected_input_hashes.items():
        item = parent_inputs[label]
        if (
            item.get("exists") is not True
            or item.get("kind") != "file"
            or item.get("error") is not None
            or item.get("sha256") != expected_hash
        ):
            raise MultiEventInputError(
                "driver parent scientific input identity mismatch"
            )

    artifacts = _registered_driver_artifacts(manifest, run_dir)
    selection_path = artifacts[DRIVER_SELECTION_FILENAME]
    plan = _read_json(artifacts[DRIVER_PLAN_FILENAME], "driver plan")
    selection = _read_json(selection_path, "driver selection")
    summary = _read_json(artifacts[DRIVER_SUMMARY_FILENAME], "driver summary")
    execution_plan = _validated_execution_plan(
        selection.get("execution_plan"), protocol
    )
    if (
        parent_identity.get("execution_mode") != execution_plan["execution_mode"]
        or parent_identity.get("protocol_adherence")
        is not execution_plan["protocol_adherence"]
    ):
        raise MultiEventInputError("driver parent execution identity mismatch")
    jobs, planned = _driver_plan_cells(
        plan, protocol=protocol, execution_plan=execution_plan
    )
    _validate_selection_partition(
        _list(selection.get("children"), "children"),
        _list(selection.get("missing_or_rejected_slots"), "missing_or_rejected_slots"),
        protocol,
        execution_plan,
    )
    ledger = _read_jsonl_objects(
        artifacts[DRIVER_ATTEMPT_LEDGER_FILENAME], "driver attempt ledger"
    )
    _validate_driver_attempt_ledger(ledger, jobs=jobs, selection=selection)

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
    if (
        summary.get("schema_version") != "1.0"
        or summary.get("run_id") != manifest["run_id"]
        or summary.get("driver") != DRIVER_COMMAND
        or summary.get("planned_runs") != len(planned)
        or summary.get("completed_runs") != accepted_count
        or summary.get("failed_runs") != failed_count
        or summary.get("honest_n_runs") != accepted_count
        or summary.get("multi_event_protocol_sha256") != expected_protocol_hash
        or summary.get("multi_event_selection") != DRIVER_SELECTION_FILENAME
        or summary.get("honest_n_complete_seed_pairs") != complete_pairs
        or summary.get("honest_n_complete_seed_pairs_by_event")
        != complete_pairs_by_event
        or summary.get("incomplete") is not bool(failed_count)
        or summary.get("reported_model_aliases")
        != selection.get("study_model_identity", {}).get("reported_model_aliases")
        or summary.get("underlying_model_identity_verified") is not False
        or summary.get("model_specific_inference_allowed") is not False
    ):
        raise MultiEventInputError("driver summary disagrees with registered selection")
    experiment_completion = _mapping(
        manifest.get("experiment_completion"), "experiment_completion"
    )
    if any(
        experiment_completion.get(field) != expected
        for field, expected in {
            "planned_runs": len(planned),
            "completed_runs": accepted_count,
            "failed_runs": failed_count,
            "honest_n_runs": accepted_count,
        }.items()
    ):
        raise MultiEventInputError(
            "driver manifest completion disagrees with selection"
        )

    prepared = prepare_selection(
        selection_path,
        protocol=protocol,
        protocol_path=protocol_path,
        child_root=child_root,
        reference_root=reference_root,
    )
    anchored_inputs = dict(prepared.input_paths)
    anchored_inputs.update(
        {
            "driver_parent_manifest": manifest_path,
            "driver_plan": artifacts[DRIVER_PLAN_FILENAME],
            "driver_attempt_ledger": artifacts[DRIVER_ATTEMPT_LEDGER_FILENAME],
            "driver_summary": artifacts[DRIVER_SUMMARY_FILENAME],
        }
    )
    return replace(
        prepared,
        input_paths=anchored_inputs,
        driver_manifest_path=manifest_path,
        driver_run_id=str(manifest["run_id"]),
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
    }


def _config_mismatches(
    manifest: Mapping[str, Any],
    protocol: Mapping[str, Any],
    selection: ChildSelection,
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
    event_bound = {"news_timeline", "reference_path"}
    for field, expected in scientific.items():
        if field in event_bound:
            continue
        wanted = factors.get(field, expected)
        actual = config.get(field)
        if field == "population":
            if (
                not isinstance(actual, Mapping)
                or actual != wanted
                or list(actual) != list(wanted)
            ):
                mismatches.append(f"config_mismatch:{field}")
        elif actual != wanted:
            mismatches.append(f"config_mismatch:{field}")
    model = protocol["effective_config_freeze"]["model_request"]
    strict_model_fields = ["temperature", "max_tokens", "cache_enabled"]
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
    reported = attempts.get("reported_models")
    if not isinstance(reported, list) or any(
        not isinstance(model, str) or not model for model in reported
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
    execution_mode: str,
) -> list[str]:
    """Bind manifest attempt evidence, selection identity, and result projection."""

    manifest_aliases = _reported_model_aliases_from_manifest(
        manifest, execution_mode=execution_mode
    )
    selected = list(selected_aliases)
    result = list(result_aliases)
    if manifest_aliases != selected or result != selected:
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
    return {
        "event_id": selection.event_id,
        "arm": selection.arm,
        "seed": selection.seed,
        "repeat_idx": selection.repeat_idx,
        "run_id": selection.expected_identity.get("run_id"),
        "reason_codes": list(dict.fromkeys(str(reason) for reason in reasons)),
    }


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
        candidate = ReusableRunCandidate(selected.manifest_path, prepared.child_root)
        try:
            child, _compatibility = load_child_run_identity(candidate)
        except ResultReuseError as error:
            rejections.append(_rejection(selected, [error.reason_code]))
            continue
        expected = ExpectedRunIdentity.from_child(
            child, required_artifacts=(RESULT_ARTIFACT,), git_commit=child.git_commit
        )
        decision = validate_child_run_reuse(candidate, expected)
        if not decision.reusable:
            reasons.extend(decision.reason_codes)
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
        reasons.extend(
            _config_mismatches(
                raw_manifest,
                protocol,
                selected,
                execution_mode=prepared.execution_plan["execution_mode"],
            )
        )
        event = prepared.events[selected.event_id]
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
            validate_reported_model_alias_binding(
                raw_manifest,
                selected_aliases=selected.expected_identity[
                    "reported_model_aliases"
                ],
                result_aliases=reported_aliases,
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
            health = _mapping(result.get("health"), "health")
            bad_frac = _finite(health.get("bad_frac"), "health.bad_frac")
            if bad_frac < 0 or bad_frac > 1:
                raise MultiEventInputError("health.bad_frac must be within [0,1]")
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
        if bad_frac > health_max:
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
                bad_frac,
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
) -> Mapping[str, Any]:
    """Pure event-complete aggregation with a stricter cross-event intersection."""

    design = protocol["design"]
    event_ids = [row["event_id"] for row in design["events"]]
    effective_plan = (
        _validated_execution_plan(execution_plan, protocol)
        if execution_plan is not None
        else {
            "protocol_adherence": True,
            "execution_mode": "openai_live",
            "seeds": list(design["seeds"]),
            "repeat_indices": list(design["repeat_indices"]),
            "planned_runs": int(design["planned_runs"]),
            "override_reason": None,
        }
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
    protocol_adherent_realism = bool(
        effective_plan["protocol_adherence"]
        and effective_plan["execution_mode"] == "openai_live"
    )
    return {
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
                else "engineering_only_nonadherent_or_mock_not_claim_eligible"
            ),
            "no_curve_fit": protocol["reference_phase_transform"]["no_curve_fit"],
        },
        "scientific_semantics_change": protocol["scientific_semantics_change"],
    }


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


def main(argv: Optional[Sequence[str]] = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
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
        prepared = prepare_driver_selection(
            Path(args.driver_manifest),
            protocol=protocol,
            protocol_path=protocol_path,
            child_root=Path(args.child_root),
            reference_root=Path(args.reference_root),
        )
    except (ManagedCLIError, MultiEventInputError, MultiEventProtocolError,
            ApprovalRequiredError, OSError, ValueError) as error:
        fail_cli(bootstrap, error, failure_stage="config_validation")

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
        "selection_mode": "explicit_manifest_no_glob",
        "trust_anchor": "finished_registered_experiments.multi_event_parent",
        "driver_run_id": prepared.driver_run_id,
        "driver_manifest_sha256": sha256_file(prepared.driver_manifest_path),
        "study_model_identity": dict(prepared.study_model_identity),
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
            )
            summary = dict(summary)
            summary["protocol_sha256"] = prepared.protocol_sha256
            summary["selection_manifest_sha256"] = sha256_file(prepared.selection_path)
            summary["driver_parent_run_id"] = prepared.driver_run_id
            summary["driver_parent_manifest_sha256"] = sha256_file(
                prepared.driver_manifest_path
            )
            summary["study_model_identity"] = dict(prepared.study_model_identity)
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
    print(
        "multi-event analysis complete: cross_event_complete_seed_clusters={} -> {}".format(
            summary["honest_n"]["cross_event_complete_seed_clusters"],
            managed.run_dir,
        )
    )


if __name__ == "__main__":
    main()
