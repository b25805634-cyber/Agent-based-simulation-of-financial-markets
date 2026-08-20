"""Managed-boundary tests for the V2 Teacher -> Student -> market entrypoint.

Every path here is offline.  Real-provider construction and socket access are
patched to observable failures wherever a guard or dry-run must stop first.
"""
from __future__ import annotations

import asyncio
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import re
import socket
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from experiments import v2_attention_market as entrypoint


FAKE_PRIVATE_REASONING = (
    "Synthetic engineering test-double response; it is not empirical evidence "
    "about people."
)
INVALID_PRIVATE_MARKER = "INVALID_V2_PRIVATE_REASONING_MARKER"
HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list:
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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                return digest.hexdigest()
            digest.update(block)


def _small_full_args(out_root: Path) -> list:
    return [
        "--provider",
        "fake_test_teacher",
        "--states",
        "24",
        "--replicates",
        "2",
        "--training-epochs",
        "10",
        "--market-agents",
        "8",
        "--market-rounds",
        "3",
        "--market-seeds",
        "1",
        "--workers",
        "2",
        "--out",
        str(out_root),
    ]


def _pilot_args(
    out_root: Path,
    *,
    live: bool = False,
    profile: str = entrypoint.MINIMAX_M27_JOINT54X3_PILOT,
    **overrides,
) -> list:
    explicit_overrides = set(overrides)
    values = {
        "provider": "openai",
        "model": "MiniMax-M2.7",
        "temperature": "0",
        "max_tokens": "1024",
        "states": "54",
        "replicates": "3",
        "workers": "1",
        "seed": "20260811",
        "training_epochs": "400",
        "market_agents": "48",
        "market_rounds": "60",
        "market_seeds": "3",
    }
    values.update({key: str(value) for key, value in overrides.items()})
    argv = [
        "--provider",
        values["provider"],
        "--model",
        values["model"],
        "--temperature",
        values["temperature"],
        "--max-tokens",
        values["max_tokens"],
        "--states",
        values["states"],
        "--replicates",
        values["replicates"],
        "--workers",
        values["workers"],
        "--seed",
        values["seed"],
        "--training-epochs",
        values["training_epochs"],
        "--market-agents",
        values["market_agents"],
        "--market-rounds",
        values["market_rounds"],
        "--market-seeds",
        values["market_seeds"],
        "--pilot-profile",
        profile,
        "--out",
        str(out_root),
    ]
    if live:
        argv.extend(["--live", "--confirm-request-count", "162"])
    else:
        argv.append("--dry-run")
    if profile == entrypoint.MINIMAX_M27_REQUEST_HIGGSAI_REPORTED_JOINT54X3_PILOT:
        argv.extend(
            [
                "--run-id",
                (
                    "v2-teacher-pilot-live-20260812-a2"
                    if live
                    else "v2-teacher-pilot-v2-dry-20260812-a1"
                ),
            ]
        )
    elif profile == entrypoint.MINIMAX_M27_HIGGSAI_FINISH_AUDIT_JOINT54X3_PILOT:
        if "max_tokens" not in explicit_overrides:
            argv[argv.index("--max-tokens") + 1] = "4096"
        argv.extend(
            [
                "--run-id",
                (
                    "v2-teacher-pilot-live-20260813-a3"
                    if live
                    else "v2-teacher-pilot-v3-dry-20260813-a1"
                ),
            ]
        )
    elif (
        profile
        == entrypoint.MINIMAX_M27_HIGGSAI_FINISH_AUDIT_EXTERNAL_JOINT54X3_PILOT
    ):
        if "max_tokens" not in explicit_overrides:
            argv[argv.index("--max-tokens") + 1] = "4096"
        argv.extend(
            [
                "--run-id",
                (
                    "v2-teacher-pilot-live-20260813-a4"
                    if live
                    else "v2-teacher-pilot-v4-dry-20260813-a1"
                ),
            ]
        )
    elif profile == entrypoint.MINIMAX_M27_HIGGSAI_LONG_TIMEOUT_JOINT54X3_PILOT:
        if "max_tokens" not in explicit_overrides:
            argv[argv.index("--max-tokens") + 1] = "4096"
        argv.extend(
            [
                "--run-id",
                (
                    "v2-teacher-pilot-live-20260813-a5"
                    if live
                    else "v2-teacher-pilot-v5-dry-20260813-a1"
                ),
            ]
        )
    elif (
        profile
        == entrypoint.MINIMAX_M27_HIGGSAI_TIMEOUT600_OUTPUT16384_JOINT54X3_PILOT
    ):
        if "max_tokens" not in explicit_overrides:
            argv[argv.index("--max-tokens") + 1] = "16384"
        argv.extend(
            [
                "--run-id",
                (
                    "v2-teacher-pilot-live-20260820-a6"
                    if live
                    else "v2-teacher-pilot-v6-dry-20260820-a1"
                ),
            ]
        )
    elif (
        profile
        == entrypoint.MINIMAX_M27_HIGGSAI_TIMEOUT1800_OUTPUT65536_JOINT54X3_PILOT
    ):
        if "max_tokens" not in explicit_overrides:
            argv[argv.index("--max-tokens") + 1] = "65536"
        argv.extend(
            [
                "--run-id",
                (
                    "v2-teacher-pilot-live-20260820-a7"
                    if live
                    else "v2-teacher-pilot-v7-dry-20260820-a1"
                ),
            ]
        )
    elif (
        profile
        == entrypoint.MINIMAX_M27_HIGGSAI_T1_P095_K40_TIMEOUT7200_OUTPUT190000_JOINT54X3_PILOT
    ):
        if "temperature" not in explicit_overrides:
            argv[argv.index("--temperature") + 1] = "1"
        if "max_tokens" not in explicit_overrides:
            argv[argv.index("--max-tokens") + 1] = "190000"
        argv.extend(
            [
                "--run-id",
                (
                    "v2-teacher-pilot-live-20260820-a8"
                    if live
                    else "v2-teacher-pilot-v8-dry-20260820-a1"
                ),
            ]
        )
    return argv


class V2BootstrapAndGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)

    def test_help_and_version_do_not_create_a_managed_run(self):
        for option in ("--help", "--version"):
            with self.subTest(option=option):
                out = self.root / option.lstrip("-")
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        entrypoint.main([option, "--out", str(out)])
                self.assertEqual(raised.exception.code, 0)
                self.assertFalse(out.exists())

    def test_openai_guards_fail_before_provider_construction_or_socket_access(self):
        cases = (
            (
                "missing-live",
                ["--provider", "openai", "--model", "guarded-model"],
                "--live",
            ),
            (
                "missing-model",
                [
                    "--provider",
                    "openai",
                    "--live",
                    "--confirm-request-count",
                    "12",
                ],
                "--model",
            ),
            (
                "request-count-mismatch",
                [
                    "--provider",
                    "openai",
                    "--model",
                    "guarded-model",
                    "--live",
                    "--confirm-request-count",
                    "11",
                ],
                "--confirm-request-count",
            ),
            (
                "negative-seed",
                [
                    "--provider",
                    "openai",
                    "--model",
                    "guarded-model",
                    "--live",
                    "--confirm-request-count",
                    "12",
                    "--seed",
                    "-1",
                ],
                "--seed",
            ),
        )
        for name, prefix, expected_message in cases:
            with self.subTest(case=name):
                out = self.root / name
                argv = prefix + [
                    "--states",
                    "12",
                    "--replicates",
                    "1",
                    "--out",
                    str(out),
                ]
                stderr = io.StringIO()
                with mock.patch.object(
                    entrypoint,
                    "_build_openai_provider",
                    side_effect=AssertionError("unguarded provider construction"),
                ) as build_provider, mock.patch.object(
                    socket,
                    "create_connection",
                    side_effect=AssertionError("unguarded socket access"),
                ) as network, redirect_stderr(stderr), redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        entrypoint.main(argv)
                self.assertEqual(raised.exception.code, 2)
                self.assertIn(expected_message, stderr.getvalue())
                build_provider.assert_not_called()
                network.assert_not_called()
                manifest = _read_json(_single_run(out) / "run_manifest.json")
                self.assertEqual(manifest["status"], "failed")
                self.assertEqual(manifest["failure_stage"], "config_validation")
                self.assertEqual(
                    manifest["completion"]["provider_calls"]["attempted"], 0
                )
                self.assertFalse(manifest["llm"]["runtime"]["network_access"])

    def test_openai_dry_run_has_zero_provider_and_network_activity(self):
        out = self.root / "dry-run"
        with mock.patch.object(
            entrypoint,
            "_build_openai_provider",
            side_effect=AssertionError("dry-run constructed a provider"),
        ) as build_provider, mock.patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError("dry-run opened a socket"),
        ) as network, redirect_stdout(io.StringIO()):
            entrypoint.main(
                [
                    "--provider",
                    "openai",
                    "--model",
                    "plan-only-model",
                    "--dry-run",
                    "--states",
                    "24",
                    "--replicates",
                    "2",
                    "--out",
                    str(out),
                ]
            )
        build_provider.assert_not_called()
        network.assert_not_called()
        run_dir = _single_run(out)
        manifest = _read_json(run_dir / "run_manifest.json")
        summary = _read_json(run_dir / "dry_run_summary.json")
        self.assertEqual(manifest["status"], "finished")
        self.assertTrue(summary["dry_run"])
        self.assertEqual(summary["planned_teacher_requests"], 48)
        self.assertEqual(summary["provider_calls"], 0)
        self.assertFalse(summary["network_access"])
        self.assertEqual(summary["honest_n_teacher_samples"], 0)
        self.assertEqual(summary["honest_n_aggregated_examples"], 0)
        self.assertEqual(summary["honest_n_market_runs"], 0)
        self.assertEqual(
            summary["scientific_claim_status"],
            "plan_only_no_teacher_or_market_result",
        )
        self.assertEqual(
            manifest["v2_attention_market"]["scientific_claim_status"],
            summary["scientific_claim_status"],
        )
        self.assertEqual(
            manifest["completion"]["provider_calls"]["attempted"], 0
        )
        self.assertEqual(
            manifest["completion"]["llm_logical_requests"]["attempted"], 0
        )
        self.assertFalse(manifest["llm"]["runtime"]["network_access"])
        persona_contract = manifest["research_profile"]["persona_contract"]
        self.assertFalse(persona_contract["applicable"])
        self.assertIn("no identity label", persona_contract["reason"])
        self.assertFalse((run_dir / "teacher_samples.jsonl").exists())
        self.assertFalse((run_dir / "private_teacher_records.jsonl").exists())

    def test_openai_constructor_failure_records_no_attempt_or_network(self):
        out = self.root / "constructor-failure"
        argv = [
            "--provider",
            "openai",
            "--model",
            "guarded-model",
            "--live",
            "--states",
            "24",
            "--replicates",
            "1",
            "--confirm-request-count",
            "24",
            "--out",
            str(out),
        ]
        with mock.patch.object(
            entrypoint,
            "_build_openai_provider",
            side_effect=RuntimeError("constructor failed before transport"),
        ), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                entrypoint.main(argv)
        self.assertEqual(raised.exception.code, 1)
        run_dir = _single_run(out)
        manifest = _read_json(run_dir / "run_manifest.json")
        events = _read_jsonl(run_dir / "events.jsonl")
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["completion"]["provider_calls"]["attempted"], 0)
        self.assertFalse(manifest["llm"]["runtime"]["network_access"])
        self.assertFalse(
            any(row.get("type") == "LLMRequestRecorded" for row in events)
        )
        self.assertFalse((run_dir / "teacher_samples.jsonl").exists())

    def test_endpoint_identity_hash_is_secret_free_and_secret_invariant(self):
        endpoints = (
            (
                "HTTPS://user-one:pass-one-sentinel@example.invalid/gateway/key-one/v1?api_key=query-one-sentinel&region=test&sig=signature-one#fragment-one",
                "key-one",
            ),
            (
                "https://user-two:pass-two-sentinel@example.invalid/gateway/key-two/v1?region=other&sig=signature-two&api_key=query-two-sentinel#fragment-two",
                "key-two",
            ),
        )
        hashes = []
        forbidden = (
            "user-one",
            "pass-one-sentinel",
            "query-one-sentinel",
            "user-two",
            "pass-two-sentinel",
            "query-two-sentinel",
            "signature-one",
            "signature-two",
            "fragment-one",
            "fragment-two",
            "key-one",
            "key-two",
        )
        for index, (endpoint, api_key) in enumerate(endpoints):
            out = self.root / "endpoint-{}".format(index)
            with mock.patch.dict(
                os.environ,
                {"OPENAI_BASE_URL": endpoint, "OPENAI_API_KEY": api_key},
                clear=False,
            ), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                entrypoint.main(
                    [
                        "--provider",
                        "openai",
                        "--model",
                        "plan-only-model",
                        "--dry-run",
                        "--states",
                        "24",
                        "--out",
                        str(out),
                    ]
                )
            run_dir = _single_run(out)
            manifest = _read_json(run_dir / "run_manifest.json")
            request = manifest["v2_config_identities"]["model_request_config"]
            hashes.append(
                manifest["v2_config_identities"][
                    "v2_model_request_config_hash"
                ]
            )
            self.assertTrue(request["endpoint_identity"]["userinfo_redacted"])
            self.assertTrue(
                request["endpoint_identity"]["sensitive_query_redacted"]
            )
            all_text = "\n".join(
                path.read_text(encoding="utf-8", errors="replace")
                for path in run_dir.rglob("*")
                if path.is_file()
            )
            for secret in forbidden:
                self.assertNotIn(secret, all_text)
        self.assertEqual(hashes[0], hashes[1])

    def test_reusing_run_id_fails_exclusively_without_overwriting_first_run(self):
        out = self.root / "exclusive"
        argv = [
            "--provider",
            "fake_null_teacher",
            "--dry-run",
            "--states",
            "24",
            "--replicates",
            "1",
            "--run-id",
            "v2-exclusive-run",
            "--out",
            str(out),
        ]
        with redirect_stdout(io.StringIO()):
            entrypoint.main(argv)
        run_dir = out / "runs" / "v2-exclusive-run"
        before = {
            path.relative_to(run_dir).as_posix(): path.read_bytes()
            for path in run_dir.rglob("*")
            if path.is_file()
        }
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(FileExistsError):
                entrypoint.main(argv)
        after = {
            path.relative_to(run_dir).as_posix(): path.read_bytes()
            for path in run_dir.rglob("*")
            if path.is_file()
        }
        self.assertEqual(after, before)
        self.assertEqual(len(list((out / "runs").iterdir())), 1)


class V2FrozenTeacherPilotTests(unittest.TestCase):
    ENDPOINT_ENV = {
        "OPENAI_BASE_URL": "http://10.214.32.152:8000/v1",
        "OPENAI_API_KEY": "EMPTY",
    }

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)

    def test_frozen_profile_dry_run_is_zero_network_162_plan_with_identities(self):
        out = self.root / "pilot-dry-run"
        with mock.patch.dict(
            os.environ, self.ENDPOINT_ENV, clear=False
        ), mock.patch.object(
            entrypoint,
            "_build_openai_provider",
            side_effect=AssertionError("pilot dry-run constructed a provider"),
        ) as build_provider, mock.patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError("pilot dry-run opened a socket"),
        ) as network, redirect_stdout(io.StringIO()):
            entrypoint.main(_pilot_args(out))

        build_provider.assert_not_called()
        network.assert_not_called()
        run_dir = _single_run(out)
        manifest = _read_json(run_dir / "run_manifest.json")
        summary = _read_json(run_dir / "dry_run_summary.json")
        profile = entrypoint.pilot_profile_descriptor(
            entrypoint.MINIMAX_M27_JOINT54X3_PILOT
        )
        self.assertEqual(summary["planned_states"], 54)
        self.assertEqual(summary["planned_replicates_per_state"], 3)
        self.assertEqual(summary["planned_teacher_requests"], 162)
        self.assertEqual(summary["provider_calls"], 0)
        self.assertFalse(summary["network_access"])
        self.assertEqual(summary["pilot_profile"], profile)
        self.assertEqual(manifest["status"], "finished")
        self.assertEqual(
            manifest["execution"]["batching"]["teacher"]["strategy"],
            "first_planned_canary_then_strict_sequential_fail_fast",
        )
        self.assertEqual(
            manifest["completion"]["provider_calls"]["attempted"], 0
        )
        self.assertEqual(
            manifest["v2_attention_market"]["teacher_acceptance_gate"]["status"],
            "plan_only",
        )
        identities = manifest["v2_config_identities"]
        scientific = identities["scientific_config"]
        request = identities["model_request_config"]
        execution = identities["execution_config"]
        self.assertEqual(
            scientific["teacher_sampling"]["pilot_profile_id"],
            profile["profile_id"],
        )
        self.assertEqual(
            scientific["teacher_sampling"]["teacher_acceptance_gate"],
            profile["teacher_acceptance_gate"],
        )
        self.assertEqual(
            request["pilot_profile_id"],
            entrypoint.MINIMAX_M27_JOINT54X3_PILOT,
        )
        self.assertEqual(request["planned_requests"], 162)
        self.assertEqual(request["required_reported_model"], "MiniMax-M2.7")
        self.assertEqual(
            request["planned_sample_order_hash"],
            profile["planned_sample_order_hash"],
        )
        self.assertEqual(
            request["canary_sample_id"], profile["canary_sample_id"]
        )
        self.assertTrue(execution["strict_sequential_teacher_transport"])
        self.assertTrue(execution["first_planned_sample_is_canary"])
        self.assertTrue(
            execution["fail_fast_after_any_resolved_teacher_failure"]
        )
        for key in (
            "v2_scientific_config_hash",
            "v2_model_request_config_hash",
            "v2_execution_config_hash",
            "v2_full_effective_config_hash",
        ):
            self.assertRegex(summary[key], HASH_RE)

    def test_every_frozen_profile_argument_mutation_fails_config_validation(self):
        mutations = {
            "provider": "fake_null_teacher",
            "model": "MiniMax-M2.7-mutated",
            "temperature": "0.1",
            "max_tokens": "1025",
            "states": "55",
            "replicates": "4",
            "workers": "2",
            "seed": "20260812",
            "training_epochs": "401",
            "market_agents": "49",
            "market_rounds": "61",
            "market_seeds": "4",
        }
        for field, mutated_value in mutations.items():
            with self.subTest(field=field):
                out = self.root / "mutated-{}".format(field)
                stderr = io.StringIO()
                with mock.patch.dict(
                    os.environ, self.ENDPOINT_ENV, clear=False
                ), mock.patch.object(
                    entrypoint,
                    "_build_openai_provider",
                    side_effect=AssertionError(
                        "invalid profile constructed a provider"
                    ),
                ) as build_provider, mock.patch.object(
                    socket,
                    "create_connection",
                    side_effect=AssertionError("invalid profile opened a socket"),
                ) as network, redirect_stdout(io.StringIO()), redirect_stderr(
                    stderr
                ):
                    with self.assertRaises(SystemExit) as raised:
                        entrypoint.main(
                            _pilot_args(out, **{field: mutated_value})
                        )
                self.assertEqual(raised.exception.code, 2)
                build_provider.assert_not_called()
                network.assert_not_called()
                manifest = _read_json(_single_run(out) / "run_manifest.json")
                self.assertEqual(manifest["status"], "failed")
                self.assertEqual(
                    manifest["failure_stage"], "config_validation"
                )
                self.assertEqual(
                    manifest["completion"]["provider_calls"]["attempted"], 0
                )

        out = self.root / "mutated-endpoint-identity"
        with mock.patch.dict(
            os.environ,
            {
                "OPENAI_BASE_URL": "http://10.214.32.153:8000/v1",
                "OPENAI_API_KEY": "EMPTY",
            },
            clear=False,
        ), mock.patch.object(
            entrypoint,
            "_build_openai_provider",
            side_effect=AssertionError("invalid endpoint constructed a provider"),
        ) as build_provider, mock.patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError("invalid endpoint opened a socket"),
        ) as network, redirect_stdout(io.StringIO()), redirect_stderr(
            io.StringIO()
        ):
            with self.assertRaises(SystemExit) as raised:
                entrypoint.main(_pilot_args(out))
        self.assertEqual(raised.exception.code, 2)
        build_provider.assert_not_called()
        network.assert_not_called()
        manifest = _read_json(_single_run(out) / "run_manifest.json")
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["failure_stage"], "config_validation")
        self.assertEqual(
            manifest["completion"]["provider_calls"]["attempted"], 0
        )

    def test_strict_sequential_callback_failure_stops_after_first_call(self):
        class Completions:
            def __init__(self):
                self.calls = 0

            async def create(self, **kwargs):
                del kwargs
                self.calls += 1
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(
                                content=(
                                    '{"action":"hold","intensity":0,'
                                    '"reasoning":"private canary"}'
                                )
                            )
                        )
                    ],
                    model="MiniMax-M2.7",
                    id="strict-sequential-response",
                    usage=SimpleNamespace(prompt_tokens=4, completion_tokens=5),
                )

        completions_api = Completions()
        provider = object.__new__(entrypoint.OpenAITeacherProvider)
        provider._client = SimpleNamespace(
            chat=SimpleNamespace(completions=completions_api)
        )
        provider.model = "MiniMax-M2.7"
        provider.temperature = 0.0
        provider.max_tokens = 1024
        provider.workers = 1
        provider.network_access = False
        provider.request_count = 0
        provider.response_count = 0
        provider.batch_sizes = []
        attempted = []
        resolved = []

        def reject_first(index, completion):
            resolved.append((index, completion))
            raise entrypoint.V2TeacherGateError("injected canary rejection")

        with self.assertRaises(entrypoint.V2TeacherGateError):
            asyncio.run(
                provider.complete_many(
                    [("system", "user")] * 3,
                    before_attempt=attempted.append,
                    on_completion=reject_first,
                    strict_sequential=True,
                )
            )
        self.assertEqual(attempted, [0])
        self.assertEqual([index for index, _ in resolved], [0])
        self.assertEqual(completions_api.calls, 1)
        self.assertEqual(provider.request_count, 1)
        self.assertEqual(provider.response_count, 1)
        self.assertEqual(provider.batch_sizes, [3])

    def test_reported_model_mismatch_stops_after_canary_without_downstream(self):
        class MismatchedProvider:
            model = "MiniMax-M2.7"

            def __init__(self):
                self.request_count = 0
                self.response_count = 0
                self.network_access = False
                self.batch_sizes = []

            async def complete_many(
                self,
                prompts,
                *,
                before_attempt=None,
                on_completion=None,
                strict_sequential=False,
            ):
                self.batch_sizes.append(len(prompts))
                self.assert_strict = strict_sequential
                values = []
                for index, _ in enumerate(prompts):
                    if before_attempt is not None:
                        before_attempt(index)
                    self.request_count += 1
                    self.network_access = True
                    self.response_count += 1
                    completion = entrypoint.TeacherCompletion(
                        raw_response=(
                            '{"action":"hold","intensity":0,'
                            '"reasoning":"private model mismatch canary"}'
                        ),
                        reported_model="MiniMax-M2.7-mutated",
                        reported_model_raw="MiniMax-M2.7-mutated",
                        input_tokens=11,
                        output_tokens=7,
                        response_id="mismatch-response-id",
                    )
                    values.append(completion)
                    if on_completion is not None:
                        on_completion(index, completion)
                return values

            async def aclose(self):
                return None

        provider = MismatchedProvider()
        out = self.root / "model-mismatch"
        with mock.patch.dict(
            os.environ, self.ENDPOINT_ENV, clear=False
        ), mock.patch.object(
            entrypoint, "_build_openai_provider", return_value=provider
        ), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                entrypoint.main(_pilot_args(out, live=True))
        self.assertEqual(raised.exception.code, 1)
        self.assertTrue(provider.assert_strict)
        self.assertEqual(provider.batch_sizes, [162])
        self.assertEqual(provider.request_count, 1)
        self.assertEqual(provider.response_count, 1)

        run_dir = _single_run(out)
        manifest = _read_json(run_dir / "run_manifest.json")
        teacher = manifest["v2_attention_market"]["teacher_samples"]
        gate = manifest["v2_attention_market"]["teacher_acceptance_gate"]
        public_rows = _read_jsonl(run_dir / "teacher_samples.jsonl")
        private_path = run_dir / "private_teacher_records.jsonl"
        private_rows = _read_jsonl(private_path)
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["failure_stage"], "provider_setup")
        self.assertEqual(teacher["attempted"], 1)
        self.assertEqual(teacher["resolved"], 1)
        self.assertEqual(teacher["valid"], 0)
        self.assertEqual(teacher["failed"], 1)
        self.assertEqual(teacher["skipped"], 161)
        self.assertEqual(teacher["honest_n_teacher_samples"], 0)
        self.assertEqual(manifest["honest_n_teacher_samples"], 0)
        self.assertEqual(gate["status"], "failed")
        self.assertEqual(gate["canary_status"], "failed")
        self.assertEqual(gate["reason_codes"], ["reported_model_mismatch"])
        self.assertFalse(gate["student_and_market_released"])
        self.assertEqual(len(public_rows), 1)
        self.assertEqual(public_rows[0]["status"], "failed")
        self.assertEqual(
            public_rows[0]["failure_code"], "reported_model_mismatch"
        )
        self.assertNotIn(
            "finish_reason",
            public_rows[0],
            "the frozen v1 public-row schema must not acquire v3 fields",
        )
        self.assertEqual(len(private_rows), 1)
        self.assertEqual(stat.S_IMODE(private_path.stat().st_mode), 0o600)
        for forbidden in (
            "aggregated_dataset.json",
            "linear_student.json",
            "mlp_student.json",
            "student_model_envelope.json",
            "student_evaluation.json",
            "market_2x2_summary.json",
            "v2_attention_market_summary.json",
        ):
            self.assertFalse((run_dir / forbidden).exists(), forbidden)
        self.assertEqual(list(run_dir.glob("market_*_seed_*.json")), [])
        self.assertEqual(list(run_dir.glob("market_rounds_*_seed_*.jsonl")), [])

    def test_valid_canary_then_invalid_second_response_stops_at_two(self):
        from nmsim import v2_attention

        valid_canary = v2_attention.fake_test_teacher(
            v2_attention.generate_state_design(
                54, 20260811, study_id="v2-attention-market"
            )[0],
            0,
        )

        class SecondInvalidProvider:
            model = "MiniMax-M2.7"

            def __init__(self):
                self.request_count = 0
                self.response_count = 0
                self.network_access = False
                self.batch_sizes = []

            async def complete_many(
                self, prompts, *, before_attempt=None, on_completion=None,
                strict_sequential=False,
            ):
                self.batch_sizes.append(len(prompts))
                self.assert_strict = strict_sequential
                values = []
                for index, _ in enumerate(prompts):
                    if before_attempt is not None:
                        before_attempt(index)
                    self.request_count += 1
                    self.response_count += 1
                    self.network_access = True
                    completion = entrypoint.TeacherCompletion(
                        raw_response=(
                            valid_canary if index == 0 else '{"action":"invalid"}'
                        ),
                        reported_model="MiniMax-M2.7",
                        reported_model_raw="MiniMax-M2.7",
                        input_tokens=1,
                        output_tokens=1,
                        response_id="second-invalid-{}".format(index),
                    )
                    values.append(completion)
                    if on_completion is not None:
                        on_completion(index, completion)
                return values

            async def aclose(self):
                return None

        provider = SecondInvalidProvider()
        out = self.root / "second-invalid"
        with mock.patch.dict(
            os.environ, self.ENDPOINT_ENV, clear=False
        ), mock.patch.object(
            entrypoint, "_build_openai_provider", return_value=provider
        ), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                entrypoint.main(_pilot_args(out, live=True))
        self.assertEqual(raised.exception.code, 1)
        self.assertTrue(provider.assert_strict)
        self.assertEqual(provider.request_count, 2)
        run_dir = _single_run(out)
        manifest = _read_json(run_dir / "run_manifest.json")
        teacher = manifest["v2_attention_market"]["teacher_samples"]
        gate = manifest["v2_attention_market"]["teacher_acceptance_gate"]
        self.assertEqual(teacher["attempted"], 2)
        self.assertEqual(teacher["valid"], 1)
        self.assertEqual(teacher["failed"], 1)
        self.assertEqual(teacher["skipped"], 160)
        self.assertEqual(gate["canary_status"], "passed")
        self.assertEqual(gate["status"], "failed")
        self.assertEqual(gate["reason_codes"], ["teacher_response_invalid"])
        self.assertFalse((run_dir / "aggregated_dataset.json").exists())
        self.assertFalse((run_dir / "market_2x2_summary.json").exists())

    def test_teacher_gate_requires_all_162_rows_and_three_per_state(self):
        from nmsim import v2_attention

        args = entrypoint.build_argparser().parse_args(
            _pilot_args(self.root / "unused")
        )
        observations = v2_attention.generate_state_design(
            54, 20260811, study_id="v2-attention-market"
        )
        plan = entrypoint._sample_plan(observations, 3)
        public_rows = [
            {
                "sample_id": item["sample_id"],
                "state_id": item["observation"].state_id,
                "status": "valid",
                "model_requested": "MiniMax-M2.7",
                "reported_model": "MiniMax-M2.7",
            }
            for item in plan
        ]
        passed = entrypoint._teacher_gate_result(
            args=args,
            plan=plan,
            public_rows=public_rows,
            reported_models=["MiniMax-M2.7"] * 162,
        )
        self.assertEqual(passed["status"], "passed")
        self.assertEqual(passed["planned_samples"], 162)
        self.assertEqual(passed["resolved_samples"], 162)
        self.assertEqual(passed["valid_samples"], 162)
        self.assertEqual(passed["planned_states"], 54)
        self.assertEqual(passed["states_with_exact_required_replicates"], 54)
        self.assertEqual(passed["required_valid_replicates_per_state"], 3)
        self.assertTrue(passed["student_and_market_released"])

        wrong_attempted = entrypoint._teacher_gate_result(
            args=args,
            plan=plan,
            public_rows=public_rows,
            reported_models=["MiniMax-M2.7"] * 162,
            attempted_samples=161,
        )
        self.assertEqual(wrong_attempted["status"], "failed")
        self.assertIn(
            "not_all_planned_samples_attempted",
            wrong_attempted["reason_codes"],
        )

        failed = entrypoint._teacher_gate_result(
            args=args,
            plan=plan,
            public_rows=public_rows[:-1],
            reported_models=["MiniMax-M2.7"] * 161,
        )
        self.assertEqual(failed["status"], "failed")
        self.assertIn(
            "not_all_planned_samples_resolved", failed["reason_codes"]
        )
        self.assertIn("not_all_planned_samples_valid", failed["reason_codes"])
        self.assertIn(
            "state_replicate_coverage_incomplete", failed["reason_codes"]
        )
        self.assertEqual(failed["states_with_exact_required_replicates"], 53)
        self.assertFalse(failed["student_and_market_released"])


