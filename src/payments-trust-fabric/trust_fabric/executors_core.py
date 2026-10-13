"""Core workflow executors: intake, parallel shield + dispatcher, writer/judge loop, finalizer."""

import copy
import json
import re
from dataclasses import dataclass
from typing import Any

from agent_framework import Content, Executor, Message, WorkflowContext, handler
from typing_extensions import Never

from . import policy, questions, stages, tooltrace
from .case import CaseState, case_from_text, reference
from .config import Settings
from .decision_engine import DecisionClient, DecisionError, choice
from .llm import LLMPool
from .report import render_report

MAX_REVISIONS = 2


@dataclass
class Deps:
    settings: Settings
    decisions: DecisionClient
    llms: LLMPool


def reasoning(text: str) -> Content:
    # Consecutive reasoning contents are merged into one Responses reasoning item; keep trail lines on separate lines.
    return Content.from_text_reasoning(text=text.rstrip("\n") + "\n")


def _user_text(messages: list[Message]) -> str:
    for m in reversed(messages):
        if str(getattr(m, "role", "")).lower().endswith("user") and (m.text or "").strip():
            return m.text
    return messages[-1].text if messages else ""


class Intake(Executor):
    def __init__(self) -> None:
        super().__init__(id="intake")

    @handler
    async def handle(self, messages: list[Message], ctx: WorkflowContext[CaseState, Content]) -> None:
        case = case_from_text(_user_text(messages))
        if case.mode != "help":
            await ctx.yield_output(reasoning(case.log("intake", "Intake", "info", f"{case.case_id} — {case.title}")))
        await ctx.send_message(case)


class ScreeningHub(Executor):
    """Forks the case so the Shield and the Dispatcher run in parallel."""

    def __init__(self) -> None:
        super().__init__(id="screening_hub")

    @handler
    async def handle(self, case: CaseState, ctx: WorkflowContext[CaseState]) -> None:
        case.findings["_fork"] = {"timeline": len(case.timeline), "decisions": len(case.ledger.decisions)}
        await ctx.send_message(case)


class Shield(Executor):
    def __init__(self, deps: Deps) -> None:
        super().__init__(id="shield")
        self.deps = deps

    @handler
    async def handle(self, case: CaseState, ctx: WorkflowContext[CaseState, Content]) -> None:
        case = copy.deepcopy(case)
        since = len(case.ledger.decisions)
        lines = await stages.shield_stage(case, self.deps.decisions)
        await tooltrace.emit(ctx, case, "shield", since=since)
        for line in lines:
            await ctx.yield_output(reasoning(line))
        await ctx.send_message(case)


class Dispatcher(Executor):
    def __init__(self, deps: Deps) -> None:
        super().__init__(id="dispatcher")
        self.deps = deps

    @handler
    async def handle(self, case: CaseState, ctx: WorkflowContext[CaseState, Content]) -> None:
        case = copy.deepcopy(case)
        since = len(case.ledger.decisions)
        lines = await stages.dispatch_stage(case, self.deps.decisions)
        await tooltrace.emit(ctx, case, "dispatch", since=since)
        for line in lines:
            await ctx.yield_output(reasoning(line))
        await ctx.send_message(case)


class Gate(Executor):
    """Fan-in: merges the Shield and Dispatcher branches and decides where the case goes next."""

    def __init__(self) -> None:
        super().__init__(id="gate")

    @handler
    async def handle(self, cases: list[CaseState], ctx: WorkflowContext[CaseState, Content]) -> None:
        mark = cases[0].findings.get("_fork", {"timeline": 0, "decisions": 0})
        base = copy.deepcopy(cases[0])
        for other in cases[1:]:
            base.timeline += other.timeline[mark["timeline"]:]
            base.ledger.decisions += other.ledger.decisions[mark["decisions"]:]
            base.shield_flags = base.shield_flags or other.shield_flags
            base.quarantined = base.quarantined or other.quarantined
            if other.lane or other.lane_confidence or other.urgency:  # the Dispatcher branch, even below its threshold
                base.lane, base.lane_confidence, base.urgency = other.lane, other.lane_confidence, other.urgency
            if other.disposition and not base.disposition:
                base.disposition, base.human_queue = other.disposition, other.human_queue
            base.reasons += [r for r in other.reasons if r not in base.reasons]
        base.findings.pop("_fork", None)
        prefix, forked = base.timeline[:mark["timeline"]], base.timeline[mark["timeline"]:]
        base.timeline = prefix + sorted(forked, key=lambda e: 0 if e["stage"] == "shield" else 1)
        stages.route_after_screening(base)
        if base.disposition:  # quarantine, fail-safe or low dispatch confidence decided at screening
            for content in tooltrace.policy_contents(base, "screening", [r.agent for r in base.ledger.decisions]):
                await ctx.yield_output(content)
        await ctx.send_message(base)


