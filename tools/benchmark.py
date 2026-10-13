"""Benchmark Microsoft-Decision-1 against LLMs on the same labelled decisions.

Usage:  python tools/benchmark.py [--models gpt-5.4-mini,gpt-6-sol] [--out console/data/benchmark.json]

Every model answers the same production question definitions (instructions + options) one decision per call.
LLMs return JSON through chat completions; Microsoft-Decision-1 uses the decision API.
"""

import argparse
import asyncio
import json
import random
import statistics
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
AGENT_DIR = ROOT / "src" / "payments-trust-fabric"
sys.path.insert(0, str(AGENT_DIR))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(AGENT_DIR / ".env")

from azure.identity import AzureCliCredential  # noqa: E402

from trust_fabric import questions  # noqa: E402
from trust_fabric.case import dataset, shield_view  # noqa: E402
from trust_fabric.config import DECISION_PRICE_PER_M_INPUT, load_settings  # noqa: E402
from trust_fabric.decision_engine import DecisionClient  # noqa: E402

# Assumed list prices (USD per 1M tokens, Global Standard). Third-party estimates, Oct 2026 — confirm in the Azure pricing calculator.
LLM_PRICES = {"gpt-5.4-mini": (0.75, 4.50), "gpt-5.6-luna": (0.20, 1.20), "gpt-6-sol": (2.00, 10.00)}


def build_tasks() -> list[dict]:
    events = list(dataset()["events"].values())
    rng = random.Random(42)
    tasks = []
    for e in events:
        g, p = e["gold"], e["payload"]
        if e["id"].startswith("DP-"):
            tasks.append({"task": "dispute_reason", "id": e["id"], "q": "reason_category", "def": questions.DISPUTE_TRIAGE["reason_category"],
                          "state": {"cardholder_message": p["message"], "transaction": p["transaction"]}, "gold": g["reason_category"]})
        if e["id"].startswith("SC-"):
            tasks.append({"task": "sanctions_true_hit", "id": e["id"], "q": "sanctions_true_hit", "def": questions.SCAM["sanctions_true_hit"],
                          "state": {"payment": p["payment"], "context": p.get("context")}, "gold": g["sanctions_true_hit"]})
        if e["id"].startswith("AP-"):
            tasks.append({"task": "mandate_match", "id": e["id"], "q": "mandate_match", "def": questions.CHECKOUT["mandate_match"],
                          "state": {"intent_mandate": p["intent_mandate"], "cart": p["cart"]}, "gold": g["mandate_match"]})
    adversarial = [e for e in events if e["id"].startswith("AD-")]
    benign = rng.sample([e for e in events if not e["id"].startswith("AD-")], 15)
    for e in adversarial + benign:
        tasks.append({"task": "prompt_injection", "id": e["id"], "q": "prompt_injection", "def": questions.SHIELD["prompt_injection"],
                      "state": shield_view(e["payload"]), "gold": e["gold"].get("prompt_injection", False)})
    return tasks


def reordered(defn: dict) -> dict:
    if isinstance(defn.get("criteria"), dict) and defn["type"] == "choice":
        return {**defn, "criteria": dict(reversed(list(defn["criteria"].items())))}
    return defn


def llm_prompt(task: dict, defn: dict) -> str:
    if defn["type"] == "choice":
        options = "\n".join(f"- {k}: {v}" for k, v in defn["criteria"].items())
        answer = 'Reply with JSON {"answer": "<one option key>"}.'
    else:
        crit = defn.get("criteria") or {}
        options = "\n".join(f"- {k}: {v}" for k, v in crit.items()) if crit else ""
        answer = 'Reply with JSON {"answer": true} or {"answer": false}.'
    return (f"Question: {defn['instructions']}\n" + (f"Options:\n{options}\n" if options else "") +
            f"Input (JSON):\n{json.dumps(task['state'], ensure_ascii=False)}\n{answer}")


class LLM:
    _token_lock = asyncio.Lock()
    _shared: dict = {"token": None, "expires": 0.0}

    def __init__(self, endpoint: str, credential, model: str) -> None:
        self.url = f"{endpoint.rstrip('/')}/openai/v1/chat/completions"
        self.model, self.credential = model, credential
        self.http = httpx.AsyncClient(timeout=120)
        self.reasoning = True
        self.blocked = 0

    async def _bearer(self) -> str:
        async with LLM._token_lock:
            if not LLM._shared["token"] or time.time() > LLM._shared["expires"] - 300:
                tok = await asyncio.to_thread(self.credential.get_token, "https://cognitiveservices.azure.com/.default")
                LLM._shared.update(token=tok.token, expires=float(tok.expires_on))
            return LLM._shared["token"]

    async def ask(self, prompt: str) -> tuple[object, float, int, int]:
        token = await self._bearer()
        body = {"model": self.model, "messages": [{"role": "user", "content": prompt}], "response_format": {"type": "json_object"}}
        if self.reasoning:
            body["reasoning_effort"] = "low"
        for attempt in range(6):
            started = time.perf_counter()
            r = await self.http.post(self.url, headers={"Authorization": f"Bearer {token}"}, json=body)
            ms = (time.perf_counter() - started) * 1000
            if r.status_code == 400:
                text = r.text.lower()
                if "content_filter" in text or "responsibleaipolicyviolation" in text or "jailbreak" in text:
                    self.blocked += 1
                    return None, ms, 0, 0
                if "reasoning" in text and "reasoning_effort" in body:
                    body.pop("reasoning_effort", None)
                    self.reasoning = False
                    continue
                if "response_format" in text and "response_format" in body:
                    body.pop("response_format", None)
                    continue
                raise RuntimeError(f"{self.model} HTTP 400: {r.text[:300]}")
            if r.status_code in (429, 500, 502, 503) and attempt < 5:
                await asyncio.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            j = r.json()
            try:
                content = j["choices"][0]["message"]["content"]
                answer = json.loads(content[content.find("{"): content.rfind("}") + 1]).get("answer")
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                answer = None
            u = j.get("usage") or {}
            return answer, ms, int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0))
        raise RuntimeError("LLM retries exhausted")


