"""Offline contracts for the 2,500 x four information-view Teacher study.

The production plan is fully constructed once to lock its exact identities.
No test executes the 10,000-row Fake Teacher, constructs a real Provider, or
opens a network connection.  Scheduler/acquisition behavior is exercised with
two four-row groups only.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import stat
import tempfile
import unittest
from unittest import mock

from experiments import information_weight_teacher as pilot_entrypoint
from experiments import information_weight_teacher_scale as entrypoint
from nmsim import entrypoints
from nmsim import information_weight as pilot
from nmsim import information_weight_scale as contract


EXPECTED_PILOT_CONTRACT_HASH = (
    "9df50ce3bcd3aadc58b9edee8f74c5190f29f8c00167569ac171539defb465cf"
)
EXPECTED_PILOT_PLAN_HASH = (
    "774252b843ad63b873ce1fbc64f11ff7a9850acc9388f09a97ea1a29b20dd468"
)
EXPECTED_PILOT_A1_MANIFEST_SHA256 = (
    "e4ca60802f781f772703976c62410743c2c92074ca66f558ae079d0481d7c350"
)
EXPECTED_SCALE_CONTRACT_HASH = (
    "2eab51ccfe7f0b87ab07d32715c787fff8e0c78e39432c4c8992ff01356d2ae5"
)
EXPECTED_SCALE_STATE_DESIGN_HASH = (
    "3f459bd12e3d47e83b5d6f3fc51d8c531baac17870be0aa14205c078fab3060d"
)
EXPECTED_SCALE_PLAN_HASH = (
    "e75948e3d33dbcbd631bc3c2e39253be41537ed0c89814579248cf1dff4b324f"
)
EXPECTED_SCALE_FIRST_SAMPLE_ID = (
    "301fc9b3e779e8b128fe7fdbbfd171ff56de2347ed476f6a1d2a91439ca776e0"
)
EXPECTED_SCALE_LAST_SAMPLE_ID = (
    "8f5dd3f97f1279498e94ad7380b885b41c89ab8154faf8f47790b98e71c150b5"
)
FAKE_PRIVATE_MARKER = (
    "Deterministic engineering test-double response; not empirical evidence "
    "about people."
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
        raise AssertionError("expected one managed run, found {}".format(len(manifests)))
    return manifests[0].parent


def _parsed_args(*values: str):
    args = entrypoint.build_argparser().parse_args(list(values))
    args.provider = str(args.provider).strip().lower()
    entrypoint._validate_args(args)
    return args


class _Sanitizer:
    @staticmethod
    def _sanitize_text(value, max_length=None):
        del max_length
        return value


class _Events:
    def __init__(self) -> None:
        self.rows: list[tuple] = []

    def emit(self, *args, **kwargs) -> None:
        self.rows.append((args, kwargs))


class _AccountingManager:
    """Small manager double for accounting and eight-row privacy tests."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.run_dir.mkdir(parents=True, exist_ok=False)
        self.run_id = "offline-two-group-test"
        self.events = _Events()
        self._manager = _Sanitizer()
        self.network_access = False
        self.active_llm = None
        self.llm_mode = None
        self.runtime_updates: list[dict] = []
        self.manifest = {
            entrypoint.RUN_KIND: {"network_access": False, "honest_n": {}},
            "completion": {
                "llm_logical_requests": {
                    "planned": entrypoint.FROZEN_REQUEST_COUNT,
                    "attempted": 0,
                    "completed": 0,
                    "failed": 0,
                },
                "provider_calls": {},
                "response_sources": {"provider": 0, "cache": 0, "replay": 0},
                "agent_decisions": {},
                "parsing": {},
                "application_provider_attempts": {},
            },
            "llm": {"runtime": {}},
        }

    def sync_llm_accounting(self, llm=None, tracker=None) -> None:
        del tracker
        subject = llm or self.active_llm
        if subject is None:
            return
        # Model the shared context's broad logical-count projection.  The
        # scale wrapper must replace this with audited raw-response counts on
        # every explicit sync and on the argument-free finish-like sync.
        logical = int(getattr(subject, "request_count", 0))
        sources = {"provider": logical, "cache": 0, "replay": 0}
        self.manifest["completion"]["response_sources"].update(sources)
        self.manifest["llm"]["runtime"].update(
            {"recorded_responses": logical, "response_sources": dict(sources)}
        )

    def register_llm_runtime(self, **kwargs) -> None:
        self.runtime_updates.append(dict(kwargs))
        self.manifest["llm"]["runtime"].update(kwargs)

    def _refresh_derived(self, *args, **kwargs) -> None:
        del args, kwargs

    def _write(self) -> None:
        return None


