"""Append-only structured event logs for simulation provenance.

Two logs are kept deliberately separate:

``events.jsonl``
    The public, operational event stream.  Full prompts, raw completions and
    private reasoning are rejected before a line can be written.

``private_events.jsonl``
    The restricted audit stream.  Callers may attach sensitive details with
    ``private_data``; the file is always chmod 0600.

The logger is intentionally small and uses only the standard library.  A
logical event written to both streams has the same event id and timestamp, so
the streams can be joined without copying private content into the public log.
"""
from __future__ import annotations

import dataclasses
import datetime as _datetime
import enum
import json
import math
import os
import pathlib
import threading
from collections.abc import Mapping
from typing import Any, Optional, Union


SCHEMA_VERSION = "1.0"


class PublicEventPrivacyError(ValueError):
    """Raised when sensitive material is about to enter the public log."""


# Exact key matching is used so harmless fields such as ``response_hash`` and
# ``prompt_hash`` remain legal.  Keys are normalised by removing punctuation,
# making variants such as ``raw-response`` and ``RAW_RESPONSE`` equivalent.
_PUBLIC_FORBIDDEN_KEYS = {
    "completion",
    "fullcompletion",
    "fullprompt",
    "privaterationale",
    "privatereasoning",
    "prompt",
    "rationale",
    "rawcompletion",
    "rawresponse",
    "reasoning",
    "responsetext",
    "system",
    "systemprompt",
    "user",
    "userprompt",
}


def _normalise_key(key: object) -> str:
    return "".join(ch for ch in str(key).lower() if ch.isalnum())


