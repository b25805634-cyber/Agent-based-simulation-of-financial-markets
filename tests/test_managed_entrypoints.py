"""Managed-entrypoint boundaries and experiment-driver completion tests.

The tests use only MockLLM, strict offline replay, or local test doubles.  Any
attempt to open a network connection in a failure-path case is made observable.
"""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import importlib
import io
import json
from pathlib import Path
import re
import socket
import tempfile
import unittest
from unittest import mock

from experiments import grid2x2
from experiments.driver_utils import DriverJobResult, ManagedDriverCompletion
from nmsim import entrypoints
from nmsim.config import Config
from nmsim.recording import ReplayMismatchError


run_module = importlib.import_module("nmsim.run")
REPO_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_OUTPUTS = {
    "price_path.csv",
    "reasoning_traces.csv",
    "propagation.csv",
    "stylized_facts.json",
    "config.json",
    "sim_overview.png",
}


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_events(run_dir: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _single_run_dir(out_root: Path) -> Path:
    run_dirs = sorted(path.parent for path in (out_root / "runs").glob("*/run_manifest.json"))
    if len(run_dirs) != 1:
        raise AssertionError("expected one managed run directory, found {}".format(len(run_dirs)))
    return run_dirs[0]


class EntrypointRegistryTests(unittest.TestCase):
    def test_executable_surfaces_are_registered_and_official_entries_are_managed(self):
        """A new executable module cannot silently remain unclassified."""

        main_guard = re.compile(r"^if __name__ == [\"']__main__[\"']:", re.MULTILINE)
        discovered = set()
        for root_name in ("nmsim", "experiments"):
            for path in (REPO_ROOT / root_name).glob("*.py"):
                if main_guard.search(path.read_text(encoding="utf-8")):
                    discovered.add(path.relative_to(REPO_ROOT).as_posix())
        legacy = REPO_ROOT / "narrative_market_sim.py"
        if main_guard.search(legacy.read_text(encoding="utf-8")):
            discovered.add(legacy.relative_to(REPO_ROOT).as_posix())

        registered = {spec.path for spec in entrypoints.ENTRYPOINTS}
        self.assertEqual(discovered - registered, set())

        official = entrypoints.official_entrypoints()
        self.assertTrue(official)
        for spec in official:
            with self.subTest(entrypoint=spec.entrypoint_id):
                self.assertTrue(spec.formal_research_allowed)
                self.assertIn(
                    spec.management,
                    {
                        entrypoints.DIRECT_MANAGED,
                        entrypoints.DELEGATED_MANAGED_DRIVER,
                        entrypoints.ANALYSIS_MANAGED,
                    },
                )

    def test_registry_validation_fails_closed_for_unmanaged_official_entry(self):
        invalid = replace(
            entrypoints.official_entrypoints()[0],
            entrypoint_id="test.unmanaged-official",
            management=entrypoints.NULL_CONTEXT,
        )
        with mock.patch.object(
            entrypoints, "ENTRYPOINTS", entrypoints.ENTRYPOINTS + (invalid,)
        ):
            with self.assertRaisesRegex(ValueError, "official entrypoint is not managed"):
                entrypoints.validate_registry()


class ManagedCLIEntrypointTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)

    def _assert_pre_simulation_failure(self, run_dir: Path, stage: str) -> dict:
        manifest = _read_json(run_dir / "run_manifest.json")
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["failure_stage"], stage)
        self.assertFalse(manifest["outputs_complete"])
        self.assertFalse(manifest["managed_run_completed"])
        runtime = manifest["llm"]["runtime"]
        self.assertEqual(runtime["provider_calls"], 0)
        self.assertFalse(runtime["network_access"])
        event_types = [event["type"] for event in _read_events(run_dir)]
        self.assertEqual(event_types[-1], "RunFailed")
        self.assertNotIn("RoundStarted", event_types)
        self.assertFalse(CANONICAL_OUTPUTS & {path.name for path in run_dir.iterdir()})
        return manifest

    def _write_config(self, name: str, out_root: Path, **changes) -> Path:
        cfg = Config(
            provider="mock",
            seed=911,
            n_rounds=1,
            news_round=1,
            n_llm_agents=1,
            n_noise_agents=0,
            cache_enabled=False,
            out_dir=str(out_root),
        )
        payload = json.loads(cfg.to_json())
        payload.update(changes)
        path = self.root / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_help_does_not_create_a_run_directory(self):
        out = self.root / "help-must-not-create"
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                run_module.main(["--help", "--out", str(out)])
        self.assertEqual(raised.exception.code, 0)
        self.assertFalse(out.exists())

    def test_invalid_run_id_path_traversal_creates_no_unsafe_directory(self):
        out = self.root / "safe-output"
        escaped = self.root / "escaped-run"
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                run_module.main(
                    [
                        "--provider",
                        "mock",
                        "--out",
                        str(out),
                        "--run-id",
                        "../escaped-run",
                    ]
                )
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("provenance_not_created_reason", stderr.getvalue())
        self.assertFalse(out.exists())
        self.assertFalse(escaped.exists())

    def test_unknown_config_key_creates_safe_config_validation_failure(self):
        out = self.root / "unknown-config"
        config_path = self._write_config(
            "unknown-config.json", out, maintenence_margin=0.99
        )
        stderr = io.StringIO()
        with mock.patch(
            "nmsim.llm.build_llm",
            side_effect=AssertionError("provider construction attempted"),
        ) as build_llm, mock.patch.object(
            run_module,
            "run_sim",
            side_effect=AssertionError("simulation entered"),
        ) as run_sim, mock.patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError("network attempted"),
        ) as network, redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                run_module.main(["--config-json", str(config_path)])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("maintenence_margin", stderr.getvalue())
        build_llm.assert_not_called()
        run_sim.assert_not_called()
        network.assert_not_called()
        run_dir = _single_run_dir(out)
        self._assert_pre_simulation_failure(run_dir, "config_validation")

    def test_provider_setup_failure_is_managed_before_any_round(self):
        out = self.root / "provider-failure"
        private_detail = "PRIVATE_PROVIDER_FAILURE_DETAIL"
        with mock.patch(
            "nmsim.llm.build_llm",
            side_effect=RuntimeError(private_detail),
        ) as build_llm, mock.patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError("network attempted"),
        ) as network, redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, private_detail):
                run_module.main(
                    [
                        "--provider",
                        "mock",
                        "--rounds",
                        "1",
                        "--news-round",
                        "1",
                        "--llm-agents",
                        "1",
                        "--out",
                        str(out),
                        "--run-id",
                        "provider-failure",
                    ]
                )

        build_llm.assert_called_once()
        network.assert_not_called()
        run_dir = out / "runs" / "provider-failure"
        self._assert_pre_simulation_failure(run_dir, "provider_setup")
        self.assertNotIn(
            private_detail,
            (run_dir / "run_manifest.json").read_text(encoding="utf-8"),
        )
        self.assertNotIn(
            private_detail,
            (run_dir / "events.jsonl").read_text(encoding="utf-8"),
        )

    def test_replay_preflight_mismatch_is_offline_and_records_remain_schema_1_2(self):
        out = self.root / "replay-preflight"
        config_path = self._write_config("replay-config.json", out)
        with mock.patch.object(run_module, "_plot", return_value=None), mock.patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError("network attempted"),
        ) as network, redirect_stdout(io.StringIO()):
            run_module.main(
                [
                    "--config-json",
                    str(config_path),
                    "--run-id",
                    "replay-source",
                ]
            )
        network.assert_not_called()

        source = out / "runs" / "replay-source"
        records = [
            json.loads(line)
            for line in (source / "llm_records.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        self.assertTrue(records)
        self.assertEqual(
            {record["recording_schema_version"] for record in records}, {"1.2"}
        )
        self.assertEqual({record["schema_version"] for record in records}, {"1.2"})

        with mock.patch(
            "nmsim.llm.build_llm",
            side_effect=AssertionError("provider construction attempted during replay"),
        ) as build_llm, mock.patch.object(
            run_module,
            "run_sim",
            side_effect=AssertionError("simulation entered before replay preflight"),
        ) as run_sim, mock.patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError("network attempted"),
        ) as network, redirect_stdout(io.StringIO()):
            with self.assertRaises(ReplayMismatchError):
                run_module.main(
                    [
                        "--config-json",
                        str(config_path),
                        "--model",
                        "different-model-identity",
                        "--replay-from",
                        str(source),
                        "--run-id",
                        "replay-mismatch",
                    ]
                )

        build_llm.assert_not_called()
        run_sim.assert_not_called()
        network.assert_not_called()
        self._assert_pre_simulation_failure(
            out / "runs" / "replay-mismatch", "replay_preflight"
        )


class GridDriverCompletionTests(unittest.TestCase):
    def test_driver_private_failure_detail_never_enters_public_summary(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            out = Path(temporary) / "driver-private"
            sentinel = "PRIVATE_CHILD_RATIONALE_SENTINEL"
            managed = ManagedDriverCompletion.create(
                out_root=out,
                command_identity="test.driver-private",
                cell_plans={"cell": 1},
                worker_count=1,
            )
            with managed:
                managed.record_started("cell")
                managed.record_failed(
                    DriverJobResult(
                        cell="cell",
                        tag="cell s1",
                        seed=1,
                        ok=False,
                        source="failed",
                        attempts=1,
                        reason_code="child_run_failed",
                        private_details=({"detail": sentinel},),
                    )
                )
                managed.finish()

            public = (managed.run_dir / "driver_summary.json").read_text(
                encoding="utf-8"
            )
            manifest = (managed.run_dir / "run_manifest.json").read_text(
                encoding="utf-8"
            )
            events = (managed.run_dir / "events.jsonl").read_text(encoding="utf-8")
            private_path = managed.run_dir / "driver_failures.private.jsonl"
            self.assertNotIn(sentinel, public + manifest + events)
            self.assertIn(sentinel, private_path.read_text(encoding="utf-8"))
            self.assertEqual(private_path.stat().st_mode & 0o777, 0o600)

    def test_grid_summary_uses_child_runs_as_honest_n_not_decisions(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            out = Path(temporary) / "grid"

            def successful_child(label, news, social, seed, provider, temp, root):
                return DriverJobResult(
                    cell=label,
                    tag=f"{label} s{seed}",
                    seed=seed,
                    ok=True,
                    source="executed",
                    attempts=1,
                )

            with mock.patch.object(
                grid2x2, "_healthy", return_value=False
            ), mock.patch.object(
                grid2x2, "_run", side_effect=successful_child
            ), redirect_stdout(io.StringIO()):
                grid2x2.main(
                    [
                        "--seeds",
                        "1",
                        "--provider",
                        "mock",
                        "--temp",
                        "0",
                        "--workers",
                        "1",
                        "--out",
                        str(out),
                    ]
                )

            run_dir = _single_run_dir(out)
            summary = _read_json(run_dir / "driver_summary.json")
            manifest = _read_json(run_dir / "run_manifest.json")
            self.assertEqual(summary["planned_runs"], 4)
            self.assertEqual(summary["started_runs"], 4)
            self.assertEqual(summary["completed_runs"], 4)
            self.assertEqual(summary["failed_runs"], 0)
            self.assertEqual(summary["honest_n_runs"], 4)
            for label, _, _ in grid2x2.CELLS:
                cell = summary["cells"][label]
                self.assertEqual(cell["planned_runs"], 1)
                self.assertEqual(cell["started_runs"], 1)
                self.assertEqual(cell["completed_runs"], 1)
                self.assertEqual(cell["failed_runs"], 0)
                self.assertEqual(cell["honest_n_runs"], 1)

            experiment = manifest["experiment_completion"]
            self.assertEqual(experiment["unit"], "runs")
            self.assertEqual(experiment["honest_n_runs"], 4)
            self.assertEqual(
                manifest["completion"]["agent_decisions"]["completed"], 0
            )


if __name__ == "__main__":
    unittest.main()
