"""Wave0-T1 endpoint-stochasticity acceptance tests.

All execution paths in this module are offline.  The 1,080-sample study grid is
exercised with the in-process Fake provider; dry-run and rejected-live paths
make provider construction and socket access observable test failures.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict, is_dataclass
import io
import json
import math
import os
from pathlib import Path
import socket
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from experiments import endpoint_stochasticity as endpoint
from nmsim.config import Config


HASH_RE = r"^[0-9a-f]{64}$"
PRIVATE_MARKERS = (
    "private_rationale",
    "private deterministic endpoint-control rationale",
    '"raw_response":',
    '"system_prompt":',
    '"user_prompt":',
)
FROZEN_PANEL = (
    (
        "fuel",
        "retail_crowd",
        "negative_news_price_unchanged",
        "qcase-cc0dec3634c3c8bd87d254f8",
    ),
    (
        "fuel",
        "fomo_momentum",
        "price_crash_no_news",
        "qcase-0587fd0f734b430d3ef77b41",
    ),
    (
        "dampener",
        "value_institution",
        "deep_discount_to_fundamental",
        "qcase-b1760dde034a888e00e388cb",
    ),
    (
        "dampener",
        "quant_arb",
        "neutral_placebo_news",
        "qcase-d05d85bfa6ecc87c74905e43",
    ),
    (
        "spark",
        "influencer_amplifier",
        "conflicting_neighbor_views",
        "qcase-ddaedf5354a0d12a03ba066b",
    ),
    (
        "dampener",
        "contrarian_fund",
        "unanimous_neighbor_panic",
        "qcase-a5a5109b01a620d9dbc25f51",
    ),
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
        raise AssertionError("expected one managed run, found {}".format(len(manifests)))
    return manifests[0].parent


def _mapping(value) -> dict:
    if isinstance(value, dict):
        return value
    if is_dataclass(value):
        return asdict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        converted = to_dict()
        if isinstance(converted, dict):
            return converted
    raise AssertionError("study plan must be JSON-serializable as an object")


def _case_mapping(value) -> dict:
    return _mapping(value)


def _aggregate(rows: list[dict], plan) -> dict:
    return _mapping(endpoint.aggregate_grid_results(rows, plan))


class EndpointStudyPlanTests(unittest.TestCase):
    def test_constants_and_stable_48_to_6_plan_have_honest_request_counts(self):
        self.assertEqual(endpoint.TEMPERATURES, (0.0, 0.3))
        self.assertEqual(endpoint.CONCURRENCY_LEVELS, (1, 8, 32))
        self.assertEqual(endpoint.REPEATS, 30)

        first_plan = endpoint.load_study_plan()
        second_plan = endpoint.load_study_plan()
        first = _mapping(first_plan.public_plan)
        second = _mapping(second_plan.public_plan)
        all_cases = [_case_mapping(row) for row in first_plan.all_cases]
        selected_cases = [_case_mapping(row) for row in first_plan.selected_cases]
        self.assertEqual(first, second)
        self.assertEqual(first_plan.study_plan_hash, second_plan.study_plan_hash)
        self.assertEqual(first["source_case_count"], 48)
        self.assertEqual(first["selected_case_count"], 6)
        self.assertEqual(len(all_cases), 48)
        self.assertEqual(len(selected_cases), 6)
        self.assertEqual(
            len({row["case_id"] for row in all_cases}),
            48,
        )
        self.assertEqual(
            len({row["case_id"] for row in selected_cases}),
            6,
        )
        self.assertEqual(
            {row["persona_id"] for row in selected_cases},
            {
                "retail_crowd",
                "fomo_momentum",
                "value_institution",
                "quant_arb",
                "influencer_amplifier",
                "contrarian_fund",
            },
        )
        self.assertEqual(
            {row.get("mechanism_role", row.get("role")) for row in selected_cases},
            {"fuel", "dampener", "spark"},
        )
        self.assertEqual(
            first["role_coverage"],
            {"fuel": 2, "dampener": 3, "spark": 1},
        )
        self.assertEqual(
            tuple(
                (
                    row.get("mechanism_role", row.get("role")),
                    row["persona_id"],
                    row["fixture_id"],
                    row["case_id"],
                )
                for row in selected_cases
            ),
            FROZEN_PANEL,
        )
        for name in ("protocol_hash", "fixture_set_hash", "selection_hash"):
            self.assertRegex(first[name], HASH_RE)
        self.assertRegex(first_plan.study_plan_hash, HASH_RE)
        self.assertEqual(
            first_plan.study_plan_hash,
            endpoint.EXPECTED_STUDY_PLAN_HASH,
        )
        self.assertEqual(
            first["selection_hash"],
            "db008c386d6eb5a9dccdd91d4c8e978c22523a969857187b1e3e56327488223c",
        )

        self.assertEqual(first["temperatures"], [0.0, 0.3])
        self.assertEqual(first["concurrency_levels"], [1, 8, 32])
        self.assertEqual(first["repeats"], 30)
        self.assertEqual(first["planned_grid_requests"], 6 * 2 * 30 * 3)
        self.assertEqual(first["planned_seed_probe_requests"], 2)
        self.assertEqual(first["planned_total_requests"], 1082)
        schedule = first["block_schedule"]
        self.assertEqual(len(schedule), 36)
        self.assertEqual(
            {row["wave_index"] for row in schedule},
            set(range(6)),
        )
        condition_counts = Counter()
        position_counts = Counter()
        for row in schedule:
            condition = (row["temperature"], row["concurrency_level"])
            repeat_count = (
                row["repeat_stop_exclusive"] - row["repeat_start_inclusive"]
            )
            condition_counts[condition] += repeat_count * 6
            position_counts[(condition, row["within_wave_position"])] += 1
        self.assertEqual(
            set(condition_counts),
            {(temperature, concurrency)
             for temperature in endpoint.TEMPERATURES
             for concurrency in endpoint.CONCURRENCY_LEVELS},
        )
        self.assertEqual(set(condition_counts.values()), {180})
        self.assertEqual(set(position_counts.values()), {1})
        requests = endpoint.build_grid_requests(first_plan)
        self.assertEqual(
            [request.attempt_order for request in requests],
            list(range(1080)),
        )
        for block_index, block_spec in enumerate(schedule):
            block_requests = [
                request
                for request in requests
                if request.schedule_block_index == block_index
            ]
            expected_count = 6 * (
                block_spec["repeat_stop_exclusive"]
                - block_spec["repeat_start_inclusive"]
            )
            self.assertEqual(len(block_requests), expected_count)
            self.assertEqual(
                {request.wave_index for request in block_requests},
                {block_spec["wave_index"]},
            )
            self.assertEqual(
                {request.within_wave_position for request in block_requests},
                {block_spec["within_wave_position"]},
            )

    def test_known_grid_values_define_pairwise_rate_and_sample_sigma(self):
        plan_object = endpoint.load_study_plan()
        plan = _mapping(plan_object.public_plan)
        case = _case_mapping(plan_object.selected_cases[0])
        signed_orders = (-2, -2, 4, 4)
        sentiments = (-1.0, -1.0, 1.0, 1.0)
        hashes = ("a" * 64, "a" * 64, "b" * 64, "b" * 64)
        rows = []
        for repeat_index, (signed, sentiment, response_hash) in enumerate(
            zip(signed_orders, sentiments, hashes)
        ):
            side = "sell" if signed < 0 else "buy"
            rows.append(
                {
                    "sample_schema_version": "1.0",
                    "measurement_kind": "grid",
                    "sample_id": "known-{}".format(repeat_index),
                    "attempt_order": repeat_index,
                    "case_id": case["case_id"],
                    "fixture_id": case["fixture_id"],
                    "persona_id": case["persona_id"],
                    "mechanism_role": case["mechanism_role"],
                    "temperature": 0.0,
                    "concurrency_level": 1,
                    "repeat_index": repeat_index,
                    "status": "completed",
                    "raw_response_sha256": response_hash,
                    "parse_failed": False,
                    "sentiment": sentiment,
                    "order": {
                        "side": side,
                        "quantity": abs(signed),
                        "limit_price": 100.0,
                        "signed_quantity": signed,
                    },
                    "public_take": "public",
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "latency_ms": 1.0,
                }
            )

        aggregate = _aggregate(rows, plan_object)
        self.assertEqual(aggregate["honest_n_endpoint_responses"], 4)
        self.assertEqual(aggregate["honest_n_parsed_decisions"], 4)
        self.assertEqual(aggregate["parse_failure_count"], 0)
        self.assertEqual(len(aggregate["cell_summaries"]), 1)
        self.assertEqual(len(aggregate["sigma_table"]), 1)
        cell = aggregate["cell_summaries"][0]
        self.assertEqual(cell["temperature"], 0.0)
        self.assertEqual(cell["concurrency_level"], 1)
        self.assertEqual(cell["honest_n"], 4)
        self.assertEqual(cell["parsed_n"], 4)
        self.assertEqual(cell["matching_response_pairs"], 2)
        self.assertEqual(cell["eligible_response_pairs"], 6)
        self.assertTrue(math.isclose(cell["byte_identical_rate"], 1.0 / 3.0))
        self.assertTrue(math.isclose(cell["sentiment_variance"], 4.0 / 3.0))
        self.assertTrue(math.isclose(cell["sentiment_sigma"], math.sqrt(4.0 / 3.0)))
        self.assertTrue(math.isclose(cell["signed_order_variance"], 12.0))
        self.assertTrue(math.isclose(cell["signed_order_sigma"], math.sqrt(12.0)))
        pooled = aggregate["sigma_table"][0]
        self.assertEqual(pooled["temperature"], 0.0)
        self.assertEqual(pooled["concurrency_level"], 1)
        self.assertEqual(pooled["within_case_degrees_of_freedom"], 3)
        self.assertTrue(
            math.isclose(pooled["sentiment_sigma"], math.sqrt(4.0 / 3.0))
        )
        self.assertTrue(
            math.isclose(pooled["signed_order_sigma"], math.sqrt(12.0))
        )

    def test_failures_keep_response_parse_and_pairwise_denominators_distinct(self):
        plan = endpoint.load_study_plan()
        case = _case_mapping(plan.selected_cases[0])
        base = {
            "sample_schema_version": "1.0",
            "measurement_kind": "grid",
            "case_id": case["case_id"],
            "fixture_id": case["fixture_id"],
            "persona_id": case["persona_id"],
            "mechanism_role": case["mechanism_role"],
            "temperature": 0.0,
            "concurrency_level": 1,
        }
        rows = [
            {
                **base,
                "sample_id": "valid-0",
                "status": "completed",
                "raw_response_sha256": "a" * 64,
                "parse_failed": False,
                "sentiment": 0.5,
                "order": {
                    "side": "buy",
                    "quantity": 2,
                    "signed_quantity": 2,
                    "limit_price": 100.0,
                },
            },
            {
                **base,
                "sample_id": "valid-1",
                "status": "completed",
                "raw_response_sha256": "a" * 64,
                "parse_failed": False,
                "sentiment": -0.5,
                "order": {
                    "side": "sell",
                    "quantity": 2,
                    "signed_quantity": -2,
                    "limit_price": 100.0,
                },
            },
            {
                **base,
                "sample_id": "unparsed",
                "status": "completed",
                "raw_response_sha256": "b" * 64,
                "parse_failed": True,
                "sentiment": 0.0,
                "order": {
                    "side": "hold",
                    "quantity": 0,
                    "signed_quantity": 0,
                    "limit_price": 100.0,
                },
            },
            {
                **base,
                "sample_id": "transport-failed",
                "status": "transport_failed",
                "raw_response_sha256": None,
                "parse_failed": None,
                "sentiment": None,
                "order": None,
            },
        ]

        aggregate = endpoint.aggregate_grid_results(rows, plan)
        self.assertEqual(aggregate["honest_n_endpoint_responses"], 3)
        self.assertEqual(aggregate["honest_n_parsed_decisions"], 2)
        self.assertEqual(aggregate["parse_failure_count"], 1)
        cell = aggregate["cell_summaries"][0]
        self.assertEqual(cell["attempted"], 4)
        self.assertEqual(cell["honest_n"], 3)
        self.assertEqual(cell["parsed_n"], 2)
        self.assertEqual(cell["transport_failure_count"], 1)
        self.assertEqual(cell["parse_failure_count"], 1)
        self.assertEqual(cell["matching_response_pairs"], 1)
        self.assertEqual(cell["eligible_response_pairs"], 3)
        self.assertTrue(math.isclose(cell["byte_identical_rate"], 1.0 / 3.0))

    def test_summary_distinguishes_total_and_grid_parse_failures(self):
        plan = endpoint.load_study_plan()
        case = _case_mapping(plan.selected_cases[0])

        def row(sample_id, measurement_kind, parse_failed, attempt_order):
            return {
                "sample_schema_version": "1.0",
                "measurement_kind": measurement_kind,
                "sample_id": sample_id,
                "attempt_order": attempt_order,
                "case_id": case["case_id"],
                "fixture_id": case["fixture_id"],
                "persona_id": case["persona_id"],
                "mechanism_role": case["mechanism_role"],
                "temperature": 0.3 if measurement_kind == "seed_probe" else 0.0,
                "concurrency_level": 1,
                "repeat_index": 0 if measurement_kind == "grid" else None,
                "seed_probe_index": (
                    attempt_order - 1 if measurement_kind == "seed_probe" else None
                ),
                "seed": (
                    endpoint.SEED_PROBE_VALUE
                    if measurement_kind == "seed_probe"
                    else None
                ),
                "status": "completed",
                "raw_response_sha256": chr(ord("a") + attempt_order) * 64,
                "parse_failed": parse_failed,
                "sentiment": 0.0,
                "order": {
                    "side": "hold",
                    "quantity": 0,
                    "signed_quantity": 0,
                    "limit_price": 100.0,
                },
                "input_tokens": 1,
                "output_tokens": 1,
                "total_tokens": 2,
                "token_count_source": "provider_usage",
            }

        rows = [
            row("grid", "grid", False, 0),
            row("seed-0", "seed_probe", True, 1),
            row("seed-1", "seed_probe", False, 2),
        ]
        summary = endpoint.build_summary(
            SimpleNamespace(run_id="summary-scope"),
            args=SimpleNamespace(
                provider="fake_test_provider",
                model="",
                live=False,
            ),
            plan=plan,
            model=endpoint.FakeEndpointProvider.model,
            provider=SimpleNamespace(network_access=False),
            public_rows=rows,
        )
        self.assertEqual(summary["completion"]["grid"]["parse_failures"], 0)
        self.assertEqual(summary["completion"]["total"]["parse_failures"], 1)
        self.assertEqual(summary["grid_parse_failure_count"], 0)
        self.assertEqual(summary["parse_failure_count"], 1)


class EndpointProviderContractTests(unittest.TestCase):
    def test_dedicated_openai_adapter_is_single_attempt_env_auth_and_bounded(self):
        http_client = object()
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch(
            "httpx.AsyncClient", return_value=http_client
        ) as async_client, mock.patch("openai.AsyncOpenAI") as openai_client:
            cfg = Config(
                openai_base_url="https://example.invalid/v1",
                openai_api_key="must-not-enter-diagnostic",
            )
            provider = endpoint.OpenAIEndpointProvider(
                cfg=cfg,
                model="probe-model",
            )

        async_kwargs = async_client.call_args.kwargs
        self.assertFalse(async_kwargs["trust_env"])
        self.assertEqual(async_kwargs["limits"].max_connections, 32)
        self.assertEqual(async_kwargs["limits"].max_keepalive_connections, 32)
        client_kwargs = openai_client.call_args.kwargs
        self.assertEqual(client_kwargs["base_url"], "https://example.invalid/v1")
        self.assertEqual(client_kwargs["api_key"], "EMPTY")
        self.assertIs(client_kwargs["http_client"], http_client)
        self.assertEqual(client_kwargs["max_retries"], 0)
        self.assertEqual(provider.model, "probe-model")

        capability = endpoint._endpoint_adapter_capability_snapshot(
            "openai",
            endpoint="https://example.invalid/v1",
        )
        adapter = capability["adapter"]
        self.assertEqual(
            capability["capability_scope"],
            "endpoint_stochasticity_dedicated_adapter",
        )
        self.assertEqual(adapter["provider_retry_count"], 0)
        self.assertFalse(adapter["supports_cache"])
        self.assertFalse(adapter["supports_record_replay"])
        self.assertTrue(adapter["sends_seed_request_field"])
        self.assertEqual(adapter["http_trust_env"], False)
        self.assertRegex(capability["capability_snapshot_sha256"], HASH_RE)

    def test_openai_probe_forwards_temperature_and_seed_without_retry_wrapper(self):
        observed = []

        class FakeCompletions:
            async def create(self, **kwargs):
                observed.append(kwargs)
                return SimpleNamespace(
                    id="response-id",
                    model="reported-model",
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content='{"action":"hold"}')
                        )
                    ],
                    usage=SimpleNamespace(
                        prompt_tokens=11,
                        completion_tokens=3,
                        total_tokens=14,
                    ),
                )

        provider = object.__new__(endpoint.OpenAIEndpointProvider)
        provider._client = SimpleNamespace(
            chat=SimpleNamespace(completions=FakeCompletions())
        )
        provider.model = "requested-model"
        provider.max_tokens = endpoint.MAX_TOKENS
        provider.request_count = 0
        provider.response_count = 0
        provider.network_access = False
        provider.batch_sizes = []

        completion = asyncio.run(
            provider.complete(
                "system",
                "user",
                temperature=0.3,
                seed=endpoint.SEED_PROBE_VALUE,
            )
        )

        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]["temperature"], 0.3)
        self.assertEqual(observed[0]["seed"], endpoint.SEED_PROBE_VALUE)
        self.assertEqual(observed[0]["max_tokens"], endpoint.MAX_TOKENS)
        self.assertEqual(completion.total_tokens, 14)
        self.assertEqual(provider.request_count, 1)
        self.assertEqual(provider.response_count, 1)
        self.assertTrue(provider.network_access)


class EndpointManagedCLITests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)

    def test_help_creates_no_managed_attempt(self):
        out = self.root / "help"
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as raised:
            endpoint.main(["--help", "--out", str(out)])
        self.assertEqual(raised.exception.code, 0)
        self.assertFalse(out.exists())

    def test_dry_run_writes_complete_plan_without_provider_or_socket(self):
        out = self.root / "dry"
        with mock.patch.object(
            endpoint,
            "_build_provider",
            side_effect=AssertionError("provider constructed during dry-run"),
        ) as build, mock.patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError("socket opened during dry-run"),
        ) as network, redirect_stdout(io.StringIO()):
            endpoint.main(
                [
                    "--provider",
                    "openai",
                    "--model",
                    "dry-run-model",
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
        plan = summary["study_plan"]
        self.assertTrue(summary["dry_run"])
        self.assertEqual(plan["source_case_count"], 48)
        self.assertEqual(plan["selected_case_count"], 6)
        self.assertEqual(plan["planned_grid_requests"], 1080)
        self.assertEqual(plan["planned_seed_probe_requests"], 2)
        self.assertEqual(plan["planned_total_requests"], 1082)
        self.assertEqual(len(plan["source_cases"]), 48)
        self.assertEqual(len(plan["selected_cases"]), 6)
        self.assertEqual(manifest["status"], "finished")
        self.assertEqual(
            manifest["managed_context"]["run_kind"],
            "endpoint_stochasticity",
        )
        self.assertTrue(manifest["endpoint_stochasticity"]["dry_run"])
        self.assertFalse(manifest["endpoint_stochasticity"]["live"])
        adapter = manifest["endpoint_stochasticity"][
            "provider_capability_snapshot"
        ]["adapter"]
        self.assertEqual(
            adapter["adapter_id"],
            "endpoint_stochasticity_openai_compatible_v1",
        )
        self.assertEqual(adapter["provider_retry_count"], 0)
        self.assertFalse(adapter["supports_cache"])
        self.assertEqual(manifest["completion"]["provider_calls"]["attempted"], 0)
        self.assertEqual(
            manifest["completion"]["llm_logical_requests"]["attempted"], 0
        )
        self.assertFalse(manifest["llm"]["runtime"]["network_access"])
        self.assertEqual(summary["provider_calls"], 0)
        self.assertFalse(summary["network_access"])
        self.assertFalse((run_dir / "endpoint_samples.jsonl").exists())
        self.assertFalse((run_dir / "private_endpoint_records.jsonl").exists())

    def test_openai_without_live_fails_before_provider_construction(self):
        out = self.root / "live-guard"
        stderr = io.StringIO()
        with mock.patch.object(
            endpoint,
            "_build_provider",
            side_effect=AssertionError("unguarded OpenAI provider constructed"),
        ) as build, mock.patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError("unguarded OpenAI socket opened"),
        ) as network, redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            endpoint.main(
                [
                    "--provider",
                    "openai",
                    "--model",
                    "guarded-model",
                    "--out",
                    str(out),
                ]
            )

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--live", stderr.getvalue())
        build.assert_not_called()
        network.assert_not_called()
        manifest = _read_json(_single_run(out) / "run_manifest.json")
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["failure_stage"], "provider_setup")
        self.assertEqual(manifest["completion"]["provider_calls"]["attempted"], 0)
        self.assertFalse(manifest["llm"]["runtime"]["network_access"])

    def test_mid_grid_failure_retains_partial_rows_and_endpoint_honest_n(self):
        out = self.root / "partial-grid"
        original_execute_block = endpoint._execute_block
        block_calls = 0

        async def fail_on_second_block(*args, **kwargs):
            nonlocal block_calls
            block_calls += 1
            if block_calls == 2:
                raise RuntimeError("injected mid-grid failure")
            return await original_execute_block(*args, **kwargs)

        with mock.patch.object(
            endpoint,
            "_execute_block",
            new=fail_on_second_block,
        ), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), self.assertRaises(
            RuntimeError
        ):
            endpoint.main(
                [
                    "--provider",
                    "fake_test_provider",
                    "--out",
                    str(out),
                ]
            )

        run_dir = _single_run(out)
        manifest = _read_json(run_dir / "run_manifest.json")
        public_rows = _read_jsonl(run_dir / "endpoint_samples.jsonl")
        private_path = run_dir / "private_endpoint_records.jsonl"
        private_rows = _read_jsonl(private_path)
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(len(public_rows), 36)
        self.assertEqual(len(private_rows), 36)
        self.assertEqual(manifest["honest_n_endpoint_responses"], 36)
        self.assertEqual(manifest["honest_n_parsed_decisions"], 36)
        endpoint_manifest = manifest["endpoint_stochasticity"]
        self.assertEqual(endpoint_manifest["honest_n_endpoint_responses"], 36)
        self.assertEqual(endpoint_manifest["honest_n_parsed_decisions"], 36)
        self.assertEqual(endpoint_manifest["endpoint_responses"]["attempted"], 36)
        self.assertEqual(endpoint_manifest["endpoint_responses"]["skipped"], 1046)
        self.assertEqual(stat.S_IMODE(private_path.stat().st_mode), 0o600)

    def test_stream_failure_never_overstates_endpoint_honest_n(self):
        out = self.root / "stream-failure"
        original_append = endpoint._append_jsonl
        append_calls = 0

        def fail_first_public_append(stream, rows):
            nonlocal append_calls
            append_calls += 1
            if append_calls == 2:
                raise OSError("injected public sample fsync failure")
            return original_append(stream, rows)

        with mock.patch.object(
            endpoint,
            "_append_jsonl",
            new=fail_first_public_append,
        ), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), self.assertRaises(
            OSError
        ):
            endpoint.main(
                [
                    "--provider",
                    "fake_test_provider",
                    "--out",
                    str(out),
                ]
            )

        run_dir = _single_run(out)
        manifest = _read_json(run_dir / "run_manifest.json")
        public_rows = _read_jsonl(run_dir / "endpoint_samples.jsonl")
        private_rows = _read_jsonl(run_dir / "private_endpoint_records.jsonl")
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(public_rows, [])
        self.assertEqual(len(private_rows), 36)
        self.assertEqual(manifest["honest_n_endpoint_responses"], 0)
        self.assertEqual(manifest["honest_n_parsed_decisions"], 0)
        endpoint_manifest = manifest["endpoint_stochasticity"]
        self.assertEqual(endpoint_manifest["honest_n_endpoint_responses"], 0)
        self.assertEqual(endpoint_manifest["honest_n_parsed_decisions"], 0)
        self.assertEqual(endpoint_manifest["endpoint_responses"]["attempted"], 0)
        self.assertEqual(manifest["completion"]["llm_logical_requests"]["completed"], 36)

    def test_fake_full_grid_is_1082_offline_requests_with_zero_noise_floor(self):
        out = self.root / "fake-grid"
        fake_provider = endpoint.FakeEndpointProvider()
        with mock.patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError("Fake provider opened a socket"),
        ) as network, mock.patch.object(
            endpoint,
            "_build_provider",
            return_value=fake_provider,
        ), redirect_stdout(io.StringIO()):
            endpoint.main(
                [
                    "--provider",
                    "fake_test_provider",
                    "--out",
                    str(out),
                ]
            )

        network.assert_not_called()
        run_dir = _single_run(out)
        manifest = _read_json(run_dir / "run_manifest.json")
        summary = _read_json(run_dir / "endpoint_stochasticity_summary.json")
        samples = _read_jsonl(run_dir / "endpoint_samples.jsonl")
        private_path = run_dir / "private_endpoint_records.jsonl"
        private_rows = _read_jsonl(private_path)

        self.assertEqual(manifest["status"], "finished")
        self.assertEqual(
            manifest["managed_context"]["run_kind"],
            "endpoint_stochasticity",
        )
        self.assertFalse(manifest["llm"]["runtime"]["network_access"])
        self.assertEqual(manifest["completion"]["provider_calls"]["attempted"], 1082)
        self.assertEqual(manifest["completion"]["provider_calls"]["succeeded"], 1082)
        self.assertEqual(
            manifest["completion"]["llm_logical_requests"]["completed"], 1082
        )
        self.assertEqual(summary["completion"]["grid"]["planned"], 1080)
        self.assertEqual(summary["completion"]["grid"]["completed"], 1080)
        self.assertEqual(summary["completion"]["seed_probe"]["planned"], 2)
        self.assertEqual(summary["completion"]["seed_probe"]["completed"], 2)
        self.assertEqual(summary["completion"]["total"]["planned"], 1082)
        self.assertEqual(summary["completion"]["total"]["completed"], 1082)
        self.assertEqual(summary["honest_n_endpoint_responses"], 1082)
        self.assertEqual(summary["honest_n_parsed_decisions"], 1082)
        self.assertEqual(fake_provider.max_inflight, 32)
        self.assertEqual(
            Counter(fake_provider.batch_sizes),
            Counter({36: 6, 30: 24, 24: 6, 2: 1}),
        )
        self.assertEqual(len(samples), 1082)
        self.assertEqual(len(private_rows), 1082)
        self.assertEqual(
            [row["attempt_order"] for row in samples],
            list(range(1082)),
        )
        self.assertEqual(
            sum(row["measurement_kind"] == "grid" for row in samples),
            1080,
        )
        self.assertEqual(
            sum(row["measurement_kind"] == "seed_probe" for row in samples),
            2,
        )
        grid_counts = Counter(
            (row["case_id"], row["temperature"], row["concurrency_level"])
            for row in samples
            if row["measurement_kind"] == "grid"
        )
        self.assertEqual(len(grid_counts), 6 * 2 * 3)
        self.assertEqual(set(grid_counts.values()), {30})
        self.assertEqual(len(summary["sigma_table"]), 6)
        self.assertEqual(len(summary["cell_summaries"]), 36)
        for cell in summary["sigma_table"]:
            self.assertIn(cell["temperature"], (0.0, 0.3))
            self.assertIn(cell["concurrency_level"], (1, 8, 32))
            self.assertEqual(cell["honest_n"], 180)
            self.assertEqual(cell["parsed_n"], 180)
            self.assertEqual(cell["byte_identical_rate"], 1.0)
            self.assertEqual(cell["sentiment_sigma"], 0.0)
            self.assertEqual(cell["signed_order_sigma"], 0.0)
        for cell in summary["cell_summaries"]:
            self.assertEqual(cell["honest_n"], 30)
            self.assertEqual(cell["parsed_n"], 30)
            self.assertEqual(cell["eligible_response_pairs"], 435)
            self.assertEqual(cell["matching_response_pairs"], 435)
            self.assertEqual(cell["byte_identical_rate"], 1.0)
            self.assertEqual(cell["sentiment_variance"], 0.0)
            self.assertEqual(cell["signed_order_variance"], 0.0)
        self.assertEqual(summary["parse_failure_count"], 0)
        self.assertEqual(summary["grid_parse_failure_count"], 0)
        capability = manifest["endpoint_stochasticity"][
            "provider_capability_snapshot"
        ]
        self.assertEqual(
            capability,
            manifest["llm"]["provider_capability_snapshot"],
        )
        self.assertEqual(
            capability["adapter"]["adapter_id"],
            "endpoint_stochasticity_fake_v1",
        )
        self.assertTrue(capability["adapter"]["supports_async"])
        self.assertTrue(capability["adapter"]["supports_temperature_request"])
        self.assertTrue(capability["adapter"]["supports_usage_metadata"])
        self.assertTrue(capability["adapter"]["supports_provider_response_id"])
        public_paths = (
            run_dir / "run_manifest.json",
            run_dir / "events.jsonl",
            run_dir / "endpoint_samples.jsonl",
            run_dir / "endpoint_stochasticity_summary.json",
        )

        grid = next(row for row in samples if row["measurement_kind"] == "grid")
        self.assertTrue(
            {
                "sample_schema_version",
                "measurement_kind",
                "sample_id",
                "case_id",
                "fixture_id",
                "persona_id",
                "mechanism_role",
                "temperature",
                "concurrency_level",
                "repeat_index",
                "schedule_block_index",
                "wave_index",
                "within_wave_position",
                "raw_response_sha256",
                "parse_failed",
                "sentiment",
                "order",
                "public_take",
                "input_tokens",
                "output_tokens",
                "latency_ms",
            }.issubset(grid)
        )
        self.assertRegex(grid["raw_response_sha256"], HASH_RE)
        self.assertEqual(
            set(grid["order"]),
            {"side", "quantity", "limit_price", "signed_quantity"},
        )
        self.assertGreaterEqual(grid["input_tokens"], 0)
        self.assertGreaterEqual(grid["output_tokens"], 0)
        self.assertGreaterEqual(grid["latency_ms"], 0.0)

        probe_rows = [
            row for row in samples if row["measurement_kind"] == "seed_probe"
        ]
        probe = summary["seed_probe"]
        self.assertEqual(len(probe_rows), 2)
        self.assertEqual(probe["planned"], 2)
        self.assertEqual(probe["attempted"], 2)
        self.assertEqual(probe["endpoint_responses"], 2)
        self.assertEqual(probe["seed_parameter_status"], "accepted")
        self.assertTrue(probe["two_response_byte_identical"])
        self.assertEqual(len(probe["evidence"]), 2)
        self.assertEqual(
            len(set(probe["raw_response_sha256"])),
            1,
        )
        self.assertEqual({row["seed"] for row in probe_rows}, {probe["seed"]})

        for path in public_paths:
            public_text = path.read_text(encoding="utf-8")
            for marker in PRIVATE_MARKERS:
                self.assertNotIn(marker, public_text)
        private_text = private_path.read_text(encoding="utf-8")
        self.assertIn('"raw_response":', private_text)
        self.assertIn('"system_prompt":', private_text)
        self.assertIn('"user_prompt":', private_text)
        self.assertEqual(stat.S_IMODE(private_path.stat().st_mode), 0o600)
        self.assertEqual(
            stat.S_IMODE((run_dir / "private_events.jsonl").stat().st_mode),
            0o600,
        )


if __name__ == "__main__":
    unittest.main()
