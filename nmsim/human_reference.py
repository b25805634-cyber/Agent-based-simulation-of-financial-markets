"""Pure audit of the public Liêu--Pelster six-asset human trading records.

This is a source-specific input adapter, not a mapping to the single-asset
information-emphasis contract.  CSV text is supplied by the caller.  No file,
network, Provider or managed-run operation occurs here.  Future-marked wealth
is used only in an outcome audit and never in reconstructed decision states.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import csv
import io
from typing import Any, Mapping, Sequence


SOURCE_DOI = "10.17632/jfg8s32xdm.1"
SOURCE_CSV_SHA256 = "327f7c512733fffe0efb8ee83944cefb4320ab538a930b920b5cdd25595ac333"
AUDIT_SCHEMA = "lieu-pelster-human-reference-audit/1.0.0"
DECISION_SCHEMA = "lieu-pelster-joint-decision/1.0.0"
ASSETS = tuple("abcdef")
SOURCE_COLUMNS = (
    "", "participant.code", "participant._current_app_name", "player.age",
    "player.gender", "player.finance", "player.business", "player.economics",
    "player.selfassessment", "player.recognize_participants",
    *(f"player.holt_laury{i}" for i in range(1, 11)), "player.lottery",
    *(f"player.coin_{i}" for i in range(1, 11)),
    *(f"player.quantity_pre_{a}" for a in ASSETS),
    "player.quantity_sell_a", "player.quantity_buy_a", "player.quantity_sell_b",
    "player.quantity_buy_b", "player.quantity_buy_c", "player.quantity_sell_c",
    "player.quantity_sell_d", "player.quantity_buy_d", "player.quantity_sell_e",
    "player.quantity_buy_e", "player.quantity_sell_f", "player.quantity_buy_f",
    *(f"player.quantity_{a}" for a in ASSETS),
    *(f"player.price_{a}" for a in ASSETS),
    "player.result_final", "player.total_count_guess", "group.id_in_subsession",
    "subsession.round_number", "session.code",
)
_NUMERIC_COLUMNS = (
    *(f"player.{prefix}{a}" for prefix in (
        "quantity_pre_", "quantity_sell_", "quantity_buy_", "quantity_", "price_",
    ) for a in ASSETS),
    "player.result_final", "subsession.round_number",
)
_RANKING_RULES = {
    "spt1": "percentage_of_profitable_trades_with_portfolio_value_displayed",
    "spt2": "current_portfolio_value_without_profitable_trade_percentage",
}


def decode_csv(text: str) -> list[dict[str, str]]:
    """Preserve raw cells; reject missing, duplicated or reordered columns."""
    if not isinstance(text, str):
        raise ValueError("CSV input must be text")
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if tuple(reader.fieldnames or ()) != SOURCE_COLUMNS:
        raise ValueError("CSV header does not match the frozen source column order")
    rows = list(reader)
    if not rows or any(None in row or any(value is None for value in row.values()) for row in rows):
        raise ValueError("empty or ragged CSV input")
    return rows


def _normalize(records: Sequence[Mapping[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    if not records:
        raise ValueError("source records are empty")
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    seen: set[tuple[str, str, int]] = set()
    for raw in records:
        if not isinstance(raw, Mapping) or set(raw) != set(SOURCE_COLUMNS):
            raise ValueError("source record has unexpected or missing columns")
        row = dict(raw)
        for key in _NUMERIC_COLUMNS:
            value = row[key]
            if not isinstance(value, str) or not value or value.strip() != value:
                raise ValueError(f"source integer cell required: {key}")
            try:
                parsed = int(value)
            except ValueError as exc:
                raise ValueError(f"invalid source integer: {key}") from exc
            if str(parsed) != value:
                raise ValueError(f"noncanonical source integer: {key}")
            row[key] = parsed
        for key in ("participant.code", "session.code", "group.id_in_subsession"):
            if not isinstance(row[key], str) or not row[key] or row[key].strip() != row[key]:
                raise ValueError(f"missing source identifier: {key}")
        if row["participant._current_app_name"] not in _RANKING_RULES:
            raise ValueError("unknown source treatment condition")
        identity = row["session.code"], row["participant.code"]
        key = (*identity, row["subsession.round_number"])
        if key in seen:
            raise ValueError("duplicate source participant/round")
        seen.add(key)
        grouped[identity].append(row)
    for sequence in grouped.values():
        sequence.sort(key=lambda row: row["subsession.round_number"])
        if [row["subsession.round_number"] for row in sequence] != list(range(1, 18)):
            raise ValueError("each participant requires all 17 source rounds, including practice")
        for key in ("participant._current_app_name", "group.id_in_subsession"):
            if len({row[key] for row in sequence}) != 1:
                raise ValueError(f"source participant changes {key}")
    return grouped


def _reconstruct(grouped):
    decisions = []
    checks: Counter[str] = Counter()
    violations: Counter[str] = Counter()
    for (session, participant), sequence in sorted(grouped.items()):
        cash = 10000
        previous_holdings = None
        recorded_price_history = []
        own_trade_history = []
        for row in sequence:
            raw_round = row["subsession.round_number"]
            reset = raw_round in (1, 4)
            if reset:
                cash = 10000
            pre = {a: row[f"player.quantity_pre_{a}"] for a in ASSETS}
            post = {a: row[f"player.quantity_{a}"] for a in ASSETS}
            prices = {a: row[f"player.price_{a}"] for a in ASSETS}
            buys = {a: row[f"player.quantity_buy_{a}"] for a in ASSETS}
            sells = {a: row[f"player.quantity_sell_{a}"] for a in ASSETS}
            for a in ASSETS:
                checks["stock_holding_balance"] += 1
                if post[a] != pre[a] + buys[a] - sells[a]:
                    violations["stock_holding_balance"] += 1
                if min(pre[a], post[a], buys[a], sells[a]) < 0:
                    violations["negative_stock_quantity"] += 1
                if prices[a] <= 0:
                    violations["nonpositive_execution_price"] += 1
                if sells[a] > pre[a]:
                    violations["sales_exceed_preholdings"] += 1
                if buys[a] and sells[a]:
                    violations["same_stock_simultaneous_buy_and_sell"] += 1
                if reset:
                    checks["reset_stock_holdings"] += 1
                    if pre[a] != 0:
                        violations["nonzero_reset_holdings"] += 1
                else:
                    checks["stock_holdings_continuity"] += 1
                    if pre[a] != previous_holdings[a]:
                        violations["stock_holdings_continuity"] += 1
            buy_cost = sum(buys[a] * prices[a] for a in ASSETS)
            sell_proceeds = sum(sells[a] * prices[a] for a in ASSETS)
            cash_after = cash + sell_proceeds - buy_cost
            checks["joint_net_cash_balance"] += 1
            if cash_after < 0:
                violations["negative_joint_net_cash"] += 1
            recorded_price_history.append({"raw_round": raw_round, "prices_talers": prices})
            actions = {
                a: {"action": "buy_and_sell" if buys[a] and sells[a] else
                    "buy" if buys[a] else "sell" if sells[a] else "hold",
                    "buy_quantity": buys[a], "sell_quantity": sells[a]}
                for a in ASSETS
            }
            phase = "practice" if raw_round <= 3 else "main"
            decision = {
                "schema": DECISION_SCHEMA,
                "source_participant_id": participant,
                "source_session_id": session,
                "source_peer_group_id": row["group.id_in_subsession"],
                "source_condition": row["participant._current_app_name"],
                "source_raw_round": raw_round,
                "phase": phase,
                "main_round": raw_round - 3 if phase == "main" else None,
                "state": {
                    "cash_before_talers": cash,
                    "holdings_before": pre,
                    "current_prices_talers": prices,
                    "portfolio_value_before_talers": cash + sum(pre[a] * prices[a] for a in ASSETS),
                    "recorded_price_history": list(recorded_price_history),
                    "recorded_own_trade_history": list(own_trade_history),
                    "known_ranking_rule": _RANKING_RULES[row["participant._current_app_name"]],
                },
                "joint_action": actions,
                "settlement": {
                    "cash_after_talers": cash_after,
                    "holdings_after": post,
                    "gross_buy_cost_talers": buy_cost,
                    "gross_sell_proceeds_talers": sell_proceeds,
                    "buy_cost_exceeds_start_cash": buy_cost > cash,
                },
                "observation_completeness": {
                    "original_full_price_history_verified": False,
                    "original_rankings_reconstructed": False,
                    "original_purchase_lot_convention_verified": False,
                    "identical_to_existing_single_asset_benchmark": False,
                },
            }
            decisions.append(decision)
            own_trade_history.append({"raw_round": raw_round, "phase": phase,
                                      "prices_talers": prices, "joint_action": actions})
            cash = cash_after
            previous_holdings = post
    return decisions, checks, violations


def reconstruct_decisions(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Reconstruct six-asset decisions with observed prices and pre-action cash.

    Source ``result_final`` is intentionally absent, including from history.
    The state is a documented partial observation reconstruction, not a claim
    to reproduce the original UI, peer rankings or a twenty-day price tape.
    """
    decisions, _, violations = _reconstruct(_normalize(records))
    if violations:
        raise ValueError("source accounting violation: " + ", ".join(sorted(violations)))
    return decisions


