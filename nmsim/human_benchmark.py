"""Neutral paired tasks and descriptive human/model comparison, without I/O.

Tasks are not observations.  Synthetic fixtures never count as human evidence.
This module deliberately has no universal human-likeness pass threshold.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import math
import re
import statistics
from typing import Any, Mapping, Sequence

from nmsim.information_market import prior_policy, run_market
from nmsim.information_weight import ACCOUNT8, FIELD_RANGES, FIELD_SEMANTICS, PROFILE_FIELDS, PROFILE_IDS
from nmsim.v2_attention import ACTION_ORDER
from nmsim.v2_market_experiment import _normalise_prediction

TASK_SCHEMA = "human-information-market-task/1.0.0"
SCORE_SCHEMA = "human-information-market-comparison/1.0.0"
INSTRUCTIONS = (
    "At the daily decision point, use the supplied numeric market observations "
    "and your account state to choose buy, hold, or sell for one asset. "
    "For buy, intensity is the fraction of currently feasible cash to use; "
    "for sell, it is the fraction of held shares to release. Intensity must "
    "be between 0 and 1. For hold, intensity is 0. Your order is submitted "
    "to the next daily batch. Report only action and intensity."
)
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z")


def build_tasks(seed: int = 20260909) -> list[dict[str, Any]]:
    """Build 12 paired contrasts (24 tasks), with no simulated response labels."""
    simulation = run_market(prior_policy((0, 1, 0)), seed=seed, agents=8, rounds=11)
    tasks = []
    for replica, day in enumerate((0, 10)):
        row = simulation["ledger"][day]
        market = row["market_effective"]
        account = dict(row["decisions"][0]["account_state"])
        variants = [
            ("visible_price_financial", PROFILE_IDS[0], PROFILE_IDS[1], {}, {}),
            ("visible_price_news", PROFILE_IDS[0], PROFILE_IDS[2], {}, {}),
            ("position_fraction", PROFILE_IDS[3], PROFILE_IDS[3],
             {"position_fraction": 0.2, "log10_wealth": math.log10(2000000)},
             {"position_fraction": 0.8, "log10_wealth": math.log10(2000000)}),
            ("account_wealth", PROFILE_IDS[3], PROFILE_IDS[3],
             {"position_fraction": 0.5, "log10_wealth": math.log10(2000000)},
             {"position_fraction": 0.5, "log10_wealth": math.log10(20000000)}),
            ("purchase_reference_return", PROFILE_IDS[3], PROFILE_IDS[3],
             {"unrealized_return": -0.2, "unrealized_return_mask": 1,
              "days_since_trade_scaled": 1.0, "days_since_trade_scaled_mask": 1},
             {"unrealized_return": 0.2, "unrealized_return_mask": 1,
              "days_since_trade_scaled": 1.0, "days_since_trade_scaled_mask": 1}),
            ("last_trade_elapsed", PROFILE_IDS[3], PROFILE_IDS[3],
             {"unrealized_return": 0.0, "unrealized_return_mask": 1,
              "days_since_trade_scaled": 0.05, "days_since_trade_scaled_mask": 1},
             {"unrealized_return": 0.0, "unrealized_return_mask": 1,
              "days_since_trade_scaled": 1.0, "days_since_trade_scaled_mask": 1}),
        ]
        for index, (contrast, first, second, first_account, second_account) in enumerate(variants):
            pair_id = f"pair-{replica * 6 + index + 1:02d}"
            for condition, profile, changes in (("a", first, first_account), ("b", second, second_account)):
                own = {**account, **changes}
                tasks.append({
                    "schema": TASK_SCHEMA, "task_id": f"{pair_id}-{condition}",
                    "pair_id": pair_id, "condition": condition,
                    "contrast": contrast, "decision_day": day,
                    "visible_fields": {key: market[key] for key in PROFILE_FIELDS[profile]},
                    "account_state": own, "instructions": INSTRUCTIONS,
                    "field_semantics": {key: FIELD_SEMANTICS[key]
                                        for key in (*PROFILE_FIELDS[profile], *ACCOUNT8)},
                    "source_kind": "synthetic_task_not_human_response",
                    "design_note": "matched numerical vignette; account contrasts are counterfactual endowments/history",
                })
    return tasks


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"{name} must be an anonymous alphanumeric identifier")
    return value


def _task_map(tasks: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    result = {}
    pairs: dict[str, set[str]] = defaultdict(set)
    for task in tasks:
        task_id = _identifier(task.get("task_id"), "task_id")
        pair_id = _identifier(task.get("pair_id"), "pair_id")
        condition = task.get("condition")
        if task_id in result or condition not in ("a", "b") or condition in pairs[pair_id]:
            raise ValueError("tasks require unique ids and one a/b condition per pair")
        if set(task.get("account_state", {})) != set(ACCOUNT8):
            raise ValueError("task account state must contain A8")
        if set(task.get("visible_fields", {})) not in [set(fields) for fields in PROFILE_FIELDS.values()]:
            raise ValueError("task visible fields must match a registered information allocation")
        for name, value in {**task["visible_fields"], **task["account_state"]}.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError("task values must be finite numeric values")
            low, high = FIELD_RANGES[name]
            if not low <= value <= high:
                raise ValueError(f"task value {name} lies outside its closed domain")
        result[task_id] = task
        pairs[pair_id].add(condition)
    if any(conditions != {"a", "b"} for conditions in pairs.values()):
        raise ValueError("each paired task needs both conditions")
    return result


def _js(first: Sequence[float], second: Sequence[float]) -> float:
    middle = [(left + right) / 2 for left, right in zip(first, second)]
    return sum(value * math.log(value / mid) / 2
               for values in (first, second) for value, mid in zip(values, middle) if value > 0)


def score_responses(tasks: Sequence[Mapping[str, Any]],
                    human_rows: Sequence[Mapping[str, Any]],
                    prediction_rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Compare same-task numerical predictions to explicitly human responses.

    Human rows contain exactly subject_id/task_id/action/intensity/source_kind.
    Model rows contain task_id/action_probs/intensities and optional source_kind
    (model or synthetic_fixture). Repeated subject/task rows fail, rather than
    increasing N. source_kind is a caller assertion, not identity verification.
    """
    task_by_id = _task_map(tasks)
    by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen = set()
    subjects = set()
    fixture_count = 0
    for row in human_rows:
        if set(row) != {"subject_id", "task_id", "action", "intensity", "source_kind"}:
            raise ValueError("human response has unexpected or missing fields")
        subject = _identifier(row["subject_id"], "subject_id")
        task_id = _identifier(row["task_id"], "task_id")
        if task_id not in task_by_id:
            raise ValueError("response references unknown task")
        if row["source_kind"] not in ("human", "synthetic_fixture"):
            raise ValueError("response source_kind must be human or synthetic_fixture")
        action, intensity = row["action"], row["intensity"]
        if action not in ACTION_ORDER or isinstance(intensity, bool) or not isinstance(intensity, (int, float)) or not 0 <= intensity <= 1:
            raise ValueError("invalid action/intensity")
        if action == "hold" and intensity != 0:
            raise ValueError("hold intensity must be zero")
        identity = (subject, task_id, row["source_kind"])
        if identity in seen:
            raise ValueError("duplicate subject/task response")
        seen.add(identity)
        if row["source_kind"] == "synthetic_fixture":
            fixture_count += 1
            continue
        subjects.add(subject)
        by_task[task_id].append(dict(row))
    predictions = {}
    seen_model_tasks = set()
    model_fixture_count = 0
    for row in prediction_rows:
        if set(row) - {"task_id", "action_probs", "intensities", "source_kind"}:
            raise ValueError("prediction has unexpected fields")
        task_id = _identifier(row.get("task_id"), "task_id")
        if task_id not in task_by_id or task_id in seen_model_tasks:
            raise ValueError("unknown or duplicate model task")
        seen_model_tasks.add(task_id)
        source = row.get("source_kind", "model")
        if source not in ("model", "synthetic_fixture"):
            raise ValueError("model prediction source_kind is invalid")
        if source == "synthetic_fixture":
            model_fixture_count += 1
            continue
        predictions[task_id] = _normalise_prediction(row)
    per_task = []
    brier_values, nll_values, active_errors = [], [], []
    zero_probability_count = 0
    for task_id in task_by_id:
        rows = by_task.get(task_id, [])
        prediction = predictions.get(task_id)
        if not rows or prediction is None:
            continue
        counts = Counter(row["action"] for row in rows)
        empirical = [counts[action] / len(rows) for action in ACTION_ORDER]
        probs, intensities = prediction["action_probs"], prediction["intensities"]
        brier, nll, conditional = [], [], {"buy": [], "sell": []}
        zero_count = 0
        for row in rows:
            label = ACTION_ORDER.index(row["action"])
            brier.append(sum((value - int(index == label)) ** 2 for index, value in enumerate(probs)))
            if probs[label] == 0:
                zero_count += 1
            else:
                nll.append(-math.log(probs[label]))
            if row["action"] != "hold":
                predicted = intensities[0 if row["action"] == "buy" else 1]
                conditional[row["action"]].append(abs(predicted - row["intensity"]))
        per_task.append({
            "task_id": task_id, "human_participants": len(rows),
            "human_action_counts": {action: counts[action] for action in ACTION_ORDER},
            "human_action_distribution": empirical, "model_action_distribution": probs,
            "js_divergence_nats": _js(empirical, probs),
            "mean_brier_sum_three_classes": statistics.fmean(brier),
            "mean_nll_nats": None if zero_count else statistics.fmean(nll),
            "nll_status": "infinite_zero_predicted_probability" if zero_count else "finite",
            "zero_predicted_probability_responses": zero_count,
            "conditional_intensity_mae": {action: statistics.fmean(values) if values else None
                                          for action, values in conditional.items()},
            "conditional_intensity_response_counts": {action: len(values) for action, values in conditional.items()},
        })
        brier_values.extend(brier)
        nll_values.extend(nll)
        zero_probability_count += zero_count
        active_errors.extend(conditional["buy"] + conditional["sell"])
    pair_tasks: dict[str, dict[str, str]] = defaultdict(dict)
    for task in tasks:
        pair_tasks[task["pair_id"]][task["condition"]] = task["task_id"]
    contrasts = []
    sign = {"buy": 1, "hold": 0, "sell": -1}
    for pair_id, conditions in pair_tasks.items():
        first, second = conditions["a"], conditions["b"]
        left = {row["subject_id"]: row for row in by_task.get(first, [])}
        right = {row["subject_id"]: row for row in by_task.get(second, [])}
        complete = sorted(set(left) & set(right))
        direction = [sign[right[key]["action"]] - sign[left[key]["action"]] for key in complete]
        amount = [sign[right[key]["action"]] * right[key]["intensity"] -
                  sign[left[key]["action"]] * left[key]["intensity"] for key in complete]
        model_direction = model_amount = None
        if first in predictions and second in predictions:
            def expected(task_id):
                value = predictions[task_id]
                probs, intensity = value["action_probs"], value["intensities"]
                return probs[0] - probs[2], probs[0] * intensity[0] - probs[2] * intensity[1]
            a, b = expected(first), expected(second)
            model_direction, model_amount = b[0] - a[0], b[1] - a[1]
        human_direction = statistics.fmean(direction) if direction else None
        human_amount = statistics.fmean(amount) if amount else None
        contrasts.append({
            "pair_id": pair_id, "difference": "condition_b_minus_condition_a",
            "complete_human_participants": len(complete),
            "human_mean_signed_action_change": human_direction,
            "model_expected_signed_action_change": model_direction,
            "human_mean_signed_intensity_change": human_amount,
            "model_expected_signed_intensity_change": model_amount,
            "signed_action_contrast_absolute_error": abs(human_direction - model_direction)
            if human_direction is not None and model_direction is not None else None,
        })
    count = sum(len(rows) for rows in by_task.values())
    return {
        "schema": SCORE_SCHEMA,
        "status": ("pending_human_responses" if not count else "pending_matching_predictions"
                   if not per_task else "descriptive_comparison_available"),
        "human_likeness_validated": False,
        "task_count": len(task_by_id), "human_participants": len(subjects),
        "human_responses": count, "human_tasks_answered": len(by_task),
        "scored_task_count": len(per_task), "scored_human_responses": len(brier_values),
        "excluded_synthetic_response_count": fixture_count,
        "excluded_synthetic_prediction_count": model_fixture_count,
        "missing_model_task_ids": sorted(set(task_by_id) - set(predictions)),
        "missing_human_task_ids": sorted(set(task_by_id) - set(by_task)),
        "mean_task_js_divergence_nats": statistics.fmean(row["js_divergence_nats"] for row in per_task) if per_task else None,
        "response_weighted_brier": statistics.fmean(brier_values) if brier_values else None,
        "response_weighted_nll_nats": None if zero_probability_count or not nll_values else statistics.fmean(nll_values),
        "zero_predicted_probability_responses": zero_probability_count,
        "active_response_intensity_mae": statistics.fmean(active_errors) if active_errors else None,
        "per_task": per_task, "paired_contrasts": contrasts,
        "limitations": ["self-declared source_kind requires researcher provenance",
                        "no universal acceptance threshold; preregister after a pilot",
                        "responses within a participant are not independent people",
                        "descriptive metrics do not establish human validity"],
    }


__all__ = ["TASK_SCHEMA", "SCORE_SCHEMA", "INSTRUCTIONS", "build_tasks", "score_responses"]