class ModelRoutedWriter(Executor):
    """LLM writer. Microsoft-Decision-1 picks the cheapest sufficient model tier; judge feedback drives revisions."""

    def __init__(self, deps: Deps, lane: str, role: str, prompt_builder: Any) -> None:
        super().__init__(id=f"{lane}_writer")
        self.deps, self.lane, self.role, self.prompt_builder = deps, lane, role, prompt_builder

    @handler
    async def handle(self, case: CaseState, ctx: WorkflowContext[CaseState, Content]) -> None:
        try:
            if case.llm_tier is None:
                state = {"task": self.role.replace("_", " "), "lane": self.lane, "disposition": case.disposition,
                         "amount_usd": _amount(case.payload), **_risk_signals(case)}
                rec = case.ledger.add(await self.deps.decisions.decide(stage="router", agent="Model Router", state=state,
                                                                        questions=questions.ROUTER))
                tier, conf = choice(rec, "model_tier")
                case.llm_tier = tier
                for content in tooltrace.decision_contents(rec, {"routed_to_model": self.deps.llms.model_for(tier)}):
                    await ctx.yield_output(content)
                await ctx.yield_output(reasoning(case.log("router", "Model Router", "decision",
                                                          f"{tier} tier → {self.deps.llms.model_for(tier)} ({conf:.2f})", rec.latency_ms)))
            prompt = self.prompt_builder(case)
            if case.judge_feedback and case.draft:
                prompt += ("\n\nYour previous draft:\n" + json.dumps(case.draft, ensure_ascii=False) +
                           "\n\nA reviewer rejected it. Fix every point:\n- " + "\n- ".join(case.judge_feedback))
            res = await self.deps.llms.run(self.role, case.llm_tier, prompt)
            case.draft = res.parsed.model_dump()
            case.draft_history.append({"revision": case.revisions, "agent": res.agent_name, "model": res.model, "draft": case.draft})
            case.ledger.llm_calls.append(res.as_record())
            label = "Revised draft" if case.revisions else "Draft"
            await ctx.yield_output(reasoning(case.log("draft", res.agent_name, "llm", f"{label} written with {res.model}", res.latency_ms)))
        except Exception as exc:  # noqa: BLE001 - LLM failures must not lose the decision
            case.reasons.append(f"Draft unavailable ({type(exc).__name__}); decision stands")
            case.draft = None
            case.next_step = "finalize"
        await ctx.send_message(case)


class OutputJudge(Executor):
    """Microsoft-Decision-1 grades every LLM draft against a rubric: accept, revise (max 2) or hand to a human."""

    def __init__(self, deps: Deps, lane: str) -> None:
        super().__init__(id=f"{lane}_judge")
        self.deps, self.lane = deps, lane

    @handler
    async def handle(self, case: CaseState, ctx: WorkflowContext[CaseState, Content]) -> None:
        if not case.draft:
            case.next_step = "finalize"
            await ctx.send_message(case)
            return
        spec = questions.JUDGES[self.lane]
        state = {"case": {"id": case.case_id, "lane": self.lane, "disposition": case.disposition, "facts": _facts(case)},
                 "draft": case.draft}
        try:
            rec = case.ledger.add(await self.deps.decisions.decide(stage="judge", agent="Output Judge", state=state,
                                                                    questions=spec["questions"]))
            ok, feedback = policy.judge(self.lane, rec, spec)
            if ok:
                case.next_step, case.judge_feedback = "finalize", []
                verdict = "accepted"
            elif case.revisions < MAX_REVISIONS:
                case.revisions += 1
                case.next_step, case.judge_feedback = "revise", feedback
                failed = [m.group(1) for f in feedback if (m := re.search(r"'([^']+)'", f))] or [spec["score"]]
                verdict = f"revise #{case.revisions} — {', '.join(failed)}"
            else:
                case.next_step = "finalize"
                case.reasons.append("Draft still failed the judge after 2 revisions — a human must edit it before sending")
                verdict = "rejected → human edit"
            for content in tooltrace.decision_contents(rec, {"policy_verdict": verdict, "feedback_to_writer": feedback}):
                await ctx.yield_output(content)
            await ctx.yield_output(reasoning(case.log("judge", "Output Judge", "decision", verdict, rec.latency_ms)))
        except DecisionError as exc:
            case.next_step = "finalize"
            case.reasons.append(f"Judge unavailable ({type(exc).__name__}) — draft requires human review")
        await ctx.send_message(case)


