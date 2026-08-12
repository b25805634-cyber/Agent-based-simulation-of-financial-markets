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


class V2OpenAIAdapterTests(unittest.TestCase):
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
