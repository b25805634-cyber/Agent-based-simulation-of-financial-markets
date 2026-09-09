"""Managed frozen K=1 Teacher comparison on reconstructed six-asset human tasks."""
from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import sys
from typing import Sequence

from experiments.information_market import _append
from experiments.information_weight_teacher import _safe_private
from experiments.rollout_fidelity import (
    OpenAITeacherProvider, TeacherCompletion, REQUEST, _complete_batch,
    FakeNullTeacher as _RolloutFakeNullTeacher,
)
from nmsim.config import Config
from nmsim.human_early_acquisition import (
    ACQUISITION_PROTOCOL_ID, COMPONENT_PATHS, REAL_REQUEST, REQUEST_COUNT,
    SOURCE_MANIFEST_SHA256, build_plan, validate_plan, public_plan, action_classification,
)
from nmsim.human_early_tasks import (
    parse_joint_response, hold_baseline_predictions, evaluate_joint_predictions,
)
from nmsim.information_artifacts import (
    canonical_hash, file_sha256, read_json, verify_run, assert_unchanged,
    write_json_exclusive, ensure_separate_output, preflight_output_separation,
)
from nmsim.managed_cli import RaisingArgumentParser, bootstrap_cli, fail_cli, BootstrapCLIError
from nmsim.run_context import ManagedRunContext


SCHEMA = "human-early-teacher-experiment/0.1"
COMMAND = "python -m experiments.human_early_teacher"
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = "results_human_early_teacher"
FAKE_MODEL = "fake-human-early-hold-null"


def parser():
    result = RaisingArgumentParser(description=__doc__, allow_abbrev=False)
    result.add_argument("--task", choices=("plan", "acquire"), default="plan")
    result.add_argument("--source-run", type=Path)
    result.add_argument("--source-manifest-sha256")
    result.add_argument("--plan-run", type=Path)
    result.add_argument("--plan-manifest-sha256")
    result.add_argument("--provider", choices=("fake_test_teacher", "openai"), default="fake_test_teacher")
    result.add_argument("--live", action="store_true")
    result.add_argument("--confirm-request-count", type=int)
    result.add_argument("--workers", type=int, default=2)
    result.add_argument("--dry-run", action="store_true")
    result.add_argument("--out", default=DEFAULT_OUT)
    result.add_argument("--run-id")
    result.add_argument("--version", action="version", version=SCHEMA)
    return result


def _validate(args):
    if args.task == "plan":
        if args.source_run is None or args.source_manifest_sha256 != SOURCE_MANIFEST_SHA256:
            raise ValueError("--source-run and the exact frozen --source-manifest-sha256 are required")
        if args.plan_run is not None or args.plan_manifest_sha256 is not None:
            raise ValueError("--plan-run belongs to acquisition")
    else:
        if args.plan_run is None or not re.fullmatch(r"[0-9a-f]{64}", args.plan_manifest_sha256 or ""):
            raise ValueError("--plan-run requires its explicit --plan-manifest-sha256")
        if args.source_run is not None or args.source_manifest_sha256 is not None:
            raise ValueError("acquisition accepts only the frozen --plan-run")
    if not 1 <= args.workers <= 4:
        raise ValueError("--workers must be between 1 and 4")
    if args.live and (args.task != "acquire" or args.provider != "openai" or args.dry_run):
        raise ValueError("--live requires a real acquisition, never plan or dry-run")
    if args.provider == "openai" and args.task == "acquire" and not args.dry_run and not args.live:
        raise ValueError("real acquisition requires --live")
    if args.live and args.confirm_request_count != REQUEST_COUNT:
        raise ValueError("--confirm-request-count must exactly equal 195")
    if args.confirm_request_count is not None and not args.live:
        raise ValueError("--confirm-request-count is only accepted with --live")


def _components():
    return {name: file_sha256(ROOT/name) for name in COMPONENT_PATHS}


