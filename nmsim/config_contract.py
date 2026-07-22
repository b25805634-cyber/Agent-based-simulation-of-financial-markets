"""Canonical effective-configuration identity for strict replay.

The simulator's :class:`~nmsim.config.Config` mixes scientific, model-request,
and execution concerns.  This module classifies every field explicitly and
fails closed when the dataclass grows without a corresponding rule.  It does
not change configuration values or simulation behaviour.
"""
from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
import enum
import hashlib
import json
import math
import os
from pathlib import Path
from collections.abc import Mapping
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit


CONFIG_HASH_SCHEMA_VERSION = "1.0"
SCIENTIFIC = "scientific"
MODEL_REQUEST = "model_request"
EXECUTION = "execution"
CONFIG_CATEGORIES = (SCIENTIFIC, MODEL_REQUEST, EXECUTION)


class ConfigContractError(ValueError):
    """The effective Config cannot be represented by the replay contract."""


class UnclassifiedConfigFieldError(ConfigContractError):
    """Config contains a field with no explicit category and rationale."""


@dataclass(frozen=True)
class ConfigFieldRule:
    category: str
    rationale: str


# Keep this registry deliberately explicit.  There is no catch-all execution
# category: adding a Config dataclass field must update this table and tests.
CONFIG_FIELD_RULES: dict[str, ConfigFieldRule] = {
    "seed": ConfigFieldRule(SCIENTIFIC, "drives all simulator-local RNG streams and MockLLM"),
    "n_rounds": ConfigFieldRule(SCIENTIFIC, "sets the simulation horizon and terminal liquidation opportunity"),
    "news_round": ConfigFieldRule(SCIENTIFIC, "sets news delivery, margin gating, and metric alignment"),
    "news_text": ConfigFieldRule(SCIENTIFIC, "defines the scenario event delivered to informed agents"),
    "news_timeline": ConfigFieldRule(SCIENTIFIC, "defines the ordered cumulative public-event delivery mechanism"),
    "initial_price": ConfigFieldRule(SCIENTIFIC, "initialises price, portfolios, and leverage reference positions"),
    "fundamental_value": ConfigFieldRule(SCIENTIFIC, "enters the Mock observation and decision path"),
    "recent_window": ConfigFieldRule(SCIENTIFIC, "controls the price history visible to agents"),
    "kappa": ConfigFieldRule(SCIENTIFIC, "sets net-order-flow price impact"),
    "n_llm_agents": ConfigFieldRule(SCIENTIFIC, "sets the legacy persona population size"),
    "n_noise_agents": ConfigFieldRule(SCIENTIFIC, "sets the number of background noise traders"),
    "max_llm_agents": ConfigFieldRule(SCIENTIFIC, "caps the effective persona cast and request batch"),
    "population": ConfigFieldRule(SCIENTIFIC, "defines persona counts and the currently order-sensitive effective cast"),
    "provider": ConfigFieldRule(MODEL_REQUEST, "selects the requested LLM provider"),
    "model": ConfigFieldRule(MODEL_REQUEST, "selects an explicit model identity"),
    "cheap_model": ConfigFieldRule(MODEL_REQUEST, "selects the alternate Anthropic model when enabled"),
    "use_cheap_model": ConfigFieldRule(MODEL_REQUEST, "switches the model-selection branch"),
    "openai_base_url": ConfigFieldRule(MODEL_REQUEST, "identifies the OpenAI-compatible endpoint without persisting credentials"),
    "openai_api_key": ConfigFieldRule(EXECUTION, "authentication transport only; value is always redacted"),
    "openai_model": ConfigFieldRule(MODEL_REQUEST, "sets the OpenAI-compatible default served model"),
    "temperature": ConfigFieldRule(MODEL_REQUEST, "changes the provider sampling distribution"),
    "max_tokens": ConfigFieldRule(MODEL_REQUEST, "changes the response token cap"),
    "cache_enabled": ConfigFieldRule(MODEL_REQUEST, "changes whether logical responses may come from cache"),
    "social_enabled": ConfigFieldRule(SCIENTIFIC, "enables or removes the social contagion channel"),
    "social_mode": ConfigFieldRule(SCIENTIFIC, "selects feed versus network information routing"),
    "topology": ConfigFieldRule(SCIENTIFIC, "selects the social graph topology"),
    "n_neighbors": ConfigFieldRule(SCIENTIFIC, "controls peer degree in generated social graphs"),
    "social_weight": ConfigFieldRule(SCIENTIFIC, "sets global social coupling gain"),
    "broadcast_mode": ConfigFieldRule(SCIENTIFIC, "selects influencer broadcast ablation routing"),
    "demote_influencer": ConfigFieldRule(SCIENTIFIC, "changes hub and forced-seed treatment"),
    "leverage_enabled": ConfigFieldRule(SCIENTIFIC, "enables the leverage and forced-liquidation layer"),
    "leverage_ratio": ConfigFieldRule(SCIENTIFIC, "sets the centre leverage of reference positions"),
    "leverage_spread": ConfigFieldRule(SCIENTIFIC, "sets leverage heterogeneity and breach thresholds"),
    "maintenance_margin": ConfigFieldRule(SCIENTIFIC, "sets the margin-call threshold"),
    "leverage_fraction": ConfigFieldRule(SCIENTIFIC, "sets the fraction of LLM agents carrying reference leverage"),
    "digest_size": ConfigFieldRule(SCIENTIFIC, "caps the neighbour statements visible to each agent"),
    "seed_fraction": ConfigFieldRule(SCIENTIFIC, "sets the initially informed agent subset"),
    "reference_path": ConfigFieldRule(SCIENTIFIC, "identifies input data used in formal validation metrics"),
    "out_dir": ConfigFieldRule(EXECUTION, "changes output placement only"),
}

