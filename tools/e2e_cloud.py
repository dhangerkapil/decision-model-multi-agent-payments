"""Smoke and end-to-end test against the deployed console and Foundry hosted agent.

Reads the console URL from the active azd environment (CONSOLE_URL) unless --base is given.

Usage:
    python tools/e2e_cloud.py                       # full run: every hero case, a 12-event firehose, calibration
    python tools/e2e_cloud.py --heroes 3 --firehose 0 --skip-calibrate   # quick smoke test
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def azd_value(name: str) -> str:
    """Read one value from the default azd environment (.azure/<env>/.env), if there is one."""
    config = ROOT / ".azure" / "config.json"
    if not config.exists():
        return ""
    env = json.loads(config.read_text(encoding="utf-8")).get("defaultEnvironment", "")
    env_file = ROOT / ".azure" / env / ".env"
    if not env_file.exists():
        return ""
    for line in env_file.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        if key.strip() == name:
            return value.strip().strip('"')
    return ""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("CONSOLE_URL") or azd_value("CONSOLE_URL"))
    ap.add_argument("--heroes", type=int, default=0, help="Run only the first N hero cases (0 = all)")
    ap.add_argument("--firehose", type=int, default=12, help="Events for the decision-only firehose check (0 = skip)")
    ap.add_argument("--skip-calibrate", action="store_true", help="Skip the calibration check")
    ap.add_argument("--out", default="", help="Optional path for a JSON report")
    args = ap.parse_args()
    if not args.base:
        sys.exit("No console URL: pass --base or run from a folder with an azd environment that has CONSOLE_URL.")

    c = httpx.Client(base_url=args.base.rstrip("/"), timeout=300)
    report: dict = {"base": args.base, "started": time.strftime("%Y-%m-%d %H:%M:%S"), "checks": []}

    def check(name: str, ok: bool, detail: str) -> None:
        report["checks"].append({"name": name, "ok": ok, "detail": detail})
        print(f"{'PASS' if ok else 'FAIL'}  {name:42s} {detail}", flush=True)

    def skip(name: str, detail: str) -> None:
        print(f"SKIP  {name:42s} {detail}", flush=True)

    check("health", c.get("/healthz").json().get("status") == "ok", "GET /healthz")
    cfg = c.get("/api/config").json()
    check("config", bool(cfg.get("agent_name")) and bool(cfg.get("decision_deployments")),
          f"agent={cfg.get('agent_name')} deployments={cfg.get('decision_deployments')}")
    events = {e["id"]: e for e in c.get("/api/events").json()}
    check("events", len(events) == 120, f"{len(events)} labeled events")

    base = c.get("/api/baseline").json()
    if base:
        check("baseline", base.get("disposition_accuracy", 0) >= 0.95, f"disposition accuracy {base.get('disposition_accuracy')}")
    else:
        skip("baseline", "no console/data/firehose_baseline.json (run tools/evaluate.py to create it)")
    bench = c.get("/api/benchmark").json()
    if bench:
        check("benchmark", len(bench.get("models", [])) >= 2,
              ", ".join(f"{m['name']} {m['accuracy']:.1%}" for m in bench.get("models", [])))
    else:
        skip("benchmark", "no console/data/benchmark.json (run tools/benchmark.py to create it)")

    heroes = c.get("/api/heroes").json()
    if args.heroes:
        heroes = heroes[:args.heroes]
    runs = []
    for h in heroes:
        started = time.perf_counter()
        r = c.post("/api/agent/run", json={"input": h["id"]})
        elapsed = (time.perf_counter() - started) * 1000
        rec = (r.json() or {}).get("record") if r.status_code == 200 else None
        gold = (rec or {}).get("gold") or {}
        expected = gold.get("acceptable_dispositions") or [gold.get("disposition") or events[h["id"]]["expected"]]
        got = (rec or {}).get("disposition")
        m = (rec or {}).get("metrics") or {}
        runs.append({"hero": h["hero"], "id": h["id"], "expected": expected, "got": got, "ok": got in expected,
                     "roundtrip_ms": round(elapsed), "decisions": m.get("decisions"), "llm_calls": m.get("llm_calls")})
        check(f"hero {h['hero']}", got in expected,
              f"{h['id']} -> {got} (expected {'/'.join(expected)}) · {m.get('decisions')} decisions · {round(elapsed)} ms")
    report["heroes"] = runs

    if args.firehose:
        rows, summary = [], {}
        with c.stream("POST", "/api/firehose", json={"count": args.firehose, "seed": 3}) as s:
            for line in s.iter_lines():
                if line.startswith("data: "):
                    ev = json.loads(line[6:])
                    if ev["type"] == "row":
                        rows.append(ev)
                    elif ev["type"] == "summary":
                        summary = ev
        check("firehose", len(rows) == args.firehose and summary.get("accuracy", 0) >= 0.8,
              f"{len(rows)} events · accuracy {summary.get('accuracy')} · {summary.get('decisions')} decisions")
        report["firehose"] = summary

    if not args.skip_calibrate:
        sample = c.get("/api/calibrate/sample").text
        cal = c.post("/api/calibrate", files={"file": ("sample.csv", sample, "text/csv")},
                     data={"instructions": "Which dispute reason category best fits the cardholder's claim?", "max_rows": "30",
                           "shuffle_check": "10"}).json()
        check("calibrate", cal.get("accuracy", 0) >= 0.8, f"accuracy {cal.get('accuracy', 0):.1%} · ECE {cal.get('ece', 0):.3f}")

    queue = c.get("/api/review").json()
    check("review queue", isinstance(queue, list), f"{len(queue)} cases waiting for people")

    report["passed"] = sum(1 for x in report["checks"] if x["ok"])
    report["total"] = len(report["checks"])
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\n{report['passed']}/{report['total']} checks passed")
    sys.exit(0 if report["passed"] == report["total"] else 1)


if __name__ == "__main__":
    main()
