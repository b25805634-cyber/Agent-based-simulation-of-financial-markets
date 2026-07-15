"""Offline audit of historical LLM responses with the current decision parser.

This module deliberately does *not* construct an LLM provider or continue a
simulation.  It reads the immutable artifacts of an existing managed run,
applies :func:`nmsim.llm.parse_order` to each recorded raw response, and
compares the result with a historical ``AgentDecisionParsed`` event when one is
available.

Public output contains only public decision fields and presence/hash metadata
for private reasoning.  Reasoning bodies are confined to a separate 0600
JSONL file.
"""
from __future__ import annotations

import argparse
import datetime as _datetime
import hashlib
import json
import math
import os
import pathlib
import re
import sys
import uuid
from collections import Counter, defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Optional, Union

from .fingerprint import (
    STRICT_COMPATIBILITY_FIELDS,
    scientific_compatibility_metadata,
)
from .llm import parse_order


AUDIT_SCHEMA_VERSION = "1.0"
PUBLIC_RESULTS_NAME = "reparse_results.jsonl"
PRIVATE_RESULTS_NAME = "reparse_private.jsonl"
SUMMARY_NAME = "reparse_summary.json"

_MISSING = object()
_COMPARE_FIELDS = (
    "reasoning",
    "sentiment",
    "public_take",
    "action",
    "quantity",
    "limit_price",
    "reservation_price",
    "parse_status",
    "fallback_status",
    "validation_errors",
)
_CONTRACT_REPORT_FIELDS = (
    *STRICT_COMPATIBILITY_FIELDS,
    "git_commit",
    "git_dirty",
)
_RUNTIME_CONFIG_REPORT_FIELDS = (
    "config_hash_schema_version",
    "config_classification_hash",
    "full_effective_config_hash",
    "scientific_config_hash",
    "model_request_config_hash",
    "execution_config_hash",
)


def _utc_now() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_number(value: Any) -> Any:
    """Keep normal numeric values and represent non-finite floats safely."""

    if isinstance(value, float) and not math.isfinite(value):
        if math.isnan(value):
            return "NaN"
        return "Infinity" if value > 0 else "-Infinity"
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _write_exclusive(path: pathlib.Path, payload: bytes, mode: int) -> None:
    descriptor = os.open(
        str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode
    )
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.chmod(str(path), mode)


def _write_json(path: pathlib.Path, value: Mapping[str, Any], mode: int) -> None:
    payload = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    _write_exclusive(path, payload.encode("utf-8"), mode)


def _write_jsonl(
    path: pathlib.Path, values: Iterable[Mapping[str, Any]], mode: int
) -> None:
    payload = b"".join(
        (_canonical_json(value) + "\n").encode("utf-8") for value in values
    )
    _write_exclusive(path, payload, mode)


def _read_jsonl(path: pathlib.Path, *, required: bool) -> list[dict[str, Any]]:
    if not path.is_file():
        if required:
            raise FileNotFoundError("required audit input not found: {}".format(path))
        return []
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(
                    "invalid JSON in {} at line {}: {}".format(
                        path.name, line_number, error
                    )
                ) from error
            if not isinstance(value, dict):
                raise ValueError(
                    "{} line {} is not a JSON object".format(path.name, line_number)
                )
            records.append(value)
    return records


