"""Confidence policy: turns calibrated Microsoft-Decision-1 answers into dispositions.

Principle: Microsoft-Decision-1 never takes an adverse action alone. Automatic outcomes only go in the customer's
favour (approve a matching purchase, auto-credit a small clear error, send an information request or a
warning). Anything adverse goes to a human: the consumer (step-up) or an analyst queue.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from .decision_engine import DecisionRecord, choice, noul, score

INJECTION_QUARANTINE = 0.70
EXFILTRATION_QUARANTINE = 0.80
DISPATCH_MIN_CONFIDENCE = 0.55

HUMAN_QUEUES = {
    "QUARANTINED": "Security operations",
    "HUMAN_TRIAGE": "Operations triage",
    "CONSUMER_STEP_UP": "Consumer (step-up confirmation)",
    "RISK_REVIEW": "Agentic commerce risk desk",
    "ANALYST_REVIEW": "Disputes analyst",
    "UNDERWRITER_REVIEW": "Merchant underwriting",
    "COMPLIANCE_REFERRAL": "Merchant compliance",
    "HUMAN_HOLD": "Scam intervention team (call-back)",
    "SANCTIONS_HOLD": "Sanctions screening team",
    "ACCOUNT_SECURITY_REVIEW": "Account security team",
}

AUTOMATIC = {"AUTO_APPROVE", "AUTO_CREDIT", "AUTO_REQUEST_INFO", "APPROVE_WITH_MONITORING", "ALLOW", "AUTO_WARNING", "AUTO_REPLY"}
CONSISTENCY_CHECK_BELOW = 0.70

# When the Consistency Guard finds an unstable answer, an automatic outcome is replaced by the next safer step.
_ESCALATION = {
    "AUTO_APPROVE": {"agent_payment": ("CONSUMER_STEP_UP", "agent_payment"), "merchant_review": ("UNDERWRITER_REVIEW", "merchant_review")},
    "APPROVE_WITH_MONITORING": {"merchant_review": ("UNDERWRITER_REVIEW", "merchant_review")},
    "AUTO_CREDIT": {"dispute": ("ANALYST_REVIEW", "dispute")},
    "AUTO_REQUEST_INFO": {"dispute": ("ANALYST_REVIEW", "dispute")},
    "ALLOW": {"scam_or_account_risk": ("AUTO_WARNING", "scam_or_account_risk")},
    "AUTO_WARNING": {"scam_or_account_risk": ("HUMAN_HOLD", "scam_or_account_risk")},
}


@dataclass
class Outcome:
    disposition: str
    reasons: list[str] = field(default_factory=list)
    draft_role: str | None = None
    driver: str | None = None

    @property
    def human_queue(self) -> str | None:
        return HUMAN_QUEUES.get(self.disposition)


def escalate(outcome: Outcome, lane: str, why: str) -> Outcome:
    target = _ESCALATION.get(outcome.disposition, {}).get(lane)
    if not target:
        return outcome
    disposition, draft_role = target
    return Outcome(disposition, outcome.reasons + [why], draft_role)


def shield(rec: DecisionRecord) -> tuple[bool, dict[str, float], list[str]]:
    flags = {k: noul(rec, k) for k in rec.answers}
    reasons = []
    if flags.get("prompt_injection", 0) >= INJECTION_QUARANTINE:
        reasons.append(f"Prompt injection probability {flags['prompt_injection']:.2f} ≥ {INJECTION_QUARANTINE}")
    if flags.get("data_exfiltration", 0) >= EXFILTRATION_QUARANTINE:
        reasons.append(f"Data exfiltration probability {flags['data_exfiltration']:.2f} ≥ {EXFILTRATION_QUARANTINE}")
    return bool(reasons), flags, reasons


def dispatch(rec: DecisionRecord) -> tuple[str | None, float, str]:
    lane, conf = choice(rec, "lane")
    _, urgency = score(rec, "urgency")
    if lane == "cannot_tell" or conf < DISPATCH_MIN_CONFIDENCE:
        return None, conf, urgency
    return lane, conf, urgency


def checkout(payload: dict[str, Any], guardian: DecisionRecord, line_items: list[tuple[str, float]],
             policies: dict[str, Any]) -> Outcome:
    cart = payload.get("cart") or {}
    agent = payload.get("agent") or {}
    cap = float((payload.get("intent_mandate") or {}).get("hard_cap_usd") or policies["agent_default_hard_cap_usd"])
    total = float(cart.get("total_usd") or 0)
    over_cap = total > cap
    blocked_mcc = str(cart.get("mcc")) in set(policies["blocked_agent_mccs"])
    unverified = not agent.get("verified_signature", False)
    manip = noul(guardian, "manipulation_detected")
    match = noul(guardian, "mandate_match")
    out_scope = noul(guardian, "out_of_scope_items")
    action, conf = choice(guardian, "action")
    bad_lines = [d for d, p in line_items if p < 0.5]

    r = []
    if over_cap:
        r.append(f"Deterministic: cart total ${total:,.2f} exceeds mandate hard cap ${cap:,.2f}")
    if blocked_mcc:
        r.append(f"Deterministic: MCC {cart.get('mcc')} is blocked for agent-initiated payments")
    if bad_lines:
        r.append("Line items outside the mandate: " + "; ".join(bad_lines))

    if manip >= 0.70:
        return Outcome("RISK_REVIEW", r + [f"Manipulation of the shopping agent detected (p={manip:.2f}); merchant referred to integrity team"], "agent_payment")
    if blocked_mcc:
        return Outcome("RISK_REVIEW", r, "agent_payment")
    if unverified and (match < 0.85 or total > 200):
        return Outcome("RISK_REVIEW", r + [f"Unverified agent signature with mandate match p={match:.2f}"], "agent_payment")
    if action == "route_to_risk_review" and conf >= 0.60:
        return Outcome("RISK_REVIEW", r + [f"Microsoft-Decision-1 recommends risk review (confidence {conf:.2f})"], "agent_payment")
    if over_cap:
        return Outcome("CONSUMER_STEP_UP", r, "agent_payment")
    if match >= 0.85 and out_scope < 0.30 and not bad_lines and action == "approve" and conf >= 0.60:
        return Outcome("AUTO_APPROVE", [f"Mandate match p={match:.2f}, no out-of-scope items, approve confidence {conf:.2f}"], driver="action")
    return Outcome("CONSUMER_STEP_UP", r + [f"Mandate match p={match:.2f}, out-of-scope p={out_scope:.2f}; consumer confirmation required"],
                   "agent_payment")


def dispute(payload: dict[str, Any], triage: DecisionRecord, evidence: DecisionRecord, policies: dict[str, Any],
            today: date) -> Outcome:
    amount = float((payload.get("transaction") or {}).get("amount_usd") or 0)
    reason, rconf = choice(triage, "reason_category")
    notice = noul(triage, "billing_error_notice")
    misuse = noul(triage, "first_party_misuse_signals")
    household = noul(triage, "household_or_known_user") if "household_or_known_user" in triage.answers else 0.0
    contradicts = noul(evidence, "evidence_contradicts_claim")
    outcome, oconf = choice(evidence, "recommended_outcome")
    limit = float(policies["auto_credit_limit_usd"])

    r = [f"Reason: {reason} (confidence {rconf:.2f})"]
    if notice >= 0.5:
        due = today + timedelta(days=int(policies.get("reg_e_z_ack_days", 10)))
        r.append(f"Reg E / Reg Z error notice detected (p={notice:.2f}) — statutory clock started, acknowledge by {due.isoformat()}")
    if reason == "cannot_tell" or rconf < 0.50:
        return Outcome("ANALYST_REVIEW", r + ["Reason unclear — analyst to classify"], "dispute")
    if reason == "agent_purchase_mismatch":
        return Outcome("ANALYST_REVIEW", r + [f"New dispute type (agent purchase mismatch) — specialist review while the model is validated; "
                                              f"suggested next step {outcome} ({oconf:.2f})"], "dispute")
    if household >= 0.50:
        return Outcome("ANALYST_REVIEW", r + [f"Cardholder indicates a household member or known person made the purchase (p={household:.2f}) "
                                              "— friendly-fraud review, not eligible for automatic credit"], "dispute")
    clean = misuse < 0.30 and contradicts < 0.40 and amount <= limit
    if outcome == "auto_credit" and oconf >= 0.60 and clean:
        return Outcome("AUTO_CREDIT", r + [f"Clear error in the cardholder's favour, ${amount:,.2f} ≤ ${limit:,.0f} auto-credit limit"], "dispute",
                       driver="recommended_outcome")
    if reason == "fraud_card_absent" and rconf >= 0.80 and notice >= 0.80 and clean:
        return Outcome("AUTO_CREDIT", r + [f"Unauthorised card-absent charge with no misuse signals, ${amount:,.2f} ≤ ${limit:,.0f}: "
                                           "provisional credit, chargeback filed and card reissued automatically"], "dispute")
    if outcome == "request_more_info" and oconf >= 0.60 and (amount <= limit or rconf < 0.90):
        return Outcome("AUTO_REQUEST_INFO", r + ["Claim premature or incomplete — information request sent (non-adverse)"], "dispute",
                       driver="recommended_outcome")
    extra = [f"Recommended next step: {outcome} (confidence {oconf:.2f})"]
    if amount > limit and outcome == "auto_credit":
        extra.append(f"${amount:,.2f} exceeds the ${limit:,.0f} auto-credit limit")
    if misuse >= 0.30 or contradicts >= 0.40:
        extra.append(f"First-party misuse p={misuse:.2f}, evidence contradicts claim p={contradicts:.2f} — never auto-denied")
    return Outcome("ANALYST_REVIEW", r + extra, "dispute")


def merchant(payload: dict[str, Any], rec: DecisionRecord, mcc_catalog: dict[str, Any]) -> Outcome:
    prohibited = noul(rec, "prohibited_goods")
    mismatch = noul(rec, "declared_mcc_mismatch")
    deceptive = noul(rec, "deceptive_billing")
    manipulation = noul(rec, "agent_manipulation") if "agent_manipulation" in rec.answers else 0.0
    action, aconf = choice(rec, "action")
    integrity, _ = choice(rec, "integrity_risk_category")
    observed, oconf = choice(rec, "observed_mcc_family")
    declared = str((payload.get("merchant") or {}).get("declared_mcc", ""))
    declared_risk = (mcc_catalog.get(declared) or {}).get("risk", "medium")

    r = [f"Observed category {observed} (confidence {oconf:.2f}) vs declared MCC {declared}"]
    if prohibited >= 0.60:
        return Outcome("COMPLIANCE_REFERRAL", r + [f"Prohibited goods p={prohibited:.2f}"], "merchant_review")
    if mismatch >= 0.60 or deceptive >= 0.60:
        return Outcome("UNDERWRITER_REVIEW", r + [f"MCC mismatch p={mismatch:.2f}, deceptive billing p={deceptive:.2f}"], "merchant_review")
    if manipulation >= 0.60:
        return Outcome("UNDERWRITER_REVIEW", r + [f"Website content manipulates AI shopping agents (p={manipulation:.2f})"], "merchant_review")
    if action == "refer_to_compliance" and aconf >= 0.50:
        return Outcome("COMPLIANCE_REFERRAL", r + [f"Microsoft-Decision-1 recommends compliance referral ({aconf:.2f})"], "merchant_review")
    if action == "enhanced_due_diligence" and aconf >= 0.50:
        return Outcome("UNDERWRITER_REVIEW", r + [f"Microsoft-Decision-1 recommends enhanced due diligence ({aconf:.2f})"], "merchant_review")
    if integrity not in ("none", "cannot_tell") or action == "approve_with_monitoring" or declared_risk == "high":
        return Outcome("APPROVE_WITH_MONITORING", r + [f"Integrity category {integrity}; declared MCC risk {declared_risk}"], "merchant_review",
                       driver="action" if action == "approve_with_monitoring" else None)
    if action == "approve" and aconf >= 0.60:
        return Outcome("AUTO_APPROVE", r + [f"Low risk, approve confidence {aconf:.2f}"], "merchant_review", driver="action")
    return Outcome("UNDERWRITER_REVIEW", r + [f"Low confidence ({action} {aconf:.2f}) — underwriter to decide"], "merchant_review")


_SANCTIONS_KEYWORDS = ["cuba", "iran", "persian", "tehran", "syria", "aleppo", "north korea", "pyongyang", "crimea", "dnr", "lnr",
                       "nowruz"]


def _keyword_hits(payload: dict[str, Any]) -> list[str]:
    text = " ".join(str(v) for v in ((payload.get("payment") or {}).get("note"), payload.get("message")) if v).lower()
    return [k for k in _SANCTIONS_KEYWORDS if k in text]


def scam(payload: dict[str, Any], rec: DecisionRecord) -> Outcome:
    amount = float((payload.get("payment") or {}).get("amount_usd") or 0)
    sanctions = noul(rec, "sanctions_true_hit")
    typology, tconf = choice(rec, "scam_typology")
    ato = noul(rec, "ato_signals")
    coached = noul(rec, "coached_customer")
    vulnerable = noul(rec, "vulnerability_indicators")
    risk, risk_label = score(rec, "scam_risk")
    intervention, iconf = choice(rec, "intervention")
    scam_like = typology not in ("none", "cannot_tell", "sanctions_evasion", "account_takeover")

    r = [f"Typology {typology} ({tconf:.2f}); scam risk {risk_label}"]
    if sanctions >= 0.70:
        return Outcome("SANCTIONS_HOLD", r + [f"True sanctions hit p={sanctions:.2f} — no customer message (tipping-off risk)"])
    if ato >= 0.70:
        return Outcome("ACCOUNT_SECURITY_REVIEW", r + [f"Account takeover signals p={ato:.2f}"], "scam_or_account_risk")
    if (typology == "none" and tconf >= 0.80 and sanctions < 0.30 and ato < 0.30 and coached < 0.30 and risk < 1.5):
        hits = _keyword_hits(payload)
        return Outcome("ALLOW", r + ([f"Legacy keyword screen would have flagged {hits}; Microsoft-Decision-1 cleared it as benign "
                                      f"(sanctions p={sanctions:.3f})"] if hits else []))
    if intervention == "hold_and_callback" or (scam_like and (coached >= 0.60 or vulnerable >= 0.60 or amount >= 1000)):
        return Outcome("HUMAN_HOLD", r + [f"Coached p={coached:.2f}, vulnerable p={vulnerable:.2f}, amount ${amount:,.2f}"], "scam_or_account_risk")
    if intervention == "secure_account_review":
        return Outcome("ACCOUNT_SECURITY_REVIEW", r, "scam_or_account_risk")
    if scam_like or intervention == "in_app_warning" or risk >= 2.0:
        return Outcome("AUTO_WARNING", r + ["Specific in-app warning; the customer decides"], "scam_or_account_risk",
                       driver="intervention" if not scam_like and intervention == "in_app_warning" else None)
    if intervention == "allow" and iconf >= 0.60:
        return Outcome("ALLOW", r, driver="intervention")
    return Outcome("AUTO_WARNING", r + [f"Low confidence ({intervention} {iconf:.2f}) — show a warning"], "scam_or_account_risk")


def judge(lane: str, rec: DecisionRecord, spec: dict[str, Any]) -> tuple[bool, list[str]]:
    feedback = []
    for name in spec["must_be_true"]:
        if noul(rec, name) < 0.60:
            feedback.append(f"Fix: '{name.replace('_', ' ')}' was not satisfied (p={noul(rec, name):.2f}).")
    for name in spec["must_be_false"]:
        limit = 0.20 if name == "asks_for_secrets" else 0.40
        if noul(rec, name) > limit:
            feedback.append(f"Fix: remove '{name.replace('_', ' ')}' (p={noul(rec, name):.2f}).")
    value, label = score(rec, spec["score"])
    if value < 1.5:
        feedback.append(f"Improve {spec['score']}: rated {label} ({value:.2f}/3).")
    return not feedback, feedback
