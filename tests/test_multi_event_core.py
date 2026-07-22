from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from experiments.driver_utils import (
    assess_run_seed_reuse,
    expected_run_seed_identity,
)
from experiments.multi_event import build_multi_event_child_command
from experiments.run_seed import build_argparser, config_from_args
from nmsim.config import Config, NewsTimelineEntry, normalize_news_timeline
from nmsim.fingerprint import SCIENTIFIC_COMPONENT_FILES
from nmsim.llm import CostTracker
from nmsim.multi_event import (
    FROZEN_PROTOCOL_SHA256,
    MultiEventProtocolError,
    canonical_multi_event_basename,
    load_multi_event_material,
    load_protocol,
    transform_reference_episode,
)
from nmsim.provenance import sha256_file
from nmsim.reference_data import load_reference_episode
from nmsim.result_reuse import (
    HEALTH_GATE_REJECTED,
    REPORTED_MODEL_GATE_REJECTED,
    RESULT_IDENTITY_MISMATCH,
)
from nmsim.sim import run_sim


ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = ROOT / "experiments" / "multi_event_protocol.json"
CATALOG = ROOT / "nmsim" / "reference_data" / "v1" / "catalog.json"


def _material(event_id: str = "meta_2022_02_crash_v1"):
    protocol, _digest = load_protocol(PROTOCOL)
    row = next(item for item in protocol["design"]["events"] if item["event_id"] == event_id)
    return load_multi_event_material(
        event_id=event_id,
        reference_csv=ROOT / row["reference_csv"],
        news_timeline_jsonl=ROOT / row["news_timeline"],
        protocol_path=PROTOCOL,
        catalog_path=CATALOG,
    )


class _CaptureLLM:
    kind = "mock"
    model = "mock"

    def __init__(self) -> None:
        self.batches: list[list[tuple[str, str]]] = []
        self.response = json.dumps(
            {
                "action": "hold",
                "quantity": 0,
                "limit_price": 100.0,
                "sentiment": 0.0,
                "public_take": "PUBLIC_GENERIC",
                "reasoning": "PRIVATE_REASONING_SENTINEL",
            }
        )

    def complete_batch(self, prompts):
        batch = list(prompts)
        self.batches.append(batch)
        return [self.response for _ in batch]


class _EventLogger:
    def __init__(self) -> None:
        self.records = []

    def emit(self, name, **kwargs):
        self.records.append((name, kwargs))


class NewsTimelineTests(unittest.TestCase):
    def test_exact_schema_stable_order_and_immutable_null(self) -> None:
        self.assertEqual(Config().news_timeline, ())
        entries = normalize_news_timeline(
            [
                {"event_id": "later", "round": 3, "public_text": " later "},
                {"event_id": "same-a", "round": 2, "public_text": "A"},
                {"event_id": "same-b", "round": 2, "public_text": "B"},
            ],
            n_rounds=3,
        )
        self.assertIsInstance(entries, tuple)
        self.assertTrue(all(isinstance(item, NewsTimelineEntry) for item in entries))
        self.assertEqual([item.event_id for item in entries], ["same-a", "same-b", "later"])
        self.assertEqual(entries[-1].public_text, " later ")

    def test_schema_round_type_and_unique_id_fail_closed(self) -> None:
        bad_values = (
            [{"event_id": "a", "round": 1, "public_text": "x", "source": "private"}],
            [{"event_id": "a", "round": True, "public_text": "x"}],
            [{"event_id": "a", "round": 0, "public_text": "x"}],
            [{"event_id": "a", "round": 3, "public_text": "x"}],
            [{"event_id": 7, "round": 1, "public_text": "x"}],
            [{"event_id": "a", "round": 1, "public_text": 7}],
            [
                {"event_id": "a", "round": 1, "public_text": "x"},
                {"event_id": "a", "round": 2, "public_text": "y"},
            ],
        )
        for value in bad_values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_news_timeline(value, n_rounds=2)

    def test_single_event_is_prompt_and_trajectory_equivalent_to_legacy(self) -> None:
        headline = "EXACT PUBLIC HEADLINE"
        common = dict(
            seed=9,
            n_rounds=3,
            news_round=2,
            n_llm_agents=6,
            n_noise_agents=0,
            social_enabled=False,
            seed_fraction=2 / 6,
        )
        legacy = Config(news_text=headline, **common)
        timeline = Config(
            news_text="ignored by timeline",
            news_timeline=(
                {"event_id": "event", "round": 2, "public_text": headline},
            ),
            **common,
        )
        legacy_llm = _CaptureLLM()
        timeline_llm = _CaptureLLM()
        legacy_result = run_sim(legacy, legacy_llm, CostTracker())
        timeline_result = run_sim(timeline, timeline_llm, CostTracker())
        self.assertEqual(legacy_llm.batches, timeline_llm.batches)
        self.assertEqual(legacy_result.history, timeline_result.history)

    def test_cumulative_news_only_enters_seed_prompts_and_public_log_is_hashed(self) -> None:
        markers = ("DIRECT_EVENT_ALPHA", "DIRECT_EVENT_BETA", "DIRECT_EVENT_GAMMA")
        cfg = Config(
            seed=4,
            n_rounds=2,
            news_round=1,
            news_text="",
            news_timeline=(
                {"event_id": "a", "round": 1, "public_text": markers[0]},
                {"event_id": "b", "round": 2, "public_text": markers[1]},
                {"event_id": "c", "round": 2, "public_text": markers[2]},
            ),
            n_llm_agents=6,
            n_noise_agents=0,
            social_enabled=False,
            seed_fraction=2 / 6,
        )
        llm = _CaptureLLM()
        logger = _EventLogger()
        run_sim(cfg, llm, CostTracker(), event_logger=logger)
        round_one = [user for _system, user in llm.batches[0]]
        round_two = [user for _system, user in llm.batches[1]]
        self.assertEqual(sum(markers[0] in user for user in round_one), 2)
        for marker in markers:
            self.assertEqual(sum(marker in user for user in round_two), 2)
        self.assertFalse(any("PRIVATE_REASONING_SENTINEL" in user for user in round_two))
        public_log = json.dumps(
            [(name, payload.get("data")) for name, payload in logger.records],
            sort_keys=True,
        )
        for marker in markers:
            self.assertNotIn(marker, public_log)
        self.assertNotIn("PRIVATE_REASONING_SENTINEL", public_log)
        delivered = [item for item in logger.records if item[0] == "ScenarioEventDelivered"]
        self.assertEqual(len(delivered), 6)
        self.assertTrue(all("public_text_sha256" in item[1]["data"] for item in delivered))


