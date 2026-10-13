"""Build the Foundry evaluation golden dataset (evals/golden.jsonl) from the labelled events.

Usage:  python tools/build_golden.py
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVENTS = ROOT / "src" / "payments-trust-fabric" / "data" / "events.jsonl"
OUT = ROOT / "evals" / "golden.jsonl"

AUTOMATIC = {"AUTO_APPROVE", "AUTO_CREDIT", "AUTO_REQUEST_INFO", "APPROVE_WITH_MONITORING", "ALLOW", "AUTO_WARNING", "AUTO_REPLY"}
MEANING = {
    "AUTO_APPROVE": "approve automatically because everything matches and no risk signals fire",
    "AUTO_CREDIT": "credit the cardholder automatically (clear, low-value error in the customer's favour)",
    "AUTO_REQUEST_INFO": "send the customer a non-adverse request for the missing information",
    "APPROVE_WITH_MONITORING": "approve the merchant with registration and ongoing monitoring",
    "ALLOW": "allow the payment without friction",
    "AUTO_WARNING": "show a specific in-app scam warning and let the customer decide",
    "AUTO_REPLY": "answer the general question directly",
    "CONSUMER_STEP_UP": "pause and ask the consumer to confirm the agent's purchase",
    "RISK_REVIEW": "hold the agent purchase for the agentic-commerce risk desk",
    "ANALYST_REVIEW": "route to a disputes analyst with a draft; never deny automatically",
    "UNDERWRITER_REVIEW": "send the merchant to an underwriter for enhanced due diligence",
    "COMPLIANCE_REFERRAL": "refer the merchant to compliance for prohibited goods",
    "HUMAN_HOLD": "hold the payment and call the customer back on the number on file",
    "SANCTIONS_HOLD": "hold for the sanctions team and send no customer message (tipping-off risk)",
    "ACCOUNT_SECURITY_REVIEW": "secure the account and review possible account takeover",
    "QUARANTINED": "quarantine the event as a prompt-injection attempt; no LLM agent may see it",
    "HUMAN_TRIAGE": "route to human triage",
}


def main() -> None:
    rows = []
    for line in EVENTS.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        e = json.loads(line)
        gold = e["gold"]
        accepted = gold.get("acceptable_dispositions") or [gold["disposition"]]
        behaviour = (f"Processes event {e['id']} ({e['title']}). The final disposition must be {' or '.join(accepted)}: "
                     f"{MEANING.get(gold['disposition'], '')}. The response lists the decision trail and the reasons. "
                     + ("No human is needed." if gold["disposition"] in AUTOMATIC else "A human stays in the loop for the final action.")
                     + " Any customer-facing draft is factual, non-blaming and never asks for passwords, codes or card numbers.")
        rows.append({"query": e["id"], "expected_behavior": behaviour, "ground_truth": " or ".join(accepted),
                     "context": f"{gold['lane']}: {e['title']}",
                     "metadata": {"lane": gold["lane"], "hero": e.get("hero"), "source": "synthetic-labelled"}})
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    print(f"Wrote {len(rows)} rows to {OUT}")


if __name__ == "__main__":
    main()
