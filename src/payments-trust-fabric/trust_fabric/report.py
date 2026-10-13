"""Renders the final case report: human-readable markdown plus a machine-readable JSON record."""

import json
import time
from typing import Any

from .case import CaseState, help_text
from .policy import AUTOMATIC

_ICON = {
    "AUTO_APPROVE": "✅", "AUTO_CREDIT": "✅", "AUTO_REQUEST_INFO": "✅", "APPROVE_WITH_MONITORING": "✅", "ALLOW": "✅",
    "AUTO_REPLY": "✅", "AUTO_WARNING": "⚠️", "CONSUMER_STEP_UP": "📲", "RISK_REVIEW": "👤", "ANALYST_REVIEW": "👤",
    "UNDERWRITER_REVIEW": "👤", "HUMAN_HOLD": "👤", "ACCOUNT_SECURITY_REVIEW": "👤", "HUMAN_TRIAGE": "👤",
    "COMPLIANCE_REFERRAL": "🛑", "SANCTIONS_HOLD": "🛑", "QUARANTINED": "🛑",
}
_KIND = {"decision": "⚡ Microsoft-Decision-1", "llm": "✍️ LLM", "policy": "⚖️ Policy", "info": "📥 Intake"}


def metrics(case: CaseState) -> dict[str, Any]:
    ledger = case.ledger
    return {
        "decision_calls": len(ledger.decisions),
        "decisions": ledger.decision_count,
        "decision_ms_critical_path": round(ledger.decision_wall_ms(), 1),
        "decision_ms_sum": round(sum(r.latency_ms for r in ledger.decisions), 1),
        "decision_input_tokens": ledger.decision_tokens,
        "decision_cost_usd": round(ledger.decision_cost_usd, 8),
        "llm_calls": len(ledger.llm_calls),
        "llm_ms": round(sum(c["latency_ms"] for c in ledger.llm_calls), 1),
        "llm_input_tokens": sum(c["input_tokens"] for c in ledger.llm_calls),
        "llm_output_tokens": sum(c["output_tokens"] for c in ledger.llm_calls),
        "total_ms": round((time.perf_counter() - case.started) * 1000, 1),
    }


def _draft_md(draft: dict[str, Any]) -> list[str]:
    out = []
    for key, value in draft.items():
        if value in ("", None, []):
            continue
        label = key.replace("_", " ").capitalize()
        if isinstance(value, list):
            out.append(f"**{label}:**")
            out += [f"- {v}" for v in value] or ["- (none)"]
        else:
            out.append(f"**{label}:** {value}")
    return out


def render_report(case: CaseState) -> str:
    if case.mode == "help":
        return help_text()
    m = metrics(case)
    disposition = case.disposition or "HUMAN_TRIAGE"
    human = "No — automatic" if disposition in AUTOMATIC else (case.human_queue or "Yes")
    lines = [
        f"## {_ICON.get(disposition, '•')} {disposition.replace('_', ' ')} · {case.case_id}",
        f"**{case.title}**",
        "",
        "| Lane | Human in the loop | Microsoft-Decision-1 | Microsoft-Decision-1 latency (critical path) | Microsoft-Decision-1 cost | LLM calls | End-to-end |",
        "|---|---|---|---|---|---|---|",
        f"| {case.lane or '—'} | {human} | {m['decisions']} decisions in {m['decision_calls']} calls | {m['decision_ms_critical_path']:.0f} ms | "
        f"${m['decision_cost_usd']:.6f} | {m['llm_calls']} ({m['llm_ms']:.0f} ms) | {m['total_ms']:.0f} ms |",
        "",
        "### Why",
    ]
    lines += [f"- {r}" for r in case.reasons] or ["- No issues found."]
    lines += ["", "### Agent trail", "| # | Agent | Type | Result | ms |", "|---|---|---|---|---|"]
    for n, step in enumerate(case.timeline, 1):
        ms = "" if step["ms"] is None else f"{step['ms']:.0f}"
        lines.append(f"| {n} | {step['agent']} | {_KIND.get(step['kind'], step['kind'])} | {step['summary']} | {ms} |")
    if case.draft:
        verdict = "approved by the Output Judge" if not case.judge_feedback else "needs human edit"
        lines += ["", f"### Draft ({verdict}, {case.revisions} revision{'s' if case.revisions != 1 else ''})"] + _draft_md(case.draft)
    elif disposition == "SANCTIONS_HOLD":
        lines += ["", "_No customer message is drafted for sanctions holds, to avoid tipping off._"]
    if case.gold and case.gold.get("disposition"):
        acceptable = case.gold.get("acceptable_dispositions") or [case.gold["disposition"]]
        ok = disposition in acceptable
        lines += ["", f"**Demo label check:** expected {' or '.join(acceptable)} → {'✅ match' if ok else '❌ differs'}"]

    record = {
        "case_id": case.case_id, "title": case.title, "lane": case.lane, "lane_confidence": round(case.lane_confidence, 4),
        "urgency": case.urgency, "disposition": disposition, "automatic": disposition in AUTOMATIC, "human_queue": case.human_queue,
        "reasons": case.reasons, "shield": case.shield_flags, "findings": case.findings, "timeline": case.timeline,
        "decisions": [r.to_dict() for r in case.ledger.decisions], "llm_calls": case.ledger.llm_calls, "draft": case.draft,
        "draft_history": case.draft_history, "revisions": case.revisions, "metrics": m, "gold": case.gold,
    }
    lines += ["", "<details><summary>Machine-readable case record</summary>", "", "```json",
              json.dumps(record, ensure_ascii=False, separators=(",", ":")), "```", "</details>"]
    return "\n".join(lines)