def json_safe(value: Any) -> Any:
    """Return a deterministic, JSON-serialisable representation of ``value``.

    Event payloads are normally plain dictionaries.  Supporting dataclasses,
    paths, enums, dates and sets here keeps instrumentation from forcing core
    simulation types to know about serialisation.  Non-finite floats are
    represented as strings because strict JSON does not define them.
    """

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        if math.isnan(value):
            return "NaN"
        return "Infinity" if value > 0 else "-Infinity"
    if isinstance(value, pathlib.Path):
        return str(value)
    if isinstance(value, enum.Enum):
        return json_safe(value.value)
    if isinstance(value, (_datetime.datetime, _datetime.date, _datetime.time)):
        return value.isoformat()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return json_safe(dataclasses.asdict(value))
    if isinstance(value, Mapping):
        # JSON object keys are strings.  Sorting happens in ``_json_line``;
        # coercion here is explicit so serialisation cannot fail midway through
        # an append operation.
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, (set, frozenset)):
        normalised = [json_safe(item) for item in value]
        return sorted(
            normalised,
            key=lambda item: json.dumps(
                item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
        )
    if isinstance(value, bytes):
        return value.hex()
    if hasattr(value, "_asdict"):
        return json_safe(value._asdict())
    # Avoid a default ``repr`` because it commonly embeds a process-specific
    # memory address.  Callers should prefer explicit dictionaries, but this
    # stable type marker still keeps an incidental diagnostic value from
    # breaking the event stream.
    value_type = type(value)
    return "<unsupported:{}.{!s}>".format(value_type.__module__, value_type.__qualname__)


def _json_line(value: Mapping[str, Any]) -> bytes:
    payload = json.dumps(
        json_safe(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return (payload + "\n").encode("utf-8")


def _assert_public_safe(value: Any, path: str = "data") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _normalise_key(key) in _PUBLIC_FORBIDDEN_KEYS:
                raise PublicEventPrivacyError(
                    "sensitive field {!r} is not permitted in public event {}".format(
                        str(key), path
                    )
                )
            _assert_public_safe(item, "{}.{}".format(path, key))
    elif isinstance(value, (list, tuple, set, frozenset)):
        for index, item in enumerate(value):
            _assert_public_safe(item, "{}[{}]".format(path, index))


def _touch_append_only(path: pathlib.Path, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, mode)
    os.close(descriptor)
    # umask only removes permissions on creation; chmod enforces the private
    # mode even if a pre-existing file was more permissive.
    os.chmod(str(path), mode)


def _append_line(path: pathlib.Path, line: bytes, mode: int) -> None:
    descriptor = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, mode)
    try:
        os.write(descriptor, line)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _event_number(event_id: object) -> int:
    text = str(event_id)
    if text.startswith("evt-") and text[4:].isdigit():
        return int(text[4:])
    return 0


def _existing_counter(paths: list[pathlib.Path]) -> int:
    highest = 0
    for path in paths:
        if not path.exists():
            continue
        try:
            with path.open("r", encoding="utf-8") as stream:
                for line in stream:
                    if not line.strip():
                        continue
                    try:
                        highest = max(highest, _event_number(json.loads(line)["event_id"]))
                    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                        # Do not rewrite or truncate a damaged append-only log.
                        # A subsequent id still advances beyond every valid id.
                        continue
        except OSError:
            continue
    return highest


class EventLogger:
    """Write public and private JSONL event streams.

    Parameters
    ----------
    run_id:
        Stable identifier included in every envelope.
    run_dir:
        Directory containing the conventional ``events.jsonl`` and
        ``private_events.jsonl`` files.  Explicit paths can instead be supplied
        with ``public_path`` and ``private_path``.
    """

    def __init__(
        self,
        run_id: str,
        run_dir: Optional[Union[str, os.PathLike[str]]] = None,
        *,
        public_path: Optional[Union[str, os.PathLike[str]]] = None,
        private_path: Optional[Union[str, os.PathLike[str]]] = None,
        schema_version: str = SCHEMA_VERSION,
    ) -> None:
        if not run_id:
            raise ValueError("run_id must be non-empty")
        if run_dir is None and (public_path is None or private_path is None):
            raise ValueError("provide run_dir or both public_path and private_path")

        directory = pathlib.Path(run_dir) if run_dir is not None else None
        self.public_path = pathlib.Path(
            public_path if public_path is not None else directory / "events.jsonl"  # type: ignore[operator]
        )
        self.private_path = pathlib.Path(
            private_path
            if private_path is not None
            else directory / "private_events.jsonl"  # type: ignore[operator]
        )
        if self.public_path.resolve() == self.private_path.resolve():
            raise ValueError("public and private event paths must differ")

        self.run_id = str(run_id)
        self.schema_version = str(schema_version)
        self._lock = threading.RLock()
        _touch_append_only(self.public_path, 0o644)
        _touch_append_only(self.private_path, 0o600)
        self._counter = _existing_counter([self.public_path, self.private_path])

    @staticmethod
    def _timestamp() -> str:
        return _datetime.datetime.now(_datetime.timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )

    def _next_envelope(
        self,
        event_type: str,
        round_i: Optional[int],
        agent_id: Optional[str],
        data: Any,
    ) -> dict[str, Any]:
        self._counter += 1
        return {
            "run_id": self.run_id,
            "round": round_i,
            "event_id": "evt-{:08d}".format(self._counter),
            "timestamp": self._timestamp(),
            "agent_id": agent_id,
            "schema_version": self.schema_version,
            "type": str(event_type),
            "data": json_safe({} if data is None else data),
        }

    def emit(
        self,
        event_type: str,
        round_i: Optional[int] = None,
        agent_id: Optional[str] = None,
        data: Any = None,
        private_data: Any = None,
    ) -> str:
        """Append one logical event and return its event id.

        ``data`` is always written publicly.  When ``private_data`` is supplied,
        a second envelope with the same id/timestamp is written to the private
        stream; its data is the public mapping merged with the private mapping.
        Non-mapping private payloads are stored under ``private``.
        """

        if not event_type:
            raise ValueError("event_type must be non-empty")
        public_data = json_safe({} if data is None else data)
        _assert_public_safe(public_data)

        with self._lock:
            public_event = self._next_envelope(
                event_type, round_i, agent_id, public_data
            )
            _append_line(self.public_path, _json_line(public_event), 0o644)

            if private_data is not None:
                if isinstance(public_data, Mapping) and isinstance(private_data, Mapping):
                    combined = dict(public_data)
                    combined.update(json_safe(private_data))
                else:
                    combined = {
                        "public": public_data,
                        "private": json_safe(private_data),
                    }
                private_event = dict(public_event)
                private_event["data"] = combined
                _append_line(self.private_path, _json_line(private_event), 0o600)
                os.chmod(str(self.private_path), 0o600)
            return str(public_event["event_id"])

    def emit_private(
        self,
        event_type: str,
        round_i: Optional[int] = None,
        agent_id: Optional[str] = None,
        data: Any = None,
    ) -> str:
        """Append an event only to the restricted stream."""

        if not event_type:
            raise ValueError("event_type must be non-empty")
        with self._lock:
            event = self._next_envelope(event_type, round_i, agent_id, data)
            _append_line(self.private_path, _json_line(event), 0o600)
            os.chmod(str(self.private_path), 0o600)
            return str(event["event_id"])

    def flush(self) -> None:
        """Compatibility no-op: every append is fsynced immediately."""

    def close(self) -> None:
        """Compatibility no-op: files are opened only for individual appends."""

    def __enter__(self) -> "EventLogger":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()
