"""Decision stages shared by the hosted workflow, the decision-only Firehose and the evaluation harness.

Each stage mutates the CaseState and returns human-readable trail lines.
"""

import asyncio
import re
from datetime import date

from . import policy, questions
from .case import CaseState, dispatch_view, reference, shield_view
from .decision_engine import DecisionClient, DecisionError, choice, noul, score


def fail_safe(case: CaseState, stage: str, exc: Exception) -> list[str]:
    case.disposition = "HUMAN_TRIAGE"
    case.human_queue = policy.HUMAN_QUEUES["HUMAN_TRIAGE"]
    detail = re.sub(r"\s+", " ", str(exc))[:90]
    case.reasons.append(f"{stage} unavailable ({type(exc).__name__}: {detail}) — failing safe to a human")
    case.next_step = "finalize"
    return [case.log(stage, "Fail-safe", "policy", "Decision service error — routed to human triage")]


def _apply(case: CaseState, outcome: policy.Outcome) -> list[str]:
    case.disposition = outcome.disposition
    case.human_queue = outcome.human_queue
    case.reasons += outcome.reasons
    case.next_step = "draft" if outcome.draft_role else "finalize"
    return [case.log("policy", "Confidence Policy", "policy", f"{case.disposition} → {case.human_queue or 'automatic'}")]


async def consistency_guard(case: CaseState, dc: DecisionClient, lane: str, outcome: policy.Outcome, record, state,
                            question_defs: dict) -> tuple[policy.Outcome, list[str]]:
    """Second opinion for borderline automatic outcomes: re-ask the deciding question with the options reversed.

    Microsoft-Decision-1 is designed to be stable under option reordering; if the answer still changes, the automatic outcome is
    replaced by the next safer step so that a person (or the consumer) decides.
    """
    name = outcome.driver
    if outcome.disposition not in policy.AUTOMATIC or not name or name not in question_defs:
        return outcome, []
    first, conf = choice(record, name)
    if conf >= policy.CONSISTENCY_CHECK_BELOW:
        return outcome, []
    original = question_defs[name]
    reordered = {**original, "criteria": dict(reversed(list(original["criteria"].items())))}
    try:
        rec = case.ledger.add(await dc.decide(stage="consistency", agent="Consistency Guard", state=state, questions={name: reordered}))
    except DecisionError:
        return policy.escalate(outcome, lane, "Consistency Guard unavailable — escalated to a safer step"), []
    second, conf2 = choice(rec, name)
    stable = second == first
    case.findings["consistency"] = {"question": name, "first": first, "first_confidence": round(conf, 3), "reordered": second,
                                    "reordered_confidence": round(conf2, 3), "stable": stable}
    line = case.log("consistency", "Consistency Guard", "decision",
                    f"{name} was borderline ({first} {conf:.2f}); re-asked with options reversed → {second} ({conf2:.2f}) · "
                    + ("stable, automatic outcome kept" if stable else "UNSTABLE → escalated"), rec.latency_ms)
    if stable:
        return outcome, [line]
    return policy.escalate(outcome, lane, f"Consistency Guard: '{name}' changed from {first} to {second} when options were reordered — "
                                          "a person decides"), [line]


async def shield_stage(case: CaseState, dc: DecisionClient) -> list[str]:
    try:
        rec = case.ledger.add(await dc.decide(stage="shield", agent="Shield (AI firewall)", group="screening",
                                              state=shield_view(case.payload), questions=questions.SHIELD))
    except DecisionError as exc:
        case.quarantined = True
        return fail_safe(case, "shield", exc)
    quarantine, flags, reasons = policy.shield(rec)
    case.shield_flags, case.quarantined = flags, quarantine
    if quarantine:
        case.disposition, case.human_queue = "QUARANTINED", policy.HUMAN_QUEUES["QUARANTINED"]
        case.reasons += reasons + ["Content withheld from all LLM agents"]
    summary = (f"injection {flags['prompt_injection']:.2f} · social engineering {flags['social_engineering']:.2f} · "
               f"exfiltration {flags['data_exfiltration']:.2f}" + (" → QUARANTINE" if quarantine else " → clear"))
    return [case.log("shield", "Shield (AI firewall)", "decision", summary, rec.latency_ms)]