async def run(models: list[str], out: Path, concurrency: int) -> dict:
    settings = load_settings()
    cred = AzureCliCredential(process_timeout=90)
    for scope in ("https://cognitiveservices.azure.com/.default", "https://ai.azure.com/.default"):
        cred.get_token(scope)  # warm the token cache once, before any concurrency
    d1 = DecisionClient(settings.decision_endpoint, settings.decision_deployment, cred, rpm_per_deployment=settings.decision_rpm_per_deployment)
    account = "https://" + settings.project_endpoint.split("//")[1].split(".")[0] + ".openai.azure.com"
    tasks = build_tasks()
    flip_tasks = [t for t in tasks if t["def"]["type"] == "choice"]
    sem = asyncio.Semaphore(concurrency)

    async def d1_one(t: dict, defn: dict) -> tuple[object, float, int, int]:
        async with sem:
            answers, tokens, ms = await d1.decide_raw(t["state"], {t["q"]: defn})
        a = answers[t["q"]]
        value = a.get("choice") if a.get("type") == "choice" else (a.get("noul", 0) >= 0.5)
        return value, ms, tokens, 0

    results = []
    runners = {"Microsoft-Decision-1": lambda t, defn: d1_one(t, defn)}
    llms: dict[str, LLM] = {}
    for m in models:
        llm = LLM(account, cred, m)
        llms[m] = llm

        async def llm_one(t: dict, defn: dict, llm=llm) -> tuple[object, float, int, int]:
            async with sem:
                return await llm.ask(llm_prompt(t, defn))
        runners[m] = llm_one

    for name, fn in runners.items():
        started = time.perf_counter()
        outs = await asyncio.gather(*[fn(t, t["def"]) for t in tasks])
        flips_out = await asyncio.gather(*[fn(t, reordered(t["def"])) for t in flip_tasks])
        wall = time.perf_counter() - started
        correct = [o[0] == t["gold"] for o, t in zip(outs, tasks)]
        first = {t["id"] + t["task"]: o[0] for o, t in zip(outs, tasks)}
        flips = sum(1 for o, t in zip(flips_out, flip_tasks) if o[0] != first[t["id"] + t["task"]])
        lat = sorted(o[1] for o in outs)
        tin, tout = sum(o[2] for o in outs), sum(o[3] for o in outs)
        if name == "Microsoft-Decision-1":
            cost = tin * DECISION_PRICE_PER_M_INPUT / 1e6
        else:
            pin, pout = LLM_PRICES.get(name, (0.0, 0.0))
            cost = tin * pin / 1e6 + tout * pout / 1e6
        per_task = {}
        for task_name in sorted({t["task"] for t in tasks}):
            idx = [i for i, t in enumerate(tasks) if t["task"] == task_name]
            per_task[task_name] = {"accuracy": sum(correct[i] for i in idx) / len(idx), "n": len(idx)}
        results.append({"name": name, "accuracy": sum(correct) / len(correct), "p50_ms": statistics.median(lat),
                        "p95_ms": lat[int(0.95 * (len(lat) - 1))], "cost_per_1k_usd": cost / len(tasks) * 1000,
                        "input_tokens_per_decision": tin / len(tasks), "output_tokens_per_decision": tout / len(tasks),
                        "flip_rate": flips / len(flip_tasks), "flip_checked": len(flip_tasks), "calibrated": name == "Microsoft-Decision-1",
                        "blocked_by_content_filter": llms[name].blocked if name in llms else 0,
                        "per_task": per_task, "wall_s": round(wall, 1)})
        print(f"{name:22s} acc {results[-1]['accuracy']:.1%}  p50 {results[-1]['p50_ms']:.0f} ms  p95 {results[-1]['p95_ms']:.0f} ms  "
              f"${results[-1]['cost_per_1k_usd']:.4f}/1K  flips {flips}/{len(flip_tasks)}  blocked {results[-1]['blocked_by_content_filter']}")
    await d1.aclose()
    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M"),
        "description": f"{len(tasks)} labeled payments decisions (dispute reason codes, true versus benign sanctions hits, agent mandate "
                       f"match, prompt injection), one decision per call, identical instructions and options for each model. Flip rate re-asks "
                       f"the {len(flip_tasks)} multi-option decisions with the option order reversed.",
        "notes": "Latency measured from the demo client to East US 2 and includes network time. LLMs ran with JSON output and low reasoning "
                 "effort where supported. Costs use Azure list prices for Global Standard deployments (October 2026): Microsoft-Decision-1 "
                 "at $0.042 per 1M tokens, and each LLM at its input and output token prices. Microsoft-Decision-1 returns a calibrated "
                 "probability for each option; the LLMs returned one answer. LLM requests rejected by the deployment's content filter "
                 "(adversarial inputs) count as no decision.",
        "tasks": sorted({t["task"] for t in tasks}),
        "models": results,
    }
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="gpt-5.4-mini,gpt-6-sol")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--out", default=str(ROOT / "console" / "data" / "benchmark.json"))
    args = ap.parse_args()
    asyncio.run(run([m.strip() for m in args.models.split(",") if m.strip()], Path(args.out), args.concurrency))


if __name__ == "__main__":
    main()
