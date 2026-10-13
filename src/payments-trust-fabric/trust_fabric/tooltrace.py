"""Expose every Microsoft-Decision-1 call and every confidence-policy step as a tool call in the agent's output.

The workflow calls Microsoft-Decision-1 and the policy from code, so without this the Responses output only carried a narrative
report. Emitting each call as an executed (informational-only) function call with its result lets auditors, the Foundry
playground and evaluators such as task adherence see the evidence behind every decision.
"""

import uuid
from typing import Any

from agent_framework import Content, WorkflowContext

from . import policy
from .case import CaseState
from .decision_engine import DecisionRecord

DECISION_TOOL = "microsoft_decision_1"
POLICY_TOOL = "apply_trust_policy"


def _call_id() -> str:
    return f"call_{uuid.uuid4().hex[:24]}"


def _answer(raw: dict[str, Any]) -> dict[str, Any]:
    kind = raw.get("type")
    if kind == "noul":
        return {"probability_true": raw.get("p")}
    if kind == "choice":
        return {"choice": raw.get("choice"), "confidence": raw.get("confidence")}
    if kind == "score":
        return {"level": raw.get("label"), "score": raw.get("score"), "confidence": raw.get("confidence")}
    return {"refused": True}


def _short(value: Any, limit: int = 200) -> Any:
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "…"
    if isinstance(value, list):
        return [_short(v, limit) for v in value[:6]] + (["…"] if len(value) > 6 else [])
    if isinstance(value, dict):
        return {k: _short(v, limit) for k, v in value.items() if not str(k).startswith("_")}
    return value


def decision_contents(rec: DecisionRecord, extra: dict[str, Any] | None = None) -> list[Content]:
    call_id = _call_id()
    arguments = {"agent": rec.agent, "stage": rec.stage,
                 "questions": {name: {"type": q.get("type"), "question": _short(q.get("instructions", ""), 140)}
                               for name, q in rec.questions.items()}}
    result = {"answers": {name: _answer(a) for name, a in rec.answers.items()}, "latency_ms": round(rec.latency_ms),
              "input_tokens": rec.input_tokens, **(extra or {})}
    return [Content.from_function_call(call_id, DECISION_TOOL, arguments=arguments, informational_only=True),
            Content.from_function_result(call_id, result=result)]


def policy_contents(case: CaseState, stage: str, based_on: list[str]) -> list[Content]:
    call_id = _call_id()
    arguments = {"lane": case.lane, "stage": stage, "based_on_decisions_from": based_on}
    result = {"disposition": case.disposition, "automatic": case.disposition in policy.AUTOMATIC,
              "next_step_owner": case.human_queue or "automatic, in the customer's favour",
              "reasons": _short(case.reasons[-8:], 300), "findings": _short(case.findings)}
    return [Content.from_function_call(call_id, POLICY_TOOL, arguments=arguments, informational_only=True),
            Content.from_function_result(call_id, result=result)]


def outcome_key(case: CaseState) -> tuple[str | None, str | None]:
    return case.disposition, case.human_queue


async def emit(ctx: WorkflowContext[Any, Content], case: CaseState, stage: str, since: int | None = None,
               before: tuple[str | None, str | None] | None = None) -> None:
    """Yield tool items for the Microsoft-Decision-1 calls made since ``since`` and, if the outcome changed, the policy step."""
    new = case.ledger.decisions[since:] if since is not None else []
    for rec in new:
        for content in decision_contents(rec):
            await ctx.yield_output(content)
    if before is not None and case.disposition and outcome_key(case) != before:
        for content in policy_contents(case, stage, [rec.agent for rec in new]):
            await ctx.yield_output(content)