class ProtocolAndTransformTests(unittest.TestCase):
    def test_frozen_protocol_and_all_authoritative_source_hashes(self) -> None:
        protocol, digest = load_protocol(PROTOCOL)
        self.assertEqual(digest, FROZEN_PROTOCOL_SHA256)
        self.assertEqual(sha256_file(CATALOG), protocol["reference_data_catalog"]["sha256"])
        for row in protocol["design"]["events"]:
            self.assertEqual(sha256_file(ROOT / row["reference_csv"]), row["reference_csv_sha256"])
            self.assertEqual(sha256_file(ROOT / row["news_timeline"]), row["news_timeline_sha256"])

    def test_any_protocol_byte_tamper_is_rejected(self) -> None:
        original = json.loads(PROTOCOL.read_text(encoding="utf-8"))
        mutations = (
            ("workers", lambda value: value["acceptance_and_execution"].__setitem__("workers", 2)),
            ("seed", lambda value: value["design"]["seeds"].__setitem__(0, 12)),
            ("source", lambda value: value["design"]["events"][0].__setitem__("reference_csv_sha256", "0" * 64)),
            ("estimand", lambda value: value["design"].__setitem__("estimand", "changed")),
        )
        for name, mutate in mutations:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as raw:
                value = json.loads(json.dumps(original))
                mutate(value)
                path = Path(raw) / "protocol.json"
                path.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises(MultiEventProtocolError):
                    load_protocol(path)

    def test_catalog_and_event_files_cannot_be_rebound_to_copies(self) -> None:
        material = _material()
        with tempfile.TemporaryDirectory() as raw:
            copied = Path(raw) / material.reference_csv.name
            copied.write_bytes(material.reference_csv.read_bytes())
            with self.assertRaises(MultiEventProtocolError):
                load_multi_event_material(
                    event_id=material.event_id,
                    reference_csv=copied,
                    news_timeline_jsonl=material.news_timeline_jsonl,
                    protocol_path=PROTOCOL,
                    catalog_path=CATALOG,
                )

    def test_transform_is_25_point_full_horizon_and_delivery_formula_is_exact(self) -> None:
        spy = _material("spy_2020_03_covid_v_recovery_v1")
        self.assertEqual(spy.transformed.terminal_t, 126)
        self.assertEqual(len(spy.transformed.norm_log_path), 25)
        self.assertEqual(spy.transformed.norm_log_path[0], 0.0)
        self.assertEqual(spy.transformed.news_timeline[0].round, 1)
        source = load_reference_episode(
            spy.reference_csv,
            news_timeline_path=spy.news_timeline_jsonl,
            include_news_timeline=True,
        )
        terminal_event = replace(
            source.news_timeline[0], event_id="terminal", delivery_t=126
        )
        transformed = transform_reference_episode(
            replace(source, news_timeline=(terminal_event,)),
            reference_path_hash=spy.reference_hash,
            source_timeline_hash=spy.timeline_hash,
        )
        self.assertEqual(transformed.news_timeline[0].round, 24)

    def test_reference_transform_hashes_are_frozen(self) -> None:
        expected = {
            "meta_2022_02_crash_v1": "2eac4124c4da9085adb50d6941c194c603f124d8d768040a748efbddf79209ef",
            "spy_2020_03_covid_v_recovery_v1": "57b13968e2bd73af4611945ba08d4f955d97fa18b00a0b3445f32295eff97885",
            "meta_2023_02_efficiency_jump_v1": "2961e1348fd4879fecd6d2c848fb010367921c9f8e095bebdf49dde8b52e4551",
        }
        for event_id, digest in expected.items():
            with self.subTest(event_id=event_id):
                self.assertEqual(_material(event_id).reference_transform_sha256, digest)