class V2ConfirmedGatewayAliasPilotTests(unittest.TestCase):
    ENDPOINT_ENV = V2FrozenTeacherPilotTests.ENDPOINT_ENV
    PROFILE = (
        entrypoint.MINIMAX_M27_REQUEST_HIGGSAI_REPORTED_JOINT54X3_PILOT
    )

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)

    def test_v2_profile_keeps_requested_and_reported_identity_separate(self):
        out = self.root / "v2-dry"
        with mock.patch.dict(
            os.environ, self.ENDPOINT_ENV, clear=False
        ), mock.patch.object(
            entrypoint,
            "_build_openai_provider",
            side_effect=AssertionError("v2 dry-run constructed provider"),
        ) as build_provider, mock.patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError("v2 dry-run opened socket"),
        ) as network, redirect_stdout(io.StringIO()):
            entrypoint.main(_pilot_args(out, profile=self.PROFILE))
        build_provider.assert_not_called()
        network.assert_not_called()
        run_dir = _single_run(out)
        manifest = _read_json(run_dir / "run_manifest.json")
        summary = _read_json(run_dir / "dry_run_summary.json")
        profile = entrypoint.pilot_profile_descriptor(self.PROFILE)
        self.assertEqual(profile["model_requested"], "MiniMax-M2.7")
        self.assertEqual(profile["required_reported_model"], "HiggsAI")
        self.assertEqual(
            profile["teacher_acceptance_gate"]["required_unique_reported_models"],
            ["HiggsAI"],
        )
        self.assertFalse(
            profile["model_identity_semantics"][
                "reported_alias_is_underlying_serving_weights_identity_claim"
            ]
        )
        self.assertEqual(
            profile["predecessor_failed_run"]["run_id"],
            "v2-teacher-pilot-live-20260812-a1",
        )
        self.assertTrue(
            profile["predecessor_failed_run"][
                "reuse_supplement_or_merge_forbidden"
            ]
        )
        self.assertEqual(summary["model_requested"], "MiniMax-M2.7")
        self.assertEqual(summary["pilot_profile"], profile)
        request = manifest["v2_config_identities"]["model_request_config"]
        self.assertEqual(request["model_requested"], "MiniMax-M2.7")
        self.assertEqual(request["required_reported_model"], "HiggsAI")
        self.assertNotIn(
            "response_termination_contract",
            request,
            "the frozen v2 model-request projection must not acquire v3 fields",
        )
        pilot_input = next(
            row for row in manifest["inputs"]
            if row["label"] == "v2_teacher_pilot_protocol"
        )
        self.assertTrue(pilot_input["path"].endswith("V2_TEACHER_PILOT_V2.md"))

    def test_v1_profile_remains_exact_minimax_reported_contract(self):
        v1 = entrypoint.pilot_profile_descriptor(
            entrypoint.MINIMAX_M27_JOINT54X3_PILOT
        )
        self.assertEqual(
            entrypoint.stable_hash(v1),
            "1228cd39c038771a916fb747e1e767218874232ddb1bad4f16d1f3d5a2712d1a",
        )
        self.assertEqual(v1["schema_version"], "v2_teacher_pilot_profile/0.1")
        self.assertEqual(v1["model_requested"], "MiniMax-M2.7")
        self.assertEqual(v1["required_reported_model"], "MiniMax-M2.7")
        self.assertNotIn("predecessor_failed_run", v1)
        self.assertNotIn("model_identity_semantics", v1)
        v2 = entrypoint.pilot_profile_descriptor(self.PROFILE)
        self.assertEqual(
            entrypoint.stable_hash(v2),
            "3f586f02974265e243bc49a0f925eb7e274a2554b4aaee6e706a354becacfebc",
        )
        v3 = entrypoint.pilot_profile_descriptor(
            entrypoint.MINIMAX_M27_HIGGSAI_FINISH_AUDIT_JOINT54X3_PILOT
        )
        self.assertEqual(
            entrypoint.stable_hash(v3),
            "c042e41f2263f7c3ee093f7c6b258ee8972edb3a4db44bcae725cc6e0e00aa3e",
        )
        v4 = entrypoint.pilot_profile_descriptor(
            entrypoint.MINIMAX_M27_HIGGSAI_FINISH_AUDIT_EXTERNAL_JOINT54X3_PILOT
        )
        self.assertEqual(
            entrypoint.stable_hash(v4),
            "5851865c1a22ec2d772524db55e2f57d9bd7e101a02eb7be692bd5f42da5f9b5",
        )

    def test_v1_and_v2_have_distinct_named_config_identities(self):
        from nmsim import v2_attention

        repo_root = Path(entrypoint.__file__).resolve().parents[1]
        with mock.patch.dict(os.environ, self.ENDPOINT_ENV, clear=False):
            v1_args = entrypoint.build_argparser().parse_args(
                _pilot_args(self.root / "v1-identities")
            )
            v2_args = entrypoint.build_argparser().parse_args(
                _pilot_args(
                    self.root / "v2-identities", profile=self.PROFILE
                )
            )
            entrypoint._validate_args(v1_args)
            entrypoint._validate_args(v2_args)
            observations = v2_attention.generate_state_design(
                54, 20260811, study_id="v2-attention-market"
            )
            v1 = entrypoint.build_v2_identities(
                v1_args, observations, repo_root=repo_root
            )
            v2 = entrypoint.build_v2_identities(
                v2_args, observations, repo_root=repo_root
            )
        self.assertEqual(v1["state_design_hash"], v2["state_design_hash"])
        self.assertNotEqual(
            v1["v2_scientific_config_hash"],
            v2["v2_scientific_config_hash"],
        )
        self.assertNotEqual(
            v1["v2_model_request_config_hash"],
            v2["v2_model_request_config_hash"],
        )
        self.assertNotEqual(
            v1["v2_full_effective_config_hash"],
            v2["v2_full_effective_config_hash"],
        )

    def test_v2_gate_accepts_only_higgsai_reported_alias(self):
        from nmsim import v2_attention

        args = entrypoint.build_argparser().parse_args(
            _pilot_args(self.root / "unused", profile=self.PROFILE)
        )
        observations = v2_attention.generate_state_design(
            54, 20260811, study_id="v2-attention-market"
        )
        plan = entrypoint._sample_plan(observations, 3)
        rows = [
            {
                "state_id": item["observation"].state_id,
                "status": "valid",
                "model_requested": "MiniMax-M2.7",
                "reported_model": "HiggsAI",
            }
            for item in plan
        ]
        accepted = entrypoint._teacher_gate_result(
            args=args,
            plan=plan,
            public_rows=rows,
            reported_models=["HiggsAI"] * 162,
            attempted_samples=162,
        )
        rejected = entrypoint._teacher_gate_result(
            args=args,
            plan=plan,
            public_rows=rows,
            reported_models=["MiniMax-M2.7"] * 162,
            attempted_samples=162,
        )
        self.assertEqual(accepted["status"], "passed")
        self.assertEqual(accepted["required_reported_model"], "HiggsAI")
        self.assertEqual(rejected["status"], "failed")
        self.assertIn(
            "reported_model_identity_not_unique_exact_match",
            rejected["reason_codes"],
        )
        invalid_alias_sets = (
            [],
            ["MiniMax-M2.7"],
            ["higgsai"],
            ["HiggsAI "],
            ["HiggsAI-v2"],
            ["HiggsAI", "Other"],
        )
        for aliases in invalid_alias_sets:
            with self.subTest(reported_models=aliases):
                invalid_alias = entrypoint._teacher_gate_result(
                    args=args,
                    plan=plan,
                    public_rows=rows,
                    reported_models=aliases,
                    attempted_samples=162,
                )
                self.assertEqual(invalid_alias["status"], "failed")
                self.assertIn(
                    "reported_model_identity_not_unique_exact_match",
                    invalid_alias["reason_codes"],
                )
        duplicate_exact = entrypoint._teacher_gate_result(
            args=args,
            plan=plan,
            public_rows=rows,
            reported_models=["HiggsAI", "HiggsAI"],
            attempted_samples=162,
        )
        self.assertEqual(duplicate_exact["status"], "passed")
        for invalid_value in (None, "HiggsAI"):
            invalid_rows = [dict(row) for row in rows]
            invalid_rows[0]["model_requested"] = invalid_value
            invalid = entrypoint._teacher_gate_result(
                args=args,
                plan=plan,
                public_rows=invalid_rows,
                reported_models=["HiggsAI"] * 162,
                attempted_samples=162,
            )
            self.assertEqual(invalid["status"], "failed")
            self.assertIn(
                "requested_model_identity_not_exact_match",
                invalid["reason_codes"],
            )
        invalid_reported_rows = [dict(row) for row in rows]
        invalid_reported_rows[0]["reported_model"] = "MiniMax-M2.7"
        invalid_reported = entrypoint._teacher_gate_result(
            args=args,
            plan=plan,
            public_rows=invalid_reported_rows,
            reported_models=["HiggsAI"] * 162,
            attempted_samples=162,
        )
        self.assertEqual(invalid_reported["status"], "failed")
        self.assertIn(
            "reported_model_row_identity_not_exact_match",
            invalid_reported["reason_codes"],
        )

    def test_v2_run_ids_are_frozen_and_cannot_reuse_a1(self):
        invalid_ids = (
            "v2-teacher-pilot-live-20260812-a1",
            "v2-teacher-pilot-live-20260812-a3",
        )
        for run_id in invalid_ids:
            with self.subTest(run_id=run_id):
                out = self.root / run_id
                argv = _pilot_args(out, live=True, profile=self.PROFILE)
                argv[argv.index("--run-id") + 1] = run_id
                with mock.patch.dict(
                    os.environ, self.ENDPOINT_ENV, clear=False
                ), mock.patch.object(
                    entrypoint,
                    "_build_openai_provider",
                    side_effect=AssertionError("invalid run id built provider"),
                ) as provider, redirect_stdout(io.StringIO()), redirect_stderr(
                    io.StringIO()
                ):
                    with self.assertRaises(SystemExit) as raised:
                        entrypoint.main(argv)
                self.assertEqual(raised.exception.code, 2)
                provider.assert_not_called()
                manifest = _read_json(_single_run(out) / "run_manifest.json")
                self.assertEqual(manifest["failure_stage"], "config_validation")
                self.assertEqual(
                    manifest["completion"]["provider_calls"]["attempted"], 0
                )

    def test_existing_a1_directory_is_not_modified_on_reuse_attempt(self):
        out = self.root / "existing-a1"
        run_dir = out / "runs" / "v2-teacher-pilot-live-20260812-a1"
        run_dir.mkdir(parents=True)
        sentinel = run_dir / "immutable-sentinel.bin"
        sentinel.write_bytes(b"permanent failed a1\x00")
        before_hash = _sha256(sentinel)
        before_entries = sorted(path.name for path in run_dir.iterdir())
        argv = _pilot_args(out, live=True, profile=self.PROFILE)
        argv[argv.index("--run-id") + 1] = run_dir.name
        with mock.patch.dict(
            os.environ, self.ENDPOINT_ENV, clear=False
        ), mock.patch.object(
            entrypoint,
            "_build_openai_provider",
            side_effect=AssertionError("a1 reuse built provider"),
        ) as provider, redirect_stdout(io.StringIO()), redirect_stderr(
            io.StringIO()
        ):
            with self.assertRaises(SystemExit) as raised:
                entrypoint.main(argv)
        self.assertEqual(raised.exception.code, 2)
        provider.assert_not_called()
        self.assertEqual(_sha256(sentinel), before_hash)
        self.assertEqual(
            sorted(path.name for path in run_dir.iterdir()), before_entries
        )

    def test_v2_valid_canary_then_invalid_second_response_stops_at_two(self):
        from nmsim import v2_attention

        valid_canary = v2_attention.fake_test_teacher(
            v2_attention.generate_state_design(
                54, 20260811, study_id="v2-attention-market"
            )[0],
            0,
        )

        class SecondInvalidAliasProvider:
            model = "MiniMax-M2.7"

            def __init__(self):
                self.request_count = 0
                self.response_count = 0
                self.network_access = False
                self.batch_sizes = []

            async def complete_many(
                self,
                prompts,
                *,
                before_attempt=None,
                on_completion=None,
                strict_sequential=False,
            ):
                self.batch_sizes.append(len(prompts))
                self.strict_sequential = strict_sequential
                values = []
                for index, _ in enumerate(prompts):
                    if before_attempt is not None:
                        before_attempt(index)
                    self.request_count += 1
                    self.response_count += 1
                    self.network_access = True
                    completion = entrypoint.TeacherCompletion(
                        raw_response=(
                            valid_canary
                            if index == 0
                            else '{"action":"invalid"}'
                        ),
                        reported_model="HiggsAI",
                        reported_model_raw="HiggsAI",
                        input_tokens=1,
                        output_tokens=1,
                        response_id="v2-second-invalid-{}".format(index),
                    )
                    values.append(completion)
                    if on_completion is not None:
                        on_completion(index, completion)
                return values

            async def aclose(self):
                return None

        provider = SecondInvalidAliasProvider()
        out = self.root / "v2-second-invalid"
        with mock.patch.dict(
            os.environ, self.ENDPOINT_ENV, clear=False
        ), mock.patch.object(
            entrypoint, "_build_openai_provider", return_value=provider
        ), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                entrypoint.main(
                    _pilot_args(out, live=True, profile=self.PROFILE)
                )
        self.assertEqual(raised.exception.code, 1)
        self.assertTrue(provider.strict_sequential)
        self.assertEqual(provider.request_count, 2)
        run_dir = _single_run(out)
        manifest = _read_json(run_dir / "run_manifest.json")
        rows = _read_jsonl(run_dir / "teacher_samples.jsonl")
        teacher = manifest["v2_attention_market"]["teacher_samples"]
        gate = manifest["v2_attention_market"]["teacher_acceptance_gate"]
        self.assertEqual(teacher["attempted"], 2)
        self.assertEqual(teacher["valid"], 1)
        self.assertEqual(teacher["failed"], 1)
        self.assertEqual(teacher["skipped"], 160)
        self.assertEqual(gate["canary_status"], "passed")
        self.assertEqual(gate["status"], "failed")
        self.assertEqual(gate["reason_codes"], ["teacher_response_invalid"])
        self.assertFalse(gate["student_and_market_released"])
        self.assertEqual(
            {row["model_requested"] for row in rows}, {"MiniMax-M2.7"}
        )
        self.assertEqual({row["reported_model"] for row in rows}, {"HiggsAI"})
        self.assertTrue(
            all("finish_reason" not in row for row in rows),
            "the frozen v2 public-row schema must not acquire v3 fields",
        )
        private_rows = _read_jsonl(run_dir / "private_teacher_records.jsonl")
        self.assertTrue(
            all(
                "provider_finish_reason_raw" not in row
                and "provider_sdk_response_json" not in row
                for row in private_rows
            ),
            "the frozen v2 private-row schema must not acquire v3 fields",
        )
        self.assertEqual(
            manifest["v2_attention_market"]["reported_models"], ["HiggsAI"]
        )
        self.assertFalse((run_dir / "aggregated_dataset.json").exists())
        self.assertFalse((run_dir / "market_2x2_summary.json").exists())

    def test_reports_label_requested_and_reported_without_resolving_weights(self):
        summary = {
            "run_id": "identity-report-test",
            "provider": "openai",
            "model_requested": "MiniMax-M2.7",
            "model_resolved": "HiggsAI",
            "reported_models": ["HiggsAI"],
            "model_identity": {
                "underlying_serving_weights_independently_verified": False,
            },
        }
        markdown = entrypoint.render_markdown_report(summary)
        rendered_html = entrypoint.render_html_report(summary)
        for rendered in (markdown, rendered_html):
            self.assertIn("Requested model", rendered)
            self.assertIn("MiniMax-M2.7", rendered)
            self.assertIn("reported", rendered.lower())
            self.assertIn("HiggsAI", rendered)
            self.assertIn("underlying", rendered.lower())
            self.assertIn("False", rendered)
        self.assertNotIn("Provider: `openai`; model: `HiggsAI`", markdown)


