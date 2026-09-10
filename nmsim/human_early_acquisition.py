"""Pure frozen K=1 schedule for the reconstructed early human task.

No filesystem, Provider, network or managed-run operation occurs here.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import hashlib
import re
from typing import Any, Mapping, Sequence

from .human_early_tasks import (
    TASK_SCHEMA, PROTOCOL_ID as TASK_PROTOCOL_ID, _instructions,
    evaluate_joint_predictions, hold_baseline_predictions, _settlement,
)
from .human_reference import ASSETS
from .information_artifacts import canonical_hash


PLAN_SCHEMA = "human-early-teacher-plan/0.1"
ACQUISITION_PROTOCOL_ID = "human-early-teacher-k1-shuffled-contexts/0.1"
REQUEST_COUNT = 195
ORDER_SEED = 20260909
SOURCE_MANIFEST_SHA256 = "5f7ffcbede579c520756f92570b6e79de3d73fc97ff146378829f692be920286"
REAL_REQUEST = {"model": "MiniMax-M2.7", "temperature": 1.0, "top_p": 0.95,
                "top_k": 40, "max_tokens": 190000,
                "response_format": {"type": "json_object"}}
SYSTEM_PROMPT = "Complete the supplied laboratory decision task. Return only its requested JSON object."
COMPONENT_PATHS = (
    "nmsim/human_reference.py", "nmsim/human_early_tasks.py", "nmsim/human_early_acquisition.py",
    "nmsim/information_artifacts.py", "experiments/human_early_teacher.py",
    "experiments/information_market.py", "experiments/information_weight_teacher.py",
    "experiments/rollout_fidelity.py", "experiments/v2_attention_market.py",
    "docs/HUMAN_EARLY_TEACHER.md",
)


def _sha(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _bank_units(tasks: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    if not isinstance(tasks, list) or len(tasks) != REQUEST_COUNT:
        raise ValueError("frozen human early bank requires exactly 195 tasks")
    people = defaultdict(list)
    groups = defaultdict(set)
    seen = set()
    for task in tasks:
        if not isinstance(task, Mapping) or task.get("schema") != TASK_SCHEMA:
            raise ValueError("invalid human task schema")
        task_id = task.get("task_id")
        if not isinstance(task_id, str) or not task_id or task_id in seen:
            raise ValueError("missing or duplicate task identity")
        seen.add(task_id)
        origin, state, protocol = task.get("source_origin"), task.get("state"), task.get("protocol")
        if not all(isinstance(value, Mapping) for value in (origin, state, protocol)):
            raise ValueError("task metadata missing")
        if (protocol.get("id") != TASK_PROTOCOL_ID or origin.get("source_condition") != "spt2"
                or protocol.get("original_ui_fully_recovered") is not False
                or protocol.get("human_likeness_validated") is not False
                or protocol.get("realized_peer_rankings_in_window") != 0):
            raise ValueError("unexpected source task contract")
        raw_round = origin.get("source_raw_round")
        if type(raw_round) is not int or raw_round not in range(4, 9):
            raise ValueError("task outside frozen early window")
        if state.get("main_round") != raw_round-3 or state.get("current_published_price_index") != raw_round-4:
            raise ValueError("human task clock mismatch")
        if task.get("prompt") != _instructions(state):
            raise ValueError("source prompt differs from the frozen task renderer")
        person = (origin.get("source_session_id"), origin.get("source_participant_id"))
        group = (origin.get("source_session_id"), origin.get("source_peer_group_id"))
        if any(not isinstance(key, str) or not key for key in (*person, *group)):
            raise ValueError("source origin missing")
        people[person].append(raw_round)
        groups[group].add(person)
    if len(people) != 39 or len(groups) != 13 or any(len(v) != 3 for v in groups.values()):
        raise ValueError("frozen source requires 39 people in 13 three-person groups")
    if any(sorted(v) != list(range(4, 9)) for v in people.values()):
        raise ValueError("source participant window incomplete")
    # This checks the gold joint accounting without treating a hold control as
    # a Teacher acquisition. The comparison itself is not retained in the plan.
    evaluate_joint_predictions(tasks, hold_baseline_predictions(tasks))
    return {"historical_human_participants": 39, "historical_peer_groups": 13,
            "historical_joint_decisions": REQUEST_COUNT,
            "historical_stock_opportunities": 6 * REQUEST_COUNT,
            "distinct_prompt_utf8_hashes": len({hashlib.sha256(t["prompt"].encode()).hexdigest() for t in tasks}),
            "distinct_state_semantic_hashes": len({canonical_hash(t["state"]) for t in tasks})}


def build_plan(tasks: list[dict[str, Any]], *, source_manifest_sha256: str,
               source_run_id: str, source_task_bank_hash: str,
               component_hashes: Mapping[str, str]) -> dict[str, Any]:
    """Freeze all 195 tasks once, with a fixed non-contiguous request ordering."""
    bank = deepcopy(tasks)
    units = _bank_units(bank)
    if not _sha(source_manifest_sha256) or not isinstance(source_run_id, str) or not source_run_id:
        raise ValueError("missing source identity")
    if source_task_bank_hash != canonical_hash(bank):
        raise ValueError("source task bank semantic hash mismatch")
    if set(component_hashes) != set(COMPONENT_PATHS) or not all(_sha(v) for v in component_hashes.values()):
        raise ValueError("component fingerprint set is incomplete")
    ordered = sorted(bank, key=lambda task: (
        canonical_hash([ACQUISITION_PROTOCOL_ID, ORDER_SEED, task["task_id"]]), task["task_id"]))
    samples = [{"request_index": i, "task_id": task["task_id"], "replicate": 1,
                "prompt_utf8_sha256": hashlib.sha256(task["prompt"].encode()).hexdigest(),
                "state_semantic_hash": canonical_hash(task["state"])}
               for i, task in enumerate(ordered)]
    plan = {"schema": PLAN_SCHEMA, "acquisition_protocol_id": ACQUISITION_PROTOCOL_ID,
            "task_protocol_id": TASK_PROTOCOL_ID,
            "source_manifest_sha256": source_manifest_sha256, "source_run_id": source_run_id,
            "source_task_bank_hash": source_task_bank_hash,
            "component_hashes": dict(component_hashes),
            "planned_logical_requests": REQUEST_COUNT, "replicates_per_task": 1,
            "units": units,
            "order_rule": "ascending SHA256(canonical JSON([acquisition_protocol_id, 20260909, task_id])); task_id tie-break",
            "order_seed": ORDER_SEED, "sample_order_hash": canonical_hash(samples),
            "samples": samples, "system_prompt": SYSTEM_PROMPT,
            "real_request": deepcopy(REAL_REQUEST),
            "required_reported_model": "HiggsAI", "required_finish_reason": "stop",
            "execution_policy": {"cache": False, "shared_conversations": False,
                                 "application_max_attempts": 1, "sdk_retries": 0,
                                 "request_timeout_seconds": 7200, "hard_request_deadline_seconds": 7200,
                                 "stop_rule": "stop after a batch with only provider failures; leave remainder unattempted",
                                 "resume_or_replacement_samples": False},
            "scientific_scope": {"human_likeness_validated": False,
                                 "full_original_ui_verified": False,
                                 "population_distribution_estimated": False,
                                 "single_state_teacher_distribution_estimated": False,
                                 "student_training_or_model_selection": False,
                                 "endogenous_market_validation": False},
            "tasks": bank}
    plan["plan_hash"] = canonical_hash(plan)
    return plan


def validate_plan(plan: Any) -> None:
    if not isinstance(plan, Mapping) or plan.get("schema") != PLAN_SCHEMA:
        raise ValueError("invalid frozen human Teacher plan")
    try:
        expected = build_plan(plan["tasks"], source_manifest_sha256=plan["source_manifest_sha256"],
                              source_run_id=plan["source_run_id"],
                              source_task_bank_hash=plan["source_task_bank_hash"],
                              component_hashes=plan["component_hashes"])
    except (KeyError, TypeError) as exc:
        raise ValueError("incomplete frozen human Teacher plan") from exc
    if dict(plan) != expected:
        raise ValueError("frozen human Teacher plan changed")


def public_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Only hashes, schedule and aggregate contracts; no prompt or trajectory."""
    return {key: deepcopy(value) for key, value in plan.items() if key not in {"tasks", "system_prompt"}}


