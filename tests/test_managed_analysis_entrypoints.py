"""Managed lifecycle coverage for provider-free analysis CLIs."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from experiments import aggregate_grid
from experiments import aggregate_seeds
from experiments import aggregate_sweep
from experiments import calib_n
from experiments import critsweep_analyze
from experiments import lev_analyze


def _manifest(root: Path) -> tuple[Path, dict]:
    paths = list((root / "runs").glob("*/run_manifest.json"))
    if len(paths) != 1:
        raise AssertionError(f"expected one managed run below {root}, got {paths}")
    path = paths[0]
    return path, json.loads(path.read_text(encoding="utf-8"))


def _event_types(path: Path) -> list[str]:
    return [
        json.loads(line)["type"]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class ManagedAnalysisEntrypointTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def _case(self, name: str):
        base = self.root / name
        input_dir = base / "inputs"
        output_dir = base / "outputs"
        baseline_dir = base / "baseline"
        input_dir.mkdir(parents=True)
        output_dir.mkdir(parents=True)
        baseline_dir.mkdir(parents=True)

        if name == "aggregate_grid":
            source = input_dir / "real_on_s1.json"
            source.write_text("{}", encoding="utf-8")
            argv = ["--results", str(input_dir), "--out", str(output_dir)]
            module, target = aggregate_grid, "aggregate"
            outputs = ("grid_summary.json", "envelope_2x2.png")

            def invoke(out_path):
                module.main(argv)

            def writer(_results, out_path):
                for filename in outputs:
                    (Path(out_path) / filename).write_bytes(filename.encode())

            manifest_root = output_dir
            bootstrap_root = output_dir
        elif name == "aggregate_seeds":
            source = input_dir / "g1.0_s1.json"
            source.write_text("{}", encoding="utf-8")
            argv = [
                "--gain", "1.0", "--results", str(input_dir), "--out", str(output_dir)
            ]
            module, target = aggregate_seeds, "aggregate"
            outputs = ("summary_g1.0.json", "envelope_g1.0.png")

            def invoke(out_path):
                module.main(argv)

            def writer(_gain, _results, out_path):
                for filename in outputs:
                    (Path(out_path) / filename).write_bytes(filename.encode())

            manifest_root = output_dir
            bootstrap_root = output_dir
        elif name == "aggregate_sweep":
            source = input_dir / "m0.5_real_on_s1.json"
            source.write_text("{}", encoding="utf-8")
            plot = output_dir / "custom_sweep.png"
            argv = ["--out", str(input_dir), "--plot", str(plot)]
            module, target = aggregate_sweep, "analyze"
            outputs = (plot.name,)

            def invoke(out_path):
                module.main(argv)

            def writer(_input_dir, out_path):
                Path(out_path).write_bytes(b"sweep")

            manifest_root = output_dir
            bootstrap_root = input_dir
        elif name == "calib_n":
            source = input_dir / "m0.5_real_on_s1_r1.json"
            source.write_text("{}", encoding="utf-8")
            argv = ["--out", str(input_dir), "--m", "0.5", "--seed", "1"]
            module, target = calib_n, "calibrate"
            outputs = ("calib_N.txt",)

            def invoke(out_path):
                module.main(argv)

            def writer(_input_dir, _m, _seed, out_path):
                Path(out_path).write_text("10", encoding="utf-8")
                return 10

            manifest_root = input_dir
            bootstrap_root = input_dir
        elif name == "lev_analyze":
            source = input_dir / "m0.7_real_on_s1.json"
            source.write_text("{}", encoding="utf-8")
            plot = output_dir / "leverage.png"
            argv = ["--out", str(input_dir), "--m", "0.7", "--plot", str(plot)]
            module, target = lev_analyze, "analyze"
            outputs = (plot.name,)

            def invoke(out_path):
                module.main(argv)

            def writer(_input_dir, _m, out_path):
                Path(out_path).write_bytes(b"leverage")

            manifest_root = output_dir
            bootstrap_root = input_dir
        elif name == "critsweep_analyze":
            source = input_dir / "m0.7_critL2_s1.json"
            source.write_text("{}", encoding="utf-8")
            (baseline_dir / "m0.7_real_on_s1.json").write_text("{}", encoding="utf-8")
            plot = output_dir / "critical.png"
            argv = [
                "--out", str(input_dir),
                "--baseline-dir", str(baseline_dir),
                "--m", "0.7", "--levels", "2.0", "--plot", str(plot),
            ]
            module, target = critsweep_analyze, "analyze"
            outputs = (plot.name,)

            def invoke(out_path):
                module.main(argv)

            def writer(_input_dir, _baseline_dir, _m, _levels, out_path):
                Path(out_path).write_bytes(b"critical")

            manifest_root = output_dir
            bootstrap_root = input_dir
        else:  # pragma: no cover - test construction guard
            raise AssertionError(name)

        return {
            "module": module,
            "target": target,
            "invoke": invoke,
            "writer": writer,
            "outputs": outputs,
            "manifest_root": manifest_root,
            "bootstrap_root": bootstrap_root,
            "source": source,
            "argv": argv,
        }

    def test_all_persistent_analysis_entrypoints_are_managed_and_provider_free(self):
        for name in (
            "aggregate_grid",
            "aggregate_seeds",
            "aggregate_sweep",
            "calib_n",
            "lev_analyze",
            "critsweep_analyze",
        ):
            with self.subTest(entrypoint=name):
                case = self._case(name)
                with mock.patch.object(
                    case["module"], case["target"], side_effect=case["writer"]
                ), mock.patch(
                    "nmsim.llm.build_llm",
                    side_effect=AssertionError("analysis must not construct a Provider"),
                ), redirect_stdout(io.StringIO()):
                    case["invoke"](case["manifest_root"])

                manifest_path, manifest = _manifest(case["manifest_root"])
                self.assertEqual(manifest["status"], "finished")
                self.assertEqual(manifest["managed_context"]["run_kind"], "analysis")
                self.assertTrue(manifest["managed_run_completed"])
                self.assertTrue(manifest["outputs_complete"])
                self.assertEqual(
                    manifest["analysis_completion"],
                    {
                        "schema_version": "1.0",
                        "unit": "analysis_attempts",
                        "planned": 1,
                        "started": 1,
                        "completed": 1,
                        "failed": 0,
                        "input_files": len(manifest["inputs"]),
                    },
                )
                self.assertEqual(manifest["experiment_completion"]["honest_n_runs"], 0)
                self.assertEqual(manifest["honest_n"], 0)
                self.assertEqual(manifest["honest_n_unit"], "agent_decisions")
                self.assertFalse(manifest["llm"]["runtime"]["network_access"])
                self.assertEqual(manifest["llm"]["runtime"]["provider_calls"], 0)
                self.assertIn(
                    hashlib.sha256(case["source"].read_bytes()).hexdigest(),
                    {item["sha256"] for item in manifest["inputs"]},
                )

                run_dir = manifest_path.parent
                for filename in case["outputs"]:
                    self.assertTrue((run_dir / filename).is_file(), filename)
                    self.assertTrue((case["manifest_root"] / filename).is_symlink(), filename)
                events = _event_types(run_dir / "events.jsonl")
                self.assertEqual(events.count("RunFinished"), 1)
                self.assertNotIn("RunFailed", events)

    def test_analysis_failure_is_finalized_without_flat_success_projection(self):
        for name in (
            "aggregate_grid",
            "aggregate_seeds",
            "aggregate_sweep",
            "calib_n",
            "lev_analyze",
            "critsweep_analyze",
        ):
            with self.subTest(entrypoint=name):
                case = self._case(name)
                error = RuntimeError("sensitive-analysis-detail")
                with mock.patch.object(
                    case["module"], case["target"], side_effect=error
                ), mock.patch(
                    "nmsim.llm.build_llm",
                    side_effect=AssertionError("analysis must not construct a Provider"),
                ), redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
                    case["invoke"](case["manifest_root"])

                manifest_path, manifest = _manifest(case["manifest_root"])
                self.assertEqual(manifest["status"], "failed")
                self.assertEqual(manifest["failure_stage"], "result_export")
                self.assertFalse(manifest["managed_run_completed"])
                self.assertFalse(manifest["outputs_complete"])
                self.assertEqual(manifest["analysis_completion"]["completed"], 0)
                self.assertEqual(manifest["analysis_completion"]["failed"], 1)
                self.assertFalse(manifest["llm"]["runtime"]["network_access"])
                self.assertEqual(manifest["llm"]["runtime"]["provider_calls"], 0)
                for filename in case["outputs"]:
                    self.assertFalse((case["manifest_root"] / filename).exists(), filename)
                public_events = manifest_path.parent / "events.jsonl"
                self.assertNotIn(
                    "sensitive-analysis-detail",
                    public_events.read_text(encoding="utf-8"),
                )
                events = _event_types(public_events)
                self.assertEqual(events.count("RunFailed"), 1)
                self.assertNotIn("RunFinished", events)

    def test_help_exits_without_creating_a_managed_attempt(self):
        for name in (
            "aggregate_grid",
            "aggregate_seeds",
            "aggregate_sweep",
            "calib_n",
            "lev_analyze",
            "critsweep_analyze",
        ):
            with self.subTest(entrypoint=name):
                case = self._case(name)
                with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as caught:
                    case["module"].main([*case["argv"], "--help"])
                self.assertEqual(caught.exception.code, 0)
                self.assertFalse((case["bootstrap_root"] / "runs").exists())
                self.assertFalse((case["manifest_root"] / "runs").exists())

    def test_unknown_argument_is_a_managed_config_validation_failure(self):
        for name in (
            "aggregate_grid",
            "aggregate_seeds",
            "aggregate_sweep",
            "calib_n",
            "lev_analyze",
            "critsweep_analyze",
        ):
            with self.subTest(entrypoint=name):
                case = self._case(name)
                stderr = io.StringIO()
                with mock.patch(
                    "nmsim.llm.build_llm",
                    side_effect=AssertionError("validation must not construct a Provider"),
                ), redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
                    case["module"].main(
                        [*case["argv"], "--definitely-unknown-analysis-option"]
                    )
                self.assertEqual(caught.exception.code, 2)
                self.assertIn("unrecognized arguments", stderr.getvalue())
                manifest_path, manifest = _manifest(case["bootstrap_root"])
                self.assertEqual(manifest["status"], "failed")
                self.assertEqual(manifest["failure_stage"], "config_validation")
                self.assertFalse(manifest["llm"]["runtime"]["network_access"])
                self.assertEqual(manifest["llm"]["runtime"]["provider_calls"], 0)
                events = _event_types(manifest_path.parent / "events.jsonl")
                self.assertEqual(events.count("RunFailed"), 1)
                self.assertNotIn("RoundStarted", events)
                self.assertNotIn("RunFinished", events)

    def test_missing_required_and_type_errors_are_managed_after_bootstrap(self):
        cases = (
            (
                aggregate_seeds,
                ["--out", str(self.root / "missing-gain")],
                self.root / "missing-gain",
            ),
            (
                critsweep_analyze,
                ["--out", str(self.root / "missing-levels")],
                self.root / "missing-levels",
            ),
            (
                calib_n,
                ["--out", str(self.root / "bad-type"), "--m", "not-a-float"],
                self.root / "bad-type",
            ),
        )
        for module, argv, root in cases:
            with self.subTest(entrypoint=module.__name__):
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                    module.main(argv)
                self.assertEqual(caught.exception.code, 2)
                _path, manifest = _manifest(root)
                self.assertEqual(manifest["status"], "failed")
                self.assertEqual(manifest["failure_stage"], "config_validation")
                self.assertEqual(manifest["completion"]["rounds"]["started"], 0)
                self.assertEqual(manifest["completion"]["provider_calls"]["attempted"], 0)


if __name__ == "__main__":
    unittest.main()