def _source_plan(args, receipt):
    required = {"private_human_early_tasks.json", "private_human_early_prompts.json",
                "human_early_catalog.json", "diagnostic_result.json", "summary.json"}
    if not required <= set(receipt["registered_artifact_paths"]):
        raise ValueError("source is missing registered early-task artifacts")
    summary = read_json(args.source_run/"summary.json")
    result = read_json(args.source_run/"diagnostic_result.json")
    catalog = read_json(args.source_run/"human_early_catalog.json")
    if summary.get("task") != "human-early" or summary.get("status") != "finished":
        raise ValueError("source is not a finished human-early task export")
    if result.get("source_audit", {}).get("status") != "input_audit_passed":
        raise ValueError("source human audit did not pass")
    tasks = read_json(args.source_run/"private_human_early_tasks.json")
    if canonical_hash(tasks) != result.get("task_bank_hash") or catalog.get("task_bank_hash") != result.get("task_bank_hash"):
        raise ValueError("source bank hash mismatch")
    prompts = read_json(args.source_run/"private_human_early_prompts.json")
    if prompts != [{key: task[key] for key in ("schema", "task_id", "protocol", "state", "prompt")} for task in tasks]:
        raise ValueError("source prompt bank differs from task bank")
    return build_plan(tasks, source_manifest_sha256=receipt["manifest_sha256"],
                      source_run_id=receipt["run_id"], source_task_bank_hash=result["task_bank_hash"],
                      component_hashes=_components())


class FakeHumanTeacher(_RolloutFakeNullTeacher):
    """Sequential callback fixture; no socket, model endpoint or human inference."""
    model = FAKE_MODEL

    async def complete_many(self, prompts, *, before_attempt, on_application_attempt, on_completion, **kwargs):
        self.batch_sizes.append(len(prompts))
        for index, _ in enumerate(prompts):
            before_attempt(index)
            self.request_count += 1
            self.application_attempt_count += 1
            on_application_attempt(index, {"application_attempt_index": 1, "status": "response_received",
                                          "retry_scheduled": False, "retryable": False})
            self.response_count += 1
            on_completion(index, TeacherCompletion(
                raw_response=json.dumps({"signed_quantities": dict.fromkeys("abcdef", 0),
                                         "private_reasoning": "fake null control"}),
                reported_model=self.model, input_tokens=0, output_tokens=0,
                response_id=None, finish_reason="stop"))


def _public_comparison(comparison):
    return {key: value for key, value in comparison.items() if key != "failures"}


