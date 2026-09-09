"""Managed planning and repeated Teacher checks of available rollout states."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
from dataclasses import asdict
import json
import os
from pathlib import Path
import platform
import sys
from typing import Sequence

from experiments.information_market import _append, _load_study
from experiments.information_weight_teacher import _safe_private
from experiments.v2_attention_market import OpenAITeacherProvider, TeacherCompletion
from nmsim.config import Config
from nmsim.information_artifacts import (canonical_hash, file_sha256, read_json, verify_run,
    assert_unchanged, write_json_exclusive)
from nmsim.information_student import make_predictor
from nmsim.information_weight import P8
from nmsim.managed_cli import RaisingArgumentParser, bootstrap_cli, fail_cli, BootstrapCLIError
from nmsim.rollout_probes import build_plan, validate_plan, summarize, SAMPLE_SCHEMA
from nmsim.run_context import ManagedRunContext
from nmsim.v2_attention import V2AttentionState, parse_teacher_response
from nmsim.v2_market_experiment import _normalise_prediction


SCHEMA = "rollout-fidelity-experiment/1.0"
COMMAND = "python -m experiments.rollout_fidelity"
ROOT = Path(__file__).resolve().parents[1]
REQUEST = {"model": "MiniMax-M2.7", "temperature": 1.0, "top_p": 0.95,
           "top_k": 40, "max_tokens": 190000, "response_format": {"type": "json_object"}}


def parser():
    result = RaisingArgumentParser(description=__doc__, allow_abbrev=False)
    result.add_argument("--task", choices=("plan", "acquire"), default="plan")
    result.add_argument("--out", default="results_rollout_fidelity")
    result.add_argument("--run-id")
    result.add_argument("--market-run", type=Path)
    result.add_argument("--market-manifest-sha256")
    result.add_argument("--model-run", type=Path)
    result.add_argument("--model-manifest-sha256")
    result.add_argument("--plan-run", type=Path)
    result.add_argument("--plan-manifest-sha256")
    result.add_argument("--max-states", type=int)
    result.add_argument("--replicates", type=int)
    result.add_argument("--seed", type=int)
    result.add_argument("--provider", choices=("fake_test_teacher", "openai"), default="fake_test_teacher")
    result.add_argument("--live", action="store_true")
    result.add_argument("--confirm-request-count", type=int)
    result.add_argument("--workers", type=int, default=2)
    result.add_argument("--dry-run", action="store_true")
    result.add_argument("--version", action="version", version=SCHEMA)
    return result


def _validate(args):
    if args.task == "plan" and (args.market_run is None or args.model_run is None):
        raise ValueError("planning requires --market-run and --model-run")
    if args.task == "acquire" and args.plan_run is None:
        raise ValueError("acquisition requires --plan-run")
    if args.task == "acquire" and any(value is not None for value in
            (args.max_states, args.replicates, args.seed, args.market_run, args.model_run)):
        raise ValueError("selection and rollout/model inputs belong to --task plan; acquisition uses its frozen plan")
    if args.task == "plan" and args.plan_run is not None:
        raise ValueError("--plan-run belongs to acquisition")
    args.max_states = 24 if args.max_states is None else args.max_states
    args.replicates = 5 if args.replicates is None else args.replicates
    args.seed = 20260909 if args.seed is None else args.seed
    if args.seed < 0 or args.max_states < 1 or args.replicates < 2 or not 1 <= args.workers <= 4:
        raise ValueError("invalid probe plan or worker count")
    if args.live and (args.task != "acquire" or args.provider != "openai" or args.dry_run):
        raise ValueError("--live is only for actual OpenAI-compatible acquisition")
    if args.task == "acquire" and args.provider == "openai" and not args.dry_run and not args.live:
        raise ValueError("real acquisition requires --live")
    if args.confirm_request_count is not None and not args.live:
        raise ValueError("request confirmation is only valid for a live acquisition")


def _candidates(market_run, market_receipt, model_receipt, study):
    source = read_json(market_run/"summary.json")
    predictor = make_predictor(study["models"][study["selected_model"]])
    registered = set(market_receipt["registered_artifact_paths"])
    for cell in source.get("markets", []):
        if cell["policy"] != "selected":
            continue
        filename = cell["cell"] + ".json"
        if filename not in registered:
            raise ValueError("unregistered market metadata")
        metadata = read_json(market_run/filename)
        if metadata["schema"] != "information-market/1.1.0" or metadata["config"].get("observation_policy") != "available_only":
            raise ValueError("probe source must use available_only observations; legacy proxy is not observed data")
        if metadata["model_input_manifest_sha256"] != model_receipt["manifest_sha256"]:
            raise ValueError("rollout and model input manifests differ")
        ledger_name = metadata["ledger_artifact"]
        if ledger_name not in registered:
            raise ValueError("unregistered market ledger")
        observed_rounds = 0
        with (market_run/ledger_name).open(encoding="utf-8") as stream:
            for text in stream:
                row = json.loads(text)
                day = row["decision_day"]
                if day != observed_rounds or row["information_cutoff_day"] != day or row["conservation_passed"] is not True:
                    raise ValueError("invalid rollout chronology or settlement audit")
                observed_rounds += 1
                if len(row["decisions"]) != metadata["config"]["agents"]:
                    raise ValueError("incomplete agent round")
                for decision in row["decisions"]:
                    visible, account = decision["visible_fields"], decision["account_state"]
                    expected = predictor(visible, account)
                    recorded = _normalise_prediction(decision)
                    for key in ("action_probs", "intensities"):
                        if any(abs(a-b) > 1e-12 for a, b in zip(expected[key], recorded[key])):
                            raise ValueError("recorded decision is not from the bound Student")
                    yield {"source_run_id": market_receipt["run_id"], "cell": cell["cell"],
                           "agent_id": decision["agent_id"], "decision_day": day,
                           "rounds": metadata["config"]["rounds"], "quote_rule": cell["quote_rule"],
                           "profile_id": decision["profile_id"], "visible_fields": visible,
                           "account_state": account, "student_prediction": expected,
                           "base_state": {**{key: row["market_effective"][key] for key in P8[:6]}, **account}}
        if observed_rounds != metadata["config"]["rounds"]:
            raise ValueError("incomplete market ledger")


class FakeNullTeacher:
    """Provider-shaped constant hold fixture, never endpoint or human evidence."""
    model = "fake-null-control"
    request_count = response_count = application_attempt_count = 0
    network_access = False

    def __init__(self):
        self.batch_sizes = []

    async def complete_many(self, prompts, *, before_attempt, on_application_attempt, on_completion, **kwargs):
        self.batch_sizes.append(len(prompts))
        for index, _prompt in enumerate(prompts):
            before_attempt(index)
            self.request_count += 1
            self.application_attempt_count += 1
            on_application_attempt(index, {"application_attempt_index": 1, "status": "response_received",
                                          "retry_scheduled": False, "retryable": False})
            self.response_count += 1
            on_completion(index, TeacherCompletion(
                raw_response='{"action":"hold","intensity":0,"reasoning":"fake null control"}',
                reported_model=self.model, input_tokens=0, output_tokens=0, response_id=None,
                finish_reason="stop"))

    async def aclose(self):
        return None


async def _complete_batch(provider, prompts, *, before_attempt, on_application_attempt, on_completion):
    """Drain this batch's owned requests before a callback error can close logs."""
    if isinstance(provider, FakeNullTeacher):
        return await provider.complete_many(prompts, before_attempt=before_attempt,
                    on_application_attempt=on_application_attempt, on_completion=on_completion)
    provider.batch_sizes.append(len(prompts))
    semaphore = asyncio.Semaphore(provider.workers)
    async def one(index, prompt):
        pair = await provider._one(index, prompt[0], prompt[1], semaphore,
                                   before_attempt, on_application_attempt)
        on_completion(pair[0], pair[1])
    tasks = [asyncio.create_task(one(index, prompt)) for index, prompt in enumerate(prompts)]
    try:
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            raise outcome