CONFIG_CONTRACT_RECORD_FIELDS = (
    "config_hash_schema_version",
    "config_classification_hash",
    "full_effective_config_hash",
    "scientific_config_hash",
    "model_request_config_hash",
    "execution_config_hash",
    "config_field_categories",
    "effective_config_summary",
    "scientific_config_summary",
    "model_request_config_summary",
    "execution_config_summary",
)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _hash_payload(label: str, value: Any) -> str:
    payload = {
        "config_hash_schema_version": CONFIG_HASH_SCHEMA_VERSION,
        "identity": label,
        "value": value,
    }
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _path_identity(value: Any, base_dir: Path) -> dict[str, Any]:
    raw = Path(value).expanduser()
    resolved = raw if raw.is_absolute() else base_dir / raw
    normalised = resolved.resolve(strict=False).as_posix()
    return {
        "kind": "path_identity",
        "resolved_path_sha256": _sha256_bytes(normalised.encode("utf-8")),
    }


def _reference_identity(value: Any, base_dir: Path) -> dict[str, Any]:
    if value in (None, ""):
        return {"configured": False, "kind": None, "size_bytes": None, "sha256": None}
    raw = Path(value).expanduser()
    path = raw if raw.is_absolute() else base_dir / raw
    path = path.resolve(strict=False)
    if path.is_file():
        content = path.read_bytes()
        return {
            "configured": True,
            "kind": "file",
            "size_bytes": len(content),
            "sha256": _sha256_bytes(content),
        }
    configured_identity = path.as_posix().encode("utf-8")
    return {
        "configured": True,
        "kind": "missing_or_non_file",
        "size_bytes": None,
        "sha256": None,
        "configured_path_sha256": _sha256_bytes(configured_identity),
    }


def _endpoint_identity(value: Any) -> dict[str, Any]:
    text = str(value or "")
    try:
        parsed = urlsplit(text)
        host = parsed.netloc.rsplit("@", 1)[-1]
        without_userinfo = urlunsplit(
            (parsed.scheme.lower(), host, parsed.path, parsed.query, parsed.fragment)
        )
    except ValueError:
        without_userinfo = text
    return {
        "configured": bool(text),
        "endpoint_identity_sha256": (
            _sha256_bytes(without_userinfo.encode("utf-8")) if text else None
        ),
        "userinfo_redacted": "@" in text,
    }


def _credential_summary(value: Any) -> dict[str, Any]:
    text = "" if value is None else str(value)
    return {
        "configured": text not in ("", "EMPTY", "<redacted>"),
        "value": "<redacted>" if text not in ("", "EMPTY") else "<not-configured>",
    }


