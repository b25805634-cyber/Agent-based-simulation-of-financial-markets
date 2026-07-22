from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

import experiments.multi_event as M
from experiments.driver_utils import expected_run_seed_identity
from nmsim.multi_event import load_protocol
from nmsim.provenance import sha256_file
from nmsim.result_reuse import (
    MANIFEST_MISSING,
    RESULT_REUSE_POLICY_VERSION,
    ReuseDecision,
)


ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = ROOT / "experiments" / "multi_event_protocol.json"
CATALOG = ROOT / "nmsim" / "reference_data" / "v1" / "catalog.json"
REAL_SUBPROCESS_RUN = subprocess.run


def _clean_environment(**extra: str) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {"LLM_PROVIDER", "LLM_MODEL", "OPENAI_BASE_URL", "OPENAI_API_KEY"}
    }
    environment.update(extra)
    return environment


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _decision(run_id: str, manifest_path: Path, *, reusable: bool) -> ReuseDecision:
    return ReuseDecision(
        policy_version=RESULT_REUSE_POLICY_VERSION,
        reusable=reusable,
        reason_codes=() if reusable else (MANIFEST_MISSING,),
        run_id=run_id,
        manifest_path=manifest_path,
        artifacts_verified=5 if reusable else 0,
        cross_commit_same_scientific_fingerprint=False,
    )


def _fake_builder(**kwargs):
    return {
        "schema_version": "1.0",
        "protocol_sha256": kwargs["protocol_sha256"],
        "execution_plan": dict(kwargs["execution_plan"]),
        "events": list(kwargs["events"]),
        "catalog_inputs": list(kwargs["catalog_inputs"]),
        "study_model_identity": dict(kwargs["study_model_identity"]),
        "children": list(kwargs["accepted_children"]),
        "missing_or_rejected_slots": list(kwargs["rejected_slots"]),
    }


