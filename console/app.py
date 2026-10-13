"""Trust Console: demo UI and API for the Payments Trust Fabric.

Runs the hosted multi-agent workflow through Foundry, streams the decision-only Firehose, calibrates
Microsoft-Decision-1 on customer data and keeps a human review queue.
"""

import asyncio
import csv
import io
import json
import os
import random
import re
import statistics
import time
from pathlib import Path
from typing import Any

import httpx
from azure.identity import DefaultAzureCredential
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from trust_fabric.case import CaseState, dataset
from trust_fabric.config import load_settings
from trust_fabric.decision_engine import DecisionClient, DecisionError
from trust_fabric.policy import AUTOMATIC
from trust_fabric.stages import decide_only

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
AGENT_NAME = os.environ.get("AGENT_NAME", "decision-payments-agent")
AI_SCOPE = "https://ai.azure.com/.default"

app = FastAPI(title="Payments Trust Fabric — Trust Console")
settings = load_settings()
credential = DefaultAzureCredential()
decisions = DecisionClient(settings.decision_endpoint, settings.decision_deployment, credential,
                           rpm_per_deployment=settings.decision_rpm_per_deployment)
http = httpx.AsyncClient(timeout=180)
review_queue: dict[str, dict[str, Any]] = {}
_token: dict[str, Any] = {"value": None, "expires": 0.0}


async def _ai_token() -> str:
    if not _token["value"] or time.time() > _token["expires"] - 300:
        tok = await asyncio.to_thread(credential.get_token, AI_SCOPE)
        _token.update(value=tok.token, expires=float(tok.expires_on))
    return _token["value"]


def _load_json(name: str) -> Any:
    path = DATA / name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


# ------------------------------------------------------------------------------------------------ catalogue
@app.get("/api/config")
async def config() -> dict[str, Any]:
    return {"agent_name": AGENT_NAME, "project_endpoint": settings.project_endpoint, "decision_deployments": settings.decision_deployment,
            "rpm_per_deployment": settings.decision_rpm_per_deployment, "events": len(dataset()["events"]),
            "portal_url": "https://ai.azure.com"}


@app.get("/api/events")
async def events() -> list[dict[str, Any]]:
    return [{"id": e["id"], "title": e["title"], "lane": e["gold"]["lane"], "channel": e["channel"], "hero": e.get("hero"),
             "expected": e["gold"].get("disposition")} for e in dataset()["events"].values()]


@app.get("/api/events/{event_id}")
async def event(event_id: str) -> dict[str, Any]:
    e = dataset()["events"].get(event_id.upper())
    if not e:
        raise HTTPException(404, "Unknown event")
    return e


@app.get("/api/heroes")
async def heroes() -> list[dict[str, Any]]:
    return dataset()["heroes"]


@app.get("/api/baseline")
async def baseline() -> Any:
    return _load_json("firehose_baseline.json") or {}


@app.get("/api/benchmark")
async def benchmark() -> Any:
    return _load_json("benchmark.json") or {}


# ------------------------------------------------------------------------------------------------ hosted agent
class RunRequest(BaseModel):
    input: str


def _parse_agent_output(payload: dict[str, Any]) -> dict[str, Any]:
    text_parts, reasoning = [], []
    for item in payload.get("output", []) or []:
        if item.get("type") == "message":
            for c in item.get("content", []) or []:
                if c.get("type") in ("output_text", "text") and c.get("text"):
                    text_parts.append(c["text"])
        elif item.get("type") == "reasoning":
            for c in (item.get("content") or []) + (item.get("summary") or []):
                if c.get("text"):
                    reasoning.append(c["text"])
    markdown = "\n".join(text_parts) or payload.get("output_text", "")
    match = re.search(r"```json\n(.*?)\n```", markdown, re.S)
    record = json.loads(match.group(1)) if match else None
    readable = markdown.split("<details>")[0].strip()
    return {"markdown": readable, "record": record, "reasoning": reasoning}