class V3FinishAuditPilotTests(unittest.TestCase):
    ENDPOINT_ENV = V2FrozenTeacherPilotTests.ENDPOINT_ENV
    PROFILE = entrypoint.MINIMAX_M27_HIGGSAI_FINISH_AUDIT_JOINT54X3_PILOT

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)

    def test_v3_descriptor_and_dry_run_are_frozen_and_zero_network(self):
        out = self.root / "v3-dry"
        with mock.patch.dict(
            os.environ, self.ENDPOINT_ENV, clear=False
        ), mock.patch.object(
            entrypoint,
            "_build_openai_provider",
            side_effect=AssertionError("v3 dry-run constructed provider"),
        ) as provider, mock.patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError("v3 dry-run opened socket"),
        ) as network, redirect_stdout(io.StringIO()):
            entrypoint.main(_pilot_args(out, profile=self.PROFILE))
        provider.assert_not_called()
        network.assert_not_called()
        run_dir = _single_run(out)
        manifest = _read_json(run_dir / "run_manifest.json")
        summary = _read_json(run_dir / "dry_run_summary.json")
        profile = entrypoint.pilot_profile_descriptor(self.PROFILE)
        self.assertEqual(
            entrypoint.stable_hash(profile),
            "c042e41f2263f7c3ee093f7c6b258ee8972edb3a4db44bcae725cc6e0e00aa3e",
        )
        self.assertEqual(profile["schema_version"], "v2_teacher_pilot_profile/0.3")
        self.assertEqual(profile["max_tokens"], 4096)
        self.assertEqual(profile["states"], 54)
        self.assertEqual(profile["replicates_per_state"], 3)
        self.assertEqual(profile["planned_requests"], 162)
        self.assertEqual(profile["required_reported_model"], "HiggsAI")
        self.assertEqual(
            profile["response_termination_contract"]["required_finish_reason"],
            "stop",
        )
        self.assertTrue(
            profile["response_termination_contract"][
                "reasoning_content_is_never_a_decision_source"
            ]
        )
        self.assertEqual(
            [row["run_id"] for row in profile["predecessor_failed_runs"]],
            [
                "v2-teacher-pilot-live-20260812-a1",
                "v2-teacher-pilot-live-20260812-a2",
            ],
        )
        self.assertEqual(
            profile["predecessor_failed_runs"][1]["run_manifest_sha256"],
            "1dc925131c31ed24e85df891c9b6bcbb6a879082e0db6ef2f38b02c5007dc6c8",
        )
        self.assertEqual(summary["provider_calls"], 0)
        self.assertFalse(summary["network_access"])
        request = manifest["v2_config_identities"]["model_request_config"]
        self.assertEqual(
            request["schema_version"],
            entrypoint.FINISH_AUDIT_REQUEST_SCHEMA_VERSION,
        )
        self.assertEqual(request["max_tokens"], 4096)
        self.assertEqual(
            request["response_termination_contract"],
            profile["response_termination_contract"],
        )
        v2_profile = entrypoint.pilot_profile_descriptor(
            entrypoint.MINIMAX_M27_REQUEST_HIGGSAI_REPORTED_JOINT54X3_PILOT
        )
        self.assertEqual(
            request["planned_sample_order_hash"],
            v2_profile["planned_sample_order_hash"],
        )
        self.assertEqual(
            request["canary_sample_id"], v2_profile["canary_sample_id"]
        )
        self.assertEqual(
            profile["planned_sample_order_hash"],
            v2_profile["planned_sample_order_hash"],
        )
        self.assertEqual(
            profile["canary_sample_id"], v2_profile["canary_sample_id"]
        )
        pilot_input = next(
            row for row in manifest["inputs"]
            if row["label"] == "v2_teacher_pilot_protocol"
        )
        self.assertTrue(pilot_input["path"].endswith("V2_TEACHER_PILOT_V3.md"))

    def test_v3_keeps_v2_sample_ids_order_and_canary_but_versions_rows(self):
        from nmsim import v2_attention

        observations = v2_attention.generate_state_design(
            54, 20260811, study_id="v2-attention-market"
        )
        sample_ids = [
            item["sample_id"]
            for item in entrypoint._sample_plan(observations, 3)
        ]
        v2_profile = entrypoint.pilot_profile_descriptor(
            entrypoint.MINIMAX_M27_REQUEST_HIGGSAI_REPORTED_JOINT54X3_PILOT
        )
        v3_profile = entrypoint.pilot_profile_descriptor(self.PROFILE)
        self.assertEqual(len(sample_ids), 162)
        self.assertEqual(sample_ids[0], v2_profile["canary_sample_id"])
        self.assertEqual(sample_ids[0], v3_profile["canary_sample_id"])
        self.assertEqual(
            entrypoint.stable_hash(sample_ids),
            v2_profile["planned_sample_order_hash"],
        )
        self.assertEqual(
            entrypoint.stable_hash(sample_ids),
            v3_profile["planned_sample_order_hash"],
        )
        self.assertEqual(
            entrypoint._teacher_request_schema_version(
                entrypoint.MINIMAX_M27_REQUEST_HIGGSAI_REPORTED_JOINT54X3_PILOT
            ),
            entrypoint.MODEL_REQUEST_SCHEMA_VERSION,
        )
        self.assertEqual(
            entrypoint._teacher_request_schema_version(self.PROFILE),
            entrypoint.FINISH_AUDIT_REQUEST_SCHEMA_VERSION,
        )

    def test_v3_mutations_and_predecessor_run_ids_fail_before_provider(self):
        cases = (
            ("max-1024", {"max_tokens": 1024}, None),
            ("max-4095", {"max_tokens": 4095}, None),
            ("max-4097", {"max_tokens": 4097}, None),
            ("a2", {}, "v2-teacher-pilot-live-20260812-a2"),
            ("a4", {}, "v2-teacher-pilot-live-20260813-a4"),
        )
        for name, overrides, run_id in cases:
            with self.subTest(name=name):
                out = self.root / name
                argv = _pilot_args(
                    out, live=True, profile=self.PROFILE, **overrides
                )
                if run_id is not None:
                    argv[argv.index("--run-id") + 1] = run_id
                with mock.patch.dict(
                    os.environ, self.ENDPOINT_ENV, clear=False
                ), mock.patch.object(
                    entrypoint,
                    "_build_openai_provider",
                    side_effect=AssertionError("invalid v3 built provider"),
                ) as provider, redirect_stdout(io.StringIO()), redirect_stderr(
                    io.StringIO()
                ):
                    with self.assertRaises(SystemExit) as raised:
                        entrypoint.main(argv)
                self.assertEqual(raised.exception.code, 2)
                provider.assert_not_called()
                manifest = _read_json(_single_run(out) / "run_manifest.json")
                self.assertEqual(manifest["failure_stage"], "config_validation")
                self.assertEqual(
                    manifest["completion"]["provider_calls"]["attempted"], 0
                )

    def test_v3_gate_requires_stop_for_every_row(self):
        from nmsim import v2_attention

        args = entrypoint.build_argparser().parse_args(
            _pilot_args(self.root / "gate", profile=self.PROFILE)
        )
        observations = v2_attention.generate_state_design(
            54, 20260811, study_id="v2-attention-market"
        )
        plan = entrypoint._sample_plan(observations, 3)
        rows = [
            {
                "state_id": item["observation"].state_id,
                "status": "valid",
                "model_requested": "MiniMax-M2.7",
                "reported_model": "HiggsAI",
                "finish_reason": "stop",
            }
            for item in plan
        ]
        passed = entrypoint._teacher_gate_result(
            args=args,
            plan=plan,
            public_rows=rows,
            reported_models=["HiggsAI"] * 162,
            attempted_samples=162,
        )
        self.assertEqual(passed["status"], "passed")
        self.assertEqual(passed["required_finish_reason"], "stop")
        for value in (None, "length", "content_filter", "tool_calls"):
            with self.subTest(finish_reason=value):
                invalid_rows = [dict(row) for row in rows]
                invalid_rows[0]["finish_reason"] = value
                failed = entrypoint._teacher_gate_result(
                    args=args,
                    plan=plan,
                    public_rows=invalid_rows,
                    reported_models=["HiggsAI"] * 162,
                    attempted_samples=162,
                )
                self.assertEqual(failed["status"], "failed")
                self.assertIn(
                    "finish_reason_not_exact_match", failed["reason_codes"]
                )

    def test_v3_all_162_stop_rows_release_distillation_only_after_gate(self):
        class AllStopProvider:
            model = "MiniMax-M2.7"

            def __init__(self):
                self.request_count = 0
                self.response_count = 0
                self.network_access = False
                self.batch_sizes = []
                self.strict_sequential = None

            async def complete_many(
                self,
                prompts,
                *,
                before_attempt=None,
                on_completion=None,
                strict_sequential=False,
            ):
                self.batch_sizes.append(len(prompts))
                self.strict_sequential = strict_sequential
                values = []
                for index, _ in enumerate(prompts):
                    if before_attempt is not None:
                        before_attempt(index)
                    self.request_count += 1
                    self.response_count += 1
                    self.network_access = True
                    completion = entrypoint.TeacherCompletion(
                        raw_response=(
                            '{"action":"hold","intensity":0,'
                            '"reasoning":"private synthetic stop fixture"}'
                        ),
                        reported_model="HiggsAI",
                        reported_model_raw="HiggsAI",
                        input_tokens=7,
                        output_tokens=5,
                        response_id="all-stop-{}".format(index),
                        finish_reason="stop",
                        finish_reason_raw="stop",
                    )
                    values.append(completion)
                    if on_completion is not None:
                        on_completion(index, completion)
                return values

            async def aclose(self):
                return None

        class DownstreamReached(RuntimeError):
            pass

        provider = AllStopProvider()
        out = self.root / "all-162-stop"
        distillation = mock.Mock(side_effect=DownstreamReached("sentinel"))
        market = mock.Mock(
            side_effect=AssertionError("market ran past distillation sentinel")
        )
        with mock.patch.dict(
            os.environ, self.ENDPOINT_ENV, clear=False
        ), mock.patch.object(
            entrypoint, "_build_openai_provider", return_value=provider
        ), mock.patch.object(
            entrypoint, "run_distillation_phase", distillation
        ), mock.patch.object(
            entrypoint, "run_market_phase", market
        ), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                entrypoint.main(
                    _pilot_args(out, live=True, profile=self.PROFILE)
                )
        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(provider.batch_sizes, [162])
        self.assertTrue(provider.strict_sequential)
        self.assertEqual(provider.request_count, 162)
        self.assertEqual(provider.response_count, 162)
        distillation.assert_called_once()
        market.assert_not_called()

        run_dir = _single_run(out)
        manifest = _read_json(run_dir / "run_manifest.json")
        rows = _read_jsonl(run_dir / "teacher_samples.jsonl")
        teacher = manifest["v2_attention_market"]["teacher_samples"]
        gate = manifest["v2_attention_market"]["teacher_acceptance_gate"]
        self.assertEqual(len(rows), 162)
        self.assertEqual(
            {row["schema_version"] for row in rows},
            {entrypoint.FINISH_AUDIT_REQUEST_SCHEMA_VERSION},
        )
        self.assertEqual({row["finish_reason"] for row in rows}, {"stop"})
        self.assertEqual({row["status"] for row in rows}, {"valid"})
        self.assertEqual(teacher["attempted"], 162)
        self.assertEqual(teacher["resolved"], 162)
        self.assertEqual(teacher["valid"], 162)
        self.assertEqual(teacher["failed"], 0)
        self.assertEqual(teacher["skipped"], 0)
        self.assertEqual(manifest["honest_n_teacher_samples"], 162)
        self.assertEqual(gate["status"], "passed")
        self.assertEqual(gate["canary_status"], "passed")
        self.assertEqual(gate["required_finish_reason"], "stop")
        self.assertTrue(gate["student_and_market_released"])

    def test_v3_length_canary_fails_closed_and_keeps_sdk_envelope_private(self):
        from nmsim import v2_attention

        valid = v2_attention.fake_test_teacher(
            v2_attention.generate_state_design(
                54, 20260811, study_id="v2-attention-market"
            )[0],
            0,
        )
        private_marker = "PRIVATE_SDK_ENVELOPE_MARKER"

        class LengthProvider:
            model = "MiniMax-M2.7"

            def __init__(self):
                self.request_count = 0
                self.response_count = 0
                self.network_access = False
                self.batch_sizes = []

            async def complete_many(
                self, prompts, *, before_attempt=None, on_completion=None,
                strict_sequential=False,
            ):
                self.batch_sizes.append(len(prompts))
                self.strict = strict_sequential
                if before_attempt is not None:
                    before_attempt(0)
                self.request_count = 1
                self.response_count = 1
                self.network_access = True
                completion = entrypoint.TeacherCompletion(
                    raw_response=valid,
                    reported_model="HiggsAI",
                    reported_model_raw="HiggsAI",
                    input_tokens=710,
                    output_tokens=4096,
                    response_id="v3-length-canary",
                    finish_reason="length",
                    finish_reason_raw="length",
                    provider_sdk_response_json=private_marker,
                )
                if on_completion is not None:
                    on_completion(0, completion)
                return [completion]

            async def aclose(self):
                return None

        provider = LengthProvider()
        out = self.root / "length-canary"
        with mock.patch.dict(
            os.environ, self.ENDPOINT_ENV, clear=False
        ), mock.patch.object(
            entrypoint, "_build_openai_provider", return_value=provider
        ), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                entrypoint.main(
                    _pilot_args(out, live=True, profile=self.PROFILE)
                )
        self.assertEqual(raised.exception.code, 1)
        self.assertTrue(provider.strict)
        self.assertEqual(provider.request_count, 1)
        run_dir = _single_run(out)
        manifest = _read_json(run_dir / "run_manifest.json")
        rows = _read_jsonl(run_dir / "teacher_samples.jsonl")
        private_path = run_dir / "private_teacher_records.jsonl"
        private_text = private_path.read_text(encoding="utf-8")
        self.assertEqual(rows[0]["failure_code"], "finish_reason_invalid")
        self.assertEqual(rows[0]["finish_reason"], "length")
        self.assertNotIn(private_marker, (run_dir / "teacher_samples.jsonl").read_text())
        self.assertNotIn(private_marker, (run_dir / "run_manifest.json").read_text())
        self.assertIn(private_marker, private_text)
        self.assertEqual(stat.S_IMODE(private_path.stat().st_mode), 0o600)
        teacher = manifest["v2_attention_market"]["teacher_samples"]
        gate = manifest["v2_attention_market"]["teacher_acceptance_gate"]
        self.assertEqual(teacher["attempted"], 1)
        self.assertEqual(teacher["valid"], 0)
        self.assertEqual(teacher["skipped"], 161)
        self.assertEqual(manifest["honest_n_teacher_samples"], 0)
        self.assertEqual(
            manifest["completion"]["parsing"]["attempted"], 0
        )
        self.assertEqual(manifest["completion"]["parsing"]["failed"], 0)
        attempts = manifest["completion"]["application_provider_attempts"]
        self.assertEqual(attempts["finish_reason_counts"], {"length": 1})
        self.assertEqual(attempts["missing_finish_reason_count"], 0)
        self.assertEqual(gate["reason_codes"], ["finish_reason_invalid"])
        self.assertFalse(gate["student_and_market_released"])
        self.assertFalse((run_dir / "aggregated_dataset.json").exists())
        self.assertFalse((run_dir / "market_2x2_summary.json").exists())

    def test_v3_predecessor_and_target_directories_are_o_excl_immutable(self):
        for run_id in (
            "v2-teacher-pilot-live-20260812-a2",
            "v2-teacher-pilot-live-20260813-a3",
        ):
            with self.subTest(run_id=run_id):
                out = self.root / run_id
                run_dir = out / "runs" / run_id
                run_dir.mkdir(parents=True)
                sentinel = run_dir / "immutable-sentinel.bin"
                sentinel.write_bytes(b"immutable-v3-boundary\x00")
                before_hash = _sha256(sentinel)
                before_entries = sorted(path.name for path in run_dir.iterdir())
                argv = _pilot_args(out, live=True, profile=self.PROFILE)
                argv[argv.index("--run-id") + 1] = run_id
                with mock.patch.dict(
                    os.environ, self.ENDPOINT_ENV, clear=False
                ), mock.patch.object(
                    entrypoint,
                    "_build_openai_provider",
                    side_effect=AssertionError("O_EXCL path built provider"),
                ) as provider, redirect_stdout(io.StringIO()), redirect_stderr(
                    io.StringIO()
                ):
                    with self.assertRaises((SystemExit, FileExistsError)) as raised:
                        entrypoint.main(argv)
                if isinstance(raised.exception, SystemExit):
                    self.assertEqual(raised.exception.code, 2)
                provider.assert_not_called()
                self.assertEqual(_sha256(sentinel), before_hash)
                self.assertEqual(
                    sorted(path.name for path in run_dir.iterdir()),
                    before_entries,
                )


class V4ExternalExecutionSuccessorTests(unittest.TestCase):
    ENDPOINT_ENV = V2FrozenTeacherPilotTests.ENDPOINT_ENV
    PROFILE = (
        entrypoint.MINIMAX_M27_HIGGSAI_FINISH_AUDIT_EXTERNAL_JOINT54X3_PILOT
    )

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)

    def _identities(self, profile: str, out_name: str) -> dict:
        from nmsim import v2_attention

        args = entrypoint.build_argparser().parse_args(
            _pilot_args(self.root / out_name, profile=profile)
        )
        entrypoint._validate_args(args)
        observations = v2_attention.generate_state_design(
            54, 20260811, study_id="v2-attention-market"
        )
        return entrypoint.build_v2_identities(
            args,
            observations,
            repo_root=Path(entrypoint.__file__).resolve().parents[1],
        )

    def test_v4_descriptor_and_dry_run_are_frozen_and_zero_network(self):
        out = self.root / "v4-dry"
        with mock.patch.dict(
            os.environ, self.ENDPOINT_ENV, clear=False
        ), mock.patch.object(
            entrypoint,
            "_build_openai_provider",
            side_effect=AssertionError("v4 dry-run constructed provider"),
        ) as provider, mock.patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError("v4 dry-run opened socket"),
        ) as network, redirect_stdout(io.StringIO()):
            entrypoint.main(_pilot_args(out, profile=self.PROFILE))
        provider.assert_not_called()
        network.assert_not_called()

        run_dir = _single_run(out)
        manifest = _read_json(run_dir / "run_manifest.json")
        summary = _read_json(run_dir / "dry_run_summary.json")
        profile = entrypoint.pilot_profile_descriptor(self.PROFILE)
        self.assertEqual(
            entrypoint.stable_hash(profile),
            "5851865c1a22ec2d772524db55e2f57d9bd7e101a02eb7be692bd5f42da5f9b5",
        )
        self.assertEqual(profile["schema_version"], "v2_teacher_pilot_profile/0.4")
        self.assertEqual(profile["successor_scope"], "execution_only")
        self.assertTrue(profile["external_network_required"])
        self.assertEqual(profile["planned_requests"], 162)
        self.assertEqual(profile["max_tokens"], 4096)
        self.assertEqual(profile["required_reported_model"], "HiggsAI")
        self.assertEqual(
            profile["response_termination_contract"]["required_finish_reason"],
            "stop",
        )
        self.assertEqual(
            [row["run_id"] for row in profile["predecessor_failed_runs"]],
            [
                "v2-teacher-pilot-live-20260812-a1",
                "v2-teacher-pilot-live-20260812-a2",
                "v2-teacher-pilot-live-20260813-a3",
            ],
        )
        self.assertEqual(
            profile["predecessor_failed_runs"][2]["run_manifest_sha256"],
            "ede68ce4d7c069feb2424884cf62f0f0d92546c56638c5e5550cb56f6ee326cf",
        )
        self.assertEqual(
            profile["required_run_ids"],
            {
                "dry_run": "v2-teacher-pilot-v4-dry-20260813-a1",
                "live": "v2-teacher-pilot-live-20260813-a4",
            },
        )
        self.assertEqual(summary["provider_calls"], 0)
        self.assertFalse(summary["network_access"])
        self.assertEqual(summary["planned_teacher_requests"], 162)
        self.assertEqual(summary["pilot_profile"], profile)
        request = manifest["v2_config_identities"]["model_request_config"]
        self.assertEqual(
            request["schema_version"],
            entrypoint.FINISH_AUDIT_REQUEST_SCHEMA_VERSION,
        )
        self.assertEqual(request["pilot_profile_id"], self.PROFILE)
        pilot_input = next(
            row for row in manifest["inputs"]
            if row["label"] == "v2_teacher_pilot_protocol"
        )
        self.assertTrue(pilot_input["path"].endswith("V2_TEACHER_PILOT_V4.md"))

    def test_v4_changes_provenance_only_not_frozen_request_or_science(self):
        from nmsim import v2_attention

        v3_id = entrypoint.MINIMAX_M27_HIGGSAI_FINISH_AUDIT_JOINT54X3_PILOT
        with mock.patch.dict(os.environ, self.ENDPOINT_ENV, clear=False):
            v3 = self._identities(v3_id, "v3-identities")
            v4 = self._identities(self.PROFILE, "v4-identities")

        v3_science = json.loads(json.dumps(v3["scientific_config"]))
        v4_science = json.loads(json.dumps(v4["scientific_config"]))
        self.assertEqual(
            v3_science["teacher_sampling"].pop("pilot_profile_id"), v3_id
        )
        self.assertEqual(
            v4_science["teacher_sampling"].pop("pilot_profile_id"), self.PROFILE
        )
        self.assertEqual(v4_science, v3_science)

        v3_request = json.loads(json.dumps(v3["model_request_config"]))
        v4_request = json.loads(json.dumps(v4["model_request_config"]))
        self.assertEqual(v3_request.pop("pilot_profile_id"), v3_id)
        self.assertEqual(v4_request.pop("pilot_profile_id"), self.PROFILE)
        self.assertEqual(v4_request, v3_request)
        self.assertNotEqual(
            v4["v2_scientific_config_hash"], v3["v2_scientific_config_hash"]
        )
        self.assertNotEqual(
            v4["v2_model_request_config_hash"],
            v3["v2_model_request_config_hash"],
        )
        self.assertNotEqual(
            v4["v2_execution_config_hash"], v3["v2_execution_config_hash"]
        )
        self.assertNotEqual(
            v4["v2_full_effective_config_hash"],
            v3["v2_full_effective_config_hash"],
        )

        observations = v2_attention.generate_state_design(
            54, 20260811, study_id="v2-attention-market"
        )
        prompt_hashes = [
            v2_attention.render_teacher_prompt(row).prompt_hash
            for row in observations
        ]
        plan = entrypoint._sample_plan(observations, 3)
        sample_ids = [row["sample_id"] for row in plan]
        v3_profile = entrypoint.pilot_profile_descriptor(v3_id)
        v4_profile = entrypoint.pilot_profile_descriptor(self.PROFILE)
        self.assertEqual(len(prompt_hashes), 54)
        self.assertEqual(
            entrypoint.stable_hash(prompt_hashes),
            "3a3431b90ee082ec87c32bf58f181523f9c203aeb911b30f58bd57553aea84bc",
        )
        self.assertEqual(len(sample_ids), 162)
        self.assertEqual(
            entrypoint.stable_hash(sample_ids),
            v3_profile["planned_sample_order_hash"],
        )
        self.assertEqual(
            v4_profile["planned_sample_order_hash"],
            v3_profile["planned_sample_order_hash"],
        )
        self.assertEqual(v4_profile["canary_sample_id"], sample_ids[0])
        for key in (
            "state_design_hash",
            "planned_split_hash",
            "planned_split_counts",
            "planned_sample_order_hash",
            "canary_sample_id",
            "temperature",
            "max_tokens",
            "model_requested",
            "required_reported_model",
            "response_termination_contract",
            "teacher_acceptance_gate",
            "transport_release_policy",
        ):
            with self.subTest(field=key):
                self.assertEqual(v4_profile[key], v3_profile[key])

    def test_v4_wrong_run_ids_fail_before_provider_and_gate_is_strict(self):
        for run_id in (
            "v2-teacher-pilot-live-20260813-a3",
            "v2-teacher-pilot-live-20260813-a5",
        ):
            with self.subTest(run_id=run_id):
                out = self.root / run_id
                argv = _pilot_args(out, live=True, profile=self.PROFILE)
                argv[argv.index("--run-id") + 1] = run_id
                with mock.patch.dict(
                    os.environ, self.ENDPOINT_ENV, clear=False
                ), mock.patch.object(
                    entrypoint,
                    "_build_openai_provider",
                    side_effect=AssertionError("invalid v4 built provider"),
                ) as provider, redirect_stdout(io.StringIO()), redirect_stderr(
                    io.StringIO()
                ):
                    with self.assertRaises(SystemExit) as raised:
                        entrypoint.main(argv)
                self.assertEqual(raised.exception.code, 2)
                provider.assert_not_called()
                manifest = _read_json(_single_run(out) / "run_manifest.json")
                self.assertEqual(manifest["failure_stage"], "config_validation")
                self.assertEqual(
                    manifest["completion"]["provider_calls"]["attempted"], 0
                )

        from nmsim import v2_attention

        args = entrypoint.build_argparser().parse_args(
            _pilot_args(self.root / "gate", profile=self.PROFILE)
        )
        observations = v2_attention.generate_state_design(
            54, 20260811, study_id="v2-attention-market"
        )
        plan = entrypoint._sample_plan(observations, 3)
        rows = [
            {
                "state_id": item["observation"].state_id,
                "status": "valid",
                "model_requested": "MiniMax-M2.7",
                "reported_model": "HiggsAI",
                "finish_reason": "stop",
            }
            for item in plan
        ]
        passed = entrypoint._teacher_gate_result(
            args=args,
            plan=plan,
            public_rows=rows,
            reported_models=["HiggsAI"] * 162,
            attempted_samples=162,
        )
        self.assertEqual(passed["status"], "passed")
        self.assertTrue(passed["student_and_market_released"])
        rows[0]["finish_reason"] = "length"
        failed = entrypoint._teacher_gate_result(
            args=args,
            plan=plan,
            public_rows=rows,
            reported_models=["HiggsAI"] * 162,
            attempted_samples=162,
        )
        self.assertEqual(failed["status"], "failed")
        self.assertFalse(failed["student_and_market_released"])

    def test_v4_a3_and_a4_directories_are_o_excl_immutable(self):
        for run_id in (
            "v2-teacher-pilot-live-20260813-a3",
            "v2-teacher-pilot-live-20260813-a4",
        ):
            with self.subTest(run_id=run_id):
                out = self.root / run_id
                run_dir = out / "runs" / run_id
                run_dir.mkdir(parents=True)
                sentinel = run_dir / "immutable-sentinel.bin"
                sentinel.write_bytes(b"immutable-v4-boundary\x00")
                before_hash = _sha256(sentinel)
                before_entries = sorted(path.name for path in run_dir.iterdir())
                argv = _pilot_args(out, live=True, profile=self.PROFILE)
                argv[argv.index("--run-id") + 1] = run_id
                with mock.patch.dict(
                    os.environ, self.ENDPOINT_ENV, clear=False
                ), mock.patch.object(
                    entrypoint,
                    "_build_openai_provider",
                    side_effect=AssertionError("O_EXCL path built provider"),
                ) as provider, redirect_stdout(io.StringIO()), redirect_stderr(
                    io.StringIO()
                ):
                    with self.assertRaises((SystemExit, FileExistsError)) as raised:
                        entrypoint.main(argv)
                if isinstance(raised.exception, SystemExit):
                    self.assertEqual(raised.exception.code, 2)
                provider.assert_not_called()
                self.assertEqual(_sha256(sentinel), before_hash)
                self.assertEqual(
                    sorted(path.name for path in run_dir.iterdir()),
                    before_entries,
                )


