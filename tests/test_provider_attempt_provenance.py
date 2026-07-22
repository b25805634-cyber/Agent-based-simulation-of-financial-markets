"""Focused fake-client tests for application Provider-attempt provenance.

No test in this module constructs a real SDK client or contacts a network.
"""
from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from nmsim.config import Config
from nmsim.config_contract import build_effective_config_contract
from nmsim.fingerprint import scientific_compatibility_metadata
from nmsim.llm import AnthropicLLM, CachingLLM, CostTracker, OpenAILLM
from nmsim.provider_attempts import (
    ProviderAttemptContext,
    prompt_hash,
    sha256_text,
)
from nmsim.recording import RecordingLLM, ReplayLLM
from nmsim.run_context import ManagedRunContext


VALID = (
    '{"side":"hold","quantity":0,"limit_price":100,'
    '"sentiment":0.0,"public_take":"","rationale":"ok"}'
)
INVALID = "provider returned prose instead of JSON"
REMINDER = "\n\nREMINDER: reply with ONLY the JSON object, no prose."
REPO_ROOT = Path(__file__).resolve().parents[1]


class ObserverWriteError(RuntimeError):
    pass


class CollectingObserver:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.items = []

    def observe_provider_attempt(self, context, observation) -> None:
        if self.fail:
            raise ObserverWriteError("attempt observer write failed")
        self.items.append((context, observation))


class RecordingEventCollector(CollectingObserver):
    run_id = "provider-attempt-test"

    def __init__(self) -> None:
        super().__init__()
        self.events = []

    def emit(self, event_type, **kwargs):
        self.events.append((event_type, kwargs))
        return "evt-{:08d}".format(len(self.events))


class ScriptedAsyncOpenAICompletions:
    def __init__(self, script) -> None:
        self.script = list(script)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        value = self.script.pop(0)
        if isinstance(value, BaseException):
            raise value
        text, reported_model = (
            value if isinstance(value, tuple) else (value, "HiggsAI")
        )
        return SimpleNamespace(
            model=reported_model,
            choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
            usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7),
        )


class ScriptedSyncOpenAICompletions:
    def __init__(self, script) -> None:
        self.script = list(script)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        value = self.script.pop(0)
        if isinstance(value, BaseException):
            raise value
        text, reported_model = (
            value if isinstance(value, tuple) else (value, "HiggsAI")
        )
        return SimpleNamespace(
            model=reported_model,
            choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
            usage=SimpleNamespace(prompt_tokens=13, completion_tokens=5),
        )


class ScriptedAsyncAnthropicMessages:
    def __init__(self, script) -> None:
        self.script = list(script)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        value = self.script.pop(0)
        if isinstance(value, BaseException):
            raise value
        text, reported_model = (
            value if isinstance(value, tuple) else (value, "claude-reported")
        )
        return SimpleNamespace(
            model=reported_model,
            content=[SimpleNamespace(text=text)],
            usage=SimpleNamespace(input_tokens=17, output_tokens=9),
        )


class ScriptedSyncAnthropicMessages:
    def __init__(self, script) -> None:
        self.script = list(script)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        value = self.script.pop(0)
        if isinstance(value, BaseException):
            raise value
        return SimpleNamespace(
            model="claude-sync-reported",
            content=[SimpleNamespace(text=value)],
            usage=SimpleNamespace(input_tokens=19, output_tokens=3),
        )


def _openai(*, async_script=(), sync_script=()):
    llm = object.__new__(OpenAILLM)
    llm._init_provider_attempt_contexts()
    llm.model = "MiniMax-M2.7"
    llm.temperature = 0.3
    llm.max_tokens = 256
    llm.tracker = CostTracker()
    async_completions = ScriptedAsyncOpenAICompletions(async_script)
    sync_completions = ScriptedSyncOpenAICompletions(sync_script)
    llm._async = SimpleNamespace(
        chat=SimpleNamespace(completions=async_completions)
    )
    llm._sync = SimpleNamespace(chat=SimpleNamespace(completions=sync_completions))
    return llm, async_completions, sync_completions


