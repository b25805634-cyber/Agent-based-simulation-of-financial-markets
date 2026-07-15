"""Effective-configuration contract tests for strict offline replay.

The fixtures in this module never construct a real provider.  A small local
double is used to create one recording; replay itself has no inner provider or
network fallback.
"""
from __future__ import annotations

import importlib
import json
from contextlib import redirect_stdout
from dataclasses import asdict, dataclass, replace
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from nmsim.config import Config
from nmsim.config_contract import (
    CONFIG_CONTRACT_RECORD_FIELDS,
    UnclassifiedConfigFieldError,
    build_effective_config_contract,
)
from nmsim.fingerprint import (
    STRICT_COMPATIBILITY_FIELDS,
    scientific_compatibility_metadata,
)
from nmsim.recording import RecordingLLM, ReplayLLM, ReplayMismatchError
from nmsim.reparse_audit import (
    PRIVATE_RESULTS_NAME,
    PUBLIC_RESULTS_NAME,
    SUMMARY_NAME,
    run_reparse_audit,
)


run_module = importlib.import_module("nmsim.run")
REPO_ROOT = Path(__file__).resolve().parents[1]
PRIVATE_RATIONALE = "PRIVATE_CONFIG_CONTRACT_RATIONALE_MUST_NOT_LEAK"
PRIVATE_SYSTEM = "PRIVATE_CONFIG_CONTRACT_SYSTEM_PROMPT"
PRIVATE_USER = "PRIVATE_CONFIG_CONTRACT_USER_PROMPT"


class _CountingProvider:
    kind = "mock"
    model = "config-contract-fixture"
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
                "public_take": "public config-contract fixture",
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