class V5LongTimeoutExecutionSuccessorTests(unittest.TestCase):
    ENDPOINT_ENV = V2FrozenTeacherPilotTests.ENDPOINT_ENV
    PROFILE = entrypoint.MINIMAX_M27_HIGGSAI_LONG_TIMEOUT_JOINT54X3_PILOT
    V4_PROFILE = (
        entrypoint.MINIMAX_M27_HIGGSAI_FINISH_AUDIT_EXTERNAL_JOINT54X3_PILOT
    )

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)

    def _identities(self, profile: str, out_name: str) -> dict:
        from nmsim import v2_attention

        args = entrypoint.build_argparser().parse_args(
            _pilot_args(self.root / out_name, profile=profile)
        )
        entrypoint._validate_args(args)
        observations = v2_attention.generate_state_design(
            54, 20260811, study_id="v2-attention-market"
        )
        return entrypoint.build_v2_identities(
            args,
            observations,
            repo_root=Path(entrypoint.__file__).resolve().parents[1],
        )

    def test_v5_descriptor_and_dry_run_are_frozen_and_zero_network(self):
        out = self.root / "v5-dry"
        with mock.patch.dict(
            os.environ, self.ENDPOINT_ENV, clear=False
        ), mock.patch.object(
            entrypoint,
            "_build_openai_provider",
            side_effect=AssertionError("v5 dry-run constructed provider"),
        ) as provider, mock.patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError("v5 dry-run opened socket"),
        ) as network, redirect_stdout(io.StringIO()):
            entrypoint.main(_pilot_args(out, profile=self.PROFILE))
        provider.assert_not_called()
        network.assert_not_called()

        run_dir = _single_run(out)
        manifest = _read_json(run_dir / "run_manifest.json")
        summary = _read_json(run_dir / "dry_run_summary.json")
        profile = entrypoint.pilot_profile_descriptor(self.PROFILE)
        self.assertEqual(
            entrypoint.stable_hash(profile),
            "5be40a9f4ed6ea858d8997360512d6ba5587813d9a1d81460af65960289a1c03",
        )
        self.assertEqual(profile["schema_version"], "v2_teacher_pilot_profile/0.5")
        self.assertEqual(profile["successor_scope"], "execution_only")
        self.assertEqual(profile["httpx_phase_inactivity_timeout_seconds"], 600)
        self.assertEqual(profile["hard_request_deadline_seconds"], 600)
        self.assertEqual(profile["connect_timeout_seconds"], 10)
        self.assertEqual(
            profile["transport_release_policy"]["provider_retry_count"], 0
        )
        self.assertEqual(
            profile["required_run_ids"],
            {
                "dry_run": "v2-teacher-pilot-v5-dry-20260813-a1",
                "live": "v2-teacher-pilot-live-20260813-a5",
            },
        )
        self.assertEqual(
            [row["run_id"] for row in profile["predecessor_failed_runs"]],
            [
                "v2-teacher-pilot-live-20260812-a1",
                "v2-teacher-pilot-live-20260812-a2",
                "v2-teacher-pilot-live-20260813-a3",
                "v2-teacher-pilot-live-20260813-a4",
            ],
        )
        self.assertEqual(
            profile["predecessor_failed_runs"][3]["run_manifest_sha256"],
            "86c1deb28f85c2f49e9fd36c410c951f3fa27e8d8fa48b94cf3febf86d1f888e",
        )
        self.assertEqual(summary["provider_calls"], 0)
        self.assertFalse(summary["network_access"])
        self.assertEqual(summary["planned_teacher_requests"], 162)
        self.assertEqual(summary["pilot_profile"], profile)

        identities = manifest["v2_config_identities"]
        self.assertEqual(
            identities["model_request_config"]["schema_version"],
            entrypoint.FINISH_AUDIT_REQUEST_SCHEMA_VERSION,
        )
        execution = identities["execution_config"]
        self.assertEqual(execution["schema_version"], "v2_attention_execution/0.2")
        self.assertEqual(
            execution["httpx_phase_inactivity_timeout_seconds"], 600.0
        )
        self.assertEqual(execution["hard_request_deadline_seconds"], 600.0)
        self.assertEqual(execution["connect_timeout_seconds"], 10.0)
        self.assertEqual(execution["provider_retry_count"], 0)
        pilot_input = next(
            row
            for row in manifest["inputs"]
            if row["label"] == "v2_teacher_pilot_protocol"
        )
        self.assertTrue(pilot_input["path"].endswith("V2_TEACHER_PILOT_V5.md"))

    def test_v5_changes_only_execution_timeout_not_wire_or_science_parameters(self):
        from nmsim import v2_attention

        with mock.patch.dict(os.environ, self.ENDPOINT_ENV, clear=False):
            v4 = self._identities(self.V4_PROFILE, "v4-identities")
            v5 = self._identities(self.PROFILE, "v5-identities")

        v4_science = json.loads(json.dumps(v4["scientific_config"]))
        v5_science = json.loads(json.dumps(v5["scientific_config"]))
        self.assertEqual(
            v4_science["teacher_sampling"].pop("pilot_profile_id"),
            self.V4_PROFILE,
        )
        self.assertEqual(
            v5_science["teacher_sampling"].pop("pilot_profile_id"), self.PROFILE
        )
        self.assertEqual(v5_science, v4_science)

        v4_request = json.loads(json.dumps(v4["model_request_config"]))
        v5_request = json.loads(json.dumps(v5["model_request_config"]))
        self.assertEqual(v4_request.pop("pilot_profile_id"), self.V4_PROFILE)
        self.assertEqual(v5_request.pop("pilot_profile_id"), self.PROFILE)
        self.assertEqual(v5_request, v4_request)
        self.assertEqual(
            {
                key: v5_request[key]
                for key in (
                    "model_requested",
                    "temperature",
                    "max_tokens",
                    "request_seed",
                    "provider_retry_count",
                    "response_termination_contract",
                )
            },
            {
                "model_requested": "MiniMax-M2.7",
                "temperature": 0.0,
                "max_tokens": 4096,
                "request_seed": None,
                "provider_retry_count": 0,
                "response_termination_contract": entrypoint.pilot_profile_descriptor(
                    self.V4_PROFILE
                )["response_termination_contract"],
            },
        )

        v4_execution = v4["execution_config"]
        v5_execution = v5["execution_config"]
        self.assertEqual(
            v4_execution["schema_version"], "v2_attention_execution/0.1"
        )
        self.assertEqual(
            v5_execution["schema_version"], "v2_attention_execution/0.2"
        )
        self.assertNotIn("request_timeout_seconds", v4_execution)
        self.assertNotIn("connect_timeout_seconds", v4_execution)
        self.assertNotIn("provider_retry_count", v4_execution)
        self.assertEqual(entrypoint._request_timeout_seconds(self.V4_PROFILE), 120.0)
        self.assertIsNone(
            entrypoint._hard_request_deadline_seconds(self.V4_PROFILE)
        )
        self.assertEqual(
            v5_execution["httpx_phase_inactivity_timeout_seconds"], 600.0
        )
        self.assertEqual(v5_execution["hard_request_deadline_seconds"], 600.0)
        self.assertEqual(v5_execution["connect_timeout_seconds"], 10.0)
        self.assertEqual(v5_execution["provider_retry_count"], 0)
        normalized_v4_execution = json.loads(json.dumps(v4_execution))
        normalized_v5_execution = json.loads(json.dumps(v5_execution))
        for execution in (normalized_v4_execution, normalized_v5_execution):
            execution.pop("schema_version")
            execution.pop("output_root_identity_sha256")
            execution.pop("caller_run_id")
            execution.pop("pilot_profile_id")
        normalized_v5_execution.pop("httpx_phase_inactivity_timeout_seconds")
        normalized_v5_execution.pop("hard_request_deadline_seconds")
        normalized_v5_execution.pop("connect_timeout_seconds")
        normalized_v5_execution.pop("provider_retry_count")
        self.assertEqual(normalized_v5_execution, normalized_v4_execution)
        self.assertNotEqual(
            v5["v2_execution_config_hash"], v4["v2_execution_config_hash"]
        )
        self.assertNotEqual(
            v5["v2_full_effective_config_hash"],
            v4["v2_full_effective_config_hash"],
        )
        self.assertNotEqual(
            v5["v2_scientific_config_hash"], v4["v2_scientific_config_hash"]
        )
        self.assertNotEqual(
            v5["v2_model_request_config_hash"],
            v4["v2_model_request_config_hash"],
        )

        observations = v2_attention.generate_state_design(
            54, 20260811, study_id="v2-attention-market"
        )
        rendered = [v2_attention.render_teacher_prompt(row) for row in observations]
        prompt_hashes = [prompt.prompt_hash for prompt in rendered]
        prompt_bytes_hash = entrypoint.stable_hash(
            [prompt.to_messages() for prompt in rendered]
        )
        plan = entrypoint._sample_plan(observations, 3)
        sample_ids = [row["sample_id"] for row in plan]
        v4_profile = entrypoint.pilot_profile_descriptor(self.V4_PROFILE)
        v5_profile = entrypoint.pilot_profile_descriptor(self.PROFILE)
        self.assertEqual(
            entrypoint.stable_hash(prompt_hashes),
            "3a3431b90ee082ec87c32bf58f181523f9c203aeb911b30f58bd57553aea84bc",
        )
        self.assertEqual(
            prompt_bytes_hash,
            "201d981933acf51f5150525d2a3280c44f8022aa127fe0827247e248c6cbfe5f",
        )
        self.assertEqual(
            entrypoint.stable_hash(sample_ids),
            v4_profile["planned_sample_order_hash"],
        )
        self.assertEqual(
            v5_profile["planned_sample_order_hash"],
            v4_profile["planned_sample_order_hash"],
        )
        self.assertEqual(v5_profile["canary_sample_id"], sample_ids[0])
        for key in (
            "state_design_hash",
            "planned_split_hash",
            "planned_split_counts",
            "planned_sample_order_hash",
            "canary_sample_id",
            "temperature",
            "max_tokens",
            "model_requested",
            "required_reported_model",
            "response_termination_contract",
            "teacher_acceptance_gate",
            "transport_release_policy",
            "training_epochs",
            "market_agents",
            "market_rounds",
            "market_seeds",
        ):
            with self.subTest(field=key):
                self.assertEqual(v5_profile[key], v4_profile[key])

    def test_v5_builds_600_second_zero_retry_transport_without_network(self):
        import httpx

        http_client = object()
        with mock.patch.dict(
            os.environ, self.ENDPOINT_ENV, clear=False
        ), mock.patch(
            "httpx.AsyncClient", return_value=http_client
        ) as async_client, mock.patch("openai.AsyncOpenAI") as openai_client:
            provider = entrypoint.OpenAITeacherProvider(
                model="MiniMax-M2.7",
                temperature=0.0,
                max_tokens=4096,
                workers=1,
                request_timeout_seconds=600.0,
                hard_request_deadline_seconds=600.0,
            )

        timeout = async_client.call_args.kwargs["timeout"]
        self.assertIsInstance(timeout, httpx.Timeout)
        self.assertEqual(timeout.connect, 10.0)
        self.assertEqual(timeout.read, 600.0)
        self.assertEqual(timeout.write, 600.0)
        self.assertEqual(timeout.pool, 600.0)
        self.assertFalse(async_client.call_args.kwargs["trust_env"])
        client_kwargs = openai_client.call_args.kwargs
        self.assertIs(client_kwargs["http_client"], http_client)
        self.assertEqual(client_kwargs["max_retries"], 0)
        self.assertEqual(provider.request_timeout_seconds, 600.0)
        self.assertEqual(provider.hard_request_deadline_seconds, 600.0)
        self.assertEqual(provider.connect_timeout_seconds, 10.0)
        self.assertEqual(provider.provider_retry_count, 0)

        args = entrypoint.build_argparser().parse_args(
            _pilot_args(self.root / "provider-build", profile=self.PROFILE)
        )
        sentinel = object()
        with mock.patch.object(
            entrypoint, "OpenAITeacherProvider", return_value=sentinel
        ) as constructor:
            built = entrypoint._build_openai_provider(args)
        self.assertIs(built, sentinel)
        self.assertEqual(
            constructor.call_args.kwargs["request_timeout_seconds"], 600.0
        )
        self.assertEqual(
            constructor.call_args.kwargs["hard_request_deadline_seconds"],
            600.0,
        )

    def test_v5_hard_deadline_uses_wait_for_600_and_v4_has_no_wrapper(self):
        class Completions:
            def __init__(self):
                self.calls = []

            async def create(self, **kwargs):
                self.calls.append(kwargs)
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            finish_reason="stop",
                            message=SimpleNamespace(content='{"ok":true}'),
                        )
                    ],
                    model="HiggsAI",
                    id="hard-deadline-test",
                    usage=SimpleNamespace(
                        prompt_tokens=1, completion_tokens=1
                    ),
                )

        async def exercise(hard_deadline):
            completions_api = Completions()
            provider = object.__new__(entrypoint.OpenAITeacherProvider)
            provider._client = SimpleNamespace(
                chat=SimpleNamespace(completions=completions_api)
            )
            provider.model = "MiniMax-M2.7"
            provider.temperature = 0.0
            provider.max_tokens = 4096
            provider.workers = 1
            provider.request_timeout_seconds = 600.0
            provider.hard_request_deadline_seconds = hard_deadline
            provider.network_access = False
            provider.request_count = 0
            provider.response_count = 0
            provider.batch_sizes = []
            with mock.patch(
                "experiments.v2_attention_market.asyncio.wait_for",
                wraps=asyncio.wait_for,
            ) as wait_for:
                completion = (
                    await provider.complete_many(
                        [("system", "user")], strict_sequential=True
                    )
                )[0]
            return provider, completion, wait_for, completions_api

        v5_provider, v5_completion, v5_wait_for, v5_api = asyncio.run(
            exercise(600.0)
        )
        self.assertEqual(v5_wait_for.call_count, 1)
        self.assertEqual(v5_wait_for.call_args.kwargs["timeout"], 600.0)
        self.assertEqual(v5_provider.response_count, 1)
        self.assertEqual(v5_completion.raw_response, '{"ok":true}')
        self.assertEqual(len(v5_api.calls), 1)

        v4_provider, v4_completion, v4_wait_for, v4_api = asyncio.run(
            exercise(None)
        )
        v4_wait_for.assert_not_called()
        self.assertEqual(v4_provider.response_count, 1)
        self.assertEqual(v4_completion.raw_response, '{"ok":true}')
        self.assertEqual(len(v4_api.calls), 1)

        for profile, expected in (
            (self.V4_PROFILE, None),
            (self.PROFILE, 600.0),
        ):
            with self.subTest(profile=profile):
                args = entrypoint.build_argparser().parse_args(
                    _pilot_args(self.root / profile, profile=profile)
                )
                sentinel = object()
                with mock.patch.object(
                    entrypoint, "OpenAITeacherProvider", return_value=sentinel
                ) as constructor:
                    self.assertIs(entrypoint._build_openai_provider(args), sentinel)
                self.assertEqual(
                    constructor.call_args.kwargs[
                        "hard_request_deadline_seconds"
                    ],
                    expected,
                )

    def test_v5_hard_timeout_is_provider_exception_without_parse_or_release(self):
        class NeverCompletes:
            def __init__(self):
                self.created = 0

            async def create(self, **kwargs):
                self.created += 1
                await asyncio.Future()

        api = NeverCompletes()
        provider = object.__new__(entrypoint.OpenAITeacherProvider)
        provider._client = SimpleNamespace(
            chat=SimpleNamespace(completions=api),
            close=mock.AsyncMock(),
        )
        provider.model = "MiniMax-M2.7"
        provider.temperature = 0.0
        provider.max_tokens = 4096
        provider.workers = 1
        provider.request_timeout_seconds = 600.0
        provider.hard_request_deadline_seconds = 600.0
        provider.network_access = False
        provider.request_count = 0
        provider.response_count = 0
        provider.batch_sizes = []

        real_wait_for = asyncio.wait_for

        async def immediate_deadline(awaitable, *, timeout):
            self.assertEqual(timeout, 600.0)
            return await real_wait_for(awaitable, timeout=0.001)

        out = self.root / "hard-timeout"
        with mock.patch.dict(
            os.environ, self.ENDPOINT_ENV, clear=False
        ), mock.patch.object(
            entrypoint, "_build_openai_provider", return_value=provider
        ), mock.patch(
            "experiments.v2_attention_market.asyncio.wait_for",
            side_effect=immediate_deadline,
        ) as wait_for, mock.patch.object(
            entrypoint, "run_distillation_phase"
        ) as distillation, mock.patch.object(
            entrypoint, "run_market_phase"
        ) as market, redirect_stdout(io.StringIO()), redirect_stderr(
            io.StringIO()
        ):
            with self.assertRaises(SystemExit) as raised:
                entrypoint.main(
                    _pilot_args(out, live=True, profile=self.PROFILE)
                )
        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(wait_for.call_count, 1)
        self.assertEqual(api.created, 1)
        self.assertEqual(provider.request_count, 1)
        self.assertEqual(provider.response_count, 0)
        distillation.assert_not_called()
        market.assert_not_called()

        run_dir = _single_run(out)
        manifest = _read_json(run_dir / "run_manifest.json")
        rows = _read_jsonl(run_dir / "teacher_samples.jsonl")
        private_rows = _read_jsonl(run_dir / "private_teacher_records.jsonl")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "failed")
        self.assertEqual(rows[0]["failure_code"], "provider_exception")
        self.assertIsNone(rows[0]["response_hash"])
        self.assertIsNone(rows[0]["decision"])
        self.assertEqual(private_rows[0]["provider_error_type"], "TimeoutError")
        teacher = manifest["v2_attention_market"]["teacher_samples"]
        gate = manifest["v2_attention_market"]["teacher_acceptance_gate"]
        self.assertEqual(teacher["attempted"], 1)
        self.assertEqual(teacher["valid"], 0)
        self.assertEqual(teacher["skipped"], 161)
        self.assertEqual(teacher["failure_counts"], {"provider_exception": 1})
        self.assertEqual(manifest["honest_n_teacher_samples"], 0)
        self.assertEqual(manifest["completion"]["parsing"]["attempted"], 0)
        self.assertEqual(manifest["completion"]["parsing"]["succeeded"], 0)
        self.assertEqual(
            manifest["completion"]["application_provider_attempts"][
                "provider_exceptions"
            ],
            1,
        )
        self.assertEqual(gate["status"], "failed")
        self.assertEqual(gate["reason_codes"], ["provider_exception"])
        self.assertFalse(gate["student_and_market_released"])
        self.assertFalse((run_dir / "aggregated_dataset.json").exists())
        self.assertFalse((run_dir / "market_2x2_summary.json").exists())

    def test_v5_wrong_a4_and_a6_ids_fail_before_provider_and_gate_is_inherited(self):
        for run_id in (
            "v2-teacher-pilot-live-20260813-a4",
            "v2-teacher-pilot-live-20260813-a6",
        ):
            with self.subTest(run_id=run_id):
                out = self.root / run_id
                argv = _pilot_args(out, live=True, profile=self.PROFILE)
                argv[argv.index("--run-id") + 1] = run_id
                with mock.patch.dict(
                    os.environ, self.ENDPOINT_ENV, clear=False
                ), mock.patch.object(
                    entrypoint,
                    "_build_openai_provider",
                    side_effect=AssertionError("invalid v5 built provider"),
                ) as provider, redirect_stdout(io.StringIO()), redirect_stderr(
                    io.StringIO()
                ):
                    with self.assertRaises(SystemExit) as raised:
                        entrypoint.main(argv)
                self.assertEqual(raised.exception.code, 2)
                provider.assert_not_called()
                manifest = _read_json(_single_run(out) / "run_manifest.json")
                self.assertEqual(manifest["failure_stage"], "config_validation")
                self.assertEqual(
                    manifest["completion"]["provider_calls"]["attempted"], 0
                )

        from nmsim import v2_attention

        args = entrypoint.build_argparser().parse_args(
            _pilot_args(self.root / "gate", profile=self.PROFILE)
        )
        observations = v2_attention.generate_state_design(
            54, 20260811, study_id="v2-attention-market"
        )
        plan = entrypoint._sample_plan(observations, 3)
        rows = [
            {
                "state_id": item["observation"].state_id,
                "status": "valid",
                "model_requested": "MiniMax-M2.7",
                "reported_model": "HiggsAI",
                "finish_reason": "stop",
            }
            for item in plan
        ]
        passed = entrypoint._teacher_gate_result(
            args=args,
            plan=plan,
            public_rows=rows,
            reported_models=["HiggsAI"] * 162,
            attempted_samples=162,
        )
        self.assertEqual(passed["status"], "passed")
        self.assertTrue(passed["student_and_market_released"])
        rows[0]["finish_reason"] = "length"
        failed = entrypoint._teacher_gate_result(
            args=args,
            plan=plan,
            public_rows=rows,
            reported_models=["HiggsAI"] * 162,
            attempted_samples=162,
        )
        self.assertEqual(failed["status"], "failed")
        self.assertFalse(failed["student_and_market_released"])

    def test_v5_a4_and_a5_directories_are_o_excl_immutable(self):
        for run_id in (
            "v2-teacher-pilot-live-20260813-a4",
            "v2-teacher-pilot-live-20260813-a5",
        ):
            with self.subTest(run_id=run_id):
                out = self.root / run_id
                run_dir = out / "runs" / run_id
                run_dir.mkdir(parents=True)
                sentinel = run_dir / "immutable-sentinel.bin"
                sentinel.write_bytes(b"immutable-v5-boundary\x00")
                before_hash = _sha256(sentinel)
                before_entries = sorted(path.name for path in run_dir.iterdir())
                argv = _pilot_args(out, live=True, profile=self.PROFILE)
                argv[argv.index("--run-id") + 1] = run_id
                with mock.patch.dict(
                    os.environ, self.ENDPOINT_ENV, clear=False
                ), mock.patch.object(
                    entrypoint,
                    "_build_openai_provider",
                    side_effect=AssertionError("O_EXCL path built provider"),
                ) as provider, redirect_stdout(io.StringIO()), redirect_stderr(
                    io.StringIO()
                ):
                    with self.assertRaises((SystemExit, FileExistsError)) as raised:
                        entrypoint.main(argv)
                if isinstance(raised.exception, SystemExit):
                    self.assertEqual(raised.exception.code, 2)
                provider.assert_not_called()
                self.assertEqual(_sha256(sentinel), before_hash)
                self.assertEqual(
                    sorted(path.name for path in run_dir.iterdir()), before_entries
                )


