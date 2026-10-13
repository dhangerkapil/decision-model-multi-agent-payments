"""Summarise a downloaded Foundry batch-evaluation run: per-evaluator pass rates and scores, per-lane breakdown, and the
failing items with the judges' reasons (custom evaluators emit two entries per item, which are merged here).

Usage:  python tools/foundry/analyze_eval.py [path-to-run.json] [--json out.json]
"""

import argparse
import collections
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from foundry.common import FOUNDRY_DIR, write_json  # noqa: E402


def latest_run_file() -> Path:
    files = [p for p in (FOUNDRY_DIR / "results").rglob("evalrun_*.json")]
    return max(files, key=lambda p: p.stat().st_mtime)


def merged_results(item: dict) -> dict[str, dict]:
    """criterion name -> {score, passed, reason, label}; merges the custom_score + detail entries of custom evaluators."""
    out: dict[str, dict] = {}
    for r in item.get("results") or []:
        name = r.get("name") or r.get("metric") or "?"
        entry = out.setdefault(name, {"score": None, "passed": None, "reason": None, "label": None})
        if r.get("score") is not None and entry["score"] is None:
            entry["score"] = r["score"]
        for key in ("passed", "reason", "label"):
            if r.get(key) is not None:
                entry[key] = r[key]
        sample = r.get("sample") or {}
        if entry["reason"] is None and isinstance(sample, dict) and sample.get("error"):
            entry["reason"] = f"error: {json.dumps(sample.get('error'))[:200]}"
    return out


def analyse(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data.get("output_items") or []
    per_eval: dict[str, dict] = collections.defaultdict(lambda: {"n": 0, "passed": 0, "failed": 0, "errored": 0, "scores": []})
    per_lane: dict[tuple[str, str], list[bool]] = collections.defaultdict(list)
    failures: dict[str, list[dict]] = collections.defaultdict(list)
    for item in items:
        row = item.get("datasource_item") or {}
        lane, name = row.get("lane", "?"), row.get("name") or row.get("event_id") or "?"
        for evaluator, res in merged_results(item).items():
            stats = per_eval[evaluator]
            stats["n"] += 1
            if isinstance(res["score"], (int, float)):
                stats["scores"].append(float(res["score"]))
            if res["passed"] is True:
                stats["passed"] += 1
                per_lane[(evaluator, lane)].append(True)
            elif res["passed"] is False:
                stats["failed"] += 1
                per_lane[(evaluator, lane)].append(False)
                failures[evaluator].append({"case": name, "lane": lane, "score": res["score"], "reason": (res["reason"] or "")[:500]})
            else:
                stats["errored"] += 1
                if res["reason"]:
                    failures[evaluator].append({"case": name, "lane": lane, "score": None, "reason": f"[no verdict] {res['reason'][:300]}"})
    summary = {
        "run_file": str(path), "items": len(items), "run_status": (data.get("run") or {}).get("status"),
        "report_url": (data.get("run") or {}).get("report_url"),
        "evaluators": {k: {"n": v["n"], "passed": v["passed"], "failed": v["failed"], "errored": v["errored"],
                           "pass_rate": round(v["passed"] / max(v["passed"] + v["failed"], 1), 4),
                           "mean_score": round(statistics.mean(v["scores"]), 4) if v["scores"] else None}
                       for k, v in sorted(per_eval.items())},
        "by_lane": {f"{e}|{lane}": f"{sum(v)}/{len(v)}" for (e, lane), v in sorted(per_lane.items())},
        "failures": failures,
    }
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?")
    parser.add_argument("--json")
    args = parser.parse_args()
    summary = analyse(Path(args.path) if args.path else latest_run_file())
    print(f"run: {summary['run_file']}\nstatus: {summary['run_status']}  items: {summary['items']}\nreport: {summary['report_url']}\n")
    print(f"{'evaluator':24} {'pass rate':>9} {'passed':>7} {'failed':>7} {'no verdict':>10} {'mean score':>11}")
    for name, s in summary["evaluators"].items():
        print(f"{name:24} {s['pass_rate']:>9.1%} {s['passed']:>7} {s['failed']:>7} {s['errored']:>10} {str(s['mean_score']):>11}")
    for evaluator, rows in summary["failures"].items():
        print(f"\n-- {evaluator}: {len(rows)} failing / no-verdict item(s)")
        for row in rows[:12]:
            print(f"   {row['case']:6} {row['lane']:22} score={row['score']} :: {row['reason'][:220]}")
    if args.json:
        write_json(Path(args.json), summary)
        print(f"\nsaved {args.json}")


if __name__ == "__main__":
    main()
