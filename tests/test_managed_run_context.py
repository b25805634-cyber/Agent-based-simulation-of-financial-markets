"""Lifecycle and unitized-completion tests for managed run contexts.

All provider-facing integration cases use MockLLM or strict offline replay.
No test in this module may contact a network provider.
"""
from __future__ import annotations

from contextlib import redirect_stdout
from dataclasses import asdict
import importlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from nmsim.config import Config
from nmsim.llm import CostTracker, MockLLM
from nmsim.run_context import ACTIVE, FAILED, FINISHED, ManagedRunContext, NullRunContext
from nmsim.sim import run_sim


run_module = importlib.import_module("nmsim.run")
REPO_ROOT = Path(__file__).resolve().parents[1]


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _events(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _event_count(context: ManagedRunContext, event_type: str) -> int:
    return sum(
        event["type"] == event_type
        for event in _events(Path(context.public_events_path))
    )


def _cfg(out_dir: Path, *, rounds: int = 4, cache: bool = False) -> Config:
    return Config(
        provider="mock",
        seed=707,
        n_rounds=rounds,
        news_round=min(2, rounds),
        n_llm_agents=6,
        n_noise_agents=0,
        cache_enabled=cache,
        out_dir=str(out_dir),
    )


class ManagedRunContextLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)

    def _context(self, name: str, **kwargs) -> ManagedRunContext:
        return ManagedRunContext.create(
            _cfg(self.root / name, rounds=2),
            run_id=name,
            repo_root=REPO_ROOT,
            command_identity="test:managed-run-context",
            **kwargs,
        )

    def test_success_and_repeated_finish_write_exactly_one_finished_event(self):
        context = self._context("finish-idempotent")

        first = context.finish()
        second = context.finish()
        context.close()

        self.assertEqual(first, second)
        self.assertEqual(context.state, FINISHED)
        self.assertEqual(_event_count(context, "RunFinished"), 1)
        self.assertEqual(_event_count(context, "RunFailed"), 0)
        manifest = _read_json(first)
        self.assertEqual(manifest["status"], "finished")
        self.assertTrue(manifest["managed_run_completed"])
        self.assertTrue(manifest["outputs_complete"])

    def test_repeated_fail_and_cross_terminal_transitions_are_noops(self):
        failed = self._context("fail-idempotent")
        failed.fail(RuntimeError("first failure"), failure_stage="simulation")
        snapshot = Path(failed.manifest_path).read_bytes()
        failed.fail(RuntimeError("second failure"), failure_stage="result_export")
        failed.finish()

        self.assertEqual(failed.state, FAILED)
        self.assertEqual(_event_count(failed, "RunFailed"), 1)
        self.assertEqual(_event_count(failed, "RunFinished"), 0)
        self.assertEqual(Path(failed.manifest_path).read_bytes(), snapshot)

        finished = self._context("finish-wins")
        finished.finish()
        snapshot = Path(finished.manifest_path).read_bytes()
        finished.fail(RuntimeError("too late"), failure_stage="simulation")
        self.assertEqual(finished.state, FINISHED)
        self.assertEqual(_event_count(finished, "RunFinished"), 1)
        self.assertEqual(_event_count(finished, "RunFailed"), 0)
        self.assertEqual(Path(finished.manifest_path).read_bytes(), snapshot)

    def test_context_exit_does_not_duplicate_explicit_finish(self):
        context = self._context("context-exit-finished")
        with context:
            context.finish()

        self.assertEqual(context.state, FINISHED)
        self.assertEqual(_event_count(context, "RunFinished"), 1)
        self.assertEqual(_event_count(context, "RunFailed"), 0)

    def test_context_exit_without_finish_fails_once(self):
        context = self._context("context-exit-unfinished")
        with context:
            self.assertEqual(context.state, ACTIVE)

        manifest = _read_json(context.manifest_path)
        self.assertEqual(context.state, FAILED)
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["failure_stage"], "finalization")
        self.assertEqual(_event_count(context, "RunFailed"), 1)

    def test_keyboard_interrupt_is_persisted_with_partial_completion(self):
        context = self._context("keyboard-interrupt")

        with self.assertRaises(KeyboardInterrupt):
            with context:
                context.set_stage("simulation")
                context.manifest["completion"]["simulation_runs"]["started"] = 1
                context.events.emit("RoundStarted", round_i=1, data={"price": 100.0})
                raise KeyboardInterrupt()

        manifest = _read_json(context.manifest_path)
        completion = manifest["completion"]
        self.assertEqual(context.state, FAILED)
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["failure_stage"], "simulation")
        self.assertEqual(manifest["failure_type"], "keyboard_interrupt")
        self.assertEqual(completion["simulation_runs"]["failed"], 1)
        self.assertEqual(completion["rounds"]["started"], 1)
        self.assertEqual(completion["rounds"]["failed"], 1)
        self.assertFalse(manifest["managed_run_completed"])
        self.assertFalse(manifest["outputs_complete"])
        self.assertEqual(_event_count(context, "RunFailed"), 1)

    def test_generic_private_error_does_not_enter_public_manifest_or_events(self):
        secret = "secret-api-key-for-managed-context-test"
        private_rationale = "PRIVATE_RATIONALE_MUST_NOT_BE_PUBLIC"
        cfg = _cfg(self.root / "private-error", rounds=1)
        cfg.openai_api_key = secret
        context = ManagedRunContext.create(
            cfg,
            run_id="private-error",
            repo_root=REPO_ROOT,
            command_identity="test:private-error",
        )

        context.fail(
            RuntimeError("provider failed using {} {}".format(secret, private_rationale)),
            failure_stage="provider_setup",
        )

        manifest_text = Path(context.manifest_path).read_text(encoding="utf-8")
        public_text = Path(context.public_events_path).read_text(encoding="utf-8")
        private_text = Path(context.private_events_path).read_text(encoding="utf-8")
        for public in (manifest_text, public_text):
            self.assertNotIn(secret, public)
            self.assertNotIn(private_rationale, public)
        self.assertNotIn(secret, private_text)
        self.assertIn(private_rationale, private_text)
        self.assertIn("<redacted>", private_text)


class ManagedRunContextIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)

    def _run(self, cfg: Config, **kwargs):
        with mock.patch.object(run_module, "_plot", return_value=None), redirect_stdout(
            io.StringIO()
        ):
            return run_module.run(cfg, **kwargs)

    def test_null_context_has_no_side_effects_and_preserves_trajectory(self):
        output = self.root / "must-not-exist"
        cfg_direct = _cfg(output, rounds=4)
        cfg_null = _cfg(output, rounds=4)

        direct = run_sim(
            cfg_direct,
            MockLLM(seed=cfg_direct.seed),
            CostTracker(),
            event_logger=None,
        )
        null = NullRunContext()
        with null:
            through_null = null.execute_simulation(
                run_sim,
                cfg_null,
                MockLLM(seed=cfg_null.seed),
                CostTracker(),
                event_logger=null.observer,
            )
            null.finish()

        self.assertFalse(null.managed)
        self.assertIsNone(null.run_dir)
        self.assertFalse(output.exists())
        self.assertEqual(direct.history, through_null.history)
        self.assertEqual(direct.rows, through_null.rows)
        self.assertEqual(direct.traces, through_null.traces)
        self.assertEqual(asdict(direct.metrics), asdict(through_null.metrics))
        self.assertEqual(
            [(agent.name, agent.cash, agent.shares) for agent in direct.agents],
            [(agent.name, agent.cash, agent.shares) for agent in through_null.agents],
        )

    def test_four_round_mock_has_unitized_completion_and_provider_source(self):
        result = self._run(_cfg(self.root / "mock-four", rounds=4))
        manifest = _read_json(Path(result.run_dir) / "run_manifest.json")
        completion = manifest["completion"]

        self.assertEqual(completion["simulation_runs"]["completed"], 1)
        self.assertEqual(completion["rounds"]["completed"], 4)
        self.assertEqual(completion["agent_decisions"]["completed"], 24)
        self.assertEqual(completion["llm_logical_requests"]["completed"], 24)
        self.assertEqual(completion["response_sources"]["provider"], 24)
        self.assertEqual(completion["response_sources"]["cache"], 0)
        self.assertEqual(completion["response_sources"]["replay"], 0)
        self.assertEqual(completion["provider_calls"]["attempted"], 24)
        self.assertEqual(completion["provider_calls"]["succeeded"], 24)
        self.assertEqual(completion["provider_calls"]["failed"], 0)
        self.assertEqual(manifest["honest_n"], 24)
        self.assertEqual(manifest["honest_n_unit"], "agent_decisions")
        self.assertTrue(manifest["honest_n_deprecated"])
        self.assertFalse(manifest["llm"]["runtime"]["network_access"])

    def test_strict_replay_counts_replay_responses_and_zero_provider_calls(self):
        recorded = self._run(_cfg(self.root / "record-replay", rounds=4))

        with mock.patch(
            "nmsim.llm.build_llm",
            side_effect=AssertionError("provider construction attempted during replay"),
        ):
            replayed = self._run(
                _cfg(self.root / "record-replay", rounds=4),
                replay_from=recorded.run_dir,
            )

        manifest = _read_json(Path(replayed.run_dir) / "run_manifest.json")
        completion = manifest["completion"]
        self.assertEqual(recorded.history, replayed.history)
        self.assertEqual(recorded.rows, replayed.rows)
        self.assertEqual(recorded.traces, replayed.traces)
        self.assertEqual(completion["agent_decisions"]["completed"], 24)
        self.assertEqual(completion["llm_logical_requests"]["completed"], 24)
        self.assertEqual(completion["response_sources"]["replay"], 24)
        self.assertEqual(completion["response_sources"]["provider"], 0)
        self.assertEqual(completion["response_sources"]["cache"], 0)
        self.assertEqual(completion["provider_calls"]["attempted"], 0)
        self.assertEqual(completion["provider_calls"]["succeeded"], 0)
        self.assertEqual(completion["provider_calls"]["failed"], 0)
        self.assertFalse(manifest["llm"]["runtime"]["network_access"])

    def test_cache_hit_is_a_response_source_but_not_a_second_provider_call(self):
        context = ManagedRunContext.create(
            _cfg(self.root / "cache-accounting", rounds=1, cache=True),
            run_id="cache-accounting",
            repo_root=REPO_ROOT,
            command_identity="test:cache-accounting",
        )
        with context:
            llm, tracker = context.prepare_llm()
            prompt = (
                "PERSONA: cache-test\nMOCK_PARAMS: {}",
                "LAST_PRICE: 100\nFUNDAMENTAL: 100\nTREND: 0\nNEWS: no fresh news",
            )

            def repeated_calls():
                llm.set_batch_context(
                    1, [{"agent_id": "cache-agent", "persona_id": "cache-test"}]
                )
                first = llm.complete_batch([prompt])
                llm.set_batch_context(
                    2, [{"agent_id": "cache-agent", "persona_id": "cache-test"}]
                )
                second = llm.complete_batch([prompt])
                return first, second

            first, second = context.execute_simulation(repeated_calls)
            self.assertEqual(first, second)
            context.sync_llm_accounting(llm, tracker)
            context.finish()

        manifest = _read_json(context.manifest_path)
        completion = manifest["completion"]
        self.assertEqual(completion["llm_logical_requests"]["completed"], 2)
        self.assertEqual(completion["response_sources"]["provider"], 1)
        self.assertEqual(completion["response_sources"]["cache"], 1)
        self.assertEqual(completion["provider_calls"]["attempted"], 1)
        self.assertEqual(completion["provider_calls"]["succeeded"], 1)
        self.assertEqual(completion["provider_calls"]["failed"], 0)

    def test_parser_fallback_is_completed_decision_and_private_rationale_stays_private(self):
        context = ManagedRunContext.create(
            _cfg(self.root / "parser-fallback", rounds=1),
            run_id="parser-fallback",
            repo_root=REPO_ROOT,
            command_identity="test:parser-fallback",
        )
        private_rationale = "PRIVATE_PARSE_FAILURE_RATIONALE"
        context.events.emit(
            "LLMRequestRecorded",
            round_i=1,
            agent_id="agent-1",
            data={"sequence": 1},
        )
        context.events.emit(
            "LLMResponseRecorded",
            round_i=1,
            agent_id="agent-1",
            data={"sequence": 1, "source": "record"},
            private_data={"raw_response": "not json"},
        )
        context.events.emit(
            "AgentDecisionParsed",
            round_i=1,
            agent_id="agent-1",
            data={
                "action": "hold",
                "quantity": 0,
                "public_take": "",
                "parse_status": "error",
            },
            private_data={"private_rationale": private_rationale},
        )
        context.finish()

        manifest = _read_json(context.manifest_path)
        completion = manifest["completion"]
        self.assertEqual(completion["agent_decisions"]["attempted"], 1)
        self.assertEqual(completion["agent_decisions"]["completed"], 1)
        self.assertEqual(completion["agent_decisions"]["failed"], 0)
        self.assertEqual(completion["parsing"]["attempted"], 1)
        self.assertEqual(completion["parsing"]["succeeded"], 0)
        self.assertEqual(completion["parsing"]["failed"], 1)
        self.assertEqual(completion["parsing"]["fallbacks"], 1)
        self.assertEqual(manifest["honest_n"], 1)
        public_text = Path(context.public_events_path).read_text(encoding="utf-8")
        private_text = Path(context.private_events_path).read_text(encoding="utf-8")
        self.assertNotIn(private_rationale, public_text)
        self.assertIn(private_rationale, private_text)

    def test_result_export_error_is_failed_after_completed_simulation(self):
        out = self.root / "export-failure"
        with mock.patch.object(
            run_module, "_write_price_csv", side_effect=RuntimeError("export exploded")
        ), mock.patch.object(run_module, "_plot", return_value=None), redirect_stdout(
            io.StringIO()
        ):
            with self.assertRaisesRegex(RuntimeError, "export exploded"):
                run_module.run(_cfg(out, rounds=2), run_id="export-failure")

        manifests = list((out / "runs").glob("*/run_manifest.json"))
        self.assertEqual(len(manifests), 1)
        manifest = _read_json(manifests[0])
        run_dir = manifests[0].parent
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["failure_stage"], "result_export")
        self.assertTrue(manifest["simulation_computation_completed"])
        self.assertEqual(manifest["completion"]["simulation_runs"]["completed"], 1)
        self.assertFalse(manifest["managed_run_completed"])
        self.assertFalse(manifest["outputs_complete"])
        self.assertFalse((out / "latest").exists())
        self.assertFalse((out / "price_path.csv").exists())
        self.assertEqual(
            sum(event["type"] == "RunFailed" for event in _events(run_dir / "events.jsonl")),
            1,
        )

    def test_simulation_failure_preserves_completed_round_and_decision_units(self):
        cfg = _cfg(self.root / "simulation-failure", rounds=4)
        context = ManagedRunContext.create(
            cfg,
            run_id="simulation-failure",
            repo_root=REPO_ROOT,
            command_identity="test:simulation-failure",
        )

        with self.assertRaisesRegex(RuntimeError, "round-three-failure"):
            with context:
                llm, tracker = context.prepare_llm()
                complete_batch = llm.complete_batch
                calls = {"count": 0}

                def fail_on_third_round(prompts):
                    calls["count"] += 1
                    if calls["count"] == 3:
                        raise RuntimeError("round-three-failure")
                    return complete_batch(prompts)

                llm.complete_batch = fail_on_third_round
                context.execute_simulation(
                    run_sim,
                    cfg,
                    llm,
                    tracker,
                    event_logger=context.observer,
                    run_id=context.run_id,
                )

        manifest = _read_json(context.manifest_path)
        completion = manifest["completion"]
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["failure_stage"], "simulation")
        self.assertEqual(completion["simulation_runs"]["started"], 1)
        self.assertEqual(completion["simulation_runs"]["completed"], 0)
        self.assertEqual(completion["simulation_runs"]["failed"], 1)
        self.assertEqual(completion["rounds"]["started"], 3)
        self.assertEqual(completion["rounds"]["completed"], 2)
        self.assertEqual(completion["rounds"]["failed"], 1)
        self.assertEqual(completion["rounds"]["skipped"], 1)
        self.assertEqual(completion["agent_decisions"]["completed"], 12)
        self.assertEqual(completion["llm_logical_requests"]["completed"], 12)


if __name__ == "__main__":
    unittest.main()
