"""Managed, provider-free training and closed-loop information-market research.

Historical data are verified analysis inputs; never resumed or mutated. No
code path constructs a Teacher or issues model requests.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import itertools
import json
import math
import os
from pathlib import Path
import platform
import sys
import time
from typing import Any, Sequence

from nmsim.config import Config
from nmsim.information_artifacts import (
    assert_unchanged, canonical_hash, file_sha256, read_json, read_public_samples,
    verify_run, write_json_exclusive, write_text_exclusive, ensure_separate_output, preflight_output_separation,
)
from nmsim.information_reporting import render_html, render_markdown
from nmsim.managed_cli import (BootstrapCLIError, RaisingArgumentParser,
                              bootstrap_cli, fail_cli)
from nmsim.run_context import ManagedRunContext


COMMAND = "python -m experiments.information_market"
SCHEMA = "information-market-experiment/1.0"
DEFAULT_OUT = "results_information_market"
ROOT = Path(__file__).resolve().parents[1]
SCIENTIFIC_FILES = ("nmsim/information_student.py", "nmsim/information_market.py",
                    "nmsim/information_weight.py", "nmsim/information_weight_scale.py", "nmsim/v2_attention.py",
                    "nmsim/v2_distillation.py", "nmsim/v2_market.py",
                    "nmsim/v2_market_experiment.py")
LIMITATIONS = [
    "Teacher-generated training labels have no same-task human ground truth.",
    "Historical 10k cells have K=1; per-cell Teacher variability was not measured.",
    "Historical synthetic marginal state coverage is not a real economic joint distribution.",
    "Closed-loop world and quote rules are explicit new exploratory mechanisms.",
    "Market rollout fidelity and human resemblance require independent evidence.",
    "Daily-close-range proxy is not observed intraday range; it is structural OOD.",
]


def build_parser() -> argparse.ArgumentParser:
    parser = RaisingArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--task", choices=("train", "simulate", "benchmark"), default="train")
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--run-id")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--source-run", type=Path)
    parser.add_argument("--source-manifest-sha256")
    parser.add_argument("--model-run", type=Path)
    parser.add_argument("--model-manifest-sha256")
    parser.add_argument("--distribution-run", type=Path)
    parser.add_argument("--distribution-manifest-sha256")
    parser.add_argument("--sizing-policy", choices=("legacy_mean", "distribution_mean", "distribution_sampled"), default="legacy_mean")
    parser.add_argument("--sizing-candidate", choices=("selected", "empirical", "conditional_softmax"), default="selected")
    parser.add_argument("--human-responses", type=Path)
    parser.add_argument("--seed", type=int, default=20260909)
    parser.add_argument("--epochs", type=int, default=120)
    parser.add_argument("--hidden-dim", type=int, default=16)
    parser.add_argument("--backend", choices=("auto", "numpy", "python"), default="auto")
    parser.add_argument("--agents", type=int, default=200)
    parser.add_argument("--rounds", type=int, default=60)
    parser.add_argument("--seeds", type=int, default=3)
    parser.add_argument("--profile-weights", type=float, nargs=4, default=[1.0]*4,
                        metavar=("PV", "FUND", "NEWS", "BAL"))
    parser.add_argument("--quote-rule", choices=("independent", "legacy_intensity_linked", "both"), default="both")
    parser.add_argument("--news-mode", choices=("eventful", "neutral"), default="eventful")
    parser.add_argument("--observation-policy", choices=("legacy_proxy", "available_only"), default="legacy_proxy")
    parser.add_argument("--policies", nargs="+", choices=("selected", "prior", "random"),
                        default=["selected", "prior", "random"])
    parser.add_argument("--version", action="version", version=SCHEMA)
    return parser


def _validate(args) -> None:
    if args.sizing_policy != "legacy_mean":
        if args.task != "simulate" or args.distribution_run is None or args.policies != ["selected"] or args.observation_policy != "available_only":
            raise ValueError("new sizing requires --task simulate, --distribution-run, --policies selected, and --observation-policy available_only")
    elif args.distribution_run is not None or args.distribution_manifest_sha256 is not None or args.sizing_candidate != "selected":
        raise ValueError("distribution input/candidate requires an explicit sizing policy")
    if args.task == "train" and args.source_run is None:
        raise ValueError("--source-run is required for training")
    if args.task == "simulate" and args.model_run is None:
        raise ValueError("--model-run is required for simulation")
    if args.human_responses is not None and args.task != "benchmark":
        raise ValueError("--human-responses only applies to benchmark")
    if args.task == "train" and args.observation_policy != "legacy_proxy":
        raise ValueError("--observation-policy cannot redefine historical training observations")
    if min(args.epochs, args.hidden_dim, args.agents, args.rounds, args.seeds) <= 0:
        raise ValueError("counts must be positive")
    if args.agents < 2:
        raise ValueError("--agents must be at least two")
    if args.seed < 0:
        raise ValueError("seed must be nonnegative")
    if len(set(args.policies)) != len(args.policies):
        raise ValueError("duplicate policy")
    if (any(not math.isfinite(x) or x < 0 for x in args.profile_weights) or
            not 0 < sum(args.profile_weights) < math.inf):
        raise ValueError("invalid --profile-weights")
    for digest in (args.source_manifest_sha256, args.model_manifest_sha256, args.distribution_manifest_sha256):
        if digest is not None and (len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)):
            raise ValueError("invalid manifest SHA-256")


def _quotes(args):
    return ["independent", "legacy_intensity_linked"] if args.quote_rule == "both" else [args.quote_rule]


def _source_identity(receipt):
    if receipt is None:
        return {"input_use": "new_synthetic_tasks_only_no_external_observations"}
    return {key: receipt[key] for key in ("input_use", "run_id", "manifest_sha256",
            "registered_artifacts_verified", "snapshot_hash")}


def build_identities(args, receipt: dict, distribution_receipt=None) -> dict:
    from nmsim import information_market as market
    source_files = {name: file_sha256(ROOT/name) for name in SCIENTIFIC_FILES}
    if args.task == "train":
        input_identity = {"public_samples_sha256": next(row["sha256"] for row in receipt["files"]
                           if row["path"] == "information_weight_scale_samples.jsonl")}
    elif args.task == "simulate":
        study = _load_study(args.model_run, receipt)
        used = {study["selected_model"] if name == "selected" else name
                for name in args.policies if name != "random"}
        input_identity = {"encoder": study["encoder"],
                          "models": {name: canonical_hash(study["models"][name]) for name in sorted(used)}}
        if args.sizing_policy != "legacy_mean":
            sizing = _load_distribution(args.distribution_run, distribution_receipt, study)
            input_identity["intensity_study_semantic_hash"] = sizing["study_semantic_hash"]
            input_identity["sizing_candidate"] = args.sizing_candidate
            source_files["nmsim/intensity_distribution.py"] = file_sha256(ROOT/"nmsim/intensity_distribution.py")
    else:
        from nmsim.human_benchmark import build_tasks
        source_files["nmsim/human_benchmark.py"] = file_sha256(ROOT/"nmsim/human_benchmark.py")
        input_identity = {"task_bank_hash": canonical_hash(build_tasks(args.seed, observation_policy=args.observation_policy)),
                          "human_responses_sha256": file_sha256(args.human_responses) if args.human_responses else None}
        if args.model_run is not None:
            study = _load_study(args.model_run, receipt)
            input_identity["selected_model_hash"] = canonical_hash(study["models"][study["selected_model"]])
            input_identity["encoder"] = study["encoder"]
    scientific = {"schema_version": "information-market-scientific-config/1.0",
                  "task": args.task, "seed": args.seed,
                  "scientific_input": input_identity,
                  "components": source_files,
                  "training": {"epochs": args.epochs, "hidden_dim": args.hidden_dim,
                               "selection": "validation_action_cross_entropy_only"} if args.task == "train" else None,
                  "market": {"agents": args.agents, "rounds": args.rounds, "seeds": args.seeds,
                             "profile_weights": args.profile_weights, "quote_rules": _quotes(args),
                             "policies": args.policies, "news_mode": args.news_mode,
                             "contract": market.descriptor(args.observation_policy, args.sizing_policy)} if args.task == "simulate" else None}
    model_request = {"schema_version": "information-market-model-request/1.0", "provider_access": "none",
                     "new_teacher_requests": 0}
    execution = {"schema_version": "information-market-execution-config/1.0", "dry_run": args.dry_run,
                 "input_manifest_sha256": receipt["manifest_sha256"] if receipt else None,
                 "input_snapshot_hash": receipt["snapshot_hash"] if receipt else None,
                 "requested_training_backend": args.backend, "python": platform.python_version(),
                 "platform": platform.platform(), "entrypoint_sha256": file_sha256(Path(__file__)),
                 "artifact_io_sha256": file_sha256(ROOT/"nmsim/information_artifacts.py"),
                 "reporting_sha256": file_sha256(ROOT/"nmsim/information_reporting.py")}
    if distribution_receipt is not None:
        execution["distribution_input_manifest_sha256"] = distribution_receipt["manifest_sha256"]
        execution["distribution_input_snapshot_hash"] = distribution_receipt["snapshot_hash"]
    try:
        import numpy
        execution["numpy_version"] = numpy.__version__
    except ImportError:
        execution["numpy_version"] = None
    result = {"schema_version": "information-market-identities/1.0",
              "scientific_config_hash": canonical_hash(scientific),
              "model_request_config_hash": canonical_hash(model_request),
              "execution_config_hash": canonical_hash(execution)}
    result["full_effective_config_hash"] = canonical_hash(result)
    return {**result, "scientific_config": scientific, "model_request_config": model_request,
            "execution_config": execution}


def _summary(context, args, identities, receipt):
    return {"schema_version": SCHEMA, "run_id": context.run_id, "task": args.task,
            "status": "dry_run" if args.dry_run else "finished", "source": _source_identity(receipt),
            "identities": {k: v for k, v in identities.items() if k.endswith("hash") or k == "schema_version"},
            "human_validation": "not_established", "limitations": [
                ("Unobserved intraday range is omitted; the new visibility pattern needs fidelity checks."
                 if args.observation_policy == "available_only" and text.startswith("Daily-close-range") else text)
                for text in LIMITATIONS],
            "honest_n": {"new_teacher_requests": 0, "human_participants": 0,
                         "trained_models": 0, "market_runs": 0, "rounds": 0, "agent_decisions": 0},
            "next_evidence_needed": ["same-task human choices", "repeated Teacher probes on rollout states",
                                     "matched market validation and composition identifiability"]}


def _append(stream, value):
    stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)+"\n")
    stream.flush()
    os.fsync(stream.fileno())


def _train(context, args, receipt, summary, progress):
    from nmsim.information_student import train_models
    rows = read_public_samples(args.source_run, receipt)
    context.set_stage("simulation")
    _append(progress, {"stage": "student_training", "source_rows": len(rows)})
    result = train_models(rows, seed=args.seed, epochs=args.epochs,
                          hidden_dim=args.hidden_dim, backend=args.backend)
    result["analysis_source"] = _source_identity(receipt)
    result["provenance"]["source_file_byte_hash"] = next(row["sha256"] for row in receipt["files"]
        if row["path"] == "information_weight_scale_samples.jsonl")
    write_json_exclusive(context.run_dir/"student_study.json", result)
    summary.update({"student_comparison": result["evaluation"],
                    "selected_model": result["selected_model"],
                    "selection_rule": "lowest validation action CE; frozen test not used for selection"})
    summary["honest_n"].update({"historical_source_rows": len(rows),
                               "training_data_accounting": result["accounting"],
                               "trained_models": len(result["models"])})
    write_json_exclusive(context.run_dir/"split.json", result["split"])
    return ["student_study.json", "split.json"]


def _load_study(root, receipt):
    from nmsim.information_student import encoder_descriptor, SCHEMA_VERSION, MODEL_NAMES
    filename = "student_study.json"
    if filename not in receipt["registered_artifact_paths"]:
        raise ValueError("model study is not a registered source artifact")
    study = read_json(root/filename)
    if study.get("schema_version") != SCHEMA_VERSION or study.get("encoder") != encoder_descriptor():
        raise ValueError("incompatible Student study or encoder")
    if study.get("selected_model") not in MODEL_NAMES or set(study.get("models", {})) != set(MODEL_NAMES):
        raise ValueError("invalid Student model roster or selection")
    for name, payload in study["models"].items():
        if canonical_hash(payload) != study["provenance"]["model_serialization_hashes"].get(name):
            raise ValueError("model serialization identity mismatch")
    return study


def _load_distribution(root, receipt, original_study):
    from nmsim.intensity_distribution import make_distribution_predictor
    if not receipt or "intensity_study.json" not in receipt["registered_artifact_paths"]:
        raise ValueError("unregistered sizing study")
    result = read_json(root/"intensity_study.json")
    make_distribution_predictor(result, original_study)  # strict source/encoder/model/selection validation
    return result


def _simulate(context, args, receipt, summary, progress, distribution_receipt=None):
    from nmsim import information_market as market
    from nmsim.information_student import make_predictor, make_support_checker
    study = _load_study(args.model_run, receipt)
    selected = study["selected_model"]
    predictors = {"selected": make_predictor(study["models"][selected]),
                  "prior": make_predictor(study["models"]["prior"]), "random": market.random_policy}
    if args.sizing_policy != "legacy_mean":
        from nmsim.intensity_distribution import make_distribution_predictor
        sizing = _load_distribution(args.distribution_run, distribution_receipt, study)
        predictors["selected"] = make_distribution_predictor(sizing, study, candidate=args.sizing_candidate)
        summary.update(sizing_policy=args.sizing_policy, sizing_candidate=args.sizing_candidate)
    summary["selected_model"] = selected
    from nmsim.information_weight import PROFILE_IDS, PROFILE_FIELDS
    check_support = make_support_checker(study["ood_reference"])
    weights = dict(zip(PROFILE_IDS, args.profile_weights))
    cells = list(itertools.product(args.policies, _quotes(args), range(args.seeds)))
    planned = len(cells)
    context.set_experiment_completion(planned_runs=planned, started_runs=0, completed_runs=0, failed_runs=0)
    context.manifest["completion"]["rounds"]["planned"] = planned * args.rounds
    context.manifest["completion"]["agent_decisions"]["planned"] = planned * args.rounds * args.agents
    markets, artifacts = [], []
    context.set_stage("simulation")
    for index, (policy_name, quote, seed_index) in enumerate(cells):
        name = f"market_{policy_name}_{quote}_{seed_index:03d}"
        context.set_experiment_completion(planned_runs=planned, started_runs=index+1,
                                          completed_runs=index, failed_runs=0)
        start = time.monotonic()
        ood_counts = Counter()
        ood_rows = 0
        max_abs_z = 0.0
        ledger_path = context.run_dir/(name+"_rounds.jsonl")
        descriptor = os.open(ledger_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(descriptor, "w", encoding="utf-8") as ledger:
            def on_round(row):
                nonlocal ood_rows, max_abs_z
                for decision in row["decisions"]:
                    visible = decision.get("visible_fields")
                    if visible is None:
                        visible = {field: row["market_effective"][field]
                                   for field in PROFILE_FIELDS[decision["profile_id"]]}
                    support = check_support(visible, decision["account_state"])
                    ood_rows += int(support["outside_train_range"])
                    ood_counts.update(support["outside_features"])
                    max_abs_z = max(max_abs_z, support["max_abs_z"])
                _append(ledger, row)
                counts = summary["honest_n"]
                counts["rounds"] += 1
                counts["agent_decisions"] += len(row["decisions"])
                completion = context.manifest["completion"]
                completion["rounds"].update(started=counts["rounds"], completed=counts["rounds"])
                completion["agent_decisions"].update(attempted=counts["agent_decisions"], completed=counts["agent_decisions"])
                context.manifest["information_market_honest_n"] = dict(counts)
                context._write()
                _append(progress, {"stage": "market_simulation", "cell": name,
                                   "completed_market_runs": index, "planned_market_runs": planned,
                                   "completed_rounds": counts["rounds"]})
            try:
                result = market.run_market(predictors[policy_name], seed=args.seed+seed_index,
                                           agents=args.agents, rounds=args.rounds,
                                           profile_weights=weights, quote_rule=quote,
                                           news_mode=args.news_mode, observation_policy=args.observation_policy,
                                           sizing_policy=args.sizing_policy,
                                           on_round=on_round)
            except BaseException:
                context.set_experiment_completion(planned_runs=planned, started_runs=index+1,
                                                  completed_runs=index, failed_runs=1)
                raise
        result.pop("ledger")
        result["summary"]["train_support_diagnostic"] = {
            "evaluated_agent_rounds": args.agents * args.rounds,
            "outside_train_range_rows": ood_rows,
            "outside_train_range_fraction": ood_rows / (args.agents * args.rounds),
            "outside_features": dict(ood_counts), "max_abs_z": max_abs_z,
            "joint_support_assessed": False, "not_teacher_fidelity_evidence": True}
        result["ledger_artifact"] = ledger_path.name
        result["model_input_manifest_sha256"] = receipt["manifest_sha256"]
        if distribution_receipt is not None:
            result["distribution_input_manifest_sha256"] = distribution_receipt["manifest_sha256"]
            result["sizing_candidate"] = args.sizing_candidate
            result["summary"]["sizing_audit"] = result["sizing_audit"]
        result["elapsed_seconds"] = time.monotonic()-start
        write_json_exclusive(context.run_dir/(name+".json"), result)
        artifacts.extend([name+".json", ledger_path.name])
        markets.append({"cell": name, "policy": policy_name, "quote_rule": quote,
                        "seed": args.seed+seed_index, "world_hash": result["world_hash"],
                        "elapsed_seconds": result["elapsed_seconds"], **result["summary"]})
        summary["honest_n"]["market_runs"] = index+1
        context.set_experiment_completion(planned_runs=planned, started_runs=index+1,
                                          completed_runs=index+1, failed_runs=0)
    summary["markets"] = markets
    context.manifest["simulation_computation_completed"] = True
    return artifacts


def _benchmark(context, args, receipt, summary, progress):
    from nmsim.human_benchmark import build_tasks, score_responses
    from nmsim.information_student import make_predictor
    tasks = build_tasks(args.seed, observation_policy=args.observation_policy)
    predictions = []
    if args.model_run is not None:
        study = _load_study(args.model_run, receipt)
        predictor = make_predictor(study["models"][study["selected_model"]])
        predictions = [{"task_id": task["task_id"], **predictor(task["visible_fields"], task["account_state"])}
                       for task in tasks]
    humans = []
    if args.human_responses is not None:
        before = file_sha256(args.human_responses)
        with args.human_responses.open(encoding="utf-8") as source:
            humans = [json.loads(line) for line in source if line.strip()]
        if before != file_sha256(args.human_responses):
            raise ValueError("human responses changed while read")
        if before != context.manifest["information_market_identities"]["scientific_config"]["scientific_input"]["human_responses_sha256"]:
            raise ValueError("human response input identity mismatch")
    scores = score_responses(tasks, humans, predictions)
    write_json_exclusive(context.run_dir/"human_tasks.json", tasks)
    write_json_exclusive(context.run_dir/"benchmark_predictions.json", predictions)
    write_json_exclusive(context.run_dir/"human_comparison.json", scores)
    summary["human_validation"] = scores["status"]
    summary["honest_n"].update({"human_participants": scores["human_participants"],
                               "human_responses": scores["human_responses"],
                               "synthetic_tasks": len(tasks), "benchmark_predictions": len(predictions)})
    _append(progress, {"stage": "human_benchmark", "status": scores["status"],
                       "human_participants": scores["human_participants"]})
    return ["human_tasks.json", "benchmark_predictions.json", "human_comparison.json"]


def main(argv: Sequence[str] | None = None) -> None:
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
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        _validate(args)
    except (ValueError, TypeError, OSError) as error:
        fail_cli(bootstrap, error)
    try:
        ensure_separate_output(Path(args.out), (args.source_run, args.model_run, args.distribution_run))
    except ValueError:
        print("provenance_not_created_reason=output_overlaps_historical_input", file=sys.stderr)
        raise SystemExit(2)
    cfg = Config(provider="mock", seed=args.seed, n_rounds=0, n_llm_agents=0, n_noise_agents=0,
                 cache_enabled=False, openai_base_url="", openai_api_key="", out_dir=args.out)
    source = args.source_run if args.task == "train" else args.model_run
    pin = args.source_manifest_sha256 if args.task == "train" else args.model_manifest_sha256
    started = time.monotonic()
    inputs = {"historical_input_manifest": source/"run_manifest.json"} if source else {}
    if args.human_responses is not None:
        inputs["anonymous_human_responses"] = args.human_responses
    if args.distribution_run is not None:
        inputs["distribution_input_manifest"] = args.distribution_run/"run_manifest.json"
    try:
        context = ManagedRunContext.create(cfg, out_root=args.out, run_id=args.run_id,
                    scenario_id="information-market/1.0", repo_root=ROOT, command_identity=COMMAND,
                    run_kind="information_market_"+args.task, planned_simulation_runs=0,
                    input_paths=inputs,
                    research_profile={"profile_id": SCHEMA,
                                      "persona_contract": {"applicable": False},
                                      "prompt_contract": {"provider_calls": 0}})
    except (OSError, ValueError) as error:
        print("provenance_not_created_reason="+type(error).__name__, file=sys.stderr)
        raise SystemExit(2)
    with context:
        context.register_llm_runtime(provider="none", model=None, mode="offline_information_market",
                                     cache_enabled=False, network_access=False, provider_calls=0)
        context.set_stage("config_validation")
        receipt = verify_run(source, pin) if source is not None else None
        distribution_receipt = verify_run(args.distribution_run, args.distribution_manifest_sha256) if args.distribution_run is not None else None
        identities = build_identities(args, receipt, distribution_receipt)
        context.manifest["information_market_identities"] = identities
        context.manifest["legacy_config_scope"] = "lifecycle_only_not_information_market_science"
        context._write()
        write_json_exclusive(context.run_dir/"source_receipt.json", receipt)
        if distribution_receipt is not None:
            write_json_exclusive(context.run_dir/"distribution_source_receipt.json", distribution_receipt)
        write_json_exclusive(context.run_dir/"identities.json", identities)
        summary = _summary(context, args, identities, receipt)
        descriptor = os.open(context.run_dir/"progress.jsonl", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(descriptor, "w", encoding="utf-8") as progress:
            _append(progress, {"stage": "validated", "task": args.task,
                               "at": datetime.now(timezone.utc).isoformat()})
            artifacts = []
            if not args.dry_run:
                if args.task == "simulate":
                    artifacts = _simulate(context,args,receipt,summary,progress,distribution_receipt)
                else:
                    execute = {"train": _train, "benchmark": _benchmark}[args.task]
                    artifacts = execute(context,args,receipt,summary,progress)
            context.set_stage("result_export")
            if source is not None:
                assert_unchanged(source, receipt)
            if distribution_receipt is not None:
                assert_unchanged(args.distribution_run, distribution_receipt)
            if args.human_responses is not None:
                if file_sha256(args.human_responses) != identities["scientific_config"]["scientific_input"]["human_responses_sha256"]:
                    raise ValueError("human response input changed")
            _append(progress, {"stage": "finished", "source_unchanged": True})
        context.set_stage("result_export")
        summary["elapsed_seconds"] = time.monotonic()-started
        summary["artifacts"] = ["summary.json", "report.md", "identities.json"]+artifacts
        write_json_exclusive(context.run_dir/"summary.json", summary)
        write_text_exclusive(context.run_dir/"report.md", render_markdown(summary))
        write_text_exclusive(context.run_dir/"report.html", render_html(summary))
        context.manifest["information_market_honest_n"] = summary["honest_n"]
        context.finish()
        print(json.dumps({"run_id": context.run_id, "run_dir": str(context.run_dir),
                          "status": summary["status"], "honest_n": summary["honest_n"],
                          "selected_model": summary.get("selected_model"),
                          "elapsed_seconds": summary["elapsed_seconds"]}, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