class V6OutputBudgetSuccessorTests(unittest.TestCase):
    ENDPOINT_ENV = V2FrozenTeacherPilotTests.ENDPOINT_ENV
    PROFILE = (
        entrypoint.MINIMAX_M27_HIGGSAI_TIMEOUT600_OUTPUT16384_JOINT54X3_PILOT
    )
    V5_PROFILE = entrypoint.MINIMAX_M27_HIGGSAI_LONG_TIMEOUT_JOINT54X3_PILOT

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)

    def _identities(self, profile: str, out_name: str) -> dict:
        from nmsim import v2_attention

        args = entrypoint.build_argparser().parse_args(
            _pilot_args(self.root / out_name, profile=profile)
        )
        entrypoint._validate_args(args)
        observations = v2_attention.generate_state_design(
            54, 20260811, study_id="v2-attention-market"
        )
        return entrypoint.build_v2_identities(
            args,
            observations,
            repo_root=Path(entrypoint.__file__).resolve().parents[1],
        )

    def test_v6_descriptor_and_dry_run_are_frozen_and_zero_network(self):
        out = self.root / "v6-dry"
        with mock.patch.dict(
            os.environ, self.ENDPOINT_ENV, clear=False
        ), mock.patch.object(
            entrypoint,
            "_build_openai_provider",
            side_effect=AssertionError("v6 dry-run constructed provider"),
        ) as provider, mock.patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError("v6 dry-run opened socket"),
        ) as network, redirect_stdout(io.StringIO()):
            entrypoint.main(_pilot_args(out, profile=self.PROFILE))
        provider.assert_not_called()
        network.assert_not_called()

        run_dir = _single_run(out)
        manifest = _read_json(run_dir / "run_manifest.json")
        summary = _read_json(run_dir / "dry_run_summary.json")
        profile = entrypoint.pilot_profile_descriptor(self.PROFILE)
        self.assertEqual(
            entrypoint.stable_hash(profile),
            "eb5540b0049d0f7a6ecbb640e372bdced2e11deac3f5746e24b80de3f5c40d30",
        )
        self.assertEqual(profile["schema_version"], "v2_teacher_pilot_profile/0.6")
        self.assertEqual(profile["successor_scope"], "output_budget_model_request_only")
        self.assertEqual(profile["max_tokens"], 16384)
        self.assertEqual(profile["httpx_phase_inactivity_timeout_seconds"], 600)
        self.assertEqual(profile["hard_request_deadline_seconds"], 600)
        self.assertEqual(profile["connect_timeout_seconds"], 10)
        self.assertEqual(
            profile["required_run_ids"],
            {
                "dry_run": "v2-teacher-pilot-v6-dry-20260820-a1",
                "live": "v2-teacher-pilot-live-20260820-a6",
            },
        )
        self.assertEqual(
            [row["run_id"] for row in profile["predecessor_failed_runs"]],
            [
                "v2-teacher-pilot-live-20260812-a1",
                "v2-teacher-pilot-live-20260812-a2",
                "v2-teacher-pilot-live-20260813-a3",
                "v2-teacher-pilot-live-20260813-a4",
                "v2-teacher-pilot-live-20260813-a5",
            ],
        )
        a5 = profile["predecessor_failed_runs"][-1]
        self.assertEqual(
            a5,
            {
                "run_id": "v2-teacher-pilot-live-20260813-a5",
                "status": "failed",
                "attempted": 7,
                "responses": 7,
                "valid": 6,
                "honest_n": 6,
                "skipped": 155,
                "failure_code": "provider_response_shape_invalid",
                "finish_reason": "length",
                "output_tokens": 4096,
                "student_runs": 0,
                "market_runs": 0,
                "run_manifest_sha256": (
                    "76e79010a55a2c57766d52b147f686b4c0854c9abaa7cadc3abe468adf96b16a"
                ),
                "reuse_supplement_or_merge_forbidden": True,
            },
        )
        self.assertEqual(summary["provider_calls"], 0)
        self.assertFalse(summary["network_access"])
        self.assertEqual(summary["planned_teacher_requests"], 162)
        self.assertEqual(summary["pilot_profile"], profile)

        identities = manifest["v2_config_identities"]
        request = identities["model_request_config"]
        execution = identities["execution_config"]
        self.assertEqual(
            request["schema_version"], entrypoint.FINISH_AUDIT_REQUEST_SCHEMA_VERSION
        )
        self.assertEqual(request["schema_version"], "v2_teacher_request/0.2")
        self.assertEqual(request["max_tokens"], 16384)
        self.assertEqual(execution["schema_version"], "v2_attention_execution/0.2")
        self.assertEqual(execution["httpx_phase_inactivity_timeout_seconds"], 600.0)
        self.assertEqual(execution["hard_request_deadline_seconds"], 600.0)
        self.assertEqual(execution["connect_timeout_seconds"], 10.0)
        self.assertEqual(execution["provider_retry_count"], 0)
        pilot_input = next(
            row
            for row in manifest["inputs"]
            if row["label"] == "v2_teacher_pilot_protocol"
        )
        self.assertTrue(pilot_input["path"].endswith("V2_TEACHER_PILOT_V6.md"))

    def test_v6_changes_only_max_tokens_from_v5_and_preserves_sample_identity(self):
        from nmsim import v2_attention

        expected_descriptor_hashes = {
            entrypoint.MINIMAX_M27_JOINT54X3_PILOT: (
                "1228cd39c038771a916fb747e1e767218874232ddb1bad4f16d1f3d5a2712d1a"
            ),
            entrypoint.MINIMAX_M27_REQUEST_HIGGSAI_REPORTED_JOINT54X3_PILOT: (
                "3f586f02974265e243bc49a0f925eb7e274a2554b4aaee6e706a354becacfebc"
            ),
            entrypoint.MINIMAX_M27_HIGGSAI_FINISH_AUDIT_JOINT54X3_PILOT: (
                "c042e41f2263f7c3ee093f7c6b258ee8972edb3a4db44bcae725cc6e0e00aa3e"
            ),
            entrypoint.MINIMAX_M27_HIGGSAI_FINISH_AUDIT_EXTERNAL_JOINT54X3_PILOT: (
                "5851865c1a22ec2d772524db55e2f57d9bd7e101a02eb7be692bd5f42da5f9b5"
            ),
            entrypoint.MINIMAX_M27_HIGGSAI_LONG_TIMEOUT_JOINT54X3_PILOT: (
                "5be40a9f4ed6ea858d8997360512d6ba5587813d9a1d81460af65960289a1c03"
            ),
        }
        for profile_id, expected_hash in expected_descriptor_hashes.items():
            with self.subTest(legacy_profile=profile_id):
                self.assertEqual(
                    entrypoint.stable_hash(
                        entrypoint.pilot_profile_descriptor(profile_id)
                    ),
                    expected_hash,
                )

        with mock.patch.dict(os.environ, self.ENDPOINT_ENV, clear=False):
            v5 = self._identities(self.V5_PROFILE, "v5-identities")
            v6 = self._identities(self.PROFILE, "v6-identities")

        v5_science = json.loads(json.dumps(v5["scientific_config"]))
        v6_science = json.loads(json.dumps(v6["scientific_config"]))
        self.assertEqual(
            v5_science["teacher_sampling"].pop("pilot_profile_id"),
            self.V5_PROFILE,
        )
        self.assertEqual(
            v6_science["teacher_sampling"].pop("pilot_profile_id"), self.PROFILE
        )
        self.assertEqual(v6_science, v5_science)

        v5_request = json.loads(json.dumps(v5["model_request_config"]))
        v6_request = json.loads(json.dumps(v6["model_request_config"]))
        self.assertEqual(v5_request.pop("pilot_profile_id"), self.V5_PROFILE)
        self.assertEqual(v6_request.pop("pilot_profile_id"), self.PROFILE)
        self.assertEqual(v5_request.pop("max_tokens"), 4096)
        self.assertEqual(v6_request.pop("max_tokens"), 16384)
        self.assertEqual(v6_request, v5_request)
        self.assertEqual(
            v6["model_request_config"]["schema_version"],
            "v2_teacher_request/0.2",
        )

        v5_execution = json.loads(json.dumps(v5["execution_config"]))
        v6_execution = json.loads(json.dumps(v6["execution_config"]))
        for execution, expected_profile in (
            (v5_execution, self.V5_PROFILE),
            (v6_execution, self.PROFILE),
        ):
            self.assertEqual(execution["schema_version"], "v2_attention_execution/0.2")
            self.assertEqual(execution["pilot_profile_id"], expected_profile)
            self.assertEqual(execution["httpx_phase_inactivity_timeout_seconds"], 600.0)
            self.assertEqual(execution["hard_request_deadline_seconds"], 600.0)
            self.assertEqual(execution["connect_timeout_seconds"], 10.0)
            self.assertEqual(execution["provider_retry_count"], 0)
            execution.pop("output_root_identity_sha256")
            execution.pop("caller_run_id")
            execution.pop("pilot_profile_id")
        self.assertEqual(v6_execution, v5_execution)

        observations = v2_attention.generate_state_design(
            54, 20260811, study_id="v2-attention-market"
        )
        rendered = [v2_attention.render_teacher_prompt(row) for row in observations]
        state_hash = entrypoint.stable_hash([row.to_dict() for row in observations])
        prompt_bytes_hash = entrypoint.stable_hash(
            [prompt.to_messages() for prompt in rendered]
        )
        plan = entrypoint._sample_plan(observations, 3)
        sample_ids = [item["sample_id"] for item in plan]
        v5_profile = entrypoint.pilot_profile_descriptor(self.V5_PROFILE)
        v6_profile = entrypoint.pilot_profile_descriptor(self.PROFILE)
        self.assertEqual(entrypoint.SAMPLE_IDENTITY_SCHEMA_VERSION, "v2_teacher_request/0.1")
        self.assertEqual(state_hash, v5_profile["state_design_hash"])
        self.assertEqual(state_hash, v6_profile["state_design_hash"])
        self.assertEqual(
            prompt_bytes_hash,
            "201d981933acf51f5150525d2a3280c44f8022aa127fe0827247e248c6cbfe5f",
        )
        self.assertEqual(len(sample_ids), 162)
        self.assertEqual(
            entrypoint.stable_hash(sample_ids),
            v5_profile["planned_sample_order_hash"],
        )
        self.assertEqual(
            v6_profile["planned_sample_order_hash"],
            v5_profile["planned_sample_order_hash"],
        )
        self.assertEqual(v6_profile["canary_sample_id"], sample_ids[0])
        first = plan[0]
        self.assertEqual(
            first["sample_id"],
            v2_attention.sha256_hex(
                {
                    "schema_version": "v2_teacher_request/0.1",
                    "state_id": first["observation"].state_id,
                    "prompt_hash": first["prompt"].prompt_hash,
                    "replicate_index": first["replicate_index"],
                }
            ),
        )
        for key in (
            "state_design_hash",
            "planned_split_hash",
            "planned_split_counts",
            "planned_sample_order_hash",
            "canary_sample_id",
            "temperature",
            "model_requested",
            "required_reported_model",
            "response_termination_contract",
            "teacher_acceptance_gate",
            "transport_release_policy",
            "httpx_phase_inactivity_timeout_seconds",
            "hard_request_deadline_seconds",
            "connect_timeout_seconds",
            "training_epochs",
            "market_agents",
            "market_rounds",
            "market_seeds",
        ):
            with self.subTest(frozen_field=key):
                self.assertEqual(v6_profile[key], v5_profile[key])

    def test_v6_builds_exact_16384_request_with_v5_transport_contract(self):
        args = entrypoint.build_argparser().parse_args(
            _pilot_args(self.root / "provider-build", profile=self.PROFILE)
        )
        with mock.patch.dict(os.environ, self.ENDPOINT_ENV, clear=False):
            entrypoint._validate_args(args)
        self.assertEqual(args.max_tokens, 16384)
        sentinel = object()
        with mock.patch.object(
            entrypoint, "OpenAITeacherProvider", return_value=sentinel
        ) as constructor:
            built = entrypoint._build_openai_provider(args)
        self.assertIs(built, sentinel)
        self.assertEqual(constructor.call_args.kwargs["max_tokens"], 16384)
        self.assertEqual(
            constructor.call_args.kwargs["request_timeout_seconds"], 600.0
        )
        self.assertEqual(
            constructor.call_args.kwargs["hard_request_deadline_seconds"], 600.0
        )

    def test_v6_wrong_a5_and_a7_ids_fail_before_provider(self):
        for run_id in (
            "v2-teacher-pilot-live-20260813-a5",
            "v2-teacher-pilot-live-20260820-a7",
        ):
            with self.subTest(run_id=run_id):
                out = self.root / run_id
                argv = _pilot_args(out, live=True, profile=self.PROFILE)
                argv[argv.index("--run-id") + 1] = run_id
                with mock.patch.dict(
                    os.environ, self.ENDPOINT_ENV, clear=False
                ), mock.patch.object(
                    entrypoint,
                    "_build_openai_provider",
                    side_effect=AssertionError("invalid v6 built provider"),
                ) as provider, redirect_stdout(io.StringIO()), redirect_stderr(
                    io.StringIO()
                ):
                    with self.assertRaises(SystemExit) as raised:
                        entrypoint.main(argv)
                self.assertEqual(raised.exception.code, 2)
                provider.assert_not_called()
                manifest = _read_json(_single_run(out) / "run_manifest.json")
                self.assertEqual(manifest["failure_stage"], "config_validation")
                self.assertEqual(
                    manifest["completion"]["provider_calls"]["attempted"], 0
                )

    def test_v6_all_162_stop_higgsai_rows_release_only_after_complete_gate(self):
        class AllStopProvider:
            model = "MiniMax-M2.7"

            def __init__(self):
                self.request_count = 0
                self.response_count = 0
                self.network_access = False
                self.batch_sizes = []
                self.strict_sequential = None

            async def complete_many(
                self,
                prompts,
                *,
                before_attempt=None,
                on_completion=None,
                strict_sequential=False,
            ):
                self.batch_sizes.append(len(prompts))
                self.strict_sequential = strict_sequential
                values = []
                for index, _ in enumerate(prompts):
                    if distillation.called or market.called:
                        raise AssertionError("downstream released before full Teacher gate")
                    if before_attempt is not None:
                        before_attempt(index)
                    self.request_count += 1
                    self.response_count += 1
                    self.network_access = True
                    completion = entrypoint.TeacherCompletion(
                        raw_response=(
                            '{"action":"hold","intensity":0,'
                            '"reasoning":"private synthetic v6 stop fixture"}'
                        ),
                        reported_model="HiggsAI",
                        reported_model_raw="HiggsAI",
                        input_tokens=7,
                        output_tokens=5,
                        response_id="v6-all-stop-{}".format(index),
                        finish_reason="stop",
                        finish_reason_raw="stop",
                    )
                    values.append(completion)
                    if on_completion is not None:
                        on_completion(index, completion)
                return values

            async def aclose(self):
                return None

        class DownstreamReached(RuntimeError):
            pass

        provider = AllStopProvider()
        downstream_request_counts = []

        def stop_at_distillation(*args, **kwargs):
            downstream_request_counts.append(provider.request_count)
            raise DownstreamReached("full gate released")

        distillation = mock.Mock(side_effect=stop_at_distillation)
        market = mock.Mock(
            side_effect=AssertionError("market ran past distillation sentinel")
        )
        out = self.root / "all-162-stop"
        with mock.patch.dict(
            os.environ, self.ENDPOINT_ENV, clear=False
        ), mock.patch.object(
            entrypoint, "_build_openai_provider", return_value=provider
        ), mock.patch.object(
            entrypoint, "run_distillation_phase", distillation
        ), mock.patch.object(
            entrypoint, "run_market_phase", market
        ), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                entrypoint.main(_pilot_args(out, live=True, profile=self.PROFILE))
        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(provider.batch_sizes, [162])
        self.assertTrue(provider.strict_sequential)
        self.assertEqual(provider.request_count, 162)
        self.assertEqual(provider.response_count, 162)
        self.assertEqual(downstream_request_counts, [162])
        distillation.assert_called_once()
        market.assert_not_called()

        run_dir = _single_run(out)
        manifest = _read_json(run_dir / "run_manifest.json")
        rows = _read_jsonl(run_dir / "teacher_samples.jsonl")
        teacher = manifest["v2_attention_market"]["teacher_samples"]
        gate = manifest["v2_attention_market"]["teacher_acceptance_gate"]
        self.assertEqual(len(rows), 162)
        self.assertEqual(
            {row["schema_version"] for row in rows},
            {"v2_teacher_request/0.2"},
        )
        self.assertEqual({row["finish_reason"] for row in rows}, {"stop"})
        self.assertEqual({row["reported_model"] for row in rows}, {"HiggsAI"})
        self.assertEqual({row["status"] for row in rows}, {"valid"})
        self.assertEqual(teacher["attempted"], 162)
        self.assertEqual(teacher["resolved"], 162)
        self.assertEqual(teacher["valid"], 162)
        self.assertEqual(teacher["failed"], 0)
        self.assertEqual(teacher["skipped"], 0)
        self.assertEqual(manifest["honest_n_teacher_samples"], 162)
        self.assertEqual(gate["status"], "passed")
        self.assertEqual(gate["canary_status"], "passed")
        self.assertEqual(gate["required_finish_reason"], "stop")
        self.assertTrue(gate["student_and_market_released"])

    def test_v6_length_still_fails_closed_without_downstream_release(self):
        from nmsim import v2_attention

        raw = v2_attention.fake_test_teacher(
            v2_attention.generate_state_design(
                54, 20260811, study_id="v2-attention-market"
            )[0],
            0,
        )

        class LengthProvider:
            model = "MiniMax-M2.7"

            def __init__(self):
                self.request_count = 0
                self.response_count = 0
                self.network_access = False
                self.batch_sizes = []

            async def complete_many(
                self,
                prompts,
                *,
                before_attempt=None,
                on_completion=None,
                strict_sequential=False,
            ):
                self.batch_sizes.append(len(prompts))
                self.strict_sequential = strict_sequential
                if before_attempt is not None:
                    before_attempt(0)
                self.request_count = 1
                self.response_count = 1
                self.network_access = True
                completion = entrypoint.TeacherCompletion(
                    raw_response=raw,
                    reported_model="HiggsAI",
                    reported_model_raw="HiggsAI",
                    input_tokens=710,
                    output_tokens=16384,
                    response_id="v6-length-canary",
                    finish_reason="length",
                    finish_reason_raw="length",
                )
                if on_completion is not None:
                    on_completion(0, completion)
                return [completion]

            async def aclose(self):
                return None

        provider = LengthProvider()
        out = self.root / "length-canary"
        with mock.patch.dict(
            os.environ, self.ENDPOINT_ENV, clear=False
        ), mock.patch.object(
            entrypoint, "_build_openai_provider", return_value=provider
        ), mock.patch.object(
            entrypoint, "run_distillation_phase"
        ) as distillation, mock.patch.object(
            entrypoint, "run_market_phase"
        ) as market, redirect_stdout(io.StringIO()), redirect_stderr(
            io.StringIO()
        ):
            with self.assertRaises(SystemExit) as raised:
                entrypoint.main(_pilot_args(out, live=True, profile=self.PROFILE))
        self.assertEqual(raised.exception.code, 1)
        self.assertTrue(provider.strict_sequential)
        self.assertEqual(provider.request_count, 1)
        distillation.assert_not_called()
        market.assert_not_called()

        run_dir = _single_run(out)
        manifest = _read_json(run_dir / "run_manifest.json")
        rows = _read_jsonl(run_dir / "teacher_samples.jsonl")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["schema_version"], "v2_teacher_request/0.2")
        self.assertEqual(rows[0]["failure_code"], "finish_reason_invalid")
        self.assertEqual(rows[0]["finish_reason"], "length")
        teacher = manifest["v2_attention_market"]["teacher_samples"]
        gate = manifest["v2_attention_market"]["teacher_acceptance_gate"]
        self.assertEqual(teacher["attempted"], 1)
        self.assertEqual(teacher["valid"], 0)
        self.assertEqual(teacher["skipped"], 161)
        self.assertEqual(manifest["honest_n_teacher_samples"], 0)
        self.assertEqual(manifest["completion"]["parsing"]["attempted"], 0)
        self.assertEqual(
            manifest["completion"]["application_provider_attempts"][
                "finish_reason_counts"
            ],
            {"length": 1},
        )
        self.assertEqual(gate["status"], "failed")
        self.assertEqual(gate["reason_codes"], ["finish_reason_invalid"])
        self.assertFalse(gate["student_and_market_released"])
        self.assertFalse((run_dir / "aggregated_dataset.json").exists())
        self.assertFalse((run_dir / "market_2x2_summary.json").exists())

    def test_v6_a5_and_a6_directories_are_o_excl_immutable(self):
        for run_id in (
            "v2-teacher-pilot-live-20260813-a5",
            "v2-teacher-pilot-live-20260820-a6",
        ):
            with self.subTest(run_id=run_id):
                out = self.root / run_id
                run_dir = out / "runs" / run_id
                run_dir.mkdir(parents=True)
                sentinel = run_dir / "immutable-sentinel.bin"
                sentinel.write_bytes(b"immutable-v6-boundary\x00")
                before_hash = _sha256(sentinel)
                before_entries = sorted(path.name for path in run_dir.iterdir())
                argv = _pilot_args(out, live=True, profile=self.PROFILE)
                argv[argv.index("--run-id") + 1] = run_id
                with mock.patch.dict(
                    os.environ, self.ENDPOINT_ENV, clear=False
                ), mock.patch.object(
                    entrypoint,
                    "_build_openai_provider",
                    side_effect=AssertionError("O_EXCL path built provider"),
                ) as provider, redirect_stdout(io.StringIO()), redirect_stderr(
                    io.StringIO()
                ):
                    with self.assertRaises((SystemExit, FileExistsError)) as raised:
                        entrypoint.main(argv)
                if isinstance(raised.exception, SystemExit):
                    self.assertEqual(raised.exception.code, 2)
                provider.assert_not_called()
                self.assertEqual(_sha256(sentinel), before_hash)
                self.assertEqual(
                    sorted(path.name for path in run_dir.iterdir()), before_entries
                )


class V7ExpandedOutputAndTimeoutSuccessorTests(unittest.TestCase):
    ENDPOINT_ENV = V2FrozenTeacherPilotTests.ENDPOINT_ENV
    PROFILE = (
        entrypoint.MINIMAX_M27_HIGGSAI_TIMEOUT1800_OUTPUT65536_JOINT54X3_PILOT
    )
    V6_PROFILE = (
        entrypoint.MINIMAX_M27_HIGGSAI_TIMEOUT600_OUTPUT16384_JOINT54X3_PILOT
    )

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)

    def _identities(self, profile: str, out_name: str) -> dict:
        from nmsim import v2_attention

        args = entrypoint.build_argparser().parse_args(
            _pilot_args(self.root / out_name, profile=profile)
        )
        entrypoint._validate_args(args)
        observations = v2_attention.generate_state_design(
            54, 20260811, study_id="v2-attention-market"
        )
        return entrypoint.build_v2_identities(
            args,
            observations,
            repo_root=Path(entrypoint.__file__).resolve().parents[1],
        )

    def test_v7_descriptor_and_dry_run_freeze_a6_with_zero_network(self):
        out = self.root / "v7-dry"
        with mock.patch.dict(
            os.environ, self.ENDPOINT_ENV, clear=False
        ), mock.patch.object(
            entrypoint,
            "_build_openai_provider",
            side_effect=AssertionError("v7 dry-run constructed provider"),
        ) as provider, mock.patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError("v7 dry-run opened socket"),
        ) as network, redirect_stdout(io.StringIO()):
            entrypoint.main(_pilot_args(out, profile=self.PROFILE))
        provider.assert_not_called()
        network.assert_not_called()

        run_dir = _single_run(out)
        manifest = _read_json(run_dir / "run_manifest.json")
        summary = _read_json(run_dir / "dry_run_summary.json")
        profile = entrypoint.pilot_profile_descriptor(self.PROFILE)
        self.assertEqual(
            entrypoint.stable_hash(profile),
            "3a0b8e1f5109bab30058867886cda4b74af6d0b89b7d7075cada9c69066f96ea",
        )
        self.assertEqual(profile["schema_version"], "v2_teacher_pilot_profile/0.7")
        self.assertEqual(
            profile["successor_scope"], "output_budget_and_execution_timeout"
        )
        self.assertEqual(profile["max_tokens"], 65536)
        self.assertEqual(profile["httpx_phase_inactivity_timeout_seconds"], 1800)
        self.assertEqual(profile["hard_request_deadline_seconds"], 1800)
        self.assertEqual(profile["connect_timeout_seconds"], 10)
        self.assertEqual(
            profile["required_run_ids"],
            {
                "dry_run": "v2-teacher-pilot-v7-dry-20260820-a1",
                "live": "v2-teacher-pilot-live-20260820-a7",
            },
        )
        self.assertEqual(
            [row["run_id"] for row in profile["predecessor_failed_runs"]],
            [
                "v2-teacher-pilot-live-20260812-a1",
                "v2-teacher-pilot-live-20260812-a2",
                "v2-teacher-pilot-live-20260813-a3",
                "v2-teacher-pilot-live-20260813-a4",
                "v2-teacher-pilot-live-20260813-a5",
                "v2-teacher-pilot-live-20260820-a6",
            ],
        )
        self.assertEqual(
            profile["predecessor_failed_runs"][-1],
            {
                "run_id": "v2-teacher-pilot-live-20260820-a6",
                "status": "failed",
                "attempted": 33,
                "responses": 33,
                "valid": 32,
                "honest_n": 32,
                "skipped": 129,
                "failure_code": "provider_response_shape_invalid",
                "finish_reason": "length",
                "output_tokens": 16384,
                "parsing_succeeded": 32,
                "student_runs": 0,
                "market_runs": 0,
                "run_manifest_sha256": (
                    "341033808a8c15f02282100ab56b05edd14838fec004f7e65be7737a970fcfbb"
                ),
                "reuse_supplement_or_merge_forbidden": True,
            },
        )
        self.assertEqual(summary["provider_calls"], 0)
        self.assertFalse(summary["network_access"])
        self.assertEqual(summary["planned_teacher_requests"], 162)
        self.assertEqual(summary["pilot_profile"], profile)

        identities = manifest["v2_config_identities"]
        request = identities["model_request_config"]
        execution = identities["execution_config"]
        self.assertEqual(request["schema_version"], "v2_teacher_request/0.2")
        self.assertEqual(request["max_tokens"], 65536)
        self.assertEqual(request["temperature"], 0.0)
        self.assertNotIn("top_p", request)
        self.assertNotIn("top_k", request)
        self.assertEqual(execution["schema_version"], "v2_attention_execution/0.2")
        self.assertEqual(execution["httpx_phase_inactivity_timeout_seconds"], 1800.0)
        self.assertEqual(execution["hard_request_deadline_seconds"], 1800.0)
        self.assertEqual(execution["connect_timeout_seconds"], 10.0)
        self.assertEqual(execution["provider_retry_count"], 0)
        pilot_input = next(
            row
            for row in manifest["inputs"]
            if row["label"] == "v2_teacher_pilot_protocol"
        )
        self.assertTrue(pilot_input["path"].endswith("V2_TEACHER_PILOT_V7.md"))

    def test_v7_changes_only_profile_output_budget_and_two_timeout_fields(self):
        from nmsim import v2_attention

        expected_descriptor_hashes = {
            entrypoint.MINIMAX_M27_JOINT54X3_PILOT: (
                "1228cd39c038771a916fb747e1e767218874232ddb1bad4f16d1f3d5a2712d1a"
            ),
            entrypoint.MINIMAX_M27_REQUEST_HIGGSAI_REPORTED_JOINT54X3_PILOT: (
                "3f586f02974265e243bc49a0f925eb7e274a2554b4aaee6e706a354becacfebc"
            ),
            entrypoint.MINIMAX_M27_HIGGSAI_FINISH_AUDIT_JOINT54X3_PILOT: (
                "c042e41f2263f7c3ee093f7c6b258ee8972edb3a4db44bcae725cc6e0e00aa3e"
            ),
            entrypoint.MINIMAX_M27_HIGGSAI_FINISH_AUDIT_EXTERNAL_JOINT54X3_PILOT: (
                "5851865c1a22ec2d772524db55e2f57d9bd7e101a02eb7be692bd5f42da5f9b5"
            ),
            entrypoint.MINIMAX_M27_HIGGSAI_LONG_TIMEOUT_JOINT54X3_PILOT: (
                "5be40a9f4ed6ea858d8997360512d6ba5587813d9a1d81460af65960289a1c03"
            ),
            entrypoint.MINIMAX_M27_HIGGSAI_TIMEOUT600_OUTPUT16384_JOINT54X3_PILOT: (
                "eb5540b0049d0f7a6ecbb640e372bdced2e11deac3f5746e24b80de3f5c40d30"
            ),
        }
        for profile_id, expected_hash in expected_descriptor_hashes.items():
            with self.subTest(legacy_profile=profile_id):
                self.assertEqual(
                    entrypoint.stable_hash(
                        entrypoint.pilot_profile_descriptor(profile_id)
                    ),
                    expected_hash,
                )

        with mock.patch.dict(os.environ, self.ENDPOINT_ENV, clear=False):
            v6 = self._identities(self.V6_PROFILE, "v6-identities")
            v7 = self._identities(self.PROFILE, "v7-identities")

        v6_science = json.loads(json.dumps(v6["scientific_config"]))
        v7_science = json.loads(json.dumps(v7["scientific_config"]))
        self.assertEqual(
            v6_science["teacher_sampling"].pop("pilot_profile_id"),
            self.V6_PROFILE,
        )
        self.assertEqual(
            v7_science["teacher_sampling"].pop("pilot_profile_id"), self.PROFILE
        )
        self.assertEqual(v7_science, v6_science)

        v6_request = json.loads(json.dumps(v6["model_request_config"]))
        v7_request = json.loads(json.dumps(v7["model_request_config"]))
        self.assertEqual(v6_request.pop("pilot_profile_id"), self.V6_PROFILE)
        self.assertEqual(v7_request.pop("pilot_profile_id"), self.PROFILE)
        self.assertEqual(v6_request.pop("max_tokens"), 16384)
        self.assertEqual(v7_request.pop("max_tokens"), 65536)
        self.assertEqual(v7_request, v6_request)
        self.assertEqual(v7["model_request_config"]["temperature"], 0.0)
        self.assertNotIn("top_p", v7["model_request_config"])
        self.assertNotIn("top_k", v7["model_request_config"])

        v6_execution = json.loads(json.dumps(v6["execution_config"]))
        v7_execution = json.loads(json.dumps(v7["execution_config"]))
        for execution, expected_profile in (
            (v6_execution, self.V6_PROFILE),
            (v7_execution, self.PROFILE),
        ):
            self.assertEqual(execution["schema_version"], "v2_attention_execution/0.2")
            self.assertEqual(execution.pop("pilot_profile_id"), expected_profile)
            execution.pop("output_root_identity_sha256")
            execution.pop("caller_run_id")
        self.assertEqual(
            v6_execution.pop("httpx_phase_inactivity_timeout_seconds"), 600.0
        )
        self.assertEqual(v6_execution.pop("hard_request_deadline_seconds"), 600.0)
        self.assertEqual(
            v7_execution.pop("httpx_phase_inactivity_timeout_seconds"), 1800.0
        )
        self.assertEqual(v7_execution.pop("hard_request_deadline_seconds"), 1800.0)
        self.assertEqual(v7_execution, v6_execution)
        self.assertEqual(v7_execution["connect_timeout_seconds"], 10.0)
        self.assertEqual(v7_execution["provider_retry_count"], 0)

        observations = v2_attention.generate_state_design(
            54, 20260811, study_id="v2-attention-market"
        )
        rendered = [v2_attention.render_teacher_prompt(row) for row in observations]
        plan = entrypoint._sample_plan(observations, 3)
        sample_ids = [item["sample_id"] for item in plan]
        v6_profile = entrypoint.pilot_profile_descriptor(self.V6_PROFILE)
        v7_profile = entrypoint.pilot_profile_descriptor(self.PROFILE)
        self.assertEqual(entrypoint.SAMPLE_IDENTITY_SCHEMA_VERSION, "v2_teacher_request/0.1")
        self.assertEqual(
            entrypoint.stable_hash([row.to_dict() for row in observations]),
            v7_profile["state_design_hash"],
        )
        self.assertEqual(
            entrypoint.stable_hash([prompt.to_messages() for prompt in rendered]),
            "201d981933acf51f5150525d2a3280c44f8022aa127fe0827247e248c6cbfe5f",
        )
        self.assertEqual(len(sample_ids), 162)
        self.assertEqual(
            entrypoint.stable_hash(sample_ids),
            v7_profile["planned_sample_order_hash"],
        )
        self.assertEqual(v7_profile["canary_sample_id"], sample_ids[0])
        for key in (
            "state_design_hash",
            "planned_split_hash",
            "planned_split_counts",
            "planned_sample_order_hash",
            "canary_sample_id",
            "temperature",
            "model_requested",
            "required_reported_model",
            "response_termination_contract",
            "teacher_acceptance_gate",
            "transport_release_policy",
            "connect_timeout_seconds",
            "training_epochs",
            "market_agents",
            "market_rounds",
            "market_seeds",
        ):
            with self.subTest(frozen_field=key):
                self.assertEqual(v7_profile[key], v6_profile[key])

    def test_v7_actual_provider_transport_and_wire_kwargs_are_exact(self):
        import httpx

        http_client = object()
        with mock.patch.dict(
            os.environ, self.ENDPOINT_ENV, clear=False
        ), mock.patch(
            "httpx.AsyncClient", return_value=http_client
        ) as async_client, mock.patch("openai.AsyncOpenAI") as openai_client:
            provider = entrypoint.OpenAITeacherProvider(
                model="MiniMax-M2.7",
                temperature=0.0,
                max_tokens=65536,
                workers=1,
                request_timeout_seconds=1800.0,
                hard_request_deadline_seconds=1800.0,
            )
        timeout = async_client.call_args.kwargs["timeout"]
        self.assertIsInstance(timeout, httpx.Timeout)
        self.assertEqual(timeout.connect, 10.0)
        self.assertEqual(timeout.read, 1800.0)
        self.assertEqual(timeout.write, 1800.0)
        self.assertEqual(timeout.pool, 1800.0)
        self.assertFalse(async_client.call_args.kwargs["trust_env"])
        self.assertIs(openai_client.call_args.kwargs["http_client"], http_client)
        self.assertEqual(openai_client.call_args.kwargs["max_retries"], 0)
        self.assertEqual(provider.max_tokens, 65536)
        self.assertEqual(provider.temperature, 0.0)
        self.assertEqual(provider.request_timeout_seconds, 1800.0)
        self.assertEqual(provider.hard_request_deadline_seconds, 1800.0)
        self.assertEqual(provider.connect_timeout_seconds, 10.0)
        self.assertEqual(provider.provider_retry_count, 0)

        args = entrypoint.build_argparser().parse_args(
            _pilot_args(self.root / "provider-build", profile=self.PROFILE)
        )
        with mock.patch.dict(os.environ, self.ENDPOINT_ENV, clear=False):
            entrypoint._validate_args(args)
        sentinel = object()
        with mock.patch.object(
            entrypoint, "OpenAITeacherProvider", return_value=sentinel
        ) as constructor:
            self.assertIs(entrypoint._build_openai_provider(args), sentinel)
        self.assertEqual(constructor.call_args.kwargs["max_tokens"], 65536)
        self.assertEqual(constructor.call_args.kwargs["temperature"], 0.0)
        self.assertEqual(
            constructor.call_args.kwargs["request_timeout_seconds"], 1800.0
        )
        self.assertEqual(
            constructor.call_args.kwargs["hard_request_deadline_seconds"], 1800.0
        )

        class Completions:
            async def create(self, **kwargs):
                self.kwargs = kwargs
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            finish_reason="stop",
                            message=SimpleNamespace(content='{"ok":true}'),
                        )
                    ],
                    model="HiggsAI",
                    id="v7-wire-audit",
                    usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
                )

        api = Completions()
        wire_provider = object.__new__(entrypoint.OpenAITeacherProvider)
        wire_provider._client = SimpleNamespace(
            chat=SimpleNamespace(completions=api)
        )
        wire_provider.model = "MiniMax-M2.7"
        wire_provider.temperature = 0.0
        wire_provider.max_tokens = 65536
        wire_provider.workers = 1
        wire_provider.hard_request_deadline_seconds = 1800.0
        wire_provider.network_access = False
        wire_provider.request_count = 0
        wire_provider.response_count = 0
        wire_provider.batch_sizes = []
        with mock.patch(
            "experiments.v2_attention_market.asyncio.wait_for",
            wraps=asyncio.wait_for,
        ) as wait_for:
            completion = asyncio.run(
                wire_provider.complete_many(
                    [("system", "user")], strict_sequential=True
                )
            )[0]
        self.assertEqual(wait_for.call_args.kwargs["timeout"], 1800.0)
        self.assertEqual(
            api.kwargs,
            {
                "model": "MiniMax-M2.7",
                "max_tokens": 65536,
                "temperature": 0.0,
                "messages": [
                    {"role": "system", "content": "system"},
                    {"role": "user", "content": "user"},
                ],
            },
        )
        self.assertNotIn("top_p", api.kwargs)
        self.assertNotIn("top_k", api.kwargs)
        self.assertEqual(completion.finish_reason, "stop")

    def test_v7_mutated_budget_and_wrong_a6_a8_ids_fail_before_provider(self):
        cases = (
            ("max-16384", {"max_tokens": 16384}, None),
            ("max-65535", {"max_tokens": 65535}, None),
            ("max-65537", {"max_tokens": 65537}, None),
            ("a6", {}, "v2-teacher-pilot-live-20260820-a6"),
            ("a8", {}, "v2-teacher-pilot-live-20260820-a8"),
        )
        for name, overrides, run_id in cases:
            with self.subTest(name=name):
                out = self.root / name
                argv = _pilot_args(
                    out, live=True, profile=self.PROFILE, **overrides
                )
                if run_id is not None:
                    argv[argv.index("--run-id") + 1] = run_id
                with mock.patch.dict(
                    os.environ, self.ENDPOINT_ENV, clear=False
                ), mock.patch.object(
                    entrypoint,
                    "_build_openai_provider",
                    side_effect=AssertionError("invalid v7 built provider"),
                ) as provider, redirect_stdout(io.StringIO()), redirect_stderr(
                    io.StringIO()
                ):
                    with self.assertRaises(SystemExit) as raised:
                        entrypoint.main(argv)
                self.assertEqual(raised.exception.code, 2)
                provider.assert_not_called()
                manifest = _read_json(_single_run(out) / "run_manifest.json")
                self.assertEqual(manifest["failure_stage"], "config_validation")
                self.assertEqual(
                    manifest["completion"]["provider_calls"]["attempted"], 0
                )

    def test_v7_all_162_stop_higgsai_rows_release_only_after_complete_gate(self):
        class AllStopProvider:
            model = "MiniMax-M2.7"

            def __init__(self):
                self.request_count = 0
                self.response_count = 0
                self.network_access = False
                self.batch_sizes = []
                self.strict_sequential = None

            async def complete_many(
                self,
                prompts,
                *,
                before_attempt=None,
                on_completion=None,
                strict_sequential=False,
            ):
                self.batch_sizes.append(len(prompts))
                self.strict_sequential = strict_sequential
                values = []
                for index, _ in enumerate(prompts):
                    if distillation.called or market.called:
                        raise AssertionError("downstream released before full Teacher gate")
                    if before_attempt is not None:
                        before_attempt(index)
                    self.request_count += 1
                    self.response_count += 1
                    self.network_access = True
                    completion = entrypoint.TeacherCompletion(
                        raw_response=(
                            '{"action":"hold","intensity":0,'
                            '"reasoning":"private synthetic v7 stop fixture"}'
                        ),
                        reported_model="HiggsAI",
                        reported_model_raw="HiggsAI",
                        input_tokens=7,
                        output_tokens=5,
                        response_id="v7-all-stop-{}".format(index),
                        finish_reason="stop",
                        finish_reason_raw="stop",
                    )
                    values.append(completion)
                    if on_completion is not None:
                        on_completion(index, completion)
                return values

            async def aclose(self):
                return None

        class DownstreamReached(RuntimeError):
            pass

        provider = AllStopProvider()
        downstream_request_counts = []

        def stop_at_distillation(*args, **kwargs):
            downstream_request_counts.append(provider.request_count)
            raise DownstreamReached("full gate released")

        distillation = mock.Mock(side_effect=stop_at_distillation)
        market = mock.Mock(
            side_effect=AssertionError("market ran past distillation sentinel")
        )
        out = self.root / "all-162-stop"
        with mock.patch.dict(
            os.environ, self.ENDPOINT_ENV, clear=False
        ), mock.patch.object(
            entrypoint, "_build_openai_provider", return_value=provider
        ), mock.patch.object(
            entrypoint, "run_distillation_phase", distillation
        ), mock.patch.object(
            entrypoint, "run_market_phase", market
        ), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                entrypoint.main(_pilot_args(out, live=True, profile=self.PROFILE))
        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(provider.batch_sizes, [162])
        self.assertTrue(provider.strict_sequential)
        self.assertEqual(provider.request_count, 162)
        self.assertEqual(provider.response_count, 162)
        self.assertEqual(downstream_request_counts, [162])
        distillation.assert_called_once()
        market.assert_not_called()

        run_dir = _single_run(out)
        manifest = _read_json(run_dir / "run_manifest.json")
        rows = _read_jsonl(run_dir / "teacher_samples.jsonl")
        teacher = manifest["v2_attention_market"]["teacher_samples"]
        gate = manifest["v2_attention_market"]["teacher_acceptance_gate"]
        self.assertEqual(len(rows), 162)
        self.assertEqual(
            {row["schema_version"] for row in rows},
            {"v2_teacher_request/0.2"},
        )
        self.assertEqual({row["finish_reason"] for row in rows}, {"stop"})
        self.assertEqual({row["reported_model"] for row in rows}, {"HiggsAI"})
        self.assertEqual({row["status"] for row in rows}, {"valid"})
        self.assertEqual(teacher["attempted"], 162)
        self.assertEqual(teacher["resolved"], 162)
        self.assertEqual(teacher["valid"], 162)
        self.assertEqual(teacher["failed"], 0)
        self.assertEqual(teacher["skipped"], 0)
        self.assertEqual(manifest["honest_n_teacher_samples"], 162)
        self.assertEqual(gate["status"], "passed")
        self.assertEqual(gate["canary_status"], "passed")
        self.assertEqual(gate["required_finish_reason"], "stop")
        self.assertTrue(gate["student_and_market_released"])

    def test_v7_length_at_65536_fails_closed_and_sdk_envelope_stays_private(self):
        from nmsim import v2_attention

        raw = v2_attention.fake_test_teacher(
            v2_attention.generate_state_design(
                54, 20260811, study_id="v2-attention-market"
            )[0],
            0,
        )
        private_marker = "PRIVATE_V7_SDK_ENVELOPE_MARKER"

        class LengthProvider:
            model = "MiniMax-M2.7"

            def __init__(self):
                self.request_count = 0
                self.response_count = 0
                self.network_access = False
                self.batch_sizes = []

            async def complete_many(
                self,
                prompts,
                *,
                before_attempt=None,
                on_completion=None,
                strict_sequential=False,
            ):
                self.batch_sizes.append(len(prompts))
                self.strict_sequential = strict_sequential
                if before_attempt is not None:
                    before_attempt(0)
                self.request_count = 1
                self.response_count = 1
                self.network_access = True
                completion = entrypoint.TeacherCompletion(
                    raw_response=raw,
                    reported_model="HiggsAI",
                    reported_model_raw="HiggsAI",
                    input_tokens=710,
                    output_tokens=65536,
                    response_id="v7-length-canary",
                    finish_reason="length",
                    finish_reason_raw="length",
                    provider_sdk_response_json=private_marker,
                )
                if on_completion is not None:
                    on_completion(0, completion)
                return [completion]

            async def aclose(self):
                return None

        provider = LengthProvider()
        out = self.root / "length-canary"
        with mock.patch.dict(
            os.environ, self.ENDPOINT_ENV, clear=False
        ), mock.patch.object(
            entrypoint, "_build_openai_provider", return_value=provider
        ), mock.patch.object(
            entrypoint, "run_distillation_phase"
        ) as distillation, mock.patch.object(
            entrypoint, "run_market_phase"
        ) as market, redirect_stdout(io.StringIO()), redirect_stderr(
            io.StringIO()
        ):
            with self.assertRaises(SystemExit) as raised:
                entrypoint.main(_pilot_args(out, live=True, profile=self.PROFILE))
        self.assertEqual(raised.exception.code, 1)
        self.assertTrue(provider.strict_sequential)
        self.assertEqual(provider.request_count, 1)
        distillation.assert_not_called()
        market.assert_not_called()

        run_dir = _single_run(out)
        manifest_path = run_dir / "run_manifest.json"
        public_path = run_dir / "teacher_samples.jsonl"
        private_path = run_dir / "private_teacher_records.jsonl"
        manifest = _read_json(manifest_path)
        rows = _read_jsonl(public_path)
        self.assertEqual(rows[0]["schema_version"], "v2_teacher_request/0.2")
        self.assertEqual(rows[0]["failure_code"], "finish_reason_invalid")
        self.assertEqual(rows[0]["finish_reason"], "length")
        self.assertNotIn(private_marker, public_path.read_text(encoding="utf-8"))
        self.assertNotIn(private_marker, manifest_path.read_text(encoding="utf-8"))
        self.assertIn(private_marker, private_path.read_text(encoding="utf-8"))
        self.assertEqual(stat.S_IMODE(private_path.stat().st_mode), 0o600)
        teacher = manifest["v2_attention_market"]["teacher_samples"]
        gate = manifest["v2_attention_market"]["teacher_acceptance_gate"]
        self.assertEqual(teacher["attempted"], 1)
        self.assertEqual(teacher["valid"], 0)
        self.assertEqual(teacher["skipped"], 161)
        self.assertEqual(manifest["honest_n_teacher_samples"], 0)
        self.assertEqual(manifest["completion"]["parsing"]["attempted"], 0)
        self.assertEqual(
            manifest["completion"]["application_provider_attempts"][
                "finish_reason_counts"
            ],
            {"length": 1},
        )
        self.assertEqual(gate["status"], "failed")
        self.assertEqual(gate["reason_codes"], ["finish_reason_invalid"])
        self.assertFalse(gate["student_and_market_released"])
        self.assertFalse((run_dir / "aggregated_dataset.json").exists())
        self.assertFalse((run_dir / "market_2x2_summary.json").exists())

    def test_v7_a6_and_a7_directories_are_o_excl_immutable(self):
        for run_id in (
            "v2-teacher-pilot-live-20260820-a6",
            "v2-teacher-pilot-live-20260820-a7",
        ):
            with self.subTest(run_id=run_id):
                out = self.root / run_id
                run_dir = out / "runs" / run_id
                run_dir.mkdir(parents=True)
                sentinel = run_dir / "immutable-sentinel.bin"
                sentinel.write_bytes(b"immutable-v7-boundary\x00")
                before_hash = _sha256(sentinel)
                before_entries = sorted(path.name for path in run_dir.iterdir())
                argv = _pilot_args(out, live=True, profile=self.PROFILE)
                argv[argv.index("--run-id") + 1] = run_id
                with mock.patch.dict(
                    os.environ, self.ENDPOINT_ENV, clear=False
                ), mock.patch.object(
                    entrypoint,
                    "_build_openai_provider",
                    side_effect=AssertionError("O_EXCL path built provider"),
                ) as provider, redirect_stdout(io.StringIO()), redirect_stderr(
                    io.StringIO()
                ):
                    with self.assertRaises((SystemExit, FileExistsError)) as raised:
                        entrypoint.main(argv)
                if isinstance(raised.exception, SystemExit):
                    self.assertEqual(raised.exception.code, 2)
                provider.assert_not_called()
                self.assertEqual(_sha256(sentinel), before_hash)
                self.assertEqual(
                    sorted(path.name for path in run_dir.iterdir()), before_entries
                )


