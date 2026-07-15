"""Offline tests for the declarative provider capability contract."""
from __future__ import annotations

import json
import inspect
import unittest

import nmsim.llm as llm_module
from nmsim.provider_capabilities import (
    CAPABILITY_SCHEMA_VERSION,
    PRODUCTION_PROVIDER_IDS,
    UnknownProviderCapabilityError,
    get_provider_capability,
    provider_capability_snapshot,
    registered_provider_ids,
)


class ProviderCapabilityTests(unittest.TestCase):
    def test_every_current_provider_implementation_is_registered(self):
        # Discover adapter classes instead of maintaining a second hand-written
        # class list: adding a new fixed-kind provider to nmsim.llm without a
        # capability record must fail this test.
        actual = {
            value.kind
            for value in vars(llm_module).values()
            if inspect.isclass(value)
            and isinstance(getattr(value, "kind", None), str)
            and callable(getattr(value, "complete", None))
        }
        self.assertEqual(actual, set(PRODUCTION_PROVIDER_IDS))
        self.assertTrue(actual.issubset(set(registered_provider_ids())))

    def test_every_capability_has_complete_versioned_shape(self):
        required = {
            "provider_id",
            "transport_type",
            "external_network_expected",
            "authentication_mode",
            "supports_batch",
            "supports_async",
            "supports_temperature",
            "supports_seed",
            "supports_structured_output",
            "supports_usage_metadata",
            "supports_provider_response_id",
            "supports_record_replay",
            "supports_cache",
            "tool_access",
            "deterministic_claim",
            "recommended_concurrency",
            "experimental",
            "capability_schema_version",
        }
        for provider_id in registered_provider_ids():
            with self.subTest(provider=provider_id):
                payload = get_provider_capability(provider_id).to_dict()
                self.assertTrue(required.issubset(payload))
                self.assertEqual(
                    payload["capability_schema_version"], CAPABILITY_SCHEMA_VERSION
                )
                json.dumps(payload, sort_keys=True, allow_nan=False)

    def test_unknown_and_unresolved_provider_ids_fail_closed(self):
        for provider_id in ("new-provider", "auto", "", "codex"):
            with self.subTest(provider=provider_id):
                with self.assertRaises(UnknownProviderCapabilityError):
                    get_provider_capability(provider_id)

    def test_mock_is_local_and_has_only_a_scoped_determinism_claim(self):
        mock = get_provider_capability("mock")
        self.assertFalse(mock.external_network_expected)
        self.assertTrue(mock.supports_seed)
        self.assertEqual(
            mock.deterministic_claim, "deterministic_given_seed_and_call_order"
        )

    def test_real_provider_adapters_never_claim_determinism(self):
        for provider_id in ("anthropic", "openai"):
            with self.subTest(provider=provider_id):
                capability = get_provider_capability(provider_id)
                self.assertTrue(capability.external_network_expected)
                self.assertFalse(capability.supports_seed)
                self.assertEqual(capability.deterministic_claim, "none")

    def test_openai_record_describes_current_bounded_pool_and_optional_usage(self):
        capability = get_provider_capability("openai")
        self.assertEqual(capability.recommended_concurrency, 40)
        self.assertEqual(
            capability.usage_metadata_behavior,
            "optional_with_local_token_estimate_fallback",
        )
        self.assertFalse(capability.supports_provider_response_id)

    def test_anthropic_temperature_support_is_explicitly_conditional(self):
        capability = get_provider_capability("anthropic")
        self.assertTrue(capability.supports_temperature)
        self.assertEqual(
            capability.temperature_behavior, "conditional_by_model_prefix"
        )

    def test_qualification_fake_is_explicitly_test_only(self):
        capability = get_provider_capability("fake_test_provider")
        self.assertFalse(capability.external_network_expected)
        self.assertTrue(capability.experimental)
        self.assertEqual(capability.implementation_scope, "qualification_test_only")

    def test_endpoint_snapshot_is_stable_and_does_not_disclose_secrets(self):
        first = provider_capability_snapshot(
            "openai",
            endpoint=(
                "HTTPS://alice:password@example.invalid/v1?"
                "api_key=super-secret&region=test&Authorization=Bearer+private-token"
            ),
        )
        second = provider_capability_snapshot(
            "openai",
            endpoint=(
                "https://bob:different@example.invalid/v1?"
                "Authorization=Bearer+other-token&region=test&api_key=replaced"
            ),
        )
        encoded = json.dumps(first, sort_keys=True)
        for secret in (
            "alice",
            "password",
            "super-secret",
            "private-token",
            "Bearer+private-token",
            "example.invalid",
        ):
            self.assertNotIn(secret, encoded)
        self.assertEqual(first, second)
        self.assertTrue(first["endpoint_identity"]["userinfo_redacted"])
        self.assertTrue(first["endpoint_identity"]["sensitive_query_redacted"])
        self.assertRegex(
            first["endpoint_identity"]["endpoint_identity_sha256"], r"^[0-9a-f]{64}$"
        )
        self.assertRegex(first["capability_snapshot_sha256"], r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
