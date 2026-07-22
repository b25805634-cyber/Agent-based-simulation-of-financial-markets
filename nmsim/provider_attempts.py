"""Application-visible Provider retry-attempt provenance.

The simulation and Record/Replay contracts operate on one *logical* LLM
request.  OpenAI- and Anthropic-compatible adapters may make several visible
application-level attempts for that request after parse failures or Provider
exceptions.  This module carries immutable logical-request identity through
the cache boundary and exposes a small observer protocol for those attempts.

It deliberately does not claim visibility into retries performed internally
by an SDK, HTTP transport, proxy, or remote serving stack.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import threading
from typing import Any, Optional, Protocol, Sequence, runtime_checkable


PROVIDER_ATTEMPT_SCHEMA = "provider_attempt_v1"
REPORTED_MODEL_ALIAS_MAX_CHARS = 256


def sha256_text(value: str) -> str:
    """Return the SHA-256 identity used for public prompt/response evidence."""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def prompt_hash(system: str, user: str) -> str:
    """Hash a prompt pair without concatenation ambiguity."""

    payload = "{}:{}{}:{}".format(len(system), system, len(user), user)
    return sha256_text(payload)


def safe_reported_model(value: Any) -> Optional[str]:
    """Return one exact bounded printable alias, or ``None`` without trimming."""

    if not isinstance(value, str):
        return None
    if (
        not 1 <= len(value) <= REPORTED_MODEL_ALIAS_MAX_CHARS
        or value != value.strip()
        or not value.isprintable()
    ):
        return None
    return value


@runtime_checkable
class ProviderAttemptObserver(Protocol):
    """Sink for one application-visible Provider attempt.

    Implementations are part of the fail-closed provenance boundary: an
    observer exception must propagate to the caller and must not be converted
    into an ``api-error; holding`` model fallback.
    """

    def observe_provider_attempt(
        self,
        context: "ProviderAttemptContext",
        observation: "ProviderAttemptObservation",
    ) -> None:
        """Persist one attempt before the adapter retries or returns."""


@dataclass(frozen=True)
class ProviderAttemptContext:
    """Immutable identity of one logical request crossing the cache boundary."""

    logical_sequence: int
    round_i: Optional[int]
    batch_sequence: int
    batch_index: int
    batch_size: int
    agent: Optional[str]
    persona: Optional[str]
    original_system_hash: str
    original_user_hash: str
    original_prompt_hash: str
    observer: Optional[ProviderAttemptObserver] = field(
        default=None, repr=False, compare=False
    )


@dataclass(frozen=True)
class ProviderAttemptObservation:
    """One Provider adapter-loop attempt, including restricted audit detail."""

    attempt_index: int
    max_attempts: int
    provider: str
    model: Optional[str]
    attempted_system: str = field(repr=False, compare=False)
    attempted_user: str = field(repr=False, compare=False)
    trigger: str
    outcome: str
    reported_model: Optional[str] = None
    response_text: Optional[str] = field(default=None, repr=False, compare=False)
    exception: Optional[BaseException] = field(default=None, repr=False, compare=False)
    latency_ms: float = 0.0
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    will_retry: bool = False

    def public_payload(self, context: ProviderAttemptContext) -> dict[str, Any]:
        """Return a public-safe event payload containing identities and codes."""

        reported_model = safe_reported_model(self.reported_model)
        return {
            "provider_attempt_schema": PROVIDER_ATTEMPT_SCHEMA,
            "logical_sequence": int(context.logical_sequence),
            "batch_sequence": int(context.batch_sequence),
            "batch_index": int(context.batch_index),
            "batch_size": int(context.batch_size),
            "round": context.round_i,
            "agent": context.agent,
            "persona": context.persona,
            "attempt_index": int(self.attempt_index),
            "max_attempts": int(self.max_attempts),
            "provider": str(self.provider),
            "model": None if self.model is None else str(self.model),
            "reported_model": reported_model,
            "reported_model_alias_invalid": bool(
                self.reported_model is not None and reported_model is None
            ),
            "original_prompt_hash": context.original_prompt_hash,
            "attempted_prompt_hash": prompt_hash(
                self.attempted_system, self.attempted_user
            ),
            "trigger": str(self.trigger),
            "outcome": str(self.outcome),
            "response_hash": (
                None if self.response_text is None else sha256_text(self.response_text)
            ),
            "latency_ms": float(self.latency_ms),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "will_retry": bool(self.will_retry),
        }

    def private_payload(self) -> dict[str, Any]:
        """Return restricted prompts, raw response, and exception diagnostics."""

        exception_type = None
        exception_detail = None
        if self.exception is not None:
            error_type = type(self.exception)
            exception_type = "{}.{}".format(
                error_type.__module__, error_type.__qualname__
            )
            exception_detail = str(self.exception)
        return {
            "attempted_system_prompt": self.attempted_system,
            "attempted_user_prompt": self.attempted_user,
            "intermediate_raw_response": self.response_text,
            "exception_type": exception_type,
            "exception_detail": exception_detail,
        }


class ProviderAttemptContextCarrier:
    """One-shot context handoff used by RecordingLLM, cache, and adapters."""

    def _init_provider_attempt_contexts(self) -> None:
        self._provider_attempt_context_lock = threading.RLock()
        self._pending_provider_attempt_contexts: Optional[
            tuple[Optional[ProviderAttemptContext], ...]
        ] = None

    def set_provider_attempt_contexts(
        self, contexts: Sequence[Optional[ProviderAttemptContext]]
    ) -> None:
        values = tuple(contexts)
        with self._provider_attempt_context_lock:
            if self._pending_provider_attempt_contexts is not None:
                raise RuntimeError("unconsumed Provider-attempt context")
            self._pending_provider_attempt_contexts = values

    def _take_provider_attempt_contexts(
        self, size: int
    ) -> list[Optional[ProviderAttemptContext]]:
        with self._provider_attempt_context_lock:
            pending = self._pending_provider_attempt_contexts
            self._pending_provider_attempt_contexts = None
        if pending is None:
            return [None] * size
        if len(pending) != size:
            raise RuntimeError(
                "Provider-attempt context size {} does not match request size {}".format(
                    len(pending), size
                )
            )
        return list(pending)


def forward_provider_attempt_contexts(
    target: Any, contexts: Sequence[Optional[ProviderAttemptContext]]
) -> bool:
    """Set one-shot contexts when the wrapped target supports provenance."""

    setter = getattr(target, "set_provider_attempt_contexts", None)
    if not callable(setter):
        return False
    setter(contexts)
    return True


def observe_provider_attempt(
    context: Optional[ProviderAttemptContext],
    observation: ProviderAttemptObservation,
) -> None:
    """Notify the observer synchronously; observer failures intentionally escape."""

    if context is not None and context.observer is not None:
        context.observer.observe_provider_attempt(context, observation)


__all__ = [
    "PROVIDER_ATTEMPT_SCHEMA",
    "REPORTED_MODEL_ALIAS_MAX_CHARS",
    "ProviderAttemptContext",
    "ProviderAttemptContextCarrier",
    "ProviderAttemptObservation",
    "ProviderAttemptObserver",
    "forward_provider_attempt_contexts",
    "observe_provider_attempt",
    "prompt_hash",
    "safe_reported_model",
    "sha256_text",
]