class V8OfficialSamplingNearContextSuccessorTests(unittest.TestCase):
    ENDPOINT_ENV = V2FrozenTeacherPilotTests.ENDPOINT_ENV
    PROFILE = (
        entrypoint.MINIMAX_M27_HIGGSAI_T1_P095_K40_TIMEOUT7200_OUTPUT190000_JOINT54X3_PILOT
    )
    V7_PROFILE = (
        entrypoint.MINIMAX_M27_HIGGSAI_TIMEOUT1800_OUTPUT65536_JOINT54X3_PILOT
    )
    SAMPLING = {
        "temperature": 1.0,
        "max_tokens": 190000,
        "top_p": 0.95,
        "top_k": 40,
    }

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)

    def _identities(self, profile: str, out_name: str) -> dict:
        from nmsim import v2_attention

        args = entrypoint.build_argparser().parse_args(
            _pilot_args(self.root / out_name, profile=profile)
        )
        entrypoint._validate_args(args)
        observations = v2_attention.generate_state_design(
            54, 20260811, study_id="v2-attention-market"
        )
        return entrypoint.build_v2_identities(
            args,
            observations,
            repo_root=Path(entrypoint.__file__).resolve().parents[1],
        )

    def test_v8_descriptor_and_dry_run_freeze_a7_with_zero_network(self):
        out = self.root / "v8-dry"
        with mock.patch.dict(
            os.environ, self.ENDPOINT_ENV, clear=False
        ), mock.patch.object(
            entrypoint,
            "_build_openai_provider",
            side_effect=AssertionError("v8 dry-run constructed provider"),
        ) as provider, mock.patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError("v8 dry-run opened socket"),
        ) as network, redirect_stdout(io.StringIO()):
            entrypoint.main(_pilot_args(out, profile=self.PROFILE))
        provider.assert_not_called()
        network.assert_not_called()

        run_dir = _single_run(out)
        manifest = _read_json(run_dir / "run_manifest.json")
        summary = _read_json(run_dir / "dry_run_summary.json")
        profile = entrypoint.pilot_profile_descriptor(self.PROFILE)
        self.assertEqual(
            entrypoint.stable_hash(profile),
            "21aff7395bfa7ec6d9ba87d61b971489bd811b1d5d74760f88f6806102bd280c",
        )
        self.assertEqual(profile["schema_version"], "v2_teacher_pilot_profile/0.8")
        self.assertEqual(
            profile["successor_scope"],
            "near_context_output_and_official_sampling_with_execution_timeout",
        )
        self.assertEqual(profile["temperature"], 1.0)
        self.assertEqual(profile["max_tokens"], 190000)
        self.assertEqual(profile["top_p"], 0.95)
        self.assertEqual(profile["top_k"], 40)
        self.assertEqual(profile["httpx_phase_inactivity_timeout_seconds"], 7200)
        self.assertEqual(profile["hard_request_deadline_seconds"], 7200)
        self.assertEqual(profile["connect_timeout_seconds"], 10)
        self.assertTrue(profile["causal_attribution_forbidden"])
        self.assertEqual(
            profile["required_run_ids"],
            {
                "dry_run": "v2-teacher-pilot-v8-dry-20260820-a1",
                "live": "v2-teacher-pilot-live-20260820-a8",
            },
        )
        self.assertEqual(
            [row["run_id"] for row in profile["predecessor_failed_runs"]],
            [
                "v2-teacher-pilot-live-20260812-a1",
                "v2-teacher-pilot-live-20260812-a2",
                "v2-teacher-pilot-live-20260813-a3",
                "v2-teacher-pilot-live-20260813-a4",
                "v2-teacher-pilot-live-20260813-a5",
                "v2-teacher-pilot-live-20260820-a6",
                "v2-teacher-pilot-live-20260820-a7",
            ],
        )
        self.assertEqual(
            profile["predecessor_failed_runs"][-1],
            {
                "run_id": "v2-teacher-pilot-live-20260820-a7",
                "status": "failed",
                "planned": 162,
                "attempted": 45,
                "responses": 45,
                "valid": 44,
                "honest_n": 44,
                "skipped": 117,
                "failure_code": "provider_response_shape_invalid",
                "finish_reason": "length",
                "input_tokens": 745,
                "output_tokens": 65536,
                "parsing_succeeded": 44,
                "student_runs": 0,
                "market_runs": 0,
                "run_manifest_sha256": (
                    "137ebb06875c6853a73f58519c90168d9ba7fc879c15c00433d484f277453641"
                ),
                "reuse_supplement_or_merge_forbidden": True,
            },
        )
        self.assertEqual(summary["provider_calls"], 0)
        self.assertFalse(summary["network_access"])
        self.assertEqual(summary["planned_teacher_requests"], 162)
        self.assertEqual(summary["pilot_profile"], profile)

        identities = manifest["v2_config_identities"]
        request = identities["model_request_config"]
        execution = identities["execution_config"]
        self.assertEqual(request["schema_version"], "v2_teacher_request/0.3")
        self.assertEqual(
            {key: request[key] for key in self.SAMPLING}, self.SAMPLING
        )
        self.assertEqual(execution["schema_version"], "v2_attention_execution/0.2")
        self.assertEqual(execution["httpx_phase_inactivity_timeout_seconds"], 7200.0)
        self.assertEqual(execution["hard_request_deadline_seconds"], 7200.0)
        self.assertEqual(execution["connect_timeout_seconds"], 10.0)
        self.assertEqual(execution["provider_retry_count"], 0)
        pilot_input = next(
            row
            for row in manifest["inputs"]
            if row["label"] == "v2_teacher_pilot_protocol"
        )
        self.assertTrue(pilot_input["path"].endswith("V2_TEACHER_PILOT_V8.md"))

    def test_v8_changes_only_frozen_request_and_timeout_fields_from_v7(self):
        from nmsim import v2_attention

        expected_descriptor_hashes = {
            entrypoint.MINIMAX_M27_JOINT54X3_PILOT: (
                "1228cd39c038771a916fb747e1e767218874232ddb1bad4f16d1f3d5a2712d1a"
            ),
            entrypoint.MINIMAX_M27_REQUEST_HIGGSAI_REPORTED_JOINT54X3_PILOT: (
                "3f586f02974265e243bc49a0f925eb7e274a2554b4aaee6e706a354becacfebc"
            ),
            entrypoint.MINIMAX_M27_HIGGSAI_FINISH_AUDIT_JOINT54X3_PILOT: (
                "c042e41f2263f7c3ee093f7c6b258ee8972edb3a4db44bcae725cc6e0e00aa3e"
            ),
            entrypoint.MINIMAX_M27_HIGGSAI_FINISH_AUDIT_EXTERNAL_JOINT54X3_PILOT: (
                "5851865c1a22ec2d772524db55e2f57d9bd7e101a02eb7be692bd5f42da5f9b5"
            ),
            entrypoint.MINIMAX_M27_HIGGSAI_LONG_TIMEOUT_JOINT54X3_PILOT: (
                "5be40a9f4ed6ea858d8997360512d6ba5587813d9a1d81460af65960289a1c03"
            ),
            entrypoint.MINIMAX_M27_HIGGSAI_TIMEOUT600_OUTPUT16384_JOINT54X3_PILOT: (
                "eb5540b0049d0f7a6ecbb640e372bdced2e11deac3f5746e24b80de3f5c40d30"
            ),
            entrypoint.MINIMAX_M27_HIGGSAI_TIMEOUT1800_OUTPUT65536_JOINT54X3_PILOT: (
                "3a0b8e1f5109bab30058867886cda4b74af6d0b89b7d7075cada9c69066f96ea"
            ),
        }
        for profile_id, expected_hash in expected_descriptor_hashes.items():
            with self.subTest(legacy_profile=profile_id):
                self.assertEqual(
                    entrypoint.stable_hash(
                        entrypoint.pilot_profile_descriptor(profile_id)
                    ),
                    expected_hash,
                )

        with mock.patch.dict(os.environ, self.ENDPOINT_ENV, clear=False):
            v7 = self._identities(self.V7_PROFILE, "v7-identities")
            v8 = self._identities(self.PROFILE, "v8-identities")

        v7_science = json.loads(json.dumps(v7["scientific_config"]))
        v8_science = json.loads(json.dumps(v8["scientific_config"]))
        self.assertEqual(
            v7_science["teacher_sampling"].pop("pilot_profile_id"),
            self.V7_PROFILE,
        )
        self.assertEqual(
            v8_science["teacher_sampling"].pop("pilot_profile_id"), self.PROFILE
        )
        self.assertEqual(v8_science, v7_science)

        v7_request = json.loads(json.dumps(v7["model_request_config"]))
        v8_request = json.loads(json.dumps(v8["model_request_config"]))
        self.assertEqual(v7_request.pop("schema_version"), "v2_teacher_request/0.2")
        self.assertEqual(v8_request.pop("schema_version"), "v2_teacher_request/0.3")
        self.assertEqual(v7_request.pop("pilot_profile_id"), self.V7_PROFILE)
        self.assertEqual(v8_request.pop("pilot_profile_id"), self.PROFILE)
        self.assertEqual(v7_request.pop("max_tokens"), 65536)
        self.assertEqual(v8_request.pop("max_tokens"), 190000)
        self.assertEqual(v7_request.pop("temperature"), 0.0)
        self.assertEqual(v8_request.pop("temperature"), 1.0)
        self.assertNotIn("top_p", v7_request)
        self.assertNotIn("top_k", v7_request)
        self.assertEqual(v8_request.pop("top_p"), 0.95)
        self.assertEqual(v8_request.pop("top_k"), 40)
        self.assertEqual(v8_request, v7_request)

        v7_execution = json.loads(json.dumps(v7["execution_config"]))
        v8_execution = json.loads(json.dumps(v8["execution_config"]))
        for execution, expected_profile in (
            (v7_execution, self.V7_PROFILE),
            (v8_execution, self.PROFILE),
        ):
            self.assertEqual(execution["schema_version"], "v2_attention_execution/0.2")
            self.assertEqual(execution.pop("pilot_profile_id"), expected_profile)
            execution.pop("output_root_identity_sha256")
            execution.pop("caller_run_id")
        self.assertEqual(
            v7_execution.pop("httpx_phase_inactivity_timeout_seconds"), 1800.0
        )
        self.assertEqual(v7_execution.pop("hard_request_deadline_seconds"), 1800.0)
        self.assertEqual(
            v8_execution.pop("httpx_phase_inactivity_timeout_seconds"), 7200.0
        )
        self.assertEqual(v8_execution.pop("hard_request_deadline_seconds"), 7200.0)
        self.assertEqual(v8_execution, v7_execution)

        observations = v2_attention.generate_state_design(
            54, 20260811, study_id="v2-attention-market"
        )
        rendered = [v2_attention.render_teacher_prompt(row) for row in observations]
        plan = entrypoint._sample_plan(observations, 3)
        sample_ids = [item["sample_id"] for item in plan]
        v7_profile = entrypoint.pilot_profile_descriptor(self.V7_PROFILE)
        v8_profile = entrypoint.pilot_profile_descriptor(self.PROFILE)
        self.assertEqual(entrypoint.SAMPLE_IDENTITY_SCHEMA_VERSION, "v2_teacher_request/0.1")
        self.assertEqual(
            entrypoint.stable_hash([row.to_dict() for row in observations]),
            v8_profile["state_design_hash"],
        )
        self.assertEqual(
            entrypoint.stable_hash([prompt.to_messages() for prompt in rendered]),
            "201d981933acf51f5150525d2a3280c44f8022aa127fe0827247e248c6cbfe5f",
        )
        self.assertEqual(len(sample_ids), 162)
        self.assertEqual(
            entrypoint.stable_hash(sample_ids),
            v8_profile["planned_sample_order_hash"],
        )
        self.assertEqual(v8_profile["canary_sample_id"], sample_ids[0])
        for key in (
            "state_design_hash",
            "planned_split_hash",
            "planned_split_counts",
            "planned_sample_order_hash",
            "canary_sample_id",
            "model_requested",
            "required_reported_model",
            "response_termination_contract",
            "teacher_acceptance_gate",
            "transport_release_policy",
            "connect_timeout_seconds",
            "training_epochs",
            "market_agents",
            "market_rounds",
            "market_seeds",
        ):
            with self.subTest(frozen_field=key):
                self.assertEqual(v8_profile[key], v7_profile[key])

        request = v8["model_request_config"]
        self.assertEqual(
            entrypoint.stable_hash(request), v8["v2_model_request_config_hash"]
        )
        for field, replacement in (("top_p", 0.94), ("top_k", 39)):
            mutated = json.loads(json.dumps(request))
            mutated[field] = replacement
            with self.subTest(request_hash_field=field):
                self.assertNotEqual(
                    entrypoint.stable_hash(mutated),
                    v8["v2_model_request_config_hash"],
                )

    def test_v8_actual_provider_wire_and_old_profile_omission_are_exact(self):
        import httpx

        http_client = object()
        with mock.patch.dict(
            os.environ, self.ENDPOINT_ENV, clear=False
        ), mock.patch(
            "httpx.AsyncClient", return_value=http_client
        ) as async_client, mock.patch("openai.AsyncOpenAI") as openai_client:
            provider = entrypoint.OpenAITeacherProvider(
                model="MiniMax-M2.7",
                temperature=1.0,
                max_tokens=190000,
                workers=1,
                request_timeout_seconds=7200.0,
                hard_request_deadline_seconds=7200.0,
                top_p=0.95,
                top_k=40,
            )
        timeout = async_client.call_args.kwargs["timeout"]
        self.assertIsInstance(timeout, httpx.Timeout)
        self.assertEqual(timeout.connect, 10.0)
        self.assertEqual(timeout.read, 7200.0)
        self.assertEqual(timeout.write, 7200.0)
        self.assertEqual(timeout.pool, 7200.0)
        self.assertFalse(async_client.call_args.kwargs["trust_env"])
        self.assertIs(openai_client.call_args.kwargs["http_client"], http_client)
        self.assertEqual(openai_client.call_args.kwargs["max_retries"], 0)
        self.assertEqual(provider.max_tokens, 190000)
        self.assertEqual(provider.temperature, 1.0)
        self.assertEqual(provider.top_p, 0.95)
        self.assertEqual(provider.top_k, 40)
        self.assertEqual(provider.request_timeout_seconds, 7200.0)
        self.assertEqual(provider.hard_request_deadline_seconds, 7200.0)
        self.assertEqual(provider.connect_timeout_seconds, 10.0)
        self.assertEqual(provider.provider_retry_count, 0)

        args = entrypoint.build_argparser().parse_args(
            _pilot_args(self.root / "provider-build", profile=self.PROFILE)
        )
        with mock.patch.dict(os.environ, self.ENDPOINT_ENV, clear=False):
            entrypoint._validate_args(args)
        sentinel = object()
        with mock.patch.object(
            entrypoint, "OpenAITeacherProvider", return_value=sentinel
        ) as constructor:
            self.assertIs(entrypoint._build_openai_provider(args), sentinel)
        for key, expected in {
            "temperature": 1.0,
            "max_tokens": 190000,
            "request_timeout_seconds": 7200.0,
            "hard_request_deadline_seconds": 7200.0,
            "top_p": 0.95,
            "top_k": 40,
        }.items():
            with self.subTest(constructor_field=key):
                self.assertEqual(constructor.call_args.kwargs[key], expected)

        old_profiles = (
            entrypoint.MINIMAX_M27_JOINT54X3_PILOT,
            entrypoint.MINIMAX_M27_REQUEST_HIGGSAI_REPORTED_JOINT54X3_PILOT,
            entrypoint.MINIMAX_M27_HIGGSAI_FINISH_AUDIT_JOINT54X3_PILOT,
            entrypoint.MINIMAX_M27_HIGGSAI_FINISH_AUDIT_EXTERNAL_JOINT54X3_PILOT,
            entrypoint.MINIMAX_M27_HIGGSAI_LONG_TIMEOUT_JOINT54X3_PILOT,
            entrypoint.MINIMAX_M27_HIGGSAI_TIMEOUT600_OUTPUT16384_JOINT54X3_PILOT,
            entrypoint.MINIMAX_M27_HIGGSAI_TIMEOUT1800_OUTPUT65536_JOINT54X3_PILOT,
        )
        for old_profile in old_profiles:
            with self.subTest(old_profile=old_profile):
                self.assertIsNone(entrypoint._request_top_p(old_profile))
                self.assertIsNone(entrypoint._request_top_k(old_profile))

        class Completions:
            def __init__(self):
                self.calls = []

            async def create(self, **kwargs):
                self.calls.append(kwargs)
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            finish_reason="stop",
                            message=SimpleNamespace(content='{"ok":true}'),
                        )
                    ],
                    model="HiggsAI",
                    id="sampling-wire-audit",
                    usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
                )

        async def exercise(top_p, top_k, max_tokens, temperature, deadline):
            api = Completions()
            wire_provider = object.__new__(entrypoint.OpenAITeacherProvider)
            wire_provider._client = SimpleNamespace(
                chat=SimpleNamespace(completions=api)
            )
            wire_provider.model = "MiniMax-M2.7"
            wire_provider.temperature = temperature
            wire_provider.max_tokens = max_tokens
            wire_provider.top_p = top_p
            wire_provider.top_k = top_k
            wire_provider.workers = 1
            wire_provider.hard_request_deadline_seconds = deadline
            wire_provider.network_access = False
            wire_provider.request_count = 0
            wire_provider.response_count = 0
            wire_provider.batch_sizes = []
            completion = (
                await wire_provider.complete_many(
                    [("system", "user")], strict_sequential=True
                )
            )[0]
            return api.calls[0], completion

        v8_wire, v8_completion = asyncio.run(
            exercise(0.95, 40, 190000, 1.0, 7200.0)
        )
        self.assertEqual(
            v8_wire,
            {
                "model": "MiniMax-M2.7",
                "max_tokens": 190000,
                "temperature": 1.0,
                "messages": [
                    {"role": "system", "content": "system"},
                    {"role": "user", "content": "user"},
                ],
                "top_p": 0.95,
                "extra_body": {"top_k": 40},
            },
        )
        self.assertEqual(v8_completion.finish_reason, "stop")
        old_wire, _ = asyncio.run(exercise(None, None, 65536, 0.0, 1800.0))
        self.assertNotIn("top_p", old_wire)
        self.assertNotIn("top_k", old_wire)
        self.assertNotIn("extra_body", old_wire)

    def test_v8_invalid_sampling_and_wrong_a7_a9_ids_fail_before_provider(self):
        provider_invalid = (
            ({"top_p": float("nan"), "top_k": 40}, "top-p-nan"),
            ({"top_p": -0.01, "top_k": 40}, "top-p-low"),
            ({"top_p": 1.01, "top_k": 40}, "top-p-high"),
            ({"top_p": True, "top_k": 40}, "top-p-bool"),
            ({"top_p": 0.95, "top_k": 0}, "top-k-zero"),
            ({"top_p": 0.95, "top_k": -1}, "top-k-negative"),
            ({"top_p": 0.95, "top_k": True}, "top-k-bool"),
            ({"top_p": 0.95, "top_k": 1.5}, "top-k-float"),
        )
        for sampling, name in provider_invalid:
            with self.subTest(provider_sampling=name), mock.patch.dict(
                os.environ, self.ENDPOINT_ENV, clear=False
            ):
                with self.assertRaises(entrypoint.V2ProviderGuardError):
                    entrypoint.OpenAITeacherProvider(
                        model="MiniMax-M2.7",
                        temperature=1.0,
                        max_tokens=190000,
                        workers=1,
                        request_timeout_seconds=7200.0,
                        hard_request_deadline_seconds=7200.0,
                        **sampling,
                    )

        cases = (
            ("temperature-zero", {"temperature": 0}, None),
            ("temperature-low", {"temperature": 0.999}, None),
            ("temperature-high", {"temperature": 1.001}, None),
            ("max-65536", {"max_tokens": 65536}, None),
            ("max-189999", {"max_tokens": 189999}, None),
            ("max-190001", {"max_tokens": 190001}, None),
            ("a7", {}, "v2-teacher-pilot-live-20260820-a7"),
            ("a9", {}, "v2-teacher-pilot-live-20260820-a9"),
        )
        for name, overrides, run_id in cases:
            with self.subTest(config=name):
                out = self.root / name
                argv = _pilot_args(
                    out, live=True, profile=self.PROFILE, **overrides
                )
                if run_id is not None:
                    argv[argv.index("--run-id") + 1] = run_id
                with mock.patch.dict(
                    os.environ, self.ENDPOINT_ENV, clear=False
                ), mock.patch.object(
                    entrypoint,
                    "_build_openai_provider",
                    side_effect=AssertionError("invalid v8 built provider"),
                ) as provider, redirect_stdout(io.StringIO()), redirect_stderr(
                    io.StringIO()
                ):
                    with self.assertRaises(SystemExit) as raised:
                        entrypoint.main(argv)
                self.assertEqual(raised.exception.code, 2)
                provider.assert_not_called()
                manifest = _read_json(_single_run(out) / "run_manifest.json")
                self.assertEqual(manifest["failure_stage"], "config_validation")
                self.assertEqual(
                    manifest["completion"]["provider_calls"]["attempted"], 0
                )

    def test_v8_all_162_stop_higgsai_rows_release_only_after_complete_gate(self):
        class AllStopProvider:
            model = "MiniMax-M2.7"

            def __init__(self):
                self.request_count = 0
                self.response_count = 0
                self.network_access = False
                self.batch_sizes = []
                self.strict_sequential = None

            async def complete_many(
                self,
                prompts,
                *,
                before_attempt=None,
                on_completion=None,
                strict_sequential=False,
            ):
                self.batch_sizes.append(len(prompts))
                self.strict_sequential = strict_sequential
                values = []
                for index, _ in enumerate(prompts):
                    if distillation.called or market.called:
                        raise AssertionError("downstream released before full Teacher gate")
                    if before_attempt is not None:
                        before_attempt(index)
                    self.request_count += 1
                    self.response_count += 1
                    self.network_access = True
                    completion = entrypoint.TeacherCompletion(
                        raw_response=(
                            '{"action":"hold","intensity":0,'
                            '"reasoning":"private synthetic v8 stop fixture"}'
                        ),
                        reported_model="HiggsAI",
                        reported_model_raw="HiggsAI",
                        input_tokens=7,
                        output_tokens=5,
                        response_id="v8-all-stop-{}".format(index),
                        finish_reason="stop",
                        finish_reason_raw="stop",
                    )
                    values.append(completion)
                    if on_completion is not None:
                        on_completion(index, completion)
                return values

            async def aclose(self):
                return None

        class DownstreamReached(RuntimeError):
            pass

        provider = AllStopProvider()
        downstream_request_counts = []

        def stop_at_distillation(*args, **kwargs):
            downstream_request_counts.append(provider.request_count)
            raise DownstreamReached("full gate released")

        distillation = mock.Mock(side_effect=stop_at_distillation)
        market = mock.Mock(
            side_effect=AssertionError("market ran past distillation sentinel")
        )
        out = self.root / "all-162-stop"
        with mock.patch.dict(
            os.environ, self.ENDPOINT_ENV, clear=False
        ), mock.patch.object(
            entrypoint, "_build_openai_provider", return_value=provider
        ), mock.patch.object(
            entrypoint, "run_distillation_phase", distillation
        ), mock.patch.object(
            entrypoint, "run_market_phase", market
        ), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                entrypoint.main(_pilot_args(out, live=True, profile=self.PROFILE))
        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(provider.batch_sizes, [162])
        self.assertTrue(provider.strict_sequential)
        self.assertEqual(provider.request_count, 162)
        self.assertEqual(provider.response_count, 162)
        self.assertEqual(downstream_request_counts, [162])
        distillation.assert_called_once()
        market.assert_not_called()

        run_dir = _single_run(out)
        manifest = _read_json(run_dir / "run_manifest.json")
        rows = _read_jsonl(run_dir / "teacher_samples.jsonl")
        private_rows = _read_jsonl(run_dir / "private_teacher_records.jsonl")
        teacher = manifest["v2_attention_market"]["teacher_samples"]
        gate = manifest["v2_attention_market"]["teacher_acceptance_gate"]
        self.assertEqual(len(rows), 162)
        self.assertEqual(
            {row["schema_version"] for row in rows},
            {"v2_teacher_request/0.3"},
        )
        self.assertTrue(all(row["generation_sampling"] == self.SAMPLING for row in rows))
        self.assertTrue(
            all(row["generation_sampling"] == self.SAMPLING for row in private_rows)
        )
        self.assertEqual({row["finish_reason"] for row in rows}, {"stop"})
        self.assertEqual({row["reported_model"] for row in rows}, {"HiggsAI"})
        self.assertEqual({row["status"] for row in rows}, {"valid"})
        self.assertEqual(teacher["attempted"], 162)
        self.assertEqual(teacher["resolved"], 162)
        self.assertEqual(teacher["valid"], 162)
        self.assertEqual(teacher["failed"], 0)
        self.assertEqual(teacher["skipped"], 0)
        self.assertEqual(manifest["honest_n_teacher_samples"], 162)
        self.assertEqual(gate["status"], "passed")
        self.assertEqual(gate["canary_status"], "passed")
        self.assertEqual(gate["required_finish_reason"], "stop")
        self.assertTrue(gate["student_and_market_released"])

    def test_v8_length_null_at_190000_fails_closed_and_private_is_0600(self):
        private_marker = "PRIVATE_V8_SDK_ENVELOPE_MARKER"

        class LengthProvider:
            model = "MiniMax-M2.7"

            def __init__(self):
                self.request_count = 0
                self.response_count = 0
                self.network_access = False
                self.batch_sizes = []

            async def complete_many(
                self,
                prompts,
                *,
                before_attempt=None,
                on_completion=None,
                strict_sequential=False,
            ):
                self.batch_sizes.append(len(prompts))
                self.strict_sequential = strict_sequential
                if before_attempt is not None:
                    before_attempt(0)
                self.request_count = 1
                self.response_count = 1
                self.network_access = True
                completion = entrypoint.TeacherCompletion(
                    raw_response=None,
                    reported_model="HiggsAI",
                    reported_model_raw="HiggsAI",
                    input_tokens=745,
                    output_tokens=190000,
                    response_id="v8-length-null-canary",
                    error_type="ProviderResponseShapeError",
                    error_detail="provider message content must be a string",
                    finish_reason="length",
                    finish_reason_raw="length",
                    provider_sdk_response_json=private_marker,
                )
                if on_completion is not None:
                    on_completion(0, completion)
                return [completion]

            async def aclose(self):
                return None

        provider = LengthProvider()
        out = self.root / "length-null-canary"
        with mock.patch.dict(
            os.environ, self.ENDPOINT_ENV, clear=False
        ), mock.patch.object(
            entrypoint, "_build_openai_provider", return_value=provider
        ), mock.patch.object(
            entrypoint, "run_distillation_phase"
        ) as distillation, mock.patch.object(
            entrypoint, "run_market_phase"
        ) as market, redirect_stdout(io.StringIO()), redirect_stderr(
            io.StringIO()
        ):
            with self.assertRaises(SystemExit) as raised:
                entrypoint.main(_pilot_args(out, live=True, profile=self.PROFILE))
        self.assertEqual(raised.exception.code, 1)
        self.assertTrue(provider.strict_sequential)
        self.assertEqual(provider.request_count, 1)
        distillation.assert_not_called()
        market.assert_not_called()

        run_dir = _single_run(out)
        manifest_path = run_dir / "run_manifest.json"
        public_path = run_dir / "teacher_samples.jsonl"
        private_path = run_dir / "private_teacher_records.jsonl"
        manifest = _read_json(manifest_path)
        rows = _read_jsonl(public_path)
        private_rows = _read_jsonl(private_path)
        self.assertEqual(rows[0]["schema_version"], "v2_teacher_request/0.3")
        self.assertEqual(rows[0]["generation_sampling"], self.SAMPLING)
        self.assertEqual(private_rows[0]["generation_sampling"], self.SAMPLING)
        self.assertEqual(rows[0]["failure_code"], "provider_response_shape_invalid")
        self.assertEqual(rows[0]["finish_reason"], "length")
        self.assertIsNone(rows[0]["response_hash"])
        self.assertNotIn(private_marker, public_path.read_text(encoding="utf-8"))
        self.assertNotIn(private_marker, manifest_path.read_text(encoding="utf-8"))
        self.assertIn(private_marker, private_path.read_text(encoding="utf-8"))
        self.assertEqual(stat.S_IMODE(private_path.stat().st_mode), 0o600)
        teacher = manifest["v2_attention_market"]["teacher_samples"]
        gate = manifest["v2_attention_market"]["teacher_acceptance_gate"]
        self.assertEqual(teacher["attempted"], 1)
        self.assertEqual(teacher["valid"], 0)
        self.assertEqual(teacher["skipped"], 161)
        self.assertEqual(manifest["honest_n_teacher_samples"], 0)
        self.assertEqual(manifest["completion"]["parsing"]["attempted"], 0)
        self.assertEqual(
            manifest["completion"]["application_provider_attempts"][
                "finish_reason_counts"
            ],
            {"length": 1},
        )
        self.assertEqual(gate["status"], "failed")
        self.assertEqual(gate["reason_codes"], ["provider_response_shape_invalid"])
        self.assertFalse(gate["student_and_market_released"])
        self.assertFalse((run_dir / "aggregated_dataset.json").exists())
        self.assertFalse((run_dir / "market_2x2_summary.json").exists())

    def test_v8_a7_and_a8_directories_are_o_excl_immutable(self):
        for run_id in (
            "v2-teacher-pilot-live-20260820-a7",
            "v2-teacher-pilot-live-20260820-a8",
        ):
            with self.subTest(run_id=run_id):
                out = self.root / run_id
                run_dir = out / "runs" / run_id
                run_dir.mkdir(parents=True)
                sentinel = run_dir / "immutable-sentinel.bin"
                sentinel.write_bytes(b"immutable-v8-boundary\x00")
                before_hash = _sha256(sentinel)
                before_entries = sorted(path.name for path in run_dir.iterdir())
                argv = _pilot_args(out, live=True, profile=self.PROFILE)
                argv[argv.index("--run-id") + 1] = run_id
                with mock.patch.dict(
                    os.environ, self.ENDPOINT_ENV, clear=False
                ), mock.patch.object(
                    entrypoint,
                    "_build_openai_provider",
                    side_effect=AssertionError("O_EXCL path built provider"),
                ) as provider, redirect_stdout(io.StringIO()), redirect_stderr(
                    io.StringIO()
                ):
                    with self.assertRaises((SystemExit, FileExistsError)) as raised:
                        entrypoint.main(argv)
                if isinstance(raised.exception, SystemExit):
                    self.assertEqual(raised.exception.code, 2)
                provider.assert_not_called()
                self.assertEqual(_sha256(sentinel), before_hash)
                self.assertEqual(
                    sorted(path.name for path in run_dir.iterdir()), before_entries
                )


