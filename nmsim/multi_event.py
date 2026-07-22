"""Frozen multi-event transform and replicate-slot identity contracts.

This module is provider-free.  It converts an explicitly loaded
``ReferenceEpisode`` into the preregistered 24-round event phase and binds the
source artifacts, transform, and replicate slot with canonical SHA-256
identities.  It never discovers sibling inputs and never writes a result.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping

from .config import Config, NewsTimelineEntry, normalize_news_timeline
from .decision_contract import MULTI_EVENT_DECISION_RESPONSE_SCHEMA
from .config_contract import (
    CONFIG_FIELD_RULES,
    CONFIG_HASH_SCHEMA_VERSION,
    SCIENTIFIC,
)
from .provenance import sha256_file
from .reference_data import ReferenceDataError, ReferenceEpisode, load_reference_episode
from .validation import norm_log_path


PROTOCOL_SCHEMA_VERSION = "multi_event_protocol_v1"
SLOT_SCHEMA_VERSION = "multi_event_slot_v1"
ATTEMPT_SERIES_SCHEMA_VERSION = "multi_event_attempt_series_v1"
TRANSFORM_ID = "event_phase_normalized_log_linear_25_v1"
TRANSFORM_N_ROUNDS = 24
TRANSFORM_POINT_COUNT = 25
FROZEN_PROTOCOL_SHA256 = (
    "1cff28c0af7ef4c0874b86d0b7d660e0d15f4f208d8f00ac7e93e71a50a39e72"
)

_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_EVENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SLOT_KEYS = frozenset(
    {
        "schema_version",
        "protocol_hash",
        "event_id",
        "social_arm",
        "seed",
        "repeat_idx",
        "slot_id",
    }
)


class MultiEventProtocolError(ValueError):
    """Protocol, catalog, or multi-event identity validation failed."""


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def stable_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _required_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MultiEventProtocolError(f"{field} must be an object")
    return value


def _required_int(value: Any, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise MultiEventProtocolError(
            f"{field} must be an integer >= {minimum}"
        )
    return value


def _load_json_object(path: Path, *, field: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise MultiEventProtocolError(f"cannot load {field}") from error
    if not isinstance(value, dict):
        raise MultiEventProtocolError(f"{field} must contain a JSON object")
    return value


@dataclass(frozen=True)
class TransformedReferenceEpisode:
    """A 25-point normalized-log reference and mapped public timeline."""

    transform_id: str
    terminal_t: int
    norm_log_path: tuple[float, ...]
    normalized_price_path: tuple[float, ...]
    news_timeline: tuple[NewsTimelineEntry, ...]
    reference_path_hash: str
    source_timeline_hash: str | None
    transformed_reference_hash: str
    transformed_timeline_hash: str
    transform_hash: str
    timeline_enabled: bool


@dataclass(frozen=True)
class MultiEventMaterial:
    """Validated source and derived identities for one catalog event."""

    event_id: str
    reference_csv: Path
    news_timeline_jsonl: Path
    protocol_path: Path
    catalog_path: Path
    protocol: Mapping[str, Any]
    catalog: Mapping[str, Any]
    dataset: Mapping[str, Any]
    protocol_hash: str
    catalog_hash: str
    reference_hash: str
    timeline_hash: str
    event_definition_hash: str
    reference_transform_sha256: str
    transformed: TransformedReferenceEpisode


def _post_t0_log_path(episode: ReferenceEpisode) -> tuple[list[float], int]:
    post = sorted(
        (point for point in episode.points if point.t >= 0),
        key=lambda point: point.t,
    )
    if not post or post[0].t != 0:
        raise MultiEventProtocolError("reference episode has no t=0 point")
    terminal_t = post[-1].t
    by_t = {point.t: point for point in post}
    if tuple(sorted(by_t)) != tuple(range(terminal_t + 1)):
        raise MultiEventProtocolError(
            "reference episode post-t0 positions must be contiguous"
        )
    base = by_t[0].price
    return [math.log(by_t[t].price / base) for t in range(terminal_t + 1)], terminal_t


def resample_reference_log_path(
    prices: list[float],
    shock_idx: int,
    target_points: int = TRANSFORM_POINT_COUNT,
) -> list[float]:
    """Analyzer-compatible full-horizon normalized-log interpolation."""

    if isinstance(target_points, bool) or not isinstance(target_points, int):
        raise MultiEventProtocolError("target_points must be an integer")
    if target_points < 2:
        raise MultiEventProtocolError("target_points must be >= 2")
    raw = norm_log_path(prices, shock_idx)
    if not raw:
        raise MultiEventProtocolError("reference path is empty")
    if len(raw) == 1:
        return [float(raw[0])] * target_points
    terminal = len(raw) - 1
    transformed: list[float] = []
    for target_t in range(target_points):
        coordinate = target_t * terminal / (target_points - 1)
        lower = int(math.floor(coordinate))
        upper = int(math.ceil(coordinate))
        fraction = coordinate - lower
        transformed.append(
            float(raw[lower] + fraction * (raw[upper] - raw[lower]))
        )
    return transformed


def reference_transform_identity(
    event_id: str,
    reference_csv_sha256: str,
    transformed_log_path: list[float] | tuple[float, ...],
) -> str:
    """Return the analyzer-compatible v1 transform digest."""

    payload = {
        "schema_version": "1.0",
        "transform_id": TRANSFORM_ID,
        "event_id": str(event_id),
        "source_reference_csv_sha256": str(reference_csv_sha256),
        "method": (
            "linear_interpolation_in_normalized_log_price_over_full_post_t0_horizon"
        ),
        "target_points": TRANSFORM_POINT_COUNT,
        "norm_log_path": [float(value) for value in transformed_log_path],
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def transform_reference_episode(
    episode: ReferenceEpisode,
    *,
    reference_path_hash: str,
    source_timeline_hash: str | None = None,
    include_timeline: bool = True,
) -> TransformedReferenceEpisode:
    """Apply the frozen v1 event-phase transform without curve fitting.

    Source position for simulator point ``t`` is exactly
    ``t * terminal_t / 24``.  Values are linearly interpolated in normalized
    log-price space.  Public events use ``delivery_t`` only; ``price_anchor_t``
    is never consulted for delivery.
    """

    if not _HEX_SHA256.fullmatch(str(reference_path_hash)):
        raise MultiEventProtocolError("reference_path_hash must be SHA-256")
    if source_timeline_hash is not None and not _HEX_SHA256.fullmatch(
        str(source_timeline_hash)
    ):
        raise MultiEventProtocolError("source_timeline_hash must be SHA-256")

    _source_log, terminal_t = _post_t0_log_path(episode)
    if terminal_t <= 0:
        raise MultiEventProtocolError(
            "event-phase transform requires terminal_t > 0"
        )
    values = resample_reference_log_path(
        episode.prices,
        episode.shock_idx,
        target_points=TRANSFORM_POINT_COUNT,
    )

    mapped = ()
    if include_timeline:
        mapped = normalize_news_timeline(
            (
                {
                    "event_id": event.event_id,
                    "round": min(
                        TRANSFORM_N_ROUNDS,
                        1
                        + math.floor(
                            (max(1, event.delivery_t) - 1)
                            * TRANSFORM_N_ROUNDS
                            / terminal_t
                        ),
                    ),
                    "public_text": event.public_text,
                }
                for event in episode.news_timeline
            ),
            n_rounds=TRANSFORM_N_ROUNDS,
        )

    norm_log_path = tuple(values)
    normalized_prices = tuple(math.exp(value) for value in norm_log_path)
    timeline_payload = [
        {
            "event_id": event.event_id,
            "round": event.round,
            "public_text": event.public_text,
        }
        for event in mapped
    ]
    transformed_reference_hash = stable_hash(
        {
            "coordinate": "normalized_log_price",
            "point_count": TRANSFORM_POINT_COUNT,
            "values": norm_log_path,
        }
    )
    transformed_timeline_hash = stable_hash(timeline_payload)
    transform_hash = stable_hash(
        {
            "transform_id": TRANSFORM_ID,
            "reference_path_hash": reference_path_hash,
            "source_timeline_hash": source_timeline_hash,
            "terminal_t": terminal_t,
            "n_rounds": TRANSFORM_N_ROUNDS,
            "interpolation": "linear_normalized_log_price",
            "source_position": "t*terminal_t/24",
            "timeline_delivery": "min(24,1+floor((max(1,d)-1)*24/terminal_t))",
            "timeline_enabled": bool(include_timeline),
            "transformed_reference_hash": transformed_reference_hash,
            "transformed_timeline_hash": transformed_timeline_hash,
        }
    )
    return TransformedReferenceEpisode(
        transform_id=TRANSFORM_ID,
        terminal_t=terminal_t,
        norm_log_path=norm_log_path,
        normalized_price_path=normalized_prices,
        news_timeline=mapped,
        reference_path_hash=reference_path_hash,
        source_timeline_hash=source_timeline_hash,
        transformed_reference_hash=transformed_reference_hash,
        transformed_timeline_hash=transformed_timeline_hash,
        transform_hash=transform_hash,
        timeline_enabled=bool(include_timeline),
    )


def load_protocol(path: str | Path) -> tuple[dict[str, Any], str]:
    protocol_path = Path(path).resolve()
    value = _load_json_object(protocol_path, field="protocol")
    protocol_hash = sha256_file(protocol_path)
    if protocol_hash != FROZEN_PROTOCOL_SHA256:
        raise MultiEventProtocolError(
            "protocol bytes differ from the preregistered frozen artifact"
        )
    if value.get("schema_version") != PROTOCOL_SCHEMA_VERSION:
        raise MultiEventProtocolError("unsupported multi-event protocol schema")
    if (
        value.get("protocol_id") != "multi_event_distribution_v1"
        or value.get("protocol_status")
        != "preregistered_before_live_multi_event_grid"
        or value.get("study_status")
        != "preregistered_variance_components_pilot"
        or value.get("confirmatory") is not False
    ):
        raise MultiEventProtocolError("multi-event pilot status changed")
    design = _required_mapping(value.get("design"), "design")
    events = design.get("events")
    if not isinstance(events, list):
        raise MultiEventProtocolError("protocol design.events must be an array")
    event_ids = [
        item.get("event_id") if isinstance(item, Mapping) else None
        for item in events
    ]
    if (
        not isinstance(event_ids, list)
        or len(event_ids) != 3
        or len(set(event_ids)) != len(event_ids)
        or not all(isinstance(item, str) and _SAFE_EVENT_ID.fullmatch(item) for item in event_ids)
    ):
        raise MultiEventProtocolError("protocol event_ids must contain three unique safe IDs")
    expected_sources = {
        "meta_2022_02_crash_v1": (
            "nmsim/reference_data/v1/meta_2022_02_crash.csv",
            "nmsim/reference_data/v1/meta_2022_02_crash_news_timeline.jsonl",
            "5f0a39c4cff4cc70d732cea1518266b9502ddf7e806e4e7127f252394f09319a",
            "27a0a9794e3b86b6b841617cf62fb2255e3e497f9f8c51cdd0a709254503b9b2",
        ),
        "spy_2020_03_covid_v_recovery_v1": (
            "nmsim/reference_data/v1/spy_2020_03_covid_v_recovery.csv",
            "nmsim/reference_data/v1/spy_2020_03_covid_v_recovery_news_timeline.jsonl",
            "6b4615af3eda402803852143d29a307a9c8090114bb9d6891451dba0e8fcb24d",
            "2b2cf2bce5d5c0fb1f939bc3068d68e35424e44d2923adf86ddb437ceace70aa",
        ),
        "meta_2023_02_efficiency_jump_v1": (
            "nmsim/reference_data/v1/meta_2023_02_efficiency_jump.csv",
            "nmsim/reference_data/v1/meta_2023_02_efficiency_jump_news_timeline.jsonl",
            "5f419bbd078ef61f3819d8a14dca593cc1fe7e4e319843254327b81689d47a88",
            "4c3e6a63fa280f0bbd41d00d90eaedbdbcc4367b4b6aad3218ac29ee73922706",
        ),
    }
    if set(event_ids) != set(expected_sources):
        raise MultiEventProtocolError("frozen event IDs changed")
    for item in events:
        if (
            item.get("reference_csv"),
            item.get("news_timeline"),
            item.get("reference_csv_sha256"),
            item.get("news_timeline_sha256"),
        ) != expected_sources[item["event_id"]]:
            raise MultiEventProtocolError("frozen event source path changed")
    arms = design.get("arms")
    if arms != {"social_off": False, "social_on": True}:
        raise MultiEventProtocolError(
            "protocol design.arms must freeze social_off/social_on"
        )
    seeds = design.get("seeds")
    if seeds != [11, 13, 17, 19, 23, 29, 31, 37]:
        raise MultiEventProtocolError("protocol frozen seed grid changed")
    repeat_indices = design.get("repeat_indices")
    if repeat_indices != [1, 2, 3]:
        raise MultiEventProtocolError("protocol repeat_indices must be [1,2,3]")
    if design.get("N") != 8 or design.get("K") != 3:
        raise MultiEventProtocolError("protocol N/K must be 8/3")
    if design.get("planned_runs") != 144:
        raise MultiEventProtocolError("protocol planned_runs must be 144")
    if design.get("primary_unit") != (
        "event_seed_complete_across_both_arms_and_all_repeats"
    ):
        raise MultiEventProtocolError("protocol primary complete-case unit changed")
    catalog = _required_mapping(
        value.get("reference_data_catalog"), "reference_data_catalog"
    )
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
        raise MultiEventProtocolError("authoritative reference catalog changed")
    transform = _required_mapping(
        value.get("reference_phase_transform"), "reference_phase_transform"
    )
    if (
        transform.get("transform_id", TRANSFORM_ID) != TRANSFORM_ID
        or transform.get("target_points") != TRANSFORM_POINT_COUNT
        or transform.get("method")
        != "linear_interpolation_in_normalized_log_price_over_full_post_t0_horizon"
    ):
        raise MultiEventProtocolError("protocol reference transform is not frozen v1")
    execution = _required_mapping(
        value.get("acceptance_and_execution"), "acceptance_and_execution"
    )
    for field, expected in {
        "max_child_attempts": 5,
        "health_bad_frac_max": 0.15,
        "workers": 1,
        "cache_enabled": False,
        "temperature": 0.3,
    }.items():
        if execution.get(field) != expected:
            raise MultiEventProtocolError(
                f"protocol acceptance field changed: {field}"
            )
    freeze = _required_mapping(
        value.get("effective_config_freeze"), "effective_config_freeze"
    )
    scientific = _required_mapping(freeze.get("scientific"), "scientific freeze")
    model_request = _required_mapping(
        freeze.get("model_request"), "model_request freeze"
    )
    if freeze.get("config_hash_schema_version") != CONFIG_HASH_SCHEMA_VERSION:
        raise MultiEventProtocolError("protocol Config hash schema changed")
    classified = {
        name
        for name, rule in CONFIG_FIELD_RULES.items()
        if rule.category == SCIENTIFIC
    }
    if set(scientific) != classified:
        raise MultiEventProtocolError(
            "protocol does not cover the exact scientific Config field set"
        )
    if scientific.get("n_rounds") != 24 or scientific.get("news_round") != 1:
        raise MultiEventProtocolError("protocol round schedule must be 24/1")
    if scientific.get("news_text") != "":
        raise MultiEventProtocolError("protocol news_text must be empty")
    defaults = Config()
    factor_fields = {
        "seed",
        "population",
        "social_enabled",
        "reference_path",
        "news_timeline",
    }
    overrides = {
        "seed_fraction": 2.0 / 30.0,
        "news_round": 1,
        "news_text": "",
        "decision_response_schema": MULTI_EVENT_DECISION_RESPONSE_SCHEMA,
    }
    for field in sorted(classified - factor_fields):
        expected = overrides.get(field, getattr(defaults, field))
        if scientific.get(field) != expected:
            raise MultiEventProtocolError(
                f"frozen scientific Config changed: {field}"
            )
    if scientific.get("seed") != {
        "factor": "design.seeds",
        "values": seeds,
    }:
        raise MultiEventProtocolError("protocol seed factor changed")
    if scientific.get("social_enabled") != {
        "factor": "design.arms",
        "values": {"social_off": False, "social_on": True},
    }:
        raise MultiEventProtocolError("protocol social factor changed")
    if scientific.get("population") != {
        "influencer_amplifier": 1,
        "retail_crowd": 7,
        "fomo_momentum": 7,
        "value_institution": 5,
        "contrarian_fund": 5,
        "quant_arb": 5,
    }:
        raise MultiEventProtocolError("protocol population changed")
    if not all(
        isinstance(scientific.get(field), Mapping)
        for field in ("news_timeline", "reference_path")
    ):
        raise MultiEventProtocolError("event-bound scientific inputs changed")
    if model_request.get("cache_enabled") is not False:
        raise MultiEventProtocolError("protocol cache must be disabled")
    if float(model_request.get("temperature", -1)) != 0.3:
        raise MultiEventProtocolError("protocol temperature must be 0.3")
    for field, expected in {
        "provider": "openai",
        "model": "MiniMax-M2.7",
        "cheap_model": "",
        "use_cheap_model": False,
        "openai_base_url": defaults.openai_base_url,
        "openai_model": "MiniMax-M2.7",
        "temperature": 0.3,
        "max_tokens": 1024,
        "cache_enabled": False,
        "provider_sdk_max_retries": 0,
    }.items():
        if model_request.get(field) != expected:
            raise MultiEventProtocolError(
                f"frozen model request changed: {field}"
            )
    frozen_execution = _required_mapping(
        freeze.get("execution"), "execution freeze"
    )
    if frozen_execution.get("out_dir") != (
        "canonical_repository_root/results_multi_event_no_symlink_or_override_for_live"
    ):
        raise MultiEventProtocolError("canonical live output root policy changed")
    return value, protocol_hash


def load_multi_event_material(
    *,
    event_id: str,
    reference_csv: str | Path,
    news_timeline_jsonl: str | Path,
    protocol_path: str | Path,
    catalog_path: str | Path,
    include_timeline: bool = True,
) -> MultiEventMaterial:
    """Load exact catalog inputs and derive all content identities."""

    if not isinstance(event_id, str) or not _SAFE_EVENT_ID.fullmatch(event_id):
        raise MultiEventProtocolError("event_id is not a safe catalog ID")
    reference = Path(reference_csv).resolve()
    timeline = Path(news_timeline_jsonl).resolve()
    protocol_file = Path(protocol_path).resolve()
    catalog_file = Path(catalog_path).resolve()
    protocol, protocol_hash = load_protocol(protocol_file)
    protocol_events = [
        item
        for item in protocol["design"]["events"]
        if item["event_id"] == event_id
    ]
    if len(protocol_events) != 1:
        raise MultiEventProtocolError("event_id is not preregistered by protocol")
    protocol_event = protocol_events[0]
    repo_root = Path(__file__).resolve().parent.parent
    expected_catalog = (
        repo_root / protocol["reference_data_catalog"]["path"]
    ).resolve()
    expected_protocol_reference = (
        repo_root / protocol_event["reference_csv"]
    ).resolve()
    expected_protocol_timeline = (
        repo_root / protocol_event["news_timeline"]
    ).resolve()
    if catalog_file != expected_catalog:
        raise MultiEventProtocolError("catalog path differs from frozen protocol")
    if reference != expected_protocol_reference:
        raise MultiEventProtocolError(
            "reference_csv path differs from frozen protocol"
        )
    if timeline != expected_protocol_timeline:
        raise MultiEventProtocolError(
            "news_timeline path differs from frozen protocol"
        )
    catalog = _load_json_object(catalog_file, field="catalog")
    if catalog.get("schema_version") != "reference_data_catalog_v1":
        raise MultiEventProtocolError("unsupported reference catalog schema")
    datasets = catalog.get("datasets")
    if not isinstance(datasets, list):
        raise MultiEventProtocolError("catalog datasets must be an array")
    matches = [
        item
        for item in datasets
        if isinstance(item, Mapping) and item.get("dataset_id") == event_id
    ]
    if len(matches) != 1:
        raise MultiEventProtocolError("event_id must resolve to one catalog dataset")
    dataset = matches[0]
    expected_reference = (catalog_file.parent / str(dataset.get("reference_csv"))).resolve()
    expected_timeline = (
        catalog_file.parent / str(dataset.get("news_timeline_jsonl"))
    ).resolve()
    if reference != expected_reference:
        raise MultiEventProtocolError("reference_csv does not match catalog event")
    if timeline != expected_timeline:
        raise MultiEventProtocolError(
            "news_timeline_jsonl does not match catalog event"
        )
    for path, field in ((reference, "reference_csv"), (timeline, "news_timeline_jsonl")):
        if not path.is_file():
            raise MultiEventProtocolError(f"{field} is not a regular file")

    reference_hash = sha256_file(reference)
    timeline_hash = sha256_file(timeline)
    catalog_hash = sha256_file(catalog_file)
    if catalog_hash != protocol["reference_data_catalog"]["sha256"]:
        raise MultiEventProtocolError("catalog bytes differ from frozen protocol")
    if reference_hash != protocol_event["reference_csv_sha256"]:
        raise MultiEventProtocolError(
            "reference CSV bytes differ from frozen protocol"
        )
    if timeline_hash != protocol_event["news_timeline_sha256"]:
        raise MultiEventProtocolError(
            "news timeline bytes differ from frozen protocol"
        )
    try:
        episode = load_reference_episode(
            reference,
            news_timeline_path=timeline,
            include_news_timeline=True,
        )
    except ReferenceDataError as error:
        raise MultiEventProtocolError("event source data failed validation") from error
    transformed = transform_reference_episode(
        episode,
        reference_path_hash=reference_hash,
        source_timeline_hash=timeline_hash,
        include_timeline=include_timeline,
    )
    reference_transform_sha256 = reference_transform_identity(
        event_id, reference_hash, transformed.norm_log_path
    )
    event_definition_hash = stable_hash(
        {
            "catalog_schema_version": catalog.get("schema_version"),
            "dataset": dataset,
            "reference_hash": reference_hash,
            "timeline_hash": timeline_hash,
        }
    )
    return MultiEventMaterial(
        event_id=event_id,
        reference_csv=reference,
        news_timeline_jsonl=timeline,
        protocol_path=protocol_file,
        catalog_path=catalog_file,
        protocol=protocol,
        catalog=catalog,
        dataset=dataset,
        protocol_hash=protocol_hash,
        catalog_hash=catalog_hash,
        reference_hash=reference_hash,
        timeline_hash=timeline_hash,
        event_definition_hash=event_definition_hash,
        reference_transform_sha256=reference_transform_sha256,
        transformed=transformed,
    )


def build_experiment_slot(
    *,
    protocol_hash: str,
    event_id: str,
    social_arm: str,
    seed: int,
    repeat_idx: int,
) -> dict[str, Any]:
    """Return the canonical first-class replicate slot identity."""

    if not _HEX_SHA256.fullmatch(str(protocol_hash)):
        raise MultiEventProtocolError("protocol_hash must be SHA-256")
    if not isinstance(event_id, str) or not _SAFE_EVENT_ID.fullmatch(event_id):
        raise MultiEventProtocolError("event_id is not safe")
    if social_arm not in {"social_on", "social_off"}:
        raise MultiEventProtocolError("social_arm must be social_on or social_off")
    seed_value = _required_int(seed, "seed")
    repeat_value = _required_int(repeat_idx, "repeat_idx", minimum=1)
    payload = {
        "schema_version": SLOT_SCHEMA_VERSION,
        "protocol_hash": str(protocol_hash),
        "event_id": event_id,
        "social_arm": social_arm,
        "seed": seed_value,
        "repeat_idx": repeat_value,
    }
    return {**payload, "slot_id": stable_hash(payload)}


def validate_experiment_slot(value: Any) -> dict[str, Any]:
    """Fail closed unless a slot has the exact v1 shape and digest."""

    if not isinstance(value, Mapping) or frozenset(value.keys()) != _SLOT_KEYS:
        raise MultiEventProtocolError("experiment slot has invalid keys")
    expected = build_experiment_slot(
        protocol_hash=value.get("protocol_hash"),
        event_id=value.get("event_id"),
        social_arm=value.get("social_arm"),
        seed=value.get("seed"),
        repeat_idx=value.get("repeat_idx"),
    )
    if dict(value) != expected:
        raise MultiEventProtocolError("experiment slot digest mismatch")
    return expected


def canonical_multi_event_basename(slot: Mapping[str, Any]) -> str:
    normalized = validate_experiment_slot(slot)
    return "me_{}_{}_s{}_r{}.json".format(
        normalized["event_id"],
        normalized["social_arm"],
        normalized["seed"],
        normalized["repeat_idx"],
    )


def build_attempt_series_id(
    slot: Mapping[str, Any], expected_identity: Any
) -> str:
    """Bind one bounded retry series to a slot and full expected child identity.

    Output placement, worker count, and Git commit are deliberately excluded;
    scientific source/schema/config/model/input/material identities are not.
    """

    normalized_slot = validate_experiment_slot(slot)
    expected = expected_identity
    material = getattr(expected, "multi_event_material_identity", None)
    if not isinstance(material, Mapping):
        raise MultiEventProtocolError(
            "expected identity lacks multi-event material identity"
        )
    runtime_identity = getattr(
        expected, "scientific_runtime_environment_identity", None
    )
    if not isinstance(runtime_identity, str) or not _HEX_SHA256.fullmatch(
        runtime_identity
    ):
        raise MultiEventProtocolError(
            "expected identity lacks scientific runtime environment identity"
        )
    payload = {
        "schema_version": ATTEMPT_SERIES_SCHEMA_VERSION,
        "experiment_slot": normalized_slot,
        "command_identity": expected.command_identity,
        "manifest_schema_version": expected.manifest_schema_version,
        "recording_schema_version": expected.recording_schema_version,
        "scientific_component_fingerprint": (
            expected.scientific_component_fingerprint
        ),
        "decision_parser_schema_version": (
            expected.decision_parser_schema_version
        ),
        "decision_parser_source_hash": expected.decision_parser_source_hash,
        "event_schema_version": expected.event_schema_version,
        "prompt_source_hash": expected.prompt_source_hash,
        "persona_source_hash": expected.persona_source_hash,
        "simulation_core_source_hash": expected.simulation_core_source_hash,
        "config_hash_schema_version": expected.config_hash_schema_version,
        "scientific_config_hash": expected.scientific_config_hash,
        "model_request_config_hash": expected.model_request_config_hash,
        "scientific_input_identity": expected.scientific_input_identity,
        "scenario_definition_hash": expected.scenario_definition_hash,
        "population_identity": expected.population_identity,
        "requested_provider": expected.requested_provider,
        "resolved_provider": expected.resolved_provider,
        "requested_model": expected.requested_model,
        "resolved_model": expected.resolved_model,
        "endpoint_identity": expected.endpoint_identity,
        "temperature": expected.temperature,
        "max_tokens": expected.max_tokens,
        "cache_enabled": expected.cache_enabled,
        "scientific_runtime_environment_identity": (
            runtime_identity
        ),
        "multi_event_material_identity": dict(material),
    }
    return stable_hash(payload)


def build_attempt_run_id(
    slot: Mapping[str, Any], attempt_series_id: str, attempt_index: int
) -> str:
    """Return one of the deterministic, full-digest retry run identities."""

    normalized_slot = validate_experiment_slot(slot)
    if not isinstance(attempt_series_id, str) or not _HEX_SHA256.fullmatch(
        attempt_series_id
    ):
        raise MultiEventProtocolError("attempt_series_id must be SHA-256")
    index = _required_int(attempt_index, "attempt_index", minimum=1)
    return "me-{}-{}-ta{}".format(
        normalized_slot["slot_id"], attempt_series_id, index
    )


def multi_event_result_identity(
    material: MultiEventMaterial, slot: Mapping[str, Any]
) -> dict[str, Any]:
    """Return the exact analyzer-facing v1 result cell identity."""

    normalized = validate_experiment_slot(slot)
    if (
        normalized["protocol_hash"] != material.protocol_hash
        or normalized["event_id"] != material.event_id
    ):
        raise MultiEventProtocolError("slot does not bind the supplied event material")
    return {
        "schema_version": "1.0",
        "protocol_sha256": material.protocol_hash,
        "event_id": material.event_id,
        "arm": normalized["social_arm"],
        "seed": normalized["seed"],
        "repeat_idx": normalized["repeat_idx"],
        "reference_csv_sha256": material.reference_hash,
        "news_timeline_sha256": material.timeline_hash,
        "reference_transform_sha256": material.reference_transform_sha256,
    }


def multi_event_material_identity(
    material: MultiEventMaterial, slot: Mapping[str, Any]
) -> dict[str, Any]:
    """Bind every authoritative source and derived transform for one slot."""

    result_identity = multi_event_result_identity(material, slot)
    return {
        **result_identity,
        "catalog_sha256": material.catalog_hash,
        "event_definition_sha256": material.event_definition_hash,
        "reference_transform_id": material.transformed.transform_id,
        "timeline_transform_sha256": (
            material.transformed.transformed_timeline_hash
        ),
        "combined_transform_sha256": material.transformed.transform_hash,
    }


__all__ = [
    "MultiEventMaterial",
    "MultiEventProtocolError",
    "ATTEMPT_SERIES_SCHEMA_VERSION",
    "PROTOCOL_SCHEMA_VERSION",
    "SLOT_SCHEMA_VERSION",
    "TRANSFORM_ID",
    "TRANSFORM_N_ROUNDS",
    "TRANSFORM_POINT_COUNT",
    "TransformedReferenceEpisode",
    "build_experiment_slot",
    "build_attempt_run_id",
    "build_attempt_series_id",
    "canonical_multi_event_basename",
    "load_multi_event_material",
    "load_protocol",
    "multi_event_material_identity",
    "multi_event_result_identity",
    "reference_transform_identity",
    "resample_reference_log_path",
    "stable_hash",
    "transform_reference_episode",
    "validate_experiment_slot",
]
