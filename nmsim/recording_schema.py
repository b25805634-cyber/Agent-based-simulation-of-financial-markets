"""Versioned schema contract for immutable LLM call recordings.

Recording versions describe evidence structure, not market semantics.  This
module is deliberately independent from the simulator's scientific component
allowlist so instrumentation changes do not masquerade as market changes.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import re
from typing import Any, Optional

from .config_contract import CONFIG_CONTRACT_RECORD_FIELDS


CURRENT_RECORDING_SCHEMA_VERSION = "1.2"
SUPPORTED_RECORDING_SCHEMA_VERSIONS = frozenset({"1.0", "1.1", "1.2"})
RECORD_TYPE = "llm_call"

SOURCE_COMPATIBILITY_FIELDS = (
    "fingerprint_schema_version",
    "decision_parser_schema_version",
    "decision_parser_source_hash",
    "event_schema_version",
    "recording_schema_version",
    "prompt_source_hash",
    "persona_source_hash",
    "simulation_core_source_hash",
    "scientific_component_fingerprint",
)

V12_REQUIRED_TOP_LEVEL_FIELDS = (
    "schema_version",
    "record_type",
    "recorded_at",
    "run_id",
    "sequence",
    "round",
    "batch_sequence",
    "batch_index",
    "batch_size",
    "agent_id",
    "persona_id",
    "request",
    "model_config",
    *SOURCE_COMPATIBILITY_FIELDS,
    *CONFIG_CONTRACT_RECORD_FIELDS,
    "git_commit",
    "git_dirty",
    "raw_response",
    "response_hash",
)

V12_REQUIRED_REQUEST_FIELDS = (
    "system",
    "user",
    "system_hash",
    "user_hash",
    "prompt_hash",
)

V12_HASH_FIELDS = (
    "decision_parser_source_hash",
    "prompt_source_hash",
    "persona_source_hash",
    "simulation_core_source_hash",
    "scientific_component_fingerprint",
    "config_classification_hash",
    "full_effective_config_hash",
    "scientific_config_hash",
    "model_request_config_hash",
    "execution_config_hash",
    "response_hash",
)

V12_VERSION_FIELDS = (
    "fingerprint_schema_version",
    "decision_parser_schema_version",
    "event_schema_version",
    "recording_schema_version",
    "config_hash_schema_version",
)

_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _prompt_hash(system: str, user: str) -> str:
    payload = "{}:{}{}:{}".format(len(system), system, len(user), user)
    return _sha256_text(payload)


@dataclass(frozen=True)
class RecordingCompatibilityRule:
    schema_version: str
    runtime_config_contract: bool
    variant: str
    strict_replay: bool
    reason: Optional[str]
    reparse_audit: bool


RECORDING_COMPATIBILITY_MATRIX = (
    RecordingCompatibilityRule(
        "1.0",
        False,
        "legacy_schema_1_0",
        False,
        "legacy_recording_missing_replay_contract",
        True,
    ),
    RecordingCompatibilityRule(
        "1.1",
        False,
        "pre_config_contract_schema_1_1",
        False,
        "recording_missing_runtime_config_contract",
        True,
    ),
    RecordingCompatibilityRule(
        "1.1",
        True,
        "transitional_schema_1_1_with_config_contract",
        False,
        "transitional_schema_1_1_with_config_contract",
        True,
    ),
    RecordingCompatibilityRule(
        "1.2",
        True,
        "complete_schema_1_2",
        True,
        None,
        True,
    ),
)


class RecordingSchemaValidationError(ValueError):
    """A recording cannot satisfy its declared structural contract."""

    def __init__(
        self,
        code: str,
        *,
        line_number: Optional[int] = None,
        fields: Sequence[str] = (),
        detail: Optional[str] = None,
    ) -> None:
        self.code = code
        self.line_number = line_number
        self.fields = tuple(sorted(set(str(field) for field in fields)))
        parts = [code]
        if line_number is not None:
            parts.append("line={}".format(line_number))
        if self.fields:
            parts.append("fields={}".format(list(self.fields)))
        if detail:
            parts.append(detail)
        super().__init__(": ".join(parts))


def _runtime_contract_complete(record: Mapping[str, Any]) -> bool:
    return all(record.get(field) is not None for field in CONFIG_CONTRACT_RECORD_FIELDS)


def compatibility_rule_for_record(
    record: Mapping[str, Any],
) -> RecordingCompatibilityRule:
    """Classify a declared schema without upgrading or guessing its version."""

    version = record.get("schema_version")
    runtime_contract = _runtime_contract_complete(record)
    if version == "1.0":
        # Version identity wins over incidental extra fields in a historical
        # file; their presence cannot upgrade the envelope.
        return RECORDING_COMPATIBILITY_MATRIX[0]
    for rule in RECORDING_COMPATIBILITY_MATRIX:
        if (
            rule.schema_version == version
            and rule.runtime_config_contract == runtime_contract
        ):
            return rule
    if version == "1.2":
        # A 1.2 envelope with an incomplete contract is malformed 1.2, never a
        # legacy variant and never an implicit 1.1 upgrade.
        return RecordingCompatibilityRule(
            "1.2",
            runtime_contract,
            "incomplete_schema_1_2",
            False,
            "recording_schema_1_2_invalid",
            True,
        )
    return RecordingCompatibilityRule(
        str(version),
        runtime_contract,
        "unsupported_recording_schema",
        False,
        "unsupported_recording_schema",
        True,
    )


def validate_v12_metadata(metadata: Mapping[str, Any]) -> None:
    """Validate metadata required before a provider may be called for Record."""

    required = (*SOURCE_COMPATIBILITY_FIELDS, *CONFIG_CONTRACT_RECORD_FIELDS)
    missing = [field for field in required if metadata.get(field) is None]
    if missing:
        raise RecordingSchemaValidationError(
            "recording_schema_1_2_missing_required_metadata", fields=missing
        )
    if metadata.get("recording_schema_version") != CURRENT_RECORDING_SCHEMA_VERSION:
        raise RecordingSchemaValidationError(
            "recording_schema_1_2_invalid_metadata",
            fields=("recording_schema_version",),
        )
    invalid_hashes = [
        field
        for field in V12_HASH_FIELDS
        if field != "response_hash"
        and not _is_sha256(metadata.get(field))
    ]
    invalid_versions = [
        field
        for field in V12_VERSION_FIELDS
        if not isinstance(metadata.get(field), str) or not metadata.get(field)
    ]
    invalid_mappings = [
        field
        for field in (
            "config_field_categories",
            "effective_config_summary",
            "scientific_config_summary",
            "model_request_config_summary",
            "execution_config_summary",
        )
        if not isinstance(metadata.get(field), Mapping)
    ]
    invalid = invalid_hashes + invalid_versions + invalid_mappings
    if invalid:
        raise RecordingSchemaValidationError(
            "recording_schema_1_2_invalid_metadata", fields=invalid
        )


def validate_v12_record(
    record: Mapping[str, Any], *, line_number: Optional[int] = None
) -> None:
    """Validate the declared 1.2 structure before strict compatibility checks."""

    missing = [field for field in V12_REQUIRED_TOP_LEVEL_FIELDS if field not in record]
    if missing:
        raise RecordingSchemaValidationError(
            "recording_schema_1_2_missing_required_fields",
            line_number=line_number,
            fields=missing,
        )
    if (
        record.get("schema_version") != CURRENT_RECORDING_SCHEMA_VERSION
        or record.get("recording_schema_version")
        != CURRENT_RECORDING_SCHEMA_VERSION
    ):
        raise RecordingSchemaValidationError(
            "recording_schema_1_2_version_mismatch",
            line_number=line_number,
            fields=("schema_version", "recording_schema_version"),
        )
    validate_v12_metadata(record)

    invalid: list[str] = []
    if record.get("record_type") != RECORD_TYPE:
        invalid.append("record_type")
    if not isinstance(record.get("recorded_at"), str) or not record.get("recorded_at"):
        invalid.append("recorded_at")
    if type(record.get("sequence")) is not int or record.get("sequence", 0) < 1:
        invalid.append("sequence")
    if record.get("round") is not None and type(record.get("round")) is not int:
        invalid.append("round")
    if type(record.get("batch_sequence")) is not int or record.get(
        "batch_sequence", 0
    ) < 1:
        invalid.append("batch_sequence")
    if type(record.get("batch_index")) is not int or record.get(
        "batch_index", -1
    ) < 0:
        invalid.append("batch_index")
    if type(record.get("batch_size")) is not int or record.get("batch_size", 0) < 1:
        invalid.append("batch_size")
    if (
        type(record.get("batch_index")) is int
        and type(record.get("batch_size")) is int
        and record["batch_index"] >= record["batch_size"]
    ):
        invalid.extend(("batch_index", "batch_size"))
    for field in ("run_id", "agent_id", "persona_id", "git_commit"):
        if record.get(field) is not None and not isinstance(record.get(field), str):
            invalid.append(field)
    if record.get("git_dirty") is not None and not isinstance(
        record.get("git_dirty"), bool
    ):
        invalid.append("git_dirty")
    if not isinstance(record.get("model_config"), Mapping):
        invalid.append("model_config")
    if not isinstance(record.get("raw_response"), str):
        invalid.append("raw_response")
    if not _is_sha256(record.get("response_hash")):
        invalid.append("response_hash")

    request = record.get("request")
    if not isinstance(request, Mapping):
        invalid.append("request")
    else:
        missing_request = [
            "request.{}".format(field)
            for field in V12_REQUIRED_REQUEST_FIELDS
            if field not in request
        ]
        if missing_request:
            raise RecordingSchemaValidationError(
                "recording_schema_1_2_missing_required_fields",
                line_number=line_number,
                fields=missing_request,
            )
        for field in ("system", "user"):
            if not isinstance(request.get(field), str):
                invalid.append("request.{}".format(field))
        for field in ("system_hash", "user_hash", "prompt_hash"):
            if not _is_sha256(request.get(field)):
                invalid.append("request.{}".format(field))

        system = request.get("system")
        user = request.get("user")
        if isinstance(system, str) and isinstance(user, str):
            expected_request_hashes = {
                "system_hash": _sha256_text(system),
                "user_hash": _sha256_text(user),
                "prompt_hash": _prompt_hash(system, user),
            }
            for field, expected in expected_request_hashes.items():
                if request.get(field) != expected:
                    invalid.append("request.{}".format(field))

    raw_response = record.get("raw_response")
    if isinstance(raw_response, str) and record.get("response_hash") != _sha256_text(
        raw_response
    ):
        invalid.append("response_hash")

    if invalid:
        raise RecordingSchemaValidationError(
            "recording_schema_1_2_invalid_fields",
            line_number=line_number,
            fields=invalid,
        )


def validate_v12_record_sequence(records: Sequence[Mapping[str, Any]]) -> None:
    """Validate complete call and batch ordering before simulation starts."""

    cursor = 0
    expected_batch = 1
    while cursor < len(records):
        first = records[cursor]
        batch_size = first.get("batch_size")
        if type(batch_size) is not int or batch_size < 1:
            raise RecordingSchemaValidationError(
                "recording_schema_1_2_invalid_batch_identity",
                line_number=cursor + 1,
                fields=("batch_size",),
            )
        if cursor + batch_size > len(records):
            raise RecordingSchemaValidationError(
                "recording_schema_1_2_invalid_batch_identity",
                line_number=cursor + 1,
                fields=("batch_size", "batch_index"),
                detail="batch extends past end of recording",
            )
        expected_round = first.get("round")
        for batch_index in range(batch_size):
            line_number = cursor + batch_index + 1
            record = records[cursor + batch_index]
            fields = []
            if record.get("sequence") != line_number:
                fields.append("sequence")
            if record.get("batch_sequence") != expected_batch:
                fields.append("batch_sequence")
            if record.get("batch_index") != batch_index:
                fields.append("batch_index")
            if record.get("batch_size") != batch_size:
                fields.append("batch_size")
            if record.get("round") != expected_round:
                fields.append("round")
            if fields:
                raise RecordingSchemaValidationError(
                    "recording_schema_1_2_invalid_batch_identity",
                    line_number=line_number,
                    fields=fields,
                )
        cursor += batch_size
        expected_batch += 1


def validate_v12_record_collection(records: Sequence[Mapping[str, Any]]) -> None:
    """Validate a complete 1.2 file, including cross-record identities."""

    if not records:
        raise RecordingSchemaValidationError("empty_recording")
    for line_number, record in enumerate(records, 1):
        validate_v12_record(record, line_number=line_number)
    validate_v12_record_sequence(records)

    identity_fields = (
        *SOURCE_COMPATIBILITY_FIELDS,
        *CONFIG_CONTRACT_RECORD_FIELDS,
        "git_commit",
        "git_dirty",
        "model_config",
    )
    first = records[0]
    for line_number, record in enumerate(records[1:], 2):
        changed = [
            field for field in identity_fields if record.get(field) != first.get(field)
        ]
        if changed:
            raise RecordingSchemaValidationError(
                "recording_schema_1_2_mixed_identity",
                line_number=line_number,
                fields=changed,
            )


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and _HEX_SHA256.fullmatch(value) is not None


__all__ = [
    "CURRENT_RECORDING_SCHEMA_VERSION",
    "RECORDING_COMPATIBILITY_MATRIX",
    "RECORD_TYPE",
    "RecordingCompatibilityRule",
    "RecordingSchemaValidationError",
    "SOURCE_COMPATIBILITY_FIELDS",
    "SUPPORTED_RECORDING_SCHEMA_VERSIONS",
    "V12_REQUIRED_REQUEST_FIELDS",
    "V12_REQUIRED_TOP_LEVEL_FIELDS",
    "compatibility_rule_for_record",
    "validate_v12_metadata",
    "validate_v12_record",
    "validate_v12_record_collection",
    "validate_v12_record_sequence",
]