async def acquire(context, args, plan):
    real = args.provider == "openai"
    provider = (OpenAITeacherProvider(**REQUEST, workers=args.workers, application_max_attempts=1,
                                    request_timeout_seconds=7200, hard_request_deadline_seconds=7200)
                if real else FakeNullTeacher())
    context.active_llm = provider
    context.llm_mode = "record"
    context.register_llm_runtime(provider=args.provider, model=provider.model, cache_enabled=False,
                                 mode="rollout_fidelity", network_access=False, provider_calls=0,
                                 application_max_attempts=1, sdk_retry_count=0)
    context.set_stage("simulation")
    rows, attempted, audits, completed_ids = [], set(), [], set()
    paths = [("probe_samples.jsonl", 0o644), ("private_probe_records.jsonl", 0o600),
             ("probe_attempts.jsonl", 0o644)]
    streams = []
    try:
        for name, mode in paths:
            streams.append(os.fdopen(os.open(context.run_dir/name, os.O_WRONLY|os.O_CREAT|os.O_EXCL, mode), "w", encoding="utf-8"))
        public_stream, private_stream, audit_stream = streams
        for base in range(0, len(plan["samples"]), args.workers):
            group = plan["samples"][base:base+args.workers]
            resolved_group = []

            def before(index):
                sample = group[index]
                if sample["sample_id"] in attempted:
                    raise ValueError("duplicate logical request")
                attempted.add(sample["sample_id"])
                context.network_access = real
                context.events.emit("LLMRequestRecorded", data={"sample_id": sample["sample_id"],
                                    "prompt_hash": sample["prompt_hash"]})

            def audit(index, event):
                value = {"sample_id": group[index]["sample_id"], "application_attempt_index": event["application_attempt_index"],
                         "status": event["status"], "retry_scheduled": False}
                _append(audit_stream, value)
                audits.append(value)
                _append(private_stream, {"kind": "attempt", "sample_id": value["sample_id"],
                                        "audit": _safe_private(context, dict(event))})

            def completion(index, value):
                if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(group):
                    raise ValueError("completion index outside active batch")
                sample = group[index]
                case = plan["cases"][sample["case_index"]]
                _append(private_stream, {"kind": "completion", "sample_id": sample["sample_id"],
                    "prompt": case["prompt"], "completion": _safe_private(context, asdict(value))})
                if sample["sample_id"] not in attempted or sample["sample_id"] in completed_ids:
                    raise ValueError("unattempted or duplicate completion")
                completed_ids.add(sample["sample_id"])
                decision, failure = None, None
                if value.error_type is not None or value.raw_response is None:
                    failure = "provider_exception_or_shape"
                elif value.finish_reason != "stop":
                    failure = "incomplete_termination"
                elif real and value.reported_model != "HiggsAI":
                    failure = "reported_model_mismatch"
                else:
                    try:
                        parsed = parse_teacher_response(value.raw_response, V2AttentionState.from_mapping(case["base_state"]))
                        public = parsed.public_record()
                        decision = {"action": public["action"], "intensity": public["intensity"]}
                    except (ValueError, TypeError):
                        failure = "teacher_response_invalid"
                row = {"schema_version": SAMPLE_SCHEMA, "sample_id": sample["sample_id"],
                       "case_id": sample["case_id"], "replicate": sample["replicate"],
                       "request_index": sample["request_index"], "source_kind": args.provider,
                       "status": "valid" if decision is not None else "failed", "decision": decision,
                       "failure_code": failure, "raw_response_sha256": hashlib.sha256(value.raw_response.encode("utf-8")).hexdigest() if value.raw_response is not None else None,
                       "application_attempt_count": value.application_attempt_count,
                       "reported_model": value.reported_model, "finish_reason": value.finish_reason,
                       "input_tokens": value.input_tokens, "output_tokens": value.output_tokens}
                _append(public_stream, row)
                rows.append(row)
                resolved_group.append(row)
                if value.raw_response is not None:
                    context.events.emit("LLMResponseRecorded", data={"sample_id": sample["sample_id"], "source": "record"})
                if decision is not None:
                    context.events.emit("AgentDecisionParsed", data={"decision_response_schema": SAMPLE_SCHEMA,
                                        "parse_status": "ok", "terminal_status": "valid"})
                context.manifest["rollout_fidelity_honest_n"] = {
                    "planned_logical_requests": len(plan["samples"]), "attempted_logical_requests": len(attempted),
                    "resolved_logical_requests": len(rows), "valid_responses": sum(r["status"] == "valid" for r in rows),
                    "physical_attempts": provider.application_attempt_count, "human_participants": 0}
                context._write()

            prompts = [(plan["cases"][item["case_index"]]["prompt"]["system"],
                        plan["cases"][item["case_index"]]["prompt"]["user"]) for item in group]
            await _complete_batch(provider, prompts, before_attempt=before,
                                  on_application_attempt=audit, on_completion=completion)
            if len(resolved_group) != len(group):
                raise ValueError("incomplete completion callback group")
            if all(row["failure_code"] == "provider_exception_or_shape" for row in resolved_group):
                break  # preserve remaining slots as unattempted; never silently resume this run
    finally:
        for stream in streams:
            stream.close()
        await provider.aclose()
        summary = summarize(plan, rows, real_endpoint=real)
        summary.update(attempted_logical_requests=len(attempted),
                       physical_attempts=provider.application_attempt_count,
                       attempts_with_terminal_audit=len(audits),
                       valid_endpoint_responses=sum(row["status"] == "valid" for row in rows) if real else 0)
        write_json_exclusive(context.run_dir/"fidelity_summary.json", summary)
    return summary


