"""Synthetic numerical and managed-boundary tests for multi-event analysis."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import hashlib
import io
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from experiments import aggregate_multi_event as A


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "nmsim" / "reference_data" / "v1" / "catalog.json"


def _material(protocol, event_id):
    row = next(
        item for item in protocol["design"]["events"]
        if item["event_id"] == event_id
    )
    return A.load_multi_event_material(
        event_id=event_id,
        reference_csv=ROOT / row["reference_csv"],
        news_timeline_jsonl=ROOT / row["news_timeline"],
        protocol_path=A.PROTOCOL_PATH,
        catalog_path=CATALOG,
    )


def _event_input(protocol, event_id):
    material = _material(protocol, event_id)
    prices, shock_idx = A.V.load_reference(str(material.reference_csv))
    return A.EventInput(
        material=material,
        event_id=event_id,
        reference_csv=material.reference_csv,
        reference_csv_sha256=material.reference_hash,
        news_timeline=material.news_timeline_jsonl,
        news_timeline_sha256=material.timeline_hash,
        reference_prices=tuple(prices),
        reference_shock_idx=shock_idx,
        transformed_reference_log_path=tuple(
            material.transformed.norm_log_path
        ),
        reference_transform_sha256=material.reference_transform_sha256,
    )


def _path(event_id: str) -> tuple[float, ...]:
    if event_id == "meta_2022_02_crash_v1":
        return tuple(math.log(1.0 - 0.35 * index / 24) for index in range(25))
    if event_id == "spy_2020_03_covid_v_recovery_v1":
        values = [1.0 - 0.25 * index / 8 for index in range(9)]
        values.extend(0.75 + 0.25 * index / 16 for index in range(1, 17))
        return tuple(math.log(value) for value in values)
    return tuple(math.log(1.0 + 0.20 * index / 24) for index in range(25))


def _observations(protocol, *, remove=()):
    removed = set(remove)
    base = {
        "meta_2022_02_crash_v1": -0.30,
        "spy_2020_03_covid_v_recovery_v1": -0.20,
        "meta_2023_02_efficiency_jump_v1": 0.02,
    }
    observations = []
    for event in protocol["design"]["events"]:
        event_id = event["event_id"]
        for arm in A.ARMS:
            for seed in protocol["design"]["seeds"]:
                for repeat_idx in protocol["design"]["repeat_indices"]:
                    cell = (event_id, arm, seed, repeat_idx)
                    if cell in removed:
                        continue
                    drop = (
                        base[event_id]
                        + (0.02 if arm == "social_on" else 0.0)
                        + seed * 0.0005
                        + (repeat_idx - 2) * 0.002
                    )
                    observations.append(
                        A.Observation(
                            event_id,
                            arm,
                            seed,
                            repeat_idx,
                            f"run-{event_id}-{arm}-{seed}-{repeat_idx}",
                            drop,
                            drop,
                            0.01,
                            _path(event_id),
                            0.1,
                            0.05,
                            0.06,
                            0.0 if event_id.endswith("crash_v1") else 1.0,
                        )
                    )
    return observations


def _execution_plan(protocol, mode="openai_live"):
    if mode == "mock":
        return {
            "protocol_adherence": False,
            "execution_mode": "mock",
            "seeds": list(protocol["design"]["seeds"][:2]),
            "repeat_indices": list(protocol["design"]["repeat_indices"][:2]),
            "planned_runs": 24,
            "override_reason": "bounded N=2 K=2 engineering acceptance fixture",
        }
    return {
        "protocol_adherence": True,
        "execution_mode": "openai_live",
        "seeds": list(protocol["design"]["seeds"]),
        "repeat_indices": list(protocol["design"]["repeat_indices"]),
        "planned_runs": protocol["design"]["planned_runs"],
        "override_reason": None,
    }


def _observations_for_plan(protocol, plan):
    return [
        item
        for item in _observations(protocol)
        if item.seed in plan["seeds"]
        and item.repeat_idx in plan["repeat_indices"]
    ]


class AggregateMultiEventTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.protocol = A.load_protocol()

    def test_protocol_freezes_pilot_and_no_curve_fit_transform(self):
        self.assertEqual(self.protocol["schema_version"], "multi_event_protocol_v1")
        self.assertEqual(self.protocol["protocol_id"], "multi_event_distribution_v1")
        self.assertEqual(self.protocol["design"]["planned_runs"], 144)
        self.assertEqual(self.protocol["study_status"], "preregistered_variance_components_pilot")
        self.assertFalse(self.protocol["confirmatory"])
        self.assertEqual(
            self.protocol["effective_config_freeze"]["scientific"]["news_round"], 1
        )
        self.assertEqual(
            self.protocol["effective_config_freeze"]["scientific"]["news_text"], ""
        )
        self.assertEqual(
            self.protocol["reference_data_catalog"]["sha256"],
            "02dad9ff1d9c6c2aaf1ab9ad10665649680ad2fab00358e4d5baa70da3752166",
        )
        self.assertTrue(
            all(
                len(event["reference_csv_sha256"]) == 64
                and len(event["news_timeline_sha256"]) == 64
                for event in self.protocol["design"]["events"]
            )
        )
        self.assertIn(
            "no outcome-dependent",
            self.protocol["reference_phase_transform"]["no_curve_fit"],
        )

    def test_reference_transform_is_fixed_linear_full_horizon(self):
        prices = [100.0, 100.0 * math.exp(0.24), 100.0 * math.exp(0.48)]
        transformed = A.resample_reference_log_path(prices, 0, target_points=25)
        self.assertEqual(len(transformed), 25)
        self.assertAlmostEqual(transformed[0], 0.0)
        self.assertAlmostEqual(transformed[12], 0.24)
        self.assertAlmostEqual(transformed[-1], 0.48)
        first = A.reference_transform_identity("event-a", "a" * 64, transformed)
        second = A.reference_transform_identity("event-a", "a" * 64, transformed)
        changed = A.reference_transform_identity("event-b", "a" * 64, transformed)
        self.assertEqual(first, second)
        self.assertNotEqual(first, changed)

    def test_balanced_variance_component_formulas(self):
        components = A.balanced_variance_components(
            {
                1: {"social_off": [0.0, 1.0, 2.0], "social_on": [1.0, 2.0, 3.0]},
                2: {"social_off": [0.0, 1.0, 2.0], "social_on": [3.0, 4.0, 5.0]},
            },
            K=3,
        )
        self.assertAlmostEqual(
            components["within_repeat_variance"]["social_off"], 1.0
        )
        self.assertAlmostEqual(
            components["within_repeat_variance"]["social_on"], 1.0
        )
        self.assertAlmostEqual(components["repeat_noise_contribution"], 2.0 / 3.0)
        self.assertAlmostEqual(components["observed_seed_effect_variance"], 2.0)
        self.assertAlmostEqual(components["between_seed_effect_variance"], 4.0 / 3.0)

    def test_clustered_bootstrap_is_deterministic(self):
        values = {1: [0.1, 0.2], 2: [0.3, 0.4], 3: [-0.1, 0.0]}
        first = A.clustered_bootstrap_ci(
            values, B=1000, seed=20260722, alpha=0.05
        )
        second = A.clustered_bootstrap_ci(
            values, B=1000, seed=20260722, alpha=0.05
        )
        self.assertEqual(first, second)
        self.assertAlmostEqual(first["estimate"], 0.15)
        self.assertLessEqual(first["ci_low"], first["estimate"])
        self.assertGreaterEqual(first["ci_high"], first["estimate"])

    def test_missing_arm_or_repeat_excludes_only_that_event_seed(self):
        missing = {
            ("meta_2022_02_crash_v1", "social_on", 11, 3),
            ("spy_2020_03_covid_v_recovery_v1", "social_off", 13, 1),
        }
        summary = A.analyze_observations(
            _observations(self.protocol, remove=missing), protocol=self.protocol
        )
        honest = summary["honest_n"]
        self.assertEqual(
            honest["complete_seed_ids_by_event"]["meta_2022_02_crash_v1"],
            [13, 17, 19, 23, 29, 31, 37],
        )
        self.assertEqual(
            honest["complete_seed_ids_by_event"]["spy_2020_03_covid_v_recovery_v1"],
            [11, 17, 19, 23, 29, 31, 37],
        )
        self.assertEqual(
            honest["complete_seed_ids_by_event"]["meta_2023_02_efficiency_jump_v1"],
            [11, 13, 17, 19, 23, 29, 31, 37],
        )
        self.assertEqual(
            honest["cross_event_complete_seed_ids"], [17, 19, 23, 29, 31, 37]
        )
        self.assertEqual(honest["complete_event_seed_pairs"], 22)
        claims = summary["qualitative_claims"]
        self.assertFalse(claims["preregistered_realism_claim_eligible"])
        self.assertEqual(
            claims["realism_assessment_status"],
            "protocol_adherent_live_pilot_incomplete_descriptive_not_claim_eligible",
        )
        meta_off = summary["events"]["meta_2022_02_crash_v1"][
            "cell_distributions"
        ]["social_off"]
        self.assertEqual(
            meta_off["all_identity_health_accepted"][
                "drop_depth_recomputed_from_norm_log_path"
            ]["n"],
            24,
        )
        self.assertEqual(
            meta_off["primary_complete_case"][
                "drop_depth_recomputed_from_norm_log_path"
            ]["n"],
            21,
        )

    def test_zero_variance_cohen_d_is_none(self):
        self.assertIsNone(A.cohen_d_arm_means([2.0, 2.0], [1.0, 1.0]))
        self.assertIsNone(A.paired_cohen_dz([1.0, 1.0, 1.0]))

    def test_truncated_extra_or_nonzero_t0_paths_fail_closed(self):
        observations = _observations(self.protocol)
        for bad_path in (
            observations[0].norm_log_path[:-1],
            observations[0].norm_log_path + (0.0,),
            (0.01,) + observations[0].norm_log_path[1:],
        ):
            mutated = [replace(observations[0], norm_log_path=bad_path), *observations[1:]]
            with self.subTest(length=len(bad_path)), self.assertRaisesRegex(
                ValueError, "invalid 25-point path"
            ):
                A.analyze_observations(mutated, protocol=self.protocol)

    def test_path_depth_is_primary_and_rounded_result_is_only_audited(self):
        path = (0.0,) + (math.log(1.0 - 0.123456),) * 24
        recomputed = A.recompute_drop_depth_from_path(
            path, reported_rounded=-0.1235
        )
        self.assertAlmostEqual(recomputed, -0.123456)
        with self.assertRaisesRegex(A.MultiEventInputError, "independently recomputed"):
            A.recompute_drop_depth_from_path(path, reported_rounded=-0.12)

    def test_reported_model_aliases_bind_pr8_attempt_evidence_selection_and_result(self):
        live_manifest = {
            "completion": {
                "application_provider_attempts": {
                    "reported_models": ["HiggsAI", "HiggsAI"],
                    "reported_models_truncated": False,
                }
            }
        }
        self.assertEqual(
            A.validate_reported_model_alias_binding(
                live_manifest,
                selected_aliases=["HiggsAI"],
                result_aliases=["HiggsAI"],
                execution_mode="openai_live",
            ),
            ["HiggsAI"],
        )
        with self.assertRaisesRegex(A.MultiEventInputError, "disagree"):
            A.validate_reported_model_alias_binding(
                live_manifest,
                selected_aliases=["MiniMax-M2.7"],
                result_aliases=["MiniMax-M2.7"],
                execution_mode="openai_live",
            )
        truncated = json.loads(json.dumps(live_manifest))
        truncated["completion"]["application_provider_attempts"][
            "reported_models_truncated"
        ] = True
        with self.assertRaisesRegex(A.MultiEventInputError, "truncated"):
            A.validate_reported_model_alias_binding(
                truncated,
                selected_aliases=["HiggsAI"],
                result_aliases=["HiggsAI"],
                execution_mode="openai_live",
            )
        mock_manifest = {
            "completion": {
                "application_provider_attempts": {
                    "reported_models": [],
                    "reported_models_truncated": False,
                }
            }
        }
        self.assertEqual(
            A.validate_reported_model_alias_binding(
                mock_manifest,
                selected_aliases=[],
                result_aliases=[],
                execution_mode="mock",
            ),
            [],
        )

    def test_mixed_reported_aliases_prohibit_model_specific_inference(self):
        mixed = A.model_identity_interpretation(
            {
                "execution_mode": "openai_live",
                "reported_model_aliases": ["HiggsAI", "MiniMax-M2.7"],
            }
        )
        self.assertFalse(mixed["model_specific_inference_allowed"])
        self.assertFalse(mixed["underlying_model_identity_verified"])
        self.assertFalse(mixed["reported_alias_homogeneous_pooling_allowed"])
        single = A.model_identity_interpretation(
            {
                "execution_mode": "openai_live",
                "reported_model_aliases": ["HiggsAI"],
            }
        )
        self.assertFalse(single["model_specific_inference_allowed"])
        self.assertFalse(single["underlying_model_identity_verified"])
        self.assertTrue(single["reported_alias_homogeneous_pooling_allowed"])
        self.assertEqual(
            single["attribution_scope"],
            "endpoint_condition_with_homogeneous_self_reported_alias",
        )

    def test_population_order_and_frozen_identity_are_independently_checked(self):
        scientific = self.protocol["effective_config_freeze"]["scientific"]
        event = _event_input(self.protocol, "meta_2022_02_crash_v1")
        expected_timeline = [
            {
                "event_id": item.event_id,
                "round": item.round,
                "public_text": item.public_text,
            }
            for item in event.material.transformed.news_timeline
        ]
        config = {
            field: (
                11
                if field == "seed"
                else False
                if field == "social_enabled"
                else expected_timeline
                if field == "news_timeline"
                else str(event.reference_csv)
                if field == "reference_path"
                else value
            )
            for field, value in scientific.items()
        }
        config.update(
            {
                "provider": "mock",
                "temperature": 0.3,
                "max_tokens": 1024,
                "cache_enabled": False,
            }
        )
        selection = A.ChildSelection(
            "meta_2022_02_crash_v1",
            "social_off",
            11,
            1,
            Path("run_manifest.json"),
            "a" * 64,
            "b" * 64,
            {},
        )
        self.assertEqual(
            A._config_mismatches(
                {"config": config},
                self.protocol,
                selection,
                event,
                execution_mode="mock",
            ),
            [],
        )
        reordered = dict(config)
        reordered["population"] = dict(reversed(list(config["population"].items())))
        self.assertNotIn(
            "config_mismatch:population",
            A._config_mismatches(
                {"config": reordered},
                self.protocol,
                selection,
                event,
                execution_mode="mock",
            ),
        )
        frozen_identity = A.expected_population_identity(self.protocol)
        changed_protocol = json.loads(json.dumps(self.protocol))
        changed_protocol["effective_config_freeze"]["scientific"]["population"] = (
            reordered["population"]
        )
        self.assertNotEqual(
            frozen_identity, A.expected_population_identity(changed_protocol)
        )

    def test_inclusive_qualitative_boundaries_and_finite_fail_closed(self):
        thresholds = self.protocol["qualitative_thresholds"]
        path = [0.0] * 24 + [math.log(0.95)]
        categories = A._qualitative_categories(
            drop_depth=-0.15,
            norm_log_path=path,
            recovery_fraction=0.5,
            thresholds=thresholds,
        )
        self.assertTrue(categories["crash"])
        self.assertTrue(categories["full_recovery"])
        jump_path = [0.0, math.log(1.10)] + [0.0] * 23
        self.assertTrue(
            A._qualitative_categories(
                drop_depth=0.0,
                norm_log_path=jump_path,
                recovery_fraction=None,
                thresholds=thresholds,
            )["positive_jump"]
        )
        failed = A._qualitative_categories(
            drop_depth=float("nan"),
            norm_log_path=path,
            recovery_fraction=None,
            thresholds=thresholds,
        )
        self.assertFalse(failed["finite"])
        self.assertIsNone(failed["crash"])

    def test_selection_builder_requires_exact_execution_plan_partition(self):
        slots = [
            {
                "event_id": event["event_id"],
                "arm": arm,
                "seed": seed,
                "repeat_idx": repeat_idx,
                "status": "missing",
                "reason_codes": ["not_executed_fixture"],
            }
            for event in self.protocol["design"]["events"]
            for arm in A.ARMS
            for seed in self.protocol["design"]["seeds"]
            for repeat_idx in self.protocol["design"]["repeat_indices"]
        ]
        built = A.build_selection_document(
            protocol=self.protocol,
            protocol_sha256="a" * 64,
            execution_plan=_execution_plan(self.protocol),
            events=[],
            catalog_inputs=[],
            study_model_identity={},
            planned_slots=slots,
            accepted_children=[],
            rejected_slots=slots,
        )
        self.assertEqual(len(built["missing_or_rejected_slots"]), 144)
        with self.assertRaisesRegex(A.MultiEventInputError, "partition every execution-plan"):
            A.build_selection_document(
                protocol=self.protocol,
                protocol_sha256="a" * 64,
                execution_plan=_execution_plan(self.protocol),
                events=[],
                catalog_inputs=[],
                study_model_identity={},
                planned_slots=slots,
                accepted_children=[],
                rejected_slots=slots[:-1],
            )

        mock_plan = _execution_plan(self.protocol, mode="mock")
        mock_slots = [
            {
                "event_id": event["event_id"],
                "arm": arm,
                "seed": seed,
                "repeat_idx": repeat_idx,
                "status": "missing",
                "reason_codes": ["bounded_mock_fixture"],
            }
            for event in self.protocol["design"]["events"]
            for arm in A.ARMS
            for seed in mock_plan["seeds"]
            for repeat_idx in mock_plan["repeat_indices"]
        ]
        mock_built = A.build_selection_document(
            protocol=self.protocol,
            protocol_sha256="a" * 64,
            execution_plan=mock_plan,
            events=[],
            catalog_inputs=[],
            study_model_identity={},
            planned_slots=mock_slots,
            accepted_children=[],
            rejected_slots=mock_slots,
        )
        self.assertEqual(len(mock_built["missing_or_rejected_slots"]), 24)
        self.assertFalse(mock_built["execution_plan"]["protocol_adherence"])

        accepted = {
            key: mock_slots[0][key]
            for key in ("event_id", "arm", "seed", "repeat_idx")
        }
        accepted.update(
            {
                "attempt_run_ids": ["failed-once", "accepted-run"],
                "accepted_run_id": "accepted-run",
                "identity": {"run_id": "accepted-run"},
            }
        )
        accepted_built = A.build_selection_document(
            protocol=self.protocol,
            protocol_sha256="a" * 64,
            execution_plan=mock_plan,
            events=[],
            catalog_inputs=[],
            study_model_identity={},
            planned_slots=mock_slots,
            accepted_children=[accepted],
            rejected_slots=mock_slots[1:],
        )
        self.assertEqual(accepted_built["children"][0]["accepted_run_id"], "accepted-run")
        broken_accepted = json.loads(json.dumps(accepted))
        broken_accepted["accepted_run_id"] = "different-run"
        with self.assertRaisesRegex(A.MultiEventInputError, "accepted_run_id"):
            A.build_selection_document(
                protocol=self.protocol,
                protocol_sha256="a" * 64,
                execution_plan=mock_plan,
                events=[],
                catalog_inputs=[],
                study_model_identity={},
                planned_slots=mock_slots,
                accepted_children=[broken_accepted],
                rejected_slots=mock_slots[1:],
            )

    def test_mock_subset_is_engineering_only_and_not_realism_claim_eligible(self):
        plan = _execution_plan(self.protocol, mode="mock")
        summary = A.analyze_observations(
            _observations_for_plan(self.protocol, plan),
            protocol=self.protocol,
            execution_plan=plan,
        )
        self.assertEqual(summary["design"]["planned_runs"], 24)
        self.assertEqual(summary["design"]["N"], 2)
        self.assertEqual(summary["design"]["K"], 2)
        claims = summary["qualitative_claims"]
        self.assertFalse(claims["protocol_adherent_realism_claim_allowed"])
        self.assertFalse(claims["preregistered_realism_claim_eligible"])
        self.assertEqual(
            claims["realism_assessment_status"],
            "engineering_only_nonadherent_or_mock_not_claim_eligible",
        )

    def test_outcome_aware_accepted_child_cannot_be_relabelled_missing(self):
        cell = ("meta_2022_02_crash_v1", "social_off", 11, 1)
        jobs = {
            cell: {
                "event_id": cell[0],
                "arm": cell[1],
                "seed": cell[2],
                "repeat_idx": cell[3],
                "slot": {"slot_id": "slot-1"},
                "allowed_attempt_run_ids": [
                    "attempt-1",
                    "attempt-2",
                    "attempt-3",
                    "attempt-4",
                    "attempt-5",
                ],
            }
        }
        selection = {
            "children": [],
            "missing_or_rejected_slots": [
                {
                    "event_id": cell[0],
                    "arm": cell[1],
                    "seed": cell[2],
                    "repeat_idx": cell[3],
                    "status": "missing",
                    "reason_codes": ["hand_edited_omission"],
                    "attempt_run_ids": [],
                }
            ],
        }
        ledger = [
            {
                "schema_version": "1.0",
                "event_id": cell[0],
                "arm": cell[1],
                "seed": cell[2],
                "repeat_idx": cell[3],
                "slot_id": "slot-1",
                "run_id": "attempt-1",
                "status": "launched",
                "source": "executed",
                "technical_retry_idx": 1,
                "reason_code": "child_process_launched",
            },
            {
                "schema_version": "1.0",
                "event_id": cell[0],
                "arm": cell[1],
                "seed": cell[2],
                "repeat_idx": cell[3],
                "slot_id": "slot-1",
                "run_id": "attempt-1",
                "status": "accepted",
                "source": "executed",
                "technical_retry_idx": 1,
                "reason_code": "identity_and_health_valid",
            },
        ]
        with self.assertRaisesRegex(
            A.MultiEventInputError, "disagrees with durable attempt ledger"
        ):
            A._validate_driver_attempt_ledger(
                ledger, jobs=jobs, selection=selection
            )

    def test_explicit_selection_paths_hashes_and_transform_are_validated(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            root = Path(temporary)
            child_root = root / "children"
            child_root.mkdir()
            materials = [
                _material(self.protocol, event["event_id"])
                for event in self.protocol["design"]["events"]
            ]
            events = [
                {
                    "event_id": material.event_id,
                    "reference_csv": {
                        "path": str(material.reference_csv.relative_to(ROOT)),
                        "sha256": material.reference_hash,
                    },
                    "news_timeline": {
                        "path": str(
                            material.news_timeline_jsonl.relative_to(ROOT)
                        ),
                        "sha256": material.timeline_hash,
                    },
                    "transformed_reference": {
                        "schema_version": "1.0",
                        "norm_log_path": list(
                            material.transformed.norm_log_path
                        ),
                        "sha256": material.reference_transform_sha256,
                    },
                }
                for material in materials
            ]
            plan = _execution_plan(self.protocol, mode="mock")
            slots = [
                {
                    "event_id": event["event_id"],
                    "arm": arm,
                    "seed": seed,
                    "repeat_idx": repeat_idx,
                    "status": "missing",
                    "reason_codes": ["not_executed_fixture"],
                }
                for event in self.protocol["design"]["events"]
                for arm in A.ARMS
                for seed in plan["seeds"]
                for repeat_idx in plan["repeat_indices"]
            ]
            model = {
                "execution_mode": "mock",
                "model_request_config_hash": "a" * 64,
                "requested_provider": "mock",
                "requested_model": None,
                "resolved_provider": "mock",
                "resolved_model": "mock-v1",
                "endpoint_identity": "b" * 64,
                "reported_model_aliases": [],
            }
            document = A.build_selection_document(
                protocol=self.protocol,
                protocol_sha256=A.protocol_sha256(),
                execution_plan=plan,
                events=events,
                catalog_inputs=[
                    {
                        "path": self.protocol[
                            "reference_data_catalog"
                        ]["path"],
                        "sha256": A.sha256_file(CATALOG),
                    }
                ],
                study_model_identity=model,
                planned_slots=slots,
                accepted_children=[],
                rejected_slots=slots,
            )
            selection_path = root / "multi_event_selection.json"
            selection_path.write_text(json.dumps(document), encoding="utf-8")
            prepared = A.prepare_selection(
                selection_path,
                protocol=self.protocol,
                protocol_path=A.PROTOCOL_PATH,
                child_root=child_root,
                reference_root=ROOT,
            )
            self.assertEqual(
                tuple(prepared.events),
                tuple(row["event_id"] for row in events),
            )
            self.assertTrue(
                all(
                    prepared.events[event_id].material.event_id == event_id
                    for event_id in prepared.events
                )
            )
            self.assertEqual(len(prepared.children), 0)
            self.assertEqual(
                len(prepared.declared_missing_or_rejected), 24
            )

            wrong_source = json.loads(json.dumps(document))
            wrong_source["events"][0]["reference_csv"]["path"] = (
                wrong_source["events"][1]["reference_csv"]["path"]
            )
            selection_path.write_text(
                json.dumps(wrong_source), encoding="utf-8"
            )
            with self.assertRaisesRegex(
                A.MultiEventInputError, "preregistered event source"
            ):
                A.prepare_selection(
                    selection_path,
                    protocol=self.protocol,
                    protocol_path=A.PROTOCOL_PATH,
                    child_root=child_root,
                    reference_root=ROOT,
                )

            wrong_transform = json.loads(json.dumps(document))
            wrong_transform["events"][0]["transformed_reference"][
                "norm_log_path"
            ][1] += 0.001
            selection_path.write_text(
                json.dumps(wrong_transform), encoding="utf-8"
            )
            with self.assertRaisesRegex(
                A.MultiEventInputError, "shared frozen transform"
            ):
                A.prepare_selection(
                    selection_path,
                    protocol=self.protocol,
                    protocol_path=A.PROTOCOL_PATH,
                    child_root=child_root,
                    reference_root=ROOT,
                )

            wrong_catalog = json.loads(json.dumps(document))
            wrong_catalog["catalog_inputs"][0]["sha256"] = "f" * 64
            selection_path.write_text(
                json.dumps(wrong_catalog), encoding="utf-8"
            )
            with self.assertRaisesRegex(
                A.MultiEventInputError, "frozen protocol source bytes"
            ):
                A.prepare_selection(
                    selection_path,
                    protocol=self.protocol,
                    protocol_path=A.PROTOCOL_PATH,
                    child_root=child_root,
                    reference_root=ROOT,
                )


    def test_three_panel_figure(self):
        summary = A.analyze_observations(
            _observations(self.protocol),
            protocol=self.protocol,
            reference_log_paths={
                event["event_id"]: _path(event["event_id"])
                for event in self.protocol["design"]["events"]
            },
        )
        figure = A.create_three_panel_figure(summary)
        self.addCleanup(lambda: __import__("matplotlib.pyplot").pyplot.close(figure))
        self.assertEqual(len(figure.axes), 3)
        for event in summary["events"].values():
            for arm in A.ARMS:
                band = event["trajectory_envelope"][arm]
                self.assertEqual(len(band["mean"]), 25)
                self.assertEqual(len(band["lower"]), 25)
                self.assertEqual(len(band["upper"]), 25)
                self.assertTrue(
                    all(
                        low - 1e-12 <= center <= high + 1e-12
                        for low, center, high in zip(
                            band["lower"], band["mean"], band["upper"]
                        )
                    )
                )
        self.assertFalse(summary["confirmatory"])
        self.assertFalse(
            summary["qualitative_claims"]["confirmatory_claims_allowed"]
        )
        self.assertTrue(
            summary["qualitative_claims"]["primary_realism_assessment_social_on"][
                "passes_all_preregistered_criteria"
            ]
        )
        self.assertTrue(
            summary["qualitative_claims"][
                "preregistered_realism_claim_eligible"
            ]
        )
        meta_realism = summary["events"]["meta_2022_02_crash_v1"][
            "qualitative_by_arm"
        ]["social_on"]
        self.assertNotAlmostEqual(
            meta_realism["trajectory_derived_drop_depth"],
            meta_realism["distributional_child_drop_depth_mean"],
        )

    def test_help_has_no_run_and_confirmatory_request_is_managed_dry_failure(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            root = Path(temporary)
            out = root / "out"
            with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as caught:
                A.main(["--help"])
            self.assertEqual(caught.exception.code, 0)
            self.assertFalse((out / "runs").exists())

            stderr = io.StringIO()
            with mock.patch(
                "nmsim.llm.build_llm",
                side_effect=AssertionError("analysis must not construct a Provider"),
            ), redirect_stderr(stderr), self.assertRaises(SystemExit) as failed:
                A.main(
                    [
                        "--driver-manifest", "not-read-because-claim-gate.json",
                        "--child-root", str(root),
                        "--out", str(out),
                        "--require-confirmatory",
                    ]
                )
            self.assertEqual(failed.exception.code, 2)
            manifests = list((out / "runs").glob("*/run_manifest.json"))
            self.assertEqual(len(manifests), 1)
            manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual(manifest["failure_stage"], "config_validation")
            self.assertFalse(manifest["llm"]["runtime"]["network_access"])
            self.assertEqual(manifest["llm"]["runtime"]["provider_calls"], 0)

    def test_successful_cli_is_managed_provider_free_analysis(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            root = Path(temporary)
            out = root / "out"
            selection_path = root / "selection.json"
            selection_path.write_text("{}\n", encoding="utf-8")
            prepared = A.PreparedSelection(
                A.protocol_sha256(),
                selection_path,
                root,
                root,
                {},
                _execution_plan(self.protocol, mode="mock"),
                {"execution_mode": "mock", "reported_model_aliases": []},
                (),
                (),
                (),
                {
                    "protocol": A.PROTOCOL_PATH,
                    "analysis_selection": selection_path,
                },
                selection_path,
                "mock-driver-parent",
            )
            references = {
                event["event_id"]: _path(event["event_id"])
                for event in self.protocol["design"]["events"]
            }
            with mock.patch.object(A, "prepare_driver_selection", return_value=prepared), mock.patch.object(
                A,
                "validate_selected_children",
                return_value=(
                    _observations_for_plan(
                        self.protocol, _execution_plan(self.protocol, mode="mock")
                    ),
                    [],
                ),
            ), mock.patch.object(A, "_reference_log_paths", return_value=references), mock.patch(
                "nmsim.llm.build_llm",
                side_effect=AssertionError("analysis must not construct a Provider"),
            ), redirect_stdout(io.StringIO()):
                A.main(
                    [
                        "--driver-manifest", str(selection_path),
                        "--child-root", str(root),
                        "--reference-root", str(root),
                        "--out", str(out),
                    ]
                )
            manifests = list((out / "runs").glob("*/run_manifest.json"))
            self.assertEqual(len(manifests), 1)
            manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "finished")
            self.assertEqual(manifest["managed_context"]["run_kind"], "analysis")
            self.assertTrue(manifest["managed_run_completed"])
            self.assertTrue(manifest["outputs_complete"])
            self.assertEqual(manifest["analysis_completion"]["completed"], 1)
            self.assertFalse(manifest["llm"]["runtime"]["network_access"])
            self.assertEqual(manifest["llm"]["runtime"]["provider_calls"], 0)
            run_dir = manifests[0].parent
            self.assertTrue((run_dir / A.SUMMARY_FILENAME).is_file())
            self.assertTrue((run_dir / A.PLOT_FILENAME).is_file())
            self.assertTrue((out / A.SUMMARY_FILENAME).is_symlink())
            self.assertTrue((out / A.PLOT_FILENAME).is_symlink())


class ManagedParentAndChildIntegrationTests(unittest.TestCase):
    """Exercise the real mock driver/child artifacts through the analyzer."""

    @classmethod
    def setUpClass(cls):
        cls.protocol = A.load_protocol()
        cls.temporary = tempfile.TemporaryDirectory(dir="/tmp")
        cls.root = Path(cls.temporary.name)
        environment = {
            key: value
            for key, value in os.environ.items()
            if key
            not in {
                "LLM_PROVIDER",
                "LLM_MODEL",
                "OPENAI_BASE_URL",
                "OPENAI_API_KEY",
            }
        }
        command = [
            sys.executable,
            "-m",
            "experiments.multi_event",
            "--provider",
            "mock",
            "--n",
            "1",
            "--k",
            "1",
            "--workers",
            "1",
            "--out",
            str(cls.root),
            "--run-id",
            "analysis-parent-fixture",
        ]
        completed = subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
        )
        if completed.returncode:
            raise AssertionError(completed.stderr or completed.stdout)
        cls.parent_dir = cls.root / "runs" / "analysis-parent-fixture"
        cls.parent_manifest = cls.parent_dir / "run_manifest.json"
        cls.plan_path = cls.parent_dir / A.DRIVER_PLAN_FILENAME
        cls.selection_path = cls.parent_dir / A.DRIVER_SELECTION_FILENAME
        cls.private_parent_paths = (
            cls.parent_dir / A.DRIVER_PRIVATE_ATTEMPT_LEDGER_FILENAME,
            cls.parent_dir / A.DRIVER_PRIVATE_FAILURES_FILENAME,
        )
        selection = json.loads(cls.selection_path.read_text(encoding="utf-8"))
        cls.child_manifest_paths = [
            cls.root / item["manifest_path"]
            for item in selection["children"]
        ]
        cls.original_bytes = {
            path: path.read_bytes()
            for path in (
                cls.parent_manifest,
                cls.plan_path,
                cls.selection_path,
                *cls.private_parent_paths,
                *cls.child_manifest_paths,
                *(
                    path.parent / A.RESULT_ARTIFACT
                    for path in cls.child_manifest_paths
                ),
            )
        }

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        for path, content in self.original_bytes.items():
            if path.is_symlink():
                path.unlink()
            path.write_bytes(content)
        for path in self.private_parent_paths:
            path.chmod(0o600)

    @staticmethod
    def _write_json(path, value):
        path.write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _refresh_parent_descriptor(self, artifact_path):
        manifest = json.loads(
            self.parent_manifest.read_text(encoding="utf-8")
        )
        descriptor = next(
            item
            for item in manifest["results"]
            if item["path"] == artifact_path.name
        )
        content = artifact_path.read_bytes()
        descriptor["size_bytes"] = len(content)
        descriptor["sha256"] = hashlib.sha256(content).hexdigest()
        self._write_json(self.parent_manifest, manifest)

    def _prepare(self):
        return A.prepare_driver_selection(
            self.parent_manifest,
            protocol=self.protocol,
            protocol_path=A.PROTOCOL_PATH,
            child_root=self.root,
            reference_root=ROOT,
        )

    def test_finished_parent_and_real_mock_children_pass_full_gate(self):
        prepared = self._prepare()
        observations, rejections = A.validate_selected_children(
            prepared, self.protocol
        )
        self.assertEqual(len(prepared.children), 6)
        self.assertEqual(len(observations), 6)
        self.assertEqual(rejections, [])

    def test_registered_parent_artifact_byte_tamper_is_rejected(self):
        self.plan_path.write_bytes(self.plan_path.read_bytes() + b"\n")
        with self.assertRaisesRegex(
            A.MultiEventInputError,
            "registered driver artifact integrity mismatch",
        ):
            self._prepare()

    def test_private_parent_artifacts_reject_mode_and_symlink_tamper(self):
        private_path = self.private_parent_paths[0]
        private_path.chmod(0o644)
        with self.assertRaisesRegex(
            A.MultiEventInputError, "mode 0600"
        ):
            self._prepare()

        private_path.chmod(0o600)
        content = private_path.read_bytes()
        decoy = self.parent_dir / "private-ledger-decoy.jsonl"
        decoy.write_bytes(content)
        private_path.unlink()
        private_path.symlink_to(decoy.name)
        with self.assertRaisesRegex(
            A.MultiEventInputError, "cannot be a symlink"
        ):
            self._prepare()

    def test_self_consistent_plan_command_substitution_is_rejected(self):
        plan = json.loads(self.plan_path.read_text(encoding="utf-8"))
        plan["jobs"][0]["child_command"] = list(
            plan["jobs"][1]["child_command"]
        )
        self._write_json(self.plan_path, plan)
        self._refresh_parent_descriptor(self.plan_path)
        with self.assertRaisesRegex(
            A.MultiEventInputError,
            "canonical command/attempt identity",
        ):
            self._prepare()

    def test_parent_cannot_omit_a_durably_accepted_child(self):
        selection = json.loads(
            self.selection_path.read_text(encoding="utf-8")
        )
        accepted = selection["children"].pop(0)
        selection["missing_or_rejected_slots"].append(
            {
                "event_id": accepted["event_id"],
                "arm": accepted["arm"],
                "seed": accepted["seed"],
                "repeat_idx": accepted["repeat_idx"],
                "status": "missing",
                "reason_codes": ["hand_edited_outcome_omission"],
                "attempt_run_ids": [],
            }
        )
        self._write_json(self.selection_path, selection)
        self._refresh_parent_descriptor(self.selection_path)
        with self.assertRaisesRegex(
            A.MultiEventInputError,
            "durable attempt ledger",
        ):
            self._prepare()

    def test_old_scientific_fingerprint_tamper_fails_canonical_expected(self):
        child_manifest = self.child_manifest_paths[0]
        manifest = json.loads(child_manifest.read_text(encoding="utf-8"))
        manifest["scientific_component_fingerprint"] = "0" * 64
        manifest["simulation_core_source_hash"] = "1" * 64
        if isinstance(manifest.get("scientific_compatibility"), dict):
            manifest["scientific_compatibility"][
                "scientific_component_fingerprint"
            ] = "0" * 64
            manifest["scientific_compatibility"][
                "simulation_core_source_hash"
            ] = "1" * 64
        self._write_json(child_manifest, manifest)

        selection = json.loads(
            self.selection_path.read_text(encoding="utf-8")
        )
        selection["children"][0]["manifest_sha256"] = A.sha256_file(
            child_manifest
        )
        self._write_json(self.selection_path, selection)
        self._refresh_parent_descriptor(self.selection_path)
        prepared = self._prepare()
        observations, rejections = A.validate_selected_children(
            prepared, self.protocol
        )
        self.assertEqual(len(observations), 5)
        reasons = rejections[0]["reason_codes"]
        self.assertIn("scientific_fingerprint_mismatch", reasons)
        self.assertIn("simulation_core_mismatch", reasons)

    def test_selected_child_substitution_fails_reconstructed_cell_identity(self):
        prepared = self._prepare()
        first, second, *remaining = prepared.children
        substituted = replace(
            first,
            manifest_path=second.manifest_path,
            manifest_sha256=second.manifest_sha256,
            result_sha256=second.result_sha256,
            expected_identity=second.expected_identity,
        )
        mutated = replace(
            prepared,
            children=(substituted, second, *remaining),
        )
        observations, rejections = A.validate_selected_children(
            mutated, self.protocol
        )
        self.assertEqual(len(observations), 5)
        self.assertTrue(
            {
                "scientific_config_mismatch",
                "experiment_slot_mismatch",
                "result_identity_mismatch",
            }
            & set(rejections[0]["reason_codes"])
        )

    def test_health_fraction_must_equal_exact_counts(self):
        child_manifest = self.child_manifest_paths[0]
        result_path = child_manifest.parent / A.RESULT_ARTIFACT
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result["health"] = {
            "bad_orders": 1,
            "total_llm_orders": 10,
            "bad_frac": 0.0,
        }
        self._write_json(result_path, result)
        manifest = json.loads(child_manifest.read_text(encoding="utf-8"))
        descriptor = next(
            item
            for item in manifest["results"]
            if item["path"] == A.RESULT_ARTIFACT
        )
        result_bytes = result_path.read_bytes()
        descriptor["size_bytes"] = len(result_bytes)
        descriptor["sha256"] = hashlib.sha256(result_bytes).hexdigest()
        self._write_json(child_manifest, manifest)

        selection = json.loads(
            self.selection_path.read_text(encoding="utf-8")
        )
        selection["children"][0]["manifest_sha256"] = A.sha256_file(
            child_manifest
        )
        selection["children"][0]["result_artifact"][
            "sha256"
        ] = A.sha256_file(result_path)
        self._write_json(self.selection_path, selection)
        self._refresh_parent_descriptor(self.selection_path)
        prepared = self._prepare()
        observations, rejections = A.validate_selected_children(
            prepared, self.protocol
        )
        self.assertEqual(len(observations), 5)
        self.assertEqual(
            rejections[0]["reason_codes"],
            ["result_cell_contract_mismatch"],
        )


if __name__ == "__main__":
    unittest.main()
