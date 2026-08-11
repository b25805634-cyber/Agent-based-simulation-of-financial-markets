"""Focused unit tests for immutable run provenance."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from nmsim.agents import make_agents
from nmsim.config import Config
from nmsim.provenance import RunManager


def _read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as stream:
        return json.load(stream)


def _event_types(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8") as stream:
        return [json.loads(line)["type"] for line in stream if line.strip()]


class RunProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.secret = "unit-test-api-key-must-not-leak"
        self.cfg = Config(
            provider="mock",
            seed=41,
            n_rounds=2,
            news_round=1,
            out_dir=str(self.root / "outputs"),
            openai_api_key=self.secret,
        )

    def _manager(self, run_id: str, **kwargs) -> RunManager:
        return RunManager.create(
            self.cfg,
            run_id=run_id,
            repo_root=Path.cwd(),
            **kwargs,
        )

    def test_manifest_captures_required_provenance_and_redacts_secret(self) -> None:
        input_path = self.root / "episode.csv"
        input_bytes = b"timestamp,price\n0,100.0\n"
        input_path.write_bytes(input_bytes)

        manager = self._manager(
            "manifest-fields",
            scenario_id="unit-scenario",
            worker_count=3,
            batching={"strategy": "one-round", "max_batch_size": 6},
            input_paths={"episode": input_path},
        )
        manager.set_population(make_agents(self.cfg))
        manifest = _read_json(manager.manifest_path)

        required = {
            "schema_version",
            "run_id",
            "scenario_id",
            "created_at",
            "started_at",
            "ended_at",
            "status",
            "failure_reason",
            "git",
            "config",
            "config_sha256",
            "scenario",
            "rng",
            "llm",
            "execution",
            "personas",
            "prompt",
            "inputs",
            "environment",
            "samples",
            "results",
        }
        self.assertTrue(required.issubset(manifest), required - set(manifest))
        self.assertEqual(manifest["status"], "running")
        self.assertEqual(manifest["scenario_id"], "unit-scenario")
        self.assertEqual(manifest["scenario"]["id"], "unit-scenario")
        self.assertEqual(manifest["rng"]["seed"], self.cfg.seed)

        # Normalize tuple-backed immutable config fields through JSON before
        # comparing them with the persisted JSON manifest representation.
        original_config = json.loads(json.dumps(asdict(self.cfg)))
        self.assertEqual(set(manifest["config"]), set(original_config))
        self.assertEqual(manifest["config"]["openai_api_key"], "<redacted>")
        for key, expected in original_config.items():
            if key != "openai_api_key":
                self.assertEqual(manifest["config"][key], expected, key)
        self.assertNotIn(self.secret, manager.manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(len(manifest["inputs"]), 1)
        self.assertEqual(manifest["inputs"][0]["label"], "episode")
        self.assertEqual(
            manifest["inputs"][0]["sha256"], hashlib.sha256(input_bytes).hexdigest()
        )
        self.assertEqual(manifest["inputs"][0]["size_bytes"], len(input_bytes))

        definitions = manifest["personas"]["definitions"]
        self.assertEqual(len(definitions), 6)
        self.assertTrue(all("id" in persona and "persona" in persona for persona in definitions))
        population = manifest["personas"]["population"]
        self.assertEqual(population["actual_llm_total"], self.cfg.n_llm_agents)
        self.assertEqual(population["actual_noise_total"], self.cfg.n_noise_agents)
        self.assertEqual(len(population["actual_agent_ids"]),
                         self.cfg.n_llm_agents + self.cfg.n_noise_agents)

        prompt = manifest["prompt"]
        self.assertRegex(prompt["template_version"], r"^sha256:[0-9a-f]{64}$")
        self.assertRegex(prompt["source_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(set(prompt["system_prompt_sha256"]),
                         {persona["id"] for persona in definitions})

        environment = manifest["environment"]
        self.assertTrue(environment["python_version"])
        self.assertTrue(environment["platform"])
        for dependency in ("numpy", "matplotlib", "anthropic", "openai", "httpx"):
            self.assertIn(dependency, environment["dependencies"])
        self.assertEqual(
            {"commit", "dirty", "diff_hash", "diff_hash_method", "error"},
            set(manifest["git"]),
        )
        self.assertEqual(manifest["execution"]["worker_count"], 3)
        self.assertEqual(manifest["execution"]["batching"]["max_batch_size"], 6)
        self.assertNotIn("research_profile", manifest)

    def test_non_persona_research_profile_does_not_load_legacy_prompts(self) -> None:
        profile = {
            "profile_id": "v2_attention_market/0.1",
            "persona_contract": {
                "applicable": False,
                "reason": "the protocol has no identity or Persona input",
            },
            "prompt_contract": {
                "schema_version": "v2_teacher_prompt/0.1",
                "source_sha256": "0" * 64,
            },
        }
        with mock.patch(
            "nmsim.provenance._prompt_metadata",
            side_effect=AssertionError("legacy Persona prompt was loaded"),
        ):
            manager = self._manager(
                "non-persona-profile", research_profile=profile
            )

        manifest = _read_json(manager.manifest_path)
        self.assertEqual(manifest["research_profile"], profile)
        self.assertFalse(manifest["personas"]["applicable"])
        self.assertEqual(manifest["personas"]["definitions"], [])
        self.assertEqual(
            manifest["personas"]["population"]["planned_llm_total"], 0
        )
        self.assertFalse(manifest["prompt"]["legacy_v1_prompt_applicable"])
        text = manager.manifest_path.read_text(encoding="utf-8")
        self.assertNotIn("retail_crowd", text)

    def test_research_profile_fails_closed_on_unknown_fields(self) -> None:
        profile = {
            "profile_id": "v2_attention_market/0.1",
            "persona_contract": {"applicable": False},
            "prompt_contract": {"schema_version": "v2_teacher_prompt/0.1"},
            "unexpected": True,
        }
        with self.assertRaisesRegex(ValueError, "fields must be exactly"):
            self._manager("bad-profile", research_profile=profile)
        self.assertFalse((self.root / "outputs" / "runs" / "bad-profile").exists())

    def test_manifest_redacts_endpoint_userinfo_query_and_fragment_credentials(self) -> None:
        userinfo_secret = "endpoint-userinfo-secret"
        query_secret = "endpoint-query-api-key"
        fragment_secret = "access_token=endpoint-fragment-secret"
        self.cfg.openai_base_url = (
            "https://user:{}@example.invalid/v1?api_key={}#{}".format(
                userinfo_secret,
                query_secret,
                fragment_secret,
            )
        )
        manager = self._manager("endpoint-redaction")
        manifest_text = manager.manifest_path.read_text(encoding="utf-8")
        for secret in (userinfo_secret, query_secret, fragment_secret):
            self.assertNotIn(secret, manifest_text)
        endpoint = _read_json(manager.manifest_path)["config"]["openai_base_url"]
        self.assertIn("<redacted>@example.invalid", endpoint)
        self.assertNotIn("user:", endpoint)

    def test_finish_writes_honest_n_results_and_finished_event(self) -> None:
        manager = self._manager("finished-run")
        result = manager.run_dir / "price_path.csv"
        result_bytes = b"round,price,volume\n1,99.5,12\n"
        result.write_bytes(result_bytes)
        manager.record_batch(6, round=1)

        returned = manager.finish(expected=12, completed=11, failed=2, honest_n=9)
        manifest = _read_json(returned)

        self.assertEqual(manifest["status"], "finished")
        self.assertIsNotNone(manifest["ended_at"])
        self.assertIsNone(manifest["failure_reason"])
        self.assertEqual(
            manifest["samples"],
            {"expected": 12, "completed": 11, "failed": 2, "honest_n": 9},
        )
        artifacts = {item["path"]: item for item in manifest["results"]}
        self.assertIn("price_path.csv", artifacts)
        self.assertEqual(
            artifacts["price_path.csv"]["sha256"],
            hashlib.sha256(result_bytes).hexdigest(),
        )
        self.assertIn("events.jsonl", artifacts)
        self.assertIn("private_events.jsonl", artifacts)
        self.assertIn("RunFinished", _event_types(manager.public_events_path))

    def test_failed_run_keeps_manifest_and_failure_event(self) -> None:
        manager = self._manager("failed-run")
        manager.fail(
            RuntimeError("provider failed while using {}".format(self.secret)),
            expected=12,
            completed=5,
            failed=2,
        )
        manifest = _read_json(manager.manifest_path)

        self.assertEqual(manifest["status"], "failed")
        self.assertIsNotNone(manifest["ended_at"])
        self.assertEqual(manifest["samples"]["honest_n"], 3)
        self.assertIn("RuntimeError", manifest["failure_reason"])
        self.assertNotIn(self.secret, manifest["failure_reason"])
        self.assertNotIn(self.secret, manager.manifest_path.read_text(encoding="utf-8"))
        self.assertIn("RunFailed", _event_types(manager.public_events_path))

    def test_duplicate_run_id_is_rejected_without_mutating_original(self) -> None:
        manager = self._manager("collision-run")
        sentinel = manager.run_dir / "sentinel.bin"
        sentinel.write_bytes(b"do-not-change")
        manifest_before = manager.manifest_path.read_bytes()
        files_before = sorted(path.name for path in manager.run_dir.iterdir())

        with self.assertRaises(FileExistsError):
            self._manager("collision-run")

        self.assertEqual(manager.manifest_path.read_bytes(), manifest_before)
        self.assertEqual(sentinel.read_bytes(), b"do-not-change")
        self.assertEqual(
            sorted(path.name for path in manager.run_dir.iterdir()), files_before
        )

    def test_legacy_links_never_overwrite_existing_regular_file(self) -> None:
        manager = self._manager("legacy-links")
        (manager.run_dir / "price_path.csv").write_text("new-price", encoding="utf-8")
        (manager.run_dir / "propagation.csv").write_text("new-propagation", encoding="utf-8")
        historical = manager.out_root / "price_path.csv"
        historical.write_text("historical-price", encoding="utf-8")

        compatibility = manager.publish_legacy_links(
            ["price_path.csv", "propagation.csv"]
        )

        self.assertFalse(historical.is_symlink())
        self.assertEqual(historical.read_text(encoding="utf-8"), "historical-price")
        self.assertTrue(any("price_path.csv" in item
                            for item in compatibility["skipped"]))
        self.assertTrue((manager.out_root / "latest").is_symlink())
        propagated = manager.out_root / "propagation.csv"
        self.assertTrue(propagated.is_symlink())
        self.assertEqual(propagated.read_text(encoding="utf-8"), "new-propagation")

    def test_flat_links_keep_their_immutable_source_when_latest_moves(self) -> None:
        first = self._manager("legacy-first")
        (first.run_dir / "first.json").write_text("first", encoding="utf-8")
        first.publish_legacy_links(["first.json"])

        second = self._manager("legacy-second")
        (second.run_dir / "second.json").write_text("second", encoding="utf-8")
        second.publish_legacy_links(["second.json"])

        first_link = first.out_root / "first.json"
        second_link = first.out_root / "second.json"
        self.assertTrue(first_link.is_symlink())
        self.assertTrue(second_link.is_symlink())
        self.assertEqual(first_link.read_text(encoding="utf-8"), "first")
        self.assertEqual(second_link.read_text(encoding="utf-8"), "second")
        self.assertIn("runs/legacy-first/first.json", os.readlink(first_link))
        self.assertIn("runs/legacy-second/second.json", os.readlink(second_link))


if __name__ == "__main__":
    unittest.main()