def stable_normalize(value: Any, *, base_dir: Optional[Path] = None) -> Any:
    """Convert supported values to deterministic, JSON-safe typed values."""

    root = Path(base_dir or Path.cwd()).resolve()
    if isinstance(value, enum.Enum):
        value_type = type(value)
        return {
            "__enum__": "{}.{}".format(value_type.__module__, value_type.__qualname__),
            "value": stable_normalize(value.value, base_dir=root),
        }
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ConfigContractError("non-finite float is not valid in effective Config")
        return {"__float_hex__": value.hex()}
    if isinstance(value, Path):
        return _path_identity(value, root)
    if is_dataclass(value) and not isinstance(value, type):
        return {
            "__dataclass__": "{}.{}".format(type(value).__module__, type(value).__qualname__),
            "fields": {
                item.name: stable_normalize(getattr(value, item.name), base_dir=root)
                for item in sorted(fields(value), key=lambda item: item.name)
            },
        }
    if isinstance(value, Mapping):
        if all(isinstance(key, str) for key in value):
            return {
                str(key): stable_normalize(value[key], base_dir=root)
                for key in sorted(value)
            }
        pairs = [
            [
                stable_normalize(key, base_dir=root),
                stable_normalize(item, base_dir=root),
            ]
            for key, item in value.items()
        ]
        pairs.sort(key=lambda pair: _canonical_json(pair[0]))
        return {"__mapping__": pairs}
    if isinstance(value, tuple):
        return {"__tuple__": [stable_normalize(item, base_dir=root) for item in value]}
    if isinstance(value, list):
        return [stable_normalize(item, base_dir=root) for item in value]
    if isinstance(value, (set, frozenset)):
        items = [stable_normalize(item, base_dir=root) for item in value]
        items.sort(key=_canonical_json)
        return {"__set__": items}
    if isinstance(value, bytes):
        return {"__bytes_sha256__": _sha256_bytes(value), "size_bytes": len(value)}
    raise ConfigContractError(
        "unsupported effective Config value type: {}.{}".format(
            type(value).__module__, type(value).__qualname__
        )
    )


def _population_summary(value: Any, max_llm_agents: int) -> Any:
    if value is None:
        return {"mode": "legacy", "counts": None, "effective_cast": None}
    if not isinstance(value, Mapping):
        raise ConfigContractError("population must be a mapping or None")
    counts = {str(key): int(item) for key, item in value.items()}
    cast: list[str] = []
    # This preserves the currently executable ordering explicitly.  The raw
    # counts are still sorted canonically; insertion order is not implicit.
    for persona_id, count in value.items():
        n = int(count)
        if str(persona_id) == "influencer_amplifier":
            n = min(n, 1)
        if n > 0:
            cast.extend([str(persona_id)] * n)
    cast = cast[: int(max_llm_agents)]
    return {
        "mode": "explicit",
        "counts": {key: counts[key] for key in sorted(counts)},
        "effective_cast": cast,
    }


def _normalise_field(name: str, value: Any, cfg: Any, base_dir: Path) -> Any:
    if name == "reference_path":
        return _reference_identity(value, base_dir)
    if name == "out_dir":
        return _path_identity(value, base_dir)
    if name == "openai_base_url":
        return _endpoint_identity(value)
    if name == "openai_api_key":
        return _credential_summary(value)
    if name == "population":
        return _population_summary(value, int(getattr(cfg, "max_llm_agents")))
    return stable_normalize(value, base_dir=base_dir)


def validate_config_classification(cfg: Any) -> tuple[str, ...]:
    if not is_dataclass(cfg) or isinstance(cfg, type):
        raise ConfigContractError("effective Config must be a dataclass instance")
    actual = {item.name for item in fields(cfg)}
    classified = set(CONFIG_FIELD_RULES)
    missing = sorted(actual - classified)
    stale = sorted(classified - actual)
    if missing or stale:
        parts = []
        if missing:
            parts.append("unclassified Config fields={}".format(missing))
        if stale:
            parts.append("classification entries absent from Config={}".format(stale))
        raise UnclassifiedConfigFieldError("; ".join(parts))
    invalid = sorted(
        name
        for name, rule in CONFIG_FIELD_RULES.items()
        if rule.category not in CONFIG_CATEGORIES or not rule.rationale.strip()
    )
    if invalid:
        raise ConfigContractError("invalid Config classification rules={}".format(invalid))
    return tuple(sorted(actual))


