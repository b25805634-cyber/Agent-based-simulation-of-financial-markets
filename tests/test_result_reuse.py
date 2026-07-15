from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from nmsim.config import Config
from nmsim.result_reuse import (
    ARTIFACT_HASH_MISMATCH,
    ARTIFACT_MISSING,
    COMPLETION_INCOMPLETE,
    INPUT_IDENTITY_MISMATCH,
    LEGACY_FLAT_RESULT_UNVERIFIED,
    MANAGED_RUN_INCOMPLETE,
    MODEL_MISMATCH,
    MODEL_REQUEST_CONFIG_MISMATCH,
    OUTPUTS_INCOMPLETE,
    PERSONA_MISMATCH,
    POPULATION_MISMATCH,
    PROMPT_MISMATCH,
    PROVIDER_MISMATCH,
    RESULT_IDENTITY_MISMATCH,
    RESULT_REUSE_POLICY_VERSION,
    SCENARIO_MISMATCH,
    SCIENTIFIC_CONFIG_MISMATCH,
    SCIENTIFIC_FINGERPRINT_MISMATCH,
    SEED_MISMATCH,
    STATUS_NOT_FINISHED,
    UNSAFE_SYMLINK,
    ChildRunIdentity,
    ExpectedRunIdentity,
    ReusableRunCandidate,
    inspect_legacy_analysis_inputs,
    validate_child_run_reuse,
)


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _h(number: int) -> str:
    return "{:064x}".format(number)


class ResultReuseTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.run_dir = self.root / "runs" / "run-1"
        self.run_dir.mkdir(parents=True)
        self.manifest_path = self.run_dir / "run_manifest.json"
        self.result_path = self.run_dir / "experiment_result.json"
        self._write_result()
        self.manifest = self._manifest()
        self._write_manifest()
        self.expected = ExpectedRunIdentity.from_manifest(
            self.manifest_path, required_artifacts=("experiment_result.json",)
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _completion() -> dict:
        return {
            "schema_version": "1.0",
            "simulation_runs": {
                "unit": "simulation_runs",
                "planned": 1,
                "started": 1,
                "completed": 1,
                "failed": 0,
            },
            "agent_decisions": {
                "unit": "agent_decisions",
                "planned": 24,
                "attempted": 24,
                "completed": 24,
                "failed": 0,
                "skipped": 0,
            },
            "rounds": {
                "unit": "rounds",
                "planned": 4,
                "started": 4,
                "completed": 4,
                "failed": 0,
                "skipped": 0,
            },
            "llm_logical_requests": {
                "unit": "llm_logical_requests",
                "planned": 24,
                "attempted": 24,
                "completed": 24,
                "failed": 0,
            },
            "response_sources": {
                "unit": "final_responses",
                "provider": 24,
                "cache": 0,
                "replay": 0,
            },
            "provider_calls": {
                "unit": "logical_provider_requests_after_cache_and_replay",
                "attempted": 24,
                "succeeded": 24,
                "failed": 0,
            },
            "parsing": {
                "unit": "agent_decision_parse_operations",
                "attempted": 24,
                "succeeded": 24,
                "failed": 0,
                "fallbacks": 0,
            },
        }

    def _write_result(self, **updates) -> None:
        result = {
            "run_id": "run-1",
            "seed": 7,
            "model": "mock",
            "completion": self._completion(),
        }
        result.update(updates)
        self.result_path.write_text(
            json.dumps(result, sort_keys=True), encoding="utf-8"
        )

    def _manifest(self) -> dict:
        artifact = self.result_path.read_bytes()
        return {
            "schema_version": "1.0",
            "run_id": "run-1",
            "status": "finished",
            "failure_stage": None,
            "managed_run_completed": True,
            "outputs_complete": True,
            "managed_context": {
                "run_kind": "simulation",
                "command_identity": "python -m experiments.run_seed",
                "state": "FINISHED",
            },
            "completion": self._completion(),
            "recording_schema_version": "1.2",
            "scientific_component_fingerprint": _h(1),
            "decision_parser_schema_version": "1.0",
            "decision_parser_source_hash": _h(2),
            "event_schema_version": "1.0",
            "prompt_source_hash": _h(3),
            "persona_source_hash": _h(4),
            "simulation_core_source_hash": _h(5),
            "config_hash_schema_version": "1.0",
            "scientific_config_hash": _h(6),
            "model_request_config_hash": _h(7),
            "scientific_config_summary": {
                "max_llm_agents": 40,
                "n_llm_agents": 6,
                "n_noise_agents": 8,
                "population": {"mode": "legacy", "counts": None},
            },
            "model_request_config_summary": {
                "openai_base_url": {
                    "configured": True,
                    "endpoint_identity_sha256": _h(8),
                    "userinfo_redacted": False,
                }
            },
            "scenario": {"id": "human-label", "definition_sha256": _h(9)},
            "rng": {"seed": 7},
            "inputs": [
                {
                    "label": "reference_path",
                    "path": "/some/other/root/reference.csv",
                    "exists": True,
                    "kind": "file",
                    "size_bytes": 3,
                    "sha256": _h(10),
                    "error": None,
                }
            ],
            "personas": {
                "population": {
                    "planned_llm_total": 6,
                    "planned_noise_total": 8,
                    "actual_llm_total": 6,
                    "actual_noise_total": 8,
                    "actual_agent_ids": ["agent-{}".format(i) for i in range(14)],
                }
            },
            "llm": {
                "provider": "mock",
                "resolved_provider": "mock",
                "model": None,
                "resolved_model": "mock",
                "temperature": 0.0,
                "max_tokens": 1024,
                "cache_enabled": True,
                "runtime": {
                    "model_config": {
                        "requested_provider": "mock",
                        "resolved_provider": "mock",
                        "provider": "mock",
                        "model": "mock",
                        "temperature": 0.0,
                        "max_tokens": 1024,
                        "cache_enabled": True,
                        "endpoint_sha256": None,
                    }
                },
            },
            "git": {
                "commit": "a" * 40,
                "dirty": False,
                "diff_hash": None,
            },
            "results": [
                {
                    "path": "experiment_result.json",
                    "exists": True,
                    "kind": "file",
                    "size_bytes": len(artifact),
                    "sha256": _hash_bytes(artifact),
                    "error": None,
                    "inside_run_directory": True,
                }
            ],
            "compatibility": {"legacy_links": []},
        }

    def _write_manifest(self) -> None:
        self.manifest_path.write_text(
            json.dumps(self.manifest, sort_keys=True), encoding="utf-8"
        )

    def _refresh_artifact_descriptor(self) -> None:
        content = self.result_path.read_bytes()
        descriptor = self.manifest["results"][0]
        descriptor["size_bytes"] = len(content)
        descriptor["sha256"] = _hash_bytes(content)
        self._write_manifest()

    def _decision(self, path: Path | None = None, expected=None):
        return validate_child_run_reuse(
            ReusableRunCandidate(path or self.manifest_path, self.root),
            expected or self.expected,
        )

    def test_matching_finished_child_is_reusable(self) -> None:
        decision = self._decision()
        self.assertTrue(decision.reusable)
        self.assertEqual(decision.policy_version, RESULT_REUSE_POLICY_VERSION)
        self.assertEqual(decision.artifacts_verified, 1)
        self.assertEqual(decision.reason_codes, ())

    def test_relative_candidate_already_prefixed_by_relative_out_root(self) -> None:
        candidate = Path(os.path.relpath(self.manifest_path, Path.cwd()))
        decision = self._decision(path=candidate)
        self.assertTrue(decision.reusable)

    def test_cross_commit_same_scientific_fingerprint_is_reusable(self) -> None:
        expected = replace(self.expected, git_commit="b" * 40)
        decision = self._decision(expected=expected)
        self.assertTrue(decision.reusable)
        self.assertTrue(decision.cross_commit_same_scientific_fingerprint)

    def test_scientific_source_and_runtime_config_mismatches_reject(self) -> None:
        cases = (
            ("scientific_component_fingerprint", _h(50), SCIENTIFIC_FINGERPRINT_MISMATCH),
            ("scientific_config_hash", _h(51), SCIENTIFIC_CONFIG_MISMATCH),
            ("model_request_config_hash", _h(52), MODEL_REQUEST_CONFIG_MISMATCH),
            ("prompt_source_hash", _h(53), PROMPT_MISMATCH),
            ("persona_source_hash", _h(54), PERSONA_MISMATCH),
        )
        for field, value, reason in cases:
            with self.subTest(field=field):
                decision = self._decision(expected=replace(self.expected, **{field: value}))
                self.assertFalse(decision.reusable)
                self.assertIn(reason, decision.reason_codes)

    def test_provider_and_model_mismatch_reject(self) -> None:
        provider = self._decision(
            expected=replace(self.expected, resolved_provider="openai")
        )
        model = self._decision(
            expected=replace(self.expected, resolved_model="different-model")
        )
        self.assertIn(PROVIDER_MISMATCH, provider.reason_codes)
        self.assertIn(MODEL_MISMATCH, model.reason_codes)

    def test_seed_population_scenario_and_input_mismatch_reject(self) -> None:
        cases = (
            (replace(self.expected, seed=8), SEED_MISMATCH),
            (replace(self.expected, population_identity=_h(70)), POPULATION_MISMATCH),
            (replace(self.expected, scenario_definition_hash=_h(71)), SCENARIO_MISMATCH),
            (replace(self.expected, scientific_input_identity=_h(72)), INPUT_IDENTITY_MISMATCH),
        )
        for expected, reason in cases:
            with self.subTest(reason=reason):
                self.assertIn(reason, self._decision(expected=expected).reason_codes)

    def test_failed_or_incomplete_lifecycle_rejects(self) -> None:
        cases = (
            ("status", "failed", STATUS_NOT_FINISHED),
            ("managed_run_completed", False, MANAGED_RUN_INCOMPLETE),
            ("outputs_complete", False, OUTPUTS_INCOMPLETE),
        )
        for field, value, reason in cases:
            with self.subTest(field=field):
                original = self.manifest[field]
                self.manifest[field] = value
                self._write_manifest()
                self.assertIn(reason, self._decision().reason_codes)
                self.manifest[field] = original
                self._write_manifest()

    def test_incomplete_simulation_completion_rejects(self) -> None:
        self.manifest["completion"]["simulation_runs"]["completed"] = 0
        self._write_manifest()
        self.assertIn(COMPLETION_INCOMPLETE, self._decision().reason_codes)

    def test_missing_or_tampered_artifact_rejects(self) -> None:
        self.result_path.unlink()
        self.assertIn(ARTIFACT_MISSING, self._decision().reason_codes)
        self._write_result()
        self.result_path.write_text("tampered", encoding="utf-8")
        self.assertIn(ARTIFACT_HASH_MISMATCH, self._decision().reason_codes)

    def test_every_registered_artifact_is_rehashed(self) -> None:
        extra = self.run_dir / "events.jsonl"
        extra.write_text("original\n", encoding="utf-8")
        content = extra.read_bytes()
        self.manifest["results"].append(
            {
                "path": "events.jsonl",
                "exists": True,
                "kind": "file",
                "size_bytes": len(content),
                "sha256": _hash_bytes(content),
                "error": None,
                "inside_run_directory": True,
            }
        )
        self._write_manifest()
        self.assertTrue(self._decision().reusable)
        extra.write_text("changed\n", encoding="utf-8")
        self.assertIn(ARTIFACT_HASH_MISMATCH, self._decision().reason_codes)

    def test_result_completion_identity_mismatch_rejects(self) -> None:
        result = json.loads(self.result_path.read_text(encoding="utf-8"))
        result["completion"]["agent_decisions"]["completed"] = 23
        self.result_path.write_text(json.dumps(result), encoding="utf-8")
        self._refresh_artifact_descriptor()
        self.assertIn(RESULT_IDENTITY_MISMATCH, self._decision().reason_codes)

    def test_artifact_symlink_outside_run_is_rejected(self) -> None:
        outside = self.root / "outside.json"
        outside.write_text(self.result_path.read_text(encoding="utf-8"), encoding="utf-8")
        self.result_path.unlink()
        self.result_path.symlink_to(outside)
        self.assertIn(UNSAFE_SYMLINK, self._decision().reason_codes)

    def test_managed_compatibility_link_can_resolve_to_child_manifest(self) -> None:
        link = self.root / "cell_s7.json"
        link.symlink_to(Path("runs/run-1/experiment_result.json"))
        self.manifest["compatibility"]["legacy_links"] = [
            {
                "path": str(link),
                "target": "runs/run-1/experiment_result.json",
            }
        ]
        self._write_manifest()
        self.assertTrue(self._decision(path=link).reusable)

    def test_flat_result_without_child_manifest_is_not_reuse(self) -> None:
        flat = self.root / "legacy.json"
        flat.write_text("{}", encoding="utf-8")
        decision = self._decision(path=flat)
        self.assertFalse(decision.reusable)
        self.assertEqual(decision.primary_reason, LEGACY_FLAT_RESULT_UNVERIFIED)

    def test_missing_or_invalid_manifest_fails_closed(self) -> None:
        missing = self.run_dir / "missing" / "run_manifest.json"
        self.assertEqual(self._decision(path=missing).primary_reason, "manifest_missing")
        self.manifest_path.write_text("not-json", encoding="utf-8")
        self.assertEqual(self._decision().primary_reason, "manifest_invalid")

    def test_unsafe_compatibility_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as other:
            target = Path(other) / "legacy.json"
            target.write_text("{}", encoding="utf-8")
            link = self.root / "escaped.json"
            link.symlink_to(target)
            self.assertEqual(self._decision(path=link).primary_reason, UNSAFE_SYMLINK)

    def test_public_summary_contains_only_identity_codes(self) -> None:
        decision = self._decision(
            expected=replace(self.expected, resolved_model="Bearer private-token")
        )
        public = json.dumps(decision.public_summary(), sort_keys=True)
        self.assertNotIn("private-token", public)
        self.assertNotIn("rationale", public)
        self.assertIn(MODEL_MISMATCH, public)

    def test_expected_identity_from_config_never_constructs_provider(self) -> None:
        cfg = Config(provider="mock", seed=7, reference_path="nmsim/meta_feb2022_reference.csv")
        with mock.patch.dict(os.environ, {"LLM_PROVIDER": "mock"}), mock.patch(
            "nmsim.llm.build_llm", side_effect=AssertionError("provider built")
        ):
            expected = ExpectedRunIdentity.from_effective_config(
                cfg,
                command_identity="python -m experiments.run_seed",
                required_artifacts=("experiment_result.json",),
                base_dir=Path(__file__).resolve().parent.parent,
            )
        self.assertEqual(expected.requested_provider, "mock")
        self.assertEqual(expected.resolved_provider, "mock")
        self.assertEqual(expected.resolved_model, "mock")
        self.assertEqual(expected.recording_schema_version, "1.2")

    def test_expected_identity_resolves_anthropic_default_without_sdk_client(self) -> None:
        cfg = Config(
            provider="anthropic",
            seed=7,
            reference_path="nmsim/meta_feb2022_reference.csv",
        )
        with mock.patch.dict(os.environ, {"LLM_PROVIDER": "anthropic"}), mock.patch(
            "nmsim.llm.build_llm", side_effect=AssertionError("provider built")
        ):
            expected = ExpectedRunIdentity.from_effective_config(
                cfg,
                command_identity="python -m experiments.run_seed",
                required_artifacts=("experiment_result.json",),
                base_dir=Path(__file__).resolve().parent.parent,
            )
        self.assertEqual(expected.resolved_provider, "anthropic")
        self.assertTrue(expected.resolved_model.startswith("claude-"))

    def test_legacy_analysis_inputs_are_hashed_but_not_counted_as_runs(self) -> None:
        readable = self.root / "historical.json"
        readable.write_text('{"old": true}', encoding="utf-8")
        missing = self.root / "missing.json"
        summary = inspect_legacy_analysis_inputs((readable, missing))
        payload = summary.as_manifest_payload()
        self.assertEqual(payload["provenance_class"], "legacy_unverified_input")
        self.assertEqual(payload["total_files"], 2)
        self.assertEqual(payload["readable_files"], 1)
        self.assertEqual(payload["failed_files"], 1)
        self.assertEqual(payload["identity_unverified_files"], 2)
        self.assertEqual(payload["inputs"][0]["sha256"], _hash_bytes(readable.read_bytes()))
        self.assertNotIn("executed_runs", payload)
        self.assertNotIn("reused_runs", payload)
        self.assertNotIn("honest_n_runs", payload)


if __name__ == "__main__":
    unittest.main()
