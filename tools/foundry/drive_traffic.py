"""Send the golden events to the deployed hosted agent and save every response.

The traffic produces the production-style traces that Foundry Insights analyses, and the saved responses can be scored
offline (tools/foundry/score_traffic.py) when server-side evaluation jobs are unavailable.

Usage:  python tools/foundry/drive_traffic.py [--limit N] [--concurrency 3]
"""

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from foundry.common import AGENT_NAME, EVALS_DIR, FOUNDRY_DIR, agent_version, azd_env, credential, project_endpoint  # noqa: E402

RETRYABLE = {408, 424, 429, 500, 502, 503, 504}


class Token:
    def __init__(self) -> None:
        self._cred = credential()
        self._token = None

    def get(self) -> str:
        if self._token is None or self._token.expires_on - time.time() < 300:
            self._token = self._cred.get_token("https://ai.azure.com/.default")
        return self._token.token


def output_text(body: dict) -> str:
    """Text of the final assistant message (the case report)."""
    texts = []
    for item in body.get("output") or []:
        if item.get("type") == "message":
            texts = [c.get("text", "") for c in item.get("content") or [] if c.get("type") == "output_text"] or texts
    return "\n".join(texts) or body.get("output_text") or ""


def tool_trace(body: dict) -> dict:
    """Counts of the Microsoft-Decision-1 and policy tool calls in the output, and calls left without a result."""
    items = body.get("output") or []
    calls = {i.get("call_id"): i.get("name") for i in items if i.get("type") == "function_call"}
    results = {i.get("call_id") for i in items if i.get("type") == "function_call_output"}
    names = list(calls.values())
    return {"decision_calls": names.count("microsoft_decision_1"), "policy_calls": names.count("apply_trust_policy"),
            "unpaired": len(set(calls) - results)}


async def run_one(http: httpx.AsyncClient, url: str, token: Token, row: dict, sem: asyncio.Semaphore) -> dict:
    async with sem:
        started, error, status, body = time.perf_counter(), None, None, {}
        for attempt in range(4):
            try:
                resp = await http.post(url, headers={"Authorization": f"Bearer {token.get()}"},
                                       json={"input": row["query"], "stream": False})
                status = resp.status_code
                if status < 400:
                    body, error = resp.json(), None
                    break
                error = resp.text[:300]
                if status not in RETRYABLE:
                    break
            except (httpx.HTTPError, ValueError) as exc:
                error = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(5 * (attempt + 1))
        return {"name": row["name"], "lane": row["lane"], "ground_truth": row["ground_truth"],
                "expected_behavior": row["expected_behavior"], "query": row["query"], "status": status, "error": error,
                "response_id": body.get("id"), "latency_ms": round((time.perf_counter() - started) * 1000),
                "tool_trace": tool_trace(body), "response": output_text(body)}


async def main_async(limit: int | None, concurrency: int, ids: list[str] | None) -> Path:
    rows = [json.loads(line) for line in (EVALS_DIR / "foundry_golden.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    rows = [r for r in rows if not ids or r["name"] in ids][:limit] if (limit or ids) else rows
    url = f"{project_endpoint()}/agents/{AGENT_NAME}/endpoint/protocols/openai/responses?api-version=v1"
    env = azd_env().get("AZURE_ENV_NAME", "dev")
    out = FOUNDRY_DIR / "results" / env / "traffic" / f"traffic-v{agent_version()}-{datetime.now():%Y%m%d-%H%M%S}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    token, sem = Token(), asyncio.Semaphore(concurrency)
    done = 0
    async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=30)) as http:
        tasks = [asyncio.create_task(run_one(http, url, token, r, sem)) for r in rows]
        with out.open("w", encoding="utf-8") as fh:
            for task in asyncio.as_completed(tasks):
                result = await task
                fh.write(json.dumps(result, ensure_ascii=False) + "\n")
                fh.flush()
                done += 1
                ok = "ok" if result["status"] and result["status"] < 400 else f"ERR {result['status']} {result['error']}"
                print(f"[{datetime.now():%H:%M:%S}] {done}/{len(rows)} {result['name']} {result['latency_ms']} ms {ok}", flush=True)
    print(f"saved {out}")
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int)
    parser.add_argument("--ids", nargs="*")
    parser.add_argument("--concurrency", type=int, default=3)
    args = parser.parse_args()
    asyncio.run(main_async(args.limit, args.concurrency, args.ids))


if __name__ == "__main__":
    main()