class V2OpenAIAdapterTests(unittest.TestCase):
    def test_teacher_completion_preserves_pre_v3_positional_error_fields(self):
        completion = entrypoint.TeacherCompletion(
            None,
            None,
            None,
            None,
            None,
            None,
            "LegacyTransportError",
            "legacy positional detail",
        )
        self.assertEqual(completion.error_type, "LegacyTransportError")
        self.assertEqual(completion.error_detail, "legacy positional detail")
        self.assertIsNone(completion.finish_reason)
        self.assertIsNone(completion.finish_reason_raw)
        self.assertIsNone(completion.provider_sdk_response_json)

    def test_transport_sends_requested_minimax_and_records_higgsai_alias(self):
        class Completions:
            def __init__(self):
                self.kwargs = []

            async def create(self, **kwargs):
                self.kwargs.append(kwargs)
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content='{"ok":true}')
                        )
                    ],
                    model="HiggsAI",
                    id="transport-separation-response",
                    usage=SimpleNamespace(
                        prompt_tokens=7, completion_tokens=3
                    ),
                )

        completions_api = Completions()
        provider = object.__new__(entrypoint.OpenAITeacherProvider)
        provider._client = SimpleNamespace(
            chat=SimpleNamespace(completions=completions_api)
        )
        provider.model = "MiniMax-M2.7"
        provider.temperature = 0.0
        provider.max_tokens = 1024
        provider.workers = 1
        provider.network_access = False
        provider.request_count = 0
        provider.response_count = 0
        provider.batch_sizes = []
        completions = asyncio.run(
            provider.complete_many(
                [("system", "user")], strict_sequential=True
            )
        )
        self.assertEqual(completions_api.kwargs[0]["model"], "MiniMax-M2.7")
        self.assertEqual(provider.model, "MiniMax-M2.7")
        self.assertEqual(completions[0].reported_model, "HiggsAI")
        self.assertEqual(completions[0].reported_model_raw, "HiggsAI")

    def test_transport_records_stop_finish_reason_and_private_sdk_envelope(self):
        class Response(SimpleNamespace):
            def model_dump_json(self):
                return '{"private_sdk_field":"audit-only"}'

        class Completions:
            async def create(self, **kwargs):
                self.kwargs = kwargs
                return Response(
                    choices=[
                        SimpleNamespace(
                            finish_reason="stop",
                            message=SimpleNamespace(content='{"ok":true}'),
                        )
                    ],
                    model="HiggsAI",
                    id="finish-audit-response",
                    usage=SimpleNamespace(
                        prompt_tokens=7, completion_tokens=3
                    ),
                )

        completions_api = Completions()
        provider = object.__new__(entrypoint.OpenAITeacherProvider)
        provider._client = SimpleNamespace(
            chat=SimpleNamespace(completions=completions_api)
        )
        provider.model = "MiniMax-M2.7"
        provider.temperature = 0.0
        provider.max_tokens = 4096
        provider.workers = 1
        provider.network_access = False
        provider.request_count = 0
        provider.response_count = 0
        provider.batch_sizes = []
        completion = asyncio.run(
            provider.complete_many(
                [("system", "user")], strict_sequential=True
            )
        )[0]
        self.assertEqual(completions_api.kwargs["max_tokens"], 4096)
        self.assertEqual(completion.finish_reason, "stop")
        self.assertEqual(completion.finish_reason_raw, "stop")
        self.assertEqual(
            completion.provider_sdk_response_json,
            '{"private_sdk_field":"audit-only"}',
        )

    def test_length_with_null_content_is_preserved_as_shape_failure(self):
        class Response(SimpleNamespace):
            def model_dump_json(self):
                return '{"choices":[{"finish_reason":"length"}]}'

        class Completions:
            async def create(self, **kwargs):
                return Response(
                    choices=[
                        SimpleNamespace(
                            finish_reason="length",
                            message=SimpleNamespace(
                                content=None,
                                reasoning_content="must never become decision",
                            ),
                        )
                    ],
                    model="HiggsAI",
                    id="length-response",
                    usage=SimpleNamespace(
                        prompt_tokens=710, completion_tokens=1024
                    ),
                )

        provider = object.__new__(entrypoint.OpenAITeacherProvider)
        provider._client = SimpleNamespace(
            chat=SimpleNamespace(completions=Completions())
        )
        provider.model = "MiniMax-M2.7"
        provider.temperature = 0.0
        provider.max_tokens = 4096
        provider.workers = 1
        provider.network_access = False
        provider.request_count = 0
        provider.response_count = 0
        provider.batch_sizes = []
        completion = asyncio.run(
            provider.complete_many(
                [("system", "user")], strict_sequential=True
            )
        )[0]
        self.assertIsNone(completion.raw_response)
        self.assertEqual(completion.error_type, "ProviderResponseShapeError")
        self.assertEqual(completion.finish_reason, "length")
        self.assertNotIn(
            "must never become decision", completion.error_detail or ""
        )

    def test_public_model_alias_rejects_short_key_and_fragment_secrets(self):
        with mock.patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "abc", "OPENAI_BASE_URL": "https://x/v1"},
            clear=False,
        ):
            self.assertIsNone(entrypoint._safe_public_model_alias("abc"))
        with mock.patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "EMPTY",
                "OPENAI_BASE_URL": "https://x/v1#fragment-secret",
            },
            clear=False,
        ):
            self.assertIsNone(
                entrypoint._safe_public_model_alias("fragment-secret")
            )

    def test_malformed_response_shape_is_one_resolved_failure_not_batch_abort(self):
        class Completions:
            async def create(self, **kwargs):
                return SimpleNamespace(
                    choices=[],
                    model="safe-model",
                    id="sensitive-provider-id",
                    usage=SimpleNamespace(prompt_tokens=7, completion_tokens=3),
                )

        provider = object.__new__(entrypoint.OpenAITeacherProvider)
        provider._client = SimpleNamespace(
            chat=SimpleNamespace(completions=Completions())
        )
        provider.model = "requested-model"
        provider.temperature = 0.3
        provider.max_tokens = 256
        provider.workers = 1
        provider.network_access = False
        provider.request_count = 0
        provider.response_count = 0
        provider.batch_sizes = []
        attempted = []
        resolved = []
        completions = asyncio.run(
            provider.complete_many(
                [("system", "user")],
                before_attempt=attempted.append,
                on_completion=lambda index, value: resolved.append((index, value)),
            )
        )
        self.assertEqual(attempted, [0])
        self.assertEqual(len(resolved), 1)
        self.assertEqual(len(completions), 1)
        self.assertIsNone(completions[0].raw_response)
        self.assertEqual(
            completions[0].error_type, "ProviderResponseShapeError"
        )
        self.assertEqual(completions[0].response_id, "sensitive-provider-id")
        self.assertEqual(completions[0].input_tokens, 7)
        self.assertEqual(provider.request_count, 1)
        self.assertEqual(provider.response_count, 1)
        self.assertTrue(provider.network_access)

    def test_provider_id_and_printable_secret_alias_are_private_only(self):
        response_id = "PROVIDER_RESPONSE_ID_SECRET"
        raw_alias = "MODEL_ALIAS_PRINTABLE_SECRET"

        class PatchedProvider:
            model = "requested-model"

            def __init__(self):
                self.request_count = 0
                self.response_count = 0
                self.network_access = False
                self.batch_sizes = []

            async def complete_many(
                self, prompts, *, before_attempt=None, on_completion=None
            ):
                self.batch_sizes.append(len(prompts))
                values = []
                for index, _ in enumerate(prompts):
                    if before_attempt is not None:
                        before_attempt(index)
                    self.request_count += 1
                    self.network_access = True
                    self.response_count += 1
                    completion = entrypoint.TeacherCompletion(
                        raw_response=(
                            '{"action":"hold","intensity":0,'
                            '"reasoning":"private patched endpoint"}'
                        ),
                        reported_model=None,
                        input_tokens=1,
                        output_tokens=1,
                        response_id=response_id,
                        reported_model_raw=raw_alias,
                    )
                    values.append(completion)
                    if on_completion is not None:
                        on_completion(index, completion)
                return values

            async def aclose(self):
                return None

        temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(temporary.cleanup)
        out = Path(temporary.name) / "private-provider-metadata"
        with mock.patch.object(
            entrypoint,
            "_build_openai_provider",
            return_value=PatchedProvider(),
        ), mock.patch.dict(
            os.environ,
            {"OPENAI_API_KEY": raw_alias},
            clear=False,
        ), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            entrypoint.main(
                [
                    "--provider",
                    "openai",
                    "--model",
                    "requested-model",
                    "--live",
                    "--confirm-request-count",
                    "24",
                    "--states",
                    "24",
                    "--replicates",
                    "1",
                    "--training-epochs",
                    "2",
                    "--market-agents",
                    "8",
                    "--market-rounds",
                    "2",
                    "--market-seeds",
                    "1",
                    "--out",
                    str(out),
                ]
            )
        run_dir = _single_run(out)
        private_text = (run_dir / "private_teacher_records.jsonl").read_text(
            encoding="utf-8"
        )
        public_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in run_dir.rglob("*")
            if path.is_file()
            and path.name
            not in {"private_teacher_records.jsonl", "private_events.jsonl"}
            and path.suffix in {".json", ".jsonl", ".md", ".html"}
        )
        self.assertIn(response_id, private_text)
        self.assertNotIn(raw_alias, private_text)
        self.assertIn("<redacted>", private_text)
        self.assertIn(hashlib.sha256(raw_alias.encode("utf-8")).hexdigest(), private_text)
        self.assertNotIn(response_id, public_text)
        self.assertNotIn(raw_alias, public_text)
        manifest = _read_json(run_dir / "run_manifest.json")
        self.assertEqual(
            manifest["completion"]["application_provider_attempts"][
                "invalid_reported_model_alias_count"
            ],
            24,
        )


