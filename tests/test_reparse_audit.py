"""Offline reparse-audit tests; no provider or network is constructed."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from nmsim.fingerprint import (
    STRICT_COMPATIBILITY_FIELDS,
    scientific_compatibility_metadata,
)
from nmsim.reparse_audit import (
    PRIVATE_RESULTS_NAME,
    PUBLIC_RESULTS_NAME,
    SUMMARY_NAME,
    main,
    run_reparse_audit,
)


def _write_jsonl(path: Path, values: list[dict]) -> None:
    path.write_text(
        "".join(
            json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n"
            for value in values
        ),
        encoding="utf-8",
    )


def _record(raw: str, *, response_hash: str, sequence: int = 1) -> dict:
    return {
        "schema_version": "1.0",
        "record_type": "llm_call",
        "run_id": "historical-run",
        "sequence": sequence,
        "round": 3,
        "batch_sequence": 2,
        "batch_index": 0,
        "batch_size": 1,
        "agent_id": "agent-a",
        "persona_id": "persona-a",
        "request": {
            "system": "private historical system prompt",
            "user": "ROUND: 3\nLAST_PRICE: 99.50\nprivate historical user prompt",
            "system_hash": "system-hash",
            "user_hash": "user-hash",
            "prompt_hash": "prompt-hash",
        },
        "model_config": {"provider": "mock", "model": "mock"},
        "raw_response": raw,
        "response_hash": response_hash,
    }


def _snapshot(directory: Path) -> dict[str, str]:
    return {
        path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


class ReparseAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.run_dir = self.root / "historical"
        self.run_dir.mkdir()
        self.out_root = self.root / "audits"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_differing_history(self) -> tuple[str, str]:
        old_secret = "HISTORICAL_PRIVATE_RATIONALE_DO_NOT_PUBLISH"
        current_secret = "CURRENT_PRIVATE_RATIONALE_DO_NOT_PUBLISH"
        raw = json.dumps(
            {
                "action": "buy",
                "quantity": 3,
                "limit_price": 101.25,
                "reservation_price": 102.5,
                "sentiment": 0.75,
                "public_take": "current public take",
                "reasoning": current_secret,
            }
        )
        response_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        _write_jsonl(
            self.run_dir / "llm_records.jsonl",
            [_record(raw, response_hash=response_hash)],
        )
        public_data = {
            "persona_id": "persona-a",
            "action": "sell",
            "quantity": 1,
            "limit_price": 98.0,
            "reservation_price": 97.5,
            "sentiment": -0.5,
            "public_take": "historical public take",
            "parse_status": "parsed",
            "fallback_status": "none",
            "validation_errors": [],
            "raw_response_sha256": response_hash,
        }
        event = {
            "run_id": "historical-run",
            "round": 3,
            "event_id": "evt-00000008",
            "timestamp": "2026-07-15T00:00:00Z",
            "agent_id": "agent-a",
            "schema_version": "1.0",
            "type": "AgentDecisionParsed",
            "data": public_data,
        }
        private_event = dict(event)
        private_event["data"] = {**public_data, "private_rationale": old_secret}
        _write_jsonl(self.run_dir / "events.jsonl", [event])
        _write_jsonl(self.run_dir / "private_events.jsonl", [private_event])
        os.chmod(self.run_dir / "private_events.jsonl", 0o600)
        os.chmod(self.run_dir / "llm_records.jsonl", 0o600)
        return old_secret, current_secret

    def test_audit_detects_field_differences_without_mutating_source_or_leaking_reasoning(self):
        old_secret, current_secret = self._write_differing_history()
        before = _snapshot(self.run_dir)

        audit_dir = run_reparse_audit(self.run_dir, self.out_root)

        self.assertEqual(before, _snapshot(self.run_dir))
        self.assertNotEqual(self.run_dir, audit_dir)
        self.assertTrue(audit_dir.parent.samefile(self.out_root))
        public_text = (audit_dir / PUBLIC_RESULTS_NAME).read_text(encoding="utf-8")
        summary_text = (audit_dir / SUMMARY_NAME).read_text(encoding="utf-8")
        for secret in (old_secret, current_secret):
            self.assertNotIn(secret, public_text)
            self.assertNotIn(secret, summary_text)

        result = json.loads(public_text)
        self.assertEqual(result["comparison_status"], "different")
        self.assertTrue(result["field_differences"]["action"]["changed"])
        self.assertTrue(result["field_differences"]["quantity"]["changed"])
        self.assertTrue(result["field_differences"]["reasoning"]["changed"])
        self.assertNotIn("system", result["request_identity"])
        self.assertNotIn("user", result["request_identity"])

        summary = json.loads(summary_text)
        self.assertEqual(summary["total_response_count"], 1)
        self.assertEqual(summary["successful_reparse_count"], 1)
        self.assertEqual(summary["parse_failure_count"], 0)
        self.assertEqual(summary["different_count"], 1)
        self.assertEqual(summary["field_difference_counts"]["action"], 1)
        self.assertEqual(summary["provider_calls"], 0)
        self.assertFalse(summary["network_access"])
        self.assertFalse(summary["simulation_continued"])
        self.assertFalse(summary["price_path_generated"])
        self.assertEqual(
            summary["recorded_contract"]["status"],
            "legacy_contract_unavailable",
        )
        self.assertIn(
            "decision_parser_source_hash",
            summary["recorded_contract"]["missing_fields"],
        )
        self.assertIn(
            "scientific_component_fingerprint",
            summary["current_parser_contract"],
        )

        private_path = audit_dir / PRIVATE_RESULTS_NAME
        self.assertEqual(private_path.stat().st_mode & 0o777, 0o600)
        private_text = private_path.read_text(encoding="utf-8")
        self.assertIn(old_secret, private_text)
        self.assertIn(current_secret, private_text)

        # A second audit never reuses or overwrites the first directory.
        second = run_reparse_audit(self.run_dir, self.out_root)
        self.assertNotEqual(audit_dir, second)
        self.assertTrue((audit_dir / SUMMARY_NAME).is_file())
        self.assertTrue((second / SUMMARY_NAME).is_file())

    def test_missing_historical_decision_is_explicit_and_malformed_privacy_stays_safe(self):
        private_secret = "LEGACY_RATIONALE_MUST_NOT_BECOME_PUBLIC_TAKE"
        raw = json.dumps(
            {
                "side": "hold",
                "quantity": 0,
                "sentiment": 0.0,
                "rationale": private_secret,
            }
        )
        response_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        _write_jsonl(
            self.run_dir / "llm_records.jsonl",
            [_record(raw, response_hash=response_hash)],
        )

        audit_dir = run_reparse_audit(self.run_dir, self.out_root)
        public_text = (audit_dir / PUBLIC_RESULTS_NAME).read_text(encoding="utf-8")
        self.assertNotIn(private_secret, public_text)
        result = json.loads(public_text)
        self.assertEqual(result["comparison_status"], "comparison_unavailable")
        self.assertIsNone(result["historical_decision"])
        self.assertEqual(result["current_decision"]["public_take"], "")
        self.assertTrue(result["current_decision"]["reasoning_present"])

        summary = json.loads(
            (audit_dir / SUMMARY_NAME).read_text(encoding="utf-8")
        )
        self.assertEqual(summary["comparison_unavailable_count"], 1)
        private_text = (audit_dir / PRIVATE_RESULTS_NAME).read_text(encoding="utf-8")
        self.assertIn(private_secret, private_text)

    def test_output_inside_historical_run_is_rejected(self):
        raw = "not-json"
        _write_jsonl(
            self.run_dir / "llm_records.jsonl",
            [_record(raw, response_hash=hashlib.sha256(raw.encode()).hexdigest())],
        )
        with self.assertRaisesRegex(ValueError, "outside the immutable"):
            run_reparse_audit(self.run_dir, self.run_dir / "new-audit")
        self.assertFalse((self.run_dir / "new-audit").exists())

    def test_incompatible_recorded_contract_is_reported_but_does_not_block_audit(self):
        raw = json.dumps(
            {
                "action": "hold",
                "quantity": 0,
                "limit_price": 99.5,
                "sentiment": 0.0,
                "public_take": "public",
                "reasoning": "private",
            }
        )
        record = _record(raw, response_hash=hashlib.sha256(raw.encode()).hexdigest())
        metadata = scientific_compatibility_metadata()
        for field in (
            "fingerprint_schema_version",
            *STRICT_COMPATIBILITY_FIELDS,
            "git_commit",
            "git_dirty",
        ):
            record[field] = metadata.get(field)
        # Reparse is a diagnostic path, so an old parser hash must be visible
        # but must not invoke strict-replay rejection.
        record["decision_parser_source_hash"] = "0" * 64
        _write_jsonl(self.run_dir / "llm_records.jsonl", [record])

        audit_dir = run_reparse_audit(self.run_dir, self.out_root)
        summary = json.loads(
            (audit_dir / SUMMARY_NAME).read_text(encoding="utf-8")
        )
        self.assertEqual(summary["recorded_contract"]["status"], "available")
        self.assertEqual(
            summary["recorded_contract"]["fields"]["decision_parser_source_hash"],
            "0" * 64,
        )
        self.assertNotEqual(
            summary["recorded_contract"]["fields"]["decision_parser_source_hash"],
            summary["current_parser_contract"]["decision_parser_source_hash"],
        )
        self.assertEqual(summary["provider_calls"], 0)

    def test_cli_writes_audit_without_provider_mode(self):
        raw = "not-json"
        _write_jsonl(
            self.run_dir / "llm_records.jsonl",
            [_record(raw, response_hash=hashlib.sha256(raw.encode()).hexdigest())],
        )
        self.assertEqual(
            main(["--run", str(self.run_dir), "--out", str(self.out_root)]),
            0,
        )
        audit_dirs = list(self.out_root.iterdir())
        self.assertEqual(len(audit_dirs), 1)
        summary = json.loads(
            (audit_dirs[0] / SUMMARY_NAME).read_text(encoding="utf-8")
        )
        self.assertEqual(summary["provider_calls"], 0)
        self.assertFalse(summary["network_access"])
        self.assertEqual(summary["parse_failure_count"], 1)


if __name__ == "__main__":
    unittest.main()
