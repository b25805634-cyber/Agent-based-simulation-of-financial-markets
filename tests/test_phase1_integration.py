"""Phase-1 provenance, event-stream, and record/replay integration tests.

These tests intentionally use only the standard-library ``unittest`` runner.
They exercise the managed entry points with small configurations and never
contact an external provider.
"""
from __future__ import annotations

from contextlib import redirect_stdout
from dataclasses import asdict
import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from nmsim.config import Config
from nmsim.config_contract import build_effective_config_contract
from nmsim.events import EventLogger
from nmsim.llm import CostTracker
from nmsim.recording import RecordingLLM, ReplayMismatchError
from nmsim.sim import run_sim


run_module = importlib.import_module("nmsim.run")
REPO_ROOT = Path(__file__).resolve().parents[1]


def _read_jsonl(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def _managed_cfg(out_dir: Path) -> Config:
    return Config(
        provider="mock",
        seed=31415,
        n_rounds=3,
        news_round=2,
        n_llm_agents=3,
        n_noise_agents=2,
        cache_enabled=False,
        out_dir=str(out_dir),
    )


def _metric_snapshot(result) -> dict:
    return asdict(result.metrics)


def _portfolio_snapshot(result) -> list[tuple]:
    return [
        (
            agent.name,
            agent.cash,
            agent.shares,
            agent.is_leveraged,
            agent.lev_liquidated,
            agent.lev_pnl,
        )
        for agent in result.agents
    ]


def _event_core(events: list[dict]) -> list[dict]:
    """Remove only run identity and wall-clock time from public events."""

    core = []
    for event in events:
        item = dict(event)
        item.pop("run_id", None)
        item.pop("timestamp", None)
        core.append(item)
    return core


def _scientific_events(events: list[dict]) -> list[dict]:
    event_types = {
        "AgentDecisionParsed",
        "OrderSubmitted",
        "OrdersAggregated",
        "PriceCleared",
        "FillApplied",
        "MetricsRecorded",
    }
    return [
        {
            "type": event["type"],
            "round": event["round"],
            "agent_id": event["agent_id"],
            "data": event["data"],
        }
        for event in events
        if event["type"] in event_types
    ]


class _ScriptedRealProvider:
    """In-process provider double that takes the real-prompt code path."""

    kind = "openai"
    model = "scripted-no-network"
    temperature = 0.0
    max_tokens = 128

    def __init__(self) -> None:
        self.calls = 0
        self.response = json.dumps(
            {
                "action": "buy",
                "quantity": 2,
                "limit_price": 101.0,
                "sentiment": 0.8,
                "public_take": "PUBLIC_SIGNAL_FOR_NEIGHBORS",
                "reasoning": "PRIVATE_REASONING_MUST_STAY_PRIVATE",
            }
        )

    def complete(self, system: str, user: str) -> str:
        self.calls += 1
        return self.response

    def complete_batch(self, prompts) -> list[str]:
        prompts = list(prompts)
        self.calls += len(prompts)
        return [self.response for _ in prompts]


class Phase1IntegrationTests(unittest.TestCase):
    def _managed_run(self, cfg: Config, **kwargs):
        with mock.patch.object(run_module, "_plot", return_value=None), redirect_stdout(
            io.StringIO()
        ):
            return run_module.run(cfg, **kwargs)

    def test_two_mock_runs_have_identical_core_events_and_results(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary) / "managed"
            first = self._managed_run(_managed_cfg(out))
            second = self._managed_run(_managed_cfg(out))

            first_events = _read_jsonl(Path(first.run_dir) / "events.jsonl")
            second_events = _read_jsonl(Path(second.run_dir) / "events.jsonl")
            self.assertEqual(_event_core(first_events), _event_core(second_events))

            self.assertEqual(first.history, second.history)
            self.assertEqual(first.rows, second.rows)
            self.assertEqual(first.traces, second.traces)
            self.assertEqual(_metric_snapshot(first), _metric_snapshot(second))
            self.assertEqual(_portfolio_snapshot(first), _portfolio_snapshot(second))

    def test_record_then_replay_preserves_scientific_outputs_without_provider(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary) / "record-replay"
            recorded = self._managed_run(_managed_cfg(out))

            # Replay must not even construct the normal provider/cache stack.
            with mock.patch.object(
                run_module,
                "build_llm",
                side_effect=AssertionError("provider construction attempted during replay"),
            ):
                replayed = self._managed_run(
                    _managed_cfg(out), replay_from=recorded.run_dir
                )

            self.assertEqual(recorded.history, replayed.history)
            self.assertEqual(recorded.rows, replayed.rows)
            self.assertEqual(recorded.traces, replayed.traces)
            self.assertEqual(_metric_snapshot(recorded), _metric_snapshot(replayed))
            self.assertEqual(_portfolio_snapshot(recorded), _portfolio_snapshot(replayed))

            record_events = _read_jsonl(Path(recorded.run_dir) / "events.jsonl")
            replay_events = _read_jsonl(Path(replayed.run_dir) / "events.jsonl")
            self.assertEqual(
                _scientific_events(record_events), _scientific_events(replay_events)
            )

            replay_manifest = json.loads(
                (Path(replayed.run_dir) / "run_manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(replay_manifest["llm"]["mode"], "replay")
            self.assertFalse(replay_manifest["llm"]["runtime"]["network_access"])
            self.assertEqual(replay_manifest["llm"]["runtime"]["provider_calls"], 0)

    def test_high_level_replay_mismatch_writes_failed_manifest_and_event(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary) / "mismatch"
            recorded = self._managed_run(_managed_cfg(out))
            before = set((out / "runs").iterdir())

            changed = _managed_cfg(out)
            changed.model = "different-model-identity"
            with self.assertRaises(ReplayMismatchError):
                self._managed_run(changed, replay_from=recorded.run_dir)

            created = set((out / "runs").iterdir()) - before
            self.assertEqual(len(created), 1)
            failed_dir = created.pop()
            manifest = json.loads(
                (failed_dir / "run_manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "failed")
            self.assertIsNotNone(manifest["ended_at"])
            self.assertIn("ReplayMismatchError", manifest["failure_reason"])

            events = _read_jsonl(failed_dir / "events.jsonl")
            self.assertEqual(events[-1]["type"], "RunFailed")
            self.assertIn("ReplayMismatchError", events[-1]["data"]["failure_reason"])

    def test_private_reasoning_never_enters_neighbor_prompt_or_public_events(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "private-boundary"
            logger = EventLogger("private-boundary", run_dir=run_dir)
            provider = _ScriptedRealProvider()
            cfg = Config(
                provider="openai",
                seed=9,
                n_rounds=2,
                news_round=1,
                n_llm_agents=2,
                n_noise_agents=0,
                social_enabled=True,
                social_mode="network",
                topology="fully_connected",
                social_weight=1.0,
                cache_enabled=False,
                out_dir=str(run_dir),
            )
            recorded_llm = RecordingLLM(
                provider,
                run_dir,
                model_config={
                    "provider": provider.kind,
                    "model": provider.model,
                    "temperature": provider.temperature,
                    "max_tokens": provider.max_tokens,
                    "cache_enabled": False,
                },
                event_logger=logger,
                compatibility_metadata=build_effective_config_contract(
                    cfg,
                    base_dir=Path(temporary),
                    execution_context={"test_boundary": "private-reasoning"},
                ),
            )

            run_sim(
                cfg,
                recorded_llm,
                CostTracker(),
                event_logger=logger,
                run_id="private-boundary",
            )

            records = _read_jsonl(run_dir / "llm_records.jsonl")
            round_two_requests = [
                record["request"]["user"]
                for record in records
                if record["round"] == 2
            ]
            self.assertEqual(len(round_two_requests), 2)
            for user_prompt in round_two_requests:
                self.assertIn("PUBLIC_SIGNAL_FOR_NEIGHBORS", user_prompt)
                self.assertNotIn("PRIVATE_REASONING_MUST_STAY_PRIVATE", user_prompt)

            public_text = (run_dir / "events.jsonl").read_text(encoding="utf-8")
            self.assertIn("PUBLIC_SIGNAL_FOR_NEIGHBORS", public_text)
            self.assertNotIn("PRIVATE_REASONING_MUST_STAY_PRIVATE", public_text)

            round_two_observations = [
                event
                for event in _read_jsonl(run_dir / "events.jsonl")
                if event["type"] == "AgentObservationCreated" and event["round"] == 2
            ]
            self.assertEqual(len(round_two_observations), 2)
            for observation in round_two_observations:
                serialized_feed = json.dumps(
                    observation["data"]["social_feed"], ensure_ascii=False
                )
                self.assertIn("PUBLIC_SIGNAL_FOR_NEIGHBORS", serialized_feed)
                self.assertNotIn("PRIVATE_REASONING_MUST_STAY_PRIVATE", serialized_feed)

    def test_run_seed_subprocess_keeps_legacy_json_and_writes_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary) / "experiment"
            command = [
                sys.executable,
                "-m",
                "experiments.run_seed",
                "--seed",
                "2718",
                "--provider",
                "mock",
                "--out",
                str(out),
                "--rounds",
                "2",
                "--news-round",
                "1",
                "--label",
                "compat",
            ]
            environment = os.environ.copy()
            environment["LLM_PROVIDER"] = "mock"
            environment["PYTHONPATH"] = str(REPO_ROOT)
            process = subprocess.run(
                command,
                cwd=str(REPO_ROOT),
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(
                process.returncode,
                0,
                msg="command failed:\nstdout:\n{}\nstderr:\n{}".format(
                    process.stdout, process.stderr
                ),
            )

            legacy_path = out / "compat_s2718.json"
            self.assertTrue(legacy_path.is_file())
            legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
            expected_legacy_fields = {
                "label",
                "seed",
                "condition",
                "n_rounds",
                "news_round",
                "shock_idx",
                "metrics",
                "norm_price_path",
                "norm_log_path",
                "health",
                "model",
                "cost",
                "orders",
                "liquidations",
                "rep",
            }
            self.assertTrue(expected_legacy_fields.issubset(legacy))

            manifests = list((out / "runs").glob("*/run_manifest.json"))
            self.assertEqual(len(manifests), 1)
            manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "finished")
            self.assertEqual(manifest["run_id"], legacy["run_id"])
            self.assertGreater(manifest["samples"]["honest_n"], 0)
            self.assertTrue((manifests[0].parent / "experiment_result.json").is_file())


if __name__ == "__main__":
    unittest.main()