def _read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class EffectiveConfigContractTests(unittest.TestCase):
    model_config = {
        "provider": "mock",
        "model": "config-contract-fixture",
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
            model="config-contract-fixture",
            temperature=0.0,
            max_tokens=64,
            cache_enabled=False,
            out_dir=str(self.source_dir),
        )
        self.source_execution = {
            "run_id": "source-run",
            "worker_count": 1,
            "batching": {"strategy": "one-batch-per-round"},
        }
        self.compatibility = self._compatibility(self.cfg, self.source_execution)
        recorder = RecordingLLM(
            self.provider,
            self.source_dir,
            model_config=self.model_config,
            compatibility_metadata=self.compatibility,
        )
        recorder.set_batch_context(1, self.context)
        self.recorded_response = recorder.complete_batch([self.prompt])[0]
        self.assertEqual(self.provider.calls, 1)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _contract(self, cfg: Config, execution=None) -> dict:
        return build_effective_config_contract(
            cfg,
            base_dir=self.root,
            execution_context=self.source_execution if execution is None else execution,
        )

    def _compatibility(self, cfg: Config, execution=None) -> dict:
        return {
            **scientific_compatibility_metadata(REPO_ROOT),
            **self._contract(cfg, execution),
        }

    def _replay(self, compatibility: dict) -> ReplayLLM:
        return ReplayLLM(
            self.source_dir,
            model_config=self.model_config,
            compatibility_metadata=compatibility,
        )

    def _consume(self, replay: ReplayLLM) -> str:
        replay.set_batch_context(1, self.context)
        response = replay.complete_batch([self.prompt])[0]
        replay.assert_exhausted()
        return response

    def _assert_scientific_mismatches(self, **changes) -> None:
        changed = replace(self.cfg, **changes)
        compatibility = self._compatibility(changed)
        before = self.provider.calls
        with self.assertRaises(ReplayMismatchError) as raised:
            self._replay(compatibility)
        message = str(raised.exception)
        self.assertIn("category=scientific", message)
        for field in changes:
            self.assertIn(field, message)
        self.assertIn("expected_scientific_config_hash=sha256:", message)
        self.assertIn("actual_scientific_config_hash=sha256:", message)
        self.assertNotIn(PRIVATE_SYSTEM, message)
        self.assertNotIn(PRIVATE_USER, message)
        self.assertNotIn(PRIVATE_RATIONALE, message)
        self.assertEqual(self.provider.calls, before)

    def _make_legacy_schema_1_recording(self) -> bytes:
        path = self.source_dir / "llm_records.jsonl"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["schema_version"] = "1.0"
        for field in (*STRICT_COMPATIBILITY_FIELDS, *CONFIG_CONTRACT_RECORD_FIELDS):
            record.pop(field, None)
        payload = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode()
        path.write_bytes(payload)
        return payload

    def test_same_effective_config_replays_without_provider(self):
        record = json.loads(
            (self.source_dir / "llm_records.jsonl").read_text(encoding="utf-8")
        )
        for field in CONFIG_CONTRACT_RECORD_FIELDS:
            self.assertEqual(record[field], self.compatibility[field])
        before = self.provider.calls
        replay = self._replay(self.compatibility)
        self.assertNotIn("inner", vars(replay))
        self.assertEqual(self._consume(replay), self.recorded_response)
        self.assertEqual(self.provider.calls, before)

    def test_seed_mismatch_is_rejected_during_preflight(self):
        self._assert_scientific_mismatches(seed=self.cfg.seed + 1)

    def test_kappa_mismatch_is_rejected_during_preflight(self):
        self._assert_scientific_mismatches(kappa=self.cfg.kappa + 0.01)

    def test_population_mismatch_is_rejected_during_preflight(self):
        self._assert_scientific_mismatches(population={"value_institution": 1})

    def test_news_round_mismatch_is_rejected_during_preflight(self):
        self._assert_scientific_mismatches(news_round=self.cfg.news_round - 1)

    def test_social_mismatches_are_rejected_during_preflight(self):
        self._assert_scientific_mismatches(
            social_enabled=not self.cfg.social_enabled,
            social_weight=self.cfg.social_weight + 0.25,
            topology="fully_connected",
        )

    def test_leverage_mismatches_are_rejected_during_preflight(self):
        self._assert_scientific_mismatches(
            leverage_enabled=not self.cfg.leverage_enabled,
            leverage_ratio=self.cfg.leverage_ratio + 0.25,
            leverage_spread=self.cfg.leverage_spread + 0.1,
        )

    def test_maintenance_margin_mismatch_is_rejected_during_preflight(self):
        self._assert_scientific_mismatches(
            maintenance_margin=self.cfg.maintenance_margin + 0.01,
        )

    def test_other_scientific_schedule_and_population_fields_are_strict(self):
        for field, value in (
            ("n_rounds", self.cfg.n_rounds + 1),
            ("n_noise_agents", self.cfg.n_noise_agents + 1),
            ("leverage_fraction", self.cfg.leverage_fraction + 0.1),
        ):
            with self.subTest(field=field):
                self._assert_scientific_mismatches(**{field: value})

    def test_execution_out_run_id_and_workers_may_differ_and_are_reported(self):
        changed = replace(self.cfg, out_dir=str(self.root / "different-output"))
        execution = {
            "run_id": "different-run",
            "worker_count": 7,
            "batching": {"strategy": "one-batch-per-round"},
        }
        current = self._compatibility(changed, execution)
        self.assertEqual(
            self.compatibility["scientific_config_hash"],
            current["scientific_config_hash"],
        )
        self.assertNotEqual(
            self.compatibility["execution_config_hash"],
            current["execution_config_hash"],
        )

        replay = self._replay(current)
        self.assertEqual(self._consume(replay), self.recorded_response)
        self.assertIn("config_fields.out_dir", replay.execution_config_differences)
        self.assertIn("runtime.run_id", replay.execution_config_differences)
        self.assertIn("runtime.worker_count", replay.execution_config_differences)

    def test_cache_policy_is_a_strict_model_request_field(self):
        changed = replace(self.cfg, cache_enabled=True)
        with self.assertRaises(ReplayMismatchError) as raised:
            self._replay(self._compatibility(changed))
        self.assertIn("category=model_request", str(raised.exception))
        self.assertIn("cache_enabled", str(raised.exception))
        self.assertEqual(self.provider.calls, 1)

    def test_new_config_dataclass_field_fails_closed(self):
        @dataclass
        class ExtendedConfig(Config):
            future_scientific_knob: int = 1

        with self.assertRaisesRegex(
            UnclassifiedConfigFieldError, "future_scientific_knob"
        ):
            build_effective_config_contract(ExtendedConfig(), base_dir=self.root)

    def test_top_level_config_and_ordinary_mapping_order_do_not_change_hashes(self):
        values = asdict(self.cfg)
        reversed_values = dict(reversed(list(values.items())))
        left = Config.from_dict(values)
        right = Config.from_dict(reversed_values)
        left_contract = self._contract(
            left, {"mapping": {"z": 3, "a": 1, "m": 2}}
        )
        right_contract = self._contract(
            right, {"mapping": {"m": 2, "z": 3, "a": 1}}
        )
        self.assertEqual(
            left_contract["full_effective_config_hash"],
            right_contract["full_effective_config_hash"],
        )
        self.assertEqual(
            left_contract["execution_config_hash"],
            right_contract["execution_config_hash"],
        )

    def test_population_insertion_order_changes_effective_cast_and_science_hash(self):
        first = replace(
            self.cfg,
            max_llm_agents=2,
            population={"value_institution": 1, "retail_crowd": 1},
        )
        second = replace(
            self.cfg,
            max_llm_agents=2,
            population={"retail_crowd": 1, "value_institution": 1},
        )
        first_contract = self._contract(first)
        second_contract = self._contract(second)
        self.assertNotEqual(
            first_contract["scientific_config_hash"],
            second_contract["scientific_config_hash"],
        )
        self.assertNotEqual(
            first_contract["scientific_config_summary"]["population"]["effective_cast"],
            second_contract["scientific_config_summary"]["population"]["effective_cast"],
        )

    def test_cli_explicit_defaults_and_omitted_defaults_have_same_effective_hash(self):
        parser = run_module.build_argparser()
        omitted = run_module.cfg_from_args(parser.parse_args([]))
        explicit = run_module.cfg_from_args(
            parser.parse_args(
                [
                    "--provider", "auto",
                    "--temperature", "0",
                    "--max-tokens", "1024",
                    "--rounds", "24",
                    "--news-round", "12",
                    "--llm-agents", "6",
                    "--topology", "scale_free",
                    "--social-mode", "network",
                    "--social-weight", "1.0",
                    "--seed-fraction", "0.34",
                    "--seed", "7",
                    "--out", "outputs",
                ]
            )
        )
        self.assertEqual(
            self._contract(omitted)["full_effective_config_hash"],
            self._contract(explicit)["full_effective_config_hash"],
        )

    def test_absolute_output_path_changes_only_execution_identity(self):
        left = replace(self.cfg, out_dir=str(self.root / "out-a"))
        right = replace(self.cfg, out_dir=str(self.root / "out-b"))
        left_contract = self._contract(left)
        right_contract = self._contract(right)
        self.assertEqual(
            left_contract["scientific_config_hash"],
            right_contract["scientific_config_hash"],
        )
        self.assertNotEqual(
            left_contract["execution_config_hash"],
            right_contract["execution_config_hash"],
        )

    def test_reference_file_identity_uses_content_not_absolute_path(self):
        first_path = self.root / "inputs-a" / "reference.csv"
        second_path = self.root / "inputs-b" / "renamed.csv"
        first_path.parent.mkdir()
        second_path.parent.mkdir()
        content = b"timestamp,price\n0,100\n"
        first_path.write_bytes(content)
        second_path.write_bytes(content)
        first = self._contract(replace(self.cfg, reference_path=str(first_path)))
        second = self._contract(replace(self.cfg, reference_path=str(second_path)))
        self.assertEqual(
            first["scientific_config_hash"], second["scientific_config_hash"]
        )

        second_path.write_bytes(content + b"1,99\n")
        changed = self._contract(replace(self.cfg, reference_path=str(second_path)))
        self.assertNotEqual(
            first["scientific_config_hash"], changed["scientific_config_hash"]
        )

    def test_config_summaries_and_recording_never_persist_credentials(self):
        api_secret = "API_KEY_SECRET_MUST_NOT_LEAK"
        endpoint_secret = "ENDPOINT_PASSWORD_MUST_NOT_LEAK"
        cfg = replace(
            self.cfg,
            openai_api_key=api_secret,
            openai_base_url=(
                "https://user:{}@example.invalid/v1".format(endpoint_secret)
            ),
        )
        compatibility = self._compatibility(cfg)
        serialized = json.dumps(compatibility, ensure_ascii=False, sort_keys=True)
        self.assertNotIn(api_secret, serialized)
        self.assertNotIn(endpoint_secret, serialized)
        self.assertEqual(
            compatibility["execution_config_summary"]["config_fields"][
                "openai_api_key"
            ]["value"],
            "<redacted>",
        )

        target = self.root / "secret-record"
        target.mkdir()
        provider = _CountingProvider()
        recorder = RecordingLLM(
            provider,
            target,
            model_config=self.model_config,
            compatibility_metadata=compatibility,
        )
        recorder.set_batch_context(1, self.context)
        recorder.complete_batch([self.prompt])
        recorded_text = (target / "llm_records.jsonl").read_text(encoding="utf-8")
        self.assertNotIn(api_secret, recorded_text)
        self.assertNotIn(endpoint_secret, recorded_text)

    def test_schema_1_0_strict_replay_has_tailored_fail_closed_error(self):
        self._make_legacy_schema_1_recording()
        before = self.provider.calls
        with self.assertRaises(ReplayMismatchError) as raised:
            self._replay(self.compatibility)
        message = str(raised.exception)
        self.assertIn("legacy_recording_missing_replay_contract", message)
        self.assertIn("Reparse Audit", message)
        self.assertIn("missing_fields", message)
        self.assertNotIn(PRIVATE_RATIONALE, message)
        self.assertNotIn(PRIVATE_SYSTEM, message)
        self.assertNotIn(PRIVATE_USER, message)
        self.assertEqual(self.provider.calls, before)

    def test_schema_1_0_remains_reparse_auditable_and_public_output_is_private_safe(self):
        original = self._make_legacy_schema_1_recording()
        records_path = self.source_dir / "llm_records.jsonl"
        before_calls = self.provider.calls
        audit_dir = run_reparse_audit(self.source_dir, self.root / "audits")

        self.assertEqual(records_path.read_bytes(), original)
        self.assertEqual(self.provider.calls, before_calls)
        summary = json.loads(
            (audit_dir / SUMMARY_NAME).read_text(encoding="utf-8")
        )
        self.assertEqual(summary["provider_calls"], 0)
        self.assertFalse(summary["network_access"])
        self.assertFalse(summary["simulation_continued"])
        self.assertFalse(summary["price_path_generated"])
        self.assertEqual(summary["total_response_count"], 1)

        public_text = "\n".join(
            (
                (audit_dir / PUBLIC_RESULTS_NAME).read_text(encoding="utf-8"),
                (audit_dir / SUMMARY_NAME).read_text(encoding="utf-8"),
            )
        )
        self.assertNotIn(PRIVATE_RATIONALE, public_text)
        self.assertNotIn(PRIVATE_SYSTEM, public_text)
        self.assertNotIn(PRIVATE_USER, public_text)
        private_path = audit_dir / PRIVATE_RESULTS_NAME
        self.assertEqual(private_path.stat().st_mode & 0o777, 0o600)
        self.assertIn(PRIVATE_RATIONALE, private_path.read_text(encoding="utf-8"))


