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
from experiments.run_seed import (
    _multi_event_health_from_decision_audits,
    _reported_model_aliases,
    build_argparser,
    config_from_args,
)
from nmsim.config import Config, NewsTimelineEntry, normalize_news_timeline
from nmsim.decision_contract import (
    ADAPTER_TERMINAL_STATUS_FIELD,
    DECISION_VALID,
    LEGACY_PARSE_INVALID,
    MULTI_EVENT_DECISION_RESPONSE_SCHEMA,
    PROVIDER_EXCEPTION_EXHAUSTED,
    PROVIDER_PARSE_EXHAUSTED,
    STRICT_SCHEMA_INVALID,
)
from nmsim.fingerprint import SCIENTIFIC_COMPONENT_FILES
from nmsim.llm import CostTracker, parse_order_with_validity
from nmsim.multi_event import (
    FROZEN_PROTOCOL_SHA256,
    FROZEN_PROTOCOL_SHA256_V2,
    PROTOCOL_SCHEMA_VERSION,
    PROTOCOL_SCHEMA_VERSION_V2,
    MultiEventProtocolError,
    ProtocolProfile,
    build_experiment_slot,
    canonical_multi_event_basename,
    get_protocol_profile,
    load_multi_event_material,
    load_protocol,
    transform_reference_episode,
)
from nmsim.provenance import sha256_file
from nmsim.reference_data import load_reference_episode
from nmsim.result_reuse import (
    ARTIFACT_INVALID,
    ARTIFACT_MISSING,
    HEALTH_GATE_REJECTED,
    REPORTED_MODEL_GATE_REJECTED,
    RESULT_IDENTITY_MISMATCH,
    RUNTIME_ENVIRONMENT_MISMATCH,
)
from nmsim.sim import run_sim


ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = ROOT / "experiments" / "multi_event_protocol.json"
PROTOCOL_V2 = ROOT / "experiments" / "multi_event_protocol_workers2.json"
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

    def test_frozen_protocol_profiles_are_versioned_and_fail_closed(self) -> None:
        v1, v1_digest = load_protocol(PROTOCOL)
        v2, v2_digest = load_protocol(PROTOCOL_V2)
        v1_profile = get_protocol_profile(v1, v1_digest)
        v2_profile = get_protocol_profile(v2, v2_digest)
        self.assertIsInstance(v1_profile, ProtocolProfile)
        self.assertIsInstance(v2_profile, ProtocolProfile)
        self.assertEqual(v1_digest, FROZEN_PROTOCOL_SHA256)
        self.assertEqual(v2_digest, FROZEN_PROTOCOL_SHA256_V2)
        self.assertEqual(sha256_file(PROTOCOL_V2), FROZEN_PROTOCOL_SHA256_V2)
        self.assertEqual(v1_profile.schema_version, PROTOCOL_SCHEMA_VERSION)
        self.assertEqual(v2_profile.schema_version, PROTOCOL_SCHEMA_VERSION_V2)
        self.assertEqual(v1_profile.protocol_id, "multi_event_distribution_v1")
        self.assertEqual(
            v2_profile.protocol_id,
            "multi_event_distribution_workers2_v1",
        )
        self.assertEqual(
            v1_profile.canonical_protocol_relative_path,
            "experiments/multi_event_protocol.json",
        )
        self.assertEqual(
            v2_profile.canonical_protocol_relative_path,
            "experiments/multi_event_protocol_workers2.json",
        )
        self.assertEqual(
            v1_profile.canonical_live_output_relative_path,
            "results_multi_event",
        )
        self.assertEqual(
            v2_profile.canonical_live_output_relative_path,
            "results_multi_event_workers2",
        )
        self.assertEqual((v1_profile.workers, v2_profile.workers), (1, 2))
        self.assertIsNone(v1_profile.execution_acceleration)
        self.assertEqual(
            v2_profile.execution_acceleration,
            v2["execution_acceleration"],
        )

    def test_both_frozen_protocols_reject_any_byte_tamper(self) -> None:
        for source in (PROTOCOL, PROTOCOL_V2):
            with self.subTest(source=source.name), tempfile.TemporaryDirectory() as raw:
                copied = Path(raw) / source.name
                copied.write_bytes(source.read_bytes() + b" ")
                with self.assertRaises(MultiEventProtocolError):
                    load_protocol(copied)

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

    def test_workers2_profile_rejects_worker_root_and_acceleration_drift(self) -> None:
        original, digest = load_protocol(PROTOCOL_V2)

        def clone():
            return json.loads(json.dumps(original))

        mutations = {
            "acceptance_workers": lambda value: value[
                "acceptance_and_execution"
            ].__setitem__("workers", 1),
            "frozen_workers": lambda value: value[
                "effective_config_freeze"
            ]["execution"].__setitem__("workers", 1),
            "frozen_root": lambda value: value["effective_config_freeze"][
                "execution"
            ].__setitem__(
                "out_dir",
                "canonical_repository_root/results_multi_event_no_symlink_or_override_for_live",
            ),
            "treatment_workers": lambda value: value[
                "execution_acceleration"
            ].__setitem__("treatment_workers", 1),
            "acceleration_root": lambda value: value[
                "execution_acceleration"
            ].__setitem__("canonical_live_output_root", "results_multi_event"),
            "missing_acceleration_key": lambda value: value[
                "execution_acceleration"
            ].pop("scheduler"),
            "extra_acceleration_key": lambda value: value[
                "execution_acceleration"
            ].__setitem__("unregistered", True),
            "missing_metric_definition": lambda value: value[
                "execution_acceleration"
            ]["canary_metric_definitions"].pop("terminal_children"),
            "extra_metric_definition": lambda value: value[
                "execution_acceleration"
            ]["canary_metric_definitions"].__setitem__("unregistered", "x"),
            "missing_canary_gate": lambda value: value[
                "execution_acceleration"
            ]["canary_promotion_gate"].pop("minimum_terminal_children"),
            "extra_canary_gate": lambda value: value[
                "execution_acceleration"
            ]["canary_promotion_gate"].__setitem__("unregistered", 0),
            "canary_cross_field": lambda value: value[
                "execution_acceleration"
            ].__setitem__("canary_slot_count", 3),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                value = clone()
                mutate(value)
                with self.assertRaises(MultiEventProtocolError):
                    get_protocol_profile(value, digest)

        v1, v1_digest = load_protocol(PROTOCOL)
        v1["execution_acceleration"] = {}
        with self.assertRaises(MultiEventProtocolError):
            get_protocol_profile(v1, v1_digest)

    def test_protocol_hash_isolates_workers2_experiment_slots(self) -> None:
        common = {
            "event_id": "meta_2022_02_crash_v1",
            "social_arm": "social_on",
            "seed": 11,
            "repeat_idx": 1,
        }
        v1_slot = build_experiment_slot(
            protocol_hash=FROZEN_PROTOCOL_SHA256, **common
        )
        v2_slot = build_experiment_slot(
            protocol_hash=FROZEN_PROTOCOL_SHA256_V2, **common
        )
        self.assertNotEqual(v1_slot["slot_id"], v2_slot["slot_id"])
        self.assertNotEqual(v1_slot["protocol_hash"], v2_slot["protocol_hash"])
        for field, expected in common.items():
            self.assertEqual(v1_slot[field], expected)
            self.assertEqual(v2_slot[field], expected)

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


class StrictDecisionResponseTests(unittest.TestCase):
    def _parse(self, payload, direction_field):
        return parse_order_with_validity(
            json.dumps(payload),
            100.0,
            response_schema=MULTI_EVENT_DECISION_RESPONSE_SCHEMA,
            direction_field=direction_field,
        )

    def test_valid_live_action_and_mock_side_forms(self) -> None:
        common = {
            "quantity": 3,
            "limit_price": 101.5,
            "sentiment": 0.2,
            "public_take": "Adding carefully.",
            "reasoning": "The signal is positive.",
        }
        for field in ("action", "side"):
            with self.subTest(field=field):
                order, validity = self._parse(
                    {**common, field: "buy"}, field
                )
                self.assertTrue(validity.valid)
                self.assertEqual(order["side"], "buy")
                self.assertEqual(order["quantity"], 3)

    def test_empty_unknown_missing_and_invalid_values_fail_strictly(self) -> None:
        valid = {
            "action": "hold",
            "quantity": 0,
            "limit_price": 100.0,
            "sentiment": 0.0,
            "public_take": "Waiting for clarity.",
            "reasoning": "No edge yet.",
        }
        cases = (
            ({}, "missing_required_field"),
            ({"unknown": 1}, "missing_required_field"),
            ({key: value for key, value in valid.items() if key != "action"}, "missing_required_field"),
            ({**valid, "action": "wait"}, "invalid_action"),
            ({**valid, "quantity": True}, "invalid_quantity"),
            ({**valid, "quantity": 2}, "quantity_action_mismatch"),
            ({**valid, "limit_price": float("nan")}, "invalid_limit_price"),
            ({**valid, "limit_price": 0}, "invalid_limit_price"),
            ({**valid, "sentiment": 1.1}, "invalid_sentiment"),
            ({**valid, "reasoning": " "}, "blank_reasoning"),
            ({**valid, "public_take": ""}, "blank_public_take"),
        )
        for payload, code in cases:
            with self.subTest(code=code):
                order, validity = self._parse(payload, "action")
                self.assertFalse(validity.valid)
                self.assertEqual(validity.error_code, code)
                self.assertEqual(order["side"], "hold")
                self.assertIn("strict-schema-failed", order["rationale"])

    def test_legacy_empty_object_coercion_is_unchanged(self) -> None:
        order, validity = parse_order_with_validity("{}", 100.0)
        self.assertTrue(validity.valid)
        self.assertEqual(order["side"], "hold")
        self.assertNotIn("strict-schema-failed", order["rationale"])

    @staticmethod
    def _audit(agent, validity):
        return {
            "agent": agent,
            "decision_response_schema": MULTI_EVENT_DECISION_RESPONSE_SCHEMA,
            "strict_schema_valid": validity.valid,
            "strict_schema_error_code": validity.error_code,
            "terminal_status": validity.terminal_status,
        }

    def test_machine_health_never_scans_valid_reasoning_markers(self) -> None:
        payload = {
            "action": "hold",
            "quantity": 0,
            "limit_price": 100.0,
            "sentiment": 0.0,
            "public_take": "Waiting.",
            "reasoning": (
                "This discusses api-error, parse-failed, and "
                "parse-retries-exhausted as text only."
            ),
        }
        order, validity = self._parse(payload, "action")
        self.assertTrue(validity.valid)
        self.assertEqual(validity.terminal_status, DECISION_VALID)
        bad, total, counts = _multi_event_health_from_decision_audits(
            {1: [self._audit("valid", validity)]}
        )
        self.assertEqual((bad, total), (0, 1))
        self.assertEqual(counts["valid_decisions"], 1)
        self.assertIn("api-error", order["rationale"])

    def test_machine_health_terminal_statuses_are_mutually_exclusive(self) -> None:
        def strict_fallback(status, rationale):
            raw = json.dumps(
                {
                    "side": "hold",
                    "quantity": 0,
                    "limit_price": 100.0,
                    "sentiment": 0.0,
                    "public_take": "",
                    "rationale": rationale,
                    ADAPTER_TERMINAL_STATUS_FIELD: status,
                }
            )
            _order, validity = parse_order_with_validity(
                raw,
                100.0,
                response_schema=MULTI_EVENT_DECISION_RESPONSE_SCHEMA,
                direction_field="action",
            )
            return validity

        parse_exhausted = strict_fallback(
            PROVIDER_PARSE_EXHAUSTED,
            "parse-retries-exhausted; holding",
        )
        provider_exhausted = strict_fallback(
            PROVIDER_EXCEPTION_EXHAUSTED,
            "api-error; holding",
        )
        _, strict_invalid = self._parse({}, "action")
        audits = {
            1: [
                self._audit("strict", strict_invalid),
                self._audit("provider-exception", provider_exhausted),
                self._audit("provider-parse", parse_exhausted),
            ]
        }
        bad, total, counts = _multi_event_health_from_decision_audits(audits)
        self.assertEqual((bad, total), (3, 3))
        self.assertEqual(
            counts,
            {
                "strict_schema_invalid": 1,
                "legacy_parse_invalid": 0,
                "provider_exception_exhausted": 1,
                "provider_parse_exhausted": 1,
                "valid_decisions": 0,
            },
        )
        legacy_audit = {
            "agent": "legacy",
            "decision_response_schema": MULTI_EVENT_DECISION_RESPONSE_SCHEMA,
            "strict_schema_valid": None,
            "strict_schema_error_code": "invalid_or_missing_json_object",
            "terminal_status": LEGACY_PARSE_INVALID,
        }
        with self.assertRaises(ValueError):
            _multi_event_health_from_decision_audits({1: [legacy_audit]})

    def test_run_seed_alias_projection_rejects_without_trimming(self) -> None:
        for alias in (" ", " leading", "trailing ", "bad\nline", "x" * 257):
            manager = type(
                "Manager",
                (),
                {
                    "manifest": {
                        "completion": {
                            "application_provider_attempts": {
                                "invalid_reported_model_alias_count": 0,
                                "reported_models_truncated": False,
                                "reported_models": [alias],
                            }
                        }
                    }
                },
            )()
            with self.subTest(alias=repr(alias)), self.assertRaises(ValueError):
                _reported_model_aliases(manager)

        mixed = type(
            "Manager",
            (),
            {
                "manifest": {
                    "completion": {
                        "application_provider_attempts": {
                            "invalid_reported_model_alias_count": 1,
                            "reported_models_truncated": False,
                            "reported_models": ["HiggsAI"],
                        }
                    }
                }
            },
        )()
        with self.assertRaises(ValueError):
            _reported_model_aliases(mixed)

    def test_scientific_fingerprint_covers_loader_and_result_assembler(self) -> None:
        self.assertIn("experiments/run_seed.py", SCIENTIFIC_COMPONENT_FILES)
        self.assertIn("nmsim/multi_event.py", SCIENTIFIC_COMPONENT_FILES)
        self.assertIn("nmsim/decision_contract.py", SCIENTIFIC_COMPONENT_FILES)
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
        cls.audit_artifacts = {
            name: (cls.run_dir / name).read_bytes()
            for name in (
                "llm_records.jsonl",
                "events.jsonl",
                "private_events.jsonl",
            )
        }

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def setUp(self) -> None:
        self.manifest_path.write_bytes(self.original_manifest)
        self.result_path.write_bytes(self.original_result)
        for name, content in self.audit_artifacts.items():
            path = self.run_dir / name
            if os.path.lexists(path):
                path.unlink()
            path.write_bytes(content)
            path.chmod(0o600 if name != "events.jsonl" else 0o644)
        for path in self.run_dir.glob("*.symlink-target"):
            path.unlink()

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
        result = json.loads(self.result_path.read_text(encoding="utf-8"))
        counts = result["health"]["failure_union_counts"]
        self.assertEqual(sum(counts.values()), result["health"]["total_llm_orders"])
        self.assertEqual(
            sum(value for key, value in counts.items() if key != "valid_decisions"),
            result["health"]["bad_orders"],
        )

    def test_raw_audit_artifacts_are_required_and_registered(self) -> None:
        expected = expected_run_seed_identity(self.command)
        self.assertTrue(
            {
                "llm_records.jsonl",
                "events.jsonl",
                "private_events.jsonl",
            }
            <= set(expected.required_artifacts)
        )
        for name in ("llm_records.jsonl", "events.jsonl", "private_events.jsonl"):
            with self.subTest(deleted=name):
                self.setUp()
                (self.run_dir / name).unlink()
                decision = self._decision()
                self.assertFalse(decision.reusable)
                self.assertIn(ARTIFACT_MISSING, decision.reason_codes)
            with self.subTest(unregistered=name):
                self.setUp()
                manifest = json.loads(self.original_manifest)
                manifest["results"] = [
                    item for item in manifest["results"] if item["path"] != name
                ]
                self.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                decision = self._decision()
                self.assertFalse(decision.reusable)
                self.assertIn(ARTIFACT_MISSING, decision.reason_codes)

    def test_private_artifact_mode_and_symlink_are_rejected(self) -> None:
        for name in ("llm_records.jsonl", "private_events.jsonl"):
            with self.subTest(mode=name):
                self.setUp()
                (self.run_dir / name).chmod(0o644)
                decision = self._decision()
                self.assertFalse(decision.reusable)
                self.assertIn(ARTIFACT_INVALID, decision.reason_codes)
            with self.subTest(symlink=name):
                self.setUp()
                path = self.run_dir / name
                target = self.run_dir / (name + ".symlink-target")
                target.write_bytes(path.read_bytes())
                target.chmod(0o600)
                path.unlink()
                path.symlink_to(target.name)
                decision = self._decision()
                self.assertFalse(decision.reusable)
                self.assertIn(ARTIFACT_INVALID, decision.reason_codes)

    def test_runtime_environment_version_drift_changes_series_and_rejects_reuse(self) -> None:
        expected = expected_run_seed_identity(self.command)
        from nmsim.multi_event import build_attempt_series_id

        original_series = build_attempt_series_id(expected.experiment_slot, expected)
        drifted = replace(
            expected,
            scientific_runtime_environment_identity="f" * 64,
        )
        self.assertNotEqual(
            original_series,
            build_attempt_series_id(expected.experiment_slot, drifted),
        )
        manifest = json.loads(self.original_manifest)
        runtime = manifest["environment"]["scientific_runtime_environment"]
        runtime["dependencies"]["httpx"] = "999.0"
        manifest["environment"]["scientific_runtime_environment_identity"] = hashlib.sha256(
            json.dumps(runtime, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        ).hexdigest()
        self.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        decision = self._decision()
        self.assertFalse(decision.reusable)
        self.assertIn(RUNTIME_ENVIRONMENT_MISMATCH, decision.reason_codes)

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

    def test_health_union_must_exactly_match_bad_orders(self) -> None:
        manifest = json.loads(self.original_manifest)
        result = json.loads(self.original_result)
        counts = result["health"]["failure_union_counts"]
        self.assertGreater(counts["valid_decisions"], 0)
        counts["valid_decisions"] -= 1
        counts["strict_schema_invalid"] += 1
        self.result_path.write_text(json.dumps(result), encoding="utf-8")
        self._refresh_result_descriptor(manifest)
        decision = self._decision()
        self.assertFalse(decision.reusable)
        self.assertIn(HEALTH_GATE_REJECTED, decision.reason_codes)

    def test_health_must_close_against_manifest_completion_counts(self) -> None:
        manifest = json.loads(self.original_manifest)
        result = json.loads(self.original_result)
        health = result["health"]
        counts = health["failure_union_counts"]
        self.assertGreater(counts["valid_decisions"], 0)
        counts["valid_decisions"] -= 1
        counts["strict_schema_invalid"] += 1
        health["bad_orders"] = 1
        health["bad_frac"] = round(1 / health["total_llm_orders"], 4)
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

    def test_mixed_valid_and_invalid_alias_evidence_is_rejected(self) -> None:
        manifest = json.loads(self.original_manifest)
        result = json.loads(self.original_result)
        manifest["completion"]["application_provider_attempts"][
            "reported_models"
        ] = ["HiggsAI"]
        manifest["completion"]["application_provider_attempts"][
            "invalid_reported_model_alias_count"
        ] = 1
        manifest["multi_event"]["reported_model_aliases"] = ["HiggsAI"]
        manifest["multi_event"]["invalid_reported_model_alias_count"] = 1
        result["reported_model_aliases"] = ["HiggsAI"]
        result["invalid_reported_model_alias_count"] = 1
        self.result_path.write_text(json.dumps(result), encoding="utf-8")
        self._refresh_result_descriptor(manifest)
        decision = self._decision()
        self.assertFalse(decision.reusable)
        self.assertIn(REPORTED_MODEL_GATE_REJECTED, decision.reason_codes)


if __name__ == "__main__":
    unittest.main()
