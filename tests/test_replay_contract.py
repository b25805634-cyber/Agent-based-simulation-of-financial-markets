"""Strict replay compatibility and scientific-fingerprint contract tests.

These tests are deliberately offline.  Provider doubles are used only while
creating a private recording; every replay path must operate without an inner
provider or network fallback.
"""
from __future__ import annotations

from contextlib import redirect_stdout
import importlib
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from nmsim.config import Config
from nmsim.config_contract import build_effective_config_contract
from nmsim.fingerprint import (
    SCIENTIFIC_COMPONENT_FILES,
    STRICT_COMPATIBILITY_FIELDS,
    hash_relative_files,
    scientific_compatibility_metadata,
)
from nmsim.provenance import RunManager
from nmsim.recording import (
    RecordingLLM,
    ReplayLLM,
    ReplayMismatchError,
    request_fingerprint,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
run_module = importlib.import_module("nmsim.run")


class _CountingProvider:
    """Provider double exposing any accidental call made after recording."""

    kind = "mock"
    model = "offline-contract-fixture"
    temperature = 0.0
    max_tokens = 64

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, system: str, user: str) -> str:
        self.calls += 1
        return self._response()

    def complete_batch(self, prompts) -> list[str]:
        prompts = list(prompts)
        self.calls += len(prompts)
        return [self._response() for _ in prompts]

    @staticmethod
    def _response() -> str:
        return json.dumps(
            {
                "action": "hold",
                "quantity": 0,
                "limit_price": 100.0,
                "sentiment": 0.0,
                "public_take": "public fixture",
                "reasoning": "PRIVATE_PROVIDER_RATIONALE_MUST_NOT_LEAK",
            },
            sort_keys=True,
        )


