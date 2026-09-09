"""Pure reconstructed six-asset tasks before the first human peer ranking.

This module does not authenticate source bytes, acquire model responses, write
artifacts or create research runs. A managed caller owns those boundaries.
The original display is not fully recovered; this task explicitly preserves
the price-clock and net-budget assumptions instead of asserting UI identity.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
from typing import Any, Mapping, Sequence

from .human_reference import ASSETS, reconstruct_decisions


TASK_SCHEMA = "lieu-pelster-spt2-early-task/0.1"
PARSED_SCHEMA = "lieu-pelster-signed-joint-response/0.1"
EVALUATION_SCHEMA = "lieu-pelster-early-descriptive-comparison/0.1"
PROTOCOL_ID = "spt2-main-1-through-5-published-clock-net-budget/0.1"


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def _instructions(state: Mapping[str, Any]) -> str:
    return (
        "You are making one decision in a six-stock laboratory investment task. "
        "Money is measured in Talers; shares must be whole numbers. The paid task "
        "has 14 rounds and began with 10,000 Talers and no shares. Three practice "
        "rounds preceded it; cash and holdings reset before paid round 1. Practice "
        "profits and losses do not affect payment.\n"
        "Stock prices are predetermined independently of all participants' trades. "
        "There is one stock with a 65% chance of rising, one with 55%, two with 50%, "
        "one with 45% and one with 35%. You are not told which stock has which type. "
        "After the direction is drawn, the price change is 1, 3 or 5 Talers, each "
        "with probability one third, independently of direction. There are no "
        "transaction fees. You may keep cash and leave any stock position unchanged.\n"
        "For this joint decision, buy and sell at the stated current prices. You "
        "cannot sell more shares of a stock than you already own. You cannot "
        "borrow. The total purchase cost may not exceed current cash plus the "
        "proceeds of stocks sold in this same submission.\n"
        "After paid rounds 5, 10 and 14 you will be ranked with the same two peers "
        "by cash plus the value of all stocks. Rankings do not affect payment. "
        "No such ranking has yet been displayed at this decision. After paid "
        "rounds 7 and 14 you will guess the hidden stock types; each correct guess "
        "earns 200 Talers at the end. At the end of the paid task, remaining shares "
        "are liquidated. Cash and liquidation proceeds are converted at 1,000 "
        "Talers per Euro. The original experiment also pays a 2.50 Euro show-up fee "
        "and offers separately paid tasks after trading.\n"
        "Below are the observed prices and your own earlier trades. Published "
        "price index 0 is the first paid decision's price; negative indices refer "
        "to practice exposure. Decide using only information available now.\n"
        + _canonical(state)
        + '\nReturn exactly one JSON object with keys "signed_quantities" and '
        '"private_reasoning". "signed_quantities" must contain exactly a, b, c, d, '
        'e and f with integer values: positive means buy, negative means sell, '
        'zero means no trade. "private_reasoning" must be a string. This text '
        "is private and will not be shown to peers. Do not include Markdown."
    )


def build_early_tasks(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Build spt2 main rounds 1–5; source IDs and current labels stay out of prompt.

    Complete source histories are validated by the existing source adapter.
    Engineering fixtures may exercise this function; only a caller's verified
    CSV byte identity can turn its counts into claims about the source study.
    """
    tasks = []
    for decision in reconstruct_decisions(records):
        if (decision["source_condition"] != "spt2" or decision["phase"] != "main"
                or decision["main_round"] > 5):
            continue
        original = decision["state"]
        state = {
            "main_round": decision["main_round"],
            "total_main_rounds": 14,
            "current_published_price_index": decision["source_raw_round"] - 4,
            "cash_before_talers": original["cash_before_talers"],
            "holdings_before": dict(original["holdings_before"]),
            "current_prices_talers": dict(original["current_prices_talers"]),
            "portfolio_value_before_talers": original["portfolio_value_before_talers"],
            "observed_price_history": [
                {"published_price_index": h["raw_round"] - 4,
                 "prices_talers": dict(h["prices_talers"])}
                for h in original["recorded_price_history"]
            ],
            "own_prior_trades": [
                {"phase": h["phase"], "published_price_index": h["raw_round"] - 4,
                 "prices_talers": dict(h["prices_talers"]),
                 "signed_quantities": {
                     a: h["joint_action"][a]["buy_quantity"] - h["joint_action"][a]["sell_quantity"]
                     for a in ASSETS}}
                for h in original["recorded_own_trade_history"]
            ],
        }
        origin = {k: decision[k] for k in (
            "source_session_id", "source_participant_id", "source_peer_group_id",
            "source_condition", "source_raw_round",
        )}
        task_id = "lp-early-" + hashlib.sha256(
            _canonical({"protocol": PROTOCOL_ID, "origin": origin}).encode("utf-8")
        ).hexdigest()[:24]
        tasks.append({
            "schema": TASK_SCHEMA, "task_id": task_id, "source_origin": origin,
            "protocol": {
                "id": PROTOCOL_ID,
                "scope": "conditional joint choice on recorded human history",
                "price_clock": "published figure index = source raw round - 4",
                "budget_interpretation": "same-submission sales may fund purchases",
                "original_ui_fully_recovered": False,
                "main_chart_retention_of_practice_prices_verified": False,
                "own_prior_actions_supplied_as_reconstructed_memory": True,
                "production_joint_budget_validator_verified": False,
                "realized_peer_rankings_in_window": 0,
                "human_likeness_validated": False,
            },
            "state": state,
            "prompt": _instructions(state),
            "human_action": {"signed_quantities": {
                a: decision["joint_action"][a]["buy_quantity"] - decision["joint_action"][a]["sell_quantity"]
                for a in ASSETS}},
        })
    return tasks


