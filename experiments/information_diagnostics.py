"""Managed offline learning curves and published human-reference input audits."""
from __future__ import annotations

import json
from pathlib import Path
import platform
import sys
from typing import Sequence

from experiments.information_market import _load_study
from nmsim.config import Config
from nmsim.information_artifacts import (canonical_hash, file_sha256, verify_run, assert_unchanged,
    read_public_samples, write_json_exclusive, write_text_exclusive)
from nmsim.managed_cli import RaisingArgumentParser, bootstrap_cli, fail_cli, BootstrapCLIError
from nmsim.run_context import ManagedRunContext


SCHEMA = "information-diagnostics/1.0"
COMMAND = "python -m experiments.information_diagnostics"
ROOT = Path(__file__).resolve().parents[1]


def parser():
    result = RaisingArgumentParser(description=__doc__, allow_abbrev=False)
    result.add_argument("--task", choices=("learning-curves", "human-reference"), required=True)
    result.add_argument("--source-run", type=Path)
    result.add_argument("--source-manifest-sha256")
    result.add_argument("--model-run", type=Path)
    result.add_argument("--model-manifest-sha256")
    result.add_argument("--csv", type=Path)
    result.add_argument("--fractions", type=float, nargs="+", default=[0.125, 0.25, 0.5, 1.0])
    result.add_argument("--epochs", type=int, default=120)
    result.add_argument("--hidden-dim", type=int, default=16)
    result.add_argument("--backend", choices=("auto", "numpy", "python"), default="auto")
    result.add_argument("--dry-run", action="store_true")
    result.add_argument("--out", default="results_information_diagnostics")
    result.add_argument("--run-id")
    result.add_argument("--version", action="version", version=SCHEMA)
    return result