def action_classification(tasks: Sequence[Mapping[str, Any]],
                          accepted_predictions: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Descriptive 3x3 confusion on accepted joint decisions only, no CI/pass."""
    by_id = {task["task_id"]: task for task in tasks}
    if len(by_id) != len(tasks):
        raise ValueError("duplicate classification task")
    labels = ("buy", "hold", "sell")
    counts = {human: {model: 0 for model in labels} for human in labels}
    seen = set()
    direction = lambda n: "buy" if n > 0 else "sell" if n < 0 else "hold"
    for prediction in accepted_predictions:
        if not isinstance(prediction, Mapping) or set(prediction) != {"task_id", "signed_quantities"}:
            raise ValueError("accepted prediction shape invalid")
        task_id = prediction["task_id"]
        if not isinstance(task_id, str) or task_id not in by_id or task_id in seen:
            raise ValueError("classification prediction identity invalid")
        seen.add(task_id)
        task = by_id[task_id]
        model = _settlement(prediction["signed_quantities"], task)["signed_quantities"]
        human = _settlement(task["human_action"]["signed_quantities"], task)["signed_quantities"]
        for asset in ASSETS:
            counts[direction(human[asset])][direction(model[asset])] += 1
    support = {label: sum(counts[label].values()) for label in labels}
    recall = {label: counts[label][label] / support[label] if support[label] else None for label in labels}
    observed = [value for value in recall.values() if value is not None]
    return {"schema": "human-early-paired-valid-action-confusion/0.1",
            "orientation": "rows=human action; columns=predicted action",
            "joint_decisions": len(seen), "stock_opportunities": len(seen) * 6,
            "counts": counts, "human_class_support": support, "class_recall": recall,
            "macro_recall_over_observed_human_classes": sum(observed)/len(observed) if observed else None,
            "human_classes_in_macro_recall": [label for label in labels if support[label]],
            "unobserved_class_recall": None, "interpretation": "descriptive only; no pass threshold or independent-stock CI"}


__all__ = ["PLAN_SCHEMA", "ACQUISITION_PROTOCOL_ID", "REQUEST_COUNT", "ORDER_SEED",
           "SOURCE_MANIFEST_SHA256", "REAL_REQUEST", "COMPONENT_PATHS",
           "build_plan", "validate_plan", "public_plan", "action_classification"]
