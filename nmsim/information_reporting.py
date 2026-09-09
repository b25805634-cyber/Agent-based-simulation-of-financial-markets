"""Pure, allow-listed reports for the information-market pipeline."""
from __future__ import annotations

import html
import json
from typing import Any


def public_report(summary: dict[str, Any]) -> dict[str, Any]:
    allowed = ("schema_version", "run_id", "task", "status", "honest_n",
               "identities", "source", "student_comparison", "selected_model",
               "selection_rule", "markets", "limitations", "elapsed_seconds",
               "human_validation", "artifacts", "next_evidence_needed", "sizing_policy", "sizing_candidate")
    return {key: summary[key] for key in allowed if key in summary}


def render_markdown(summary: dict[str, Any]) -> str:
    data = public_report(summary)
    lines = ["# Agent market — run report", "", f"Run: `{data['run_id']}`",
             f"Task: `{data['task']}`", "",
             "Human resemblance: **not established**. This report distinguishes "
             "historical Teacher data from new offline model fits and simulated markets.", ""]
    if "student_comparison" in data:
        lines.extend(["| Model | Validation CE | Test CE | Test Brier |", "|---|---:|---:|---:|"])
        for name, metrics in data["student_comparison"].items():
            lines.append(f"| {name} | {metrics['validation']['action_cross_entropy']:.6f} | "
                         f"{metrics['test']['action_cross_entropy']:.6f} | "
                         f"{metrics['test']['action_brier']:.6f} |")
        lines.extend(["", "Selected with validation data only: " + str(data.get("selected_model")), ""])
    for key in ("honest_n", "markets", "limitations", "next_evidence_needed", "identities"):
        if key in data:
            lines.extend(["## " + key.replace("_", " "), "", "```json",
                          json.dumps(data[key], ensure_ascii=False, indent=2, allow_nan=False), "```", ""])
    return "\n".join(lines)


def render_html(summary: dict[str, Any]) -> str:
    data = public_report(summary)
    sections = []
    labels = {"honest_n": "实际完成数量", "student_comparison": "Student 对照（损失越低越好）",
              "markets": "市场结果", "limitations": "结论边界", "identities": "运行身份与输入依据",
              "next_evidence_needed": "还缺哪些证据"}
    for key, label in labels.items():
        if key in data:
            encoded = html.escape(json.dumps(data[key], ensure_ascii=False, indent=2, allow_nan=False))
            sections.append(f"<section><h2>{label}</h2><pre>{encoded}</pre></section>")
    links = " ".join(f'<a href="{html.escape(name, quote=True)}">{html.escape(name)}</a>'
                     for name in data.get("artifacts", [])
                     if isinstance(name, str) and "/" not in name and not name.startswith("private"))
    return ("<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>Agent 市场实验报告</title><style>"
            "body{max-width:1050px;margin:40px auto;padding:0 24px;background:#f5f6f8;color:#182334;"
            "font:16px/1.65 system-ui,sans-serif}h1{font-size:30px}h2{font-size:20px}"
            "section{background:white;border:1px solid #dce1e7;border-radius:10px;padding:18px 24px;margin:20px 0}"
            "pre{white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.6 ui-monospace,monospace}"
            "a{color:#1261a0;margin-right:14px}.note{border-left:4px solid #bc7b18;padding:12px;background:#fff5e5}"
            "</style><h1>Agent 市场实验报告</h1><p>"
            + html.escape(data["run_id"]) + " · " + html.escape(data["task"])
            + "</p><p class=\"note\">这是已有 Teacher 数据的离线训练或合成市场实验。"
            "真人相似性尚未验证；模拟交易者数量不等于真人样本数量。</p><nav>" + links
            + "</nav>" + "".join(sections) + "</html>\n")


__all__ = ["public_report", "render_markdown", "render_html"]
