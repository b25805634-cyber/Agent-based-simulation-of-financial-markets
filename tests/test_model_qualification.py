"""Phase 1.2A model-qualification protocol and offline managed CLI tests.

Every provider used here is in-process.  Any attempt to construct a production
provider or open a socket is treated as a test failure.
"""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import socket
import stat
import tempfile
import unittest
from unittest import mock

from experiments import model_qualification as qualification


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _single_run(out_root: Path) -> Path:
    manifests = sorted((out_root / "runs").glob("*/run_manifest.json"))
    if len(manifests) != 1:
        raise AssertionError("expected one managed run, found {}".format(len(manifests)))
    return manifests[0].parent


class QualificationProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bundle = qualification.load_protocol_bundle()
        cls.cases = qualification.build_cases(cls.bundle)

    def test_exact_six_by_eight_matrix_has_48_stable_unique_cases(self):
        self.assertEqual(len(self.bundle["protocol"]["persona_ids"]), 6)
        self.assertEqual(len(self.bundle["observations"]["fixtures"]), 8)
        self.assertEqual(len(self.cases), 48)
        self.assertEqual(len({case.case_id for case in self.cases}), 48)
        rebuilt = qualification.build_cases(qualification.load_protocol_bundle())
        self.assertEqual(
            [case.case_id for case in self.cases],
            [case.case_id for case in rebuilt],
        )

    def test_stable_subset_selection_is_independent_of_selector_order(self):
        first = qualification.select_cases(
            self.bundle,
            self.cases,
            provider_id="mock",
            fixture_ids=[
                "conflicting_neighbor_views",
                "negative_news_price_unchanged",
            ],
            persona_ids=["value_institution", "retail_crowd"],
            max_cases=3,
        )
        second = qualification.select_cases(
            self.bundle,
            list(reversed(self.cases)),
            provider_id="mock",
            fixture_ids=[
                "negative_news_price_unchanged",
                "conflicting_neighbor_views",
                "conflicting_neighbor_views",
            ],
            persona_ids=["retail_crowd", "value_institution"],
            max_cases=3,
        )
        self.assertEqual(
            [case.case_id for case in first.cases],
            [case.case_id for case in second.cases],
        )
        self.assertEqual(
            first.metadata["selection_hash"], second.metadata["selection_hash"]
        )
        self.assertEqual(first.metadata["selected_case_count"], 3)
        self.assertEqual(first.metadata["qualification_scope"], "subset_pilot")
        self.assertFalse(first.metadata["is_full_qualification"])

    def test_case_fixture_and_persona_filters_use_and_semantics(self):
        target = next(
            case
            for case in self.cases
            if case.persona_id == "retail_crowd"
            and case.fixture_id == "neutral_placebo_news"
        )
        selected = qualification.select_cases(
            self.bundle,
            self.cases,
            provider_id="mock",
            case_ids=[target.case_id],
            fixture_ids=[target.fixture_id],
            persona_ids=[target.persona_id],
        )
        self.assertEqual([case.case_id for case in selected.cases], [target.case_id])
        self.assertEqual(
            selected.metadata["selected_fixture_ids"], ["neutral_placebo_news"]
        )
        self.assertEqual(
            selected.metadata["selected_persona_ids"], ["retail_crowd"]
        )

    def test_codex_omitted_max_cases_defaults_to_one_but_mock_remains_48(self):
        codex = qualification.select_cases(
            self.bundle, self.cases, provider_id="codex_exec"
        )
        mock_selection = qualification.select_cases(
            self.bundle, self.cases, provider_id="mock"
        )
        self.assertEqual(len(codex.cases), 1)
        self.assertEqual(codex.metadata["effective_max_cases"], 1)
        self.assertTrue(codex.metadata["max_cases_defaulted"])
        self.assertEqual(len(mock_selection.cases), 48)
        self.assertTrue(mock_selection.metadata["is_full_qualification"])

    def test_selection_hash_binds_protocol_fixture_rubric_and_exact_cases(self):
        first = qualification.select_cases(
            self.bundle, self.cases, provider_id="mock", max_cases=1
        )
        second = qualification.select_cases(
            self.bundle, self.cases, provider_id="mock", max_cases=2
        )
        self.assertNotEqual(
            first.metadata["selection_hash"], second.metadata["selection_hash"]
        )
        for field in (
            "protocol_hash",
            "fixture_set_hash",
            "rubric_hash",
            "visibility_contract_hash",
        ):
            self.assertEqual(first.metadata[field], self.bundle[field])

    def test_unknown_selector_and_invalid_case_cap_fail_closed(self):
        with self.assertRaisesRegex(
            qualification.QualificationProtocolError, "unknown qualification persona_id"
        ):
            qualification.select_cases(
                self.bundle,
                self.cases,
                provider_id="mock",
                persona_ids=["not_a_persona"],
            )
        for value in (0, 49):
            with self.subTest(max_cases=value), self.assertRaisesRegex(
                qualification.QualificationProtocolError,
                "--max-cases must be between 1 and 48",
            ):
                qualification.select_cases(
                    self.bundle,
                    self.cases,
                    provider_id="mock",
                    max_cases=value,
                )

    def test_fixture_order_does_not_change_fixture_set_hash(self):
        fixtures = self.bundle["observations"]["fixtures"]
        self.assertEqual(
            qualification.fixture_set_hash(fixtures),
            qualification.fixture_set_hash(list(reversed(fixtures))),
        )

    def test_fixture_content_change_changes_fixture_set_hash(self):
        fixtures = copy.deepcopy(self.bundle["observations"]["fixtures"])
        original = qualification.fixture_set_hash(fixtures)
        fixtures[0]["cash"] += 1.0
        self.assertNotEqual(original, qualification.fixture_set_hash(fixtures))

    def test_rubric_content_change_changes_rubric_hash(self):
        rubric = copy.deepcopy(self.bundle["rubric"])
        original = qualification.stable_json_hash(rubric)
        rubric["not_scored_policy"] += " changed"
        self.assertNotEqual(original, qualification.stable_json_hash(rubric))

    def test_protocol_version_and_frozen_rubric_are_recordable(self):
        self.assertEqual(self.bundle["protocol"]["protocol_version"], "1.1")
        self.assertEqual(self.bundle["observations"]["protocol_version"], "1.0")
        self.assertEqual(self.bundle["rubric"]["rubric_version"], "1.1")
        self.assertEqual(
            self.bundle["visibility_contract"]["visibility_contract_version"],
            "1.0",
        )
        self.assertTrue(self.bundle["rubric"]["frozen_before_external_provider_calls"])
        for key in (
            "protocol_hash",
            "fixture_set_hash",
            "rubric_hash",
            "visibility_contract_hash",
        ):
            self.assertRegex(self.bundle[key], r"^[0-9a-f]{64}$")

    def test_fixture_payload_hash_is_unchanged_by_protocol_and_rubric_upgrade(self):
        self.assertEqual(
            self.bundle["fixture_set_hash"],
            "95109438f101ea1251520b3deb71fed9b96b097d2c9b89c1a6b73e16294aaf34",
        )

    def test_visibility_contract_covers_every_fixture_field(self):
        fields = {
            row["field"]: row
            for row in self.bundle["visibility_contract"]["fields"]
        }
        self.assertEqual(fields["fundamental_value"]["visible_to_model"], "mode_dependent")
        self.assertEqual(
            fields["fundamental_value"]["visibility_by_prompt_mode"],
            {"real": "never", "mock": "direct"},
        )
        self.assertFalse(fields["invisible_fields"]["allowed_for_scoring"])
        self.assertEqual(len(fields), 14)

    def test_visibility_claims_match_actual_real_and_mock_prompts(self):
        agents = qualification._agents_by_persona(7)
        for case in self.cases:
            fixture = case.fixture
            state = fixture["market_state"]
            for provider_id in ("fake_test_provider", "mock"):
                with self.subTest(
                    fixture=case.fixture_id,
                    persona=case.persona_id,
                    provider=provider_id,
                ):
                    system, user = qualification._build_prompt(
                        case, agents[case.persona_id], provider_id
                    )
                    self.assertIn("ROUND: {}".format(fixture["round"]), user)
                    self.assertIn("{:.2f}".format(state["latest_price"]), user)
                    for price in state["recent_prices"]:
                        self.assertIn("{:.2f}".format(price), user)
                    self.assertIn("{:.2f}".format(fixture["cash"]), user)
                    self.assertIn(str(fixture["shares"]), user)
                    self.assertIn(fixture["memory"][-1], user)
                    if fixture["visible_news"]:
                        self.assertIn(fixture["visible_news"], user)
                    if provider_id == "mock":
                        self.assertIn(
                            "FUNDAMENTAL: {:.2f}".format(
                                fixture["fundamental_value"]
                            ),
                            user,
                        )
                    else:
                        self.assertNotIn("FUNDAMENTAL:", user)
                        self.assertNotIn("fundamental_value", user)
                    self.assertNotIn(case.fixture_id, system + user)
                    self.assertNotIn(fixture["input_hash"], system + user)
                    for forbidden in self.bundle["visibility_contract"][
                        "forbidden_prompt_content"
                    ]:
                        self.assertNotIn(forbidden, system + user)

    def test_social_visibility_matches_prompt_variant_and_persona_gate(self):
        agents = qualification._agents_by_persona(7)
        panic = next(
            case
            for case in self.cases
            if case.persona_id == "retail_crowd"
            and case.fixture_id == "unanimous_neighbor_panic"
        )
        _system, real_user = qualification._build_prompt(
            panic, agents["retail_crowd"], "fake_test_provider"
        )
        _system, mock_user = qualification._build_prompt(
            panic, agents["retail_crowd"], "mock"
        )
        for item in panic.fixture["visible_social_feed"]:
            self.assertIn(item["public_take"], real_user)
            self.assertNotIn(item["public_take"], mock_user)
        self.assertIn("SOCIAL_SENTIMENT: -0.9000", mock_user)

        conflict = next(
            case
            for case in self.cases
            if case.persona_id == "retail_crowd"
            and case.fixture_id == "conflicting_neighbor_views"
        )
        _system, conflict_mock = qualification._build_prompt(
            conflict, agents["retail_crowd"], "mock"
        )
        self.assertRegex(conflict_mock, r"SOCIAL_SENTIMENT: -?0\.0000")
        for item in conflict.fixture["visible_social_feed"]:
            self.assertNotIn(item["public_take"], conflict_mock)

        quant = next(
            case
            for case in self.cases
            if case.persona_id == "quant_arb"
            and case.fixture_id == "unanimous_neighbor_panic"
        )
        _system, quant_real = qualification._build_prompt(
            quant, agents["quant_arb"], "fake_test_provider"
        )
        _system, quant_mock = qualification._build_prompt(
            quant, agents["quant_arb"], "mock"
        )
        self.assertIn("The floor is quiet", quant_real)
        self.assertNotIn("SOCIAL_SENTIMENT", quant_mock)
        for item in quant.fixture["visible_social_feed"]:
            self.assertNotIn(item["public_take"], quant_real + quant_mock)

    def test_rubric_cannot_score_a_model_invisible_dependency(self):
        rubric = copy.deepcopy(self.bundle["rubric"])
        metric = rubric["behavioral_diagnostics"]["fundamental_anchor_score"]
        metric["mode"] = "relative_diagnostic"
        metric.pop("reason", None)
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            rubric_path = Path(temporary) / "invalid-rubric.json"
            rubric_path.write_text(json.dumps(rubric), encoding="utf-8")
            with self.assertRaisesRegex(
                qualification.QualificationProtocolError,
                "model-invisible field fundamental_value",
            ):
                qualification.load_protocol_bundle(rubric_path=rubric_path)

    def test_fundamental_anchor_is_not_scored_but_keeps_raw_evidence(self):
        rows = []
        for case in self.cases:
            raw = json.dumps(
                {
                    "action": "buy" if case.persona_id == "value_institution" else "hold",
                    "quantity": 1 if case.persona_id == "value_institution" else 0,
                    "limit_price": case.fixture["market_state"]["latest_price"],
                    "sentiment": 0.5 if case.persona_id == "value_institution" else 0.0,
                    "public_take": "Public diagnostic response.",
                    "reasoning": "private",
                }
            )
            rows.append(qualification.evaluate_response(case, raw)[0])
        metric = qualification.aggregate_results(rows)["behavioral_diagnostics"][
            "fundamental_anchor_score"
        ]
        self.assertEqual(metric["status"], "not_scored")
        self.assertEqual(metric["reason"], "fundamental_anchor_not_visible")
        self.assertIsNone(metric["score"])
        self.assertEqual(metric["raw_evidence"]["value_institution"]["action"], "buy")

    def test_observations_exclude_private_future_and_expected_answer_payloads(self):
        forbidden = {
            "future_prices",
            "market_clearing_formula",
            "other_agent_private_rationale",
            "private_margin_reference_book",
            "expected_answer",
            "evaluation_rubric",
        }
        for fixture in self.bundle["observations"]["fixtures"]:
            with self.subTest(fixture=fixture["fixture_id"]):
                self.assertFalse(forbidden.intersection(fixture))
                self.assertTrue(forbidden.issubset(set(fixture["invisible_fields"])))
                self.assertEqual(
                    fixture["input_hash"], qualification.fixture_input_hash(fixture)
                )

    def test_missing_public_take_never_uses_private_text_as_public(self):
        raw = json.dumps(
            {
                "action": "buy",
                "quantity": 2,
                "limit_price": 100.0,
                "sentiment": 0.5,
                "reasoning": "PRIVATE_TEXT_MUST_STAY_PRIVATE",
            }
        )
        public, private = qualification.evaluate_response(self.cases[0], raw)
        self.assertEqual(public["public_take"], "")
        self.assertTrue(public["diagnostic_flags"]["missing_public_take"])
        self.assertNotIn("PRIVATE_TEXT_MUST_STAY_PRIVATE", json.dumps(public))
        self.assertIn("PRIVATE_TEXT_MUST_STAY_PRIVATE", json.dumps(private))

    def test_engineering_and_behavior_metrics_preserve_raw_distributions(self):
        raw_values = (
            json.dumps(
                {
                    "action": "buy",
                    "quantity": 1,
                    "limit_price": 100.0,
                    "sentiment": -0.5,
                    "public_take": "Buying despite concern.",
                    "reasoning": "private one",
                }
            ),
            json.dumps(
                {
                    "action": "hold",
                    "quantity": "not-an-integer",
                    "limit_price": 100.0,
                    "sentiment": 0.0,
                    "public_take": "Waiting.",
                    "reasoning": "private two",
                }
            ),
        )
        rows = [
            qualification.evaluate_response(case, raw)[0]
            for case, raw in zip(self.cases[:2], raw_values)
        ]
        metrics = qualification.aggregate_results(rows)
        engineering = metrics["engineering"]
        behavior = metrics["behavioral_diagnostics"]
        self.assertEqual(engineering["schema_success_rate"], 1.0)
        self.assertEqual(engineering["parse_success_rate"], 1.0)
        self.assertEqual(engineering["invalid_quantity_count"], 1)
        self.assertEqual(engineering["validation_failure_rate"], 0.5)
        self.assertEqual(behavior["sentiment_action_consistency"], 0.5)
        self.assertIn("raw_action_distributions", behavior)
        self.assertEqual(behavior["raw_action_distributions"]["global"]["buy"], 1)
        self.assertNotEqual(set(behavior), {"score"})