async def dispatch_stage(case: CaseState, dc: DecisionClient) -> list[str]:
    try:
        rec = case.ledger.add(await dc.decide(stage="dispatch", agent="Dispatcher", group="screening",
                                              state=dispatch_view(case.payload), questions=questions.DISPATCHER))
    except DecisionError as exc:
        case.lane = None
        return fail_safe(case, "dispatch", exc)
    lane, conf, urgency = policy.dispatch(rec)
    raw_lane, _ = choice(rec, "lane")
    case.lane, case.lane_confidence, case.urgency = lane, conf, urgency
    return [case.log("dispatch", "Dispatcher", "decision", f"lane {raw_lane} ({conf:.2f}) · urgency {urgency}" + ("" if lane else " → human triage"),
                     rec.latency_ms)]


def route_after_screening(case: CaseState) -> None:
    if not case.quarantined and not case.lane and not case.disposition:
        case.disposition, case.human_queue = "HUMAN_TRIAGE", policy.HUMAN_QUEUES["HUMAN_TRIAGE"]
        case.reasons.append(f"Dispatcher confidence {case.lane_confidence:.2f} below {policy.DISPATCH_MIN_CONFIDENCE} or lane unclear")


async def checkout_stage(case: CaseState, dc: DecisionClient) -> list[str]:
    p = case.payload
    if not p.get("cart") or not p.get("intent_mandate"):
        return _apply(case, policy.Outcome("HUMAN_TRIAGE", ["Agent payment lane selected but the event has no cart or intent mandate"]))
    items = p["cart"].get("items", [])
    state = {"intent_mandate": p["intent_mandate"], "cart": p["cart"], "agent": p.get("agent"),
             "agent_browsing_trace": p.get("agent_browsing_trace", "")}
    try:
        guardian = case.ledger.add(await dc.decide(stage="checkout", agent="Mandate Guardian", state=state,
                                                   questions={**questions.CHECKOUT, **questions.line_item_questions(items)}))
    except DecisionError as exc:
        return fail_safe(case, "checkout", exc)
    line_scores = [(i["description"], noul(guardian, f"line_{n}_in_mandate")) for n, i in enumerate(items, 1)]
    case.findings["line_items"] = [{"item": d, "p_in_mandate": round(s, 3)} for d, s in line_scores]
    manipulation = noul(guardian, "manipulation_detected")
    case.findings["referral_pending"] = manipulation >= 0.70
    action, conf = choice(guardian, "action")
    outside = [d for d, s in line_scores if s < 0.5]
    trail = [case.log("checkout", "Mandate Guardian", "decision",
                      f"{len(guardian.answers)} decisions in one call: match {noul(guardian, 'mandate_match'):.2f} · manipulation "
                      f"{manipulation:.2f} · out-of-scope {noul(guardian, 'out_of_scope_items'):.2f} · "
                      f"action {action} ({conf:.2f}) · {len(items)} line items" + (f", outside mandate: {'; '.join(outside)}" if outside else
                                                                                   ", all inside mandate"), guardian.latency_ms)]
    outcome, guard = await consistency_guard(case, dc, "agent_payment", policy.checkout(p, guardian, line_scores, reference()["policies"]),
                                             guardian, state, questions.CHECKOUT)
    return trail + guard + _apply(case, outcome)


async def dispute_stage(case: CaseState, dc: DecisionClient) -> list[str]:
    """Dispute Analyst: reason code, Reg E/Z notice, misuse signals and evidence assessment in one request."""
    p = case.payload
    state = {"cardholder_message": p.get("message"), "transaction": p.get("transaction"), "merchant_evidence": p.get("merchant_evidence"),
             "account_history": p.get("account_history"), "auto_credit_limit_usd": reference()["policies"]["auto_credit_limit_usd"]}
    try:
        rec = case.ledger.add(await dc.decide(stage="dispute", agent="Dispute Analyst", state=state,
                                              questions={**questions.DISPUTE_TRIAGE, **questions.DISPUTE_EVIDENCE}))
    except DecisionError as exc:
        return fail_safe(case, "dispute", exc)
    reason, conf = choice(rec, "reason_category")
    outcome_name, oconf = choice(rec, "recommended_outcome")
    case.findings["dispute_triage"] = {"reason": reason, "confidence": conf, "billing_error_notice": noul(rec, "billing_error_notice"),
                                       "first_party_misuse": noul(rec, "first_party_misuse_signals"),
                                       "evidence_contradicts_claim": noul(rec, "evidence_contradicts_claim")}
    trail = [case.log("dispute", "Dispute Analyst", "decision",
                      f"{len(rec.answers)} decisions in one call: {reason} ({conf:.2f}) · Reg E/Z notice {noul(rec, 'billing_error_notice'):.2f} · "
                      f"first-party misuse {noul(rec, 'first_party_misuse_signals'):.2f} · contradicts claim "
                      f"{noul(rec, 'evidence_contradicts_claim'):.2f} · next step {outcome_name} ({oconf:.2f})", rec.latency_ms)]
    outcome = policy.dispute(p, rec, rec, reference()["policies"], date.fromisoformat(reference()["as_of"]))
    outcome, guard = await consistency_guard(case, dc, "dispute", outcome, rec, state, questions.DISPUTE_EVIDENCE)
    return trail + guard + _apply(case, outcome)