class HumanTriage(Executor):
    def __init__(self) -> None:
        super().__init__(id="human_triage")

    @handler
    async def handle(self, case: CaseState, ctx: WorkflowContext[CaseState, Content]) -> None:
        await ctx.yield_output(reasoning(case.log("triage", "Policy", "policy", f"{case.disposition} → {case.human_queue}")))
        await ctx.send_message(case)


class GeneralAssistant(Executor):
    def __init__(self, deps: Deps) -> None:
        super().__init__(id="general_assistant")
        self.deps = deps

    @handler
    async def handle(self, case: CaseState, ctx: WorkflowContext[CaseState, Content]) -> None:
        case.disposition = "AUTO_REPLY"
        case.reasons.append(f"Microsoft-Decision-1 routed the message to general assistance (lane confidence {case.lane_confidence:.2f}): "
                            "a general question with no account action, answered directly")
        for content in tooltrace.policy_contents(case, "general", ["Dispatcher"]):
            await ctx.yield_output(content)
        try:
            res = await self.deps.llms.run("general_assistant", "fast", case.payload.get("message", ""))
            case.draft = res.parsed.model_dump()
            case.ledger.llm_calls.append(res.as_record())
            await ctx.yield_output(reasoning(case.log("draft", res.agent_name, "llm", f"Answer written with {res.model}", res.latency_ms)))
        except Exception as exc:  # noqa: BLE001
            case.reasons.append(f"Assistant unavailable ({type(exc).__name__})")
        await ctx.send_message(case)


class Finalizer(Executor):
    def __init__(self) -> None:
        super().__init__(id="finalizer")

    @handler
    async def handle(self, case: CaseState, ctx: WorkflowContext[Never, str]) -> None:
        await ctx.yield_output(render_report(case))


def _amount(payload: dict[str, Any]) -> float | None:
    for path in (("cart", "total_usd"), ("transaction", "amount_usd"), ("payment", "amount_usd")):
        node = payload.get(path[0]) or {}
        if path[1] in node:
            return node[path[1]]
    return None


def _risk_signals(case: CaseState) -> dict[str, Any]:
    text = " ".join(case.reasons).lower()
    triage = case.findings.get("dispute_triage") or {}
    return {
        "conflicting_evidence": bool(triage.get("first_party_misuse", 0) >= 0.3 or "contradicts" in text or "misuse" in text),
        "suspected_fraud_or_manipulation": any(k in text for k in ("manipulation", "prohibited", "mismatch", "deceptive", "fraud",
                                                                    "coached", "takeover", "unverified")),
        "audience": "analyst and customer" if case.disposition in ("ANALYST_REVIEW", "UNDERWRITER_REVIEW", "COMPLIANCE_REFERRAL") else "customer",
    }


def _facts(case: CaseState) -> dict[str, Any]:
    facts: dict[str, Any] = {"decision_reasons": case.reasons[:8]}
    p = case.payload
    if p.get("cart"):
        facts.update({"merchant": p["cart"].get("merchant_name"), "total_usd": p["cart"].get("total_usd"),
                      "mandate": (p.get("intent_mandate") or {}).get("text"), "items": [i.get("description") for i in p["cart"].get("items", [])]})
    if p.get("transaction"):
        facts.update({"transaction": p["transaction"], "customer_message": p.get("message")})
    if p.get("merchant"):
        facts.update({"merchant": p["merchant"], "website_evidence": p.get("website_snapshot")})
    if p.get("payment"):
        facts.update({"payment": p["payment"], "support_chat": p.get("support_chat")})
    facts["policies"] = {"auto_credit_limit_usd": reference()["policies"]["auto_credit_limit_usd"]}
    return facts