def _integers(values: Any, name: str, *, positive: bool = False,
              nonnegative: bool = False) -> dict[str, int]:
    if not isinstance(values, Mapping) or set(values) != set(ASSETS):
        raise ValueError(name + " must contain exactly six asset keys")
    for value in values.values():
        if type(value) is not int or (positive and value <= 0) or (nonnegative and value < 0):
            raise ValueError(name + " has an invalid integer")
    return dict(values)


def _settlement(signed: Any, task: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(task, Mapping) or task.get("schema") != TASK_SCHEMA:
        raise ValueError("unexpected task schema")
    state = task.get("state")
    if not isinstance(state, Mapping):
        raise ValueError("missing task state")
    quantities = _integers(signed, "signed_quantities")
    prices = _integers(state.get("current_prices_talers"), "prices", positive=True)
    holdings = _integers(state.get("holdings_before"), "holdings", nonnegative=True)
    cash = state.get("cash_before_talers")
    if type(cash) is not int or cash < 0:
        raise ValueError("invalid task cash")
    if any(holdings[a] + quantities[a] < 0 for a in ASSETS):
        raise ValueError("sales exceed shares held")
    cost = sum(max(quantities[a], 0) * prices[a] for a in ASSETS)
    proceeds = sum(max(-quantities[a], 0) * prices[a] for a in ASSETS)
    cash_after = cash + proceeds - cost
    if cash_after < 0:
        raise ValueError("joint submission exceeds net cash budget")
    return {"signed_quantities": quantities,
            "cash_after_talers": cash_after,
            "holdings_after": {a: holdings[a] + quantities[a] for a in ASSETS},
            "gross_buy_cost_talers": cost, "gross_sell_proceeds_talers": proceeds}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_nonfinite(_):
    raise ValueError("nonfinite JSON")


def parse_joint_response(raw: str, task: Mapping[str, Any]) -> dict[str, Any]:
    """Strict JSON and integer feasibility; private text is structurally separate."""
    if not isinstance(raw, str):
        raise ValueError("raw response must be JSON text")
    try:
        response = json.loads(raw, object_pairs_hook=_unique_object,
                              parse_constant=_reject_nonfinite)
    except (ValueError, RecursionError) as exc:
        raise ValueError("response is not strict JSON") from exc
    if not isinstance(response, dict) or set(response) != {"signed_quantities", "private_reasoning"}:
        raise ValueError("response has unexpected or missing fields")
    if not isinstance(response["private_reasoning"], str):
        raise ValueError("private_reasoning must be a string")
    return {"schema": PARSED_SCHEMA,
            "public_decision": _settlement(response["signed_quantities"], task),
            "private_rationale": response["private_reasoning"]}


def hold_baseline_predictions(tasks: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """Explicit no-trade null; these are synthetic predictions, not Teacher calls."""
    raw = _canonical({"signed_quantities": dict.fromkeys(ASSETS, 0), "private_reasoning": ""})
    return [{"task_id": task["task_id"], "raw_response": raw} for task in tasks]


def evaluate_joint_predictions(tasks: Sequence[Mapping[str, Any]],
                               predictions: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Descriptive paired agreement; no universal pass threshold or human claim.

    There is at most one submitted prediction per task. Missing and invalid
    predictions remain in coverage counts and never become chosen holds.
    """
    task_map = {}
    for task in tasks:
        task_id = task.get("task_id")
        if not isinstance(task_id, str) or not task_id or task_id in task_map:
            raise ValueError("missing or duplicate task_id")
        _settlement(task.get("human_action", {}).get("signed_quantities"), task)
        task_map[task_id] = task
    seen = set()
    valid = exact = signs = absolute_error = 0
    failures = []
    human_actions, model_actions = Counter(), Counter()
    direction = lambda v: "buy" if v > 0 else "sell" if v < 0 else "hold"
    for prediction in predictions:
        if not isinstance(prediction, Mapping) or set(prediction) != {"task_id", "raw_response"}:
            raise ValueError("prediction requires task_id and raw_response")
        task_id = prediction["task_id"]
        if not isinstance(task_id, str) or task_id not in task_map or task_id in seen:
            raise ValueError("unknown or repeated prediction task_id")
        seen.add(task_id)
        task = task_map[task_id]
        try:
            parsed = parse_joint_response(prediction["raw_response"], task)
        except ValueError as exc:
            failures.append({"task_id": task_id, "reason": str(exc)})
            continue
        model = parsed["public_decision"]["signed_quantities"]
        human = task["human_action"]["signed_quantities"]
        valid += 1
        exact += model == human
        for a in ASSETS:
            signs += direction(model[a]) == direction(human[a])
            absolute_error += abs(model[a] - human[a])
            human_actions[direction(human[a])] += 1
            model_actions[direction(model[a])] += 1
    subjects = {(t["source_origin"]["source_session_id"], t["source_origin"]["source_participant_id"])
                for t in tasks}
    groups = {(t["source_origin"]["source_session_id"], t["source_origin"]["source_peer_group_id"])
              for t in tasks}
    return {"schema": EVALUATION_SCHEMA, "task_protocol_id": PROTOCOL_ID,
            "units": {"source_subjects": len(subjects), "source_peer_groups": len(groups),
                      "joint_decision_tasks": len(tasks), "stock_opportunities": len(tasks) * 6,
                      "independent_human_sample_is_not_stock_opportunity": True},
            "predictions_received": len(seen), "valid_joint_predictions": valid,
            "invalid_joint_predictions": len(failures), "missing_predictions": len(tasks) - len(seen),
            "valid_coverage": valid / len(tasks) if tasks else None,
            "paired_valid_comparison": {
                "denominator_joint_decisions": valid,
                "exact_joint_decisions": exact,
                "exact_joint_fraction": exact / valid if valid else None,
                "stock_action_agreement": signs / (6 * valid) if valid else None,
                "mean_absolute_signed_share_error": absolute_error / (6 * valid) if valid else None,
                "human_stock_actions": {a: human_actions[a] for a in ("buy", "hold", "sell")},
                "model_stock_actions": {a: model_actions[a] for a in ("buy", "hold", "sell")}},
            "failures": failures, "human_likeness_validated": False,
            "interpretation": "descriptive agreement on reconstructed first-block human histories; no universal pass threshold"}


__all__ = ["TASK_SCHEMA", "PARSED_SCHEMA", "EVALUATION_SCHEMA", "PROTOCOL_ID",
           "build_early_tasks", "parse_joint_response", "hold_baseline_predictions",
           "evaluate_joint_predictions"]