@app.post("/api/agent/run")
async def run_agent(req: RunRequest) -> dict[str, Any]:
    url = f"{settings.project_endpoint}/agents/{AGENT_NAME}/endpoint/protocols/openai/responses?api-version=v1"
    started = time.perf_counter()
    resp = await http.post(url, headers={"Authorization": f"Bearer {await _ai_token()}"},
                           json={"model": settings.llm_fast, "input": req.input, "stream": False})
    elapsed = (time.perf_counter() - started) * 1000
    if resp.status_code >= 400:
        raise HTTPException(resp.status_code, f"Hosted agent error: {resp.text[:400]}")
    result = _parse_agent_output(resp.json())
    result["roundtrip_ms"] = round(elapsed, 1)
    rec = result.get("record")
    if rec and rec.get("disposition") and not rec.get("automatic"):
        review_queue[rec["case_id"]] = {"case_id": rec["case_id"], "title": rec["title"], "lane": rec.get("lane"),
                                        "disposition": rec["disposition"], "queue": rec.get("human_queue"), "reasons": rec.get("reasons", []),
                                        "draft": rec.get("draft"), "status": "pending", "received": time.strftime("%H:%M:%S")}
    return result


# ------------------------------------------------------------------------------------------------ firehose
class FirehoseRequest(BaseModel):
    count: int = 24
    lane: str | None = None
    seed: int | None = None


@app.post("/api/firehose")
async def firehose(req: FirehoseRequest) -> StreamingResponse:
    pool = [e for e in dataset()["events"].values() if not req.lane or e["gold"]["lane"] == req.lane]
    rng = random.Random(req.seed if req.seed is not None else time.time())
    picked = rng.sample(pool, min(max(1, req.count), len(pool)))

    async def stream():
        started = time.perf_counter()
        queue: asyncio.Queue = asyncio.Queue()

        async def one(e: dict[str, Any]) -> None:
            case = CaseState(case_id=e["id"], title=e["title"], channel=e["channel"], payload=e["payload"], gold=e["gold"])
            t0 = time.perf_counter()
            try:
                await decide_only(case, decisions)
            except DecisionError:
                case.disposition = "HUMAN_TRIAGE"
            acceptable = e["gold"].get("acceptable_dispositions") or [e["gold"]["disposition"]]
            await queue.put({"type": "row", "id": e["id"], "title": e["title"], "lane": e["gold"]["lane"], "disposition": case.disposition,
                             "expected": acceptable, "match": case.disposition in acceptable, "automatic": case.disposition in AUTOMATIC,
                             "decisions": case.ledger.decision_count, "calls": len(case.ledger.decisions),
                             "critical_path_ms": round(case.ledger.decision_wall_ms(), 1), "cost_usd": case.ledger.decision_cost_usd,
                             "latencies": [r.latency_ms for r in case.ledger.decisions], "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
                             "reason": (case.reasons or [""])[0]})

        tasks = [asyncio.create_task(one(e)) for e in picked]
        yield f"data: {json.dumps({'type': 'start', 'total': len(picked)})}\n\n"
        rows = []
        for _ in picked:
            row = await queue.get()
            rows.append(row)
            yield f"data: {json.dumps(row)}\n\n"
        await asyncio.gather(*tasks)
        lat = sorted(x for r in rows for x in r["latencies"])
        summary = {"type": "summary", "events": len(rows), "wall_s": round(time.perf_counter() - started, 2),
                   "decisions": sum(r["decisions"] for r in rows), "calls": sum(r["calls"] for r in rows),
                   "cost_usd": sum(r["cost_usd"] for r in rows), "accuracy": sum(r["match"] for r in rows) / len(rows),
                   "automation_rate": sum(r["automatic"] for r in rows) / len(rows),
                   "p50_ms": statistics.median(lat) if lat else 0, "p95_ms": lat[int(0.95 * (len(lat) - 1))] if lat else 0}
        yield f"data: {json.dumps(summary)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ------------------------------------------------------------------------------------------------ calibrate
@app.get("/api/calibrate/sample", response_class=PlainTextResponse)
async def calibrate_sample() -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["text", "label"])
    for e in dataset()["events"].values():
        if e["id"].startswith("DP-"):
            w.writerow([e["payload"]["message"], e["gold"]["reason_category"]])
    return out.getvalue()


