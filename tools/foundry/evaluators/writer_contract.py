import json
import re

# Per-role delivery contract: required fields and word caps (from the writer skills in .agent_configs/).
CONTRACTS = {
    "consumer_notification": ({"title", "body", "approve_label", "decline_label"}, {"body": 70}),
    "dispute_letter": ({"customer_message", "analyst_summary"}, {"customer_message": 140, "analyst_summary": 120}),
    "underwriting_memo": ({"memo"}, {"memo": 160}),  # conditions are optional ("where relevant")
    "protection_message": ({"in_app_message", "callback_script"}, {"in_app_message": 90, "callback_script": 90}),
    "general_answer": ({"answer"}, {"answer": 100}),
}
# Account-status claims are only true for ACCOUNT_SECURITY_REVIEW; a HUMAN_HOLD pauses one payment, not the account.
ACCOUNT_CLAIM = (r"regain access|(?:secured|locked|froze|frozen|restricted|suspended) (?:your|the) account|"
                 r"account (?:has been |is |was |is now |has now been )?(?:secured|locked|frozen|restricted|suspended)")


def _response(sample, item):
    for source in (sample, item):
        if isinstance(source, dict):
            for key in ("output_text", "sample.output_text", "response"):
                value = source.get(key)
                if value:
                    return value if isinstance(value, str) else json.dumps(value)
    return ""


def _record(response):
    match = re.search(r"```json\s*(\{.*?\})\s*```", response, re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except ValueError:
        return None


def _words(text):
    return len(re.findall(r"[^\s]+", str(text)))


def grade(sample, item) -> float:
    """Share of the writer's delivery contract that holds: required fields present, every capped field within its word
    limit, and (protection messages) no account-status claim unless the account really was secured. An AUTO_WARNING
    needs no callback script because nobody calls. Cases without a draft (no writer involved) score 1.0."""
    item = item if isinstance(item, dict) else {}
    record = _record(_response(sample, item))
    if record is None:
        return 0.0
    draft = record.get("draft") or {}
    if not isinstance(draft, dict) or not draft:
        return 1.0
    keys = set(draft)
    role = next((name for name, (required, _) in CONTRACTS.items() if required & keys), None)
    if role is None:
        return 0.0
    required, caps = CONTRACTS[role]
    if role == "protection_message" and record.get("disposition") == "AUTO_WARNING":
        required = required - {"callback_script"}
    checks = [field in keys and bool(str(draft.get(field) or "").strip()) for field in required]
    checks += [_words(draft.get(field, "")) <= cap for field, cap in caps.items()]
    if role == "protection_message" and record.get("disposition") != "ACCOUNT_SECURITY_REVIEW":
        text = " ".join(str(draft.get(f) or "") for f in ("in_app_message", "callback_script")).lower()
        checks.append(re.search(ACCOUNT_CLAIM, text) is None)
    return round(sum(1.0 for ok in checks if ok) / len(checks), 4)
