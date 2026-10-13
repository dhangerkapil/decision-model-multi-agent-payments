"""Watch an Agent Optimizer job and save its final result (candidates, scores, strategies).

Usage:  python tools/foundry/optimizer_status.py <job-id> [--watch]
"""

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from foundry.common import FOUNDRY_DIR, azd_env, credential, project_endpoint, write_json  # noqa: E402

API = "2025-11-15-preview"
TERMINAL = {"succeeded", "failed", "canceled", "cancelled"}


def get(path: str, token_cred) -> dict:
    for attempt in range(8):
        try:
            token = token_cred.get_token("https://ai.azure.com/.default").token
            resp = httpx.get(f"{project_endpoint()}{path}", headers={"Authorization": f"Bearer {token}"}, timeout=90)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            print(f"  retry {attempt + 1} ({type(exc).__name__})", flush=True)
            time.sleep(10)
    raise RuntimeError(f"GET {path} failed repeatedly")


def summarize(job: dict) -> None:
    progress = job.get("progress") or {}
    print(f"[{datetime.now():%H:%M:%S}] {job.get('status')} candidates={progress.get('candidates_completed')} "
          f"baseline={progress.get('baseline_score')} best={progress.get('best_score')} "
          f"elapsed={progress.get('elapsed_seconds')}s", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("job_id")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=int, default=90)
    args = parser.parse_args()
    cred = credential()
    job = get(f"/agent_optimization_jobs/{args.job_id}?api-version={API}", cred)
    summarize(job)
    while args.watch and job.get("status") not in TERMINAL:
        time.sleep(args.interval)
        job = get(f"/agent_optimization_jobs/{args.job_id}?api-version={API}", cred)
        summarize(job)
    env = azd_env().get("AZURE_ENV_NAME", "dev")
    out = write_json(FOUNDRY_DIR / "results" / env / "optimizer" / f"{args.job_id}.json", job)
    print(f"saved {out}")
    result = job.get("result") or job.get("outputs") or {}
    if result:
        print(json.dumps(result, indent=1)[:4000])
    if job.get("error"):
        print("error:", json.dumps(job["error"])[:1000])


if __name__ == "__main__":
    main()