class _TwoGroupOutOfOrderProvider:
    """No-network Provider double with one retry in each four-row group."""

    model = entrypoint.FROZEN_MODEL

    def __init__(self, plan, delivered: list[int]) -> None:
        self.plan = tuple(plan)
        self.delivered = delivered
        self.call_count = 0
        self.barrier_snapshots: list[tuple[int, ...]] = []
        self.batch_sizes: list[int] = []
        self.application_attempt_audits: list[dict] = []
        self.scale_global_application_attempt_audits: list[dict] = []
        self.request_count = 0
        self.response_count = 0
        self.application_attempt_count = 0
        self.provider_exception_attempts = 0
        self.retries_scheduled = 0
        self.logical_requests_with_retry = 0
        self.network_access = False

    def _audit(self, local_index: int, attempt: int, status: str, retry: bool, callback):
        self.application_attempt_count += 1
        if status == "provider_exception":
            self.provider_exception_attempts += 1
        if retry:
            self.retries_scheduled += 1
        audit = {
            "logical_request_index": local_index,
            "application_attempt_index": attempt,
            "application_max_attempts": entrypoint.FROZEN_APPLICATION_MAX_ATTEMPTS,
            "status": status,
            "provider_error_type": (
                "SyntheticRetry" if status == "provider_exception" else None
            ),
            "provider_error_detail": (
                "offline synthetic retry" if status == "provider_exception" else None
            ),
            "retryable": status == "provider_exception",
            "retry_scheduled": retry,
            "retry_delay_seconds": 0.0 if retry else None,
        }
        self.application_attempt_audits.append(dict(audit))
        callback(local_index, audit)

    async def complete_many(
        self,
        prompts,
        *,
        before_attempt=None,
        on_application_attempt=None,
        on_completion=None,
        strict_sequential=False,
    ):
        self.batch_sizes.append(len(prompts))
        if strict_sequential:
            raise AssertionError("scale groups must allow bounded within-group concurrency")
        if len(prompts) != 4:
            raise AssertionError("scale scheduler released a non-four-row group")
        group_index = self.call_count
        base = group_index * 4
        self.barrier_snapshots.append(tuple(self.delivered))
        if group_index and len(self.delivered) != base:
            raise AssertionError("next group released before prior group resolved")

        for local_index in (2, 0, 3, 1):
            before_attempt(local_index)
            self.request_count += 1
            self.network_access = True
            if local_index == 0:
                self.logical_requests_with_retry += 1
                self._audit(
                    local_index,
                    1,
                    "provider_exception",
                    True,
                    on_application_attempt,
                )
                self._audit(
                    local_index,
                    2,
                    "response_received",
                    False,
                    on_application_attempt,
                )
            else:
                self._audit(
                    local_index,
                    1,
                    "response_received",
                    False,
                    on_application_attempt,
                )

        for local_index in (3, 1, 0, 2):
            global_index = base + local_index
            raw = contract.fake_teacher_response(self.plan[global_index])
            self.response_count += 1
            on_completion(
                local_index,
                entrypoint.TeacherCompletion(
                    raw_response=raw,
                    reported_model=entrypoint.REQUIRED_REPORTED_MODEL,
                    input_tokens=10,
                    output_tokens=20,
                    response_id="offline-{}".format(global_index),
                    reported_model_raw=entrypoint.REQUIRED_REPORTED_MODEL,
                    finish_reason=entrypoint.REQUIRED_FINISH_REASON,
                    finish_reason_raw=entrypoint.REQUIRED_FINISH_REASON,
                    application_attempt_count=2 if local_index == 0 else 1,
                    technical_retry_count=1 if local_index == 0 else 0,
                ),
            )
        self.call_count += 1
        return []

    async def aclose(self) -> None:
        return None


class _SecondGroupInterruptedProvider(_TwoGroupOutOfOrderProvider):
    """First group resolves; the first request of group two stays in flight."""

    async def complete_many(self, prompts, **kwargs):
        if self.call_count == 0:
            return await super().complete_many(prompts, **kwargs)
        self.barrier_snapshots.append(tuple(self.delivered))
        if len(self.delivered) != 4:
            raise AssertionError("second group crossed the first group barrier")
        kwargs["before_attempt"](0)
        self.request_count += 1
        self.application_attempt_count += 1
        self.network_access = True
        raise KeyboardInterrupt()


