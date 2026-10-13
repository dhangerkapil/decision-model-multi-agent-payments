"""Lane executors: thin Agent Framework wrappers over the shared Microsoft-Decision-1 stages, plus the writer prompts."""

import json

from agent_framework import Content, Executor, WorkflowContext, handler

from . import stages, tooltrace
from .case import CaseState, consumer
from .executors_core import Deps, reasoning


class _StageExecutor(Executor):
    stage = None

    def __init__(self, deps: Deps, executor_id: str) -> None:
        super().__init__(id=executor_id)
        self.deps = deps

    async def _run(self, case: CaseState, ctx: WorkflowContext[CaseState, Content]) -> None:
        since, before = len(case.ledger.decisions), tooltrace.outcome_key(case)
        lines = await type(self).stage(case, self.deps.decisions)
        await tooltrace.emit(ctx, case, self.id, since=since, before=before)
        for line in lines:
            await ctx.yield_output(reasoning(line))
        await ctx.send_message(case)


class MandateGuardian(_StageExecutor):
    stage = staticmethod(stages.checkout_stage)

    def __init__(self, deps: Deps) -> None:
        super().__init__(deps, "mandate_guardian")

    @handler
    async def handle(self, case: CaseState, ctx: WorkflowContext[CaseState, Content]) -> None:
        await self._run(case, ctx)


class DisputeAnalyst(_StageExecutor):
    stage = staticmethod(stages.dispute_stage)

    def __init__(self, deps: Deps) -> None:
        super().__init__(deps, "dispute_analyst")

    @handler
    async def handle(self, case: CaseState, ctx: WorkflowContext[CaseState, Content]) -> None:
        await self._run(case, ctx)


class LiabilityArbiter(_StageExecutor):
    """Decides who bears the loss when an AI shopping agent bought the wrong thing."""

    stage = staticmethod(stages.liability_stage)

    def __init__(self, deps: Deps) -> None:
        super().__init__(deps, "liability_arbiter")

    @handler
    async def handle(self, case: CaseState, ctx: WorkflowContext[CaseState, Content]) -> None:
        await self._run(case, ctx)


class MerchantReferral(_StageExecutor):
    """Cross-lane hand-off: opens a Merchant Integrity case for a merchant that manipulated a shopping agent."""

    stage = staticmethod(stages.referral_stage)

    def __init__(self, deps: Deps) -> None:
        super().__init__(deps, "merchant_referral")

    @handler
    async def handle(self, case: CaseState, ctx: WorkflowContext[CaseState, Content]) -> None:
        await self._run(case, ctx)


class MerchantClassifier(_StageExecutor):
    stage = staticmethod(stages.merchant_stage)

    def __init__(self, deps: Deps) -> None:
        super().__init__(deps, "merchant_classifier")

    @handler
    async def handle(self, case: CaseState, ctx: WorkflowContext[CaseState, Content]) -> None:
        await self._run(case, ctx)


class ScamScreen(_StageExecutor):
    stage = staticmethod(stages.scam_stage)

    def __init__(self, deps: Deps) -> None:
        super().__init__(deps, "scam_screen")

    @handler
    async def handle(self, case: CaseState, ctx: WorkflowContext[CaseState, Content]) -> None:
        await self._run(case, ctx)


def checkout_prompt(case: CaseState) -> str:
    p = case.payload
    return ("Write the consumer notification for this agent-initiated purchase.\n"
            f"Disposition: {case.disposition}\nConsumer: {consumer(p).get('name', 'the consumer')}\n"
            f"Shopping agent: {(p.get('agent') or {}).get('name')}\n"
            f"Consumer's instructions (mandate): {p['intent_mandate']['text']} (hard cap ${p['intent_mandate'].get('hard_cap_usd')})\n"
            f"Cart at {p['cart']['merchant_name']}: " + json.dumps(p["cart"]["items"]) + f" — total ${p['cart']['total_usd']}\n"
            "Why it needs attention: " + " | ".join(case.reasons[:5]))


def dispute_prompt(case: CaseState) -> str:
    p = case.payload
    return ("Write the dispute communications.\n"
            f"Disposition: {case.disposition}\nCardholder: {consumer(p).get('name', 'the cardholder')} ({p.get('payment_method', '')})\n"
            f"Cardholder message: {p.get('message')}\nTransaction: {json.dumps(p.get('transaction'))}\n"
            f"Merchant evidence: {json.dumps(p.get('merchant_evidence'))}\nTriage: {json.dumps(case.findings.get('dispute_triage'))}\n"
            + (f"Liability assessment (for the analyst only, do not share with the customer): {json.dumps(case.findings['liability'])}\n"
               if case.findings.get("liability") else "")
            + "Decision reasons: " + " | ".join(case.reasons[:6]))


def merchant_prompt(case: CaseState) -> str:
    p = case.payload
    return ("Write the underwriting memo.\n"
            f"Disposition: {case.disposition}\nMerchant: {json.dumps(p.get('merchant'))}\nTrigger: {p.get('trigger')}\n"
            f"Website snapshot: {p.get('website_snapshot')}\nAcquirer notes: {p.get('acquirer_notes')}\n"
            "Sentences flagged by the evidence highlighter: " + json.dumps(case.findings.get("evidence_lines", [])) + "\n"
            "Decision reasons: " + " | ".join(case.reasons[:6]))


def scam_prompt(case: CaseState) -> str:
    p = case.payload
    return ("Write the customer protection messages.\n"
            f"Disposition: {case.disposition}\nCustomer profile: {json.dumps(p.get('customer_profile'))}\n"
            f"Payment: {json.dumps(p.get('payment'))}\nContext: {json.dumps(p.get('context'))}\n"
            f"Support chat: {p.get('support_chat', '')}\nMessage: {p.get('message', '')}\nDecision reasons: " + " | ".join(case.reasons[:6]))