def build_effective_config_contract(
    cfg: Any,
    *,
    base_dir: Optional[Path] = None,
    execution_context: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Return hashes and auditable summaries for the final effective Config."""

    root = Path(base_dir or Path.cwd()).resolve()
    field_names = validate_config_classification(cfg)
    normalised = {
        name: _normalise_field(name, getattr(cfg, name), cfg, root)
        for name in field_names
    }
    categories = {
        category: sorted(
            name for name, rule in CONFIG_FIELD_RULES.items()
            if rule.category == category
        )
        for category in CONFIG_CATEGORIES
    }
    classification_payload = {
        name: CONFIG_FIELD_RULES[name].category for name in sorted(CONFIG_FIELD_RULES)
    }
    scientific_summary = {
        name: normalised[name] for name in categories[SCIENTIFIC]
    }
    model_summary = {
        name: normalised[name] for name in categories[MODEL_REQUEST]
    }
    # Provider-adapter identity is conditional so the established Mock,
    # Anthropic and OpenAI-compatible hashes remain byte-for-byte stable.  The
    # Codex CLI wrapper/schema/binary affect the logical request even though
    # they are not Config dataclass fields, so they must be bound here rather
    # than hidden in descriptive manifest metadata.
    requested_provider = str(
        os.environ.get("LLM_PROVIDER") or getattr(cfg, "provider", "auto")
    ).strip().lower()
    if requested_provider == "codex_exec":
        from .codex_exec import (
            codex_reasoning_effort_from_environment,
            codex_static_adapter_identity,
        )

        model_summary["_provider_adapter_contract"] = (
            codex_static_adapter_identity(
                os.environ.get("NMSIM_CODEX_EXECUTABLE", "codex"),
                model=(
                    os.environ.get("LLM_MODEL")
                    or getattr(cfg, "model", "")
                    or None
                ),
                reasoning_effort=(
                    getattr(cfg, "codex_reasoning_effort", None)
                    or codex_reasoning_effort_from_environment()
                ),
            )
        )
    execution_summary = {
        "config_fields": {
            name: normalised[name] for name in categories[EXECUTION]
        },
        "runtime": stable_normalize(dict(execution_context or {}), base_dir=root),
    }
    effective_summary = {name: normalised[name] for name in sorted(normalised)}
    return {
        "config_hash_schema_version": CONFIG_HASH_SCHEMA_VERSION,
        "config_classification_hash": _hash_payload(
            "config_field_classification", classification_payload
        ),
        "full_effective_config_hash": _hash_payload(
            "full_effective_config", effective_summary
        ),
        "scientific_config_hash": _hash_payload(
            SCIENTIFIC, scientific_summary
        ),
        "model_request_config_hash": _hash_payload(
            MODEL_REQUEST, model_summary
        ),
        "execution_config_hash": _hash_payload(
            EXECUTION, execution_summary
        ),
        "config_field_categories": categories,
        "effective_config_summary": effective_summary,
        "scientific_config_summary": scientific_summary,
        "model_request_config_summary": model_summary,
        "execution_config_summary": execution_summary,
    }


def category_field_differences(
    expected: Mapping[str, Any],
    actual: Mapping[str, Any],
    category: str,
) -> list[str]:
    """Return deterministic field paths whose normalised values differ."""

    summary_key = {
        SCIENTIFIC: "scientific_config_summary",
        MODEL_REQUEST: "model_request_config_summary",
        EXECUTION: "execution_config_summary",
    }[category]
    left = expected.get(summary_key)
    right = actual.get(summary_key)
    if not isinstance(left, Mapping) or not isinstance(right, Mapping):
        return ["<summary-unavailable>"]
    if category != EXECUTION:
        return [
            name
            for name in sorted(set(left) | set(right))
            if left.get(name) != right.get(name)
        ]

    differences: list[str] = []
    left_config = left.get("config_fields", {})
    right_config = right.get("config_fields", {})
    if isinstance(left_config, Mapping) and isinstance(right_config, Mapping):
        differences.extend(
            "config_fields." + name
            for name in sorted(set(left_config) | set(right_config))
            if left_config.get(name) != right_config.get(name)
        )
    elif left_config != right_config:
        differences.append("config_fields")
    left_runtime = left.get("runtime", {})
    right_runtime = right.get("runtime", {})
    if isinstance(left_runtime, Mapping) and isinstance(right_runtime, Mapping):
        differences.extend(
            "runtime." + name
            for name in sorted(set(left_runtime) | set(right_runtime))
            if left_runtime.get(name) != right_runtime.get(name)
        )
    elif left_runtime != right_runtime:
        differences.append("runtime")
    return differences


def normalised_value_digest(value: Any) -> str:
    """Safe digest used in mismatch diagnostics instead of raw field values."""

    return _sha256_bytes(_canonical_json(value))


__all__ = [
    "CONFIG_CATEGORIES",
    "CONFIG_CONTRACT_RECORD_FIELDS",
    "CONFIG_FIELD_RULES",
    "CONFIG_HASH_SCHEMA_VERSION",
    "ConfigContractError",
    "ConfigFieldRule",
    "EXECUTION",
    "MODEL_REQUEST",
    "SCIENTIFIC",
    "UnclassifiedConfigFieldError",
    "build_effective_config_contract",
    "category_field_differences",
    "normalised_value_digest",
    "stable_normalize",
    "validate_config_classification",
]