async def liability_stage(case: CaseState, dc: DecisionClient) -> list[str]:
    """Agent Liability Arbiter: who bears the loss when an AI shopping agent bought the wrong thing."""
    p = case.payload
    state = {"cardholder_message": p.get("message"), "transaction": p.get("transaction"), "merchant_evidence": p.get("merchant_evidence"),
             "account_history": p.get("account_history")}
    try:
        rec = case.ledger.add(await dc.decide(stage="liability", agent="Agent Liability Arbiter", state=state, questions=questions.LIABILITY))
    except DecisionError as exc:
        case.reasons.append(f"Liability Arbiter unavailable ({type(exc).__name__}) — analyst assigns liability")
        return []
    party, pconf = choice(rec, "liable_party")
    case.findings["liability"] = {"liable_party": party, "confidence": round(pconf, 3),
                                  "agent_exceeded_mandate": round(noul(rec, "agent_exceeded_mandate"), 3),
                                  "merchant_steered_agent": round(noul(rec, "merchant_steered_agent"), 3),
                                  "consumer_confirmed_purchase": round(noul(rec, "consumer_confirmed_purchase"), 3)}
    case.reasons.append(f"Liability Arbiter: {party.replace('_', ' ')} ({pconf:.2f}) — agent exceeded mandate "
                        f"p={noul(rec, 'agent_exceeded_mandate'):.2f}, merchant steered agent p={noul(rec, 'merchant_steered_agent'):.2f}, "
                        f"consumer confirmed p={noul(rec, 'consumer_confirmed_purchase'):.2f}")
    return [case.log("liability", "Agent Liability Arbiter", "decision",
                     f"liable party {party} ({pconf:.2f}) · agent exceeded mandate {noul(rec, 'agent_exceeded_mandate'):.2f} · merchant steered "
                     f"{noul(rec, 'merchant_steered_agent'):.2f} · consumer confirmed {noul(rec, 'consumer_confirmed_purchase'):.2f}", rec.latency_ms)]


def needs_liability(case: CaseState) -> bool:
    return (case.findings.get("dispute_triage") or {}).get("reason") == "agent_purchase_mismatch" and case.disposition != "HUMAN_TRIAGE"


def _sentences(text: str, limit: int = 8) -> list[str]:
    parts = [s.strip() for s in re.split(r"(?<=[.!?'])\s+", text or "") if len(s.strip()) > 12]
    return parts[:limit]


async def merchant_stage(case: CaseState, dc: DecisionClient) -> list[str]:
    p = case.payload
    merchant = dict(p.get("merchant") or {})
    mcc = str(merchant.get("declared_mcc", ""))
    merchant["declared_mcc_name"] = (reference()["mcc_catalog"].get(mcc) or {}).get("name", "unknown")
    lines = _sentences(p.get("website_snapshot", ""))
    state = {"merchant": merchant, "trigger": p.get("trigger"), "website_snapshot": p.get("website_snapshot"),
             "website_sentences": {f"sentence_{n}": s for n, s in enumerate(lines, 1)}, "acquirer_notes": p.get("acquirer_notes")}
    try:
        rec = case.ledger.add(await dc.decide(stage="merchant", agent="Merchant Classifier", state=state,
                                              questions={**questions.MERCHANT, **questions.evidence_line_questions(lines)}))
    except DecisionError as exc:
        return fail_safe(case, "merchant", exc)
    flagged = [s for n, s in enumerate(lines, 1) if noul(rec, f"sentence_{n}_risk_evidence") >= 0.5]
    case.findings["evidence_lines"] = flagged
    observed, oconf = choice(rec, "observed_mcc_family")
    integrity, _ = choice(rec, "integrity_risk_category")
    trail = [case.log("merchant", "Merchant Classifier", "decision",
                      f"{len(rec.answers)} decisions in one call: observed {observed} ({oconf:.2f}) vs declared {mcc} · mismatch "
                      f"{noul(rec, 'declared_mcc_mismatch'):.2f} · prohibited {noul(rec, 'prohibited_goods'):.2f} · deceptive billing "
                      f"{noul(rec, 'deceptive_billing'):.2f} · integrity {integrity} · {len(flagged)}/{len(lines)} website sentences flagged",
                      rec.latency_ms)]
    outcome = policy.merchant(p, rec, reference()["mcc_catalog"])
    outcome, guard = await consistency_guard(case, dc, "merchant_review", outcome, rec, state, questions.MERCHANT)
    if outcome.disposition == "AUTO_APPROVE":
        outcome.draft_role = None
        outcome.reasons.append("No LLM memo needed — clean approval documented by the decision ledger")
    return trail + guard + _apply(case, outcome)


