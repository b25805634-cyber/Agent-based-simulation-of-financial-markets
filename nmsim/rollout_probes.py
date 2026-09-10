"""Pure, prospectively frozen probes of a Student's own visited states.

Selection never uses Teacher answers. Missing fields remain missing. A probe
measures endpoint fidelity at selected states, not population or human realism.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
import json
import math
import statistics
from typing import Any, Iterable, Mapping

from nmsim.information_artifacts import canonical_hash
from nmsim.information_student import encode_observation
from nmsim.information_weight import FIELD_SEMANTICS, PROFILE_FIELDS, SYSTEM_PROMPT
from nmsim.v2_attention import V2AttentionState, TIME_SEMANTICS, RESPONSE_SCHEMA_VERSION
from nmsim.v2_market_experiment import _normalise_prediction


PLAN_SCHEMA = "rollout-fidelity-plan/1.0"
PROMPT_SCHEMA = "available-rollout-teacher-prompt/1.0"
SAMPLE_SCHEMA = "rollout-fidelity-sample/1.0"


def render_prompt(visible: Mapping[str, Any], account: Mapping[str, Any]) -> dict:
    encode_observation(visible, account)
    if "intraday_range_5d_mean" in visible:
        raise ValueError("rollout probes require observed inputs, not the legacy range proxy")
    payload = {"prompt_schema_version": PROMPT_SCHEMA,
               "response_schema_version": RESPONSE_SCHEMA_VERSION,
               "time_semantics": TIME_SEMANTICS,
               "observable_state": dict(visible), "account_state": dict(account),
               "field_semantics": {field: FIELD_SEMANTICS[field] for field in (*visible, *account)},
               "missing_value_rule": "Absent market fields are unavailable, not zero. An account mask of 0 marks an unavailable paired value with a numeric 0 placeholder.",
               "representation": "These are effective bounded numeric observations. Do not invent unavailable fields."}
    user = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    prompt = {"system": SYSTEM_PROMPT, "user": user}
    return {**prompt, "prompt_hash": canonical_hash(prompt)}


def infeasible_action_mass(prediction: Mapping[str, Any], account: Mapping[str, Any]) -> float:
    value = _normalise_prediction(prediction)
    position = account["position_fraction"]
    return (value["action_probs"][2] if position == 0 else
            value["action_probs"][0] if position == 1 else 0.0)


def build_plan(candidates: Iterable[Mapping[str, Any]], *, max_states: int = 24,
               replicates: int = 5, seed: int = 20260909) -> dict:
    """SHA-ranked, round-robin stratification over actual visited states.

    Strata are profile x quote rule x time-third x portfolio boundary. Selection
    is descriptive coverage sampling, not a population-weighted error estimate.
    At most max_states candidates per observed stratum are retained in memory.
    """
    for name, value, minimum in (("max_states", max_states, 1), ("replicates", replicates, 2), ("seed", seed, 0)):
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError(f"invalid {name}")
    buckets: dict[tuple, dict[str, dict]] = defaultdict(dict)
    counts = Counter()
    candidate_count = 0
    for candidate in candidates:
        visible, account = candidate["visible_fields"], candidate["account_state"]
        profile = candidate["profile_id"]
        if profile not in PROFILE_FIELDS:
            raise ValueError("unknown rollout profile")
        expected = set(PROFILE_FIELDS[profile]) - {"intraday_range_5d_mean"}
        if set(visible) not in (expected, expected - {"turnover_change_5d"}):
            raise ValueError("unexpected available rollout field set")
        prompt = render_prompt(visible, account)
        base_state = V2AttentionState.from_mapping(candidate["base_state"])
        if any(base_state.to_dict()[key] != value for key, value in account.items()):
            raise ValueError("account and parser state disagree")
        prediction = _normalise_prediction(candidate["student_prediction"])
        day, horizon = candidate["decision_day"], candidate["rounds"]
        if (isinstance(day, bool) or not isinstance(day, int) or isinstance(horizon, bool)
                or not isinstance(horizon, int) or not 0 <= day < horizon):
            raise ValueError("invalid rollout time")
        position = account["position_fraction"]
        regime = "empty" if position == 0 else "full" if position == 1 else "interior"
        stratum = (profile, candidate["quote_rule"], min(2, day * 3 // horizon), regime)
        case_id = canonical_hash({"schema": PROMPT_SCHEMA, "visible_fields": dict(visible), "account_state": dict(account)})
        origin = {key: candidate[key] for key in ("source_run_id", "cell", "agent_id", "decision_day")}
        item = {"case_id": case_id, "profile_id": profile, "stratum": list(stratum),
                "origin": origin, "visible_fields": dict(visible), "account_state": dict(account),
                "base_state": base_state.to_dict(), "student_prediction": prediction,
                "student_infeasible_action_mass": infeasible_action_mass(prediction, account),
                "prompt": prompt}
        candidate_count += 1
        counts[stratum] += 1
        # Prefer a deterministic origin when a visible state is encountered again.
        current = buckets[stratum].get(case_id)
        if current is None or canonical_hash(origin) < canonical_hash(current["origin"]):
            buckets[stratum][case_id] = item
        if len(buckets[stratum]) > max_states:
            worst = max(buckets[stratum], key=lambda key: canonical_hash([seed, key]))
            del buckets[stratum][worst]
    if not candidate_count:
        raise ValueError("no eligible rollout states")
    strata = sorted(buckets, key=lambda key: canonical_hash([seed, list(key)]))
    ranked = {key: sorted(buckets[key].values(), key=lambda item: canonical_hash([seed, item["case_id"]])) for key in strata}
    selected, seen = [], set()
    for depth in range(max_states):
        for key in strata:
            if depth >= len(ranked[key]):
                continue
            item = ranked[key][depth]
            if item["case_id"] in seen:
                continue
            selected.append(item)
            seen.add(item["case_id"])
            if len(selected) == max_states:
                break
        if len(selected) == max_states:
            break
    samples = []
    for replicate in range(replicates):
        # Replicates span complete rounds of cases instead of adjacent repeats.
        for offset in range(len(selected)):
            index = (offset + replicate) % len(selected)
            item = selected[index]
            samples.append({"sample_id": canonical_hash([PLAN_SCHEMA, item["case_id"], replicate]),
                            "case_id": item["case_id"], "case_index": index,
                            "replicate": replicate, "request_index": len(samples),
                            "prompt_hash": item["prompt"]["prompt_hash"]})
    selected_counts = Counter(tuple(item["stratum"]) for item in selected)
    plan = {"schema_version": PLAN_SCHEMA, "seed": seed, "max_states": max_states,
            "replicates": replicates, "candidate_agent_rounds": candidate_count,
            "selected_states": len(selected), "planned_logical_requests": len(samples),
            "cases": selected, "samples": samples,
            "coverage": [{"stratum": list(key), "candidate_agent_rounds": counts[key],
                          "selected_states": selected_counts[key]} for key in sorted(counts)],
            "selection": "state-SHA rank; round-robin over seeded strata; Teacher-answer blind",
            "evidence_scope": "selected rollout-state endpoint fidelity; not population or human validation"}
    plan["plan_hash"] = canonical_hash(plan)
    return plan


def validate_plan(plan: Mapping[str, Any]) -> None:
    if plan.get("schema_version") != PLAN_SCHEMA:
        raise ValueError("unknown probe plan schema")
    body = {key: value for key, value in plan.items() if key != "plan_hash"}
    if canonical_hash(body) != plan.get("plan_hash"):
        raise ValueError("probe plan hash mismatch")
    cases = plan["cases"]
    if not cases or len(cases) != plan["selected_states"]:
        raise ValueError("incorrect probe case count")
    if len({case["case_id"] for case in cases}) != len(cases):
        raise ValueError("duplicate probe case")
    for case in cases:
        if case["prompt"] != render_prompt(case["visible_fields"], case["account_state"]):
            raise ValueError("probe prompt differs from available observation")
        expected = canonical_hash({"schema": PROMPT_SCHEMA, "visible_fields": case["visible_fields"], "account_state": case["account_state"]})
        if case["case_id"] != expected:
            raise ValueError("probe case identity mismatch")
        state = V2AttentionState.from_mapping(case["base_state"]).to_dict()
        if any(state[key] != value for key, value in case["account_state"].items()):
            raise ValueError("probe parser state disagrees with visible account")
        if case["student_infeasible_action_mass"] != infeasible_action_mass(case["student_prediction"], case["account_state"]):
            raise ValueError("infeasible action mass audit mismatch")
    expected_samples = []
    for replicate in range(plan["replicates"]):
        for offset in range(len(cases)):
            index = (offset + replicate) % len(cases)
            case = cases[index]
            expected_samples.append({"sample_id": canonical_hash([PLAN_SCHEMA, case["case_id"], replicate]),
                                     "case_id": case["case_id"], "case_index": index,
                                     "replicate": replicate, "request_index": len(expected_samples),
                                     "prompt_hash": case["prompt"]["prompt_hash"]})
    if plan["samples"] != expected_samples or plan["planned_logical_requests"] != len(expected_samples):
        raise ValueError("probe request order differs from frozen replication plan")


def summarize(plan: Mapping[str, Any], rows: list[dict], *, real_endpoint: bool) -> dict:
    validate_plan(plan)
    expected = {item["sample_id"]: item for item in plan["samples"]}
    seen, by_case, failures = set(), defaultdict(list), Counter()
    for row in rows:
        if row.get("schema_version") != SAMPLE_SCHEMA or row.get("source_kind") != ("openai" if real_endpoint else "fake_test_teacher"):
            raise ValueError("probe response source/schema mismatch")
        sid = row.get("sample_id")
        if sid not in expected or sid in seen or row["case_id"] != expected[sid]["case_id"]:
            raise ValueError("unknown/duplicate/mismatched probe response")
        if any(row.get(key) != expected[sid][key] for key in ("replicate", "request_index")):
            raise ValueError("probe response order identity mismatch")
        seen.add(sid)
        if row["status"] == "valid":
            decision = row["decision"]
            if (decision["action"] not in ("buy", "hold", "sell") or
                    isinstance(decision["intensity"], bool) or not isinstance(decision["intensity"], (float, int)) or
                    not 0 <= decision["intensity"] <= 1 or (decision["action"] == "hold" and decision["intensity"] != 0)):
                raise ValueError("invalid public probe decision")
            by_case[row["case_id"]].append(decision)
        elif row["status"] == "failed":
            failures[row["failure_code"]] += 1
        else:
            raise ValueError("unresolved is not a completed response")
    cases = []
    for case in plan["cases"]:
        valid = by_case[case["case_id"]]
        probabilities = case["student_prediction"]["action_probs"]
        action_counts = Counter(row["action"] for row in valid)
        truth = [action_counts[name]/len(valid) for name in ("buy", "hold", "sell")] if valid else None
        zero_probability = sum(count for count, probability in zip(
            [action_counts[name] for name in ("buy", "hold", "sell")], probabilities) if probability == 0)
        nll = (sum(-math.log(probabilities[("buy", "hold", "sell").index(row["action"])]) for row in valid) / len(valid)
               if valid and not zero_probability else None)
        conditional = {}
        for index, action in enumerate(("buy", "sell")):
            values = [row["intensity"] for row in valid if row["action"] == action]
            student_value = case["student_prediction"]["intensities"][index]
            conditional[action] = {"n": len(values), "student_intensity": student_value,
                "teacher_mean": statistics.mean(values) if values else None,
                "teacher_sample_variance": statistics.variance(values) if len(values) >= 2 else None,
                "mean_absolute_error": statistics.mean(abs(v-student_value) for v in values) if values else None}
        cases.append({"case_id": case["case_id"], "origin": deepcopy(case["origin"]),
                      "valid_replicates": len(valid), "planned_replicates": plan["replicates"],
                      "teacher_action_counts": dict(action_counts), "teacher_empirical_probs": truth,
                      "student_action_probs": probabilities, "student_cross_entropy": nll,
                      "zero_predicted_probability_responses": zero_probability,
                      "conditional_intensity": conditional,
                      "total_variation": sum(abs(a-b) for a, b in zip(truth, probabilities))/2 if truth else None,
                      "student_infeasible_action_mass": case["student_infeasible_action_mass"]})
    return {"schema_version": "rollout-fidelity-summary/1.0", "plan_hash": plan["plan_hash"],
            "real_endpoint": real_endpoint, "human_participants": 0,
            "planned_logical_requests": len(expected), "resolved_logical_requests": len(rows),
            "unresolved_logical_requests": len(expected)-len(rows),
            "valid_responses": sum(len(v) for v in by_case.values()),
            "valid_endpoint_responses": sum(len(v) for v in by_case.values()) if real_endpoint else 0,
            "failure_counts": dict(failures), "cases": cases,
            "acceptance_status": "descriptive_only_no_preregistered_acceptance_threshold",
            "interpretation": "Repeated endpoint choices are not independent people; finite K empirical probabilities are noisy."}


__all__ = ["PLAN_SCHEMA", "PROMPT_SCHEMA", "SAMPLE_SCHEMA", "render_prompt", "build_plan",
           "validate_plan", "infeasible_action_mass", "summarize"]