class V2IdentityTests(unittest.TestCase):
    def _identities(self, **overrides):
        from nmsim import v2_attention

        values = {
            "states": 24,
            "replicates": 2,
            "workers": 2,
            "training_epochs": 10,
            "out": "/tmp/v2-identity-a",
        }
        values.update(overrides)
        argv = [
            "--provider",
            "fake_test_teacher",
            "--states",
            str(values["states"]),
            "--replicates",
            str(values["replicates"]),
            "--workers",
            str(values["workers"]),
            "--training-epochs",
            str(values["training_epochs"]),
            "--out",
            str(values["out"]),
        ]
        args = entrypoint.build_argparser().parse_args(argv)
        entrypoint._validate_args(args)
        observations = v2_attention.generate_state_design(
            args.states, args.seed, study_id="v2-attention-market"
        )
        return entrypoint.build_v2_identities(
            args,
            observations,
            repo_root=Path(entrypoint.__file__).resolve().parents[1],
        )

    def test_default_split_covers_all_nine_designed_regimes(self):
        from nmsim import v2_attention

        observations = v2_attention.generate_state_design(
            96, 20260811, study_id="v2-attention-market"
        )
        split = entrypoint.preflight_group_split(observations, 20260811)
        coverage = entrypoint._split_regime_coverage(
            observations, split.family_assignments, split.family_strata
        )
        self.assertEqual(
            coverage["split_stratification_unit"],
            "return_20d_tape_regime_x_position_fraction_regime",
        )
        self.assertTrue(coverage["all_partitions_cover_all_tape_regimes"])
        self.assertTrue(coverage["all_partitions_cover_all_design_strata"])
        self.assertEqual(coverage["missing_partition_design_strata"], [])

    def test_scientific_config_names_split_and_ood_diagnostic_semantics(self):
        identities = self._identities()
        scientific = identities["scientific_config"]
        split_contract = scientific["group_split"][
            "classification_and_allocation_contract"
        ]
        self.assertEqual(
            split_contract["tape_regimes"]["decline"],
            {"field": "return_20d", "operator": "<", "threshold": -0.10},
        )
        self.assertEqual(
            split_contract["position_regimes"]["invested"],
            {
                "field": "position_fraction",
                "operator": ">",
                "threshold": 0.80,
            },
        )
        self.assertEqual(
            split_contract["joint_stratification_eligibility"],
            {"required_exact_cell_count": 9, "minimum_families_per_cell": 5},
        )
        self.assertEqual(
            split_contract["allocation"]["fractions"], [0.70, 0.15, 0.15]
        )
        self.assertEqual(scientific["ood_diagnostics"]["z_threshold"], 3.0)
        self.assertFalse(
            scientific["ood_diagnostics"]["joint_support_assessed"]
        )

    def test_named_hashes_recompute_and_change_only_in_their_declared_scope(self):
        base = self._identities()
        self.assertEqual(
            base["scientific_config"]["group_split"]["stratification"],
            "return_20d_tape_regime_small_design_fallback",
        )
        for config_key, hash_key in (
            ("scientific_config", "v2_scientific_config_hash"),
            ("model_request_config", "v2_model_request_config_hash"),
            ("execution_config", "v2_execution_config_hash"),
            ("full_effective_config", "v2_full_effective_config_hash"),
        ):
            self.assertEqual(
                entrypoint.stable_hash(base[config_key]), base[hash_key]
            )

        replicate = self._identities(replicates=3)
        self.assertNotEqual(
            base["v2_scientific_config_hash"],
            replicate["v2_scientific_config_hash"],
        )
        self.assertNotEqual(
            base["v2_model_request_config_hash"],
            replicate["v2_model_request_config_hash"],
        )
        self.assertEqual(
            base["v2_execution_config_hash"],
            replicate["v2_execution_config_hash"],
        )

        workers = self._identities(workers=4)
        self.assertEqual(
            base["v2_scientific_config_hash"], workers["v2_scientific_config_hash"]
        )
        self.assertEqual(
            base["v2_model_request_config_hash"],
            workers["v2_model_request_config_hash"],
        )
        self.assertNotEqual(
            base["v2_execution_config_hash"], workers["v2_execution_config_hash"]
        )

        output = self._identities(out="/tmp/v2-identity-b")
        self.assertEqual(
            base["v2_scientific_config_hash"], output["v2_scientific_config_hash"]
        )
        self.assertEqual(
            base["v2_model_request_config_hash"],
            output["v2_model_request_config_hash"],
        )
        self.assertNotEqual(
            base["v2_execution_config_hash"], output["v2_execution_config_hash"]
        )


class V2SmallFullFakeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temporary = tempfile.TemporaryDirectory(dir="/tmp")
        cls.addClassCleanup(cls._temporary.cleanup)
        cls.out = Path(cls._temporary.name) / "full-fake"
        with mock.patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError("fake run opened a socket"),
        ) as network, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            entrypoint.main(_small_full_args(cls.out))
        network.assert_not_called()
        cls.run_dir = _single_run(cls.out)
        cls.manifest = _read_json(cls.run_dir / "run_manifest.json")
        cls.summary = _read_json(
            cls.run_dir / "v2_attention_market_summary.json"
        )

    def test_small_full_fake_has_honest_n_privacy_conservation_and_hashes(self):
        manifest = self.manifest
        summary = self.summary
        self.assertEqual(manifest["status"], "finished")
        self.assertEqual(summary["teacher"]["planned"], 48)
        self.assertEqual(summary["teacher"]["attempted"], 48)
        self.assertEqual(summary["teacher"]["valid"], 48)
        self.assertEqual(summary["teacher"]["failed"], 0)
        self.assertEqual(summary["teacher"]["honest_n_teacher_samples"], 48)
        self.assertEqual(summary["dataset"]["aggregated_examples"], 24)
        self.assertIn("mean_action_gini", summary["dataset"]["teacher_disagreement"])
        self.assertIsInstance(
            summary["student"]["held_out_baseline_comparison"][
                "deployed_model_failed_to_beat_a_baseline"
            ],
            bool,
        )
        self.assertEqual(summary["market"]["honest_n_market_runs"], 4)
        self.assertEqual(manifest["honest_n_teacher_samples"], 48)
        self.assertEqual(manifest["honest_n_aggregated_examples"], 24)
        self.assertEqual(manifest["honest_n_market_runs"], 4)
        self.assertEqual(
            manifest["completion"]["rounds"],
            {
                "planned": 12,
                "started": 12,
                "completed": 12,
                "failed": 0,
                "skipped": 0,
                "unit": "rounds",
            },
        )
        self.assertTrue(summary["market"]["all_round_conservation_checks_passed"])
        self.assertFalse(summary["network_access"])

        private_teacher = self.run_dir / "private_teacher_records.jsonl"
        private_events = self.run_dir / "private_events.jsonl"
        self.assertEqual(stat.S_IMODE(private_teacher.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(private_events.stat().st_mode), 0o600)
        self.assertIn(
            FAKE_PRIVATE_REASONING,
            private_teacher.read_text(encoding="utf-8"),
        )

        private_names = {
            "private_teacher_records.jsonl",
            "private_events.jsonl",
        }
        public_files = [
            path
            for path in self.run_dir.rglob("*")
            if path.is_file() and path.name not in private_names
        ]
        for path in public_files:
            if path.suffix in {".json", ".jsonl", ".md", ".html"}:
                with self.subTest(public_path=path.name):
                    text = path.read_text(encoding="utf-8")
                    self.assertNotIn(FAKE_PRIVATE_REASONING, text)
                    self.assertNotIn('"raw_response":', text)
                    self.assertNotIn('"private_rationale":', text)

        market_index = _read_json(self.run_dir / "market_2x2_summary.json")
        envelope = _read_json(self.run_dir / "student_model_envelope.json")
        envelope_content = dict(envelope)
        envelope_hash = envelope_content.pop("model_envelope_hash")
        self.assertEqual(envelope_hash, entrypoint.stable_hash(envelope_content))
        self.assertEqual(
            market_index["model_lineage"]["student_model_envelope_hash"],
            envelope_hash,
        )
        self.assertEqual(
            market_index["model_lineage"][
                "student_model_envelope_artifact_sha256"
            ],
            _sha256(self.run_dir / "student_model_envelope.json"),
        )
        for model_record in envelope["models"].values():
            model_path = self.run_dir / model_record["path"]
            self.assertEqual(model_record["artifact_sha256"], _sha256(model_path))
            self.assertEqual(
                model_record["model_semantic_hash"],
                entrypoint.stable_hash(_read_json(model_path)),
            )
        market_ood = market_index["market_vs_train_ood"]
        self.assertIsNotNone(market_ood)
        self.assertFalse(market_ood["joint_support_assessed"])
        self.assertEqual(
            market_ood["reference_hash"],
            market_index["model_lineage"]["ood_reference_hash"],
        )
        self.assertEqual(
            market_ood["all_cells"]["n"], 4 * 8 * 3
        )
        self.assertEqual(
            set(market_ood["by_cell"]),
            {
                "finite_distilled",
                "finite_momentum",
                "credit_distilled",
                "credit_momentum",
            },
        )
        self.assertEqual(summary["student"]["model_artifacts"], envelope["models"])
        self.assertEqual(
            summary["student"]["model_envelope_artifact_sha256"],
            _sha256(self.run_dir / "student_model_envelope.json"),
        )
        self.assertRegex(
            summary["v2_execution_component_fingerprint"]["sha256"], HASH_RE
        )
        self.assertEqual(market_index["planned_runs"], 4)
        self.assertEqual(market_index["honest_n_market_runs"], 4)
        self.assertEqual(len(market_index["run_catalog"]), 4)
        self.assertEqual(
            {row["cell"] for row in market_index["run_catalog"]},
            {
                "finite_distilled",
                "finite_momentum",
                "credit_distilled",
                "credit_momentum",
            },
        )
        for catalog_row in market_index["run_catalog"]:
            run = _read_json(self.run_dir / catalog_row["path"])
            self.assertEqual(run["model_lineage"], market_index["model_lineage"])
            self.assertEqual(len(run["rounds"]), 3)
            for round_row in run["rounds"]:
                with self.subTest(
                    cell=catalog_row["cell"], round=round_row.get("round_index")
                ):
                    self.assertTrue(round_row["conservation"])
                    self.assertTrue(all(round_row["conservation"].values()))

        registered = {
            row["path"]: row for row in manifest["results"] if row["inside_run_directory"]
        }
        actual = {
            path.relative_to(self.run_dir).as_posix()
            for path in self.run_dir.rglob("*")
            if path.is_file() and path.name != "run_manifest.json"
        }
        self.assertEqual(set(registered), actual)
        for relative, descriptor in registered.items():
            with self.subTest(artifact=relative):
                self.assertTrue(descriptor["exists"])
                self.assertEqual(descriptor["kind"], "file")
                self.assertIsNone(descriptor["error"])
                self.assertRegex(descriptor["sha256"], HASH_RE)
                self.assertEqual(descriptor["sha256"], _sha256(self.run_dir / relative))

    def test_html_exposes_engineering_boundary_and_embeds_public_json_only(self):
        report = (self.run_dir / "v2_attention_market_report.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("Engineering evidence only", report)
        self.assertIn("not an endpoint or a person", report)
        self.assertIn("Human comparison", report)
        self.assertIn("Teacher disagreement", report)
        self.assertIn("Fixed-MLP baseline diagnostic", report)
        self.assertIn("Market-vs-train OOD", report)
        self.assertNotIn(FAKE_PRIVATE_REASONING, report)
        self.assertNotIn('"raw_response":', report)
        self.assertNotIn('"private_rationale":', report)
        match = re.search(
            r'<script id="summary-data" type="application/json">(.*?)</script>',
            report,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(match)
        embedded = json.loads(match.group(1))
        self.assertEqual(embedded, self.summary)
        self.assertEqual(embedded["scientific_claim_status"], "engineering_fake_only")
        self.assertFalse(
            embedded["privacy_boundary"]["private_rationale_in_public_samples"]
        )


class V2InvalidTeacherResponseTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self._temporary.cleanup)
        self.out = Path(self._temporary.name) / "one-invalid"

    def test_invalid_raw_response_reduces_honest_n_without_becoming_hold(self):
        from nmsim import v2_attention

        original_fake = v2_attention.fake_test_teacher
        calls = 0

        def complete_with_one_invalid(observation, replicate_index):
            nonlocal calls
            calls += 1
            if calls == 1:
                return (
                    '{"action":"hold","intensity":0.5,'
                    '"reasoning":"' + INVALID_PRIVATE_MARKER + '"}'
                )
            return original_fake(observation, replicate_index)

        with mock.patch.object(
            v2_attention,
            "fake_test_teacher",
            new=complete_with_one_invalid,
        ), mock.patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError("fake invalid run opened a socket"),
        ) as network, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            entrypoint.main(_small_full_args(self.out))
        network.assert_not_called()
        run_dir = _single_run(self.out)
        manifest = _read_json(run_dir / "run_manifest.json")
        summary = _read_json(run_dir / "v2_attention_market_summary.json")
        samples = _read_jsonl(run_dir / "teacher_samples.jsonl")

        self.assertEqual(manifest["status"], "finished")
        self.assertEqual(summary["teacher"]["attempted"], 48)
        self.assertEqual(summary["teacher"]["valid"], 47)
        self.assertEqual(summary["teacher"]["failed"], 1)
        self.assertEqual(summary["teacher"]["honest_n_teacher_samples"], 47)
        self.assertEqual(manifest["honest_n_teacher_samples"], 47)
        self.assertEqual(manifest["honest_n_aggregated_examples"], 24)
        self.assertEqual(manifest["honest_n_market_runs"], 4)
        self.assertEqual(manifest["completion"]["parsing"]["failed"], 1)
        self.assertEqual(manifest["completion"]["parsing"]["fallbacks"], 0)

        failed_rows = [row for row in samples if row["status"] == "failed"]
        self.assertEqual(len(failed_rows), 1)
        failed = failed_rows[0]
        self.assertEqual(failed["failure_code"], "teacher_response_invalid")
        self.assertIsNone(failed["decision"])

        dataset = _read_json(run_dir / "aggregated_dataset.json")
        aggregate = next(
            row["soft_target"]
            for row in dataset["rows"]
            if row["observation"]["state_id"] == failed["state_id"]
        )
        self.assertEqual(aggregate["attempted_n"], 2)
        self.assertEqual(aggregate["valid_n"], 1)
        self.assertEqual(aggregate["failed_n"], 1)
        self.assertEqual(
            aggregate["failure_counts"], {"teacher_response_invalid": 1}
        )
        self.assertEqual(
            sum(
                aggregate["conditional_intensity"][action]["n"]
                for action in ("buy", "hold", "sell")
            ),
            1,
        )

        public_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in run_dir.rglob("*")
            if path.is_file()
            and path.name not in {
                "private_teacher_records.jsonl",
                "private_events.jsonl",
            }
            and path.suffix in {".json", ".jsonl", ".md", ".html"}
        )
        self.assertNotIn(INVALID_PRIVATE_MARKER, public_text)
        self.assertIn(
            INVALID_PRIVATE_MARKER,
            (run_dir / "private_teacher_records.jsonl").read_text(encoding="utf-8"),
        )

    def test_huge_json_integer_is_one_invalid_sample_and_batch_continues(self):
        from nmsim import v2_attention

        original_fake = v2_attention.fake_test_teacher
        calls = 0

        def complete_with_huge_integer(observation, replicate_index):
            nonlocal calls
            calls += 1
            if calls == 1:
                return (
                    '{"action":"buy","intensity":'
                    + "9" * 400
                    + ',"reasoning":"overflow must be isolated"}'
                )
            return original_fake(observation, replicate_index)

        with mock.patch.object(
            v2_attention,
            "fake_test_teacher",
            new=complete_with_huge_integer,
        ), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            entrypoint.main(_small_full_args(self.out))
        run_dir = _single_run(self.out)
        summary = _read_json(run_dir / "v2_attention_market_summary.json")
        samples = _read_jsonl(run_dir / "teacher_samples.jsonl")
        self.assertEqual(summary["teacher"]["attempted"], 48)
        self.assertEqual(summary["teacher"]["valid"], 47)
        self.assertEqual(summary["teacher"]["failed"], 1)
        self.assertEqual(
            [row["failure_code"] for row in samples].count(
                "teacher_response_invalid"
            ),
            1,
        )

    def test_all_replicates_invalid_keeps_the_preteacher_family_partition(self):
        from nmsim import v2_attention

        original_fake = v2_attention.fake_test_teacher
        calls = 0

        def invalidate_first_state(observation, replicate_index):
            nonlocal calls
            calls += 1
            if calls <= 2:
                return '{"action":"hold","intensity":0.5,"reasoning":"invalid"}'
            return original_fake(observation, replicate_index)

        with mock.patch.object(
            v2_attention, "fake_test_teacher", new=invalidate_first_state
        ), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            entrypoint.main(_small_full_args(self.out))
        run_dir = _single_run(self.out)
        manifest = _read_json(run_dir / "run_manifest.json")
        summary = _read_json(run_dir / "v2_attention_market_summary.json")
        dataset = _read_json(run_dir / "aggregated_dataset.json")
        split = _read_json(run_dir / "split_manifest.json")
        group_contract = manifest["v2_config_identities"]["scientific_config"][
            "group_split"
        ]
        zero_valid = [
            row for row in dataset["rows"] if row["soft_target"]["valid_n"] == 0
        ]
        self.assertEqual(len(zero_valid), 1)
        self.assertEqual(summary["teacher"]["valid"], 46)
        self.assertEqual(summary["dataset"]["aggregated_examples"], 23)
        self.assertEqual(manifest["honest_n_aggregated_examples"], 23)
        self.assertEqual(len(split["family_assignments"]), 24)
        self.assertEqual(
            sum(
                split["counts"][key]
                for key in ("train_rows", "validation_rows", "test_rows")
            ),
            23,
        )
        self.assertEqual(
            split["planned_split_hash"], group_contract["planned_split_hash"]
        )
        self.assertEqual(
            split["frozen_family_assignment_hash"],
            group_contract["frozen_family_assignment_hash"],
        )
        missing_row = zero_valid[0]["observation"]
        missing_family = missing_row["family_id"]
        partition = split["family_assignments"][missing_family]
        diagnostic_stratum = entrypoint._state_diagnostic_stratum(
            v2_attention.V2AttentionState.from_mapping(missing_row["state"])
        )
        planned_coverage = split[
            "planned_design_partition_regime_coverage"
        ]
        eligible_coverage = split[
            "training_eligible_partition_regime_coverage"
        ]
        planned_count = planned_coverage["partition_counts"][partition][
            "tape_x_position"
        ][diagnostic_stratum]
        eligible_count = eligible_coverage["partition_counts"][partition][
            "tape_x_position"
        ].get(diagnostic_stratum, 0)
        self.assertEqual(planned_count, eligible_count + 1)
        self.assertEqual(
            summary["dataset"]["planned_design_partition_regime_coverage"],
            planned_coverage,
        )
        self.assertEqual(
            summary["dataset"][
                "training_eligible_partition_regime_coverage"
            ],
            eligible_coverage,
        )


class V2InterruptedTeacherTests(unittest.TestCase):
    def test_live_interrupt_synchronizes_nested_network_and_honest_attempts(self):
        class InterruptedLiveProvider:
            model = "patched-live-model"

            def __init__(self):
                self.request_count = 0
                self.response_count = 0
                self.network_access = False
                self.batch_sizes = []

            async def complete_many(
                self, prompts, *, before_attempt=None, on_completion=None
            ):
                del on_completion
                self.batch_sizes.append(len(prompts))
                if before_attempt is not None:
                    before_attempt(0)
                self.request_count = 1
                self.network_access = True
                raise KeyboardInterrupt()

            async def aclose(self):
                return None

        temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(temporary.cleanup)
        out = Path(temporary.name) / "live-interrupt"
        with mock.patch.object(
            entrypoint,
            "_build_openai_provider",
            return_value=InterruptedLiveProvider(),
        ), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                entrypoint.main(
                    [
                        "--provider",
                        "openai",
                        "--model",
                        "patched-live-model",
                        "--live",
                        "--states",
                        "24",
                        "--replicates",
                        "1",
                        "--confirm-request-count",
                        "24",
                        "--out",
                        str(out),
                    ]
                )
        manifest = _read_json(_single_run(out) / "run_manifest.json")
        self.assertEqual(manifest["status"], "failed")
        self.assertTrue(manifest["llm"]["runtime"]["network_access"])
        self.assertTrue(manifest["v2_attention_market"]["network_access"])
        self.assertEqual(
            manifest["completion"]["provider_calls"]["attempted"], 1
        )
        teacher = manifest["v2_attention_market"]["teacher_samples"]
        self.assertEqual(teacher["attempted"], 1)
        self.assertEqual(teacher["unresolved_attempts"], 1)
        self.assertEqual(teacher["honest_n_teacher_samples"], 0)

    def test_keyboard_interrupt_preserves_resolved_rows_and_actual_attempts(self):
        from nmsim import v2_attention

        temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(temporary.cleanup)
        out = Path(temporary.name) / "interrupted"
        original_fake = v2_attention.fake_test_teacher
        calls = 0

        def interrupt_third(observation, replicate_index):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise KeyboardInterrupt()
            return original_fake(observation, replicate_index)

        with mock.patch.object(
            v2_attention, "fake_test_teacher", new=interrupt_third
        ), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                entrypoint.main(_small_full_args(out))
        run_dir = _single_run(out)
        manifest = _read_json(run_dir / "run_manifest.json")
        public_rows = _read_jsonl(run_dir / "teacher_samples.jsonl")
        private_rows = _read_jsonl(run_dir / "private_teacher_records.jsonl")
        events = _read_jsonl(run_dir / "events.jsonl")
        teacher = manifest["v2_attention_market"]["teacher_samples"]
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(len(public_rows), 2)
        self.assertEqual(len(private_rows), 2)
        self.assertEqual(teacher["attempted"], 3)
        self.assertEqual(teacher["resolved"], 2)
        self.assertEqual(teacher["valid"], 2)
        self.assertEqual(teacher["unresolved_attempts"], 1)
        self.assertEqual(teacher["honest_n_teacher_samples"], 2)
        self.assertEqual(manifest["honest_n_teacher_samples"], 2)
        provider_calls = manifest["completion"]["provider_calls"]
        self.assertEqual(provider_calls["attempted"], 3)
        self.assertEqual(provider_calls["succeeded"], 2)
        self.assertEqual(provider_calls["failed"], 1)
        self.assertEqual(
            sum(row.get("type") == "LLMRequestRecorded" for row in events),
            3,
        )
        self.assertEqual(
            sum(row.get("type") == "V2TeacherSampleValidated" for row in events),
            2,
        )

    def test_public_write_failure_keeps_private_raw_before_any_honest_n_claim(self):
        temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(temporary.cleanup)
        out = Path(temporary.name) / "public-write-failure"
        original_write = entrypoint._write_jsonl_row

        def fail_first_public(stream, row):
            if "system_prompt" not in row:
                raise OSError("injected public Teacher artifact failure")
            return original_write(stream, row)

        with mock.patch.object(
            entrypoint, "_write_jsonl_row", new=fail_first_public
        ), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(OSError):
                entrypoint.main(_small_full_args(out))
        run_dir = _single_run(out)
        manifest = _read_json(run_dir / "run_manifest.json")
        private_rows = _read_jsonl(run_dir / "private_teacher_records.jsonl")
        public_rows = _read_jsonl(run_dir / "teacher_samples.jsonl")
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(len(private_rows), 1)
        self.assertEqual(len(public_rows), 0)
        self.assertIsNotNone(private_rows[0]["raw_response"])
        self.assertEqual(manifest["honest_n_teacher_samples"], 0)
        self.assertEqual(
            manifest["v2_attention_market"]["teacher_samples"]["resolved"], 0
        )


class V2InterruptedMarketTests(unittest.TestCase):
    def test_interrupt_after_completed_round_counts_only_never_started_slots(self):
        from nmsim import v2_market_experiment

        temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(temporary.cleanup)
        out = Path(temporary.name) / "round-interrupted-after-progress"
        original_settle = v2_market_experiment.settle
        calls = 0

        def interrupt_second_settlement(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise KeyboardInterrupt()
            return original_settle(*args, **kwargs)

        with mock.patch.object(
            v2_market_experiment,
            "settle",
            new=interrupt_second_settlement,
        ), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                entrypoint.main(_small_full_args(out))
        run_dir = _single_run(out)
        manifest = _read_json(run_dir / "run_manifest.json")
        rounds = manifest["completion"]["rounds"]
        self.assertEqual(rounds["planned"], 12)
        self.assertEqual(rounds["started"], 2)
        self.assertEqual(rounds["completed"], 1)
        self.assertEqual(rounds["failed"], 1)
        self.assertEqual(rounds["skipped"], 10)
        market = manifest["v2_attention_market"]["market"]
        self.assertEqual(market["honest_n_market_runs"], 0)
        self.assertEqual(market["rounds"]["failed_after_start"], 1)
        self.assertEqual(
            market["rounds"]["unstarted_round_slots_in_failed_runs"], 1
        )
        round_logs = sorted(run_dir.glob("market_rounds_*_seed_*.jsonl"))
        self.assertEqual(len(round_logs), 1)
        self.assertEqual(len(_read_jsonl(round_logs[0])), 1)

    def test_late_cell_interrupt_preserves_prior_ledger_and_run_accounting(self):
        from nmsim import v2_market_experiment

        temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(temporary.cleanup)
        out = Path(temporary.name) / "market-interrupted"
        original_run_cell = v2_market_experiment._run_cell
        calls = 0

        def interrupt_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise KeyboardInterrupt()
            return original_run_cell(*args, **kwargs)

        with mock.patch.object(
            v2_market_experiment, "_run_cell", new=interrupt_second
        ), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                entrypoint.main(_small_full_args(out))
        run_dir = _single_run(out)
        manifest = _read_json(run_dir / "run_manifest.json")
        ledgers = sorted(run_dir.glob("market_*_seed_*.json"))
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(len(ledgers), 1)
        ledger = _read_json(ledgers[0])
        self.assertEqual(len(ledger["rounds"]), 3)
        self.assertTrue(
            all(
                all(round_row["conservation"].values())
                for round_row in ledger["rounds"]
            )
        )
        self.assertFalse((run_dir / "market_2x2_summary.json").exists())
        self.assertEqual(manifest["honest_n_market_runs"], 1)
        runs = manifest["completion"]["simulation_runs"]
        self.assertEqual(runs["planned"], 4)
        self.assertEqual(runs["started"], 2)
        self.assertEqual(runs["completed"], 1)
        self.assertEqual(runs["failed"], 1)
        rounds = manifest["completion"]["rounds"]
        self.assertEqual(rounds["planned"], 12)
        self.assertEqual(rounds["completed"], 3)
        self.assertEqual(rounds["skipped"], 9)
        market = manifest["v2_attention_market"]["market"]
        self.assertEqual(market["honest_n_market_runs"], 1)
        self.assertEqual(len(market["run_catalog"]), 1)
        self.assertEqual(
            market["rounds"]["unstarted_round_slots_in_failed_runs"], 3
        )

    def test_midround_interrupt_counts_started_round_without_claiming_completion(self):
        from nmsim import v2_market_experiment

        temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(temporary.cleanup)
        out = Path(temporary.name) / "round-interrupted"
        original_settle = v2_market_experiment.settle
        calls = 0

        def interrupt_fourth_settlement(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 4:
                raise KeyboardInterrupt()
            return original_settle(*args, **kwargs)

        with mock.patch.object(
            v2_market_experiment,
            "settle",
            new=interrupt_fourth_settlement,
        ), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                entrypoint.main(_small_full_args(out))
        run_dir = _single_run(out)
        manifest = _read_json(run_dir / "run_manifest.json")
        rounds = manifest["completion"]["rounds"]
        self.assertEqual(rounds["planned"], 12)
        self.assertEqual(rounds["started"], 4)
        self.assertEqual(rounds["completed"], 3)
        self.assertEqual(rounds["failed"], 1)
        self.assertEqual(rounds["skipped"], 8)
        market = manifest["v2_attention_market"]["market"]
        self.assertEqual(market["rounds"]["failed_after_start"], 1)
        self.assertEqual(
            market["rounds"]["unstarted_round_slots_in_failed_runs"], 2
        )
        round_logs = sorted(run_dir.glob("market_rounds_*_seed_*.jsonl"))
        self.assertEqual(len(round_logs), 2)
        self.assertEqual(sum(len(_read_jsonl(path)) for path in round_logs), 3)


if __name__ == "__main__":
    unittest.main()
