"""Managed offline scores for fixed sizing laws on finished rollout probes."""
from __future__ import annotations

import json
from pathlib import Path
import platform
import sys
from typing import Sequence

from experiments.information_market import _load_study, _load_distribution
from nmsim.config import Config
from nmsim.information_artifacts import (canonical_hash, file_sha256, read_json, verify_run,
    assert_unchanged, write_json_exclusive, ensure_separate_output, preflight_output_separation, _unique_object)
from nmsim.managed_cli import RaisingArgumentParser, bootstrap_cli, fail_cli, BootstrapCLIError
from nmsim.run_context import ManagedRunContext


SCHEMA_VERSION = "rollout-sizing-scores-experiment/1.0"
COMMAND = "python -m experiments.rollout_sizing_scores"
DEFAULT_OUT = "results_rollout_sizing_scores"
ROOT = Path(__file__).resolve().parents[1]


def parser():
    result = RaisingArgumentParser(description=__doc__, allow_abbrev=False)
    for name in ("source", "model", "distribution"):
        result.add_argument("--" + name + "-run", type=Path, required=True)
        result.add_argument("--" + name + "-manifest-sha256")
    result.add_argument("--out", default=DEFAULT_OUT)
    result.add_argument("--run-id")
    result.add_argument("--dry-run", action="store_true")
    result.add_argument("--version", action="version", version=SCHEMA_VERSION)
    return result


def _load_probe_source(root, receipt):
    """Read public files only after verify_run has proved finished lifecycle."""
    from nmsim import rollout_probes as probes
    required = {"probe_plan.json", "probe_samples.jsonl", "fidelity_summary.json", "identities.json"}
    if not required <= set(receipt["registered_artifact_paths"]):
        raise ValueError("source lacks registered public acquisition artifacts")
    manifest = read_json(root/"run_manifest.json")
    if manifest.get("managed_context", {}).get("run_kind") != "rollout_fidelity_acquire":
        raise ValueError("source must be a completed rollout acquisition, not a plan or market")
    plan = read_json(root/"probe_plan.json")
    probes.validate_plan(plan)
    identities = read_json(root/"identities.json")
    if identities.get("schema_version") != "rollout-fidelity-identities/1.0":
        raise ValueError("unsupported acquisition identities schema")
    hashes = {"schema_version": identities["schema_version"]}
    for name in ("scientific", "model_request", "execution"):
        expected = canonical_hash(identities[name+"_config"])
        if identities.get(name+"_config_hash") != expected:
            raise ValueError("acquisition config semantic hash mismatch")
        hashes[name+"_config_hash"] = expected
    if identities.get("full_effective_config_hash") != canonical_hash(hashes):
        raise ValueError("acquisition full effective config hash mismatch")
    kind = identities["model_request_config"].get("provider")
    execution = identities["execution_config"]
    if kind not in ("openai", "fake_test_teacher") or execution.get("task") != "acquire" or execution.get("dry_run") is not False:
        raise ValueError("source must identify a real or explicitly fake completed acquisition")
    if execution.get("live") is not (kind == "openai"):
        raise ValueError("acquisition live gate disagrees with recorded source kind")
    if identities["scientific_config"].get("plan_hash") != plan["plan_hash"]:
        raise ValueError("acquisition scientific identity differs from frozen probe plan")
    rows = []
    with (root/"probe_samples.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                raise ValueError("empty public probe sample row")
            rows.append(json.loads(line, object_pairs_hook=_unique_object,
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("nonfinite public probe JSON"))))
    expected_sha = next(row["sha256"] for row in receipt["files"] if row["path"] == "probe_samples.jsonl")
    if file_sha256(root/"probe_samples.jsonl") != expected_sha:
        raise ValueError("public probe samples changed during read")
    summary = read_json(root/"fidelity_summary.json")
    expected_summary = probes.summarize(plan, rows, real_endpoint=kind == "openai")
    if any(summary.get(key) != value for key, value in expected_summary.items()):
        raise ValueError("acquisition summary disagrees with verified public samples")
    if expected_summary["unresolved_logical_requests"] != 0:
        raise ValueError("finished acquisition contains unresolved frozen requests")
    if (summary.get("physical_attempts") != sum(row.get("application_attempt_count", 0) for row in rows)
            or summary.get("attempted_logical_requests") != len(rows)):
        raise ValueError("acquisition physical/logical accounting mismatch")
    return plan, rows, kind


