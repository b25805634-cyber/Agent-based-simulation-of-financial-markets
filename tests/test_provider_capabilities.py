"""Offline tests for the declarative provider capability contract."""
from __future__ import annotations

import json
import inspect
import os
import pathlib
import unittest
from unittest import mock

import nmsim.llm as llm_module
from nmsim.provider_capabilities import (
    CAPABILITY_SCHEMA_VERSION,
    EXPERIMENTAL_PROVIDER_IDS,
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
        # Scientific-fingerprint-covered nmsim.llm contains only the stable
        # production adapters. Experimental adapters live in separate modules
        # and are audited by their own tests without changing that allowlist.
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

    def test_codex_exec_capability_is_local_cli_and_conservative(self):
        from nmsim.codex_exec import (
            CODEX_DECISION_SCHEMA_HASH,
            CODEX_DECISION_SCHEMA_VERSION,
            CODEX_WRAPPER_PROTOCOL_VERSION,
            CODEX_WRAPPER_SOURCE_HASH,
        )

        self.assertIn("codex_exec", EXPERIMENTAL_PROVIDER_IDS)
        self.assertNotIn("codex_exec", PRODUCTION_PROVIDER_IDS)
        capability = get_provider_capability("codex_exec")
        self.assertEqual(capability.transport_type, "local_cli")
        self.assertTrue(capability.external_network_expected)
        self.assertEqual(
            capability.authentication_mode, "chatgpt_managed_codex_cli"
        )
        self.assertFalse(capability.supports_batch)
        self.assertFalse(capability.supports_async)
        self.assertEqual(capability.async_behavior, "wrapper_level_only")
        self.assertFalse(capability.supports_temperature)
        self.assertFalse(capability.supports_seed)
        self.assertTrue(capability.supports_structured_output)
        self.assertTrue(capability.supports_usage_metadata)
        self.assertEqual(capability.deterministic_claim, "none")
        self.assertEqual(capability.recommended_concurrency, 1)
        self.assertTrue(capability.experimental)
        self.assertEqual(
            capability.tool_access,
            "disabled_by_required_runtime_preflight_with_jsonl_defense",
        )
        self.assertEqual(
            capability.capability_probe_basis,
            "runtime_strict_config_and_feature_preflight_plus_exec_help",
        )
        self.assertEqual(
            capability.wrapper_protocol_version, CODEX_WRAPPER_PROTOCOL_VERSION
        )
        self.assertEqual(capability.wrapper_source_hash, CODEX_WRAPPER_SOURCE_HASH)
        self.assertEqual(
            capability.structured_output_schema_version,
            CODEX_DECISION_SCHEMA_VERSION,
        )
        self.assertEqual(
            capability.structured_output_schema_hash, CODEX_DECISION_SCHEMA_HASH
        )
        profile = capability.security_profile
        self.assertIsNotNone(profile)
        assert profile is not None
        self.assertTrue(profile["runtime_strict_preflight_required"])
        self.assertEqual(
            profile["local_real_use_readiness_claim"],
            "not_asserted_by_registry",
        )
        self.assertTrue(profile["provider_transport_network_expected"])
        self.assertFalse(profile["agent_tool_network_enabled"])
        self.assertEqual(
            profile["reasoning_effort_requirement"], "explicit_for_real_use"
        )
        controls = profile["required_effective_controls"]
        self.assertEqual(controls["forced_login_method"], "chatgpt")
        self.assertEqual(controls["approval_policy"], "never")
        self.assertEqual(controls["sandbox_mode"], "read-only")
        self.assertEqual(controls["web_search_mode"], "disabled")
        self.assertFalse(controls["shell_tool_enabled"])
        self.assertFalse(controls["unified_exec_enabled"])
        self.assertFalse(controls["apps_enabled"])
        self.assertFalse(controls["view_image_enabled"])
        self.assertEqual(controls["history_persistence"], "none")
        self.assertTrue(controls["agent_reasoning_events_hidden"])
        self.assertFalse(controls["raw_agent_reasoning_visible"])
        self.assertEqual(controls["personality"], "none")

    def test_codex_snapshot_binds_secret_free_static_adapter_contract(self):
        missing_binary = pathlib.Path(
            "/definitely/missing/nmsim-codex-capability-test"
        )
        with mock.patch.dict(
            os.environ,
            {"NMSIM_CODEX_EXECUTABLE": str(missing_binary)},
            clear=False,
        ):
            snapshot = provider_capability_snapshot("codex_exec")

        adapter = snapshot["provider_adapter_contract"]
        self.assertEqual(adapter["provider"], "codex_exec")
        self.assertIsNone(adapter["requested_model"])
        self.assertIsNone(adapter["reasoning_effort"])
        self.assertTrue(adapter["provider_transport_network_expected"])
        self.assertFalse(adapter["agent_tool_network_enabled"])
        self.assertIn("tool_surface_contract", adapter)
        self.assertRegex(adapter["tool_surface_contract_hash"], r"^[0-9a-f]{64}$")
        self.assertRegex(
            snapshot["capability_snapshot_sha256"], r"^[0-9a-f]{64}$"
        )
        encoded = json.dumps(snapshot, sort_keys=True).lower()
        for forbidden in (
            "api_key",
            "authorization",
            "bearer ",
            "access_token",
            "refresh_token",
            "cookie",
            "private_rationale",
        ):
            self.assertNotIn(forbidden, encoded)

    def test_codex_security_extension_does_not_change_other_provider_snapshots(self):
        expected_hashes = {
            "mock": "71404a68eb4debc98a5aaf9e565bf3023b1f659031ece4d0b789eccdd5b2c139",
            "anthropic": "69021e8d6c46ff10b4a4da5c56be98cef805f82eea9193bbbb6f1e9dcf6acc8f",
            "openai": "3b7969c2fb18d7121684e324ccba2909cab5832ce36d219c4b8744d0aae03553",
            "fake_test_provider": (
                "cf8cc87213d8600722bd7c302fec7a94b7f387474337eadea30e6e231bf806e7"
            ),
        }
        for provider_id, expected_hash in expected_hashes.items():
            with self.subTest(provider=provider_id):
                snapshot = provider_capability_snapshot(provider_id)
                self.assertNotIn("provider_adapter_contract", snapshot)
                self.assertNotIn("security_profile", snapshot["provider"])
                self.assertEqual(
                    snapshot["capability_snapshot_sha256"], expected_hash
                )

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