class ManagedConfigPreflightTests(unittest.TestCase):
    def _cfg(self, out_dir: Path) -> Config:
        return Config(
            provider="mock",
            seed=4242,
            n_rounds=2,
            news_round=1,
            n_llm_agents=1,
            n_noise_agents=0,
            cache_enabled=False,
            out_dir=str(out_dir),
        )

    def _run(self, cfg: Config, **kwargs):
        with mock.patch.object(run_module, "_plot", return_value=None), redirect_stdout(
            mock.MagicMock()
        ):
            return run_module.run(cfg, **kwargs)

    def test_execution_out_run_id_and_workers_are_allowed_and_manifested(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            recorded = self._run(self._cfg(root / "recorded"), worker_count=1)
            replay_cfg = self._cfg(root / "replayed")
            with mock.patch.object(
                run_module,
                "build_llm",
                side_effect=AssertionError("provider construction attempted"),
            ):
                replayed = self._run(
                    replay_cfg,
                    replay_from=recorded.run_dir,
                    run_id="different-replay-run-id",
                    worker_count=7,
                )

            self.assertEqual(recorded.history, replayed.history)
            manifest = json.loads(
                (Path(replayed.run_dir) / "run_manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            evidence = manifest["replay_compatibility"]
            self.assertTrue(evidence["strict_compatibility_passed"])
            self.assertTrue(evidence["execution_config_differences_detected"])
            self.assertTrue(evidence["execution_config_differences_allowed"])
            differences = evidence["execution_config_differences"]
            self.assertIn("config_fields.out_dir", differences)
            self.assertIn("runtime.run_id", differences)
            self.assertIn("runtime.worker_count", differences)
            self.assertEqual(manifest["execution"]["worker_count"], 7)
            self.assertEqual(manifest["run_id"], "different-replay-run-id")
            self.assertEqual(manifest["llm"]["runtime"]["provider_calls"], 0)
            self.assertFalse(manifest["llm"]["runtime"]["network_access"])

    def test_scientific_mismatch_fails_before_round_provider_or_canonical_outputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary) / "managed"
            recorded = self._run(self._cfg(out))
            before = set((out / "runs").iterdir())
            changed = replace(self._cfg(out), kappa=0.99)

            with mock.patch.object(
                run_module,
                "build_llm",
                side_effect=AssertionError("provider construction attempted"),
            ), mock.patch.object(
                run_module,
                "run_sim",
                side_effect=AssertionError("simulation entered before preflight"),
            ):
                with self.assertRaises(ReplayMismatchError) as raised:
                    self._run(changed, replay_from=recorded.run_dir)

            self.assertIn("category=scientific", str(raised.exception))
            self.assertIn("kappa", str(raised.exception))
            created = set((out / "runs").iterdir()) - before
            self.assertEqual(len(created), 1)
            failed_dir = created.pop()
            manifest = json.loads(
                (failed_dir / "run_manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual(manifest["samples"]["completed"], 0)
            self.assertEqual(manifest["samples"]["honest_n"], 0)
            self.assertEqual(manifest["llm"]["runtime"]["provider_calls"], 0)
            self.assertFalse(manifest["llm"]["runtime"]["network_access"])

            event_types = [
                event["type"] for event in _read_jsonl(failed_dir / "events.jsonl")
            ]
            self.assertEqual(event_types, ["RunStarted", "RunFailed"])
            self.assertNotIn("RoundStarted", event_types)
            self.assertNotIn("LLMRequestRecorded", event_types)
            for filename in (
                "price_path.csv",
                "reasoning_traces.csv",
                "propagation.csv",
                "stylized_facts.json",
                "config.json",
                "sim_overview.png",
            ):
                self.assertFalse((failed_dir / filename).exists())


if __name__ == "__main__":
    unittest.main()
