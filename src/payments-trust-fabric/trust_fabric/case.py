"""Case state passed between workflow executors, input parsing and Microsoft-Decision-1 state views."""

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from .decision_engine import Ledger

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_EVENT_ID = re.compile(r"\b(AP|DP|MR|SC|GI|AD)-\d{2}\b", re.IGNORECASE)


@lru_cache(maxsize=1)
def dataset() -> dict[str, Any]:
    events = [json.loads(line) for line in (DATA_DIR / "events.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    return {
        "events": {e["id"]: e for e in events},
        "reference": json.loads((DATA_DIR / "reference.json").read_text(encoding="utf-8")),
        "heroes": json.loads((DATA_DIR / "heroes.json").read_text(encoding="utf-8")),
    }


def reference() -> dict[str, Any]:
    return dataset()["reference"]


@dataclass
class CaseState:
    case_id: str
    title: str
    channel: str
    payload: dict[str, Any]
    gold: dict[str, Any] | None = None
    mode: str = "process"
    lane: str | None = None
    lane_confidence: float = 0.0
    urgency: str = ""
    shield_flags: dict[str, float] = field(default_factory=dict)
    quarantined: bool = False
    findings: dict[str, Any] = field(default_factory=dict)
    disposition: str | None = None
    reasons: list[str] = field(default_factory=list)
    human_queue: str | None = None
    next_step: str = "finalize"
    draft: dict[str, Any] | None = None
    draft_history: list[dict[str, Any]] = field(default_factory=list)
    judge_feedback: list[str] = field(default_factory=list)
    revisions: int = 0
    llm_tier: str | None = None
    ledger: Ledger = field(default_factory=Ledger)
    timeline: list[dict[str, Any]] = field(default_factory=list)
    started: float = field(default_factory=time.perf_counter)

    def log(self, stage: str, agent: str, kind: str, summary: str, ms: float | None = None) -> str:
        self.timeline.append({"stage": stage, "agent": agent, "kind": kind, "summary": summary, "ms": None if ms is None else round(ms, 1)})
        icon = {"decision": "⚡", "llm": "✍️", "policy": "⚖️", "info": "📥"}.get(kind, "•")
        timing = f" · {ms:.0f} ms" if ms is not None else ""
        return f"{icon} {agent}: {summary}{timing}"


def _from_event(event: dict[str, Any]) -> CaseState:
    return CaseState(case_id=event["id"], title=event.get("title", event["id"]), channel=event.get("channel", "api"),
                     payload=event["payload"], gold=event.get("gold"))


def case_from_text(text: str) -> CaseState:
    """Build a case from a playground message: an event id, a hero name, a JSON event or free text."""
    data = dataset()
    t = (text or "").strip()
    if not t or t.lower() in {"help", "list", "menu", "scenarios", "?", "hi", "hello"}:
        return CaseState(case_id="HELP", title="Help", channel="chat", payload={}, mode="help")
    if t.startswith("{"):
        try:
            obj = json.loads(t)
            if "payload" in obj:
                obj.setdefault("id", "JSON-" + uuid.uuid4().hex[:6].upper())
                return _from_event(obj)
            return CaseState(case_id="JSON-" + uuid.uuid4().hex[:6].upper(), title="Submitted event", channel="api", payload=obj)
        except json.JSONDecodeError:
            pass
    match = _EVENT_ID.search(t)
    if match and match.group(0).upper() in data["events"]:
        return _from_event(data["events"][match.group(0).upper()])
    lowered = t.lower()
    for hero in data["heroes"]:
        if hero["hero"] in lowered:
            return _from_event(data["events"][hero["id"]])
    return CaseState(case_id="CHAT-" + uuid.uuid4().hex[:6].upper(), title="Inbound customer message", channel="chat",
                     payload={"message": t})


def help_text() -> str:
    data = dataset()
    lines = [
        "## Payments Trust Fabric — powered by Microsoft-Decision-1",
        "Send an **event id** (for example `AP-02`), a **hero scenario name**, a JSON event, or any customer message.",
        "",
        "| Hero scenario | Event | What it shows |",
        "|---|---|---|",
    ]
    for h in data["heroes"]:
        lines.append(f"| `{h['hero']}` | `{h['id']}` | {h['story']} |")
    lines += ["", f"The bundled dataset has {len(data['events'])} labelled events: `AP-01..30` agentic checkout, `DP-01..30` disputes, "
              "`MR-01..25` merchant reviews, `SC-01..25` scam / sanctions / account takeover, `GI-01..05` general questions, "
              "`AD-01..05` adversarial prompt injections."]
    return "\n".join(lines)


# ------------------------------------------------------------------------------------- Microsoft-Decision-1 state views
def shield_view(payload: dict[str, Any]) -> dict[str, Any]:
    """Only untrusted free text. The agent browsing trace is judged separately by the Mandate Guardian."""
    view: dict[str, Any] = {}
    for key in ("message", "support_chat", "website_snapshot", "acquirer_notes"):
        if payload.get(key):
            view[key] = payload[key]
    if (payload.get("payment") or {}).get("note"):
        view["payment_note"] = payload["payment"]["note"]
    if (payload.get("intent_mandate") or {}).get("text"):
        view["intent_mandate"] = payload["intent_mandate"]["text"]
    items = ((payload.get("cart") or {}).get("items")) or []
    if items:
        view["cart_item_descriptions"] = [i.get("description") for i in items]
    return view or {"content": json.dumps(payload)[:4000]}


def dispatch_view(payload: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in payload.items() if k not in ("event_kind",)}


def consumer(payload: dict[str, Any]) -> dict[str, Any]:
    return reference()["consumers"].get(payload.get("consumer_id", ""), {})
