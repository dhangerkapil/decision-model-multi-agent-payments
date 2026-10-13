"""Builds the Payments Trust Fabric multi-agent workflow (Microsoft Agent Framework)."""

import dataclasses
import os
from typing import Any

from agent_framework import Case, Default, WorkflowBuilder
from azure.identity import DefaultAzureCredential

from .case import CaseState
from .config import load_settings
from .decision_engine import DecisionClient
from .executors_core import (Deps, Dispatcher, Finalizer, Gate, GeneralAssistant, HumanTriage, Intake, ModelRoutedWriter,
                             OutputJudge, ScreeningHub, Shield)
from .executors_lanes import (DisputeAnalyst, LiabilityArbiter, MandateGuardian, MerchantClassifier, MerchantReferral, ScamScreen,
                              checkout_prompt, dispute_prompt, merchant_prompt, scam_prompt)
from .llm import LLMPool
from .prompts import load_prompt_book
from .stages import needs_liability, needs_referral

AGENT_NAME = os.environ.get("AGENT_NAME", "decision-payments-agent")
AGENT_DESCRIPTION = ("Payments Trust Fabric: Microsoft-Decision-1 makes fast, calibrated trust decisions for agentic checkout, disputes, "
                     "merchant integrity and scam protection; LLM agents draft; humans own adverse actions.")

_DEPS: Deps | None = None


def deps() -> Deps:
    """Process-wide clients shared by every request (connection pooling and token caching)."""
    global _DEPS
    if _DEPS is None:
        prompts = load_prompt_book()
        settings = load_settings()
        if prompts.model:
            settings = dataclasses.replace(settings, llm_standard=prompts.model)
        credential = DefaultAzureCredential()
        _DEPS = Deps(settings=settings,
                     decisions=DecisionClient(settings.decision_endpoint, settings.decision_deployment, credential,
                                              rpm_per_deployment=settings.decision_rpm_per_deployment),
                     llms=LLMPool(settings, credential, prompts))
    return _DEPS


def _when(predicate: Any) -> Any:
    return lambda message: isinstance(message, CaseState) and predicate(message)


def build_workflow(d: Deps | None = None):
    d = d or deps()
    intake, hub, gate, finalizer = Intake(), ScreeningHub(), Gate(), Finalizer()
    shield, dispatcher = Shield(d), Dispatcher(d)
    triage_desk, general = HumanTriage(), GeneralAssistant(d)

    guardian = MandateGuardian(d)
    checkout_writer = ModelRoutedWriter(d, "agent_payment", "consumer_notification_writer", checkout_prompt)
    checkout_judge = OutputJudge(d, "agent_payment")
    merchant_referral = MerchantReferral(d)

    dispute_analyst = DisputeAnalyst(d)
    liability_arbiter = LiabilityArbiter(d)
    dispute_writer = ModelRoutedWriter(d, "dispute", "dispute_case_writer", dispute_prompt)
    dispute_judge = OutputJudge(d, "dispute")

    merchant = MerchantClassifier(d)
    memo_writer = ModelRoutedWriter(d, "merchant_review", "underwriting_memo_writer", merchant_prompt)
    memo_judge = OutputJudge(d, "merchant_review")

    scam = ScamScreen(d)
    protection_writer = ModelRoutedWriter(d, "scam_or_account_risk", "protection_writer", scam_prompt)
    protection_judge = OutputJudge(d, "scam_or_account_risk")

    intermediates = [intake, shield, dispatcher, gate, triage_desk, general, guardian, checkout_writer, checkout_judge,
                     merchant_referral, dispute_analyst, liability_arbiter, dispute_writer, dispute_judge, merchant, memo_writer,
                     memo_judge, scam, protection_writer, protection_judge]

    b = WorkflowBuilder(name=AGENT_NAME, description=AGENT_DESCRIPTION, start_executor=intake, output_from=[finalizer],
                        intermediate_output_from=intermediates)

    b.add_switch_case_edge_group(intake, [Case(condition=_when(lambda c: c.mode == "help"), target=finalizer), Default(target=hub)])
    b.add_fan_out_edges(hub, [shield, dispatcher])
    b.add_fan_in_edges([shield, dispatcher], gate)
    b.add_switch_case_edge_group(gate, [
        Case(condition=_when(lambda c: c.quarantined), target=finalizer),
        Case(condition=_when(lambda c: c.disposition == "HUMAN_TRIAGE" or not c.lane), target=triage_desk),
        Case(condition=_when(lambda c: c.lane == "agent_payment"), target=guardian),
        Case(condition=_when(lambda c: c.lane == "dispute"), target=dispute_analyst),
        Case(condition=_when(lambda c: c.lane == "merchant_review"), target=merchant),
        Case(condition=_when(lambda c: c.lane == "scam_or_account_risk"), target=scam),
        Default(target=general),
    ])

    is_draft = _when(lambda c: c.next_step == "draft")
    is_revise = _when(lambda c: c.next_step == "revise")

    def judge_loop(writer: Any, judge: Any, after_accept: list[Any] | None = None) -> None:
        b.add_edge(writer, judge)
        b.add_switch_case_edge_group(judge, [Case(condition=is_revise, target=writer), *(after_accept or []), Default(target=finalizer)])

    # Lane 1: Mandate Guardian -> (writer -> judge) -> cross-lane Merchant Referral when the agent was manipulated.
    to_referral = Case(condition=_when(needs_referral), target=merchant_referral)
    b.add_switch_case_edge_group(guardian, [Case(condition=is_draft, target=checkout_writer), to_referral, Default(target=finalizer)])
    judge_loop(checkout_writer, checkout_judge, [Case(condition=_when(needs_referral), target=merchant_referral)])
    b.add_edge(merchant_referral, finalizer)

    # Lane 2: Dispute Analyst -> Agent Liability Arbiter (agent-purchase disputes only) -> writer -> judge.
    b.add_switch_case_edge_group(dispute_analyst, [Case(condition=_when(needs_liability), target=liability_arbiter),
                                                   Case(condition=is_draft, target=dispute_writer), Default(target=finalizer)])
    b.add_switch_case_edge_group(liability_arbiter, [Case(condition=is_draft, target=dispute_writer), Default(target=finalizer)])
    judge_loop(dispute_writer, dispute_judge)

    # Lanes 3 and 4.
    b.add_switch_case_edge_group(merchant, [Case(condition=is_draft, target=memo_writer), Default(target=finalizer)])
    judge_loop(memo_writer, memo_judge)
    b.add_switch_case_edge_group(scam, [Case(condition=is_draft, target=protection_writer), Default(target=finalizer)])
    judge_loop(protection_writer, protection_judge)

    b.add_edge(general, finalizer)
    b.add_edge(triage_desk, finalizer)
    return b.build()


def build_agent():
    """Agent factory: the hosting server calls this once per request, so runs never share workflow state."""
    return build_workflow().as_agent(name=AGENT_NAME, description=AGENT_DESCRIPTION)
