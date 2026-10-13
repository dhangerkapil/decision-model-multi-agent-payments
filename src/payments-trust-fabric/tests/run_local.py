"""Offline smoke test: build the workflow graph and run hero scenarios in-process (no hosting server).

Usage (from src/payments-trust-fabric):  python -m tests.run_local AP-02 DP-01
"""

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
# The optimizer config loader resolves .agent_configs/ next to __main__; point it at the agent folder for test runs.
os.environ.setdefault("OPTIMIZATION_LOCAL_DIR", str(Path(__file__).resolve().parents[1] / ".agent_configs"))

from trust_fabric.workflow import build_agent  # noqa: E402


async def run(case_id: str) -> dict:
    agent = build_agent()
    started = time.perf_counter()
    response = await agent.run(case_id)
    elapsed = (time.perf_counter() - started) * 1000
    text = response.text or ""
    contents = [c for m in response.messages for c in m.contents]
    reasoning = [c.text for c in contents if c.type == "text_reasoning" and c.text]
    calls = {c.call_id: c.name for c in contents if c.type == "function_call"}
    results = {c.call_id for c in contents if c.type == "function_result"}
    match = re.search(r"```json\n(.*?)\n```", text, re.S)
    record = json.loads(match.group(1)) if match else {}
    print(f"\n=== {case_id}  ({elapsed:.0f} ms)")
    for line in reasoning:
        print("   ", line)
    print("   ->", record.get("disposition"), "| gold:", (record.get("gold") or {}).get("disposition"), "| metrics:", record.get("metrics"))
    names = [calls[k] for k in calls]
    print(f"    tool trace: {names.count('microsoft_decision_1')} Microsoft-Decision-1 calls, {names.count('apply_trust_policy')} policy calls, "
          f"{len(set(calls) - results)} without a result, prompt_config "
          f"{(record.get('llm_calls') or [{}])[0].get('prompt_config', '-') if record.get('llm_calls') else '-'}")
    if record.get("draft"):
        print("    draft:", json.dumps(record["draft"], ensure_ascii=False)[:600])
    if not record:
        print(text[:1500])
    return record


async def main(ids: list[str]) -> None:
    await asyncio.gather(*(run(case_id) for case_id in ids))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:] or ["AP-01"]))