class ReplayContractTests(unittest.TestCase):
    model_config = {
        "provider": "mock",
        "model": "offline-contract-fixture",
        "temperature": 0.0,
        "max_tokens": 64,
        "cache_enabled": False,
    }
    prompt = (
        "SYSTEM_PRIVATE_PROMPT_DO_NOT_PRINT",
        "USER_PRIVATE_PROMPT_DO_NOT_PRINT",
    )
    context = [{"agent_id": "agent-a", "persona_id": "persona-a"}]

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source_dir = self.root / "recording"
        self.source_dir.mkdir()
        self.provider = _CountingProvider()
        science = scientific_compatibility_metadata(
            REPO_ROOT,
            git_state={"commit": "a" * 40, "dirty": False},
        )
        cfg = Config(
            provider="mock",
            model="offline-contract-fixture",
            temperature=0.0,
            max_tokens=64,
            cache_enabled=False,
            out_dir=str(self.source_dir),
        )
        config_contract = build_effective_config_contract(
            cfg,
            base_dir=self.root,
            execution_context={"test_boundary": "replay-contract"},
        )
        self.compatibility = {**science, **config_contract}
        recorder = RecordingLLM(
            self.provider,
            self.source_dir,
            model_config=self.model_config,
            compatibility_metadata=self.compatibility,
        )
        recorder.set_batch_context(4, self.context)
        self.recorded_response = recorder.complete_batch([self.prompt])[0]
        self.assertEqual(self.provider.calls, 1)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _new_replay(self, compatibility=None) -> ReplayLLM:
        return ReplayLLM(
            self.source_dir,
            model_config=self.model_config,
            compatibility_metadata=(
                self.compatibility if compatibility is None else compatibility
            ),
        )

    def _replay_once(self, replay: ReplayLLM, prompt=None, context=None) -> str:
        replay.set_batch_context(4, self.context if context is None else context)
        return replay.complete_batch([self.prompt if prompt is None else prompt])[0]

    def test_same_scientific_contract_replays_without_provider(self):
        record = json.loads(
            (self.source_dir / "llm_records.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()[0]
        )
        for field in STRICT_COMPATIBILITY_FIELDS:
            self.assertEqual(record[field], self.compatibility[field])
        self.assertEqual(record["git_commit"], self.compatibility["git_commit"])
        self.assertEqual(record["git_dirty"], self.compatibility["git_dirty"])

        before = self.provider.calls
        replay = self._new_replay()
        self.assertNotIn("inner", vars(replay))
        self.assertEqual(self._replay_once(replay), self.recorded_response)
        replay.assert_exhausted()
        self.assertEqual(self.provider.calls, before)

    def test_recording_envelope_and_declared_schema_cannot_disagree(self):
        records_path = self.source_dir / "llm_records.jsonl"
        record = json.loads(records_path.read_text(encoding="utf-8"))
        record["schema_version"] = "1.0"
        records_path.write_text(
            json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            ReplayMismatchError, "inconsistent recording_schema_version"
        ):
            self._new_replay()
        self.assertEqual(self.provider.calls, 1)

    def test_every_strict_metadata_field_mismatch_is_explicit_and_private_safe(self):
        for field in STRICT_COMPATIBILITY_FIELDS:
            with self.subTest(field=field):
                changed = dict(self.compatibility)
                original = changed[field]
                if isinstance(original, str) and len(original) == 64:
                    replacement = ("0" if original[0] != "0" else "1") + original[1:]
                else:
                    replacement = "incompatible-test-version"
                changed[field] = replacement

                with self.assertRaises(ReplayMismatchError) as raised:
                    self._new_replay(changed)
                message = str(raised.exception)
                self.assertIn(field, message)
                self.assertIn("expected=", message)
                self.assertIn("actual=", message)
                if isinstance(original, str) and len(original) == 64:
                    self.assertIn("sha256:" + original[:12], message)
                    self.assertIn("sha256:" + replacement[:12], message)
                    self.assertNotIn(original, message)
                    self.assertNotIn(replacement, message)
                self.assertNotIn(self.prompt[0], message)
                self.assertNotIn(self.prompt[1], message)
                self.assertNotIn("PRIVATE_PROVIDER_RATIONALE", message)
                self.assertEqual(self.provider.calls, 1)

    def test_prompt_mismatch_reports_only_abbreviated_hashes(self):
        replay = self._new_replay()
        changed_prompt = (
            self.prompt[0],
            self.prompt[1] + "\nNEW_PRIVATE_MARKER_MUST_NOT_PRINT",
        )
        expected_hash = request_fingerprint(*self.prompt)["prompt_hash"]
        actual_hash = request_fingerprint(*changed_prompt)["prompt_hash"]
        with self.assertRaises(ReplayMismatchError) as raised:
            self._replay_once(replay, prompt=changed_prompt)
        message = str(raised.exception)
        self.assertIn("prompt_hash", message)
        self.assertIn("sha256:" + expected_hash[:12], message)
        self.assertIn("sha256:" + actual_hash[:12], message)
        self.assertNotIn(expected_hash, message)
        self.assertNotIn(actual_hash, message)
        self.assertNotIn("NEW_PRIVATE_MARKER_MUST_NOT_PRINT", message)
        self.assertNotIn(self.prompt[0], message)
        self.assertEqual(self.provider.calls, 1)

    def test_document_only_dirty_state_is_allowed_but_dirty_science_is_rejected(self):
        # Dirty is provenance, not a blanket incompatibility: a README-only
        # change leaves every strict scientific field identical.
        docs_only = dict(self.compatibility)
        docs_only["git_dirty"] = True
        replay = self._new_replay(docs_only)
        self.assertEqual(self._replay_once(replay), self.recorded_response)

        dirty_science = dict(docs_only)
        source_hash = dirty_science["simulation_core_source_hash"]
        dirty_science["simulation_core_source_hash"] = (
            ("0" if source_hash[0] != "0" else "1") + source_hash[1:]
        )
        fingerprint = dirty_science["scientific_component_fingerprint"]
        dirty_science["scientific_component_fingerprint"] = (
            ("0" if fingerprint[0] != "0" else "1") + fingerprint[1:]
        )
        with self.assertRaisesRegex(
            ReplayMismatchError, "simulation_core_source_hash"
        ):
            self._new_replay(dirty_science)
        self.assertEqual(self.provider.calls, 1)

    def test_cross_commit_same_fingerprint_replays_and_is_written_to_manifest(self):
        cfg = Config(
            provider="mock",
            n_rounds=1,
            news_round=1,
            n_llm_agents=1,
            n_noise_agents=0,
            cache_enabled=False,
            out_dir=str(self.root / "managed"),
        )
        manager = RunManager.create(cfg, run_id="cross-commit-contract")
        source_contract = dict(manager.replay_compatibility)
        current_commit = source_contract.get("git_commit")
        source_commit = "0" * 40 if current_commit != "0" * 40 else "1" * 40

        cross_source = self.root / "cross-source"
        cross_source.mkdir()
        source_contract["git_commit"] = source_commit
        source_contract["git_dirty"] = False
        provider = _CountingProvider()
        recorder = RecordingLLM(
            provider,
            cross_source,
            model_config=self.model_config,
            compatibility_metadata=source_contract,
        )
        recorder.set_batch_context(4, self.context)
        response = recorder.complete_batch([self.prompt])[0]

        replay = ReplayLLM(
            cross_source,
            model_config=self.model_config,
            event_logger=manager.events,
            compatibility_metadata=manager.replay_compatibility,
        )
        self.assertTrue(replay.cross_commit_same_scientific_fingerprint)
        replay.set_batch_context(4, self.context)
        self.assertEqual(replay.complete_batch([self.prompt])[0], response)
        replay.assert_exhausted()
        manager.register_llm_runtime(
            llm=replay,
            mode="replay",
            network_access=False,
            provider_calls=0,
        )
        manager.finish(expected=1, completed=1, failed=0, honest_n=1)

        manifest = json.loads(manager.manifest_path.read_text(encoding="utf-8"))
        for field in STRICT_COMPATIBILITY_FIELDS:
            self.assertEqual(
                manifest[field], manager.scientific_compatibility[field]
            )
        self.assertEqual(
            manifest["git_commit"],
            manager.scientific_compatibility["git_commit"],
        )
        self.assertEqual(
            manifest["git_dirty"],
            manager.scientific_compatibility["git_dirty"],
        )
        self.assertTrue(manifest["cross_commit_same_scientific_fingerprint"])
        compatibility = manifest["replay_compatibility"]
        self.assertTrue(compatibility["strict_compatibility_passed"])
        self.assertTrue(
            compatibility["cross_commit_same_scientific_fingerprint"]
        )
        self.assertEqual(compatibility["source_git_commit"], source_commit)
        self.assertEqual(
            compatibility["current_git_commit"],
            manager.scientific_compatibility.get("git_commit"),
        )
        self.assertEqual(provider.calls, 1)

    def test_high_level_mismatch_is_offline_failed_and_writes_no_canonical_outputs(self):
        out = self.root / "high-level"
        cfg = Config(
            provider="mock",
            seed=91,
            n_rounds=1,
            news_round=1,
            n_llm_agents=1,
            n_noise_agents=0,
            cache_enabled=False,
            out_dir=str(out),
        )
        with mock.patch.object(run_module, "_plot", return_value=None), redirect_stdout(
            io.StringIO()
        ):
            recorded = run_module.run(cfg, run_id="contract-source")

        records_path = Path(recorded.run_dir) / "llm_records.jsonl"
        records = [
            json.loads(line)
            for line in records_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        original_hash = records[0]["decision_parser_source_hash"]
        records[0]["decision_parser_source_hash"] = (
            ("0" if original_hash[0] != "0" else "1") + original_hash[1:]
        )
        records_path.write_text(
            "".join(
                json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
                for record in records
            ),
            encoding="utf-8",
        )

        before = set((out / "runs").iterdir())
        with mock.patch.object(
            run_module,
            "build_llm",
            side_effect=AssertionError("provider construction attempted"),
        ), mock.patch.object(run_module, "_plot", return_value=None), redirect_stdout(
            io.StringIO()
        ):
            with self.assertRaisesRegex(
                ReplayMismatchError, "decision_parser_source_hash"
            ):
                run_module.run(cfg, replay_from=recorded.run_dir)

        created = set((out / "runs").iterdir()) - before
        self.assertEqual(len(created), 1)
        failed_dir = created.pop()
        manifest = json.loads(
            (failed_dir / "run_manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["samples"]["completed"], 0)
        self.assertEqual(manifest["samples"]["honest_n"], 0)
        runtime = manifest["llm"]["runtime"]
        self.assertFalse(runtime["network_access"])
        self.assertEqual(runtime["provider_calls"], 0)

        events = [
            json.loads(line)
            for line in (failed_dir / "events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        self.assertEqual(events[-1]["type"], "RunFailed")
        canonical_outputs = {
            "price_path.csv",
            "reasoning_traces.csv",
            "propagation.csv",
            "stylized_facts.json",
            "config.json",
            "sim_overview.png",
        }
        self.assertFalse(canonical_outputs & {path.name for path in failed_dir.iterdir()})


class ScientificFingerprintTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _copy_scientific_tree(self, name: str) -> Path:
        destination = self.root / name
        (destination / "nmsim").mkdir(parents=True)
        for relative in SCIENTIFIC_COMPONENT_FILES:
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO_ROOT / relative, target)
        shutil.copy2(REPO_ROOT / "README.md", destination / "README.md")
        return destination

    @staticmethod
    def _metadata(root: Path) -> dict:
        return scientific_compatibility_metadata(
            root,
            git_state={"commit": "b" * 40, "dirty": False},
        )

    def test_hash_is_order_and_root_independent_and_ignores_documentation(self):
        first_root = self._copy_scientific_tree("first-location")
        second_root = self._copy_scientific_tree("different-absolute-location")

        first = self._metadata(first_root)
        second = self._metadata(second_root)
        self.assertEqual(first, second)
        self.assertEqual(
            hash_relative_files(first_root, SCIENTIFIC_COMPONENT_FILES),
            hash_relative_files(first_root, reversed(SCIENTIFIC_COMPONENT_FILES)),
        )

        (second_root / "README.md").write_text(
            "documentation-only mutation\n", encoding="utf-8"
        )
        after_docs = self._metadata(second_root)
        self.assertEqual(first, after_docs)
        self.assertEqual(
            first["prompt_source_hash"],
            "db9c26c22d35223ea7ee768d622c608f9ca27b4b81b58615720704c39e906171",
        )

    def test_scientific_source_mutation_changes_core_and_fingerprint(self):
        first_root = self._copy_scientific_tree("baseline")
        changed_root = self._copy_scientific_tree("changed")
        before = self._metadata(first_root)

        market_path = changed_root / "nmsim/market.py"
        source = market_path.read_text(encoding="utf-8")
        old = "last_price * (1 + kappa * net / depth)"
        new = "last_price * (1 + (kappa + 0.000001) * net / depth)"
        self.assertIn(old, source)
        market_path.write_text(source.replace(old, new, 1), encoding="utf-8")
        after = self._metadata(changed_root)

        self.assertNotEqual(
            before["simulation_core_source_hash"],
            after["simulation_core_source_hash"],
        )
        self.assertNotEqual(
            before["scientific_component_fingerprint"],
            after["scientific_component_fingerprint"],
        )
        self.assertEqual(
            before["decision_parser_source_hash"],
            after["decision_parser_source_hash"],
        )
        self.assertEqual(before["prompt_source_hash"], after["prompt_source_hash"])
        self.assertEqual(before["persona_source_hash"], after["persona_source_hash"])


if __name__ == "__main__":
    unittest.main()
