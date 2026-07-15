"""LLM record/replay adapters for exact-run audit and deterministic replay.

``RecordingLLM`` wraps the repository's existing ``complete`` /
``complete_batch`` interface.  It does not alter prompts, parse responses or
move the cache boundary: it simply records the exact request handed to the
wrapped object and the exact string returned by it.

``ReplayLLM`` owns no provider and has no network fallback.  Calls are served in
recorded order only after strict request, persona, batch and model-configuration
matching.  Replay is therefore suitable for reproducing and debugging an
existing run, not for answering counterfactual prompts from a different market
state.
"""
from __future__ import annotations

import datetime as _datetime
import hashlib
import json
import os
import pathlib
import threading
from collections.abc import Mapping, Sequence
from typing import Any, Optional, Union

from .events import EventLogger, json_safe


RECORD_SCHEMA_VERSION = "1.0"
RECORD_TYPE = "llm_call"


class ReplayMismatchError(RuntimeError):
    """A replay request did not exactly match the next recorded request."""


def _utc_now() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        json_safe(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _prompt_hash(system: str, user: str) -> str:
    # Length-prefixing removes any possible ambiguity between the two strings.
    payload = "{}:{}{}:{}".format(len(system), system, len(user), user)
    return _sha256_text(payload)


def _is_secret_key(key: object) -> bool:
    normalised = "".join(ch for ch in str(key).lower() if ch.isalnum())
    # ``max_tokens`` is sampling configuration and must remain part of strict
    # replay matching.  Match credential-shaped names rather than every key
    # containing the substring "token".
    return (
        "apikey" in normalised
        or normalised in {"authorization", "password", "secret", "token"}
        or normalised.endswith(
            ("accesstoken", "apitoken", "authtoken", "bearertoken", "password", "secret")
        )
    )


def _without_secrets(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _without_secrets(item)
            for key, item in value.items()
            if not _is_secret_key(key)
        }
    if isinstance(value, (list, tuple)):
        return [_without_secrets(item) for item in value]
    return json_safe(value)


def canonical_model_config(model_config: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    """Return the stable, secret-free model configuration used for matching."""

    if model_config is None:
        return {}
    cleaned = _without_secrets(model_config)
    # JSON round-trip guarantees only standard JSON values remain and produces
    # an independent object callers cannot mutate after construction.
    return json.loads(_canonical_json(cleaned))


def _nested_attr(value: Any, name: str) -> Any:
    current = value
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if hasattr(current, name):
            return getattr(current, name)
        current = getattr(current, "inner", None)
    return None


def model_config_from_llm(
    llm: Any, overrides: Optional[Mapping[str, Any]] = None
) -> dict[str, Any]:
    """Build the minimal model/cache configuration relevant to replay."""

    config: dict[str, Any] = {
        "provider": getattr(llm, "kind", None),
        "model": getattr(llm, "model", None),
        "temperature": _nested_attr(llm, "temperature"),
        "max_tokens": _nested_attr(llm, "max_tokens"),
    }
    if hasattr(llm, "enabled"):
        config["cache_enabled"] = bool(getattr(llm, "enabled"))
    if overrides:
        config.update(dict(overrides))
    return canonical_model_config(config)


def recorded_model_config(
    source: Union[str, os.PathLike[str]],
) -> dict[str, Any]:
    """Read the single model configuration declared by a replay source."""

    path = _resolve_records_path(source, must_exist=True)
    records = _load_records(path)
    if not records:
        return {}
    first = dict(records[0]["model_config"])
    if any(record["model_config"] != first for record in records[1:]):
        raise ReplayMismatchError("record file contains multiple model configurations")
    return first


def runtime_model_config(
    cfg: Any,
    llm: Any = None,
    recorded: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Build the strict, secret-free model identity for record or replay.

    With ``llm`` present, the actual resolved provider/model wins, faithfully
    capturing constructor fallbacks. Offline replay may pass ``recorded``;
    recorded defaults are inherited only while the requested provider remains
    unchanged, so explicit provider/model/sampling changes are rejected.
    """

    requested = str(
        os.environ.get("LLM_PROVIDER") or getattr(cfg, "provider", "auto")
    ).lower()
    source = canonical_model_config(recorded)
    source_requested = str(source.get("requested_provider", ""))

    if llm is not None:
        resolved = str(getattr(llm, "kind", "mock") or "mock")
    elif source and requested == source_requested:
        resolved = str(source.get("resolved_provider", source.get("provider", "mock")))
    elif requested == "auto":
        resolved = "anthropic" if os.environ.get("ANTHROPIC_API_KEY") else "mock"
    elif requested in ("anthropic", "openai", "mock"):
        resolved = requested
    else:
        # Preserve build_llm's existing unknown-provider compatibility behavior.
        resolved = "mock"

    explicit_model = os.environ.get("LLM_MODEL") or getattr(cfg, "model", "") or None
    if llm is not None:
        model = getattr(llm, "model", None) or (
            "mock" if resolved == "mock" else explicit_model
        )
    elif explicit_model is not None:
        model = explicit_model
    elif source and requested == source_requested:
        model = source.get("model")
    elif resolved == "openai":
        model = getattr(cfg, "openai_model", None)
    elif getattr(cfg, "use_cheap_model", False):
        model = getattr(cfg, "cheap_model", None) or None
    else:
        model = "mock" if resolved == "mock" else None

    endpoint = None
    if resolved == "openai" or requested == "openai":
        endpoint = (
            os.environ.get("OPENAI_BASE_URL")
            or getattr(cfg, "openai_base_url", None)
        )

    return canonical_model_config({
        "requested_provider": requested,
        "resolved_provider": resolved,
        "provider": resolved,
        "model": model,
        "temperature": getattr(cfg, "temperature", None),
        "max_tokens": getattr(cfg, "max_tokens", None),
        "cache_enabled": bool(getattr(cfg, "cache_enabled", False)),
        "use_cheap_model": bool(getattr(cfg, "use_cheap_model", False)),
        # Endpoint identity affects which served model is reached, but the URL
        # itself may contain userinfo. Match its digest without persisting it.
        "endpoint_sha256": _sha256_text(str(endpoint)) if endpoint else None,
    })


def request_fingerprint(system: str, user: str) -> dict[str, str]:
    """Hashes for an exact, unmodified prompt pair."""

    return {
        "system_hash": _sha256_text(system),
        "user_hash": _sha256_text(user),
        "prompt_hash": _prompt_hash(system, user),
    }


def _resolve_records_path(
    source: Union[str, os.PathLike[str]], *, must_exist: bool = False
) -> pathlib.Path:
    path = pathlib.Path(source)
    if path.is_dir() or (not path.exists() and path.suffix.lower() != ".jsonl"):
        path = path / "llm_records.jsonl"
    if must_exist and not path.is_file():
        raise FileNotFoundError("LLM replay records not found: {}".format(path))
    return path


def _prepare_private_file(path: pathlib.Path, allow_append: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size and not allow_append:
        raise FileExistsError(
            "refusing to append to non-empty LLM record file: {}".format(path)
        )
    descriptor = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    os.close(descriptor)
    os.chmod(str(path), 0o600)


def _append_record(path: pathlib.Path, record: Mapping[str, Any]) -> None:
    line = (_canonical_json(record) + "\n").encode("utf-8")
    descriptor = os.open(
        str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600
    )
    try:
        os.write(descriptor, line)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.chmod(str(path), 0o600)


def _context_item(item: Any) -> dict[str, Optional[str]]:
    if item is None:
        return {"agent_id": None, "persona_id": None}
    if isinstance(item, Mapping):
        agent = item.get("agent_id", item.get("agent", item.get("name")))
        persona = item.get("persona_id", item.get("persona"))
    elif isinstance(item, (tuple, list)):
        agent = item[0] if len(item) >= 1 else None
        persona = item[1] if len(item) >= 2 else None
    elif isinstance(item, str):
        agent = item
        persona = None
    else:
        agent = getattr(item, "agent_id", getattr(item, "name", None))
        persona = getattr(item, "persona_id", getattr(item, "persona", None))
    return {
        "agent_id": None if agent is None else str(agent),
        "persona_id": None if persona is None else str(persona),
    }


class _ContextMixin:
    def _init_context(self) -> None:
        self._context_round: Optional[int] = None
        self._context_metadata: Optional[list[dict[str, Optional[str]]]] = None

    def set_batch_context(
        self, round_i: Optional[int], metadata: Optional[Sequence[Any]] = None
    ) -> None:
        """Attach round/agent/persona metadata to the next LLM call only."""

        self._context_round = None if round_i is None else int(round_i)
        self._context_metadata = (
            None if metadata is None else [_context_item(item) for item in metadata]
        )

    def _take_context(
        self, size: int
    ) -> tuple[Optional[int], list[dict[str, Optional[str]]]]:
        round_i = self._context_round
        metadata = self._context_metadata or []
        normalised = [
            metadata[index]
            if index < len(metadata)
            else {"agent_id": None, "persona_id": None}
            for index in range(size)
        ]
        self._context_round = None
        self._context_metadata = None
        return round_i, normalised


class RecordingLLM(_ContextMixin):
    """Record exact calls made through an existing LLM-compatible object."""

    def __init__(
        self,
        inner: Any,
        records_path: Union[str, os.PathLike[str]],
        model_config: Optional[Mapping[str, Any]] = None,
        event_logger: Optional[EventLogger] = None,
        *,
        allow_append: bool = False,
    ) -> None:
        if inner is None:
            raise ValueError("RecordingLLM requires an inner LLM")
        self.inner = inner
        self.kind = getattr(inner, "kind", "mock")
        self.model = getattr(inner, "model", None)
        inferred_config = model_config_from_llm(inner)
        if model_config is not None:
            inferred_config.update(dict(model_config))
        self.model_config = canonical_model_config(inferred_config)
        self.records_path = _resolve_records_path(records_path)
        self.event_logger = event_logger
        _prepare_private_file(self.records_path, allow_append=allow_append)

        self._lock = threading.RLock()
        self._sequence = 0
        self._batch_sequence = 0
        self.request_count = 0
        self.response_count = 0
        self.record_count = 0
        self.batch_sizes: list[int] = []
        self._init_context()

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "requests": self.request_count,
            "responses": self.response_count,
            "records": self.record_count,
            "batches": len(self.batch_sizes),
            "batch_sizes": list(self.batch_sizes),
        }

    def __getattr__(self, name: str) -> Any:
        # Preserve optional attributes (tracker, enabled, etc.) without changing
        # the small complete/complete_batch contract used by the simulator.
        if name == "inner":
            raise AttributeError(name)
        return getattr(self.inner, name)

    def _emit_request(self, call: Mapping[str, Any]) -> None:
        if self.event_logger is None:
            return
        fingerprint = call["request"]
        public = {
            "sequence": call["sequence"],
            "batch_sequence": call["batch_sequence"],
            "batch_index": call["batch_index"],
            "batch_size": call["batch_size"],
            "persona_id": call["persona_id"],
            "prompt_hash": fingerprint["prompt_hash"],
            "system_hash": fingerprint["system_hash"],
            "user_hash": fingerprint["user_hash"],
            "model_config": self.model_config,
        }
        self.event_logger.emit(
            "LLMRequestRecorded",
            round_i=call["round"],
            agent_id=call["agent_id"],
            data=public,
            private_data={
                "system_prompt": fingerprint["system"],
                "user_prompt": fingerprint["user"],
            },
        )

    def _emit_response(self, call: Mapping[str, Any], raw_response: str) -> None:
        if self.event_logger is None:
            return
        self.event_logger.emit(
            "LLMResponseRecorded",
            round_i=call["round"],
            agent_id=call["agent_id"],
            data={
                "sequence": call["sequence"],
                "batch_sequence": call["batch_sequence"],
                "batch_index": call["batch_index"],
                "persona_id": call["persona_id"],
                "response_hash": _sha256_text(raw_response),
                "source": "record",
            },
            private_data={"raw_response": raw_response},
        )

    def _make_calls(
        self, prompts: Sequence[tuple[str, str]]
    ) -> list[dict[str, Any]]:
        round_i, metadata = self._take_context(len(prompts))
        self._batch_sequence += 1
        self.batch_sizes.append(len(prompts))
        calls = []
        for index, ((system, user), context) in enumerate(zip(prompts, metadata)):
            if not isinstance(system, str) or not isinstance(user, str):
                raise TypeError("LLM prompts must be (str, str) pairs")
            self._sequence += 1
            fingerprint = request_fingerprint(system, user)
            calls.append(
                {
                    "sequence": self._sequence,
                    "round": round_i,
                    "batch_sequence": self._batch_sequence,
                    "batch_index": index,
                    "batch_size": len(prompts),
                    "agent_id": context["agent_id"],
                    "persona_id": context["persona_id"],
                    "request": {
                        "system": system,
                        "user": user,
                        **fingerprint,
                    },
                }
            )
        self.request_count += len(calls)
        for call in calls:
            self._emit_request(call)
        return calls

    def _store_responses(
        self, calls: Sequence[Mapping[str, Any]], responses: Sequence[str]
    ) -> None:
        if len(calls) != len(responses):
            raise RuntimeError(
                "LLM returned {} responses for {} prompts".format(
                    len(responses), len(calls)
                )
            )
        for call, raw_response in zip(calls, responses):
            if not isinstance(raw_response, str):
                raise TypeError("LLM response must be str, got {}".format(type(raw_response)))
            record = {
                "schema_version": RECORD_SCHEMA_VERSION,
                "record_type": RECORD_TYPE,
                "recorded_at": _utc_now(),
                "run_id": (
                    self.event_logger.run_id if self.event_logger is not None else None
                ),
                **dict(call),
                "model_config": self.model_config,
                "raw_response": raw_response,
                "response_hash": _sha256_text(raw_response),
            }
            _append_record(self.records_path, record)
            self.response_count += 1
            self.record_count += 1
            self._emit_response(call, raw_response)

    def complete(self, system: str, user: str) -> str:
        with self._lock:
            calls = self._make_calls([(system, user)])
            response = self.inner.complete(system, user)
            self._store_responses(calls, [response])
            return response

    def complete_batch(self, prompts: Sequence[tuple[str, str]]) -> list[str]:
        prompt_list = list(prompts)
        with self._lock:
            calls = self._make_calls(prompt_list)
            responses = list(self.inner.complete_batch(prompt_list))
            self._store_responses(calls, responses)
            return responses


def _load_records(path: pathlib.Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise ReplayMismatchError(
                    "invalid JSON in replay records at line {}: {}".format(
                        line_number, error
                    )
                ) from error
            if not isinstance(record, dict):
                raise ReplayMismatchError(
                    "replay record at line {} is not an object".format(line_number)
                )
            if record.get("schema_version") != RECORD_SCHEMA_VERSION:
                raise ReplayMismatchError(
                    "unsupported replay schema at line {}: {!r}".format(
                        line_number, record.get("schema_version")
                    )
                )
            if record.get("record_type") != RECORD_TYPE:
                raise ReplayMismatchError(
                    "unsupported replay record type at line {}: {!r}".format(
                        line_number, record.get("record_type")
                    )
                )
            expected_sequence = len(records) + 1
            if record.get("sequence") != expected_sequence:
                raise ReplayMismatchError(
                    "non-contiguous replay call sequence at line {}: expected {}, got {}".format(
                        line_number, expected_sequence, record.get("sequence")
                    )
                )
            request = record.get("request")
            if not isinstance(request, dict):
                raise ReplayMismatchError(
                    "missing request object in replay record at line {}".format(line_number)
                )
            system, user = request.get("system"), request.get("user")
            raw_response = record.get("raw_response")
            if not isinstance(system, str) or not isinstance(user, str):
                raise ReplayMismatchError(
                    "non-string replay prompt at line {}".format(line_number)
                )
            if not isinstance(raw_response, str):
                raise ReplayMismatchError(
                    "non-string replay response at line {}".format(line_number)
                )
            fingerprint = request_fingerprint(system, user)
            for key, expected in fingerprint.items():
                if request.get(key) != expected:
                    raise ReplayMismatchError(
                        "corrupt {} at replay line {}".format(key, line_number)
                    )
            if record.get("response_hash") != _sha256_text(raw_response):
                raise ReplayMismatchError(
                    "corrupt response_hash at replay line {}".format(line_number)
                )
            record["model_config"] = canonical_model_config(record.get("model_config", {}))
            records.append(record)
    return records


class ReplayLLM(_ContextMixin):
    """Serve exact recorded responses without constructing or calling a provider."""

    def __init__(
        self,
        source: Union[str, os.PathLike[str]],
        model_config: Optional[Mapping[str, Any]] = None,
        event_logger: Optional[EventLogger] = None,
    ) -> None:
        self.records_path = _resolve_records_path(source, must_exist=True)
        self._records = _load_records(self.records_path)
        self.total_records = len(self._records)
        self.source_model_config = (
            dict(self._records[0]["model_config"]) if self._records else {}
        )
        for record in self._records[1:]:
            if record["model_config"] != self.source_model_config:
                raise ReplayMismatchError(
                    "record file contains multiple model configurations"
                )

        self.model_config = canonical_model_config(
            self.source_model_config if model_config is None else model_config
        )
        if self._records and self.model_config != self.source_model_config:
            raise ReplayMismatchError(
                "model configuration mismatch: recorded={} requested={}".format(
                    _sha256_text(_canonical_json(self.source_model_config)),
                    _sha256_text(_canonical_json(self.model_config)),
                )
            )

        self.kind = str(
            self.source_model_config.get(
                "resolved_provider",
                self.source_model_config.get(
                    "provider", self.source_model_config.get("kind", "replay")
                ),
            )
            or "replay"
        )
        self.model = self.source_model_config.get("model")
        self.event_logger = event_logger
        self._cursor = 0
        self._batch_sequence = 0
        self.request_count = 0
        self.response_count = 0
        self.record_count = 0
        self.batch_sizes: list[int] = []
        self._lock = threading.RLock()
        self._init_context()

    @property
    def records_consumed(self) -> int:
        return self._cursor

    @property
    def remaining_records(self) -> int:
        return self.total_records - self._cursor

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "requests": self.request_count,
            "responses": self.response_count,
            "records": self.record_count,
            "total_records": self.total_records,
            "remaining_records": self.remaining_records,
            "batches": len(self.batch_sizes),
            "batch_sizes": list(self.batch_sizes),
        }

    def _mismatch(
        self, sequence: int, field: str, recorded: Any, requested: Any
    ) -> ReplayMismatchError:
        return ReplayMismatchError(
            "replay mismatch at call {} for {}: recorded={!r}, requested={!r}".format(
                sequence, field, recorded, requested
            )
        )

    def _emit_request(
        self,
        *,
        sequence: int,
        round_i: Optional[int],
        batch_sequence: int,
        batch_index: int,
        batch_size: int,
        context: Mapping[str, Optional[str]],
        system: str,
        user: str,
        fingerprint: Mapping[str, str],
    ) -> None:
        if self.event_logger is None:
            return
        self.event_logger.emit(
            "LLMRequestRecorded",
            round_i=round_i,
            agent_id=context["agent_id"],
            data={
                "sequence": sequence,
                "batch_sequence": batch_sequence,
                "batch_index": batch_index,
                "batch_size": batch_size,
                "persona_id": context["persona_id"],
                **dict(fingerprint),
                "model_config": self.model_config,
                "source": "replay",
            },
            private_data={"system_prompt": system, "user_prompt": user},
        )

    def _emit_response(self, record: Mapping[str, Any]) -> None:
        if self.event_logger is None:
            return
        self.event_logger.emit(
            "LLMResponseRecorded",
            round_i=record.get("round"),
            agent_id=record.get("agent_id"),
            data={
                "sequence": record["sequence"],
                "batch_sequence": record["batch_sequence"],
                "batch_index": record["batch_index"],
                "persona_id": record.get("persona_id"),
                "response_hash": record["response_hash"],
                "source": "replay",
            },
            private_data={"raw_response": record["raw_response"]},
        )

    def _complete_many(self, prompts: Sequence[tuple[str, str]]) -> list[str]:
        round_i, metadata = self._take_context(len(prompts))
        batch_sequence = self._batch_sequence + 1
        batch_size = len(prompts)
        self.request_count += batch_size

        candidates: list[dict[str, Any]] = []
        for index, ((system, user), context) in enumerate(zip(prompts, metadata)):
            if not isinstance(system, str) or not isinstance(user, str):
                raise TypeError("LLM prompts must be (str, str) pairs")
            sequence = self._cursor + index + 1
            fingerprint = request_fingerprint(system, user)
            self._emit_request(
                sequence=sequence,
                round_i=round_i,
                batch_sequence=batch_sequence,
                batch_index=index,
                batch_size=batch_size,
                context=context,
                system=system,
                user=user,
                fingerprint=fingerprint,
            )
            if self._cursor + index >= self.total_records:
                raise ReplayMismatchError(
                    "replay exhausted before call {} ({} records total)".format(
                        sequence, self.total_records
                    )
                )
            record = self._records[self._cursor + index]
            comparisons = (
                ("sequence", record.get("sequence"), sequence),
                ("round", record.get("round"), round_i),
                ("batch_sequence", record.get("batch_sequence"), batch_sequence),
                ("batch_index", record.get("batch_index"), index),
                ("batch_size", record.get("batch_size"), batch_size),
                ("agent_id", record.get("agent_id"), context["agent_id"]),
                ("persona_id", record.get("persona_id"), context["persona_id"]),
                (
                    "prompt_hash",
                    record["request"].get("prompt_hash"),
                    fingerprint["prompt_hash"],
                ),
                ("model_config", record.get("model_config"), self.model_config),
            )
            for field, recorded, requested in comparisons:
                if recorded != requested:
                    # Prompt/model values are reported as hashes or structured
                    # sampling metadata; full private text never enters errors.
                    raise self._mismatch(sequence, field, recorded, requested)
            candidates.append(record)

        # Commit consumption only after the whole batch matches.  A failed
        # replay cannot silently advance partway through a batch.
        self._batch_sequence = batch_sequence
        self.batch_sizes.append(batch_size)
        self._cursor += batch_size
        self.response_count += batch_size
        self.record_count += batch_size
        for record in candidates:
            self._emit_response(record)
        return [str(record["raw_response"]) for record in candidates]

    def complete(self, system: str, user: str) -> str:
        with self._lock:
            return self._complete_many([(system, user)])[0]

    def complete_batch(self, prompts: Sequence[tuple[str, str]]) -> list[str]:
        prompt_list = list(prompts)
        with self._lock:
            return self._complete_many(prompt_list)

    def assert_exhausted(self) -> None:
        """Fail if a replay stopped before consuming all source calls."""

        if self.remaining_records:
            next_sequence = self._cursor + 1
            raise ReplayMismatchError(
                "replay ended early: {} unconsumed records (next call {})".format(
                    self.remaining_records, next_sequence
                )
            )