class QualificationManagedCLITests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)

    def test_help_creates_no_run(self):
        out = self.root / "help"
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as raised:
            qualification.main(["--help", "--out", str(out)])
        self.assertEqual(raised.exception.code, 0)
        self.assertFalse(out.exists())

    def test_codex_provider_and_static_identity_share_fake_executable_locator(self):
        fake_binary = str(self.root / "fake-codex")
        with mock.patch.dict(
            os.environ, {"NMSIM_CODEX_EXECUTABLE": fake_binary}
        ), mock.patch(
            "nmsim.codex_exec.CodexExecLLM"
        ) as provider_class, mock.patch(
            "nmsim.codex_exec.codex_static_adapter_identity",
            return_value={"provider": "codex_exec", "requested_model": "fake-model"},
        ) as static_identity:
            qualification._build_provider(
                "codex_exec", 7, "fake-model", reasoning_effort="low"
            )
            qualification._codex_static_identity("fake-model", "low")
        provider_class.assert_called_once_with(
            model="fake-model", reasoning_effort="low", binary=fake_binary
        )
        static_identity.assert_called_once_with(
            binary=fake_binary, model="fake-model", reasoning_effort="low"
        )

    def test_dry_run_never_constructs_provider_and_records_zero_calls(self):
        out = self.root / "dry"
        stdout = io.StringIO()
        with mock.patch.object(
            qualification,
            "_build_provider",
            side_effect=AssertionError("provider constructed during dry-run"),
        ) as build, mock.patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError("network attempted"),
        ) as network, redirect_stdout(stdout):
            qualification.main(
                ["--provider", "mock", "--dry-run", "--out", str(out)]
            )
        build.assert_not_called()
        network.assert_not_called()
        run_dir = _single_run(out)
        manifest = _read_json(run_dir / "run_manifest.json")
        summary = _read_json(run_dir / "dry_run_summary.json")
        self.assertEqual(summary["case_count"], 48)
        self.assertEqual(summary["persona_count"], 6)
        self.assertEqual(summary["fixture_count"], 8)
        self.assertEqual(summary["protocol_version"], "1.1")
        self.assertEqual(summary["observation_protocol_version"], "1.0")
        self.assertRegex(summary["visibility_contract_hash"], r"^[0-9a-f]{64}$")
        self.assertEqual(summary["provider_calls"], 0)
        self.assertFalse(summary["network_access"])
        self.assertEqual(manifest["completion"]["provider_calls"]["attempted"], 0)
        self.assertFalse(manifest["llm"]["runtime"]["network_access"])
        self.assertEqual(manifest["managed_context"]["run_kind"], "model_qualification")

    @staticmethod
    def _codex_capability_snapshot(*_args, **_kwargs):
        return {
            "capability_schema_version": "1.0",
            "provider": {
                "provider_id": "codex_exec",
                "external_network_expected": True,
                "experimental": True,
            },
        }

    @staticmethod
    def _codex_static_identity(_model, _reasoning_effort=None):
        return {
            "provider": "codex_exec",
            "requested_model": "fake-codex-model",
            "binary_identity": {
                "status": "available",
                "name": "fake-codex",
                "sha256": "a" * 64,
            },
            "codex_wrapper_protocol_version": "1.0",
            "wrapper_source_hash": "b" * 64,
            "decision_schema_version": "1.0",
            "decision_schema_hash": "c" * 64,
            "auth_probe_performed": False,
            "subprocess_started": False,
            "real_use_ready": False,
            "reasoning_effort": _reasoning_effort,
        }

    def test_codex_dry_run_defaults_to_one_and_never_constructs_provider(self):
        out = self.root / "codex-dry"
        with mock.patch.object(
            qualification,
            "_build_provider",
            side_effect=AssertionError("Codex provider constructed during dry-run"),
        ) as build, mock.patch.object(
            qualification,
            "provider_capability_snapshot",
            side_effect=self._codex_capability_snapshot,
        ), mock.patch.object(
            qualification,
            "_codex_static_identity",
            side_effect=self._codex_static_identity,
        ), mock.patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError("network attempted during dry-run"),
        ) as network, redirect_stdout(io.StringIO()):
            qualification.main(
                [
                    "--provider",
                    "codex_exec",
                    "--model",
                    "fake-codex-model",
                    "--reasoning-effort",
                    "low",
                    "--dry-run",
                    "--out",
                    str(out),
                ]
            )
        build.assert_not_called()
        network.assert_not_called()
        run_dir = _single_run(out)
        manifest = _read_json(run_dir / "run_manifest.json")
        summary = _read_json(run_dir / "dry_run_summary.json")
        self.assertEqual(summary["case_count"], 1)
        self.assertEqual(summary["estimated_logical_requests"], 1)
        self.assertTrue(summary["network_required"])
        self.assertFalse(summary["network_access"])
        self.assertEqual(summary["qualification_scope"], "subset_pilot")
        self.assertRegex(summary["selection_hash"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            manifest["qualification"]["selection_hash"], summary["selection_hash"]
        )
        self.assertFalse(
            summary["provider_static_identity"]["subprocess_started"]
        )
        self.assertEqual(manifest["completion"]["provider_calls"]["attempted"], 0)
        self.assertFalse(manifest["llm"]["runtime"]["network_access"])

    def test_codex_real_use_guard_missing_is_managed_failure_before_construction(self):
        out = self.root / "codex-unconfirmed"
        stderr = io.StringIO()
        with mock.patch.object(
            qualification,
            "_build_provider",
            side_effect=AssertionError("unconfirmed Codex provider constructed"),
        ) as build, mock.patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError("network attempted"),
        ) as network, redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            qualification.main(
                [
                    "--provider",
                    "codex_exec",
                    "--model",
                    "fake-codex-model",
                    "--reasoning-effort",
                    "low",
                    "--out",
                    str(out),
                ]
            )
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--confirm-real-codex-usage", stderr.getvalue())
        build.assert_not_called()
        network.assert_not_called()
        run_dir = _single_run(out)
        manifest = _read_json(run_dir / "run_manifest.json")
        events = (run_dir / "events.jsonl").read_text(encoding="utf-8")
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["failure_stage"], "provider_setup")
        self.assertEqual(manifest["qualification"]["case_count"], 1)
        self.assertEqual(manifest["completion"]["provider_calls"]["attempted"], 0)
        self.assertFalse(manifest["llm"]["runtime"]["network_access"])
        self.assertNotIn("RoundStarted", events)

    def test_codex_real_use_requires_explicit_reasoning_effort(self):
        out = self.root / "codex-missing-effort"
        stderr = io.StringIO()
        with mock.patch.object(
            qualification,
            "_build_provider",
            side_effect=AssertionError("effort-less Codex provider constructed"),
        ) as build, redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            qualification.main(
                [
                    "--provider",
                    "codex_exec",
                    "--model",
                    "fake-codex-model",
                    "--confirm-real-codex-usage",
                    "--max-cases",
                    "1",
                    "--out",
                    str(out),
                ]
            )
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--reasoning-effort", stderr.getvalue())
        build.assert_not_called()
        manifest = _read_json(_single_run(out) / "run_manifest.json")
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["failure_stage"], "provider_setup")
        self.assertEqual(manifest["completion"]["provider_calls"]["attempted"], 0)
        self.assertFalse(manifest["llm"]["runtime"]["network_access"])

    def test_codex_dry_run_can_report_missing_model_and_effort_without_provider(self):
        out = self.root / "codex-missing-model"
        with mock.patch.object(
            qualification,
            "_build_provider",
            side_effect=AssertionError("model-less Codex provider constructed"),
        ) as build, redirect_stdout(io.StringIO()):
            qualification.main(
                ["--provider", "codex_exec", "--dry-run", "--out", str(out)]
            )
        build.assert_not_called()
        run_dir = _single_run(out)
        manifest = _read_json(run_dir / "run_manifest.json")
        summary = _read_json(run_dir / "dry_run_summary.json")
        self.assertEqual(manifest["status"], "finished")
        self.assertEqual(manifest["completion"]["provider_calls"]["attempted"], 0)
        self.assertIsNone(summary["model_requested"])
        self.assertIsNone(summary["reasoning_effort_requested"])
        self.assertFalse(summary["provider_static_identity"]["real_use_ready"])

    def test_unsafe_codex_model_is_managed_config_validation_failure(self):
        out = self.root / "codex-unsafe-model"
        stderr = io.StringIO()
        with mock.patch.object(
            qualification,
            "_build_provider",
            side_effect=AssertionError("unsafe model constructed a provider"),
        ) as build, mock.patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError("network attempted"),
        ) as network, redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            qualification.main(
                [
                    "--provider",
                    "codex_exec",
                    "--model",
                    "unsafe model value",
                    "--dry-run",
                    "--out",
                    str(out),
                ]
            )
        self.assertEqual(raised.exception.code, 2)
        self.assertNotIn("unsafe model value", stderr.getvalue())
        build.assert_not_called()
        network.assert_not_called()
        run_dir = _single_run(out)
        manifest = _read_json(run_dir / "run_manifest.json")
        events = (run_dir / "events.jsonl").read_text(encoding="utf-8")
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["failure_stage"], "config_validation")
        self.assertEqual(manifest["completion"]["provider_calls"]["attempted"], 0)
        self.assertFalse(manifest["llm"]["runtime"]["network_access"])
        self.assertNotIn("RoundStarted", events)

    def test_codex_more_than_one_case_requires_exact_second_confirmation(self):
        for extra_args in (
            [],
            ["--confirm-case-count", "3"],
        ):
            with self.subTest(extra_args=extra_args):
                out = self.root / ("count-" + str(len(extra_args)))
                with mock.patch.object(
                    qualification,
                    "_build_provider",
                    side_effect=AssertionError("unconfirmed count constructed provider"),
                ) as build, redirect_stderr(io.StringIO()), self.assertRaises(
                    SystemExit
                ) as raised:
                    qualification.main(
                        [
                            "--provider",
                            "codex_exec",
                            "--model",
                            "fake-codex-model",
                            "--reasoning-effort",
                            "low",
                            "--confirm-real-codex-usage",
                            "--max-cases",
                            "2",
                            "--out",
                            str(out),
                            *extra_args,
                        ]
                    )
                self.assertEqual(raised.exception.code, 2)
                build.assert_not_called()
                manifest = _read_json(_single_run(out) / "run_manifest.json")
                self.assertEqual(manifest["status"], "failed")
                self.assertEqual(
                    manifest["completion"]["provider_calls"]["attempted"], 0
                )

    def test_codex_real_use_requires_explicit_max_cases_even_when_default_is_one(self):
        out = self.root / "codex-max-not-explicit"
        with mock.patch.object(
            qualification,
            "_build_provider",
            side_effect=AssertionError("Codex provider constructed without max-cases"),
        ) as build, redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            qualification.main(
                [
                    "--provider",
                    "codex_exec",
                    "--model",
                    "fake-codex-model",
                    "--reasoning-effort",
                    "low",
                    "--confirm-real-codex-usage",
                    "--out",
                    str(out),
                ]
            )
        build.assert_not_called()
        manifest = _read_json(_single_run(out) / "run_manifest.json")
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["failure_stage"], "provider_setup")
        self.assertEqual(manifest["completion"]["provider_calls"]["attempted"], 0)

    def test_codex_exact_guards_can_reach_only_a_patched_test_provider(self):
        out = self.root / "codex-guarded-test-double"

        class FakeCodexProvider(qualification.FakeTestProvider):
            model = "fake-codex-model"

            def __init__(self):
                super().__init__(seed=7)
                self.last_call_metadata = None
                self.identities = []
                self.usage_totals = {"input_tokens": 0, "output_tokens": 0}

            def identity_snapshot(self):
                return {
                    "provider": "codex_exec",
                    "requested_model": self.model,
                    "codex_cli_version": "fake-1.0",
                    "binary_identity": {"name": "fake-codex", "sha256": "a" * 64},
                    "auth_mode": "chatgpt_managed_codex",
                    "auth_verified": True,
                    "private_rationale": "MUST_NOT_LEAK",
                }

            def set_request_identity(self, *, run_id=None, agent_id=None):
                self.identities.append((run_id, agent_id))

            def complete(self, system, user):
                raw = super().complete(system, user)
                self.usage_totals["input_tokens"] += 3
                self.usage_totals["output_tokens"] += 2
                self.last_call_metadata = {
                    **self.identity_snapshot(),
                    "production_system_prompt_hash": "b" * 64,
                    "production_user_prompt_hash": "c" * 64,
                    "final_combined_input_hash": "d" * 64,
                    "final_response_hash": "e" * 64,
                    "reported_model": self.model,
                    "actual_model_verification": "verified",
                    "tool_use_violation_count": 0,
                    "usage": {"input_tokens": 3, "output_tokens": 2},
                    "raw_stdout": "MUST_NOT_LEAK",
                    "private_rationale": "MUST_NOT_LEAK",
                }
                return raw

        fake = FakeCodexProvider()
        with mock.patch.object(
            qualification, "_build_provider", return_value=fake
        ) as build, mock.patch.object(
            qualification,
            "provider_capability_snapshot",
            side_effect=self._codex_capability_snapshot,
        ), mock.patch.object(
            qualification,
            "_codex_static_identity",
            side_effect=self._codex_static_identity,
        ), mock.patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError("test double attempted network"),
        ) as network, redirect_stdout(io.StringIO()):
            qualification.main(
                [
                    "--provider",
                    "codex_exec",
                    "--model",
                    "fake-codex-model",
                    "--reasoning-effort",
                    "low",
                    "--confirm-real-codex-usage",
                    "--max-cases",
                    "2",
                    "--confirm-case-count",
                    "2",
                    "--workers",
                    "1",
                    "--out",
                    str(out),
                ]
            )
        build.assert_called_once_with(
            "codex_exec",
            7,
            "fake-codex-model",
            reasoning_effort="low",
        )
        network.assert_not_called()
        run_dir = _single_run(out)
        summary = _read_json(run_dir / "qualification_summary.json")
        manifest = _read_json(run_dir / "run_manifest.json")
        self.assertEqual(summary["honest_n_cases"], 2)
        self.assertEqual(summary["qualification_scope"], "subset_pilot")
        self.assertEqual(manifest["qualification"]["workers"], 1)
        self.assertTrue(manifest["qualification"]["real_codex_usage_confirmed"])
        self.assertEqual(manifest["qualification"]["qualification_cases"]["completed"], 2)
        self.assertEqual(len(fake.identities), 2)
        self.assertTrue(all(identity[0] == manifest["run_id"] for identity in fake.identities))
        self.assertEqual(len(manifest["qualification"]["provider_call_metadata"]), 2)
        public_text = "\n".join(
            (run_dir / filename).read_text(encoding="utf-8")
            for filename in (
                "run_manifest.json",
                "events.jsonl",
                "case_results.jsonl",
                "qualification_summary.json",
            )
        )
        self.assertNotIn("MUST_NOT_LEAK", public_text)
        public_rows = [
            json.loads(line)
            for line in (run_dir / "case_results.jsonl").read_text().splitlines()
        ]
        self.assertEqual(
            set(public_rows[0]["provider_identity"]),
            {
                "actual_model_verification",
                "final_response_hash",
                "reported_model",
                "tool_use_violation_count",
            },
        )
        private_rows = [
            json.loads(line)
            for line in (run_dir / "private_case_records.jsonl").read_text().splitlines()
        ]
        self.assertIn("production_system_prompt_hash", private_rows[0]["provider_call_metadata"])
        self.assertNotIn("raw_stdout", private_rows[0]["provider_call_metadata"])

    def test_codex_tool_use_failure_never_produces_a_decision(self):
        out = self.root / "codex-tool-use"

        class FakeToolUseError(RuntimeError):
            code = "tool_use_violation"

        class FakeToolUseProvider:
            model = "fake-codex-model"
            request_count = 0
            response_count = 0
            last_call_metadata = None

            def set_request_identity(self, **_identity):
                return None

            def complete(self, _system, _user):
                self.request_count += 1
                self.last_call_metadata = {
                    "provider": "codex_exec",
                    "requested_model": self.model,
                    "tool_use_violation_count": 1,
                    "event_type_counts": {"command_execution": 1},
                    "status": "failed",
                    "error_code": "tool_use_violation",
                    "private_rationale": "MUST_NOT_LEAK",
                }
                raise FakeToolUseError("safe test failure")

        with mock.patch.object(
            qualification, "_build_provider", return_value=FakeToolUseProvider()
        ), mock.patch.object(
            qualification,
            "provider_capability_snapshot",
            side_effect=self._codex_capability_snapshot,
        ), mock.patch.object(
            qualification,
            "_codex_static_identity",
            side_effect=self._codex_static_identity,
        ), redirect_stdout(io.StringIO()), redirect_stderr(
            io.StringIO()
        ), self.assertRaises(SystemExit) as raised:
            qualification.main(
                [
                    "--provider",
                    "codex_exec",
                    "--model",
                    "fake-codex-model",
                    "--reasoning-effort",
                    "low",
                    "--confirm-real-codex-usage",
                    "--max-cases",
                    "1",
                    "--out",
                    str(out),
                ]
            )
        self.assertEqual(raised.exception.code, 1)
        run_dir = _single_run(out)
        manifest = _read_json(run_dir / "run_manifest.json")
        events = (run_dir / "events.jsonl").read_text(encoding="utf-8")
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["failure_stage"], "provider_setup")
        self.assertEqual(
            manifest["qualification"]["failure"]["provider_error_code"],
            "tool_use_violation",
        )
        self.assertEqual(manifest["qualification"]["honest_n_cases"], 0)
        self.assertEqual(manifest["completion"]["provider_calls"]["attempted"], 1)
        self.assertEqual(manifest["completion"]["provider_calls"]["failed"], 1)
        self.assertNotIn("AgentDecisionParsed", events)
        self.assertNotIn("MUST_NOT_LEAK", events)
        self.assertIn("QualificationCaseFailed", events)
        self.assertIn("RunFailed", events)
        self.assertFalse((run_dir / "case_results.jsonl").exists())
        self.assertFalse((run_dir / "qualification_summary.json").exists())

    def test_workers_other_than_one_rejected_before_provider_construction(self):
        out = self.root / "workers-rejected"
        with mock.patch.object(
            qualification,
            "_build_provider",
            side_effect=AssertionError("provider constructed with workers > 1"),
        ) as build, redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            qualification.main(
                [
                    "--provider",
                    "codex_exec",
                    "--model",
                    "fake-codex-model",
                    "--workers",
                    "2",
                    "--out",
                    str(out),
                ]
            )
        build.assert_not_called()
        manifest = _read_json(_single_run(out) / "run_manifest.json")
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["completion"]["provider_calls"]["attempted"], 0)

    def test_cli_hard_case_cap_fails_during_managed_config_validation(self):
        out = self.root / "hard-cap-rejected"
        with mock.patch.object(
            qualification,
            "_build_provider",
            side_effect=AssertionError("provider constructed beyond hard case cap"),
        ) as build, redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            qualification.main(
                ["--provider", "mock", "--max-cases", "49", "--out", str(out)]
            )
        build.assert_not_called()
        manifest = _read_json(_single_run(out) / "run_manifest.json")
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["failure_stage"], "config_validation")
        self.assertEqual(manifest["completion"]["provider_calls"]["attempted"], 0)

    def test_cli_selection_is_recorded_without_constructing_dry_run_provider(self):
        out = self.root / "selected-dry"
        target = qualification.build_cases(qualification.load_protocol_bundle())[0]
        with mock.patch.object(
            qualification,
            "_build_provider",
            side_effect=AssertionError("dry-run constructed provider"),
        ) as build, redirect_stdout(io.StringIO()):
            qualification.main(
                [
                    "--provider",
                    "mock",
                    "--dry-run",
                    "--case-id",
                    target.case_id,
                    "--fixture-id",
                    target.fixture_id,
                    "--persona-id",
                    target.persona_id,
                    "--max-cases",
                    "1",
                    "--out",
                    str(out),
                ]
            )
        build.assert_not_called()
        summary = _read_json(_single_run(out) / "dry_run_summary.json")
        self.assertEqual(summary["case_count"], 1)
        self.assertEqual(summary["selection"]["selected_case_ids"], [target.case_id])
        self.assertEqual(summary["persona_count"], 1)
        self.assertEqual(summary["fixture_count"], 1)

    def test_external_provider_is_managed_rejection_before_provider_or_network(self):
        out = self.root / "external-rejected"
        stderr = io.StringIO()
        with mock.patch.object(
            qualification,
            "_build_provider",
            side_effect=AssertionError("external provider constructed"),
        ) as build, mock.patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError("network attempted"),
        ) as network, redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            qualification.main(["--provider", "openai", "--out", str(out)])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("forbids external provider", stderr.getvalue())
        build.assert_not_called()
        network.assert_not_called()
        manifest = _read_json(_single_run(out) / "run_manifest.json")
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["failure_stage"], "provider_setup")
        self.assertEqual(manifest["completion"]["provider_calls"]["attempted"], 0)
        self.assertFalse(manifest["llm"]["runtime"]["network_access"])

    def test_mock_qualification_completes_48_cases_without_simulation(self):
        out = self.root / "mock"
        with mock.patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError("network attempted"),
        ) as network, redirect_stdout(io.StringIO()):
            qualification.main(["--provider", "mock", "--out", str(out)])
        network.assert_not_called()
        run_dir = _single_run(out)
        manifest = _read_json(run_dir / "run_manifest.json")
        summary = _read_json(run_dir / "qualification_summary.json")
        cases = [
            json.loads(line)
            for line in (run_dir / "case_results.jsonl").read_text().splitlines()
        ]
        self.assertEqual(manifest["status"], "finished")
        self.assertEqual(manifest["managed_context"]["run_kind"], "model_qualification")
        self.assertEqual(manifest["qualification"]["qualification_cases"]["completed"], 48)
        self.assertEqual(manifest["qualification"]["honest_n_cases"], 48)
        self.assertEqual(manifest["honest_n_runs"], 0)
        self.assertEqual(manifest["completion"]["simulation_runs"]["planned"], 0)
        self.assertEqual(manifest["completion"]["simulation_runs"]["completed"], 0)
        self.assertEqual(manifest["completion"]["rounds"]["planned"], 0)
        self.assertEqual(manifest["completion"]["llm_logical_requests"]["completed"], 48)
        self.assertEqual(manifest["completion"]["agent_decisions"]["completed"], 48)
        self.assertEqual(manifest["completion"]["provider_calls"]["succeeded"], 48)
        self.assertEqual(manifest["completion"]["response_sources"]["provider"], 48)
        self.assertFalse(manifest["llm"]["runtime"]["network_access"])
        self.assertEqual(summary["honest_n_cases"], 48)
        self.assertEqual(summary["honest_n_runs"], 0)
        self.assertEqual(summary["output_schema_version"], "1.1")
        self.assertEqual(
            summary["metrics"]["behavioral_diagnostics"]["fundamental_anchor_score"]["status"],
            "not_scored",
        )
        self.assertEqual(len(cases), 48)
        self.assertFalse((run_dir / "price_path.csv").exists())
        self.assertFalse((run_dir / "sim_overview.png").exists())

    def test_public_private_outputs_are_separated_and_private_mode_is_0600(self):
        out = self.root / "privacy"
        with redirect_stdout(io.StringIO()):
            qualification.main(["--provider", "fake_test_provider", "--out", str(out)])
        run_dir = _single_run(out)
        public_paths = [
            run_dir / "run_manifest.json",
            run_dir / "events.jsonl",
            run_dir / "case_results.jsonl",
            run_dir / "qualification_summary.json",
        ]
        private_path = run_dir / "private_case_records.jsonl"
        private_text = private_path.read_text(encoding="utf-8")
        for path in public_paths:
            public_text = path.read_text(encoding="utf-8")
            self.assertNotIn("private deterministic test-double explanation", public_text)
            self.assertNotIn('"raw_response":', public_text)
            self.assertNotIn('"system_prompt":', public_text)
            self.assertNotIn('"user_prompt":', public_text)
        self.assertIn("private deterministic test-double explanation", private_text)
        self.assertIn("raw_response", private_text)
        self.assertEqual(stat.S_IMODE(private_path.stat().st_mode), 0o600)
        self.assertEqual(
            stat.S_IMODE((run_dir / "private_events.jsonl").stat().st_mode), 0o600
        )

    def test_public_summary_reports_distributions_not_only_total_score(self):
        out = self.root / "distribution"
        with redirect_stdout(io.StringIO()):
            qualification.main(["--provider", "mock", "--out", str(out)])
        summary = _read_json(_single_run(out) / "qualification_summary.json")
        behavior = summary["metrics"]["behavioral_diagnostics"]
        raw = behavior["raw_action_distributions"]
        self.assertEqual(len(raw["by_persona"]), 6)
        self.assertEqual(len(raw["by_fixture"]), 8)
        self.assertEqual(len(raw["persona_action_vectors"]), 6)
        self.assertTrue(all(len(vector) == 8 for vector in raw["persona_action_vectors"].values()))
        self.assertIn("persona_distinctiveness", behavior)
        self.assertIn("same_model_persona_collapse", behavior)


if __name__ == "__main__":
    unittest.main()
