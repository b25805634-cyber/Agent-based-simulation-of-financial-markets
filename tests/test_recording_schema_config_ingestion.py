"""Phase 1.1A.2 recording-schema and Config-ingestion contract tests.

All LLM traffic in this module is handled by a local counting double.  Replay
and reparse tests therefore also make an accidental provider call observable.
"""
from __future__ import annotations

from contextlib import redirect_stderr
from dataclasses import asdict
import hashlib
import io
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest import mock

from nmsim.config import (
    CONFIG_FIELD_ALIASES,
    Config,
    ConfigAliasConflictError,
    ConfigSchemaError,
    UnknownConfigFieldError,
)
from nmsim.config_contract import (
    CONFIG_CONTRACT_RECORD_FIELDS,
    build_effective_config_contract,
)
from nmsim.fingerprint import (
    STRICT_COMPATIBILITY_FIELDS,
    scientific_compatibility_metadata,
)
from nmsim.recording import RecordingLLM, ReplayLLM, ReplayMismatchError
from nmsim.recording_schema import (
    CURRENT_RECORDING_SCHEMA_VERSION,
    RECORDING_COMPATIBILITY_MATRIX,
)
from nmsim.reparse_audit import (
    PRIVATE_RESULTS_NAME,
    PUBLIC_RESULTS_NAME,
    SUMMARY_NAME,
    run_reparse_audit,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
PRIVATE_RATIONALE = "PRIVATE_SCHEMA_12_RATIONALE_MUST_NOT_LEAK"
PRIVATE_SYSTEM = "PRIVATE_SCHEMA_12_SYSTEM_MUST_NOT_LEAK"
PRIVATE_USER = "PRIVATE_SCHEMA_12_USER_MUST_NOT_LEAK"


class _CountingProvider:
    kind = "mock"
    model = "schema-1.2-fixture"
    temperature = 0.0
    max_tokens = 64

    def __init__(self) -> None:
        self.calls = 0

    @staticmethod
    def _response() -> str:
        return json.dumps(
            {
                "action": "hold",
                "quantity": 0,
                "limit_price": 100.0,
                "sentiment": 0.0,
                "public_take": "public schema fixture",
                "reasoning": PRIVATE_RATIONALE,
            },
            sort_keys=True,
        )

    def complete(self, system: str, user: str) -> str:
        self.calls += 1
        return self._response()

    def complete_batch(self, prompts) -> list[str]:
        prompts = list(prompts)
        self.calls += len(prompts)
        return [self._response() for _ in prompts]


def _write_record(path: Path, record: dict) -> bytes:
    payload = (
        json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")
    path.write_bytes(payload)
    return payload


class RecordingSchema12ContractTests(unittest.TestCase):
    model_config = {
        "provider": "mock",
        "model": "schema-1.2-fixture",
        "temperature": 0.0,
        "max_tokens": 64,
        "cache_enabled": False,
    }
    prompt = (PRIVATE_SYSTEM, PRIVATE_USER)
    context = [{"agent_id": "agent-a", "persona_id": "persona-a"}]

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source_dir = self.root / "recording"
        self.source_dir.mkdir()
        self.provider = _CountingProvider()
        self.cfg = Config(
            provider="mock",
            model="schema-1.2-fixture",
            temperature=0.0,
            max_tokens=64,
            cache_enabled=False,
            out_dir=str(self.source_dir),
        )
        self.compatibility = {
            **scientific_compatibility_metadata(REPO_ROOT),
            **build_effective_config_contract(
                self.cfg,
                base_dir=self.root,
                execution_context={
                    "run_id": "schema-source",
                    "worker_count": 1,
                    "batching": {"strategy": "one-batch-per-round"},
                },
            ),
        }
        recorder = RecordingLLM(
            self.provider,
            self.source_dir,
            model_config=self.model_config,
            compatibility_metadata=self.compatibility,
        )
        recorder.set_batch_context(1, self.context)
        self.response = recorder.complete_batch([self.prompt])[0]
        self.assertEqual(self.provider.calls, 1)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @property
    def records_path(self) -> Path:
        return self.source_dir / "llm_records.jsonl"

    def _record(self) -> dict:
        return json.loads(self.records_path.read_text(encoding="utf-8"))

    def _replay(self, source: Path | None = None) -> ReplayLLM:
        return ReplayLLM(
            self.source_dir if source is None else source,
            model_config=self.model_config,
            compatibility_metadata=self.compatibility,
        )

    def _consume(self, replay: ReplayLLM) -> str:
        replay.set_batch_context(1, self.context)
        response = replay.complete_batch([self.prompt])[0]
        replay.assert_exhausted()
        return response

    def _variant_directory(self, name: str, record: dict) -> Path:
        target = self.root / name
        target.mkdir()
        _write_record(target / "llm_records.jsonl", record)
        return target

    def test_new_record_declares_only_schema_1_2_and_strict_replays_offline(self):
        record = self._record()
        self.assertEqual(CURRENT_RECORDING_SCHEMA_VERSION, "1.2")
        self.assertEqual(record["schema_version"], "1.2")
        self.assertEqual(record["recording_schema_version"], "1.2")
        for field in CONFIG_CONTRACT_RECORD_FIELDS:
            self.assertIn(field, record)

        before = self.provider.calls
        replay = self._replay()
        self.assertNotIn("inner", vars(replay))
        self.assertEqual(self._consume(replay), self.response)
        self.assertEqual(self.provider.calls, before)

    def test_caller_cannot_make_a_new_recorder_emit_transitional_1_1(self):
        target = self.root / "forced-old-version"
        target.mkdir()
        requested = dict(self.compatibility)
        requested["recording_schema_version"] = "1.1"
        provider = _CountingProvider()
        recorder = RecordingLLM(
            provider,
            target,
            model_config=self.model_config,
            compatibility_metadata=requested,
        )
        recorder.set_batch_context(1, self.context)
        recorder.complete_batch([self.prompt])

        record = json.loads(
            (target / "llm_records.jsonl").read_text(encoding="utf-8")
        )
        self.assertEqual(record["schema_version"], "1.2")
        self.assertEqual(record["recording_schema_version"], "1.2")

    def test_recording_append_mode_is_rejected_before_provider_call(self):
        before = self.provider.calls
        with self.assertRaisesRegex(ValueError, "allow_append is not supported"):
            RecordingLLM(
                self.provider,
                self.source_dir,
                model_config=self.model_config,
                compatibility_metadata=self.compatibility,
                allow_append=True,
            )
        self.assertEqual(self.provider.calls, before)

    def test_empty_recording_is_rejected_during_constructor_preflight(self):
        target = self.root / "empty-recording"
        target.mkdir()
        (target / "llm_records.jsonl").write_bytes(b"")
        with self.assertRaisesRegex(ReplayMismatchError, "empty_recording"):
            self._replay(target)

    def test_schema_1_2_requires_each_runtime_config_identity(self):
        original = self._record()
        for field in (
            "scientific_config_hash",
            "model_request_config_hash",
            "execution_config_hash",
        ):
            with self.subTest(field=field):
                changed = dict(original)
                changed.pop(field)
                target = self._variant_directory("missing-{}".format(field), changed)
                before = self.provider.calls
                with self.assertRaises(ReplayMismatchError) as raised:
                    self._replay(target)
                message = str(raised.exception)
                self.assertIn("recording_schema_1_2_missing_required_fields", message)
                self.assertIn(field, message)
                self.assertNotIn(PRIVATE_RATIONALE, message)
                self.assertNotIn(PRIVATE_SYSTEM, message)
                self.assertNotIn(PRIVATE_USER, message)
                self.assertEqual(self.provider.calls, before)

    def test_compatibility_matrix_is_explicit_and_fail_closed(self):
        rows = {
            (row.schema_version, row.runtime_config_contract): row
            for row in RECORDING_COMPATIBILITY_MATRIX
        }
        self.assertEqual(
            rows[("1.0", False)].reason,
            "legacy_recording_missing_replay_contract",
        )
        self.assertEqual(
            rows[("1.1", False)].reason,
            "recording_missing_runtime_config_contract",
        )
        self.assertEqual(
            rows[("1.1", True)].reason,
            "transitional_schema_1_1_with_config_contract",
        )
        self.assertFalse(rows[("1.0", False)].strict_replay)
        self.assertFalse(rows[("1.1", False)].strict_replay)
        self.assertFalse(rows[("1.1", True)].strict_replay)
        self.assertTrue(rows[("1.2", True)].strict_replay)
        self.assertTrue(all(row.reparse_audit for row in rows.values()))

    def test_historical_schema_variants_reject_strict_but_allow_reparse(self):
        original = self._record()
        variants: list[tuple[str, dict, str, str]] = []

        legacy = dict(original)
        legacy["schema_version"] = "1.0"
        for field in (*STRICT_COMPATIBILITY_FIELDS, *CONFIG_CONTRACT_RECORD_FIELDS):
            legacy.pop(field, None)
        variants.append(
            (
                "legacy-1.0",
                legacy,
                "legacy_recording_missing_replay_contract",
                "legacy_schema_1_0",
            )
        )

        pre_contract = dict(original)
        pre_contract["schema_version"] = "1.1"
        pre_contract["recording_schema_version"] = "1.1"
        for field in CONFIG_CONTRACT_RECORD_FIELDS:
            pre_contract.pop(field, None)
        variants.append(
            (
                "pre-contract-1.1",
                pre_contract,
                "recording_missing_runtime_config_contract",
                "pre_config_contract_schema_1_1",
            )
        )

        transitional = dict(original)
        transitional["schema_version"] = "1.1"
        transitional["recording_schema_version"] = "1.1"
        variants.append(
            (
                "transitional-1.1",
                transitional,
                "transitional_schema_1_1_with_config_contract",
                "transitional_schema_1_1_with_config_contract",
            )
        )

        for name, record, rejection, variant in variants:
            with self.subTest(schema=name):
                target = self._variant_directory(name, record)
                records_path = target / "llm_records.jsonl"
                before_bytes = records_path.read_bytes()
                before_calls = self.provider.calls
                with self.assertRaisesRegex(ReplayMismatchError, rejection):
                    self._replay(target)

                audit_dir = run_reparse_audit(
                    target, self.root / "audits-{}".format(name)
                )
                self.assertEqual(records_path.read_bytes(), before_bytes)
                self.assertEqual(self.provider.calls, before_calls)
                summary = json.loads(
                    (audit_dir / SUMMARY_NAME).read_text(encoding="utf-8")
                )
                schema_report = summary["recording_schema_compatibility"]
                self.assertEqual(schema_report["variant"], variant)
                self.assertFalse(schema_report["strict_replay"])
                self.assertTrue(schema_report["reparse_audit"])
                self.assertEqual(summary["provider_calls"], 0)
                self.assertFalse(summary["network_access"])
                public = "\n".join(
                    (
                        (audit_dir / PUBLIC_RESULTS_NAME).read_text(encoding="utf-8"),
                        (audit_dir / SUMMARY_NAME).read_text(encoding="utf-8"),
                    )
                )
                self.assertNotIn(PRIVATE_RATIONALE, public)
                self.assertNotIn(PRIVATE_SYSTEM, public)
                self.assertNotIn(PRIVATE_USER, public)
                self.assertEqual(
                    (audit_dir / PRIVATE_RESULTS_NAME).stat().st_mode & 0o777,
                    0o600,
                )

    def test_reparse_never_reports_malformed_1_2_as_strict_eligible(self):
        malformed = self._record()
        malformed["request"] = dict(malformed["request"])
        malformed["request"]["prompt_hash"] = "0" * 64
        target = self._variant_directory("malformed-1.2", malformed)
        before = (target / "llm_records.jsonl").read_bytes()

        with self.assertRaisesRegex(ReplayMismatchError, "prompt_hash"):
            self._replay(target)
        audit_dir = run_reparse_audit(target, self.root / "malformed-audits")
        report = json.loads(
            (audit_dir / SUMMARY_NAME).read_text(encoding="utf-8")
        )["recording_schema_compatibility"]
        self.assertEqual(report["variant"], "invalid_schema_1_2")
        self.assertFalse(report["strict_replay"])
        self.assertIn("request.prompt_hash", report["validation_error_fields"])
        self.assertEqual((target / "llm_records.jsonl").read_bytes(), before)


class StrictConfigIngestionTests(unittest.TestCase):
    def test_unknown_typo_fails_with_safe_suggestion_instead_of_using_default(self):
        with self.assertRaises(UnknownConfigFieldError) as raised:
            Config.from_dict({"maintenence_margin": 0.99})
        message = str(raised.exception)
        self.assertIn("maintenence_margin", message)
        self.assertIn("maintenance_margin", message)
        self.assertNotEqual(Config().maintenance_margin, 0.99)

    def test_multiple_unknown_fields_are_reported_in_stable_sorted_order(self):
        values = {"z_future_field": 1, "a_future_field": 2}
        messages = []
        for mapping in (values, dict(reversed(list(values.items())))):
            with self.assertRaises(UnknownConfigFieldError) as raised:
                Config.from_dict(mapping)
            messages.append(str(raised.exception))
        self.assertEqual(messages[0], messages[1])
        self.assertLess(
            messages[0].index("a_future_field"),
            messages[0].index("z_future_field"),
        )

    def test_central_alias_mechanism_normalises_only_explicitly_registered_names(self):
        self.assertEqual(CONFIG_FIELD_ALIASES, {})
        with mock.patch.dict(
            CONFIG_FIELD_ALIASES, {"rounds": "n_rounds"}, clear=True
        ):
            cfg = Config.from_dict({"rounds": 3})
        self.assertEqual(cfg.n_rounds, 3)
        with self.assertRaises(UnknownConfigFieldError):
            Config.from_dict({"rounds": 3})

    def test_alias_and_canonical_are_always_ambiguous_even_if_values_match(self):
        with mock.patch.dict(
            CONFIG_FIELD_ALIASES, {"rounds": "n_rounds"}, clear=True
        ):
            for canonical_value in (8, 4):
                with self.subTest(canonical_value=canonical_value):
                    with self.assertRaises(ConfigAliasConflictError) as raised:
                        Config.from_dict(
                            {"rounds": 4, "n_rounds": canonical_value}
                        )
                    message = str(raised.exception)
                    self.assertIn("rounds", message)
                    self.assertIn("n_rounds", message)
                    self.assertNotIn(str(canonical_value), message)

    def test_non_strict_ingestion_is_not_an_implicit_legacy_migration(self):
        with self.assertRaisesRegex(
            ConfigSchemaError,
            "strict=False Config ingestion is not supported",
        ):
            Config.from_dict({"future_field": 1}, strict=False)

    def test_unknown_field_error_never_contains_sensitive_values(self):
        api_secret = "API_KEY_VALUE_MUST_NOT_LEAK"
        authorization = "Authorization: Bearer TOKEN_MUST_NOT_LEAK"
        rationale = "PRIVATE_RATIONALE_VALUE_MUST_NOT_LEAK"
        with self.assertRaises(UnknownConfigFieldError) as raised:
            Config.from_dict(
                {
                    "future_auth_config": {
                        "api_key": api_secret,
                        "header": authorization,
                        "reasoning": rationale,
                    }
                }
            )
        message = str(raised.exception)
        self.assertIn("future_auth_config", message)
        self.assertNotIn(api_secret, message)
        self.assertNotIn(authorization, message)
        self.assertNotIn(rationale, message)

    def test_sensitive_or_unbounded_unknown_key_text_is_redacted(self):
        secret_key = "Authorization: Bearer KEY_MUST_NOT_LEAK"
        private_key = "PRIVATE_RATIONALE_MUST_NOT_LEAK"
        long_key = "x" * 200
        with self.assertRaises(UnknownConfigFieldError) as raised:
            Config.from_dict({secret_key: 1, private_key: 2, long_key: 3})
        message = str(raised.exception)
        self.assertNotIn(secret_key, message)
        self.assertNotIn(private_key, message)
        self.assertNotIn(long_key, message)
        self.assertIn("redacted-config-key", message)
        self.assertIn("sha256", message)

    def test_unknown_input_stops_before_provider_network_run_or_outputs(self):
        provider = _CountingProvider()
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "should-not-exist"
            with mock.patch(
                "nmsim.run.build_llm",
                side_effect=AssertionError("provider construction attempted"),
            ) as build_llm, mock.patch(
                "nmsim.run.run_sim",
                side_effect=AssertionError("simulation entered"),
            ) as run_sim, mock.patch.object(
                socket,
                "create_connection",
                side_effect=AssertionError("network attempted"),
            ) as network:
                with self.assertRaises(UnknownConfigFieldError):
                    cfg = Config.from_dict(
                        {
                            "provider": "mock",
                            "out_dir": str(output),
                            "maintenence_margin": 0.10,
                        }
                    )
                    # This line is intentionally unreachable: configuration is
                    # validated before a formal run lifecycle can be created.
                    provider.complete("system", "user")
                    __import__("nmsim.run", fromlist=["run"]).run(cfg)

            self.assertEqual(provider.calls, 0)
            build_llm.assert_not_called()
            run_sim.assert_not_called()
            network.assert_not_called()
            self.assertFalse(output.exists())

    def test_managed_cli_rejects_unknown_flag_before_provider_or_simulation(self):
        from nmsim import run as run_module

        stderr = io.StringIO()
        with mock.patch.object(
            sys,
            "argv",
            ["nmsim.run", "--maintenence-margin", "0.10"],
        ), mock.patch.object(
            run_module,
            "build_llm",
            side_effect=AssertionError("provider construction attempted"),
        ) as build_llm, mock.patch.object(
            run_module,
            "run_sim",
            side_effect=AssertionError("simulation entered"),
        ) as run_sim, redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                run_module.main()
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("unrecognized arguments", stderr.getvalue())
        build_llm.assert_not_called()
        run_sim.assert_not_called()

    def test_formal_sources_do_not_request_permissive_config_ingestion(self):
        formal_sources = (
            REPO_ROOT / "nmsim" / "run.py",
            REPO_ROOT / "nmsim" / "recording.py",
            REPO_ROOT / "nmsim" / "provenance.py",
            REPO_ROOT / "experiments" / "run_seed.py",
        )
        for path in formal_sources:
            with self.subTest(path=path.relative_to(REPO_ROOT).as_posix()):
                self.assertNotIn("strict=False", path.read_text(encoding="utf-8"))

    def test_prompt_persona_and_wave1_scientific_identities_are_pinned(self):
        metadata = scientific_compatibility_metadata(REPO_ROOT)
        self.assertEqual(
            metadata["prompt_source_hash"],
            "db9c26c22d35223ea7ee768d622c608f9ca27b4b81b58615720704c39e906171",
        )
        self.assertEqual(
            metadata["persona_source_hash"],
            "6e6fc8d48dbe31106b14852094a325e38958b35eb3a0552c712db4f5807cba06",
        )
        self.assertEqual(
            metadata["simulation_core_source_hash"],
            "7341806f8276fcff4d7d4eeaa790393f06e0987044ed0bcb5fb34f2b06e4f659",
        )
        self.assertEqual(
            metadata["scientific_component_fingerprint"],
            "d875f50b50f206dafd768c5e66211c3815a0e3fc05c72edff216740e52a22109",
        )
        defaults = Config()
        self.assertEqual(defaults.n_rounds, 24)
        self.assertEqual(defaults.kappa, 0.12)
        self.assertEqual(defaults.maintenance_margin, 0.25)
        default_payload = json.dumps(
            asdict(defaults),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.assertEqual(
            hashlib.sha256(default_payload).hexdigest(),
            "5d271f9a673f522b7f7d2f937ba003374e6e45c2bdaa707f6dc7f8824935ef5d",
        )


if __name__ == "__main__":
    unittest.main()
