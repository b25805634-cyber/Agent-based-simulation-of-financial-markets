"""Offline contract and managed-boundary tests for the 50 x 4 Teacher pilot.

No test in this module is authorized to contact a real Provider.  Guard and
dry-run cases make both Provider construction and socket access observable;
the only full acquisition uses the deterministic in-process Fake Teacher.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import re
import socket
import stat
import tempfile
import unittest
from unittest import mock

from experiments import information_weight_teacher as entrypoint
from nmsim import entrypoints
from nmsim import information_weight as contract


HASH_RE = re.compile(r"^[0-9a-f]{64}$")
FAKE_PRIVATE_MARKER = (
    "Deterministic engineering test-double response; not empirical evidence "
    "about people."
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
    manifests = sorted((out_root / "runs").glob("*/run_manifest.json"))
    if len(manifests) != 1:
        raise AssertionError(
            "expected exactly one managed run, found {}".format(len(manifests))
        )
    return manifests[0].parent


def _parsed_args(*values: str):
    args = entrypoint.build_argparser().parse_args(list(values))
    entrypoint._validate_args(args)
    return args


def _official_design():
    states = contract.generate_latent_states(
        count=entrypoint.FROZEN_LATENT_STATE_COUNT,
        seed=entrypoint.FROZEN_SEED,
        study_id=entrypoint.FROZEN_STUDY_ID,
    )
    plan = contract.build_sample_plan(
        states,
        seed=entrypoint.FROZEN_SEED,
        study_id=entrypoint.FROZEN_STUDY_ID,
    )
    return states, plan


def _live_argv(out: Path) -> list[str]:
    return [
        "--provider",
        "openai",
        "--model",
        entrypoint.FROZEN_MODEL,
        "--live",
        "--confirm-request-count",
        str(entrypoint.FROZEN_REQUEST_COUNT),
        "--out",
        str(out),
    ]


class _InterruptedLiveProvider:
    """One in-flight request is cancelled before any audit or response exists."""

    model = entrypoint.FROZEN_MODEL

    def __init__(self) -> None:
        self.request_count = 0
        self.response_count = 0
        self.application_attempt_count = 0
        self.provider_exception_attempts = 0
        self.retries_scheduled = 0
        self.logical_requests_with_retry = 0
        self.application_attempt_audits: list[dict] = []
        self.network_access = False
        self.batch_sizes: list[int] = []

    async def complete_many(
        self,
        prompts,
        *,
        before_attempt=None,
        on_application_attempt=None,
        on_completion=None,
        strict_sequential=False,
    ):
        del on_application_attempt, on_completion
        self.batch_sizes.append(len(prompts))
        if not strict_sequential:
            raise AssertionError("live information pilot lost strict ordering")
        if before_attempt is not None:
            before_attempt(0)
        self.request_count = 1
        self.application_attempt_count = 1
        self.network_access = True
        raise KeyboardInterrupt()

    async def aclose(self) -> None:
        return None


class _ScriptedLiveProvider:
    """Offline transport script covering retries and semantic failures."""

    model = entrypoint.FROZEN_MODEL

    def __init__(self, plan) -> None:
        self.plan = tuple(plan)
        self.request_count = 0
        self.response_count = 0
        self.application_attempt_count = 0
        self.provider_exception_attempts = 0
        self.retries_scheduled = 0
        self.logical_requests_with_retry = 0
        self.application_attempt_audits: list[dict] = []
        self.network_access = False
        self.batch_sizes: list[int] = []

    def _emit_attempt(
        self,
        index: int,
        attempt_index: int,
        *,
        response_received: bool,
        retry_scheduled: bool,
        callback,
    ) -> None:
        self.application_attempt_count += 1
        if response_received:
            self.response_count += 1
            status = "response_received"
            error_type = None
            error_detail = None
        else:
            self.provider_exception_attempts += 1
            status = "provider_exception"
            error_type = "SyntheticTransportError"
            error_detail = "synthetic offline transport failure"
        if retry_scheduled:
            self.retries_scheduled += 1
        audit = {
            "logical_request_index": index,
            "application_attempt_index": attempt_index,
            "application_max_attempts": entrypoint.FROZEN_APPLICATION_MAX_ATTEMPTS,
            "status": status,
            "provider_error_type": error_type,
            "provider_error_detail": error_detail,
            "retryable": not response_received,
            "retry_scheduled": retry_scheduled,
            "retry_delay_seconds": 0.0 if retry_scheduled else None,
        }
        self.application_attempt_audits.append(dict(audit))
        if callback is not None:
            callback(index, audit)

    async def complete_many(
        self,
        prompts,
        *,
        before_attempt=None,
        on_application_attempt=None,
        on_completion=None,
        strict_sequential=False,
    ):
        if not strict_sequential:
            raise AssertionError("live information pilot lost strict ordering")
        if len(prompts) != entrypoint.FROZEN_REQUEST_COUNT:
            raise AssertionError("script received a non-frozen request count")
        self.batch_sizes.append(len(prompts))
        completions = []
        for index, sample in enumerate(self.plan):
            if before_attempt is not None:
                before_attempt(index)
            self.request_count += 1
            self.network_access = True

            if index == 0:
                self.logical_requests_with_retry += 1
                self._emit_attempt(
                    index,
                    1,
                    response_received=False,
                    retry_scheduled=True,
                    callback=on_application_attempt,
                )
                self._emit_attempt(
                    index,
                    2,
                    response_received=True,
                    retry_scheduled=False,
                    callback=on_application_attempt,
                )
                application_attempt_count = 2
                raw = contract.fake_teacher_response(sample)
                reported_model = entrypoint.REQUIRED_REPORTED_MODEL
                finish_reason = entrypoint.REQUIRED_FINISH_REASON
                error_type = None
                error_detail = None
            elif index == 1:
                self.logical_requests_with_retry += 1
                for attempt_index in range(
                    1, entrypoint.FROZEN_APPLICATION_MAX_ATTEMPTS + 1
                ):
                    self._emit_attempt(
                        index,
                        attempt_index,
                        response_received=False,
                        retry_scheduled=(
                            attempt_index < entrypoint.FROZEN_APPLICATION_MAX_ATTEMPTS
                        ),
                        callback=on_application_attempt,
                    )
                application_attempt_count = entrypoint.FROZEN_APPLICATION_MAX_ATTEMPTS
                raw = None
                reported_model = None
                finish_reason = None
                error_type = "SyntheticTransportError"
                error_detail = "synthetic retries exhausted"
            else:
                self._emit_attempt(
                    index,
                    1,
                    response_received=True,
                    retry_scheduled=False,
                    callback=on_application_attempt,
                )
                application_attempt_count = 1
                raw = (
                    '{"action":'
                    if index == 4
                    else contract.fake_teacher_response(sample)
                )
                reported_model = (
                    "WrongAlias"
                    if index == 2
                    else entrypoint.REQUIRED_REPORTED_MODEL
                )
                finish_reason = (
                    "length"
                    if index == 3
                    else entrypoint.REQUIRED_FINISH_REASON
                )
                error_type = None
                error_detail = None

            completion = entrypoint.TeacherCompletion(
                raw_response=raw,
                reported_model=reported_model,
                input_tokens=10 if raw is not None else None,
                output_tokens=5 if raw is not None else None,
                response_id="scripted-{}".format(index),
                reported_model_raw=reported_model,
                finish_reason=finish_reason,
                finish_reason_raw=finish_reason,
                error_type=error_type,
                error_detail=error_detail,
                application_attempt_count=application_attempt_count,
                technical_retry_count=application_attempt_count - 1,
            )
            completions.append(completion)
            if on_completion is not None:
                on_completion(index, completion)
        return completions

    async def aclose(self) -> None:
        return None


class InformationWeightContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.states, cls.plan = _official_design()

    def test_frozen_50_by_four_design_and_exact_contract_identities(self):
        self.assertEqual(contract.LATENT_STATE_COUNT, 50)
        self.assertEqual(contract.PROFILE_COUNT, 4)
        self.assertEqual(contract.PLAN_COUNT, 200)
        self.assertEqual(len(self.states), 50)
        self.assertEqual(len(self.plan), 200)
        self.assertEqual(
            contract.CONTRACT_HASH,
            "9df50ce3bcd3aadc58b9edee8f74c5190f29f8c00167569ac171539defb465cf",
        )
        self.assertEqual(
            contract.plan_hash(self.plan),
            "774252b843ad63b873ce1fbc64f11ff7a9850acc9388f09a97ea1a29b20dd468",
        )
        identities = entrypoint.build_information_weight_identities(
            _parsed_args("--provider", "fake_test_teacher"),
            self.states,
            self.plan,
        )
        self.assertEqual(
            identities["state_design_hash"],
            "d5f25ea2e5eee8f6d548a61192de5916d0dac50de3dfb3982c898d46815dcd1d",
        )
        self.assertEqual(
            self.plan[0].sample_id,
            "469a981be35fa5f77878d13fa213ad382406d27c7347a13afa677ae67693684f",
        )
        self.assertEqual(len({item.latent_hash for item in self.states}), 50)
        self.assertEqual(len({item.view.view_hash for item in self.plan}), 200)
        self.assertEqual(len({item.prompt.prompt_hash for item in self.plan}), 200)
        self.assertEqual(len({item.sample_hash for item in self.plan}), 200)

    def test_exact_p8_f8_n8_account8_pools_ranges_and_profiles(self):
        self.assertEqual(
            contract.P8,
            (
                "return_1d",
                "return_5d",
                "return_20d",
                "realized_vol_20d",
                "drawdown_20d",
                "volume_z",
                "intraday_range_5d_mean",
                "turnover_change_5d",
            ),
        )
        self.assertEqual(
            contract.F8,
            (
                "earnings_yield_ttm",
                "revenue_growth_yoy",
                "debt_to_assets",
                "operating_margin_ttm",
                "return_on_assets_ttm",
                "operating_cashflow_to_assets",
                "book_to_market",
                "current_ratio",
            ),
        )
        self.assertEqual(
            contract.N8,
            (
                "signed_event_surprise",
                "affected_revenue_fraction",
                "age_20d_scaled",
                "official_source_mask",
                "confirmation_count_scaled",
                "scheduled_event_mask",
                "duration_20d_scaled",
                "source_disagreement",
            ),
        )
        self.assertEqual(
            contract.ACCOUNT8,
            (
                "position_fraction",
                "unrealized_return",
                "unrealized_return_mask",
                "days_since_trade_scaled",
                "days_since_trade_scaled_mask",
                "post_sale_return",
                "post_sale_return_mask",
                "log10_wealth",
            ),
        )
        self.assertEqual(len(set(contract.P8 + contract.F8 + contract.N8)), 24)
        self.assertEqual(
            dict(contract.PROFILE_FIELDS),
            {
                "price_volume_8_2_2": contract.P8 + contract.F8[:2] + contract.N8[:2],
                "fundamental_2_8_2": contract.P8[:2] + contract.F8 + contract.N8[:2],
                "news_2_2_8": contract.P8[:2] + contract.F8[:2] + contract.N8,
                "balanced_4_4_4": contract.P8[:4] + contract.F8[:4] + contract.N8[:4],
            },
        )
        self.assertEqual({len(value) for value in contract.PROFILE_FIELDS.values()}, {12})
        expected_extra_ranges = {
            "intraday_range_5d_mean": (0.0, 0.5),
            "turnover_change_5d": (-0.95, 5.0),
            "earnings_yield_ttm": (-1.0, 1.0),
            "revenue_growth_yoy": (-1.0, 5.0),
            "debt_to_assets": (0.0, 2.0),
            "operating_margin_ttm": (-2.0, 1.0),
            "return_on_assets_ttm": (-1.0, 1.0),
            "operating_cashflow_to_assets": (-1.0, 1.0),
            "book_to_market": (-2.0, 5.0),
            "current_ratio": (0.0, 10.0),
            "signed_event_surprise": (-1.0, 1.0),
            "affected_revenue_fraction": (0.0, 1.0),
            "age_20d_scaled": (0.0, 1.0),
            "official_source_mask": (0.0, 1.0),
            "confirmation_count_scaled": (0.0, 1.0),
            "scheduled_event_mask": (0.0, 1.0),
            "duration_20d_scaled": (0.0, 1.0),
            "source_disagreement": (0.0, 1.0),
        }
        self.assertEqual(dict(contract.EXTRA_FIELD_RANGES), expected_extra_ranges)

    def test_views_are_paired_on_one_latent_state_with_exact_visibility(self):
        groups: dict[int, list] = defaultdict(list)
        for sample in self.plan:
            groups[sample.group_index].append(sample)
            self.assertEqual(
                tuple(sample.view.visible_fields),
                contract.PROFILE_FIELDS[sample.view.profile_id],
            )
            self.assertEqual(len(sample.view.visible_fields), 12)
            self.assertEqual(tuple(sample.view.account_state), contract.ACCOUNT8)
            self.assertEqual(len(sample.view.account_state), 8)
            self.assertEqual(
                sample.view.to_prompt_payload()["observable_state"],
                dict(sample.view.visible_fields),
            )
        self.assertEqual(len(groups), 50)
        for state_index, samples in groups.items():
            with self.subTest(state_index=state_index):
                self.assertEqual(len(samples), 4)
                self.assertEqual(
                    {item.view.profile_id for item in samples},
                    set(contract.PROFILE_IDS),
                )
                self.assertEqual(
                    len({item.view.latent_state.latent_hash for item in samples}), 1
                )
                self.assertEqual(
                    len(
                        {
                            contract.canonical_json(item.view.account_state)
                            for item in samples
                        }
                    ),
                    1,
                )

    def test_profile_order_is_cyclic_and_nearly_balanced_in_every_slot(self):
        profile_counts = Counter(item.view.profile_id for item in self.plan)
        self.assertEqual(profile_counts, Counter({name: 50 for name in contract.PROFILE_IDS}))
        slots = {
            slot: Counter(
                item.view.profile_id
                for item in self.plan
                if item.within_group_index == slot
            )
            for slot in range(4)
        }
        for slot, counts in slots.items():
            with self.subTest(slot=slot):
                self.assertEqual(sum(counts.values()), 50)
                self.assertEqual(set(counts), set(contract.PROFILE_IDS))
                self.assertTrue(set(counts.values()).issubset({12, 13}))
        for group_index in range(50):
            expected = (
                contract.PROFILE_IDS[group_index % 4 :]
                + contract.PROFILE_IDS[: group_index % 4]
            )
            actual = tuple(
                item.view.profile_id
                for item in self.plan[group_index * 4 : (group_index + 1) * 4]
            )
            self.assertEqual(actual, expected)

    def test_prompt_is_identity_blind_and_contains_no_importance_instruction(self):
        forbidden = (
            "profile",
            "persona",
            "trader type",
            "price-volume trader",
            "fundamental trader",
            "news trader",
            "balanced trader",
            "importance",
            "important",
            "focus on",
            "prioritize",
            "pay attention",
            "weight",
            "量价交易者",
            "基本面交易者",
            "消息交易者",
            "侧重",
            "重点关注",
            "更重视",
        )
        for sample in self.plan:
            rendered = sample.prompt.system + "\n" + sample.prompt.user
            folded = rendered.casefold()
            for term in forbidden:
                self.assertNotIn(term.casefold(), folded)
            for profile_id in contract.PROFILE_IDS:
                self.assertNotIn(profile_id.casefold(), folded)
            payload = json.loads(sample.prompt.user)
            self.assertNotIn("profile_id", payload)
            self.assertNotIn("latent_id", payload)
            self.assertNotIn("view_hash", payload)
            self.assertEqual(len(payload["observable_state"]), 12)
            self.assertEqual(len(payload["account_state"]), 8)

    def test_k1_plan_and_fake_parser_have_no_pseudo_replicates(self):
        cells = Counter(
            (item.view.latent_state.latent_hash, item.view.profile_id)
            for item in self.plan
        )
        self.assertEqual(len(cells), 200)
        self.assertEqual(set(cells.values()), {1})
        raw_responses = [contract.fake_teacher_response(item) for item in self.plan]
        parsed = [
            contract.parse_teacher_response(raw, sample)
            for raw, sample in zip(raw_responses, self.plan)
        ]
        self.assertEqual(len(parsed), 200)
        self.assertEqual(
            {item.public.action for item in parsed}.issubset({"buy", "hold", "sell"}),
            True,
        )
        self.assertEqual(contract.fake_teacher_response(self.plan[0]), raw_responses[0])
        with self.assertRaises(ValueError):
            contract.parse_teacher_response(
                '{"action":"hold","intensity":0.5,"reasoning":"invalid"}',
                self.plan[0],
            )

    def test_binary_latin_hypercubes_are_balanced_and_all_fields_in_range(self):
        for state in self.states:
            for name, value in state.information_fields.items():
                low, high = contract.FIELD_RANGES[name]
                self.assertGreaterEqual(value, low)
                self.assertLessEqual(value, high)
        for name in contract.BINARY_FIELDS:
            values = [state.information_fields[name] for state in self.states]
            self.assertEqual(Counter(values), Counter({0: 25, 1: 25}))

    def test_named_identity_hashes_are_scoped_and_mutation_sensitive(self):
        fake_args = _parsed_args("--provider", "fake_test_teacher")
        baseline = entrypoint.build_information_weight_identities(
            fake_args, self.states, self.plan
        )
        for name in (
            "scientific_config_hash",
            "model_request_config_hash",
            "execution_config_hash",
            "full_effective_config_hash",
        ):
            self.assertRegex(baseline[name], HASH_RE)
        self.assertEqual(baseline["sample_plan_hash"], contract.plan_hash(self.plan))

        changed_states = contract.generate_latent_states(
            seed=20260825,
            study_id=entrypoint.FROZEN_STUDY_ID,
        )
        changed_plan = contract.build_sample_plan(
            changed_states,
            seed=20260825,
            study_id=entrypoint.FROZEN_STUDY_ID,
        )
        scientific_mutation = entrypoint.build_information_weight_identities(
            fake_args, changed_states, changed_plan
        )
        self.assertNotEqual(
            scientific_mutation["scientific_config_hash"],
            baseline["scientific_config_hash"],
        )
        self.assertEqual(
            scientific_mutation["model_request_config_hash"],
            baseline["model_request_config_hash"],
        )

        with mock.patch.object(entrypoint, "FROZEN_TEMPERATURE", 0.75):
            request_mutation = entrypoint.build_information_weight_identities(
                fake_args, self.states, self.plan
            )
        self.assertEqual(
            request_mutation["scientific_config_hash"],
            baseline["scientific_config_hash"],
        )
        self.assertNotEqual(
            request_mutation["model_request_config_hash"],
            baseline["model_request_config_hash"],
        )

        # The exact prompt is both a scientific manipulation and a wire-level
        # request property.  The two named identities intentionally cross-bind
        # it rather than claiming strict statistical orthogonality.
        with mock.patch.object(
            contract, "SYSTEM_PROMPT", contract.SYSTEM_PROMPT + " synthetic mutation"
        ):
            prompt_mutation = entrypoint.build_information_weight_identities(
                fake_args, self.states, self.plan
            )
        self.assertNotEqual(
            prompt_mutation["scientific_config_hash"],
            baseline["scientific_config_hash"],
        )
        self.assertNotEqual(
            prompt_mutation["model_request_config_hash"],
            baseline["model_request_config_hash"],
        )

        dry_args = _parsed_args("--provider", "fake_test_teacher", "--dry-run")
        execution_mutation = entrypoint.build_information_weight_identities(
            dry_args, self.states, self.plan
        )
        self.assertEqual(
            execution_mutation["scientific_config_hash"],
            baseline["scientific_config_hash"],
        )
        self.assertEqual(
            execution_mutation["model_request_config_hash"],
            baseline["model_request_config_hash"],
        )
        self.assertNotEqual(
            execution_mutation["execution_config_hash"],
            baseline["execution_config_hash"],
        )
        self.assertNotEqual(
            execution_mutation["full_effective_config_hash"],
            baseline["full_effective_config_hash"],
        )


class InformationWeightManagedCLITests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)

    def test_help_and_version_create_no_run(self):
        for option in ("--help", "--version"):
            with self.subTest(option=option):
                out = self.root / option.lstrip("-")
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        entrypoint.main([option, "--out", str(out)])
                self.assertEqual(raised.exception.code, 0)
                self.assertFalse(out.exists())

    def test_dry_run_has_zero_provider_socket_and_research_samples(self):
        out = self.root / "dry"
        with mock.patch.object(
            entrypoint,
            "_build_provider",
            side_effect=AssertionError("dry-run constructed Provider"),
        ) as build, mock.patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError("dry-run opened socket"),
        ) as network, redirect_stdout(io.StringIO()):
            entrypoint.main(
                [
                    "--provider",
                    "openai",
                    "--model",
                    entrypoint.FROZEN_MODEL,
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
        self.assertEqual(manifest["status"], "finished")
        self.assertEqual(manifest["managed_context"]["run_kind"], entrypoint.RUN_KIND)
        self.assertEqual(summary["planned_latent_states"], 50)
        self.assertEqual(summary["planned_views_per_latent_state"], 4)
        self.assertEqual(summary["planned_logical_requests"], 200)
        self.assertFalse(summary["provider_constructed"])
        self.assertEqual(summary["provider_calls"], 0)
        self.assertFalse(summary["network_access"])
        self.assertEqual(set(summary["honest_n"].values()), {0})
        self.assertEqual(manifest["completion"]["provider_calls"]["attempted"], 0)
        self.assertEqual(
            manifest["completion"]["llm_logical_requests"],
            {
                "unit": "llm_logical_requests",
                "planned": 200,
                "attempted": 0,
                "completed": 0,
                "failed": 0,
            },
        )
        self.assertFalse(manifest["llm"]["runtime"]["network_access"])
        for name in (
            "latent_state_design.json",
            "sample_plan.json",
            "information_weight_samples.jsonl",
            "private_information_weight_records.jsonl",
            "information_weight_teacher_summary.json",
        ):
            self.assertFalse((run_dir / name).exists())

    def test_live_and_mode_guards_fail_before_provider_or_network(self):
        cases = (
            (
                "missing-live",
                ["--provider", "openai", "--model", entrypoint.FROZEN_MODEL],
                "--live",
            ),
            (
                "wrong-model",
                [
                    "--provider",
                    "openai",
                    "--model",
                    "wrong-model",
                    "--live",
                    "--confirm-request-count",
                    "200",
                ],
                "--model",
            ),
            (
                "wrong-confirmation",
                [
                    "--provider",
                    "openai",
                    "--model",
                    entrypoint.FROZEN_MODEL,
                    "--live",
                    "--confirm-request-count",
                    "199",
                ],
                "--confirm-request-count",
            ),
            (
                "fake-live",
                ["--provider", "fake_test_teacher", "--live"],
                "--live",
            ),
            (
                "wrong-run-id",
                ["--provider", "fake_test_teacher", "--run-id", "replace-history"],
                "--run-id",
            ),
        )
        for name, argv, expected in cases:
            with self.subTest(case=name):
                out = self.root / name
                stderr = io.StringIO()
                with mock.patch.object(
                    entrypoint,
                    "_build_provider",
                    side_effect=AssertionError("guard constructed Provider"),
                ) as build, mock.patch.object(
                    socket,
                    "create_connection",
                    side_effect=AssertionError("guard opened socket"),
                ) as network, redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                    with self.assertRaises(SystemExit) as raised:
                        entrypoint.main(argv + ["--out", str(out)])
                self.assertEqual(raised.exception.code, 2)
                self.assertIn(expected, stderr.getvalue())
                build.assert_not_called()
                network.assert_not_called()
                manifest = _read_json(_single_run(out) / "run_manifest.json")
                self.assertEqual(manifest["status"], "failed")
                self.assertEqual(manifest["failure_stage"], "config_validation")
                self.assertEqual(
                    manifest["completion"]["provider_calls"]["attempted"], 0
                )

    def test_openai_builder_freezes_exact_request_envelope_without_transport(self):
        args = _parsed_args(
            "--provider",
            "openai",
            "--model",
            entrypoint.FROZEN_MODEL,
            "--live",
            "--confirm-request-count",
            "200",
        )
        provider = object()
        with mock.patch.object(
            entrypoint, "OpenAITeacherProvider", return_value=provider
        ) as constructor:
            built = entrypoint._build_provider(args)
        self.assertIs(built, provider)
        constructor.assert_called_once_with(
            model="MiniMax-M2.7",
            temperature=1.0,
            max_tokens=190000,
            workers=1,
            top_p=0.95,
            top_k=40,
            response_format={"type": "json_object"},
            application_max_attempts=5,
            retry_delays_seconds=(10.0, 30.0, 60.0, 120.0),
            request_timeout_seconds=7200.0,
            hard_request_deadline_seconds=7200.0,
        )

    def test_live_midflight_interrupt_preserves_unresolved_attempt_accounting(self):
        out = self.root / "live-interrupt"
        provider = _InterruptedLiveProvider()
        environment = {
            "OPENAI_BASE_URL": "http://127.0.0.1:9/v1",
            "OPENAI_API_KEY": "EMPTY",
        }
        with mock.patch.dict(
            os.environ, environment, clear=False
        ), mock.patch.object(
            entrypoint, "_build_provider", return_value=provider
        ), mock.patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError("scripted interrupt opened a socket"),
        ) as network, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                entrypoint.main(_live_argv(out))
        network.assert_not_called()

        run_dir = _single_run(out)
        manifest = _read_json(run_dir / "run_manifest.json")
        public_rows = _read_jsonl(run_dir / "information_weight_samples.jsonl")
        private_rows = _read_jsonl(
            run_dir / "private_information_weight_records.jsonl"
        )
        self.assertEqual(manifest["status"], "failed")
        self.assertTrue(manifest["llm"]["runtime"]["network_access"])
        self.assertTrue(manifest[entrypoint.RUN_KIND]["network_access"])
        self.assertEqual(
            manifest[entrypoint.RUN_KIND]["honest_n"],
            {
                "logical_requests": 1,
                "physical_provider_attempts": 1,
                "raw_responses": 0,
                "parsed_decisions": 0,
                "complete_paired_latent_states": 0,
            },
        )
        self.assertEqual(public_rows, [])
        self.assertEqual(private_rows, [])
        provider_calls = manifest["completion"]["provider_calls"]
        self.assertEqual(provider_calls["attempted"], 1)
        self.assertEqual(provider_calls["succeeded"], 0)
        self.assertEqual(provider_calls["failed"], 0)
        self.assertEqual(provider_calls["unresolved"], 1)
        logical = manifest["completion"]["llm_logical_requests"]
        self.assertEqual(logical["planned"], 200)
        self.assertEqual(logical["attempted"], 1)
        self.assertEqual(logical["completed"], 0)
        self.assertEqual(logical["failed"], 1)
        attempts = manifest["completion"]["application_provider_attempts"]
        self.assertEqual(attempts["attempted"], 1)
        self.assertEqual(attempts["responses_received"], 0)
        self.assertEqual(attempts["provider_exceptions"], 0)
        self.assertEqual(attempts["unresolved_physical_attempts"], 1)
        self.assertEqual(attempts["unresolved_logical_requests"], 1)

    def test_scripted_live_failures_preserve_attempt_parser_and_honest_n_units(self):
        out = self.root / "scripted-live"
        _, plan = _official_design()
        provider = _ScriptedLiveProvider(plan)
        environment = {
            "OPENAI_BASE_URL": "http://127.0.0.1:9/v1",
            "OPENAI_API_KEY": "EMPTY",
        }
        with mock.patch.dict(
            os.environ, environment, clear=False
        ), mock.patch.object(
            entrypoint, "_build_provider", return_value=provider
        ), mock.patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError("scripted Provider opened a socket"),
        ) as network, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            entrypoint.main(_live_argv(out))
        network.assert_not_called()

        run_dir = _single_run(out)
        manifest = _read_json(run_dir / "run_manifest.json")
        summary = _read_json(run_dir / "information_weight_teacher_summary.json")
        public_rows = _read_jsonl(run_dir / "information_weight_samples.jsonl")
        private_rows = _read_jsonl(
            run_dir / "private_information_weight_records.jsonl"
        )
        expected_honest_n = {
            "logical_requests": 200,
            "physical_provider_attempts": 205,
            "raw_responses": 199,
            "parsed_decisions": 196,
            "complete_paired_latent_states": 48,
        }
        self.assertEqual(manifest["status"], "finished")
        self.assertTrue(manifest["llm"]["runtime"]["network_access"])
        self.assertEqual(summary["honest_n"], expected_honest_n)
        self.assertEqual(
            manifest[entrypoint.RUN_KIND]["honest_n"], expected_honest_n
        )
        self.assertEqual(len(public_rows), 200)
        self.assertEqual(len(private_rows), 405)
        self.assertEqual(
            Counter(row["record_type"] for row in private_rows),
            Counter({"application_attempt": 205, "logical_completion": 200}),
        )
        self.assertEqual(
            [public_rows[index]["failure_code"] for index in range(5)],
            [
                None,
                "provider_exception",
                "reported_model_mismatch",
                "finish_reason_invalid",
                "teacher_response_invalid",
            ],
        )
        self.assertEqual(
            [public_rows[index]["parser_attempted"] for index in range(5)],
            [True, False, False, False, True],
        )
        self.assertEqual(
            Counter(row["failure_code"] for row in public_rows),
            Counter(
                {
                    None: 196,
                    "provider_exception": 1,
                    "reported_model_mismatch": 1,
                    "finish_reason_invalid": 1,
                    "teacher_response_invalid": 1,
                }
            ),
        )
        provider_calls = manifest["completion"]["provider_calls"]
        self.assertEqual(
            {
                key: provider_calls[key]
                for key in ("attempted", "succeeded", "failed", "unresolved")
            },
            {"attempted": 205, "succeeded": 199, "failed": 6, "unresolved": 0},
        )
        self.assertEqual(
            {
                key: manifest["completion"]["llm_logical_requests"][key]
                for key in ("planned", "attempted", "completed", "failed")
            },
            {"planned": 200, "attempted": 200, "completed": 200, "failed": 0},
        )
        parsing = manifest["completion"]["parsing"]
        self.assertEqual(
            {
                key: parsing[key]
                for key in ("attempted", "succeeded", "failed", "fallbacks")
            },
            {"attempted": 197, "succeeded": 196, "failed": 1, "fallbacks": 0},
        )
        attempts = manifest["completion"]["application_provider_attempts"]
        self.assertEqual(attempts["attempted"], 205)
        self.assertEqual(attempts["succeeded"], 199)
        self.assertEqual(attempts["failed"], 6)
        self.assertEqual(attempts["responses_received"], 199)
        self.assertEqual(attempts["provider_exceptions"], 6)
        self.assertEqual(attempts["retries_scheduled"], 5)
        self.assertEqual(attempts["logical_requests_with_retry"], 2)
        self.assertEqual(attempts["exhausted_logical_requests"], 1)
        self.assertEqual(attempts["parse_failed_responses"], 1)
        self.assertEqual(attempts["reported_model_mismatch_count"], 1)
        self.assertEqual(attempts["invalid_finish_reason_count"], 1)
        self.assertEqual(attempts["unresolved_physical_attempts"], 0)
        self.assertEqual(attempts["unresolved_logical_requests"], 0)

    def test_nonretryable_first_provider_exception_exhausts_logical_request(self):
        provider = mock.Mock()
        provider.application_attempt_count = 1
        provider.request_count = 1
        provider.response_count = 0
        provider.provider_exception_attempts = 1
        provider.retries_scheduled = 0
        provider.logical_requests_with_retry = 0
        provider.application_attempt_audits = [
            {
                "logical_request_index": 0,
                "application_attempt_index": 1,
                "application_max_attempts": 5,
                "status": "provider_exception",
                "retryable": False,
                "retry_scheduled": False,
                "retry_delay_seconds": None,
            }
        ]
        snapshot = entrypoint._provider_attempt_snapshot(provider, ())
        self.assertEqual(snapshot["attempted"], 1)
        self.assertEqual(snapshot["failed"], 1)
        self.assertEqual(snapshot["unresolved_physical_attempts"], 0)
        self.assertEqual(snapshot["exhausted_logical_requests"], 1)

    def test_fake_full_200_preserves_public_private_boundary_and_honest_n(self):
        out = self.root / "fake-full"
        with mock.patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError("Fake Teacher opened socket"),
        ) as network, redirect_stdout(io.StringIO()):
            entrypoint.main(
                ["--provider", "fake_test_teacher", "--out", str(out)]
            )
        network.assert_not_called()
        run_dir = _single_run(out)
        manifest = _read_json(run_dir / "run_manifest.json")
        summary = _read_json(run_dir / "information_weight_teacher_summary.json")
        latent_design = _read_json(run_dir / "latent_state_design.json")
        sample_plan = _read_json(run_dir / "sample_plan.json")
        public_rows = _read_jsonl(run_dir / "information_weight_samples.jsonl")
        private_path = run_dir / "private_information_weight_records.jsonl"
        private_rows = _read_jsonl(private_path)

        expected_honest_n = {
            "logical_requests": 200,
            "physical_provider_attempts": 200,
            "raw_responses": 200,
            "parsed_decisions": 200,
            "complete_paired_latent_states": 50,
        }
        self.assertEqual(manifest["status"], "finished")
        self.assertEqual(manifest["managed_context"]["run_kind"], entrypoint.RUN_KIND)
        self.assertFalse(manifest["llm"]["runtime"]["network_access"])
        self.assertEqual(summary["honest_n"], expected_honest_n)
        self.assertEqual(
            manifest[entrypoint.RUN_KIND]["honest_n"], expected_honest_n
        )
        self.assertEqual(manifest["completion"]["provider_calls"]["attempted"], 200)
        self.assertEqual(
            manifest["completion"]["llm_logical_requests"]["completed"], 200
        )
        self.assertEqual(
            manifest["completion"]["simulation_runs"]["completed"], 0
        )
        self.assertEqual(len(latent_design["latent_states"]), 50)
        self.assertEqual(len(sample_plan["samples"]), 200)
        self.assertEqual(len(public_rows), 200)
        self.assertEqual(len(private_rows), 400)
        self.assertEqual(
            Counter(row["record_type"] for row in private_rows),
            Counter({"application_attempt": 200, "logical_completion": 200}),
        )
        self.assertEqual([row["request_index"] for row in public_rows], list(range(200)))
        self.assertEqual({row["status"] for row in public_rows}, {"valid"})
        self.assertEqual(
            {name: detail["valid"] for name, detail in summary["profile_completion"].items()},
            {name: 50 for name in contract.PROFILE_IDS},
        )
        for row in public_rows:
            self.assertNotIn("reasoning", row["decision"])
            self.assertNotIn("private_rationale", row)
            self.assertNotIn("raw_response", row)
            prompt_text = row["prompt"]["system"] + "\n" + row["prompt"]["user"]
            self.assertNotIn(row["profile_id"], prompt_text)
        logical_private = [
            row for row in private_rows if row["record_type"] == "logical_completion"
        ]
        self.assertEqual(
            {row["private_rationale"] for row in logical_private},
            {FAKE_PRIVATE_MARKER},
        )
        self.assertTrue(all(row["raw_response"] for row in logical_private))
        self.assertEqual(stat.S_IMODE(private_path.stat().st_mode), 0o600)
        for name in (
            "latent_state_design.json",
            "sample_plan.json",
            "information_weight_samples.jsonl",
            "information_weight_teacher_summary.json",
        ):
            self.assertEqual(stat.S_IMODE((run_dir / name).stat().st_mode), 0o644)
        public_text = "\n".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in run_dir.iterdir()
            if path.is_file() and not path.name.startswith("private_")
        )
        self.assertNotIn(FAKE_PRIVATE_MARKER, public_text)
        self.assertTrue(summary["teacher_only"])
        self.assertFalse(summary["student_enabled"])
        self.assertFalse(summary["market_enabled"])
        self.assertFalse(any("student" in path.name for path in run_dir.iterdir()))
        self.assertFalse(any("market_run" in path.name for path in run_dir.iterdir()))

    def test_existing_run_id_is_o_excl_and_never_overwritten(self):
        out = self.root / "exclusive"
        argv = [
            "--provider",
            "openai",
            "--model",
            entrypoint.FROZEN_MODEL,
            "--dry-run",
            "--out",
            str(out),
        ]
        with redirect_stdout(io.StringIO()):
            entrypoint.main(argv)
        run_dir = out / "runs" / entrypoint.DRY_RUN_ID
        before = {
            path.relative_to(run_dir).as_posix(): (
                path.read_bytes(),
                stat.S_IMODE(path.stat().st_mode),
                path.stat().st_mtime_ns,
            )
            for path in run_dir.rglob("*")
            if path.is_file()
        }
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(FileExistsError):
                entrypoint.main(argv)
        after = {
            path.relative_to(run_dir).as_posix(): (
                path.read_bytes(),
                stat.S_IMODE(path.stat().st_mode),
                path.stat().st_mtime_ns,
            )
            for path in run_dir.rglob("*")
            if path.is_file()
        }
        self.assertEqual(after, before)
        self.assertEqual(len(list((out / "runs").iterdir())), 1)

    def test_official_registry_declares_direct_managed_teacher_only_entrypoint(self):
        matches = [
            spec
            for spec in entrypoints.ENTRYPOINTS
            if spec.entrypoint_id == "experiments.information_weight_teacher"
        ]
        self.assertEqual(len(matches), 1)
        spec = matches[0]
        self.assertEqual(spec.path, "experiments/information_weight_teacher.py")
        self.assertEqual(spec.category, entrypoints.OFFICIAL_MANAGED_RESEARCH_ENTRYPOINT)
        self.assertEqual(spec.management, entrypoints.DIRECT_MANAGED)
        self.assertEqual(spec.provider_access, entrypoints.PROVIDER_DIRECT)
        self.assertTrue(spec.formal_research_allowed)
        self.assertIn("Teacher-only", spec.notes)


if __name__ == "__main__":
    unittest.main()