async def acquire(context, args, plan):
    """All raw payloads are fsynced privately before validation or parsing."""
    real = args.provider == "openai"
    tasks = {task["task_id"]: task for task in plan["tasks"]}
    attempted, completed, audited = set(), set(), set()
    rows, predictions = [], []
    provider = None
    streams = []
    fatal = False
    raw_response_count = 0
    stopped_early = False
    try:
        for name in ("private_teacher_raw.jsonl", "private_teacher_decisions.jsonl", "private_teacher_attempts.jsonl"):
            streams.append(os.fdopen(os.open(context.run_dir/name, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600), "w", encoding="utf-8"))
        raw_stream, decision_stream, attempt_stream = streams
        provider = (OpenAITeacherProvider(**REQUEST, workers=args.workers, application_max_attempts=1,
                        request_timeout_seconds=7200, hard_request_deadline_seconds=7200)
                    if real else FakeHumanTeacher())
        context.active_llm = provider
        context.llm_mode = "record"
        context.register_llm_runtime(provider=args.provider, model=provider.model,
            mode="human_early_teacher_k1", cache_enabled=False, network_access=False, provider_calls=0,
            application_max_attempts=1, sdk_retry_count=0, shared_conversations=False)
        context.set_stage("simulation")
        for base in range(0, REQUEST_COUNT, args.workers):
            group = plan["samples"][base:base+args.workers]
            resolved_group = []

            def slot(index):
                if type(index) is not int or not 0 <= index < len(group):
                    raise ValueError("callback index outside active batch")
                return group[index]

            def before(index):
                sample = slot(index)
                if sample["task_id"] in attempted:
                    raise ValueError("duplicate logical request")
                attempted.add(sample["task_id"])
                context.network_access = real
                context.register_llm_runtime(network_access=real)
                context.events.emit("LLMRequestRecorded", data={"request_index": sample["request_index"],
                    "prompt_utf8_sha256": sample["prompt_utf8_sha256"]})

            def audit(index, event):
                sample = slot(index)
                _append(attempt_stream, {"request_index": sample["request_index"], "task_id": sample["task_id"],
                                         "audit": _safe_private(context, dict(event))})
                if (sample["task_id"] not in attempted or sample["task_id"] in audited
                        or event.get("application_attempt_index") != 1
                        or event.get("retry_scheduled") is not False
                        or event.get("status") not in {"response_received", "provider_exception"}):
                    raise ValueError("attempt callback violates no-retry plan")
                audited.add(sample["task_id"])

            def completion(index, value):
                nonlocal raw_response_count
                sample = slot(index)
                # Preserve the secret-redacted text/SDK envelope privately first;
                # no parser, alias gate, or public callback precedes this fsync.
                _append(raw_stream, {"task_id": sample["task_id"], "request_index": sample["request_index"],
                                     "raw_response_utf8_sha256": hashlib.sha256(value.raw_response.encode()).hexdigest()
                                         if isinstance(value.raw_response, str) else None,
                                     "persisted_payload_secret_redacted": True,
                                     "completion": _safe_private(context, asdict(value))})
                if sample["task_id"] not in attempted or sample["task_id"] in completed:
                    raise ValueError("unattempted or duplicate completion")
                completed.add(sample["task_id"])
                if isinstance(value.raw_response, str):
                    raw_response_count += 1
                    context.events.emit("LLMResponseRecorded", data={"request_index": sample["request_index"], "source": "record"})
                failure = None
                parsed = None
                if value.error_type is not None or not isinstance(value.raw_response, str):
                    failure = "provider_failure"
                elif value.finish_reason != "stop":
                    failure = "incomplete_termination"
                elif value.reported_model != ("HiggsAI" if real else provider.model):
                    failure = "reported_model_mismatch"
                elif value.application_attempt_count != 1 or value.technical_retry_count != 0:
                    failure = "unexpected_retry"
                else:
                    try:
                        parsed = parse_joint_response(value.raw_response, tasks[sample["task_id"]])
                    except (ValueError, TypeError):
                        failure = "invalid_joint_response"
                row = {"task_id": sample["task_id"], "request_index": sample["request_index"],
                       "status": "valid" if parsed is not None else "failed", "failure_code": failure,
                       "raw_response_utf8_sha256": hashlib.sha256(value.raw_response.encode()).hexdigest()
                           if isinstance(value.raw_response, str) else None,
                       "parsed": parsed}
                _append(decision_stream, _safe_private(context, row))
                rows.append(row)
                resolved_group.append(row)
                predictions.append({"task_id": sample["task_id"],
                                    "raw_response": value.raw_response if parsed is not None else ""})
                if parsed is not None:
                    context.events.emit("AgentDecisionParsed", data={"decision_response_schema": SCHEMA,
                                        "parse_status": "ok", "terminal_status": "valid"})
                context.manifest["human_early_teacher_progress"] = {
                    "planned_logical_requests": REQUEST_COUNT, "attempted_logical_requests": len(attempted),
                    "resolved_logical_requests": len(rows), "raw_text_responses": raw_response_count,
                    "valid_joint_decisions": sum(r["status"] == "valid" for r in rows),
                    "application_attempts": provider.application_attempt_count,
                    "physical_endpoint_attempts": provider.application_attempt_count if real else 0}
                context._write()

            prompts = [(plan["system_prompt"], tasks[sample["task_id"]]["prompt"]) for sample in group]
            await _complete_batch(provider, prompts, before_attempt=before,
                                  on_application_attempt=audit, on_completion=completion)
            if len(resolved_group) != len(group):
                raise ValueError("incomplete batch completion callbacks")
            if all(row["failure_code"] == "provider_failure" for row in resolved_group):
                stopped_early = True
                break
    except BaseException as error:
        fatal = True
        if streams and not streams[0].closed:
            try:
                _append(streams[0], _safe_private(context, {"kind": "private_acquisition_error", "type": type(error).__name__, "detail": str(error)}))
            except BaseException:
                pass
    finally:
        if provider is not None:
            try:
                await provider.aclose()
            except BaseException as error:
                fatal = True
                if streams and not streams[0].closed:
                    try:
                        _append(streams[0], _safe_private(context, {"kind": "private_close_error", "type": type(error).__name__, "detail": str(error)}))
                    except BaseException:
                        pass
        for stream in streams:
            try:
                stream.close()
            except BaseException:
                fatal = True
    comparison = evaluate_joint_predictions(plan["tasks"], predictions)
    baseline = evaluate_joint_predictions(plan["tasks"], hold_baseline_predictions(plan["tasks"]))
    valid_ids = {row["task_id"] for row in rows if row["status"] == "valid"}
    paired_tasks = [task for task in plan["tasks"] if task["task_id"] in valid_ids]
    paired_baseline = evaluate_joint_predictions(paired_tasks, hold_baseline_predictions(paired_tasks))
    accepted = [{"task_id": row["task_id"], "signed_quantities": row["parsed"]["public_decision"]["signed_quantities"]}
                for row in rows if row["status"] == "valid"]
    paired_null = [{"task_id": task["task_id"], "signed_quantities": dict.fromkeys("abcdef", 0)} for task in paired_tasks]
    attempts = getattr(provider, "application_attempt_count", 0)
    summary = {"schema": SCHEMA, "plan_hash": plan["plan_hash"], "source_kind": args.provider,
        "units": plan["units"], "planned_logical_requests": REQUEST_COUNT,
        "attempted_logical_requests": len(attempted), "resolved_logical_requests": len(rows),
        "unattempted_logical_requests": REQUEST_COUNT-len(attempted),
        "unresolved_attempted_logical_requests": len(attempted)-len(rows),
        "raw_text_responses": raw_response_count,
        "valid_joint_decisions": comparison["valid_joint_predictions"],
        "failed_joint_decisions": comparison["invalid_joint_predictions"],
        "application_attempts": attempts, "physical_endpoint_attempts": attempts if real else 0,
        "synthetic_application_attempts": 0 if real else attempts,
        "terminal_attempt_audits": len(audited),
        "new_teacher_logical_requests": len(attempted) if real else 0,
        "valid_endpoint_responses": comparison["valid_joint_predictions"] if real else 0,
        "new_human_participants": 0, "new_market_runs": 0,
        "failure_counts": dict(sorted(Counter(row["failure_code"] for row in rows if row["failure_code"]).items())),
        "callback_or_runtime_failure": fatal, "stopped_after_provider_failure_batch": stopped_early,
        "comparison": _public_comparison(comparison),
        "teacher_paired_valid_action_classification": action_classification(plan["tasks"], accepted),
        "hold_paired_valid_action_classification": action_classification(paired_tasks, paired_null),
        "hold_null_baseline": {**_public_comparison(baseline), "prediction_source": "synthetic_hold_null_not_Teacher"},
        "hold_null_on_teacher_valid_tasks": {**_public_comparison(paired_baseline),
            "prediction_source": "synthetic_hold_null_on_same_paired_valid_tasks",
            "paired_task_ids_semantic_hash": canonical_hash(sorted(valid_ids))},
        "human_likeness_validated": False, "K1_estimates_per_state_distribution": False,
        "K1_is_independent_human_population": False,
        "status": "complete" if not fatal and len(rows) == REQUEST_COUNT else "incomplete"}
    write_json_exclusive(context.run_dir/"private_comparison.json", comparison, private=True)
    write_json_exclusive(context.run_dir/"teacher_summary.json", summary)
    context.manifest["human_early_teacher_honest_n"] = {key: value for key, value in summary.items()
        if key not in {"comparison", "hold_null_baseline", "hold_null_on_teacher_valid_tasks",
                       "teacher_paired_valid_action_classification", "hold_paired_valid_action_classification"}}
    context._write()
    if summary["status"] != "complete":
        raise RuntimeError("human early Teacher acquisition incomplete; private evidence preserved; no resume")
    return summary


