"""Evaluate the Microsoft-Decision-1 pipeline (no LLM drafting) on all labelled events.

Usage:  python tools/evaluate.py [--concurrency 16] [--out console/data/firehose_baseline.json]
"""

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT_DIR = ROOT / "src" / "payments-trust-fabric"
sys.path.insert(0, str(AGENT_DIR))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(AGENT_DIR / ".env")

from azure.identity import DefaultAzureCredential  # noqa: E402

from trust_fabric.case import CaseState, dataset  # noqa: E402
from trust_fabric.config import load_settings  # noqa: E402
from trust_fabric.decision_engine import DecisionClient  # noqa: E402
from trust_fabric.stages import decide_only  # noqa: E402


def _answers(case: CaseState, stage: str) -> dict:
    rec = next((r for r in case.ledger.decisions if r.stage == stage), None)
    return rec.answers if rec else {}


def _p(answers: dict, name: str) -> float | None:
    return answers.get(name, {}).get("p")


def _c(answers: dict, name: str) -> str | None:
    return answers.get(name, {}).get("choice")


def decision_checks(case: CaseState, gold: dict) -> dict[str, bool]:
    checks: dict[str, bool] = {}
    shield = _answers(case, "shield")
    if shield:
        checks["shield.prompt_injection"] = (_p(shield, "prompt_injection") >= 0.7) == bool(gold.get("prompt_injection", False))
    dispatch = _answers(case, "dispatch")
    if dispatch:
        checks["dispatch.lane"] = _c(dispatch, "lane") == gold["lane"]
    if gold.get("prompt_injection"):
        return checks
    lane = gold["lane"]
    if lane == "agent_payment" and (a := _answers(case, "checkout")):
        checks["checkout.mandate_match"] = (_p(a, "mandate_match") >= 0.5) == gold["mandate_match"]
        checks["checkout.manipulation"] = (_p(a, "manipulation_detected") >= 0.5) == gold["manipulation"]
    if lane == "dispute" and (a := _answers(case, "dispute")):
        checks["dispute.reason_category"] = _c(a, "reason_category") == gold["reason_category"]
        checks["dispute.billing_error_notice"] = (_p(a, "billing_error_notice") >= 0.5) == gold["billing_error_notice"]
    if lane == "merchant_review" and (a := _answers(case, "merchant")):
        checks["merchant.observed_mcc"] = (_c(a, "observed_mcc_family") or "").split("_")[0] == gold["observed_mcc"]
        checks["merchant.mcc_mismatch"] = (_p(a, "declared_mcc_mismatch") >= 0.5) == gold["declared_mcc_mismatch"]
        checks["merchant.prohibited"] = (_p(a, "prohibited_goods") >= 0.5) == gold["prohibited_goods"]
        checks["merchant.deceptive_billing"] = (_p(a, "deceptive_billing") >= 0.5) == gold["deceptive_billing"]
        checks["merchant.integrity_category"] = _c(a, "integrity_risk_category") == gold["integrity_risk_category"]
    if lane == "scam_or_account_risk" and (a := _answers(case, "scam")):
        checks["scam.sanctions_true_hit"] = (_p(a, "sanctions_true_hit") >= 0.5) == gold["sanctions_true_hit"]
        typ = gold["scam_typology"]
        checks["scam.typology"] = _c(a, "scam_typology") in (typ if isinstance(typ, list) else [typ])
        checks["scam.ato"] = (_p(a, "ato_signals") >= 0.5) == gold["ato_signals"]
    return checks


