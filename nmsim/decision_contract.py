"""Opt-in strict decision-response validation for preregistered studies."""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
from typing import Any, Mapping


MULTI_EVENT_DECISION_RESPONSE_SCHEMA = "multi_event_decision_response_v1"
SUPPORTED_DECISION_RESPONSE_SCHEMAS = frozenset(
    {MULTI_EVENT_DECISION_RESPONSE_SCHEMA}
)


@dataclass(frozen=True)
class DecisionResponseValidity:
    schema_version: str | None
    direction_field: str | None
    valid: bool
    error_code: str | None
    terminal_status: str | None = None


DECISION_VALID = "valid_decision"
STRICT_SCHEMA_INVALID = "strict_schema_invalid"
LEGACY_PARSE_INVALID = "legacy_parse_invalid"
PROVIDER_PARSE_EXHAUSTED = "provider_parse_exhausted"
PROVIDER_EXCEPTION_EXHAUSTED = "provider_exception_exhausted"
ADAPTER_TERMINAL_STATUS_FIELD = "_nmsim_terminal_status"
ADAPTER_TERMINAL_STATUSES = frozenset(
    {PROVIDER_PARSE_EXHAUSTED, PROVIDER_EXCEPTION_EXHAUSTED}
)


def exact_adapter_terminal_status(raw: str) -> str | None:
    """Read only the reserved field on exact adapter-generated JSON shapes.

    This intentionally never searches arbitrary model-authored rationale text.
    Multi-event adapters add the reserved field only when synthesizing a final
    fallback after exhausting parse retries or Provider exceptions.
    """

    if not isinstance(raw, str):
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(value, Mapping):
        return None
    base_keys = {
        "side",
        "quantity",
        "limit_price",
        "sentiment",
        "rationale",
        ADAPTER_TERMINAL_STATUS_FIELD,
    }
    if set(value) not in (base_keys, base_keys | {"public_take"}):
        return None
    limit_price = value.get("limit_price")
    sentiment = value.get("sentiment")
    if (
        value.get("side") != "hold"
        or isinstance(value.get("quantity"), bool)
        or value.get("quantity") != 0
        or isinstance(limit_price, bool)
        or not isinstance(limit_price, (int, float))
        or not math.isfinite(float(limit_price))
        or float(limit_price) <= 0.0
        or isinstance(sentiment, bool)
        or not isinstance(sentiment, (int, float))
        or float(sentiment) != 0.0
        or ("public_take" in value and value.get("public_take") != "")
    ):
        return None
    status = value.get(ADAPTER_TERMINAL_STATUS_FIELD)
    rationale_by_status = {
        PROVIDER_PARSE_EXHAUSTED: "parse-retries-exhausted; holding",
        PROVIDER_EXCEPTION_EXHAUSTED: "api-error; holding",
    }
    if status not in ADAPTER_TERMINAL_STATUSES:
        return None
    return status if value.get("rationale") == rationale_by_status[status] else None


def validate_decision_response(
    raw: str,
    *,
    schema_version: str,
    direction_field: str,
) -> tuple[Mapping[str, Any] | None, DecisionResponseValidity]:
    """Validate the frozen essential schema without coercion or trimming."""

    if schema_version not in SUPPORTED_DECISION_RESPONSE_SCHEMAS:
        raise ValueError("unsupported decision response schema")
    if direction_field not in {"action", "side"}:
        raise ValueError("direction_field must be action or side")

    def invalid(code: str):
        return None, DecisionResponseValidity(
            schema_version, direction_field, False, code
        )

    if not isinstance(raw, str):
        return invalid("invalid_json_object")
    try:
        value = json.loads(raw.strip())
    except (TypeError, ValueError, json.JSONDecodeError):
        return invalid("invalid_json_object")
    if not isinstance(value, Mapping):
        return invalid("invalid_json_object")
    required = {
        direction_field,
        "quantity",
        "limit_price",
        "sentiment",
        "public_take",
        "reasoning",
    }
    if set(value) != required:
        return invalid("missing_required_field")
    action = value.get(direction_field)
    if action not in {"buy", "sell", "hold"}:
        return invalid("invalid_action")
    quantity = value.get("quantity")
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 0:
        return invalid("invalid_quantity")
    if (action == "hold" and quantity != 0) or (
        action in {"buy", "sell"} and quantity <= 0
    ):
        return invalid("quantity_action_mismatch")
    limit_price = value.get("limit_price")
    if (
        isinstance(limit_price, bool)
        or not isinstance(limit_price, (int, float))
        or not math.isfinite(float(limit_price))
        or float(limit_price) <= 0.0
    ):
        return invalid("invalid_limit_price")
    sentiment = value.get("sentiment")
    if (
        isinstance(sentiment, bool)
        or not isinstance(sentiment, (int, float))
        or not math.isfinite(float(sentiment))
        or not -1.0 <= float(sentiment) <= 1.0
    ):
        return invalid("invalid_sentiment")
    reasoning = value.get("reasoning")
    if not isinstance(reasoning, str) or not reasoning.strip():
        return invalid("blank_reasoning")
    public_take = value.get("public_take")
    if not isinstance(public_take, str) or not public_take.strip():
        return invalid("blank_public_take")
    return value, DecisionResponseValidity(
        schema_version, direction_field, True, None
    )


__all__ = [
    "ADAPTER_TERMINAL_STATUSES",
    "ADAPTER_TERMINAL_STATUS_FIELD",
    "DECISION_VALID",
    "DecisionResponseValidity",
    "LEGACY_PARSE_INVALID",
    "MULTI_EVENT_DECISION_RESPONSE_SCHEMA",
    "PROVIDER_EXCEPTION_EXHAUSTED",
    "PROVIDER_PARSE_EXHAUSTED",
    "STRICT_SCHEMA_INVALID",
    "SUPPORTED_DECISION_RESPONSE_SCHEMAS",
    "exact_adapter_terminal_status",
    "validate_decision_response",
]