def main(argv: Sequence[str] | None = None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        preflight_output_separation(argv, DEFAULT_OUT)
    except ValueError:
        print("provenance_not_created_reason=output_overlaps_historical_input", file=sys.stderr)
        raise SystemExit(2)
    try:
        bootstrap = bootstrap_cli(argv, default_out=DEFAULT_OUT, command_identity=COMMAND)
    except BootstrapCLIError as error:
        fail_cli(None, error)
    try:
        args = parser().parse_args(argv)
        _validate(args)
    except (ValueError, TypeError, OSError) as error:
        fail_cli(bootstrap, error)
    input_root = args.source_run if args.task == "plan" else args.plan_run
    try:
        ensure_separate_output(Path(args.out), (input_root,))
    except ValueError:
        print("provenance_not_created_reason=output_overlaps_historical_input", file=sys.stderr)
        raise SystemExit(2)
    cfg = Config(provider="mock", n_rounds=0, n_llm_agents=0, n_noise_agents=0,
                 cache_enabled=False, openai_base_url="", openai_api_key="", out_dir=args.out)
    context = ManagedRunContext.create(cfg, out_root=args.out, run_id=args.run_id, repo_root=ROOT,
        command_identity=COMMAND, run_kind="human_early_teacher_"+args.task, planned_simulation_runs=0,
        input_paths={"source" if args.task == "plan" else "plan": input_root/"run_manifest.json"},
        research_profile={"profile_id": SCHEMA, "persona_contract": {"applicable": False},
                          "prompt_contract": {"protocol_id": ACQUISITION_PROTOCOL_ID}})
    with context:
        context.set_stage("config_validation")
        context.register_llm_runtime(provider="none", mode="human_early_plan", network_access=False, provider_calls=0)
        receipt = verify_run(input_root, args.source_manifest_sha256 if args.task == "plan" else args.plan_manifest_sha256)
        if REQUEST != REAL_REQUEST:
            raise ValueError("shared Teacher request changed; freeze a new protocol")
        if args.task == "plan":
            plan = _source_plan(args, receipt)
        else:
            if not {"private_teacher_plan.json", "teacher_plan.json", "plan_summary.json"} <= set(receipt["registered_artifact_paths"]):
                raise ValueError("acquisition requires a completed registered plan run")
            source_manifest = read_json(input_root/"run_manifest.json")
            source_context = source_manifest.get("managed_context", {})
            if (source_context.get("command_identity") != COMMAND
                    or source_context.get("run_kind") != "human_early_teacher_plan"):
                raise ValueError("acquisition input is not a planning run")
            plan = read_json(input_root/"private_teacher_plan.json")
            validate_plan(plan)
            if read_json(input_root/"teacher_plan.json") != public_plan(plan):
                raise ValueError("public and private frozen plans differ")
        if plan["source_manifest_sha256"] != SOURCE_MANIFEST_SHA256 or plan["component_hashes"] != _components():
            raise ValueError("frozen source or scientific components changed")
        write_json_exclusive(context.run_dir/"private_teacher_plan.json", plan, private=True)
        write_json_exclusive(context.run_dir/"teacher_plan.json", public_plan(plan))
        write_json_exclusive(context.run_dir/"source_receipt.json", receipt)
        scientific = {"schema": SCHEMA, "protocol_id": ACQUISITION_PROTOCOL_ID,
                      "plan_hash": plan["plan_hash"], "source_task_bank_hash": plan["source_task_bank_hash"],
                      "component_hashes": plan["component_hashes"]}
        model_request = {"schema": SCHEMA, "provider": args.provider,
                         "request": REAL_REQUEST if args.provider == "openai" else {"model": FAKE_MODEL},
                         "required_reported_model": "HiggsAI" if args.provider == "openai" else FAKE_MODEL,
                         "seed_sent": False, "cache": False, "shared_conversations": False}
        if args.provider == "openai":
            from experiments.information_weight_teacher import _endpoint_identity
            model_request["endpoint_identity"] = _endpoint_identity()
        execution = {"schema": SCHEMA, "task": args.task, "workers": args.workers,
                     "dry_run": args.dry_run, "live": args.live, "python": platform.python_version(),
                     "input_manifest_sha256": receipt["manifest_sha256"], **plan["execution_policy"]}
        identity = {"schema": "human-early-teacher-identities/0.1",
                    "scientific_config_hash": canonical_hash(scientific),
                    "model_request_config_hash": canonical_hash(model_request),
                    "execution_config_hash": canonical_hash(execution)}
        identity["full_effective_config_hash"] = canonical_hash(identity)
        write_json_exclusive(context.run_dir/"identities.json", {**identity,
            "scientific_config": scientific, "model_request_config": model_request, "execution_config": execution})
        context.manifest["human_early_teacher_identities"] = identity
        context.manifest["legacy_config_scope"] = "lifecycle_only_not_human_early_teacher_science"
        context._write()
        try:
            if args.task == "acquire" and not args.dry_run:
                context.set_stage("provider_setup")
                summary = asyncio.run(acquire(context, args, plan))
            else:
                summary = {"status": "plan_only", "plan_hash": plan["plan_hash"], "units": plan["units"],
                           "planned_logical_requests": REQUEST_COUNT, "attempted_logical_requests": 0,
                           "physical_endpoint_attempts": 0, "new_teacher_logical_requests": 0,
                           "valid_endpoint_responses": 0, "new_human_participants": 0,
                           "human_likeness_validated": False}
                write_json_exclusive(context.run_dir/"plan_summary.json", summary)
        finally:
            assert_unchanged(input_root, receipt)
        context.set_stage("result_export")
        context.finish()
        print(json.dumps({"run_id": context.run_id, **summary}, ensure_ascii=False))


if __name__ == "__main__":
    main()