def main(argv: Sequence[str] | None = None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        preflight_output_separation(argv, DEFAULT_OUT)
    except ValueError:
        print("provenance_not_created_reason=output_overlaps_historical_input", file=sys.stderr)
        raise SystemExit(2)
    try:
        boot = bootstrap_cli(argv, default_out=DEFAULT_OUT, command_identity=COMMAND)
    except BootstrapCLIError as error:
        fail_cli(None, error)
    try:
        args = parser().parse_args(argv)
        for name in ("source", "model", "distribution"):
            digest = getattr(args, name+"_manifest_sha256")
            if digest is not None and (len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest)):
                raise ValueError("invalid manifest SHA-256")
    except (ValueError, TypeError, OSError) as error:
        fail_cli(boot, error)
    inputs = {name: getattr(args, name+"_run") for name in ("source", "model", "distribution")}
    try:
        ensure_separate_output(Path(args.out), inputs.values())
    except ValueError:
        print("provenance_not_created_reason=output_overlaps_historical_input", file=sys.stderr)
        raise SystemExit(2)
    cfg = Config(provider="mock", n_rounds=0, n_llm_agents=0, n_noise_agents=0,
                 cache_enabled=False, openai_base_url="", openai_api_key="", out_dir=args.out)
    try:
        context = ManagedRunContext.create(cfg, out_root=args.out, run_id=args.run_id, repo_root=ROOT,
            command_identity=COMMAND, run_kind="rollout_sizing_scores", planned_simulation_runs=0,
            input_paths={name: path/"run_manifest.json" for name, path in inputs.items()},
            research_profile={"profile_id": SCHEMA_VERSION, "persona_contract": {"applicable": False},
                              "prompt_contract": {"provider_access": "none"}})
    except (ValueError, OSError) as error:
        print("provenance_not_created_reason="+type(error).__name__, file=sys.stderr)
        raise SystemExit(2)
    with context:
        from nmsim.rollout_sizing_scores import descriptor, validate_scoring_inputs, score_rollout_sizing
        context.register_llm_runtime(provider="none", mode="offline_frozen_rollout_sizing_scores",
                                     network_access=False, provider_calls=0, cache_enabled=False)
        context.set_stage("config_validation")
        # A running/failed acquisition is rejected here, before opening its plan
        # or response payload. It cannot be scored as an interim completed run.
        receipts = {name: verify_run(path, getattr(args, name+"_manifest_sha256")) for name, path in inputs.items()}
        plan, rows, kind = _load_probe_source(args.source_run, receipts["source"])
        original = _load_study(args.model_run, receipts["model"])
        distribution = _load_distribution(args.distribution_run, receipts["distribution"], original)
        validation = validate_scoring_inputs(plan, rows, original, distribution, source_kind=kind)
        files = ("nmsim/rollout_sizing_scores.py", "nmsim/rollout_probes.py", "nmsim/intensity_distribution.py",
                 "nmsim/information_student.py", "nmsim/information_weight.py", "nmsim/v2_attention.py",
                 "nmsim/v2_distillation.py", "nmsim/v2_market_experiment.py")
        scientific = {"schema_version": SCHEMA_VERSION, "score_contract": descriptor(),
                      "source_kind": kind, "bindings": validation["bindings"],
                      "public_probe_samples_byte_sha256": next(row["sha256"] for row in receipts["source"]["files"] if row["path"] == "probe_samples.jsonl"),
                      "components": {name: file_sha256(ROOT/name) for name in files}}
        request = {"schema_version": SCHEMA_VERSION, "provider_access": "none", "new_teacher_requests": 0}
        execution = {"schema_version": SCHEMA_VERSION, "dry_run": args.dry_run,
                     "python": platform.python_version(), "platform": platform.platform(),
                     "inputs": {name: {"manifest_sha256": value["manifest_sha256"], "snapshot_hash": value["snapshot_hash"]} for name, value in receipts.items()},
                     "entrypoint_byte_sha256": file_sha256(Path(__file__)),
                     "artifact_io_byte_sha256": file_sha256(ROOT/"nmsim/information_artifacts.py"),
                     "source_loader_byte_sha256": file_sha256(ROOT/"experiments/information_market.py")}
        identity = {"schema_version": SCHEMA_VERSION, "scientific_config_hash": canonical_hash(scientific),
                    "model_request_config_hash": canonical_hash(request), "execution_config_hash": canonical_hash(execution)}
        identity["full_effective_config_hash"] = canonical_hash(identity)
        write_json_exclusive(context.run_dir/"identities.json", {**identity, "scientific_config": scientific,
                             "model_request_config": request, "execution_config": execution})
        write_json_exclusive(context.run_dir/"source_receipts.json", receipts)
        write_json_exclusive(context.run_dir/"input_validation.json", validation)
        context.manifest["rollout_sizing_scores_identities"] = identity
        context.manifest["legacy_config_scope"] = "lifecycle_only_not_rollout_sizing_science"
        context._write()
        summary = {"schema_version": SCHEMA_VERSION, "run_id": context.run_id, "dry_run": args.dry_run,
                   "source_kind": kind, "identities": identity, "source_counts": validation["source_counts"],
                   "honest_n": {"scored_source_responses": 0, "new_teacher_requests": 0, "human_participants": 0,
                                "fitted_models": 0, "model_selection_performed": False, "old_test_predictions": 0},
                   "human_likeness_validated": False, "trajectory_scope": descriptor()["trajectory_scope"]}
        if not args.dry_run:
            context.set_stage("simulation")
            result = score_rollout_sizing(plan, rows, original, distribution, source_kind=kind)
            write_json_exclusive(context.run_dir/"rollout_sizing_scores.json", result)
            summary.update(score_semantic_hash=result["score_semantic_hash"], aggregate=result["aggregate"],
                           frozen_selected_by_action=result["bindings"]["frozen_selected_by_action"])
            summary["honest_n"]["scored_source_responses"] = result["honest_n"]["scored_non_hold_responses"]
        context.set_stage("result_export")
        for name, path in inputs.items():
            assert_unchanged(path, receipts[name])
        write_json_exclusive(context.run_dir/"summary.json", summary)
        context.manifest["rollout_sizing_scores_honest_n"] = summary["honest_n"]
        context.finish()
        print(json.dumps(summary, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
