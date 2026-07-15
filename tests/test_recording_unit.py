"""Unit tests for the Phase-1 LLM record/replay boundary."""
from __future__ import annotations

import json
import os
import pathlib
import tempfile
import unittest

from nmsim.events import EventLogger
from nmsim.recording import RecordingLLM, ReplayLLM, ReplayMismatchError


class CountingProvider:
    """Small provider double whose call count exposes accidental replay I/O."""

    kind = "mock"
    model = "fake-model-v1"
    temperature = 0.0
    max_tokens = 64

    def __init__(self) -> None:
        self.complete_calls = 0
        self.batch_calls = 0
        self.prompt_calls = 0

    def complete(self, system: str, user: str) -> str:
        self.complete_calls += 1
        self.prompt_calls += 1
        return json.dumps(
            {
                "action": "hold",
                "quantity": 0,
                "public_take": "public single response",
                "reasoning": "RAW_RESPONSE_SECRET_SINGLE",
                "request": [system, user],
            },
            sort_keys=True,
        )

    def complete_batch(self, prompts):
        self.batch_calls += 1
        self.prompt_calls += len(prompts)
        return [
            json.dumps(
                {
                    "action": "hold",
                    "quantity": 0,
                    "public_take": "public batch response {}".format(index),
                    "reasoning": "RAW_RESPONSE_SECRET_BATCH_{}".format(index),
                    "request": [system, user],
                },
                sort_keys=True,
            )
            for index, (system, user) in enumerate(prompts)
        ]


class RecordingReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.record_dir = self.root / "record"
        self.record_dir.mkdir()
        self.provider = CountingProvider()
        self.model_config = {
            "provider": "mock",
            "model": "fake-model-v1",
            "temperature": 0.0,
            "max_tokens": 64,
            "cache_enabled": False,
        }
        self.logger = EventLogger("record-run", self.record_dir)
        self.recorder = RecordingLLM(
            self.provider,
            self.record_dir,
            self.model_config,
            self.logger,
        )
        self.first_prompts = [
            ("SYSTEM_PRIVATE_CONTENT_A", "USER_PRIVATE_CONTENT_A"),
            ("SYSTEM_PRIVATE_CONTENT_B", "USER_PRIVATE_CONTENT_B"),
        ]
        self.first_metadata = [
            {"agent_id": "agent-a", "persona_id": "persona-a"},
            {"agent_id": "agent-b", "persona_id": "persona-b"},
        ]

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _record_two_batches(self):
        self.recorder.set_batch_context(7, self.first_metadata)
        first = self.recorder.complete_batch(self.first_prompts)
        self.recorder.set_batch_context(
            8, [{"agent_id": "agent-c", "persona_id": "persona-c"}]
        )
        second_prompt = ("SYSTEM_PRIVATE_CONTENT_C", "USER_PRIVATE_CONTENT_C")
        second = self.recorder.complete(*second_prompt)
        return first, second_prompt, second

    def _new_replay(self) -> ReplayLLM:
        replay_dir = self.root / "replay-events"
        replay_dir.mkdir(exist_ok=True)
        return ReplayLLM(
            self.record_dir,
            self.model_config,
            EventLogger("replay-run", replay_dir),
        )

    def test_record_then_replay_is_offline_and_returns_identical_responses(self):
        first, second_prompt, second = self._record_two_batches()
        provider_counts = (
            self.provider.complete_calls,
            self.provider.batch_calls,
            self.provider.prompt_calls,
        )

        replay = self._new_replay()
        self.assertNotIn("inner", vars(replay))
        self.assertFalse(hasattr(replay, "inner"))

        replay.set_batch_context(7, self.first_metadata)
        self.assertEqual(first, replay.complete_batch(self.first_prompts))
        replay.set_batch_context(
            8, [{"agent_id": "agent-c", "persona_id": "persona-c"}]
        )
        self.assertEqual(second, replay.complete(*second_prompt))
        replay.assert_exhausted()

        self.assertEqual(
            provider_counts,
            (
                self.provider.complete_calls,
                self.provider.batch_calls,
                self.provider.prompt_calls,
            ),
        )
        self.assertEqual([2, 1], replay.batch_sizes)
        self.assertEqual(3, replay.records_consumed)

    def test_records_preserve_full_prompt_persona_round_batch_and_call_order(self):
        self._record_two_batches()
        records = [
            json.loads(line)
            for line in (self.record_dir / "llm_records.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]

        self.assertEqual([1, 2, 3], [record["sequence"] for record in records])
        self.assertEqual([7, 7, 8], [record["round"] for record in records])
        self.assertEqual([1, 1, 2], [record["batch_sequence"] for record in records])
        self.assertEqual([0, 1, 0], [record["batch_index"] for record in records])
        self.assertEqual([2, 2, 1], [record["batch_size"] for record in records])
        self.assertEqual(
            ["persona-a", "persona-b", "persona-c"],
            [record["persona_id"] for record in records],
        )
        self.assertEqual(
            ["agent-a", "agent-b", "agent-c"],
            [record["agent_id"] for record in records],
        )
        self.assertEqual(
            self.first_prompts[0],
            (records[0]["request"]["system"], records[0]["request"]["user"]),
        )

        replay = self._new_replay()
        replay.set_batch_context(7, self.first_metadata)
        with self.assertRaisesRegex(ReplayMismatchError, "batch_size"):
            replay.complete(*self.first_prompts[0])

        replay = self._new_replay()
        changed_persona = [dict(item) for item in self.first_metadata]
        changed_persona[0]["persona_id"] = "different-persona"
        replay.set_batch_context(7, changed_persona)
        with self.assertRaisesRegex(ReplayMismatchError, "persona_id"):
            replay.complete_batch(self.first_prompts)

        replay = self._new_replay()
        replay.set_batch_context(99, self.first_metadata)
        with self.assertRaisesRegex(ReplayMismatchError, "round"):
            replay.complete_batch(self.first_prompts)

    def test_prompt_change_is_rejected_explicitly(self):
        self.recorder.set_batch_context(7, self.first_metadata)
        self.recorder.complete_batch(self.first_prompts)
        replay = self._new_replay()
        replay.set_batch_context(7, self.first_metadata)
        changed = list(self.first_prompts)
        changed[0] = (changed[0][0], changed[0][1] + " CHANGED")

        with self.assertRaisesRegex(ReplayMismatchError, "prompt_hash"):
            replay.complete_batch(changed)

    def test_model_and_max_tokens_changes_are_rejected_explicitly(self):
        self.recorder.set_batch_context(7, self.first_metadata)
        self.recorder.complete_batch(self.first_prompts)

        for field, value in (("model", "fake-model-v2"), ("max_tokens", 65)):
            with self.subTest(field=field):
                changed = dict(self.model_config)
                changed[field] = value
                with self.assertRaisesRegex(
                    ReplayMismatchError, "model configuration mismatch"
                ):
                    ReplayLLM(self.record_dir, changed)

    def test_private_files_are_restricted_and_public_events_are_sanitized(self):
        self._record_two_batches()
        records_path = self.record_dir / "llm_records.jsonl"
        private_path = self.record_dir / "private_events.jsonl"
        public_path = self.record_dir / "events.jsonl"

        self.assertEqual(0o600, records_path.stat().st_mode & 0o777)
        self.assertEqual(0o600, private_path.stat().st_mode & 0o777)

        public_text = public_path.read_text(encoding="utf-8")
        for secret in (
            "SYSTEM_PRIVATE_CONTENT",
            "USER_PRIVATE_CONTENT",
            "RAW_RESPONSE_SECRET",
        ):
            self.assertNotIn(secret, public_text)

        forbidden_keys = {
            "rawresponse",
            "system",
            "systemprompt",
            "user",
            "userprompt",
            "reasoning",
            "rationale",
        }
        for line in public_text.splitlines():
            event = json.loads(line)
            pending = [event["data"]]
            while pending:
                value = pending.pop()
                if isinstance(value, dict):
                    for key, item in value.items():
                        normalised = "".join(
                            character for character in key.lower() if character.isalnum()
                        )
                        self.assertNotIn(normalised, forbidden_keys)
                        pending.append(item)
                elif isinstance(value, list):
                    pending.extend(value)

        private_text = private_path.read_text(encoding="utf-8")
        records_text = records_path.read_text(encoding="utf-8")
        self.assertIn("SYSTEM_PRIVATE_CONTENT_A", private_text)
        self.assertIn("USER_PRIVATE_CONTENT_A", private_text)
        self.assertIn("RAW_RESPONSE_SECRET_BATCH_0", private_text)
        self.assertIn("SYSTEM_PRIVATE_CONTENT_A", records_text)
        self.assertIn("RAW_RESPONSE_SECRET_BATCH_0", records_text)


if __name__ == "__main__":
    unittest.main()