class MultiEventDriverTests(unittest.TestCase):
    def _one_job(self, root: Path):
        protocol, protocol_hash, materials = M._load_materials(PROTOCOL, CATALOG)
        material = materials[0]
        job = M._build_jobs(
            [material],
            seeds=[11],
            repeats=[1],
            provider="mock",
            out_root=root,
        )[0]
        expected = expected_run_seed_identity(job.base_command)
        series_id = M._attempt_series_id(job, expected)
        allowed = [M._attempt_run_id(job, index, series_id) for index in range(1, 6)]
        return protocol, protocol_hash, material, job, expected, allowed

    def _accepted_record(self, job, expected, decision, attempt_run_ids):
        return {
            "event_id": job.event_id,
            "arm": job.arm,
            "seed": job.seed,
            "repeat_idx": job.repeat_idx,
            "manifest_path": f"runs/{decision.run_id}/run_manifest.json",
            "manifest_sha256": "1" * 64,
            "result_artifact": {"path": "experiment_result.json", "sha256": "2" * 64},
            "attempt_run_ids": list(attempt_run_ids),
            "accepted_run_id": decision.run_id,
            "identity": {
                "run_id": decision.run_id,
                "command_identity": expected.command_identity,
                "config_hash_schema_version": expected.config_hash_schema_version,
                "scientific_config_hash": expected.scientific_config_hash,
                "model_request_config_hash": expected.model_request_config_hash,
                "scientific_input_identity": expected.scientific_input_identity,
                "scenario_definition_hash": expected.scenario_definition_hash,
                "population_identity": expected.population_identity,
                "requested_provider": expected.requested_provider,
                "requested_model": expected.requested_model,
                "resolved_provider": expected.resolved_provider,
                "resolved_model": expected.resolved_model,
                "endpoint_identity": expected.endpoint_identity,
                "reported_model_aliases": [],
            },
        }

    def _patch_one_job(self, root: Path):
        protocol, protocol_hash, material, job, expected, allowed = self._one_job(root)
        aggregate = types.ModuleType("experiments.aggregate_multi_event")
        aggregate.build_selection_document = _fake_builder
        patches = (
            mock.patch.object(M, "_load_materials", return_value=(protocol, protocol_hash, [material])),
            mock.patch.object(M, "_build_jobs", return_value=[job]),
            mock.patch.dict(sys.modules, {"experiments.aggregate_multi_event": aggregate}),
            mock.patch.object(M, "_gate_reported_model", side_effect=lambda decision, **_kwargs: decision),
        )
        return patches, protocol, job, expected, allowed

    def test_dry_run_has_144_slots_five_full_ids_each_and_zero_network(self) -> None:
        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(
            os.environ, _clean_environment(), clear=True
        ), mock.patch.object(
            M, "_wait_for_endpoint", side_effect=AssertionError("socket access")
        ) as endpoint_wait:
            root = Path(raw)
            M.main(["--dry-run", "--out", str(root), "--run-id", "dry-parent"])
            run_dir = root / "runs" / "dry-parent"
            plan = json.loads((run_dir / M.PLAN_NAME).read_text(encoding="utf-8"))
            manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(plan["jobs"]), 144)
        all_ids = []
        for job in plan["jobs"]:
            self.assertEqual(len(job["attempt_series_id"]), 64)
            self.assertEqual(len(job["allowed_attempt_run_ids"]), 5)
            for index, run_id in enumerate(job["allowed_attempt_run_ids"], start=1):
                self.assertTrue(run_id.endswith(f"-ta{index}"))
                parts = run_id.split("-")
                self.assertEqual(len(parts[1]), 64)
                self.assertEqual(len(parts[2]), 64)
            all_ids.extend(job["allowed_attempt_run_ids"])
        self.assertEqual(len(all_ids), len(set(all_ids)))
        self.assertEqual(manifest["status"], "finished")
        self.assertFalse(manifest["multi_event_driver"]["network_access"])
        endpoint_wait.assert_not_called()

    def test_live_worker_override_is_rejected(self) -> None:
        protocol, _digest = load_protocol(PROTOCOL)
        args = M.build_argparser().parse_args(
            ["--provider", "openai", "--live", "--workers", "2"]
        )
        with mock.patch.dict(os.environ, _clean_environment(), clear=True), self.assertRaises(ValueError):
            M._validate_cli(args, protocol)

    def test_fail_then_success_preserves_both_attempts_and_registered_parent_artifacts(self) -> None:
        secret = "TOP_SECRET_RETRY_OUTPUT"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            patches, _protocol, job, expected, allowed = self._patch_one_job(root)
            calls = []

            def run_side_effect(command, *args, **kwargs):
                if isinstance(command, (list, tuple)) and "experiments.run_seed" in command:
                    run_id = command[command.index("--run-id") + 1]
                    calls.append(run_id)
                    (root / "runs" / run_id).mkdir(parents=True)
                    if len(calls) == 1:
                        return subprocess.CompletedProcess(command, 1, "", secret)
                    return subprocess.CompletedProcess(command, 0, "ok", "")
                return REAL_SUBPROCESS_RUN(command, *args, **kwargs)

            def assess_side_effect(*, candidate_path, **_kwargs):
                candidate = Path(candidate_path)
                run_id = candidate.parent.name
                return _decision(run_id, candidate, reusable=True)

            captured = {}

            def candidate_side_effect(current_job, decision, **kwargs):
                captured["attempts"] = list(kwargs["attempt_run_ids"])
                return self._accepted_record(
                    current_job, expected, decision, kwargs["attempt_run_ids"]
                )

            with patches[0], patches[1], patches[2], patches[3], mock.patch.object(
                M.subprocess, "run", side_effect=run_side_effect
            ), mock.patch.object(
                M, "assess_run_seed_reuse", side_effect=assess_side_effect
            ), mock.patch.object(
                M, "_candidate_record", side_effect=candidate_side_effect
            ), mock.patch.dict(
                os.environ,
                _clean_environment(OPENAI_API_KEY=secret),
                clear=True,
            ):
                M.main(["--provider", "mock", "--n", "1", "--k", "1", "--out", str(root), "--run-id", "retry-parent"])

            run_dir = root / "runs" / "retry-parent"
            selection = json.loads((run_dir / M.SELECTION_NAME).read_text(encoding="utf-8"))
            summary = json.loads((run_dir / "driver_summary.json").read_text(encoding="utf-8"))
            manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
            public = _read_jsonl(run_dir / M.ATTEMPT_LEDGER_NAME)
            private_text = (run_dir / M.PRIVATE_ATTEMPT_LEDGER_NAME).read_text(encoding="utf-8")
            self.assertEqual(calls, allowed[:2])
            self.assertEqual(captured["attempts"], allowed[:2])
            self.assertEqual(selection["children"][0]["accepted_run_id"], allowed[1])
            self.assertEqual([item["status"] for item in public], ["launched", "rejected", "launched", "accepted"])
            self.assertNotIn(secret, private_text)
            self.assertEqual(summary["executed_runs"], 2)
            self.assertEqual(summary["completed_runs"], 1)
            self.assertFalse(summary["incomplete"])
            registered = {item["path"]: item for item in manifest["results"]}
            required = {
                M.PLAN_NAME,
                M.SELECTION_NAME,
                M.ATTEMPT_LEDGER_NAME,
                M.PRIVATE_ATTEMPT_LEDGER_NAME,
                "driver_summary.json",
            }
            self.assertTrue(required <= set(registered))
            for name in required:
                self.assertEqual(registered[name]["sha256"], sha256_file(run_dir / name))
            self.assertEqual(stat.S_IMODE((run_dir / M.PRIVATE_ATTEMPT_LEDGER_NAME).stat().st_mode), 0o600)

    def test_subprocess_exception_is_durable_sanitized_and_partial_exit_is_nonzero(self) -> None:
        secret = "TOP_SECRET_SUBPROCESS_EXCEPTION"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            patches, _protocol, _job, _expected, allowed = self._patch_one_job(root)

            def run_side_effect(command, *args, **kwargs):
                if isinstance(command, (list, tuple)) and "experiments.run_seed" in command:
                    raise OSError(secret)
                return REAL_SUBPROCESS_RUN(command, *args, **kwargs)

            with patches[0], patches[1], patches[2], patches[3], mock.patch.object(
                M.subprocess, "run", side_effect=run_side_effect
            ), mock.patch.dict(
                os.environ,
                _clean_environment(OPENAI_API_KEY=secret),
                clear=True,
            ), self.assertRaises(SystemExit) as raised:
                M.main(["--provider", "mock", "--n", "1", "--k", "1", "--out", str(root), "--run-id", "exception-parent"])
            self.assertEqual(raised.exception.code, 1)
            run_dir = root / "runs" / "exception-parent"
            public = _read_jsonl(run_dir / M.ATTEMPT_LEDGER_NAME)
            selection = json.loads((run_dir / M.SELECTION_NAME).read_text(encoding="utf-8"))
            summary = json.loads((run_dir / "driver_summary.json").read_text(encoding="utf-8"))
            private_text = (run_dir / M.PRIVATE_ATTEMPT_LEDGER_NAME).read_text(encoding="utf-8")
            manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual([item["run_id"] for item in public], [allowed[0], allowed[0]])
            self.assertEqual([item["status"] for item in public], ["launched", "rejected"])
            self.assertEqual(selection["missing_or_rejected_slots"][0]["attempt_run_ids"], [allowed[0]])
            self.assertTrue(summary["incomplete"])
            self.assertNotIn(secret, private_text)
            self.assertEqual(manifest["status"], "finished")

    def test_materialized_interrupted_prefix_resumes_at_next_index(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            patches, _protocol, _job, expected, allowed = self._patch_one_job(root)
            (root / "runs" / allowed[0]).mkdir(parents=True)
            launched = []

            def assess_side_effect(*, candidate_path, **_kwargs):
                candidate = Path(candidate_path)
                run_id = candidate.parent.name if candidate.name == "run_manifest.json" else candidate.name
                return _decision(run_id, candidate, reusable=run_id == allowed[1])

            def run_side_effect(command, *args, **kwargs):
                if isinstance(command, (list, tuple)) and "experiments.run_seed" in command:
                    run_id = command[command.index("--run-id") + 1]
                    launched.append(run_id)
                    (root / "runs" / run_id).mkdir(parents=True)
                    return subprocess.CompletedProcess(command, 0, "ok", "")
                return REAL_SUBPROCESS_RUN(command, *args, **kwargs)

            def candidate_side_effect(current_job, decision, **kwargs):
                return self._accepted_record(current_job, expected, decision, kwargs["attempt_run_ids"])

            with patches[0], patches[1], patches[2], patches[3], mock.patch.object(
                M, "assess_run_seed_reuse", side_effect=assess_side_effect
            ), mock.patch.object(
                M.subprocess, "run", side_effect=run_side_effect
            ), mock.patch.object(
                M, "_candidate_record", side_effect=candidate_side_effect
            ), mock.patch.dict(os.environ, _clean_environment(), clear=True):
                M.main(["--provider", "mock", "--n", "1", "--k", "1", "--out", str(root), "--run-id", "resume-parent"])
            selection = json.loads((root / "runs" / "resume-parent" / M.SELECTION_NAME).read_text(encoding="utf-8"))
            self.assertEqual(launched, [allowed[1]])
            self.assertEqual(selection["children"][0]["attempt_run_ids"], allowed[:2])

    def test_exhausted_prefix_launches_no_sixth_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            patches, _protocol, _job, _expected, allowed = self._patch_one_job(root)
            for run_id in allowed:
                (root / "runs" / run_id).mkdir(parents=True)

            def assess_side_effect(*, candidate_path, **_kwargs):
                candidate = Path(candidate_path)
                run_id = candidate.name
                return _decision(run_id, candidate, reusable=False)

            def run_side_effect(command, *args, **kwargs):
                if isinstance(command, (list, tuple)) and "experiments.run_seed" in command:
                    raise AssertionError("sixth attempt launched")
                return REAL_SUBPROCESS_RUN(command, *args, **kwargs)

            with patches[0], patches[1], patches[2], patches[3], mock.patch.object(
                M, "assess_run_seed_reuse", side_effect=assess_side_effect
            ), mock.patch.object(
                M.subprocess, "run", side_effect=run_side_effect
            ), mock.patch.dict(os.environ, _clean_environment(), clear=True), self.assertRaises(SystemExit):
                M.main(["--provider", "mock", "--n", "1", "--k", "1", "--out", str(root), "--run-id", "exhausted-parent"])
            run_dir = root / "runs" / "exhausted-parent"
            selection = json.loads((run_dir / M.SELECTION_NAME).read_text(encoding="utf-8"))
            summary = json.loads((run_dir / "driver_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(selection["missing_or_rejected_slots"][0]["attempt_run_ids"], allowed)
            self.assertEqual(summary["executed_runs"], 0)
            self.assertEqual(summary["failure_codes"], {"attempt_budget_exhausted": 1})

    def test_gap_scan_fails_parent_manifest_in_managed_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            patches, _protocol, _job, _expected, allowed = self._patch_one_job(root)
            (root / "runs" / allowed[1]).mkdir(parents=True)
            with patches[0], patches[1], patches[2], patches[3], mock.patch.dict(
                os.environ, _clean_environment(), clear=True
            ), self.assertRaises(RuntimeError):
                M.main(["--provider", "mock", "--n", "1", "--k", "1", "--out", str(root), "--run-id", "gap-parent"])
            manifest = json.loads((root / "runs" / "gap-parent" / "run_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "failed")
            registered = {item["path"] for item in manifest["results"]}
            self.assertIn(M.PLAN_NAME, registered)
            self.assertIn(M.ATTEMPT_LEDGER_NAME, registered)


if __name__ == "__main__":
    unittest.main()