def _anthropic(*, async_script=(), sync_script=()):
    llm = object.__new__(AnthropicLLM)
    llm._init_provider_attempt_contexts()
    llm.model = "claude-requested"
    llm.temperature = 0.3
    llm.max_tokens = 256
    llm.tracker = CostTracker()
    async_messages = ScriptedAsyncAnthropicMessages(async_script)
    sync_messages = ScriptedSyncAnthropicMessages(sync_script)
    llm._async = SimpleNamespace(messages=async_messages)
    llm._sync = SimpleNamespace(messages=sync_messages)
    return llm, async_messages, sync_messages


def _context(
    sequence: int,
    observer,
    *,
    system: str = "SYSTEM SECRET",
    user: str = "LATEST PRICE: 100\nUSER SECRET",
    batch_index: int = 0,
    batch_size: int = 1,
):
    return ProviderAttemptContext(
        logical_sequence=sequence,
        round_i=4,
        batch_sequence=3,
        batch_index=batch_index,
        batch_size=batch_size,
        agent="agent-{}".format(sequence),
        persona="persona-{}".format(sequence),
        original_system_hash=sha256_text(system),
        original_user_hash=sha256_text(user),
        original_prompt_hash=prompt_hash(system, user),
        observer=observer,
    )


def _outcomes(observer):
    return [item.outcome for _, item in observer.items]


class OpenAIProviderAttemptTests(unittest.TestCase):
    def test_first_valid_response_is_byte_exact_one_attempt_with_reported_model(self):
        observer = CollectingObserver()
        llm, client, _ = _openai(async_script=[(VALID, "HiggsAI")])
        llm.set_provider_attempt_contexts([_context(1, observer)])

        result = asyncio.run(llm.acomplete("SYSTEM SECRET", "LATEST PRICE: 100\nUSER SECRET"))

        self.assertEqual(result, VALID)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(llm.tracker.calls, 1)
        self.assertEqual(_outcomes(observer), ["response_parseable"])
        context, attempt = observer.items[0]
        public = attempt.public_payload(context)
        self.assertEqual(public["model"], "MiniMax-M2.7")
        self.assertEqual(public["reported_model"], "HiggsAI")
        self.assertEqual(public["prompt_tokens"], 11)
        self.assertEqual(public["completion_tokens"], 7)
        self.assertNotEqual(public["original_prompt_hash"], "SYSTEM SECRET")
        with self.assertRaises(FrozenInstanceError):
            context.logical_sequence = 2

    def test_invalid_exception_valid_preserves_prompt_nudges_and_cost_semantics(self):
        observer = CollectingObserver()
        llm, client, _ = _openai(
            async_script=[INVALID, RuntimeError("private transport detail"), VALID]
        )
        original_user = "LATEST PRICE: 100\nUSER SECRET"
        llm.set_provider_attempt_contexts(
            [_context(1, observer, user=original_user)]
        )

        with mock.patch("nmsim.llm.asyncio.sleep", new=mock.AsyncMock()) as sleep:
            result = asyncio.run(llm.acomplete("SYSTEM SECRET", original_user))

        self.assertEqual(result, VALID)
        self.assertEqual(llm.tracker.calls, 2)
        self.assertEqual(_outcomes(observer), [
            "response_parse_failed",
            "provider_exception",
            "response_parseable",
        ])
        self.assertEqual(
            [item.trigger for _, item in observer.items],
            ["initial", "parse_failure", "provider_exception"],
        )
        attempted_users = [
            call["messages"][1]["content"] for call in client.calls
        ]
        self.assertEqual(
            attempted_users,
            [original_user, original_user + REMINDER, original_user + REMINDER],
        )
        public_attempts = [
            attempt.public_payload(context)
            for context, attempt in observer.items
        ]
        self.assertEqual(
            {item["original_prompt_hash"] for item in public_attempts},
            {prompt_hash("SYSTEM SECRET", original_user)},
        )
        self.assertNotEqual(
            public_attempts[0]["attempted_prompt_hash"],
            public_attempts[1]["attempted_prompt_hash"],
        )
        self.assertEqual(
            public_attempts[1]["attempted_prompt_hash"],
            public_attempts[2]["attempted_prompt_hash"],
        )
        sleep.assert_awaited_once_with(1.0)

    def test_three_parse_failures_and_three_exceptions_keep_frozen_fallbacks(self):
        parse_observer = CollectingObserver()
        parse_llm, _, _ = _openai(async_script=[INVALID, INVALID, INVALID])
        parse_llm.set_provider_attempt_contexts([_context(1, parse_observer)])
        parse_result = asyncio.run(
            parse_llm.acomplete("SYSTEM SECRET", "LATEST PRICE: 101\nUSER SECRET")
        )
        self.assertEqual(
            json.loads(parse_result)["rationale"],
            "parse-retries-exhausted; holding",
        )
        self.assertEqual(parse_llm.tracker.calls, 3)
        self.assertEqual(
            _outcomes(parse_observer), ["response_parse_failed"] * 3
        )
        self.assertEqual(
            [item.attempted_user.count(REMINDER) for _, item in parse_observer.items],
            [0, 1, 2],
        )
        self.assertEqual(
            [item.will_retry for _, item in parse_observer.items],
            [True, True, False],
        )

        error_observer = CollectingObserver()
        errors = [RuntimeError("one"), RuntimeError("two"), RuntimeError("three")]
        error_llm, _, _ = _openai(async_script=errors)
        error_llm.set_provider_attempt_contexts([_context(2, error_observer)])
        with mock.patch("nmsim.llm.asyncio.sleep", new=mock.AsyncMock()) as sleep:
            error_result = asyncio.run(
                error_llm.acomplete("SYSTEM SECRET", "LATEST PRICE: 99\nUSER SECRET")
            )
        self.assertEqual(json.loads(error_result)["rationale"], "api-error; holding")
        self.assertEqual(error_llm.tracker.calls, 0)
        self.assertEqual(_outcomes(error_observer), ["provider_exception"] * 3)
        self.assertEqual(sleep.await_count, 2)

    def test_observer_write_failure_propagates_without_retry_or_fallback(self):
        observer = CollectingObserver(fail=True)
        llm, client, _ = _openai(async_script=[VALID, VALID, VALID])
        llm.set_provider_attempt_contexts([_context(1, observer)])

        with self.assertRaisesRegex(ObserverWriteError, "observer write failed"):
            asyncio.run(
                llm.acomplete("SYSTEM SECRET", "LATEST PRICE: 100\nUSER SECRET")
            )

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(llm.tracker.calls, 1)

    def test_sync_path_observes_exactly_one_attempt(self):
        observer = CollectingObserver()
        llm, _, client = _openai(sync_script=[VALID])
        llm.set_provider_attempt_contexts([_context(1, observer)])

        result = llm.complete("SYSTEM SECRET", "LATEST PRICE: 100\nUSER SECRET")

        self.assertEqual(result, VALID)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(len(observer.items), 1)
        self.assertEqual(observer.items[0][1].max_attempts, 1)
        self.assertFalse(observer.items[0][1].will_retry)


class CorrelationCacheReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        cfg = Config(
            provider="openai",
            model="MiniMax-M2.7",
            temperature=0.3,
            max_tokens=256,
            out_dir=str(self.root),
        )
        self.compatibility = {
            **scientific_compatibility_metadata(REPO_ROOT),
            **build_effective_config_contract(
                cfg,
                base_dir=self.root,
                execution_context={"test_boundary": "provider-attempt-provenance"},
            ),
        }

    def test_recording_cache_miss_then_hit_emits_one_attempt_and_two_final_records(self):
        provider, _, client = _openai(sync_script=[VALID])
        cache = CachingLLM(provider, provider.tracker, enabled=True)
        events = RecordingEventCollector()
        recorder = RecordingLLM(
            cache,
            self.root / "record",
            event_logger=events,
            compatibility_metadata=self.compatibility,
        )
        prompt = ("PERSONA: p\nSYSTEM SECRET", "LATEST PRICE: 100\nUSER SECRET")

        recorder.set_batch_context(4, [{"agent_id": "a1", "persona_id": "p1"}])
        first = recorder.complete(*prompt)
        recorder.set_batch_context(4, [{"agent_id": "a1", "persona_id": "p1"}])
        second = recorder.complete(*prompt)

        self.assertEqual(first.encode("utf-8"), second.encode("utf-8"))
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(provider.tracker.cache_hits, 1)
        self.assertEqual(len(events.items), 1)
        self.assertEqual(events.items[0][0].logical_sequence, 1)
        records = [
            json.loads(line)
            for line in recorder.records_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(len(records), 2)
        self.assertEqual({record["schema_version"] for record in records}, {"1.2"})
        self.assertEqual([record["raw_response"] for record in records], [VALID, VALID])

    def test_batch_context_survives_cache_filter_and_async_gather_order(self):
        provider, client, _ = _openai(
            async_script=[VALID, VALID], sync_script=[VALID]
        )
        cache = CachingLLM(provider, provider.tracker, enabled=True)
        events = RecordingEventCollector()
        recorder = RecordingLLM(
            cache,
            self.root / "batch",
            event_logger=events,
            compatibility_metadata=self.compatibility,
        )
        cached = ("PERSONA: cached", "LATEST PRICE: 100\ncached")
        recorder.set_batch_context(1, [{"agent_id": "warm", "persona_id": "cached"}])
        recorder.complete(*cached)
        events.items.clear()
        prompts = [
            cached,
            ("PERSONA: p1", "LATEST PRICE: 100\nfirst"),
            ("PERSONA: p2", "LATEST PRICE: 100\nsecond"),
        ]
        metadata = [
            {"agent_id": "a0", "persona_id": "cached"},
            {"agent_id": "a1", "persona_id": "p1"},
            {"agent_id": "a2", "persona_id": "p2"},
        ]
        recorder.set_batch_context(7, metadata)

        results = recorder.complete_batch(prompts)

        self.assertEqual(results, [VALID, VALID, VALID])
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(
            [(context.logical_sequence, context.batch_index, context.agent)
             for context, _ in events.items],
            [(3, 1, "a1"), (4, 2, "a2")],
        )

    def test_strict_replay_returns_recorded_bytes_and_observes_zero_attempts(self):
        provider, _, _ = _openai(sync_script=[VALID])
        source_events = RecordingEventCollector()
        source = RecordingLLM(
            CachingLLM(provider, provider.tracker, enabled=False),
            self.root / "source",
            event_logger=source_events,
            compatibility_metadata=self.compatibility,
        )
        prompt = ("PERSONA: p", "LATEST PRICE: 100\nreplay me")
        metadata = [{"agent_id": "a", "persona_id": "p"}]
        source.set_batch_context(2, metadata)
        recorded = source.complete(*prompt)

        replay_events = RecordingEventCollector()
        replay = ReplayLLM(
            source.records_path,
            model_config=source.model_config,
            event_logger=replay_events,
            compatibility_metadata=source.compatibility_metadata,
        )
        replay.set_batch_context(2, metadata)
        replayed = replay.complete(*prompt)

        self.assertEqual(replayed.encode("utf-8"), recorded.encode("utf-8"))
        self.assertEqual(replay_events.items, [])
        self.assertEqual(replay.records_consumed, 1)

        managed = ManagedRunContext.create(
            Config(
                provider="mock",
                n_rounds=1,
                n_llm_agents=1,
                n_noise_agents=0,
                out_dir=str(self.root / "replay-managed"),
            ),
            run_id="provider-attempt-replay",
            repo_root=REPO_ROOT,
            command_identity="tests:provider-attempt-replay",
        )
        managed_replay = ReplayLLM(
            source.records_path,
            model_config=source.model_config,
            event_logger=managed.events,
            compatibility_metadata=source.compatibility_metadata,
        )
        managed_replay.set_batch_context(2, metadata)
        self.assertEqual(managed_replay.complete(*prompt), recorded)
        managed.llm_mode = "replay"
        managed.active_llm = managed_replay
        managed.tracker = CostTracker()
        managed.sync_llm_accounting()
        completion = managed.manifest["completion"]
        self.assertEqual(completion["application_provider_attempts"]["attempted"], 0)
        self.assertEqual(
            completion["application_provider_attempts"]["reported_models"], []
        )
        self.assertEqual(completion["provider_calls"]["attempted"], 0)

    def test_managed_public_private_events_and_completion_are_fail_closed(self):
        cfg = Config(
            provider="mock",
            n_rounds=1,
            n_llm_agents=1,
            n_noise_agents=0,
            out_dir=str(self.root / "managed"),
        )
        context = ManagedRunContext.create(
            cfg,
            run_id="provider-attempt-privacy",
            repo_root=REPO_ROOT,
            command_identity="tests:provider-attempt-privacy",
        )
        raw_secret = VALID.replace('"ok"', '"PRIVATE_RAW_RESPONSE"')
        provider, _, _ = _openai(sync_script=[(raw_secret, "HiggsAI")])
        recorder = RecordingLLM(
            CachingLLM(provider, provider.tracker, enabled=False),
            context.run_dir,
            event_logger=context.events,
            compatibility_metadata=context.replay_compatibility,
        )
        recorder.set_batch_context(
            4, [{"agent_id": "agent-safe", "persona_id": "persona-safe"}]
        )
        system = "SYSTEM_PRIVATE_MARKER"
        user = "LATEST PRICE: 100\nUSER_PRIVATE_MARKER"

        self.assertEqual(recorder.complete(system, user), raw_secret)
        context.llm_mode = "record"
        context.active_llm = recorder
        context.tracker = provider.tracker
        context.sync_llm_accounting()

        public_text = Path(context.public_events_path).read_text(encoding="utf-8")
        private_path = Path(context.private_events_path)
        private_text = private_path.read_text(encoding="utf-8")
        for secret in (
            "SYSTEM_PRIVATE_MARKER",
            "USER_PRIVATE_MARKER",
            "PRIVATE_RAW_RESPONSE",
        ):
            self.assertNotIn(secret, public_text)
            self.assertIn(secret, private_text)
        self.assertEqual(stat.S_IMODE(private_path.stat().st_mode), 0o600)
        attempts = context.manifest["completion"]["application_provider_attempts"]
        self.assertEqual(context.manifest["completion"]["schema_version"], "1.1")
        self.assertEqual(attempts["attempted"], 1)
        self.assertEqual(attempts["responses_received"], 1)
        self.assertEqual(attempts["parse_failed_responses"], 0)
        self.assertEqual(attempts["provider_exceptions"], 0)
        self.assertEqual(attempts["reported_models"], ["HiggsAI"])
        self.assertFalse(attempts["reported_models_truncated"])
        self.assertEqual(
            context.manifest["completion"]["provider_calls"],
            {
                "unit": "logical_provider_requests_after_cache_and_replay",
                "attempted": 1,
                "succeeded": 1,
                "failed": 0,
                "coverage": (
                    "provider-interface requests; SDK-internal retry attempts "
                    "are not observable"
                ),
            },
        )
        attempt_events = [
            json.loads(line)
            for line in public_text.splitlines()
            if json.loads(line)["type"] == "LLMProviderAttemptObserved"
        ]
        self.assertEqual(len(attempt_events), 1)
        payload = attempt_events[0]["data"]
        self.assertEqual(payload["provider_attempt_schema"], "provider_attempt_v1")
        self.assertEqual(payload["reported_model"], "HiggsAI")
        self.assertNotIn("exception_detail", payload)

    def test_managed_completion_counts_retry_causes_without_inflating_provider_calls(self):
        credential = "sk-provider-attempt-secret-sentinel"
        context = ManagedRunContext.create(
            Config(
                provider="mock",
                n_rounds=1,
                n_llm_agents=1,
                n_noise_agents=0,
                out_dir=str(self.root / "retry-accounting"),
            ),
            run_id="provider-attempt-retry-accounting",
            repo_root=REPO_ROOT,
            command_identity="tests:provider-attempt-retry-accounting",
        )
        provider, _, _ = _openai(
            async_script=[
                INVALID,
                RuntimeError(
                    "PRIVATE_PROVIDER_EXCEPTION_DETAIL credential={}".format(
                        credential
                    )
                ),
                (VALID, "HiggsAI"),
            ]
        )
        recorder = RecordingLLM(
            CachingLLM(provider, provider.tracker, enabled=False),
            context.run_dir,
            event_logger=context.events,
            compatibility_metadata=context.replay_compatibility,
        )
        recorder.set_batch_context(
            5, [{"agent_id": "agent-safe", "persona_id": "persona-safe"}]
        )
        with mock.patch.dict(
            os.environ, {"OPENAI_API_KEY": credential}
        ), mock.patch("nmsim.llm.asyncio.sleep", new=mock.AsyncMock()):
            self.assertEqual(
                recorder.complete_batch(
                    [("SYSTEM_PRIVATE", "LATEST PRICE: 100\nUSER_PRIVATE")]
                ),
                [VALID],
            )
        context.llm_mode = "record"
        context.active_llm = recorder
        context.tracker = provider.tracker
        context.sync_llm_accounting()

        attempts = context.manifest["completion"]["application_provider_attempts"]
        self.assertEqual(
            {key: attempts[key] for key in (
                "attempted",
                "responses_received",
                "parse_failed_responses",
                "provider_exceptions",
                "retries_scheduled",
                "logical_requests_with_retry",
                "exhausted_logical_requests",
            )},
            {
                "attempted": 3,
                "responses_received": 2,
                "parse_failed_responses": 1,
                "provider_exceptions": 1,
                "retries_scheduled": 2,
                "logical_requests_with_retry": 1,
                "exhausted_logical_requests": 0,
            },
        )
        # Provider calls retain their historical logical-request unit even when
        # the additive attempt accounting sees three adapter-loop attempts.
        self.assertEqual(
            context.manifest["completion"]["provider_calls"]["attempted"], 1
        )
        records = [
            json.loads(line)
            for line in recorder.records_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["schema_version"], "1.2")
        self.assertEqual(records[0]["raw_response"], VALID)
        self.assertNotIn(INVALID, records[0]["raw_response"])
        public_text = Path(context.public_events_path).read_text(encoding="utf-8")
        private_text = Path(context.private_events_path).read_text(encoding="utf-8")
        self.assertNotIn(credential, public_text)
        self.assertNotIn(credential, private_text)
        self.assertNotIn("PRIVATE_PROVIDER_EXCEPTION_DETAIL", public_text)
        self.assertIn("PRIVATE_PROVIDER_EXCEPTION_DETAIL", private_text)
        self.assertIn("<redacted>", private_text)


class AnthropicProviderAttemptTests(unittest.TestCase):
    def test_async_retry_has_openai_parity_and_preserves_cost_tracking(self):
        observer = CollectingObserver()
        llm, client, _ = _anthropic(async_script=[INVALID, VALID])
        llm.set_provider_attempt_contexts([_context(1, observer)])

        result = asyncio.run(
            llm.acomplete("SYSTEM SECRET", "LATEST PRICE: 100\nUSER SECRET")
        )

        self.assertEqual(result, VALID)
        self.assertEqual(llm.tracker.calls, 2)
        self.assertEqual(
            _outcomes(observer), ["response_parse_failed", "response_parseable"]
        )
        self.assertEqual(observer.items[1][1].reported_model, "claude-reported")
        self.assertEqual(
            client.calls[1]["messages"][0]["content"],
            "LATEST PRICE: 100\nUSER SECRET" + REMINDER,
        )

    def test_sync_path_observes_one_attempt_and_provider_exception(self):
        valid_observer = CollectingObserver()
        llm, _, client = _anthropic(sync_script=[VALID])
        llm.set_provider_attempt_contexts([_context(1, valid_observer)])
        self.assertEqual(
            llm.complete("SYSTEM SECRET", "LATEST PRICE: 100\nUSER SECRET"),
            VALID,
        )
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(_outcomes(valid_observer), ["response_parseable"])
        self.assertEqual(
            valid_observer.items[0][1].reported_model, "claude-sync-reported"
        )

        error_observer = CollectingObserver()
        failed, _, _ = _anthropic(sync_script=[RuntimeError("private sync error")])
        failed.set_provider_attempt_contexts([_context(2, error_observer)])
        with self.assertRaisesRegex(RuntimeError, "private sync error"):
            failed.complete("SYSTEM SECRET", "LATEST PRICE: 100\nUSER SECRET")
        self.assertEqual(_outcomes(error_observer), ["provider_exception"])
        self.assertIsNone(error_observer.items[0][1].reported_model)
        self.assertEqual(failed.tracker.calls, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