def main(argv: Sequence[str] | None = None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        bootstrap = bootstrap_cli(argv, default_out="results_rollout_fidelity", command_identity=COMMAND)
    except BootstrapCLIError as error:
        fail_cli(None, error)
    try:
        args = parser().parse_args(argv)
        _validate(args)
    except (ValueError, TypeError, OSError) as error:
        fail_cli(bootstrap, error)
    inputs = ({"market": args.market_run, "model": args.model_run} if args.task == "plan" else {"plan": args.plan_run})
    cfg = Config(provider="mock", seed=args.seed, n_rounds=0, n_llm_agents=0, n_noise_agents=0,
                 cache_enabled=False, openai_base_url="", openai_api_key="", out_dir=args.out)
    context = ManagedRunContext.create(cfg, out_root=args.out, run_id=args.run_id, command_identity=COMMAND,
            run_kind="rollout_fidelity_"+args.task, planned_simulation_runs=0, repo_root=ROOT,
            input_paths={name: path/"run_manifest.json" for name, path in inputs.items()},
            research_profile={"profile_id": SCHEMA, "persona_contract": {"applicable": False},
                              "prompt_contract": {"schema_version": SCHEMA}})
    with context:
        context.set_stage("config_validation")
        receipts = {name: verify_run(path, getattr(args, name+"_manifest_sha256")) for name, path in inputs.items()}
        if args.task == "plan":
            study = _load_study(args.model_run, receipts["model"])
            plan = build_plan(_candidates(args.market_run, receipts["market"], receipts["model"], study),
                              max_states=args.max_states, replicates=args.replicates, seed=args.seed)
        else:
            if "probe_plan.json" not in receipts["plan"]["registered_artifact_paths"]:
                raise ValueError("unregistered probe plan")
            plan = read_json(args.plan_run/"probe_plan.json")
            validate_plan(plan)
        if args.live and args.confirm_request_count != plan["planned_logical_requests"]:
            raise ValueError("--confirm-request-count must exactly match the frozen plan")
        write_json_exclusive(context.run_dir/"probe_plan.json", plan)
        write_json_exclusive(context.run_dir/"source_receipts.json", receipts)
        components = ["nmsim/rollout_probes.py", "nmsim/information_student.py", "nmsim/v2_attention.py",
                      "nmsim/information_weight.py", "nmsim/v2_distillation.py"]
        scientific = {"schema": SCHEMA, "plan_hash": plan["plan_hash"],
                      "components": {name: file_sha256(ROOT/name) for name in components}}
        model_request = {"schema": SCHEMA, "provider": args.provider,
                         "request": REQUEST if args.provider == "openai" else {"model": "fake-null-control", "behavior": "constant_hold"},
                         "required_reported_model": "HiggsAI" if args.provider == "openai" else "fake-null-control", "seed_sent": False,
                         "adapter_source_sha256": file_sha256(ROOT/"experiments/v2_attention_market.py")}
        if args.provider == "openai":
            from experiments.information_weight_teacher import _endpoint_identity
            model_request["endpoint_identity"] = _endpoint_identity()
        execution = {"schema": SCHEMA, "task": args.task, "dry_run": args.dry_run, "live": args.live,
                     "workers": args.workers, "python": platform.python_version(), "application_max_attempts": 1,
                     "timeout_seconds": 7200, "stop_rule": "first batch with no provider response",
                     "entrypoint_sha256": file_sha256(Path(__file__)),
                     "inputs": {name: value["manifest_sha256"] for name, value in receipts.items()}}
        identities = {"schema_version": "rollout-fidelity-identities/1.0", "scientific_config_hash": canonical_hash(scientific),
                      "model_request_config_hash": canonical_hash(model_request), "execution_config_hash": canonical_hash(execution)}
        identities["full_effective_config_hash"] = canonical_hash(identities)
        write_json_exclusive(context.run_dir/"identities.json", {**identities, "scientific_config": scientific,
                             "model_request_config": model_request, "execution_config": execution})
        context.manifest["rollout_fidelity_identities"] = identities
        context.register_llm_runtime(provider="none", mode="probe_plan", network_access=False, provider_calls=0)
        context._write()
        if args.task == "acquire" and not args.dry_run:
            context.set_stage("provider_setup")
            summary = asyncio.run(acquire(context, args, plan))
        else:
            summary = {"plan_hash": plan["plan_hash"], "selected_states": plan["selected_states"],
                       "planned_logical_requests": plan["planned_logical_requests"], "new_teacher_requests": 0,
                       "human_participants": 0, "status": "plan_only"}
            write_json_exclusive(context.run_dir/"plan_summary.json", summary)
        context.set_stage("result_export")
        for name, path in inputs.items():
            assert_unchanged(path, receipts[name])
        if args.task == "acquire" and not args.dry_run and summary["unresolved_logical_requests"]:
            raise RuntimeError("probe acquisition incomplete; preserved as a failed attempt")
        context.finish()
        print(json.dumps({"run_id": context.run_id, "run_dir": str(context.run_dir), **{key: value for key, value in summary.items() if key != "cases"}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