def audit_records(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Return aggregate source diagnostics with no participant identifiers."""
    grouped = _normalize(records)
    decisions, checks, violations = _reconstruct(grouped)
    main = [row for row in decisions if row["phase"] == "main"]
    actions = Counter()
    joint = Counter()
    by_condition = defaultdict(Counter)
    hold_position = Counter()
    for row in main:
        has_buy = any(x["buy_quantity"] for x in row["joint_action"].values())
        has_sell = any(x["sell_quantity"] for x in row["joint_action"].values())
        joint["buy_and_sell" if has_buy and has_sell else "buy_only" if has_buy else
              "sell_only" if has_sell else "no_trade"] += 1
        for a, action in row["joint_action"].items():
            actions[action["action"]] += 1
            by_condition[row["source_condition"]][action["action"]] += 1
            if action["action"] == "hold":
                hold_position["with_preholdings" if row["state"]["holdings_before"][a] else
                              "without_preholdings"] += 1
    decision_map = {(row["source_session_id"], row["source_participant_id"],
                     row["source_raw_round"]): row for row in decisions}
    value_errors = Counter()
    group_roster = defaultdict(set)
    for (session, participant), sequence in grouped.items():
        group_roster[(session, sequence[0]["group.id_in_subsession"])].add(participant)
        for row, next_row in zip(sequence, sequence[1:]):
            decision = decision_map[(session, participant, row["subsession.round_number"])]
            predicted = decision["settlement"]["cash_after_talers"] + sum(
                row[f"player.quantity_{a}"] * next_row[f"player.price_{a}"] for a in ASSETS)
            value_errors[row["player.result_final"] - predicted] += 1
    mismatch = sum(n for error, n in value_errors.items() if error != 0)
    trading_missing = sum(raw[key] in ("", "NA") for raw in records for key in _NUMERIC_COLUMNS)
    missingness = {key: dict(Counter(raw[key] for raw in records if raw[key] in ("", "NA")))
                   for key in SOURCE_COLUMNS if any(raw[key] in ("", "NA") for raw in records)}
    subjects = len(grouped)
    return {
        "schema": AUDIT_SCHEMA, "source_doi": SOURCE_DOI,
        "status": "input_audit_passed" if not violations and not mismatch else "input_audit_failed",
        "human_subjects": subjects,
        "sessions": len({session for session, _ in grouped}),
        "peer_groups": len(group_roster),
        "peer_group_size_counts": {str(size): count for size, count in
                                    sorted(Counter(map(len, group_roster.values())).items())},
        "subject_condition_counts": dict(sorted(Counter(sequence[0]["participant._current_app_name"]
                                                        for sequence in grouped.values()).items())),
        "source_row_count_including_practice": len(records),
        "source_column_count_including_export_index": len(SOURCE_COLUMNS),
        "practice_subject_periods": len(decisions) - len(main),
        "main_subject_periods": len(main),
        "main_stock_opportunities": len(main) * len(ASSETS),
        "practice_raw_rounds": [1, 2, 3], "main_raw_rounds": list(range(4, 18)),
        "main_actions_per_stock": dict(sorted(actions.items())),
        "main_actions_by_condition": {key: dict(sorted(value.items())) for key, value in sorted(by_condition.items())},
        "main_subject_period_joint_actions": dict(sorted(joint.items())),
        "main_hold_stock_opportunities": dict(sorted(hold_position.items())),
        "trading_integer_missing_values": trading_missing,
        "missing_tokens_by_column": missingness,
        "minimum_reconstructed_main_cash_talers": min(row["settlement"]["cash_after_talers"] for row in main),
        "main_gross_buy_exceeds_start_cash_count": sum(row["settlement"]["buy_cost_exceeds_start_cash"] for row in main),
        "checks": dict(sorted(checks.items())), "accounting_violations": dict(sorted(violations.items())),
        "next_price_portfolio_value_audit": {
            "candidate_definition": "cash_after_t + sum(postholdings_t * source_price_t_plus_1)",
            "compared_subject_periods": sum(value_errors.values()),
            "exact_matches": value_errors[0], "mismatches": mismatch,
            "unverified_final_subject_periods": subjects,
            "max_abs_residual_talers": max(map(abs, value_errors), default=0),
            "result_final_used_in_predecision_state": False,
        },
        "scientific_scope": {
            "source_format": "published_human_trading_dataset",
            "publisher_checksum_verification_required": True,
            "source_authenticity_verified_by_pure_library": False,
            "source_same_task_model_predictions_available": False,
            "current_single_asset_benchmark_compatible": False,
            "original_ui_fully_reconstructed": False,
            "human_likeness_validated": False,
            "periods_are_calendar_days": False,
            "price_formation_is_exogenous": True,
            "units": {"cash_and_prices": "Talers", "quantities": "integer shares"},
        },
    }


__all__ = ["SOURCE_DOI", "SOURCE_CSV_SHA256", "SOURCE_COLUMNS", "AUDIT_SCHEMA",
           "DECISION_SCHEMA", "ASSETS", "decode_csv", "audit_records", "reconstruct_decisions"]
