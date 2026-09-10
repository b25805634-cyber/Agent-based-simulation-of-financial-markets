"""Synthetic managed inputs and fake transports only; no endpoint or human run."""
import asyncio
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from experiments import human_early_teacher as entry
from nmsim import human_early_acquisition as plans
from nmsim.human_early_tasks import build_early_tasks
from nmsim.information_artifacts import canonical_hash, file_sha256, read_json, verify_run, write_json_exclusive
from tests.test_human_reference import fixture_records


def fixture_bank():
    records = []
    for index in range(39):
        rows = fixture_records("SYNTHETIC_SUBJECT_%02d" % index)
        for row in rows:
            row["group.id_in_subsession"] = str(index // 3 + 1)
        records.extend(rows)
    return build_early_tasks(records)


def seal_fixture(root, run_id="synthetic-source"):
    descriptors = [{"path": p.name, "kind": "file", "error": None, "exists": True,
                    "inside_run_directory": True, "sha256": file_sha256(p),
                    "size_bytes": p.stat().st_size}
                   for p in sorted(root.iterdir()) if p.name != "run_manifest.json"]
    manifest = {"run_id": run_id, "status": "finished", "managed_run_completed": True,
                "outputs_complete": True, "results": descriptors}
    # Explicitly owned temporary fixture, not a research artifact.
    (root/"run_manifest.json").write_text(json.dumps(manifest))
    return file_sha256(root/"run_manifest.json")


class HumanEarlyPlanTests(unittest.TestCase):
    def setUp(self):
        self.bank = fixture_bank()
        self.plan = plans.build_plan(self.bank, source_manifest_sha256="a" * 64,
            source_run_id="synthetic-source", source_task_bank_hash=canonical_hash(self.bank),
            component_hashes={key: "b" * 64 for key in plans.COMPONENT_PATHS})

    def test_fixed_order_k1_and_distinct_prompt_units(self):
        plans.validate_plan(self.plan)
        order = sorted(self.bank, key=lambda t: (canonical_hash([
            plans.ACQUISITION_PROTOCOL_ID, 20260909, t["task_id"]]), t["task_id"]))
        self.assertEqual([s["task_id"] for s in self.plan["samples"]], [t["task_id"] for t in order])
        self.assertEqual(self.plan["planned_logical_requests"], 195)
        self.assertTrue(all(s["replicate"] == 1 for s in self.plan["samples"]))
        self.assertEqual(self.plan["units"]["historical_human_participants"], 39)
        self.assertEqual(self.plan["units"]["historical_peer_groups"], 13)
        self.assertEqual(self.plan["units"]["distinct_prompt_utf8_hashes"], 5)
        self.assertEqual(self.plan["units"]["distinct_state_semantic_hashes"], 5)
        self.assertNotEqual([s["task_id"] for s in self.plan["samples"]], [t["task_id"] for t in self.bank])
        self.assertEqual(self.bank, self.plan["tasks"])
        self.assertEqual(self.plan["real_request"], entry.REQUEST)

    def test_rehashed_order_request_or_prompt_tampering_is_rejected(self):
        for target in ("order", "request", "prompt"):
            bad = deepcopy(self.plan)
            if target == "order":
                bad["samples"].reverse()
                bad["sample_order_hash"] = canonical_hash(bad["samples"])
            elif target == "request":
                bad["real_request"]["temperature"] = 0
            else:
                bad["tasks"][0]["prompt"] += "Altered task"
                bad["source_task_bank_hash"] = canonical_hash(bad["tasks"])
            bad["plan_hash"] = canonical_hash({k: v for k, v in bad.items() if k != "plan_hash"})
            with self.subTest(target=target), self.assertRaises(ValueError):
                plans.validate_plan(bad)

    def test_wrong_window_count_groups_bank_hash_fail_closed(self):
        for bad in (self.bank[:-1], self.bank[:-1] + [self.bank[0]]):
            with self.assertRaises(ValueError):
                plans.build_plan(bad, source_manifest_sha256="a"*64, source_run_id="fixture",
                    source_task_bank_hash=canonical_hash(bad), component_hashes=self.plan["component_hashes"])
        bad = deepcopy(self.bank)
        bad[0]["source_origin"]["source_condition"] = "spt1"
        with self.assertRaises(ValueError):
            plans.build_plan(bad, source_manifest_sha256="a"*64, source_run_id="fixture",
                source_task_bank_hash=canonical_hash(bad), component_hashes=self.plan["component_hashes"])
        with self.assertRaises(ValueError):
            plans.build_plan(self.bank, source_manifest_sha256="a"*64, source_run_id="fixture",
                source_task_bank_hash="0"*64, component_hashes=self.plan["component_hashes"])

    def test_public_plan_has_no_individual_prompts_labels_or_source_person_ids(self):
        public = plans.public_plan(self.plan)
        for forbidden in ("SYNTHETIC_SUBJECT", "source_origin", "human_action", "own_prior_trades",
                          "signed_quantities", "current_prices_talers", "private_reasoning"):
            self.assertNotIn(forbidden, json.dumps(public))
        self.assertEqual(public["sample_order_hash"], canonical_hash(public["samples"]))
        self.assertEqual(len(public["samples"]), 195)

    def test_confusion_counts_recalls_and_absent_classes_use_valid_denominators(self):
        tasks = self.bank[:5]
        null = [{"task_id": t["task_id"], "signed_quantities": dict.fromkeys("abcdef", 0)} for t in tasks]
        result = plans.action_classification(tasks, null)
        self.assertEqual(result["counts"], {
            "buy": {"buy": 0, "hold": 2, "sell": 0},
            "hold": {"buy": 0, "hold": 25, "sell": 0},
            "sell": {"buy": 0, "hold": 3, "sell": 0}})
        self.assertEqual(result["class_recall"], {"buy": 0, "hold": 1, "sell": 0})
        self.assertEqual(result["macro_recall_over_observed_human_classes"], 1/3)
        first = plans.action_classification(tasks, null[:1])
        self.assertEqual(first["human_class_support"], {"buy": 1, "hold": 5, "sell": 0})
        self.assertIsNone(first["class_recall"]["sell"])
        self.assertEqual(first["macro_recall_over_observed_human_classes"], 0.5)
        empty = plans.action_classification(tasks, [])
        self.assertEqual(empty["stock_opportunities"], 0)
        self.assertTrue(all(value is None for value in empty["class_recall"].values()))
        self.assertIsNone(empty["macro_recall_over_observed_human_classes"])
        with self.assertRaises(ValueError):
            plans.action_classification(tasks, null[:1] * 2)


class HumanEarlyTeacherManagedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="human-early-teacher-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root/"source"
        self.source.mkdir()
        self.out = self.root/"out"
        self.bank = fixture_bank()
        bank_hash = canonical_hash(self.bank)
        write_json_exclusive(self.source/"private_human_early_tasks.json", self.bank, private=True)
        prompts = [{k: t[k] for k in ("schema", "task_id", "protocol", "state", "prompt")} for t in self.bank]
        write_json_exclusive(self.source/"private_human_early_prompts.json", prompts, private=True)
        write_json_exclusive(self.source/"human_early_catalog.json", {"task_bank_hash": bank_hash})
        write_json_exclusive(self.source/"summary.json", {"task": "human-early", "status": "finished"})
        write_json_exclusive(self.source/"diagnostic_result.json", {
            "source_audit": {"status": "input_audit_passed"}, "task_bank_hash": bank_hash})
        self.source_pin = seal_fixture(self.source)
        patched = patch.object(entry, "SOURCE_MANIFEST_SHA256", self.source_pin)
        patched.start()
        self.addCleanup(patched.stop)
        self.calls = 0

    def invoke(self, arguments):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), \
                patch("socket.create_connection", side_effect=AssertionError("network forbidden")), \
                patch("socket.socket.connect", side_effect=AssertionError("network forbidden")), \
                patch("socket.socket.connect_ex", side_effect=AssertionError("network forbidden")):
            # asyncio uses a local socketpair for wakeups; forbid outbound
            # connection operations without breaking its local event loop.
            entry.main(arguments)

    def plan(self, *extra, run_id="fixture-plan"):
        self.invoke(["--task", "plan", "--source-run", str(self.source),
                     "--source-manifest-sha256", entry.SOURCE_MANIFEST_SHA256,
                     "--out", str(self.out), "--run-id", run_id, *extra])
        return self.out/"runs"/run_id

    def acquire(self, plan, *extra, run_id="fixture-acquisition"):
        self.invoke(["--task", "acquire", "--plan-run", str(plan),
                     "--plan-manifest-sha256", file_sha256(plan/"run_manifest.json"),
                     "--out", str(self.out), "--run-id", run_id, *extra])
        return self.out/"runs"/run_id

    def test_plan_is_managed_pinned_private_and_source_unchanged(self):
        before = verify_run(self.source)
        with patch.object(entry, "FakeHumanTeacher", side_effect=AssertionError("Provider")), \
                patch.object(entry, "OpenAITeacherProvider", side_effect=AssertionError("Provider")):
            run = self.plan()
        self.assertEqual((run/"private_teacher_plan.json").stat().st_mode & 0o777, 0o600)
        plan = read_json(run/"private_teacher_plan.json")
        plans.validate_plan(plan)
        self.assertEqual(read_json(run/"teacher_plan.json"), plans.public_plan(plan))
        self.assertEqual(read_json(run/"plan_summary.json")["attempted_logical_requests"], 0)
        self.assertEqual(before, verify_run(self.source))
        verify_run(run)

    def test_help_and_version_create_no_output(self):
        for option in ("--help", "--version"):
            with self.assertRaises(SystemExit) as exit:
                self.invoke([option, "--out", str(self.out)])
            self.assertEqual(exit.exception.code, 0)
        self.assertFalse(self.out.exists())

    def test_fake_acquisition_independent_prompts_private_results_and_honest_n(self):
        plan_run = self.plan()
        frozen = read_json(plan_run/"private_teacher_plan.json")
        by_id = {t["task_id"]: t for t in frozen["tasks"]}
        captured = []
        original = entry.FakeHumanTeacher.complete_many
        async def spy(provider, prompts, **kwargs):
            captured.extend(prompts)
            return await original(provider, prompts, **kwargs)
        with patch.object(entry.FakeHumanTeacher, "complete_many", spy), \
                patch.object(entry, "OpenAITeacherProvider", side_effect=AssertionError("real Provider")):
            run = self.acquire(plan_run)
        self.assertEqual(captured, [(frozen["system_prompt"], by_id[s["task_id"]]["prompt"]) for s in frozen["samples"]])
        for system, prompt in captured:
            self.assertNotIn("SYNTHETIC_SUBJECT", prompt)
            self.assertNotIn("human_action", prompt)
            self.assertNotIn("source_origin", prompt)
            self.assertEqual(system, frozen["system_prompt"])
        result = read_json(run/"teacher_summary.json")
        self.assertEqual(result["attempted_logical_requests"], 195)
        self.assertEqual(result["resolved_logical_requests"], 195)
        self.assertEqual(result["valid_joint_decisions"], 195)
        self.assertEqual(result["physical_endpoint_attempts"], 0)
        self.assertEqual(result["synthetic_application_attempts"], 195)
        self.assertEqual(result["new_teacher_logical_requests"], 0)
        self.assertEqual(result["new_human_participants"], 0)
        self.assertEqual(result["comparison"]["paired_valid_comparison"], result["hold_null_baseline"]["paired_valid_comparison"])
        self.assertFalse(result["human_likeness_validated"])
        for path in run.iterdir():
            if path.name.startswith("private_"):
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            elif path.suffix in (".json", ".jsonl"):
                for forbidden in ("fake null control", "SYNTHETIC_SUBJECT", '"signed_quantities"', '"private_reasoning"'):
                    self.assertNotIn(forbidden, path.read_text())
        self.assertIn("lifecycle_only", read_json(run/"run_manifest.json")["legacy_config_scope"])
        verify_run(run)

    def test_dry_acquisition_constructs_neither_provider(self):
        plan = self.plan()
        with patch.object(entry, "FakeHumanTeacher", side_effect=AssertionError("Provider")), \
                patch.object(entry, "OpenAITeacherProvider", side_effect=AssertionError("Provider")):
            run = self.acquire(plan, "--dry-run")
        self.assertFalse((run/"private_teacher_raw.jsonl").exists())
        self.assertEqual(read_json(run/"plan_summary.json")["new_teacher_logical_requests"], 0)

    def test_real_dry_run_constructs_no_provider_and_acquisition_is_not_new_plan(self):
        plan = self.plan()
        with patch.object(entry, "FakeHumanTeacher", side_effect=AssertionError("Provider")), \
                patch.object(entry, "OpenAITeacherProvider", side_effect=AssertionError("Provider")):
            run = self.acquire(plan, "--provider", "openai", "--dry-run")
            with self.assertRaisesRegex(ValueError, "not a planning run"):
                self.acquire(run, run_id="no-chained-acquisition")

    def test_real_alias_finish_and_retry_gates_with_fake_transport_only(self):
        plan = self.plan()
        constructor = []
        original = entry.FakeHumanTeacher.complete_many
        class FakeLiveTransport(entry.FakeHumanTeacher):
            model = "MiniMax-M2.7"
            def __init__(self, **kwargs):
                super().__init__()
                constructor.append(kwargs)
            async def complete_many(self, prompts, **kwargs):
                callback = kwargs["on_completion"]
                def wrapped(index, value):
                    value = replace(value, reported_model="HiggsAI")
                    if self.request_count == 1:
                        value = replace(value, reported_model="unapproved-alias")
                    if self.request_count == 2:
                        value = replace(value, finish_reason="length")
                    if self.request_count == 3:
                        value = replace(value, application_attempt_count=2, technical_retry_count=1)
                    callback(index, value)
                kwargs["on_completion"] = wrapped
                return await original(self, prompts, **kwargs)
        # The real constructor and all outbound socket connections are replaced
        # by fixtures. These 195 callbacks are not real endpoint observations.
        with patch.object(entry, "OpenAITeacherProvider", FakeLiveTransport):
            run = self.acquire(plan, "--provider", "openai", "--live", "--confirm-request-count", "195")
        self.assertEqual(constructor, [{**entry.REQUEST, "workers": 2, "application_max_attempts": 1,
                                       "request_timeout_seconds": 7200, "hard_request_deadline_seconds": 7200}])
        result = read_json(run/"teacher_summary.json")
        self.assertEqual(result["valid_joint_decisions"], 192)
        self.assertEqual(result["failure_counts"], {"reported_model_mismatch": 1, "incomplete_termination": 1, "unexpected_retry": 1})
        self.assertEqual(result["teacher_paired_valid_action_classification"]["joint_decisions"], 192)
        self.assertEqual(result["teacher_paired_valid_action_classification"], result["hold_paired_valid_action_classification"])

    def test_live_count_and_mode_gates_precede_provider(self):
        plan = self.plan()
        cases = [('--provider', 'openai'), ('--provider', 'openai', '--live'),
                 ('--provider', 'openai', '--live', '--confirm-request-count', '194'),
                 ('--live', '--confirm-request-count', '195'),
                 ('--provider', 'openai', '--live', '--dry-run', '--confirm-request-count', '195'),
                 ('--workers', '0'), ('--workers', '5'), ('--replicates', '2'),
                 ('--confirm-request-count', '195')]
        for i, flags in enumerate(cases):
            with self.subTest(flags=flags), patch.object(entry, "OpenAITeacherProvider", side_effect=AssertionError("Provider")):
                with self.assertRaises(SystemExit):
                    self.acquire(plan, *flags, run_id=f"gate-{i}")
                manifest = read_json(self.out/f"runs/gate-{i}/run_manifest.json")
                self.assertEqual(manifest["status"], "failed")
                self.assertEqual(manifest["honest_n"], 0)

    def test_wrong_source_pin_and_artifact_are_rejected(self):
        with self.assertRaises(SystemExit):
            self.invoke(["--task", "plan", "--source-run", str(self.source),
                         "--source-manifest-sha256", "0"*64, "--out", str(self.out), "--run-id", "bad-pin"])
        with (self.source/"private_human_early_tasks.json").open("a") as stream:
            stream.write(" ")
        with self.assertRaises(ValueError):
            self.plan(run_id="bad-artifact")

    def test_source_bank_semantic_hash_is_independently_checked(self):
        path = self.source/"diagnostic_result.json"
        result = read_json(path)
        result["task_bank_hash"] = "0" * 64
        path.write_text(json.dumps(result))
        pin = seal_fixture(self.source)
        with patch.object(entry, "SOURCE_MANIFEST_SHA256", pin), self.assertRaisesRegex(ValueError, "bank hash"):
            self.plan()

    def test_plan_pin_components_and_artifacts_are_checked_before_provider(self):
        plan = self.plan()
        with patch.object(entry, "FakeHumanTeacher", side_effect=AssertionError("Provider")):
            with self.assertRaises(ValueError):
                self.invoke(["--task", "acquire", "--plan-run", str(plan), "--plan-manifest-sha256", "0"*64,
                             "--out", str(self.out), "--run-id", "bad-plan-pin"])
            modified = entry._components()
            modified[next(iter(modified))] = "0"*64
            with patch.object(entry, "_components", return_value=modified), self.assertRaisesRegex(ValueError, "components changed"):
                self.acquire(plan, run_id="changed-components")

    def test_exclusive_run_and_output_inside_source_rejected(self):
        plan = self.plan()
        before = verify_run(plan)
        with self.assertRaises(FileExistsError):
            self.plan()
        self.assertEqual(before, verify_run(plan))
        with self.assertRaises(SystemExit):
            self.invoke(["--task", "plan", "--source-run", str(self.source),
                         "--source-manifest-sha256", self.source_pin, "--out", str(self.source/"child"),
                         "--run-id", "overlap"])
        self.assertFalse((self.source/"child").exists())

    def test_raw_is_persisted_before_parser_and_secret_redacted_with_received_hash(self):
        plan = self.plan()
        original_complete = entry.FakeHumanTeacher.complete_many
        original_parse = entry.parse_joint_response
        secret = "SYNTHETIC_SECRET_5ba4c"  # engineering fixture, not a credential
        received = []
        async def completion_with_secret(provider, prompts, **kwargs):
            original_callback = kwargs["on_completion"]
            def wrapped(index, value):
                raw = value.raw_response.replace("fake null control", secret)
                received.append(raw)
                original_callback(index, replace(value, raw_response=raw, provider_sdk_response_json=secret))
            kwargs["on_completion"] = wrapped
            return await original_complete(provider, prompts, **kwargs)
        def assert_persisted(raw, task):
            path = self.out/"runs/fixture-acquisition/private_teacher_raw.jsonl"
            line = json.loads(path.read_text().splitlines()[-1])
            self.assertEqual(line["raw_response_utf8_sha256"], hashlib.sha256(raw.encode()).hexdigest())
            self.assertTrue(line["persisted_payload_secret_redacted"])
            self.assertNotIn(secret, path.read_text())
            self.assertIn("<redacted>", line["completion"]["raw_response"])
            return original_parse(raw, task)
        with patch.dict("os.environ", {"OPENAI_API_KEY": secret}), \
                patch.object(entry.FakeHumanTeacher, "complete_many", completion_with_secret), \
                patch.object(entry, "parse_joint_response", side_effect=assert_persisted):
            run = self.acquire(plan)
        self.assertEqual(len(received), 195)
        for path in run.iterdir():
            if path.is_file():
                self.assertNotIn(secret, path.read_text())

    def test_parse_failure_gets_same_valid_subset_null_not_full_denominator(self):
        plan = self.plan()
        original = entry.parse_joint_response
        first_id = read_json(plan/"private_teacher_plan.json")["samples"][0]["task_id"]
        def fail_one(raw, task):
            if task["task_id"] == first_id:
                raise ValueError("synthetic parse failure")
            return original(raw, task)
        with patch.object(entry, "parse_joint_response", side_effect=fail_one):
            run = self.acquire(plan)
        summary = read_json(run/"teacher_summary.json")
        self.assertEqual(summary["valid_joint_decisions"], 194)
        self.assertEqual(summary["failed_joint_decisions"], 1)
        self.assertEqual(summary["comparison"]["paired_valid_comparison"]["denominator_joint_decisions"], 194)
        self.assertEqual(summary["hold_null_on_teacher_valid_tasks"]["paired_valid_comparison"]["denominator_joint_decisions"], 194)
        self.assertEqual(summary["hold_null_baseline"]["paired_valid_comparison"]["denominator_joint_decisions"], 195)
        self.assertEqual(summary["failure_counts"], {"invalid_joint_response": 1})
        self.assertEqual(summary["teacher_paired_valid_action_classification"]["stock_opportunities"], 194*6)
        self.assertEqual(summary["teacher_paired_valid_action_classification"], summary["hold_paired_valid_action_classification"])

    def test_provider_failure_batch_preserves_unattempted_slots_without_hold(self):
        plan = self.plan()
        original = entry.FakeHumanTeacher.complete_many
        async def fail_batch(provider, prompts, **kwargs):
            callback = kwargs["on_completion"]
            kwargs["on_completion"] = lambda index, value: callback(index,
                replace(value, raw_response=None, error_type="SyntheticTransportError", error_detail="PRIVATE_TRANSPORT_DETAIL"))
            return await original(provider, prompts, **kwargs)
        with patch.object(entry.FakeHumanTeacher, "complete_many", fail_batch), self.assertRaises(RuntimeError):
            self.acquire(plan)
        run = self.out/"runs/fixture-acquisition"
        summary = read_json(run/"teacher_summary.json")
        self.assertEqual(summary["attempted_logical_requests"], 2)
        self.assertEqual(summary["unattempted_logical_requests"], 193)
        self.assertEqual(summary["valid_joint_decisions"], 0)
        self.assertEqual(summary["failed_joint_decisions"], 2)
        self.assertIsNone(summary["comparison"]["paired_valid_comparison"]["stock_action_agreement"])
        self.assertNotIn("PRIVATE_TRANSPORT_DETAIL", json.dumps(summary))
        self.assertNotIn("PRIVATE_TRANSPORT_DETAIL", (run/"run_manifest.json").read_text())
        self.assertEqual(read_json(run/"run_manifest.json")["status"], "failed")

    def test_callback_failure_drains_owned_concurrent_requests(self):
        plan = self.plan()
        finished = []
        class FakeConcurrent:
            model = entry.FAKE_MODEL
            workers = 2
            def __init__(self):
                self.batch_sizes = []
                self.application_attempt_count = self.request_count = self.response_count = 0
            async def _one(self, index, system, user, semaphore, before, audit):
                before(index)
                self.request_count += 1
                self.application_attempt_count += 1
                await asyncio.sleep(0.01 * index)
                audit(index, {"application_attempt_index": 1, "retry_scheduled": False, "status": "response_received"})
                self.response_count += 1
                finished.append(index)
                return index, entry.TeacherCompletion(raw_response=json.dumps({"signed_quantities": dict.fromkeys("abcdef", 0),
                    "private_reasoning": "fake"}), reported_model=self.model, finish_reason="stop",
                    input_tokens=0, output_tokens=0, response_id=None)
            async def aclose(self):
                return None
        with patch.object(entry, "FakeHumanTeacher", FakeConcurrent), \
                patch.object(entry, "parse_joint_response", side_effect=RecursionError("PRIVATE_CALLBACK_DETAIL")), \
                self.assertRaises(RuntimeError):
            self.acquire(plan)
        run = self.out/"runs/fixture-acquisition"
        self.assertEqual(finished, [0, 1])
        raw = [json.loads(line) for line in (run/"private_teacher_raw.jsonl").read_text().splitlines()]
        self.assertEqual(sum("completion" in row for row in raw), 2)
        summary = read_json(run/"teacher_summary.json")
        self.assertEqual(summary["unresolved_attempted_logical_requests"], 2)
        self.assertEqual(summary["valid_joint_decisions"], 0)
        self.assertNotIn("PRIVATE_CALLBACK_DETAIL", json.dumps(summary))
        self.assertNotIn("PRIVATE_CALLBACK_DETAIL", (run/"run_manifest.json").read_text())


if __name__ == "__main__":
    unittest.main()
