"""Offline acceptance tests for the managed persona-flip diagnostic."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from experiments import persona_flip_test as flip
from nmsim.persona_variables import REFERENCE_THETA, VARIABLE_REGISTRY


HASH_RE = r"^[0-9a-f]{64}$"
PRIVATE_MARKERS = (
    '"theta":',
    '"rendered_persona":',
    '"system_prompt":',
    '"user_prompt":',
    '"raw_response":',
    '"private_rationale":',
    "private strict-null orchestration rationale",
)


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _single_run(out_root: Path) -> Path:
    manifests = list((out_root / "runs").glob("*/run_manifest.json"))
    if len(manifests) != 1:
        raise AssertionError("expected one managed run, got {}".format(len(manifests)))
    return manifests[0].parent


class PersonaFlipPlanTests(unittest.TestCase):
    def test_frozen_bundle_protocol_and_default_plan_hashes(self):
        bundle = flip.load_persona_fixture_bundle()
        self.assertEqual(
            bundle["bundle_hash"],
            flip.EXPECTED_PERSONA_FIXTURE_BUNDLE_HASH,
        )
        plan = flip.load_study_plan()
        self.assertEqual(plan.study_plan_hash, flip.EXPECTED_DEFAULT_STUDY_PLAN_HASH)
        self.assertRegex(plan.study_plan_hash, HASH_RE)
        self.assertEqual(
            plan.public_plan["persona_protocol_sha256"],
            flip.EXPECTED_PERSONA_PROTOCOL_SHA256,
        )
        self.assertEqual(plan.public_plan["variable_count"], 14)
        self.assertEqual(plan.public_plan["k_per_arm"], 30)
        self.assertEqual(plan.public_plan["planned_requests"], 14 * 2 * 30)
        self.assertEqual(tuple(case.variable_id for case in plan.cases), tuple(VARIABLE_REGISTRY))

    def test_every_case_flips_only_one_variable_except_frozen_b2_conditioning(self):
        plan = flip.load_study_plan(k=2)
        for case in plan.cases:
            low = dict(case.low.theta)
            high = dict(case.high.theta)
            changed = {
                variable_id
                for variable_id in VARIABLE_REGISTRY
                if low[variable_id] != high[variable_id]
            }
            self.assertEqual(changed, {case.variable_id})
            for arm in (case.low, case.high):
                for variable_id in VARIABLE_REGISTRY:
                    if variable_id == case.variable_id:
                        continue
                    self.assertEqual(
                        arm.theta[variable_id],
                        REFERENCE_THETA[variable_id],
                    )

        by_id = {case.variable_id: case for case in plan.cases}
        self.assertEqual(
            (
                by_id["B1"].low.concrete_fixture_id,
                by_id["B1"].high.concrete_fixture_id,
            ),
            ("belief_source_research", "belief_source_peers"),
        )
        self.assertEqual(
            {
                by_id["B2"].low.concrete_fixture_id,
                by_id["B2"].high.concrete_fixture_id,
            },
            {"belief_source_research"},
        )
        self.assertEqual(
            by_id["B2"].low.baseline_overrides,
            {},
        )
        self.assertIn(
            "avoid_source_arm_confounding",
            plan.public_plan["b2_fixture_conditioning"]["reason"],
        )
        self.assertEqual(
            plan.public_plan["b2_fixture_conditioning"]["known_tension"],
            "the_concrete_fixture_mentions_research_while_reference_B1_is_trend",
        )
        self.assertEqual(
            {
                row["tension_code"]
                for row in plan.public_plan["known_prompt_fixture_tensions"]
            },
            {
                "render_flat_but_fixture_deep_loss",
                "render_private_but_fixture_public_recommendation",
            },
        )

    def test_requests_are_stable_balanced_and_pair_low_high(self):
        plan = flip.load_study_plan(k=3, concurrency=2)
        first = flip.build_requests(plan)
        second = flip.build_requests(plan)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 14 * 2 * 3)
        self.assertEqual(
            [request.attempt_order for request in first],
            list(range(len(first))),
        )
        for case in plan.cases:
            selected = [
                request
                for request in first
                if request.variable_id == case.variable_id
            ]
            self.assertEqual(
                [(row.repeat_index, row.arm) for row in selected],
                [
                    (repeat_index, arm)
                    for repeat_index in range(3)
                    for arm in ("low", "high")
                ],
            )
            for repeat_index in range(3):
                pair = [
                    row for row in selected if row.repeat_index == repeat_index
                ]
                self.assertEqual(len({row.pair_id for row in pair}), 1)
                self.assertEqual(len({row.request_id for row in pair}), 2)

    def test_invalid_grid_values_fail_closed(self):
        for kwargs in (
            {"k": 0},
            {"concurrency": 0},
            {"temperature": -0.1},
            {"temperature": float("nan")},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(flip.PersonaFlipError):
                    flip.load_study_plan(**kwargs)


class PersonaFlipStatisticsTests(unittest.TestCase):
    def test_bootstrap_is_deterministic_percentile_high_minus_low(self):
        first = flip.bootstrap_delta_ci(
            [0.0] * 10,
            [1.0] * 10,
            seed=17,
        )
        second = flip.bootstrap_delta_ci(
            [0.0] * 10,
            [1.0] * 10,
            seed=17,
        )
        self.assertEqual(first, second)
        self.assertEqual(first["replicates"], 2000)
        self.assertEqual(first["confidence"], 0.95)
        self.assertEqual((first["low"], first["high"]), (1.0, 1.0))
        self.assertIsNone(flip.bootstrap_delta_ci([], [1.0], seed=1))

    def test_preregistered_threshold_direction_and_a1_open_direction(self):
        plan = flip.load_study_plan(k=20)
        rows = []
        order = 0
        for variable_id in ("A1", "A2"):
            for repeat in range(20):
                for arm, action in (("low", "hold"), ("high", "sell")):
                    rows.append(
                        {
                            "attempt_order": order,
                            "variable_id": variable_id,
                            "arm": arm,
                            "status": "completed",
                            "parse_failed": False,
                            "action": action,
                            "sentiment": -1.0 if action == "sell" else 0.0,
                            "order": {
                                "signed_quantity": -2 if action == "sell" else 0
                            },
                        }
                    )
                    order += 1
        aggregate = flip.aggregate_results(rows, plan)
        by_id = {
            row["variable_id"]: row for row in aggregate["variable_results"]
        }
        for variable_id in ("A1", "A2"):
            result = by_id[variable_id]
            self.assertEqual(
                result["displacements"]["p_sell"]["delta_high_minus_low"],
                1.0,
            )
            self.assertEqual(
                result["effectiveness"]["preclassification"],
                "effective",
            )
        self.assertTrue(by_id["A1"]["effectiveness"]["direction_open"])
        self.assertFalse(by_id["A2"]["effectiveness"]["direction_open"])

        # A3 predicts a negative delta, so the same positive displacement fails
        # the direction gate even though magnitude and CI pass.
        a3_rows = [
            {
                "variable_id": "A3",
                "arm": arm,
                "status": "completed",
                "parse_failed": False,
                "action": action,
                "sentiment": 0.0,
                "order": {"signed_quantity": 0},
            }
            for _ in range(20)
            for arm, action in (("low", "hold"), ("high", "sell"))
        ]
        a3 = {
            row["variable_id"]: row
            for row in flip.aggregate_results(a3_rows, plan)["variable_results"]
        }["A3"]
        self.assertFalse(a3["effectiveness"]["direction_matches"])
        self.assertEqual(
            a3["effectiveness"]["preclassification"],
            "idle_candidate",
        )

    def test_incomplete_parse_valid_honest_n_is_not_evaluable(self):
        plan = flip.load_study_plan(k=30)
        rows = [
            {
                "variable_id": "A2",
                "arm": arm,
                "status": "completed",
                "parse_failed": False,
                "action": action,
                "sentiment": -1.0 if action == "sell" else 0.0,
                "order": {
                    "signed_quantity": -1 if action == "sell" else 0
                },
            }
            for arm, action in (("low", "hold"), ("high", "sell"))
        ]
        result = {
            row["variable_id"]: row
            for row in flip.aggregate_results(rows, plan)["variable_results"]
        }["A2"]
        self.assertEqual(
            result["displacements"]["p_sell"]["delta_high_minus_low"],
            1.0,
        )
        self.assertTrue(result["effectiveness"]["magnitude_threshold_met"])
        self.assertTrue(result["effectiveness"]["ci_excludes_zero"])
        self.assertFalse(result["effectiveness"]["classification_eligible"])
        self.assertEqual(
            result["effectiveness"]["preclassification"],
            "not_evaluable",
        )
        self.assertEqual(
            result["effectiveness"]["preclassification_zh"],
            "不可判定",
        )


class PersonaFlipManagedCLITests(unittest.TestCase):
    def test_dry_run_constructs_no_provider_and_records_zero_honest_n(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            stdout = io.StringIO()
            with mock.patch.object(
                flip,
                "_build_provider",
                side_effect=AssertionError("dry-run constructed a provider"),
            ), redirect_stdout(stdout):
                flip.main(
                    [
                        "--provider",
                        "openai",
                        "--dry-run",
                        "--k",
                        "2",
                        "--out",
                        str(out),
                    ]
                )
            run_dir = _single_run(out)
            summary = _read_json(run_dir / "dry_run_summary.json")
            manifest = _read_json(run_dir / "run_manifest.json")
            self.assertTrue(summary["dry_run"])
            self.assertFalse(summary["network_access"])
            self.assertEqual(summary["provider_calls"], 0)
            self.assertEqual(summary["planned_requests"], 56)
            self.assertEqual(summary["honest_n_endpoint_responses"], 0)
            self.assertEqual(summary["honest_n_parsed_decisions"], 0)
            self.assertEqual(summary["honest_n_runs"], 0)
            self.assertEqual(manifest["status"], "finished")
            self.assertEqual(
                manifest["managed_context"]["run_kind"],
                "persona_flip_test",
            )
            self.assertFalse((run_dir / "persona_flip_samples.jsonl").exists())
            self.assertFalse(
                (run_dir / "private_persona_flip_records.jsonl").exists()
            )

    def test_openai_without_live_fails_before_provider_construction(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            stderr = io.StringIO()
            with mock.patch.object(
                flip,
                "_build_provider",
                side_effect=AssertionError("guard constructed a provider"),
            ), redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as caught:
                    flip.main(
                        [
                            "--provider",
                            "openai",
                            "--k",
                            "1",
                            "--out",
                            str(out),
                        ]
                    )
            self.assertEqual(caught.exception.code, 2)
            self.assertIn("requires explicit --live", stderr.getvalue())
            manifest = _read_json(_single_run(out) / "run_manifest.json")
            self.assertEqual(manifest["status"], "failed")
            self.assertFalse(manifest["persona_flip_test"]["network_access"])

    def test_fake_run_is_exact_null_with_private_mode_and_honest_n(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                flip.main(
                    [
                        "--provider",
                        "fake_test_provider",
                        "--k",
                        "2",
                        "--concurrency",
                        "4",
                        "--out",
                        str(out),
                    ]
                )
            run_dir = _single_run(out)
            summary = _read_json(run_dir / "persona_flip_summary.json")
            rows = _read_jsonl(run_dir / "persona_flip_samples.jsonl")
            private_path = run_dir / "private_persona_flip_records.jsonl"
            private_rows = _read_jsonl(private_path)
            manifest = _read_json(run_dir / "run_manifest.json")
            self.assertEqual(len(rows), 56)
            self.assertEqual(len(private_rows), 56)
            self.assertEqual(summary["honest_n_endpoint_responses"], 56)
            self.assertEqual(summary["honest_n_parsed_decisions"], 56)
            self.assertEqual(summary["honest_n_runs"], 0)
            self.assertFalse(summary["network_access"])
            self.assertEqual(manifest["status"], "finished")
            self.assertEqual(
                {
                    result["effectiveness"]["preclassification"]
                    for result in summary["variable_results"]
                },
                {"idle_candidate"},
            )
            self.assertEqual(
                {
                    result["displacements"]["p_sell"][
                        "delta_high_minus_low"
                    ]
                    for result in summary["variable_results"]
                },
                {0.0},
            )
            for pair_id in {row["pair_id"] for row in rows}:
                pair = [row for row in rows if row["pair_id"] == pair_id]
                self.assertEqual(len(pair), 2)
                self.assertEqual(
                    len({row["raw_response_sha256"] for row in pair}),
                    1,
                )
            self.assertEqual(
                stat.S_IMODE(private_path.stat().st_mode),
                0o600,
            )
            private_events_path = run_dir / "private_events.jsonl"
            self.assertEqual(
                stat.S_IMODE(private_events_path.stat().st_mode),
                0o600,
            )
            public_text = "\n".join(
                (run_dir / name).read_text(encoding="utf-8")
                for name in (
                    "run_manifest.json",
                    "events.jsonl",
                    "persona_flip_summary.json",
                    "persona_flip_samples.jsonl",
                )
            )
            for marker in PRIVATE_MARKERS:
                self.assertNotIn(marker, public_text)
            private_text = private_path.read_text(encoding="utf-8")
            for marker in PRIVATE_MARKERS:
                self.assertIn(marker, private_text)

    def test_mock_is_offline_null_control(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                flip.main(
                    [
                        "--provider",
                        "mock",
                        "--k",
                        "1",
                        "--out",
                        str(out),
                    ]
                )
            summary = _read_json(
                _single_run(out) / "persona_flip_summary.json"
            )
            self.assertEqual(summary["honest_n_endpoint_responses"], 28)
            self.assertEqual(summary["honest_n_parsed_decisions"], 28)
            self.assertFalse(summary["network_access"])
            self.assertIn("null controls", summary["interpretation"])

    def test_sensitive_provider_error_code_and_detail_stay_private(self):
        private_detail = "PRIVATE_PROVIDER_FAILURE_DETAIL"
        private_code = "PRIVATE CODE WITH SPACES"

        class SensitiveProviderError(RuntimeError):
            status_code = 400
            code = private_code

        class FailingProvider:
            model = "failing-test-provider"
            network_access = False

            def __init__(self):
                self.request_count = 0
                self.response_count = 0
                self.batch_sizes = []

            async def complete(self, *_args, **_kwargs):
                self.request_count += 1
                raise SensitiveProviderError(private_detail)

            async def aclose(self):
                return None

        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            with mock.patch.object(
                flip,
                "_build_provider",
                return_value=FailingProvider(),
            ), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                flip.main(
                    [
                        "--provider",
                        "fake_test_provider",
                        "--k",
                        "1",
                        "--out",
                        str(out),
                    ]
                )
            run_dir = _single_run(out)
            summary = _read_json(run_dir / "persona_flip_summary.json")
            self.assertEqual(summary["completion"]["attempted"], 28)
            self.assertEqual(summary["completion"]["endpoint_responses"], 0)
            self.assertEqual(summary["completion"]["transport_failures"], 28)
            public_text = "\n".join(
                (run_dir / name).read_text(encoding="utf-8")
                for name in (
                    "run_manifest.json",
                    "events.jsonl",
                    "persona_flip_summary.json",
                    "persona_flip_samples.jsonl",
                )
            )
            self.assertNotIn(private_detail, public_text)
            self.assertNotIn(private_code, public_text)
            rows = _read_jsonl(run_dir / "persona_flip_samples.jsonl")
            self.assertEqual(
                {row["provider_error_code"] for row in rows},
                {None},
            )
            self.assertEqual(
                {row["provider_error_code_redacted"] for row in rows},
                {True},
            )
            private_text = (
                run_dir / "private_persona_flip_records.jsonl"
            ).read_text(encoding="utf-8")
            self.assertIn(private_detail, private_text)
            self.assertIn(private_code, private_text)

    def test_exclusive_json_writer_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "result.json"
            flip._write_json_exclusive(path, {"first": True})
            with self.assertRaises(FileExistsError):
                flip._write_json_exclusive(path, {"second": True})
            self.assertEqual(_read_json(path), {"first": True})


if __name__ == "__main__":
    unittest.main()
