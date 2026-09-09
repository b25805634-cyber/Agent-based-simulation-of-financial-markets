"""Managed optional sizing-distribution fit with a frozen action Student."""
from __future__ import annotations

import json
from pathlib import Path
import platform
import sys
from typing import Sequence

from experiments.information_market import _load_study
from nmsim.config import Config
from nmsim.information_artifacts import (canonical_hash, file_sha256, verify_run, assert_unchanged,
    read_public_samples, write_json_exclusive, ensure_separate_output, preflight_output_separation)
from nmsim.managed_cli import RaisingArgumentParser, bootstrap_cli, fail_cli, BootstrapCLIError
from nmsim.run_context import ManagedRunContext

SCHEMA = "intensity-distribution-experiment/1.0"
COMMAND = "python -m experiments.intensity_distribution"
ROOT = Path(__file__).resolve().parents[1]


def parser():
    result = RaisingArgumentParser(description=__doc__, allow_abbrev=False)
    result.add_argument("--source-run", type=Path, required=True)
    result.add_argument("--source-manifest-sha256")
    result.add_argument("--student-run", type=Path, required=True)
    result.add_argument("--student-manifest-sha256")
    result.add_argument("--epochs", type=int, default=120)
    result.add_argument("--backend", choices=("auto", "numpy", "python"), default="auto")
    result.add_argument("--out", default="results_intensity_distribution")
    result.add_argument("--run-id")
    result.add_argument("--dry-run", action="store_true")
    result.add_argument("--version", action="version", version=SCHEMA)
    return result


def main(argv: Sequence[str] | None = None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        preflight_output_separation(argv, "results_intensity_distribution")
    except ValueError:
        print("provenance_not_created_reason=output_overlaps_historical_input", file=sys.stderr)
        raise SystemExit(2)
    try:
        boot = bootstrap_cli(argv, default_out="results_intensity_distribution", command_identity=COMMAND)
    except BootstrapCLIError as error:
        fail_cli(None, error)
    try:
        args = parser().parse_args(argv)
        if args.epochs < 1:
            raise ValueError("--epochs must be positive")
    except (ValueError, TypeError, OSError) as error:
        fail_cli(boot, error)
    try:
        ensure_separate_output(Path(args.out), (args.source_run, args.student_run))
    except ValueError:
        print("provenance_not_created_reason=output_overlaps_historical_input", file=sys.stderr)
        raise SystemExit(2)
    cfg = Config(provider="mock", n_rounds=0, n_llm_agents=0, n_noise_agents=0, cache_enabled=False,
                 openai_base_url="", openai_api_key="", out_dir=args.out)
    context = ManagedRunContext.create(cfg, out_root=args.out, run_id=args.run_id, repo_root=ROOT,
        command_identity=COMMAND, run_kind="intensity_distribution_fit", planned_simulation_runs=0,
        input_paths={"teacher_source": args.source_run/"run_manifest.json", "student_source": args.student_run/"run_manifest.json"},
        research_profile={"profile_id": SCHEMA, "persona_contract": {"applicable": False},
                          "prompt_contract": {"provider_access": "none"}})
    with context:
        from nmsim.intensity_distribution import fit_intensity_distributions
        context.register_llm_runtime(provider="none", mode="offline_distribution_fit", network_access=False, provider_calls=0)
        context.set_stage("config_validation")
        teacher_receipt = verify_run(args.source_run, args.source_manifest_sha256)
        student_receipt = verify_run(args.student_run, args.student_manifest_sha256)
        original = _load_study(args.student_run, student_receipt)
        public_sha = next(row["sha256"] for row in teacher_receipt["files"] if row["path"] == "information_weight_scale_samples.jsonl")
        if (original["analysis_source"]["manifest_sha256"] != teacher_receipt["manifest_sha256"] or
                original["provenance"]["source_file_byte_hash"] != public_sha):
            raise ValueError("source observations do not match frozen Student")
        files = ("nmsim/intensity_distribution.py", "nmsim/information_learning_curve.py", "nmsim/information_student.py",
                 "nmsim/v2_distillation.py", "nmsim/information_weight.py", "nmsim/information_weight_scale.py", "nmsim/v2_attention.py")
        scientific = {"schema": SCHEMA, "epochs": args.epochs, "source_samples_sha256": public_sha,
                      "original_model_hashes": original["provenance"]["model_serialization_hashes"],
                      "selected_action_model": {"name": original["selected_model"],
                          "serialization_hash": original["provenance"]["model_serialization_hashes"][original["selected_model"]]},
                      "split_hash": original["split"]["split_semantic_hash"], "test_partition_used": False,
                      "components": {name: file_sha256(ROOT/name) for name in files}}
        execution = {"schema": SCHEMA, "python": platform.python_version(), "backend": args.backend, "dry_run": args.dry_run,
                     "source_manifest_sha256": teacher_receipt["manifest_sha256"], "student_manifest_sha256": student_receipt["manifest_sha256"],
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
        write_json_exclusive(context.run_dir/"source_receipts.json", {"teacher": teacher_receipt, "student": student_receipt})
        context.manifest["intensity_distribution_identities"] = identity
        context.manifest["legacy_config_scope"] = "lifecycle_only_not_intensity_distribution_science"
        context._write()
        context.set_stage("simulation")
        summary = {"schema_version": SCHEMA, "run_id": context.run_id, "dry_run": args.dry_run,
                   "honest_n": {"new_teacher_requests": 0, "human_participants": 0, "action_model_fits": 0,
                                "sizing_candidates": 0}, "identities": identity,
                   "human_likeness_validated": False}
        if not args.dry_run:
            result = fit_intensity_distributions(read_public_samples(args.source_run, teacher_receipt), original,
                                                epochs=args.epochs, backend=args.backend)
            write_json_exclusive(context.run_dir/"intensity_study.json", result)
            summary.update(selection=result["selection"], parameter_counts=result["parameter_counts"],
                           study_semantic_hash=result["study_semantic_hash"])
            summary["honest_n"].update(sizing_candidates=len(result["models"]),
                                     training_rows=result["training"]["valid_rows"], validation_rows=result["validation"]["valid_rows"],
                                     test_predictions=0)
        context.set_stage("result_export")
        assert_unchanged(args.source_run, teacher_receipt)
        assert_unchanged(args.student_run, student_receipt)
        write_json_exclusive(context.run_dir/"summary.json", summary)
        context.manifest["intensity_distribution_honest_n"] = summary["honest_n"]
        context.finish()
        print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