async def referral_stage(case: CaseState, dc: DecisionClient) -> list[str]:
    """Cross-lane hand-off: a merchant that manipulated a shopping agent gets its own Merchant Integrity case."""
    case.findings["referral_pending"] = False
    p = case.payload
    cart = p.get("cart") or {}
    known = reference()["merchants"].get(cart.get("merchant_id", ""), {})
    sub = CaseState(
        case_id=f"REF-{case.case_id}", title=f"Merchant integrity referral — {cart.get('merchant_name')}", channel="internal",
        payload={"merchant": {"dba": cart.get("merchant_name"), "declared_mcc": cart.get("mcc"), "country": "US",
                              "website": known.get("domain"), "vamp_ratio_pct": known.get("vamp_ratio_pct")},
                 "trigger": "agent_manipulation_referral", "website_snapshot": p.get("agent_browsing_trace", ""),
                 "acquirer_notes": "Referral from the Agentic Checkout Guardian: page content tried to steer an AI shopping agent."})
    started = len(sub.ledger.decisions)
    await merchant_stage(sub, dc)
    for rec in sub.ledger.decisions[started:]:
        rec.agent = "Merchant Classifier (referral)"
        case.ledger.add(rec)
    latency = sum(r.latency_ms for r in sub.ledger.decisions)
    case.findings["merchant_referral"] = {"case_id": sub.case_id, "merchant": cart.get("merchant_name"), "disposition": sub.disposition,
                                         "queue": sub.human_queue, "reasons": sub.reasons[:3], "evidence": sub.findings.get("evidence_lines", [])}
    case.reasons.append(f"Linked case {sub.case_id}: {cart.get('merchant_name')} → {sub.disposition} ({sub.human_queue or 'automatic'})")
    return [case.log("referral", "Merchant Integrity (referral)", "decision",
                     f"opened {sub.case_id} for {cart.get('merchant_name')}: {sub.disposition} → {sub.human_queue or 'automatic'}", latency)]


def needs_referral(case: CaseState) -> bool:
    return bool(case.findings.get("referral_pending"))


async def scam_stage(case: CaseState, dc: DecisionClient) -> list[str]:
    p = case.payload
    state = {"payment": p.get("payment"), "context": p.get("context"), "customer_profile": p.get("customer_profile"),
             "support_chat": p.get("support_chat"), "message": p.get("message"), "sanctions_watchlist": reference()["watchlist"]}
    try:
        rec = case.ledger.add(await dc.decide(stage="scam", agent="Scam & Sanctions Screen", state=state, questions=questions.SCAM))
    except DecisionError as exc:
        return fail_safe(case, "scam", exc)
    typ, tconf = choice(rec, "scam_typology")
    interv, iconf = choice(rec, "intervention")
    trail = [case.log("scam", "Scam & Sanctions Screen", "decision",
                      f"sanctions {noul(rec, 'sanctions_true_hit'):.3f} · {typ} ({tconf:.2f}) · coached {noul(rec, 'coached_customer'):.2f} · "
                      f"ATO {noul(rec, 'ato_signals'):.2f} · {interv} ({iconf:.2f})", rec.latency_ms)]
    outcome, guard = await consistency_guard(case, dc, "scam_or_account_risk", policy.scam(p, rec), rec, state, questions.SCAM)
    return trail + guard + _apply(case, outcome)


async def decide_only(case: CaseState, dc: DecisionClient) -> CaseState:
    """Firehose: the full decision path with no LLM calls (drafts are skipped)."""
    await asyncio.gather(shield_stage(case, dc), dispatch_stage(case, dc))
    route_after_screening(case)
    if case.quarantined or case.disposition == "HUMAN_TRIAGE":
        return case
    if case.lane == "agent_payment":
        await checkout_stage(case, dc)
        if needs_referral(case):
            await referral_stage(case, dc)
    elif case.lane == "dispute":
        await dispute_stage(case, dc)
        if needs_liability(case):
            await liability_stage(case, dc)
    elif case.lane == "merchant_review":
        await merchant_stage(case, dc)
    elif case.lane == "scam_or_account_risk":
        await scam_stage(case, dc)
    else:
        case.disposition = "AUTO_REPLY"
    return case