class _OneExhaustedScriptedProvider:
    """Eight logical rows: seven responses and one five-attempt exhaustion."""

    def __init__(self) -> None:
        self.request_count = 8
        self.response_count = 7
        self.application_attempt_count = 12
        self.provider_exception_attempts = 5
        self.retries_scheduled = 4
        self.logical_requests_with_retry = 1
        self.network_access = True
        audits = [
            {
                "logical_request_index": index,
                "application_attempt_index": 1,
                "status": "response_received",
                "retry_scheduled": False,
            }
            for index in range(7)
        ]
        audits.extend(
            {
                "logical_request_index": 7,
                "application_attempt_index": attempt,
                "status": "provider_exception",
                "retry_scheduled": attempt < 5,
            }
            for attempt in range(1, 6)
        )
        self.scale_global_application_attempt_audits = audits


class InformationWeightScaleContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.states = contract.generate_latent_states(
            count=entrypoint.FROZEN_LATENT_STATE_COUNT,
            seed=entrypoint.FROZEN_SEED,
            study_id=entrypoint.FROZEN_STUDY_ID,
        )
        cls.plan = contract.build_sample_plan(
            cls.states,
            seed=entrypoint.FROZEN_SEED,
            study_id=entrypoint.FROZEN_STUDY_ID,
        )
        cls.fake_args = _parsed_args("--provider", "fake_test_teacher")
        cls.identities = entrypoint.build_information_weight_scale_identities(
            cls.fake_args, cls.states, cls.plan
        )

    def test_exact_2500_by_four_hashes_uniqueness_and_canaries(self):
        self.assertEqual(len(self.states), 2_500)
        self.assertEqual(len(self.plan), 10_000)
        self.assertEqual(contract.CONTRACT_HASH, EXPECTED_SCALE_CONTRACT_HASH)
        self.assertEqual(contract.plan_hash(self.plan), EXPECTED_SCALE_PLAN_HASH)
        self.assertEqual(
            self.identities["state_design_hash"], EXPECTED_SCALE_STATE_DESIGN_HASH
        )
        self.assertEqual(self.plan[0].sample_id, EXPECTED_SCALE_FIRST_SAMPLE_ID)
        self.assertEqual(self.plan[-1].sample_id, EXPECTED_SCALE_LAST_SAMPLE_ID)
        self.assertEqual(len({item.latent_hash for item in self.states}), 2_500)
        self.assertEqual(len({item.view.view_hash for item in self.plan}), 10_000)
        self.assertEqual(len({item.prompt.prompt_hash for item in self.plan}), 10_000)
        self.assertEqual(len({item.sample_hash for item in self.plan}), 10_000)

    def test_profile_fields_account_fields_and_prompt_are_label_free_pilot_contract(self):
        forbidden = tuple(contract.FORBIDDEN_PROMPT_TERMS) + tuple(contract.PROFILE_IDS)
        for sample in self.plan:
            self.assertEqual(len(sample.view.visible_fields), 12)
            self.assertEqual(tuple(sample.view.account_state), contract.ACCOUNT8)
            self.assertEqual(len(sample.view.account_state), 8)
            self.assertEqual(sample.prompt.system, pilot.SYSTEM_PROMPT)
            self.assertEqual(
                sample.prompt.user,
                pilot.canonical_json(sample.view.to_prompt_payload()),
            )
            text = (sample.prompt.system + "\n" + sample.prompt.user).casefold()
            for term in forbidden:
                self.assertNotIn(str(term).casefold(), text)
            payload = json.loads(sample.prompt.user)
            for internal_name in ("profile_id", "latent_id", "view_hash", "weight"):
                self.assertNotIn(internal_name, payload)
        self.assertEqual(contract.PROMPT_SCHEMA_VERSION, pilot.PROMPT_SCHEMA_VERSION)
        self.assertEqual(contract.RESPONSE_FORMAT, pilot.RESPONSE_FORMAT)

    def test_groups_are_contiguous_cyclic_and_exactly_slot_balanced(self):
        self.assertEqual(
            Counter(sample.view.profile_id for sample in self.plan),
            Counter({name: 2_500 for name in contract.PROFILE_IDS}),
        )
        for slot in range(4):
            self.assertEqual(
                Counter(
                    sample.view.profile_id
                    for sample in self.plan
                    if sample.within_group_index == slot
                ),
                Counter({name: 625 for name in contract.PROFILE_IDS}),
            )
        for group_index in range(2_500):
            group = self.plan[group_index * 4 : group_index * 4 + 4]
            expected = (
                contract.PROFILE_IDS[group_index % 4 :]
                + contract.PROFILE_IDS[: group_index % 4]
            )
            self.assertEqual(tuple(item.view.profile_id for item in group), expected)
            self.assertEqual({item.group_index for item in group}, {group_index})
            self.assertEqual(len({item.view.latent_state.latent_id for item in group}), 1)

    def test_v1_contract_plan_and_historical_a1_manifest_are_not_reinterpreted(self):
        states = pilot.generate_latent_states(
            count=pilot_entrypoint.FROZEN_LATENT_STATE_COUNT,
            seed=pilot_entrypoint.FROZEN_SEED,
            study_id=pilot_entrypoint.FROZEN_STUDY_ID,
        )
        plan = pilot.build_sample_plan(
            states,
            seed=pilot_entrypoint.FROZEN_SEED,
            study_id=pilot_entrypoint.FROZEN_STUDY_ID,
        )
        self.assertEqual(pilot.CONTRACT_HASH, EXPECTED_PILOT_CONTRACT_HASH)
        self.assertEqual(pilot.plan_hash(plan), EXPECTED_PILOT_PLAN_HASH)
        self.assertEqual(pilot_entrypoint.LIVE_RUN_ID, "information-weight-pilot-live-20260824-a1")
        self.assertNotEqual(entrypoint.DEFAULT_OUTPUT_ROOT, pilot_entrypoint.DEFAULT_OUTPUT_ROOT)

        # Ignored research outputs are deliberately not test fixtures.  When
        # the preserved local a1 is present, however, this regression also
        # proves that adding/running the successor never changed one byte.
        a1_manifest = (
            Path(__file__).resolve().parents[1]
            / "results_information_weight_pilot/runs"
            / pilot_entrypoint.LIVE_RUN_ID
            / "run_manifest.json"
        )
        if a1_manifest.is_file():
            self.assertEqual(_sha256(a1_manifest), EXPECTED_PILOT_A1_MANIFEST_SHA256)

    def test_fake_parser_canary_is_strict_without_running_fake_10k(self):
        for sample in self.plan[:4]:
            raw = contract.fake_teacher_response(sample)
            parsed = contract.parse_teacher_response(raw, sample.view)
            self.assertIn(parsed.public.action, {"buy", "hold", "sell"})
        with self.assertRaises(ValueError):
            contract.parse_teacher_response(
                '{"action":"hold","intensity":0.5,"reasoning":"invalid"}',
                self.plan[0].view,
            )


class InformationWeightScaleEntrypointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.states = InformationWeightScaleContractTests.states
        cls.plan = InformationWeightScaleContractTests.plan
        cls.identities = InformationWeightScaleContractTests.identities

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)

    def test_registry_declares_scale_as_direct_managed_teacher_only_entrypoint(self):
        spec = entrypoints.ENTRYPOINT_BY_ID["experiments.information_weight_teacher_scale"]
        self.assertEqual(spec.management, entrypoints.DIRECT_MANAGED)
        self.assertEqual(
            spec.category, entrypoints.OFFICIAL_MANAGED_RESEARCH_ENTRYPOINT
        )
        self.assertTrue(spec.formal_research_allowed)
        self.assertIn("10000", " ".join(spec.call_chain + spec.outputs + (spec.notes,)))
        entrypoints.validate_registry()

    def test_live_guards_require_exact_10000_confirmation_and_frozen_run_ids(self):
        valid = _parsed_args(
            "--provider",
            "openai",
            "--model",
            entrypoint.FROZEN_MODEL,
            "--live",
            "--confirm-request-count",
            "10000",
        )
        self.assertEqual(valid.run_id, entrypoint.LIVE_RUN_ID)
        self.assertEqual(entrypoint.FROZEN_REQUEST_COUNT, 10_000)
        self.assertEqual(entrypoint.FROZEN_WORKERS, 4)
        self.assertEqual(
            _parsed_args("--provider", "fake_test_teacher").run_id,
            entrypoint.FAKE_RUN_ID,
        )
        self.assertEqual(
            _parsed_args(
                "--provider", "openai", "--model", entrypoint.FROZEN_MODEL, "--dry-run"
            ).run_id,
            entrypoint.DRY_RUN_ID,
        )
        bad_argv = (
            ["--provider", "openai", "--model", entrypoint.FROZEN_MODEL, "--live"],
            [
                "--provider",
                "openai",
                "--model",
                entrypoint.FROZEN_MODEL,
                "--live",
                "--confirm-request-count",
                "9999",
            ],
            ["--provider", "fake_test_teacher", "--run-id", "overwrite-a1"],
        )
        for argv in bad_argv:
            args = entrypoint.build_argparser().parse_args(argv)
            with self.assertRaises(
                (
                    entrypoint.InformationWeightScaleProviderGuardError,
                    entrypoint.InformationWeightScaleProtocolError,
                )
            ):
                entrypoint._validate_args(args)

    def test_openai_builder_freezes_workers_four_and_exact_request_envelope(self):
        args = _parsed_args(
            "--provider",
            "openai",
            "--model",
            entrypoint.FROZEN_MODEL,
            "--live",
            "--confirm-request-count",
            "10000",
        )
        sentinel = object()
        with mock.patch.object(
            entrypoint, "OpenAITeacherProvider", return_value=sentinel
        ) as constructor:
            self.assertIs(entrypoint._build_provider(args), sentinel)
        constructor.assert_called_once_with(
            model="MiniMax-M2.7",
            temperature=1.0,
            max_tokens=190000,
            workers=4,
            top_p=0.95,
            top_k=40,
            response_format={"type": "json_object"},
            application_max_attempts=5,
            retry_delays_seconds=(10.0, 30.0, 60.0, 120.0),
            request_timeout_seconds=7200.0,
            hard_request_deadline_seconds=7200.0,
        )

    def test_route_alias_and_finish_contract_mutate_only_model_request_identity(self):
        args = _parsed_args(
            "--provider",
            "openai",
            "--model",
            entrypoint.FROZEN_MODEL,
            "--live",
            "--confirm-request-count",
            "10000",
        )
        route_a = (
            "https://alice:password-a@provider-a.invalid/v1/key-a"
            "?api_key=raw-query-a#raw-fragment-a"
        )
        route_b = (
            "http://bob:password-b@provider-b.invalid/v2/key-b"
            "?token=raw-query-b#raw-fragment-b"
        )

        def identities_for(route: str, key: str):
            with mock.patch.dict(
                os.environ,
                {"OPENAI_BASE_URL": route, "OPENAI_API_KEY": key},
                clear=False,
            ):
                return entrypoint.build_information_weight_scale_identities(
                    args, self.states, self.plan
                )

        baseline = identities_for(route_a, "key-a")
        changed_route = identities_for(route_b, "key-b")
        self.assertEqual(
            changed_route["scientific_config_hash"],
            baseline["scientific_config_hash"],
        )
        self.assertEqual(
            changed_route["execution_config_hash"],
            baseline["execution_config_hash"],
        )
        self.assertNotEqual(
            changed_route["model_request_config_hash"],
            baseline["model_request_config_hash"],
        )
        self.assertNotEqual(
            changed_route["full_effective_config_hash"],
            baseline["full_effective_config_hash"],
        )
        self.assertIn("endpoint_identity", baseline["model_request_config"])
        self.assertNotIn("endpoint_identity", baseline["execution_config"])
        for identities, route, secrets in (
            (
                baseline,
                route_a,
                ("alice", "password-a", "provider-a.invalid", "key-a", "raw-query-a"),
            ),
            (
                changed_route,
                route_b,
                ("bob", "password-b", "provider-b.invalid", "key-b", "raw-query-b"),
            ),
        ):
            rendered = json.dumps(identities, ensure_ascii=False, sort_keys=True)
            self.assertNotIn(route, rendered)
            for secret in secrets:
                self.assertNotIn(secret, rendered)

        with mock.patch.dict(
            os.environ,
            {"OPENAI_BASE_URL": route_a, "OPENAI_API_KEY": "key-a"},
            clear=False,
        ), mock.patch.object(
            entrypoint, "REQUIRED_REPORTED_MODEL", "HiggsAI-alias-mutation"
        ):
            changed_alias = entrypoint.build_information_weight_scale_identities(
                args, self.states, self.plan
            )
        with mock.patch.dict(
            os.environ,
            {"OPENAI_BASE_URL": route_a, "OPENAI_API_KEY": "key-a"},
            clear=False,
        ), mock.patch.object(
            entrypoint, "REQUIRED_FINISH_REASON", "length"
        ):
            changed_finish = entrypoint.build_information_weight_scale_identities(
                args, self.states, self.plan
            )

        for mutated in (changed_alias, changed_finish):
            self.assertEqual(
                mutated["scientific_config_hash"], baseline["scientific_config_hash"]
            )
            self.assertEqual(
                mutated["execution_config_hash"], baseline["execution_config_hash"]
            )
            self.assertNotEqual(
                mutated["model_request_config_hash"],
                baseline["model_request_config_hash"],
            )
            self.assertNotEqual(
                mutated["full_effective_config_hash"],
                baseline["full_effective_config_hash"],
            )

    def test_dry_run_is_plan_only_zero_provider_and_zero_socket(self):
        out = self.root / "dry"
        from nmsim import information_weight_scale as imported_domain

        raw_base_url = "http://192.0.2.44:8000/v1"
        raw_api_key = "RAW_SCALE_DRY_SECRET"
        with mock.patch.dict(
            os.environ,
            {"OPENAI_BASE_URL": raw_base_url, "OPENAI_API_KEY": raw_api_key},
            clear=False,
        ), mock.patch.object(
            imported_domain, "generate_latent_states", return_value=self.states
        ), mock.patch.object(
            imported_domain, "build_sample_plan", return_value=self.plan
        ), mock.patch.object(
            entrypoint,
            "_build_provider",
            side_effect=AssertionError("dry-run constructed Provider"),
        ) as build, mock.patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError("dry-run opened socket"),
        ) as network, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
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
        manifest_path = run_dir / "run_manifest.json"
        manifest_text = manifest_path.read_text(encoding="utf-8")
        manifest = json.loads(manifest_text)
        summary = _read_json(run_dir / "dry_run_summary.json")
        self.assertNotIn(raw_base_url, manifest_text)
        self.assertNotIn("192.0.2.44", manifest_text)
        self.assertNotIn(raw_api_key, manifest_text)
        self.assertEqual(manifest["config"]["openai_base_url"], "")
        self.assertEqual(manifest["config"]["openai_api_key"], "<redacted>")
        effective = manifest["config_contract"]["effective_config_summary"]
        self.assertFalse(effective["openai_base_url"]["configured"])
        self.assertFalse(effective["openai_api_key"]["configured"])
        self.assertEqual(effective["openai_api_key"]["value"], "<not-configured>")
        self.assertEqual(manifest["status"], "finished")
        self.assertEqual(summary["planned_latent_states"], 2_500)
        self.assertEqual(summary["planned_latent_groups"], 2_500)
        self.assertEqual(summary["planned_logical_requests"], 10_000)
        self.assertEqual(summary["workers"], 4)
        self.assertEqual(summary["replicates_per_state_view"], 1)
        self.assertEqual(
            summary["scientific_limitations"],
            list(entrypoint.SCIENTIFIC_LIMITATIONS),
        )
        self.assertFalse(summary["provider_constructed"])
        self.assertEqual(summary["provider_calls"], 0)
        self.assertFalse(summary["network_access"])
        self.assertEqual(set(summary["honest_n"].values()), {0})
        self.assertFalse((run_dir / "sample_plan.json").exists())
        self.assertFalse((run_dir / "information_weight_scale_samples.jsonl").exists())

    def test_exclusive_writes_refuse_overwrite(self):
        path = self.root / "immutable.json"
        entrypoint._write_json_exclusive(path, {"first": True})
        before = path.read_bytes()
        with self.assertRaises(FileExistsError):
            entrypoint._write_json_exclusive(path, {"second": True})
        self.assertEqual(path.read_bytes(), before)

    def test_two_group_scheduler_barrier_global_indices_and_retry_accounting(self):
        delivered: list[int] = []
        attempts: list[tuple[int, dict]] = []
        provider = _TwoGroupOutOfOrderProvider(self.plan[:8], delivered)

        asyncio.run(
            entrypoint._execute_openai_group_batches(
                provider,
                self.plan[:8],
                before_attempt=lambda index: None,
                on_application_attempt=lambda index, audit: attempts.append(
                    (index, dict(audit))
                ),
                on_completion=lambda index, completion: delivered.append(index),
                group_size=4,
            )
        )
        self.assertEqual(provider.batch_sizes, [4, 4])
        self.assertEqual(provider.barrier_snapshots, [(), (3, 1, 0, 2)])
        self.assertEqual(delivered, [3, 1, 0, 2, 7, 5, 4, 6])
        self.assertEqual(len(attempts), 10)
        self.assertEqual(
            {index for index, audit in attempts if audit["retry_scheduled"]},
            {0, 4},
        )
        self.assertTrue(
            all(index == audit["callback_global_index"] for index, audit in attempts)
        )
        public_rows = [
            {
                "latent_state_id": self.plan[index].view.latent_state.latent_id,
                "profile_id": self.plan[index].view.profile_id,
                "status": "valid",
                "response_hash": "response-{}".format(index),
                "reported_model": entrypoint.REQUIRED_REPORTED_MODEL,
                "finish_reason": entrypoint.REQUIRED_FINISH_REASON,
                "parser_attempted": True,
            }
            for index in delivered
        ]
        snapshot = entrypoint._provider_attempt_snapshot(provider, public_rows)
        self.assertEqual(snapshot["attempted"], 10)
        self.assertEqual(snapshot["logical_requests_with_retry"], 2)
        self.assertEqual(snapshot["retries_scheduled"], 2)
        self.assertEqual(snapshot["unresolved_physical_attempts"], 0)

    def test_midflight_interrupt_preserves_resolved_group_and_unresolved_attempt(self):
        delivered: list[int] = []
        provider = _SecondGroupInterruptedProvider(self.plan[:8], delivered)
        with self.assertRaises(KeyboardInterrupt):
            asyncio.run(
                entrypoint._execute_openai_group_batches(
                    provider,
                    self.plan[:8],
                    before_attempt=lambda index: None,
                    on_application_attempt=lambda index, audit: None,
                    on_completion=lambda index, completion: delivered.append(index),
                    group_size=4,
                )
            )
        self.assertEqual(delivered, [3, 1, 0, 2])
        self.assertEqual(provider.barrier_snapshots, [(), (3, 1, 0, 2)])
        public_rows = [
            {
                "latent_state_id": self.plan[index].view.latent_state.latent_id,
                "profile_id": self.plan[index].view.profile_id,
                "status": "valid",
                "response_hash": "response-{}".format(index),
                "reported_model": entrypoint.REQUIRED_REPORTED_MODEL,
                "finish_reason": entrypoint.REQUIRED_FINISH_REASON,
                "parser_attempted": True,
            }
            for index in delivered
        ]
        manager = _AccountingManager(self.root / "interrupt-accounting")
        honest = entrypoint._apply_accounting_snapshot(manager, provider, public_rows)
        self.assertEqual(
            honest,
            {
                "logical_requests": 5,
                "physical_provider_attempts": 6,
                "raw_responses": 4,
                "parsed_decisions": 4,
                "resolved_logical_requests": 4,
                "complete_paired_latent_states": 1,
                "resolved_latent_groups": 1,
            },
        )
        calls = manager.manifest["completion"]["provider_calls"]
        self.assertEqual(calls["attempted"], 6)
        self.assertEqual(calls["unresolved"], 1)
        logical = manager.manifest["completion"]["llm_logical_requests"]
        self.assertEqual(logical["attempted"], 5)
        self.assertEqual(logical["completed"], 4)
        self.assertEqual(logical["failed"], 1)

    def test_exhausted_row_response_sources_stay_seven_across_repeated_and_finish_sync(self):
        provider = _OneExhaustedScriptedProvider()
        public_rows = []
        for index, sample in enumerate(self.plan[:8]):
            valid = index < 7
            public_rows.append(
                {
                    "latent_state_id": sample.view.latent_state.latent_id,
                    "profile_id": sample.view.profile_id,
                    "status": "valid" if valid else "failed",
                    "response_hash": "response-{}".format(index) if valid else None,
                    "failure_code": None if valid else "provider_exception",
                    "parser_attempted": valid,
                    "reported_model": (
                        entrypoint.REQUIRED_REPORTED_MODEL if valid else None
                    ),
                    "finish_reason": (
                        entrypoint.REQUIRED_FINISH_REASON if valid else None
                    ),
                }
            )
        manager = _AccountingManager(self.root / "exhausted-response-sources")
        manager.active_llm = provider
        entrypoint._install_physical_attempt_accounting(
            manager, provider, public_rows
        )

        # The third call mirrors ManagedRunContext.finish(), which performs an
        # argument-free final sync after earlier explicit progress syncs.
        sync_calls = (
            lambda: manager.sync_llm_accounting(provider),
            lambda: manager.sync_llm_accounting(provider),
            lambda: manager.sync_llm_accounting(),
        )
        for sync in sync_calls:
            sync()
            sources = manager.manifest["completion"]["response_sources"]
            runtime = manager.manifest["llm"]["runtime"]
            honest = manager.manifest[entrypoint.RUN_KIND]["honest_n"]
            self.assertEqual(sources, {"provider": 7, "cache": 0, "replay": 0})
            self.assertEqual(runtime["recorded_responses"], 7)
            self.assertEqual(
                runtime["response_sources"],
                {"provider": 7, "cache": 0, "replay": 0},
            )
            self.assertEqual(honest["logical_requests"], 8)
            self.assertEqual(honest["raw_responses"], 7)
            self.assertNotEqual(sources["provider"], 8)
            self.assertNotEqual(runtime["recorded_responses"], 8)

        attempts = manager.manifest["completion"]["application_provider_attempts"]
        self.assertEqual(attempts["responses_received"], 7)
        self.assertEqual(attempts["provider_exceptions"], 5)
        self.assertEqual(attempts["logical_requests_with_retry"], 1)
        self.assertEqual(attempts["exhausted_logical_requests"], 1)

    def test_eight_row_fake_acquisition_private_mode_and_public_projection(self):
        manager = _AccountingManager(self.root / "privacy")
        args = _parsed_args("--provider", "fake_test_teacher")
        entrypoint._initialise_manifest(
            manager, args=args, identities=self.identities
        )
        with mock.patch.object(entrypoint, "FROZEN_GROUP_COUNT", 2), mock.patch.object(
            entrypoint, "FROZEN_GROUP_SIZE", 4
        ), mock.patch.object(entrypoint, "FROZEN_LATENT_STATE_COUNT", 2), mock.patch.object(
            entrypoint, "FROZEN_REQUEST_COUNT", 8
        ), mock.patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError("eight-row Fake opened a socket"),
        ) as network:
            summary = entrypoint.run_teacher_acquisition(
                manager,
                args=args,
                plan=self.plan[:8],
                identities=self.identities,
            )
        network.assert_not_called()
        public_path = manager.run_dir / "information_weight_scale_samples.jsonl"
        private_path = manager.run_dir / "private_information_weight_scale_records.jsonl"
        self.assertEqual(stat.S_IMODE(private_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(public_path.stat().st_mode), 0o644)
        self.assertEqual(len(_read_jsonl(public_path)), 8)
        self.assertEqual(len(_read_jsonl(private_path)), 16)
        public_text = public_path.read_text(encoding="utf-8")
        private_text = private_path.read_text(encoding="utf-8")
        self.assertNotIn(FAKE_PRIVATE_MARKER, public_text)
        self.assertNotIn('"raw_response":', public_text)
        self.assertNotIn('"private_rationale":', public_text)
        self.assertNotIn("OPENAI_API_KEY", public_text)
        self.assertIn(FAKE_PRIVATE_MARKER, private_text)
        self.assertIn('"raw_response":', private_text)
        self.assertEqual(summary["honest_n"]["logical_requests"], 8)
        self.assertEqual(summary["honest_n"]["parsed_decisions"], 8)
        self.assertEqual(summary["honest_n"]["complete_paired_latent_states"], 2)
        self.assertEqual(summary["replicates_per_state_view"], 1)
        self.assertEqual(
            summary["scientific_limitations"],
            list(entrypoint.SCIENTIFIC_LIMITATIONS),
        )
        protocol = manager.manifest[entrypoint.RUN_KIND]
        self.assertEqual(
            protocol["scientific_limitations"],
            list(entrypoint.SCIENTIFIC_LIMITATIONS),
        )
        self.assertIn("field-count composition", protocol["observation_allocation_semantics"])
        self.assertIn("not an explicit attention coefficient", protocol["observation_allocation_semantics"])
        self.assertIn("independent marginal Latin-hypercube", protocol["synthetic_design_boundary"])
        self.assertIn("not population weighted", protocol["synthetic_design_boundary"])
        self.assertEqual(
            protocol["summary"]["scientific_limitations"],
            list(entrypoint.SCIENTIFIC_LIMITATIONS),
        )


if __name__ == "__main__":
    unittest.main()