class RunSeedMultiEventTests(unittest.TestCase):
    def _tokens(self, *extra: str) -> list[str]:
        material = _material()
        return [
            "--seed", "11", "--provider", "mock", "--social", "off",
            "--repeat-idx", "1", "--reference-csv", str(material.reference_csv),
            "--news-timeline-jsonl", str(material.news_timeline_jsonl),
            "--event-id", material.event_id, "--protocol", str(PROTOCOL),
            "--catalog", str(CATALOG), *extra,
        ]

    def test_repeat_alias_conflict_and_inactive_overrides_fail_closed(self) -> None:
        parser = build_argparser()
        with self.assertRaises(ValueError):
            config_from_args(parser.parse_args(self._tokens("--rep", "2")), environment={})
        for extra in (
            ("--gain", "2"),
            ("--total", "31"),
            ("--leverage-ratio", "3"),
            ("--leverage-spread", "0.2"),
            ("--maint", "0.2"),
            ("--lev-fraction", "0.2"),
        ):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                config_from_args(parser.parse_args(self._tokens(*extra)), environment={})

    def test_repeat_and_technical_retry_do_not_change_config_hash_or_seed(self) -> None:
        material = _material()
        base = build_multi_event_child_command(
            material=material,
            arm="social_off",
            seed=11,
            repeat_idx=1,
            provider="mock",
            out_root=Path("/tmp/multi-event-test"),
        )
        repeat_two = list(base)
        repeat_two[repeat_two.index("--repeat-idx") + 1] = "2"
        technical = list(base) + ["--technical-retry-idx", "5", "--run-id", "ignored"]
        first = expected_run_seed_identity(base)
        second = expected_run_seed_identity(repeat_two)
        retry = expected_run_seed_identity(technical)
        self.assertEqual(first.seed, 11)
        self.assertEqual(first.scientific_config_hash, second.scientific_config_hash)
        self.assertEqual(first.model_request_config_hash, second.model_request_config_hash)
        self.assertEqual(first.scientific_config_hash, retry.scientific_config_hash)
        self.assertNotEqual(first.experiment_slot, second.experiment_slot)
        self.assertEqual(first.experiment_slot, retry.experiment_slot)
        self.assertEqual(canonical_multi_event_basename(first.experiment_slot), "me_meta_2022_02_crash_v1_social_off_s11_r1.json")

    def test_expected_identity_is_independent_of_later_ambient_environment(self) -> None:
        material = _material()
        command = build_multi_event_child_command(
            material=material,
            arm="social_on",
            seed=13,
            repeat_idx=2,
            provider="mock",
            out_root=Path("/tmp/multi-event-test"),
        )
        baseline = expected_run_seed_identity(command)
        with mock.patch.dict(
            os.environ,
            {
                "LLM_PROVIDER": "openai",
                "LLM_MODEL": "wrong",
                "OPENAI_BASE_URL": "http://wrong.invalid/v1",
            },
        ):
            self.assertEqual(expected_run_seed_identity(command), baseline)

    def test_legacy_defaults_and_rep_filename_semantics_remain_available(self) -> None:
        args = build_argparser().parse_args(["--seed", "7", "--provider", "mock", "--rep", "0"])
        cfg = config_from_args(args, environment={})
        self.assertEqual(args.repeat_idx, 0)
        self.assertEqual(args.rep, 0)
        self.assertEqual(cfg.news_timeline, ())
        self.assertEqual(cfg.news_round, Config().news_round)

    def test_scientific_fingerprint_covers_loader_and_result_assembler(self) -> None:
        self.assertIn("experiments/run_seed.py", SCIENTIFIC_COMPONENT_FILES)
        self.assertIn("nmsim/multi_event.py", SCIENTIFIC_COMPONENT_FILES)
        self.assertIn("nmsim/reference_data/__init__.py", SCIENTIFIC_COMPONENT_FILES)


class ManagedReuseHealthAndIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.material = _material()
        cls.command = build_multi_event_child_command(
            material=cls.material,
            arm="social_off",
            seed=11,
            repeat_idx=1,
            provider="mock",
            out_root=cls.root,
        )
        command = list(cls.command) + ["--run-id", "multi-event-reuse-fixture"]
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in {"LLM_PROVIDER", "LLM_MODEL", "OPENAI_BASE_URL", "OPENAI_API_KEY"}
        }
        completed = subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode:
            raise AssertionError(completed.stderr)
        cls.run_dir = cls.root / "runs" / "multi-event-reuse-fixture"
        cls.manifest_path = cls.run_dir / "run_manifest.json"
        cls.result_path = cls.run_dir / "experiment_result.json"
        cls.original_manifest = cls.manifest_path.read_bytes()
        cls.original_result = cls.result_path.read_bytes()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def setUp(self) -> None:
        self.manifest_path.write_bytes(self.original_manifest)
        self.result_path.write_bytes(self.original_result)

    def _refresh_result_descriptor(self, manifest: dict) -> None:
        content = self.result_path.read_bytes()
        descriptor = next(item for item in manifest["results"] if item["path"] == "experiment_result.json")
        descriptor["size_bytes"] = len(content)
        descriptor["sha256"] = hashlib.sha256(content).hexdigest()
        self.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def _decision(self):
        return assess_run_seed_reuse(
            candidate_path=self.manifest_path,
            allowed_result_root=self.root,
            child_command=self.command,
            max_bad_frac=0.15,
        )

    def test_manifest_candidate_reads_canonical_result_health(self) -> None:
        decision = self._decision()
        self.assertTrue(decision.reusable, decision.reason_codes)

    def test_health_threshold_uses_counts_and_rejects_invalid_fraction(self) -> None:
        for health in (
            {"bad_orders": 109, "total_llm_orders": 720, "bad_frac": 0.1514},
            {"bad_orders": 0, "total_llm_orders": 720, "bad_frac": float("nan")},
            {"bad_orders": 0, "total_llm_orders": 720, "bad_frac": -0.1},
            {"bad_orders": 1, "total_llm_orders": 720, "bad_frac": 0.0},
            {"bad_orders": 0, "total_llm_orders": 0, "bad_frac": 0.0},
        ):
            with self.subTest(health=health):
                manifest = json.loads(self.original_manifest)
                result = json.loads(self.original_result)
                result["health"] = health
                self.result_path.write_text(json.dumps(result), encoding="utf-8")
                self._refresh_result_descriptor(manifest)
                decision = self._decision()
                self.assertFalse(decision.reusable)
                self.assertIn(HEALTH_GATE_REJECTED, decision.reason_codes)

    def test_self_consistent_transform_tamper_is_rejected_against_expected_material(self) -> None:
        manifest = json.loads(self.original_manifest)
        result = json.loads(self.original_result)
        tampered = "f" * 64
        manifest["multi_event"]["reference_transform_sha256"] = tampered
        result["multi_event_identity"]["reference_transform_sha256"] = tampered
        self.result_path.write_text(json.dumps(result), encoding="utf-8")
        self._refresh_result_descriptor(manifest)
        decision = self._decision()
        self.assertFalse(decision.reusable)
        self.assertIn(RESULT_IDENTITY_MISMATCH, decision.reason_codes)

    def test_mock_reported_alias_cannot_be_fabricated_self_consistently(self) -> None:
        manifest = json.loads(self.original_manifest)
        result = json.loads(self.original_result)
        aliases = ["HiggsAI"]
        manifest["multi_event"]["reported_model_aliases"] = aliases
        manifest["completion"]["application_provider_attempts"]["reported_models"] = aliases
        result["reported_model_aliases"] = aliases
        result["completion"] = manifest["completion"]
        self.result_path.write_text(json.dumps(result), encoding="utf-8")
        self._refresh_result_descriptor(manifest)
        decision = self._decision()
        self.assertFalse(decision.reusable)
        self.assertIn(REPORTED_MODEL_GATE_REJECTED, decision.reason_codes)


if __name__ == "__main__":
    unittest.main()