def _is_within(path: pathlib.Path, parent: pathlib.Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _new_audit_directory(out_root: pathlib.Path) -> pathlib.Path:
    out_root.mkdir(parents=True, exist_ok=True)
    stamp = _datetime.datetime.now(_datetime.timezone.utc).strftime(
        "%Y%m%dT%H%M%S%fZ"
    )
    # UUID makes collisions between concurrent local audit processes negligible;
    # exist_ok=False remains the actual no-overwrite guard.
    audit_dir = out_root / "reparse-audit-{}-{}".format(
        stamp, uuid.uuid4().hex[:12]
    )
    audit_dir.mkdir(mode=0o755, exist_ok=False)
    return audit_dir


def _last_price(record: Mapping[str, Any]) -> float:
    request = record.get("request")
    user = request.get("user") if isinstance(request, Mapping) else None
    if isinstance(user, str):
        match = re.search(
            r"^(?:LAST_PRICE|LATEST PRICE):\s*([-+]?\d*\.?\d+)",
            user,
            re.MULTILINE,
        )
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                pass
    return 100.0


def _raw_object(raw: str) -> tuple[Optional[dict[str, Any]], list[str]]:
    """Inspect validation issues without substituting for ``parse_order``."""

    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None, ["invalid_or_missing_json_object"]
    try:
        value = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None, ["invalid_json_object"]
    if not isinstance(value, dict):
        return None, ["json_value_is_not_object"]

    errors: list[str] = []
    action = value.get("side") or value.get("action") or "hold"
    if action not in ("buy", "sell", "hold"):
        errors.append("invalid_action_fell_back_to_hold")

    quantity = value.get("quantity")
    if quantity not in (None, ""):
        try:
            int(quantity)
        except (TypeError, ValueError):
            errors.append("invalid_quantity_fell_back_to_zero")

    for key in ("sentiment", "limit_price", "reservation_price"):
        candidate = value.get(key)
        if candidate is None:
            continue
        try:
            number = float(candidate)
        except (TypeError, ValueError):
            errors.append("invalid_{}_used_default".format(key))
            continue
        if not math.isfinite(number):
            errors.append("non_finite_{}".format(key))
        elif key == "sentiment" and not -1.0 <= number <= 1.0:
            errors.append("sentiment_clamped")

    public_take = str(value.get("public_take") or "")
    reasoning = str(value.get("reasoning") or value.get("rationale") or "")
    if len(public_take) > 140:
        errors.append("public_take_truncated")
    if len(reasoning) > 240:
        errors.append("reasoning_truncated")
    return value, errors


def _fallback_status(parse_status: str, rationale: str) -> str:
    if parse_status == "error" or rationale == "parse-failed; holding":
        return "parse_failure_hold"
    if rationale == "api-error; holding":
        return "api_error_hold"
    if rationale == "parse-retries-exhausted; holding":
        return "parse_retries_exhausted_hold"
    return "none"


def _current_decision(raw: str, last_price: float) -> tuple[dict[str, Any], str]:
    order = parse_order(raw, last_price)
    raw_object, validation_errors = _raw_object(raw)
    rationale = str(order.get("rationale", ""))
    parse_status = "error" if rationale == "parse-failed; holding" else "parsed"
    reservation: Any = None
    if isinstance(raw_object, Mapping) and "reservation_price" in raw_object:
        try:
            reservation = _json_number(float(raw_object["reservation_price"]))
        except (TypeError, ValueError):
            reservation = None
    decision = {
        "reasoning_present": bool(rationale),
        "reasoning_sha256": _sha256_text(rationale) if rationale else None,
        "sentiment": _json_number(order["sentiment"]),
        "public_take": order["public_take"],
        "action": order["side"],
        "quantity": order["quantity"],
        "limit_price": _json_number(order["limit_price"]),
        # The current Order schema has no reservation_price field.  Preserve an
        # explicit raw field for audit visibility without pretending the parser
        # routed it into the executable Order.
        "reservation_price": reservation,
        "parse_status": parse_status,
        "fallback_status": _fallback_status(parse_status, rationale),
        "validation_errors": validation_errors,
    }
    return decision, rationale


def _request_identity(record: Mapping[str, Any], line_number: int) -> dict[str, Any]:
    request = record.get("request")
    request = request if isinstance(request, Mapping) else {}
    raw = record.get("raw_response")
    response_hash = record.get("response_hash")
    if not isinstance(response_hash, str) and isinstance(raw, str):
        response_hash = _sha256_text(raw)
    return {
        "record_line": line_number,
        "sequence": record.get("sequence"),
        "run_id": record.get("run_id"),
        "round": record.get("round"),
        "agent_id": record.get("agent_id"),
        "persona_id": record.get("persona_id"),
        "batch_sequence": record.get("batch_sequence"),
        "batch_index": record.get("batch_index"),
        "prompt_hash": request.get("prompt_hash"),
        "response_hash": response_hash,
    }


def _contract_fields(value: Mapping[str, Any]) -> dict[str, Any]:
    """Select only non-private version, hash and Git identity fields."""

    return {field: value.get(field) for field in _CONTRACT_REPORT_FIELDS}


def _recorded_contract(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    fields = _contract_fields(records[0]) if records else {
        field: None for field in _CONTRACT_REPORT_FIELDS
    }
    missing = [field for field, value in fields.items() if value is None]
    compatibility_missing = [
        field
        for field in STRICT_COMPATIBILITY_FIELDS
        if fields.get(field) is None
    ]
    runtime_config_fields = {
        field: records[0].get(field) if records else None
        for field in _RUNTIME_CONFIG_REPORT_FIELDS
    }
    runtime_config_missing = [
        field for field, value in runtime_config_fields.items() if value is None
    ]
    return {
        "status": (
            "available" if not compatibility_missing else "legacy_contract_unavailable"
        ),
        "missing_fields": missing,
        "compatibility_missing_fields": compatibility_missing,
        "fields": fields,
        "runtime_config_contract": {
            "status": (
                "available"
                if not runtime_config_missing
                else "runtime_config_contract_unavailable"
            ),
            "missing_fields": runtime_config_missing,
            "fields": runtime_config_fields,
        },
    }


def _decision_index(
    run_dir: pathlib.Path,
) -> dict[tuple[Any, Any, Any], deque[dict[str, Any]]]:
    public_events = _read_jsonl(run_dir / "events.jsonl", required=False)
    private_events = _read_jsonl(run_dir / "private_events.jsonl", required=False)
    private_by_event_id = {
        str(event.get("event_id")): event
        for event in private_events
        if event.get("type") == "AgentDecisionParsed"
    }
    index: dict[tuple[Any, Any, Any], deque[dict[str, Any]]] = defaultdict(deque)
    seen_event_ids: set[str] = set()
    for event in public_events:
        if event.get("type") != "AgentDecisionParsed":
            continue
        data = event.get("data")
        if not isinstance(data, Mapping):
            continue
        key = (
            event.get("round"),
            event.get("agent_id"),
            data.get("raw_response_sha256"),
        )
        merged = dict(data)
        private = private_by_event_id.get(str(event.get("event_id")))
        private_data = private.get("data") if isinstance(private, Mapping) else None
        if isinstance(private_data, Mapping):
            merged.update(private_data)
        index[key].append(merged)
        seen_event_ids.add(str(event.get("event_id")))
    # Some pre-Phase-1 or hand-built audit fixtures may have retained a parsed
    # decision only in the private stream.  It is still historical evidence;
    # normalisation below ensures its rationale body never reaches public output.
    for event in private_events:
        if (
            event.get("type") != "AgentDecisionParsed"
            or str(event.get("event_id")) in seen_event_ids
        ):
            continue
        data = event.get("data")
        if not isinstance(data, Mapping):
            continue
        key = (
            event.get("round"),
            event.get("agent_id"),
            data.get("raw_response_sha256"),
        )
        index[key].append(dict(data))
    return index


def _record_embedded_decision(record: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    # Accept explicit future metadata without inventing it for Phase-1 records.
    for key in ("parsed_decision", "historical_parsed_decision"):
        value = record.get(key)
        if isinstance(value, Mapping):
            return dict(value)
    return None


def _historical_decision(
    record: Mapping[str, Any],
    index: dict[tuple[Any, Any, Any], deque[dict[str, Any]]],
) -> Optional[dict[str, Any]]:
    embedded = _record_embedded_decision(record)
    if embedded is not None:
        return embedded
    key = (
        record.get("round"),
        record.get("agent_id"),
        record.get("response_hash"),
    )
    matches = index.get(key)
    return matches.popleft() if matches else None


def _mapping_value(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return _MISSING


def _normalise_historical(
    value: Optional[Mapping[str, Any]],
) -> tuple[Optional[dict[str, Any]], Optional[str], set[str]]:
    if value is None:
        return None, None, set()

    unavailable: set[str] = set()
    rationale_value = _mapping_value(
        value, "private_rationale", "rationale", "reasoning"
    )
    if rationale_value is _MISSING:
        rationale: Optional[str] = None
        reasoning_present: Any = _MISSING
        reasoning_hash: Any = _MISSING
        unavailable.add("reasoning")
    else:
        rationale = str(rationale_value or "")
        reasoning_present = bool(rationale)
        reasoning_hash = _sha256_text(rationale) if rationale else None

    action = _mapping_value(value, "action", "side")
    limit_price = _mapping_value(value, "limit_price")
    reservation = _mapping_value(value, "reservation_price")
    parse_status = _mapping_value(value, "parse_status")
    if parse_status is _MISSING and rationale is not None:
        parse_status = (
            "error" if rationale == "parse-failed; holding" else "parsed"
        )
    fallback = _mapping_value(value, "fallback_status")
    if fallback is _MISSING and rationale is not None and parse_status is not _MISSING:
        fallback = _fallback_status(str(parse_status), rationale)
    errors = _mapping_value(value, "validation_errors")

    fields = {
        "reasoning_present": reasoning_present,
        "reasoning_sha256": reasoning_hash,
        "sentiment": _mapping_value(value, "sentiment"),
        "public_take": _mapping_value(value, "public_take"),
        "action": action,
        "quantity": _mapping_value(value, "quantity"),
        "limit_price": limit_price,
        "reservation_price": reservation,
        "parse_status": parse_status,
        "fallback_status": fallback,
        "validation_errors": errors,
    }
    for numeric_field in ("sentiment", "limit_price", "reservation_price"):
        if fields[numeric_field] is not _MISSING:
            fields[numeric_field] = _json_number(fields[numeric_field])
    for field in _COMPARE_FIELDS:
        source_key = "reasoning_present" if field == "reasoning" else field
        if fields.get(source_key, _MISSING) is _MISSING:
            unavailable.add(field)
    # _MISSING is internal only and must never reach JSON output.
    public = {
        key: (None if item is _MISSING else item)
        for key, item in fields.items()
    }
    return public, rationale, unavailable


def _compare(
    historical: Optional[dict[str, Any]],
    current: dict[str, Any],
    unavailable: set[str],
) -> tuple[str, dict[str, dict[str, Any]], list[str]]:
    if historical is None:
        return (
            "comparison_unavailable",
            {
                field: {"available": False, "changed": None}
                for field in _COMPARE_FIELDS
            },
            list(_COMPARE_FIELDS),
        )

    differences: dict[str, dict[str, Any]] = {}
    unavailable_sorted = sorted(unavailable)
    for field in _COMPARE_FIELDS:
        if field in unavailable:
            differences[field] = {"available": False, "changed": None}
            continue
        if field == "reasoning":
            old_value = {
                "present": historical["reasoning_present"],
                "sha256": historical["reasoning_sha256"],
            }
            new_value = {
                "present": current["reasoning_present"],
                "sha256": current["reasoning_sha256"],
            }
        else:
            old_value = historical[field]
            new_value = current[field]
        differences[field] = {
            "available": True,
            "changed": old_value != new_value,
            "historical": old_value,
            "current": new_value,
        }

    any_changed = any(
        detail.get("changed") is True for detail in differences.values()
    )
    if any_changed:
        status = "different"
    elif unavailable:
        status = "partial_match"
    else:
        status = "exact_match"
    return status, differences, unavailable_sorted


def run_reparse_audit(
    run: Union[str, os.PathLike[str]],
    out: Union[str, os.PathLike[str]],
) -> pathlib.Path:
    """Audit one historical run and return a new, non-overwriting directory."""

    run_dir = pathlib.Path(run).expanduser().resolve()
    if not run_dir.is_dir():
        raise NotADirectoryError("historical run directory not found: {}".format(run_dir))
    records_path = run_dir / "llm_records.jsonl"
    records = _read_jsonl(records_path, required=True)

    out_root = pathlib.Path(out).expanduser().resolve()
    if _is_within(out_root, run_dir):
        raise ValueError(
            "audit output must be outside the immutable historical run directory"
        )
    audit_dir = _new_audit_directory(out_root)

    decision_index = _decision_index(run_dir)
    public_results: list[dict[str, Any]] = []
    private_results: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    field_difference_counts: Counter[str] = Counter()
    successful_reparse_count = 0
    parse_failure_count = 0

    for line_number, record in enumerate(records, 1):
        raw = record.get("raw_response")
        if not isinstance(raw, str):
            raise ValueError(
                "llm_records.jsonl line {} has no string raw_response".format(
                    line_number
                )
            )
        identity = _request_identity(record, line_number)
        current, current_rationale = _current_decision(raw, _last_price(record))
        if current["parse_status"] == "parsed":
            successful_reparse_count += 1
        else:
            parse_failure_count += 1

        historical_raw = _historical_decision(record, decision_index)
        historical, historical_rationale, unavailable = _normalise_historical(
            historical_raw
        )
        status, field_diffs, unavailable_fields = _compare(
            historical, current, unavailable
        )
        status_counts[status] += 1
        for field, detail in field_diffs.items():
            if detail.get("changed") is True:
                field_difference_counts[field] += 1

        public_results.append(
            {
                "audit_schema_version": AUDIT_SCHEMA_VERSION,
                "request_identity": identity,
                "comparison_status": status,
                "comparison_unavailable_fields": unavailable_fields,
                "historical_decision": historical,
                "current_decision": current,
                "field_differences": field_diffs,
            }
        )
        private_results.append(
            {
                "audit_schema_version": AUDIT_SCHEMA_VERSION,
                "request_identity": identity,
                "comparison_status": status,
                "historical_private_rationale": historical_rationale,
                "current_private_rationale": current_rationale,
                "reasoning_changed": (
                    None
                    if historical_rationale is None
                    else historical_rationale != current_rationale
                ),
            }
        )

    summary = {
        "audit_schema_version": AUDIT_SCHEMA_VERSION,
        "audit_type": "offline_decision_reparse_audit",
        "created_at": _utc_now(),
        "source_run": str(run_dir),
        "source_records_sha256": hashlib.sha256(records_path.read_bytes()).hexdigest(),
        # Reparse intentionally reports, but does not enforce, compatibility.
        # An incompatible contract is exactly when this diagnostic is useful.
        "current_parser_contract": _contract_fields(
            scientific_compatibility_metadata()
        ),
        "recorded_contract": _recorded_contract(records),
        "provider_calls": 0,
        "network_access": False,
        "simulation_continued": False,
        "price_path_generated": False,
        "total_response_count": len(records),
        "successful_reparse_count": successful_reparse_count,
        "parse_failure_count": parse_failure_count,
        "exact_match_count": status_counts["exact_match"],
        "partial_match_count": status_counts["partial_match"],
        "different_count": status_counts["different"],
        "comparison_unavailable_count": status_counts["comparison_unavailable"],
        "field_difference_counts": {
            field: field_difference_counts[field] for field in _COMPARE_FIELDS
        },
        "public_results_file": PUBLIC_RESULTS_NAME,
        "private_results_file": PRIVATE_RESULTS_NAME,
    }
    _write_jsonl(audit_dir / PUBLIC_RESULTS_NAME, public_results, 0o644)
    _write_jsonl(audit_dir / PRIVATE_RESULTS_NAME, private_results, 0o600)
    _write_json(audit_dir / SUMMARY_NAME, summary, 0o644)
    return audit_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Offline audit: reparse recorded raw LLM responses with the current "
            "decision parser. This is not strict replay and never runs a market."
        )
    )
    parser.add_argument("--run", required=True, help="historical managed run directory")
    parser.add_argument(
        "--out", required=True, help="parent directory for a new immutable audit directory"
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    audit_dir = run_reparse_audit(args.run, args.out)
    print("Reparse audit written to {}".format(audit_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