def _calibration_metrics(rows: list[dict[str, Any]], labels: list[str]) -> dict[str, Any]:
    n = len(rows)
    correct = [r["pred"] == r["label"] for r in rows]
    bins = []
    for b in range(10):
        lo, hi = b / 10, (b + 1) / 10
        sel = [r for r in rows if lo <= r["conf"] < hi or (b == 9 and r["conf"] == 1.0)]
        if sel:
            bins.append({"lo": lo, "hi": hi, "n": len(sel), "avg_conf": statistics.mean(r["conf"] for r in sel),
                         "accuracy": sum(r["pred"] == r["label"] for r in sel) / len(sel)})
    ece = sum(b["n"] / n * abs(b["avg_conf"] - b["accuracy"]) for b in bins)
    curve = []
    for t in [x / 20 for x in range(0, 20)]:
        sel = [r for r in rows if r["conf"] >= t]
        if sel:
            curve.append({"threshold": t, "coverage": len(sel) / n, "accuracy": sum(r["pred"] == r["label"] for r in sel) / len(sel)})

    def best_coverage(target: float) -> dict[str, Any] | None:
        ok = [c for c in curve if c["accuracy"] >= target]
        return max(ok, key=lambda c: c["coverage"]) if ok else None

    confusion = {a: {p: 0 for p in labels} for a in labels}
    for r in rows:
        if r["label"] in confusion and r["pred"] in confusion[r["label"]]:
            confusion[r["label"]][r["pred"]] += 1
    return {"n": n, "accuracy": sum(correct) / n, "ece": ece, "reliability": bins, "curve": curve,
            "at_95": best_coverage(0.95), "at_99": best_coverage(0.99), "labels": labels, "confusion": confusion}


@app.post("/api/calibrate")
async def calibrate(file: UploadFile = File(...), instructions: str = Form("Which label best describes this text?"),
                    max_rows: int = Form(60), shuffle_check: int = Form(12)) -> dict[str, Any]:
    raw = (await file.read()).decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(raw))
    if not reader.fieldnames or "text" not in reader.fieldnames or "label" not in reader.fieldnames:
        raise HTTPException(400, "CSV must have 'text' and 'label' columns")
    data = [r for r in reader if (r.get("text") or "").strip() and (r.get("label") or "").strip()][: max(1, min(max_rows, 200))]
    labels = sorted({r["label"].strip() for r in data})
    if len(labels) < 2:
        raise HTTPException(400, "Need at least two distinct labels")
    criteria = {lbl: lbl.replace("_", " ") for lbl in labels}
    started = time.perf_counter()

    async def ask(text: str, crit: dict[str, str]) -> tuple[str, float, float]:
        answers, _, ms = await decisions.decide_raw(text, {"label": {"type": "choice", "instructions": instructions, "criteria": crit}})
        a = answers["label"]
        return str(a.get("choice")), float(a.get("confidence", 0.0)), ms

    results = await asyncio.gather(*[ask(r["text"], criteria) for r in data])
    rows = [{"text": r["text"][:160], "label": r["label"].strip(), "pred": p, "conf": c, "ms": ms} for r, (p, c, ms) in zip(data, results)]
    metrics = _calibration_metrics(rows, labels)
    flips = None
    if shuffle_check:
        sample = data[: min(shuffle_check, len(data))]
        reversed_criteria = dict(reversed(list(criteria.items())))
        again = await asyncio.gather(*[ask(r["text"], reversed_criteria) for r in sample])
        flips = sum(1 for (p1, _, _), (p2, _, _) in zip(results[: len(sample)], again) if p1 != p2)
        metrics["shuffle"] = {"checked": len(sample), "flips": flips, "flip_rate": flips / len(sample)}
    lat = sorted(r["ms"] for r in rows)
    metrics.update({"p50_ms": statistics.median(lat), "wall_s": round(time.perf_counter() - started, 2),
                    "rows": rows[:200], "instructions": instructions})
    return metrics


# ------------------------------------------------------------------------------------------------ review queue
class ReviewAction(BaseModel):
    action: str
    note: str = ""


@app.get("/api/review")
async def review_list() -> list[dict[str, Any]]:
    return sorted(review_queue.values(), key=lambda r: r["received"], reverse=True)


@app.post("/api/review/{case_id}")
async def review_act(case_id: str, act: ReviewAction) -> dict[str, Any]:
    item = review_queue.get(case_id)
    if not item:
        raise HTTPException(404, "Unknown case")
    item.update(status={"approve": "approved", "reject": "rejected"}.get(act.action, act.action), note=act.note,
                decided=time.strftime("%H:%M:%S"))
    return item


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(HERE / "static" / "index.html")
