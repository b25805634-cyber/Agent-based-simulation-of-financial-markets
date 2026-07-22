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
    if not required <= set(value):
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
    "DecisionResponseValidity",
    "MULTI_EVENT_DECISION_RESPONSE_SCHEMA",
    "SUPPORTED_DECISION_RESPONSE_SCHEMAS",
    "validate_decision_response",
]
