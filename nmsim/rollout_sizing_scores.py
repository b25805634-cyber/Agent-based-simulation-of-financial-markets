"""Pure, frozen-candidate sizing scores on already acquired rollout probes.

This module cannot fit models, sample decisions, read files, or call Providers.
The managed caller verifies a completed acquisition and both model sources.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
import math
from typing import Any

from nmsim import information_student as student
from nmsim import intensity_distribution as sizing
from nmsim import rollout_probes as probes


SCHEMA_VERSION = "rollout-sizing-scores/1.0"
ROWS_HASH_SCHEMA = "rollout-sizing-public-probe-rows/1.0"
CANDIDATES = ("legacy_mean", "empirical", "conditional_softmax", "selected")
SIDES = ("buy", "sell")
PREDICTION_TOLERANCE = 1e-12
SOURCE_KINDS = ("openai", "fake_test_teacher")
FAILURE_CODES = ("provider_exception_or_shape", "incomplete_termination",
                 "reported_model_mismatch", "teacher_response_invalid")
SCORE_NAMES = ("crps", "mean_absolute_error", "endpoint1_brier")
ScoreContractError = student.StudentContractError
PUBLIC_ROW_FIELDS = frozenset(("schema_version", "sample_id", "case_id", "replicate",
    "request_index", "source_kind", "status", "decision", "failure_code", "raw_response_sha256",
    "application_attempt_count", "reported_model", "finish_reason", "input_tokens", "output_tokens"))


def descriptor() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "candidates": list(CANDIDATES), "sides": list(SIDES),
            "metrics": list(SCORE_NAMES), "old_mean_distribution": "Dirac at frozen original conditional intensity",
            "model_selection": "none; selected uses original distribution-study validation selection",
            "row_weighting": "equal weight per observed valid buy/sell response",
            "case_weighting": "mean of within-case scores; only cases with that side observed contribute",
            "all_sides_case_weighting": "mean per case over its valid non-hold responses, then equal case weight",
            "missing_policy": "retain failed/missing/hold counts; never synthesize labels or fill empty scores with zero",
            "student_prediction_tolerance": PREDICTION_TOLERANCE,
            "new_provider_requests": 0, "fitted_models": 0, "old_test_access": False,
            "trajectory_scope": "frozen supplied probe-plan states only; not unprobed trajectories or a population",
            "generating_policy": "not inferred from stored legacy Student predictions; consult verified plan lineage"}


def _validate_rows(plan: Mapping[str, Any], rows: Sequence[Mapping[str, Any]], source_kind: str) -> dict[str, Any]:
    if source_kind not in SOURCE_KINDS:
        raise ScoreContractError("unknown probe source_kind")
    if not isinstance(plan, Mapping):
        raise ScoreContractError("probe plan must be a mapping")
    probes.validate_plan(plan)
    if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
        raise ScoreContractError("probe rows must be a sequence")
    cases = {case["case_id"]: case for case in plan["cases"]}
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != PUBLIC_ROW_FIELDS:
            raise ScoreContractError("probe row must contain exactly the public acquisition fields")
        for key in ("replicate", "request_index"):
            student._integer(row[key], key)
        student._integer(row["application_attempt_count"], "application_attempt_count", 1)
        for key in ("input_tokens", "output_tokens"):
            if row[key] is not None:
                student._integer(row[key], key)
        if row["raw_response_sha256"] is not None:
            student._sha(row["raw_response_sha256"], "raw_response_sha256")
        if row["status"] == "failed":
            if row["decision"] is not None or row["failure_code"] not in FAILURE_CODES:
                raise ScoreContractError("failed probe must have no decision and a known failure code")
        elif row["status"] == "valid":
            decision = row["decision"]
            if not isinstance(decision, Mapping) or set(decision) != {"action", "intensity"} or row["failure_code"] is not None:
                raise ScoreContractError("valid probe must have only a public action/intensity and no failure")
            expected_model = "HiggsAI" if source_kind == "openai" else "fake-null-control"
            if row["raw_response_sha256"] is None or row["reported_model"] != expected_model or row["finish_reason"] != "stop":
                raise ScoreContractError("valid probe lacks the required response/model/termination evidence")
            value = student._number(decision["intensity"], "intensity")
            if decision["action"] != "hold" and value <= 0:
                raise ScoreContractError("buy/sell probe intensity must be positive")
            case = cases.get(row["case_id"])
            if case is not None:
                position = case["account_state"]["position_fraction"]
                if (decision["action"] == "sell" and position == 0) or (decision["action"] == "buy" and position == 1):
                    raise ScoreContractError("valid probe contradicts the frozen Teacher feasibility rule")
        else:
            raise ScoreContractError("a probe row must be resolved valid or failed")
    # Central contract checks schema, source kind, exact sample roster, replicate
    # identity, response bounds, hold=0, duplicates and unresolved counts.
    return probes.summarize(plan, list(rows), real_endpoint=source_kind == "openai")


def _validated(plan, rows, original_study, distribution_study, source_kind):
    summary = _validate_rows(plan, rows, source_kind)
    original_predictor = student.make_predictor(original_study["models"][original_study["selected_model"]])
    predictors = {name: sizing.make_distribution_predictor(distribution_study, original_study, candidate=name)
                  for name in ("empirical", "conditional_softmax", "selected")}
    for case in plan["cases"]:
        expected = original_predictor(case["visible_fields"], case["account_state"])
        recorded = case["student_prediction"]
        if set(recorded) != {"action_probs", "intensities"}:
            raise ScoreContractError("case prediction must contain only original public Student outputs")
        for key in expected:
            if len(recorded[key]) != len(expected[key]) or any(abs(left - right) > PREDICTION_TOLERANCE for left, right in zip(expected[key], recorded[key])):
                raise ScoreContractError("probe case prediction is not from the bound original Student")
    return summary, original_predictor, predictors


def _bindings(plan, distribution_study):
    source = distribution_study["source_identity"]
    return {"plan_hash": plan["plan_hash"], "distribution_study_semantic_hash": distribution_study["study_semantic_hash"],
            "distribution_schema_version": distribution_study["schema_version"],
            "frozen_selected_by_action": dict(distribution_study["selection"]["selected_by_action"]),
            "original_action_model_name": source["action_model_name"],
            "original_action_model_serialization_semantic_hash": source["action_model_serialization_semantic_hash"],
            "original_encoder_semantic_hash": source["encoder_semantic_hash"],
            "original_normalized_records_hash_schema": source["original_normalized_records_hash_schema"],
            "original_normalized_records_semantic_hash": source["original_normalized_records_semantic_hash"]}


def _row_identity(rows):
    return {"hash_schema": ROWS_HASH_SCHEMA,
            "semantic_hash": student.stable_hash({"schema_version": ROWS_HASH_SCHEMA,
                "rows": sorted((dict(row) for row in rows), key=lambda row: row["request_index"])})}


def _counts(plan, rows, summary, source_kind):
    valid = [row for row in rows if row["status"] == "valid"]
    actions = Counter(row["decision"]["action"] for row in valid)
    return {"planned_cases": plan["selected_states"], "planned_replicates_per_case": plan["replicates"],
            "planned_logical_requests": plan["planned_logical_requests"],
            "resolved_logical_requests": len(rows), "missing_logical_requests": summary["unresolved_logical_requests"],
            "valid_responses": len(valid), "failed_responses": len(rows) - len(valid),
            "failure_counts": dict(summary["failure_counts"]), "valid_action_counts": {side: actions[side] for side in ("buy", "hold", "sell")},
            "scored_non_hold_responses": actions["buy"] + actions["sell"],
            "source_physical_attempts": sum(row["application_attempt_count"] for row in rows),
            "source_valid_endpoint_responses": len(valid) if source_kind == "openai" else 0,
            "new_teacher_requests": 0, "new_teacher_physical_attempts": 0,
            "fitted_models": 0, "model_selection_performed": False, "old_test_predictions": 0,
            "human_participants": 0}


def validate_scoring_inputs(plan: Mapping[str, Any], rows: Sequence[Mapping[str, Any]],
                            original_study: Mapping[str, Any], distribution_study: Mapping[str, Any], *,
                            source_kind: str) -> dict[str, Any]:
    """Public validation receipt; no fitting or new distribution score table."""
    summary, _old, _predictors = _validated(plan, rows, original_study, distribution_study, source_kind)
    return {"bindings": _bindings(plan, distribution_study), "source_kind": source_kind,
            "public_probe_rows_identity": _row_identity(rows),
            "source_counts": _counts(plan, rows, summary, source_kind)}


def _score(law, observations):
    distribution = sizing.validate_distribution(law)
    mean = sizing.distribution_mean(distribution)
    endpoint1 = math.fsum(probability for value, probability in zip(distribution["support"], distribution["probabilities"]) if value == 1)
    count = len(observations)
    return {"n": count, "predicted_mean": mean, "predicted_endpoint1_probability": endpoint1,
            "observed_endpoint1_count": sum(value == 1 for value in observations),
            "off_support_observations": sum(value not in distribution["support"] for value in observations),
            "crps": math.fsum(sizing.distribution_scores(distribution, value)["crps"] for value in observations) / count if count else None,
            "mean_absolute_error": math.fsum(abs(mean - value) for value in observations) / count if count else None,
            "endpoint1_brier": math.fsum((endpoint1 - float(value == 1)) ** 2 for value in observations) / count if count else None}


def _aggregate(cases, candidate, side, equal_case):
    cells = []
    sides = SIDES if side == "all_sides" else (side,)
    for case in cases:
        selected = [case["scores"][candidate][name] for name in sides]
        count = sum(row["n"] for row in selected)
        if count:
            cells.append({"n": count, **{metric: math.fsum(row[metric] * row["n"] for row in selected if row["n"]) / count for metric in SCORE_NAMES}})
    row_count = sum(row["n"] for row in cells)
    denominator = len(cells) if equal_case else row_count
    return {"scored_responses": row_count, "contributing_cases": len(cells),
            "excluded_cases_without_this_side": len(cases) - len(cells),
            "denominator": denominator, "weighting": "equal_case" if equal_case else "equal_response",
            **{metric: (math.fsum(row[metric] * (1 if equal_case else row["n"]) for row in cells) / denominator if denominator else None) for metric in SCORE_NAMES}}


def score_rollout_sizing(plan: Mapping[str, Any], rows: Sequence[Mapping[str, Any]],
                         original_study: Mapping[str, Any], distribution_study: Mapping[str, Any], *,
                         source_kind: str) -> dict[str, Any]:
    """Evaluate all frozen candidates; neither fit nor select using new labels."""
    summary, original_predictor, predictors = _validated(plan, rows, original_study, distribution_study, source_kind)
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["case_id"]].append(row)
    cases = []
    observed_ids = {row["sample_id"] for row in rows}
    for case in plan["cases"]:
        records = sorted(grouped[case["case_id"]], key=lambda row: row["request_index"])
        valid = [row for row in records if row["status"] == "valid"]
        labels = {side: [row["decision"]["intensity"] for row in valid if row["decision"]["action"] == side] for side in SIDES}
        failures = Counter(row["failure_code"] for row in records if row["status"] == "failed")
        original = original_predictor(case["visible_fields"], case["account_state"])
        laws = {"legacy_mean": {side: {"support": [original["intensities"][index]], "probabilities": [1.0]} for index, side in enumerate(SIDES)}}
        for candidate, predictor in predictors.items():
            laws[candidate] = predictor(case["visible_fields"], case["account_state"])["intensity_distributions"]
        missing = [sample["sample_id"] for sample in plan["samples"] if sample["case_id"] == case["case_id"] and sample["sample_id"] not in observed_ids]
        cases.append({"case_id": case["case_id"], "profile_id": case["profile_id"],
                      "origin": {key: case["origin"][key] for key in ("source_run_id", "cell", "agent_id", "decision_day")},
                      "planned_replicates": plan["replicates"], "resolved_responses": len(records),
                      "valid_responses": len(valid), "failed_responses": sum(failures.values()),
                      "failure_counts": dict(sorted(failures.items())), "missing_responses": len(missing),
                      "missing_sample_ids": missing,
                      "valid_action_counts": {side: sum(row["decision"]["action"] == side for row in valid) for side in ("buy", "hold", "sell")},
                      "observed_sample_ids": [row["sample_id"] for row in records],
                      "predictions": laws,
                      "scores": {candidate: {side: _score(laws[candidate][side], labels[side]) for side in SIDES} for candidate in CANDIDATES}})
    result = {"schema_version": SCHEMA_VERSION, "source_kind": source_kind,
              "evidence_kind": "endpoint_rollout_labels" if source_kind == "openai" else "synthetic_fixture_not_endpoint",
              "bindings": _bindings(plan, distribution_study), "descriptor": descriptor(),
              "public_probe_rows_identity": _row_identity(rows),
              "honest_n": _counts(plan, rows, summary, source_kind), "cases": cases,
              "aggregate": {candidate: {method: {side: _aggregate(cases, candidate, side, equal_case=method == "equal_case") for side in (*SIDES, "all_sides")}
                                         for method in ("row_weighted", "equal_case")} for candidate in CANDIDATES},
              "selection_performed": False, "human_likeness_validated": False,
              "interpretation": [
                  "The four predictors and previously selected sizing laws are frozen. These results do not select or retrain a model.",
                  "Only observed buy/sell responses contribute to conditional sizing scores. Holds, failures and missing responses retain separate counts.",
                  "CRPS scores a predictive distribution; MAE scores its mean; endpoint Brier scores exact intensity=1. They answer different questions.",
                  "Equal-case scoring excludes cases with no valid response of the scored side; their counts remain explicit, not zero-filled.",
                  "Repeated endpoint requests, including K=5, are not independent people or a population sample; no significance or acceptance threshold is claimed.",
                  "These scores cover only supplied frozen probe-plan states. Their stored legacy Student predictions do not identify the trajectory-generating sizing policy.",
                  "Unprobed trajectories, including newly visited stochastic-size states, require their own frozen probes and evidence.",
                  "No original training/test labels, private rationale or raw responses are accessed by this scoring function.",
              ]}
    result["score_semantic_hash"] = student.stable_hash(result)
    return result


__all__ = ["SCHEMA_VERSION", "ROWS_HASH_SCHEMA", "CANDIDATES", "SIDES", "PREDICTION_TOLERANCE", "SOURCE_KINDS",
           "ScoreContractError", "descriptor", "validate_scoring_inputs", "score_rollout_sizing"]