def main(argv: Sequence[str] | None = None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        bootstrap = bootstrap_cli(argv, default_out="results_information_diagnostics", command_identity=COMMAND)
    except BootstrapCLIError as error:
        fail_cli(None, error)
    try:
        args = parser().parse_args(argv)
        if args.task == "learning-curves":
            if args.source_run is None or args.model_run is None:
                raise ValueError("learning curves require --source-run and --model-run")
        elif args.csv is None:
            raise ValueError("human reference requires --csv")
        if args.epochs < 1 or args.hidden_dim < 1 or any(not 0 < f <= 1 for f in args.fractions):
            raise ValueError("invalid training configuration")
    except (ValueError, TypeError, OSError) as error:
        fail_cli(bootstrap, error)
    inputs = ({"teacher": args.source_run/"run_manifest.json", "model": args.model_run/"run_manifest.json"}
              if args.task == "learning-curves" else {"published_csv": args.csv})
    cfg = Config(provider="mock", n_rounds=0, n_llm_agents=0, n_noise_agents=0, cache_enabled=False,
                 openai_base_url="", openai_api_key="", out_dir=args.out)
    context = ManagedRunContext.create(cfg, out_root=args.out, run_id=args.run_id, repo_root=ROOT,
        command_identity=COMMAND, run_kind="information_diagnostic_"+args.task,
        planned_simulation_runs=0, input_paths=inputs,
        research_profile={"profile_id": SCHEMA, "persona_contract": {"applicable": False},
                          "prompt_contract": {"provider_access": "none"}})
    with context:
        context.register_llm_runtime(provider="none", mode="offline_diagnostic", network_access=False, provider_calls=0)
        context.set_stage("config_validation")
        receipts = {}
        honest = {"new_teacher_requests": 0, "new_human_participants": 0,
                  "market_runs": 0, "historical_reference_humans": 0, "model_fits": 0}
        if args.task == "learning-curves":
            from nmsim.information_learning_curve import learning_curves
            receipts["teacher"] = verify_run(args.source_run, args.source_manifest_sha256)
            receipts["model"] = verify_run(args.model_run, args.model_manifest_sha256)
            study = _load_study(args.model_run, receipts["model"])
            if study["analysis_source"]["manifest_sha256"] != receipts["teacher"]["manifest_sha256"]:
                raise ValueError("learning curve source differs from original study")
            public_sha = next(row["sha256"] for row in receipts["teacher"]["files"]
                              if row["path"] == "information_weight_scale_samples.jsonl")
            if study["provenance"]["source_file_byte_hash"] != public_sha:
                raise ValueError("original study sample byte hash differs from verified public input")
            scientific = {"task": args.task, "split_semantic_hash": study["split"]["split_semantic_hash"],
                          "public_samples_sha256": study["provenance"]["source_file_byte_hash"],
                          "fractions": args.fractions, "epochs": args.epochs, "hidden_dim": args.hidden_dim,
                          "test_partition_used": False}
            sources = ["nmsim/information_learning_curve.py", "nmsim/information_student.py", "nmsim/v2_distillation.py",
                       "nmsim/information_weight.py", "nmsim/information_weight_scale.py", "nmsim/v2_attention.py"]
        else:
            from nmsim.human_reference import SOURCE_CSV_SHA256, SOURCE_DOI, decode_csv, audit_records, reconstruct_decisions
            if args.csv.is_symlink() or not args.csv.is_file():
                raise ValueError("human source must be a regular CSV file")
            if file_sha256(args.csv) != SOURCE_CSV_SHA256:
                raise ValueError("CSV does not match publisher's frozen source checksum")
            scientific = {"task": args.task, "source_doi": SOURCE_DOI, "source_csv_sha256": SOURCE_CSV_SHA256}
            sources = ["nmsim/human_reference.py"]
        scientific["components"] = {name: file_sha256(ROOT/name) for name in sources}
        execution = {"python": platform.python_version(), "backend": args.backend, "dry_run": args.dry_run,
                     "input_manifest_hashes": {key: row["manifest_sha256"] for key, row in receipts.items()},
                     "entrypoint_sha256": file_sha256(Path(__file__))}
        try:
            import numpy
            execution["numpy_version"] = numpy.__version__
        except ImportError:
            execution["numpy_version"] = None
        identity = {"schema_version": SCHEMA, "scientific_config_hash": canonical_hash(scientific),
                    "model_request_config_hash": canonical_hash({"provider": "none", "new_teacher_requests": 0}),
                    "execution_config_hash": canonical_hash(execution)}
        identity["full_effective_config_hash"] = canonical_hash(identity)
        write_json_exclusive(context.run_dir/"identities.json", {**identity, "scientific_config": scientific, "execution_config": execution})
        write_json_exclusive(context.run_dir/"source_receipts.json", receipts)
        context.manifest["information_diagnostic_identities"] = identity
        context._write()
        context.set_stage("simulation")
        if args.dry_run:
            result = {"status": "plan_only", "task": args.task}
        elif args.task == "learning-curves":
            records = read_public_samples(args.source_run, receipts["teacher"])
            result = learning_curves(records, study, fractions=args.fractions, epochs=args.epochs,
                                     hidden_dim=args.hidden_dim, backend=args.backend)
            honest["model_fits"] = 3 * len(result["points"])
        else:
            records = decode_csv(args.csv.read_text(encoding="utf-8"))
            result = audit_records(records)
            if result["status"] != "input_audit_passed":
                raise ValueError("published human input failed accounting or timing audit")
            decisions = reconstruct_decisions(records)
            write_text_exclusive(context.run_dir/"private_joint_decisions.jsonl",
                                 "".join(json.dumps(row, ensure_ascii=False, sort_keys=True)+"\n" for row in decisions), private=True)
            honest["historical_reference_humans"] = result["human_subjects"]
            honest["historical_main_joint_decisions"] = result["main_subject_periods"]
            honest["historical_main_stock_opportunities"] = result["main_stock_opportunities"]
        context.set_stage("result_export")
        if args.task == "learning-curves":
            assert_unchanged(args.source_run, receipts["teacher"])
            assert_unchanged(args.model_run, receipts["model"])
        elif file_sha256(args.csv) != SOURCE_CSV_SHA256:
            raise ValueError("human source changed during analysis")
        write_json_exclusive(context.run_dir/"diagnostic_result.json", result)
        summary = {"schema_version": SCHEMA, "run_id": context.run_id, "task": args.task,
                   "status": "plan_only" if args.dry_run else "finished", "honest_n": honest,
                   "identities": identity, "human_likeness_validated": False}
        write_json_exclusive(context.run_dir/"summary.json", summary)
        context.manifest["information_diagnostic_honest_n"] = honest
        context.finish()
        print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
