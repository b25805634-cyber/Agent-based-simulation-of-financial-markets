"""Declarative capabilities for the provider implementations shipped by nmsim.

This module describes provider behaviour; it does not construct providers,
select credentials, or make network requests.  Callers must use the *resolved*
provider id.  In particular, ``auto`` is a selection policy in :mod:`nmsim.llm`,
not a provider, and therefore has no capability record.

Capabilities are intentionally conservative and describe what the current
adapter exposes to nmsim, not everything an upstream API might support.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import re
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


CAPABILITY_SCHEMA_VERSION = "1.0"


class ProviderCapabilityError(ValueError):
    """Base class for provider-capability contract errors."""


class UnknownProviderCapabilityError(ProviderCapabilityError):
    """Raised when a resolved provider has no reviewed capability record."""


_PROVIDER_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_SECRET_QUERY_RE = re.compile(
    r"(?:api[_-]?key|access[_-]?token|auth(?:orization)?|bearer|password|secret|token)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ProviderCapability:
    """Reviewed capability statement for one resolved provider adapter."""

    provider_id: str
    transport_type: str
    external_network_expected: bool
    authentication_mode: str
    supports_batch: bool
    supports_async: bool
    supports_temperature: bool
    supports_seed: bool
    supports_structured_output: bool
    supports_usage_metadata: bool
    supports_provider_response_id: bool
    supports_record_replay: bool
    supports_cache: bool
    tool_access: str
    deterministic_claim: str
    recommended_concurrency: Optional[int]
    experimental: bool
    capability_schema_version: str = CAPABILITY_SCHEMA_VERSION
    temperature_behavior: str = "unsupported"
    usage_metadata_behavior: str = "unavailable"
    implementation_scope: str = "production"

    def __post_init__(self) -> None:
        if not _PROVIDER_ID_RE.fullmatch(self.provider_id):
            raise ProviderCapabilityError(
                "invalid provider capability id: {!r}".format(self.provider_id)
            )
        if self.capability_schema_version != CAPABILITY_SCHEMA_VERSION:
            raise ProviderCapabilityError(
                "provider capability schema must be {}".format(
                    CAPABILITY_SCHEMA_VERSION
                )
            )
        if self.recommended_concurrency is not None and self.recommended_concurrency < 1:
            raise ProviderCapabilityError("recommended_concurrency must be positive or null")
        if self.external_network_expected and self.deterministic_claim != "none":
            raise ProviderCapabilityError(
                "network providers must not claim deterministic responses"
            )

    def to_dict(self) -> dict[str, Any]:
        """Return a stable JSON-compatible snapshot with no runtime secrets."""

        return asdict(self)


# Evidence for these records lives in nmsim.llm:
# - MockLLM.kind == "mock"
# - AnthropicLLM.kind == "anthropic"
# - OpenAILLM.kind == "openai" (OpenAI-compatible transport, including the
#   configured vLLM/MiniMax endpoint)
# Record/replay and caching are application wrappers, not upstream API claims.
_CAPABILITIES: dict[str, ProviderCapability] = {
    "mock": ProviderCapability(
        provider_id="mock",
        transport_type="in_process_python",
        external_network_expected=False,
        authentication_mode="none",
        supports_batch=True,
        supports_async=True,
        supports_temperature=False,
        supports_seed=True,
        supports_structured_output=True,
        supports_usage_metadata=False,
        supports_provider_response_id=False,
        supports_record_replay=True,
        supports_cache=True,
        tool_access="none",
        deterministic_claim="deterministic_given_seed_and_call_order",
        recommended_concurrency=1,
        experimental=False,
        temperature_behavior="not_used",
        usage_metadata_behavior="not_emitted",
    ),
    "anthropic": ProviderCapability(
        provider_id="anthropic",
        transport_type="anthropic_sdk_https",
        external_network_expected=True,
        authentication_mode="environment",
        supports_batch=True,
        supports_async=True,
        supports_temperature=True,
        supports_seed=False,
        supports_structured_output=False,
        supports_usage_metadata=True,
        supports_provider_response_id=False,
        supports_record_replay=True,
        supports_cache=True,
        tool_access="none",
        deterministic_claim="none",
        recommended_concurrency=None,
        experimental=False,
        temperature_behavior="conditional_by_model_prefix",
        usage_metadata_behavior="input_and_output_tokens_consumed",
    ),
    "openai": ProviderCapability(
        provider_id="openai",
        transport_type="openai_compatible_sdk_http",
        external_network_expected=True,
        authentication_mode="environment_or_config",
        supports_batch=True,
        supports_async=True,
        supports_temperature=True,
        supports_seed=False,
        supports_structured_output=False,
        supports_usage_metadata=True,
        supports_provider_response_id=False,
        supports_record_replay=True,
        supports_cache=True,
        tool_access="none",
        deterministic_claim="none",
        recommended_concurrency=40,
        experimental=False,
        temperature_behavior="sent_on_each_request",
        usage_metadata_behavior="optional_with_local_token_estimate_fallback",
    ),
    # A protocol-test double for model qualification.  It is deliberately not
    # selectable through nmsim.llm.build_llm and cannot contact a network.
    "fake_test_provider": ProviderCapability(
        provider_id="fake_test_provider",
        transport_type="in_process_test_double",
        external_network_expected=False,
        authentication_mode="none",
        supports_batch=True,
        supports_async=False,
        supports_temperature=False,
        supports_seed=True,
        supports_structured_output=True,
        supports_usage_metadata=False,
        supports_provider_response_id=False,
        supports_record_replay=False,
        supports_cache=False,
        tool_access="none",
        deterministic_claim="fixture_defined_deterministic",
        recommended_concurrency=1,
        experimental=True,
        temperature_behavior="not_used",
        usage_metadata_behavior="not_emitted",
        implementation_scope="qualification_test_only",
    ),
}


PRODUCTION_PROVIDER_IDS = frozenset({"mock", "anthropic", "openai"})
QUALIFICATION_TEST_PROVIDER_IDS = frozenset({"fake_test_provider"})


def registered_provider_ids(*, include_test_providers: bool = True) -> tuple[str, ...]:
    """Return reviewed ids in deterministic order."""

    ids = set(PRODUCTION_PROVIDER_IDS)
    if include_test_providers:
        ids.update(QUALIFICATION_TEST_PROVIDER_IDS)
    return tuple(sorted(ids))


def get_provider_capability(provider_id: str) -> ProviderCapability:
    """Return a reviewed capability record or fail closed.

    ``provider_id`` must be the resolved adapter id.  Unknown ids, including
    the unresolved selector ``auto``, are never silently treated as Mock.
    """

    normalised = str(provider_id or "").strip().lower()
    try:
        return _CAPABILITIES[normalised]
    except KeyError as exc:
        raise UnknownProviderCapabilityError(
            "unregistered resolved provider capability: {!r}".format(normalised)
        ) from exc


def _endpoint_identity(endpoint: Optional[str]) -> dict[str, Any]:
    """Return a credential-free endpoint identity.

    The URL itself, hostname, userinfo, query values, and fragment are never
    returned.  Credential-shaped query values are redacted before hashing so a
    capability snapshot identifies the service route rather than a secret.
    """

    text = str(endpoint or "")
    if not text:
        return {
            "configured": False,
            "scheme": None,
            "endpoint_identity_sha256": None,
            "userinfo_redacted": False,
            "sensitive_query_redacted": False,
        }

    try:
        parsed = urlsplit(text)
        host = parsed.netloc.rsplit("@", 1)[-1]
        sensitive_query = False
        query_items: list[tuple[str, str]] = []
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            if _SECRET_QUERY_RE.search(key) or value.strip().lower().startswith("bearer "):
                value = "<redacted>"
                sensitive_query = True
            query_items.append((key, value))
        query_items.sort()
        normalised = urlunsplit(
            (
                parsed.scheme.lower(),
                host.lower(),
                parsed.path,
                urlencode(query_items),
                "",  # fragments do not identify an HTTP API endpoint
            )
        )
        scheme = parsed.scheme.lower() or None
        userinfo_redacted = "@" in parsed.netloc
    except (TypeError, ValueError):
        # Malformed endpoint text is still represented without disclosing it.
        normalised = text
        scheme = None
        userinfo_redacted = "@" in text
        sensitive_query = bool(_SECRET_QUERY_RE.search(text))

    return {
        "configured": True,
        "scheme": scheme,
        "endpoint_identity_sha256": hashlib.sha256(
            normalised.encode("utf-8")
        ).hexdigest(),
        "userinfo_redacted": userinfo_redacted,
        "sensitive_query_redacted": sensitive_query,
    }


def provider_capability_snapshot(
    provider_id: str, *, endpoint: Optional[str] = None
) -> dict[str, Any]:
    """Build a stable, auditable and secret-free capability snapshot."""

    capability = get_provider_capability(provider_id).to_dict()
    snapshot = {
        "capability_schema_version": CAPABILITY_SCHEMA_VERSION,
        "provider": capability,
        "endpoint_identity": _endpoint_identity(endpoint),
    }
    canonical = json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    snapshot["capability_snapshot_sha256"] = hashlib.sha256(canonical).hexdigest()
    return snapshot


__all__ = [
    "CAPABILITY_SCHEMA_VERSION",
    "PRODUCTION_PROVIDER_IDS",
    "QUALIFICATION_TEST_PROVIDER_IDS",
    "ProviderCapability",
    "ProviderCapabilityError",
    "UnknownProviderCapabilityError",
    "get_provider_capability",
    "provider_capability_snapshot",
    "registered_provider_ids",
]