async def evaluate(concurrency: int, only: str = "") -> dict:
    settings = load_settings()
    client = DecisionClient(settings.decision_endpoint, settings.decision_deployment, DefaultAzureCredential(),
                            rpm_per_deployment=settings.decision_rpm_per_deployment)
    events = [e for e in dataset()["events"].values() if e["id"].startswith(only)]
    sem = asyncio.Semaphore(concurrency)
    await client.decide_raw("warm-up", {"ok": {"type": "noul", "instructions": "Is this a warm-up request?"}})

    async def one(event: dict) -> dict:
        async with sem:
            case = CaseState(case_id=event["id"], title=event["title"], channel=event["channel"], payload=event["payload"], gold=event["gold"])
            started = time.perf_counter()
            await decide_only(case, client)
            elapsed = (time.perf_counter() - started) * 1000
        gold = event["gold"]
        acceptable = gold.get("acceptable_dispositions") or [gold["disposition"]]
        return {
            "id": event["id"], "title": event["title"], "gold_lane": gold["lane"], "lane": case.lane, "disposition": case.disposition,
            "expected": acceptable, "match": case.disposition in acceptable, "checks": decision_checks(case, gold),
            "decisions": case.ledger.decision_count, "calls": len(case.ledger.decisions), "tokens": case.ledger.decision_tokens,
            "cost_usd": case.ledger.decision_cost_usd, "critical_path_ms": case.ledger.decision_wall_ms(), "elapsed_ms": elapsed,
            "call_latencies": [r.latency_ms for r in case.ledger.decisions], "reasons": case.reasons[:4],
        }

    started = time.perf_counter()
    rows = await asyncio.gather(*[one(e) for e in events])
    wall_s = time.perf_counter() - started
    await client.aclose()

    lat = sorted(x for r in rows for x in r["call_latencies"])
    per_check: dict[str, list[bool]] = {}
    for r in rows:
        for k, v in r["checks"].items():
            per_check.setdefault(k, []).append(v)
    per_lane: dict[str, list[bool]] = {}
    for r in rows:
        per_lane.setdefault(r["gold_lane"], []).append(r["match"])
    return {
        "events": len(rows),
        "disposition_accuracy": sum(r["match"] for r in rows) / len(rows),
        "per_lane_disposition_accuracy": {k: round(sum(v) / len(v), 3) for k, v in per_lane.items()},
        "decision_accuracy": {k: {"accuracy": round(sum(v) / len(v), 3), "n": len(v)} for k, v in sorted(per_check.items())},
        "total_decisions": sum(r["decisions"] for r in rows),
        "total_calls": sum(r["calls"] for r in rows),
        "total_input_tokens": sum(r["tokens"] for r in rows),
        "total_cost_usd": sum(r["cost_usd"] for r in rows),
        "call_latency_ms": {"p50": statistics.median(lat), "p95": lat[int(0.95 * (len(lat) - 1))], "max": lat[-1]},
        "event_critical_path_ms_p50": statistics.median(r["critical_path_ms"] for r in rows),
        "wall_clock_s": round(wall_s, 2),
        "events_per_second": round(len(rows) / wall_s, 2),
        "automation_rate": sum(1 for r in rows if r["disposition"] in {"AUTO_APPROVE", "AUTO_CREDIT", "AUTO_REQUEST_INFO", "APPROVE_WITH_MONITORING",
                                                                       "ALLOW", "AUTO_WARNING", "AUTO_REPLY"}) / len(rows),
        "concurrency": concurrency,
        "rows": rows,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--concurrency", type=int, default=16)
    ap.add_argument("--only", default="", help="Only evaluate event ids starting with this prefix, e.g. DP-")
    ap.add_argument("--out", default=str(ROOT / "console" / "data" / "firehose_baseline.json"))
    args = ap.parse_args()
    result = asyncio.run(evaluate(args.concurrency, args.only))
    if not args.only:
        Path(args.out).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"Events: {result['events']}  disposition accuracy: {result['disposition_accuracy']:.1%}  automation rate: {result['automation_rate']:.1%}")
    print("Per lane:", result["per_lane_disposition_accuracy"])
    for k, v in result["decision_accuracy"].items():
        print(f"  {k:32s} {v['accuracy']:.1%}  (n={v['n']})")
    print(f"Decisions: {result['total_decisions']} in {result['total_calls']} calls · cost ${result['total_cost_usd']:.4f} · "
          f"call p50 {result['call_latency_ms']['p50']:.0f} ms p95 {result['call_latency_ms']['p95']:.0f} ms · "
          f"{result['events_per_second']} events/s over {result['wall_clock_s']} s")
    print("Mismatches:")
    for r in result["rows"]:
        if not r["match"]:
            print(f"  {r['id']:6s} got {r['disposition']:24s} expected {'/'.join(r['expected']):40s} {r['title'][:60]}")


if __name__ == "__main__":
    main()
