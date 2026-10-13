"""Score a saved traffic file with the deterministic code evaluators (the same grade() functions registered in Foundry).

Usage:  python tools/foundry/score_traffic.py [path-to-traffic.jsonl]   (defaults to the newest traffic file)
"""

import collections
import importlib.util
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from foundry.common import FOUNDRY_DIR  # noqa: E402

EVALUATORS = Path(__file__).resolve().parent / "evaluators"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, EVALUATORS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else max((FOUNDRY_DIR / "results").rglob("traffic-*.jsonl"), key=lambda p: p.stat().st_mtime)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    acc, inv, contract = _load("disposition_accuracy"), _load("policy_invariants"), _load("writer_contract")
    by_lane: dict[str, list[float]] = collections.defaultdict(list)
    misses, invariant_gaps, contract_gaps, errors, latencies = [], [], [], [], []
    for r in rows:
        if r.get("error") or not r.get("response"):
            errors.append((r["name"], r.get("status"), (r.get("error") or "")[:120]))
            continue
        sample, item = {"output_text": r["response"]}, {"ground_truth": r["ground_truth"]}
        a, i, c = acc.grade(sample, item), inv.grade(sample, item), contract.grade(sample, item)
        by_lane[r["lane"]].append(a)
        latencies.append(r["latency_ms"])
        record = inv._record(r["response"]) or {}
        if a < 1:
            misses.append((r["name"], r["ground_truth"], record.get("disposition"), (record.get("reasons") or [""])[:2]))
        if i < 1:
            invariant_gaps.append((r["name"], i, record.get("disposition")))
        if c < 1:
            contract_gaps.append((r["name"], c, record.get("disposition")))
    scored = sum(len(v) for v in by_lane.values())
    correct = sum(sum(v) for v in by_lane.values())
    print(f"file: {path.name}\nrows: {len(rows)}  scored: {scored}  errors: {len(errors)}")
    print(f"disposition accuracy: {correct:.0f}/{scored} = {correct / max(scored, 1):.1%}")
    for lane, values in sorted(by_lane.items()):
        print(f"  {lane:22} {sum(values):.0f}/{len(values)}")
    print(f"policy invariants: {scored - len(invariant_gaps)}/{scored} cases with every guarantee holding")
    print(f"writer contract:   {scored - len(contract_gaps)}/{scored} cases within every field requirement and word cap")
    traced = [r["tool_trace"] for r in rows if r.get("tool_trace")]
    if traced:
        complete = sum(1 for t in traced if t["decision_calls"] >= 2 and t["policy_calls"] >= 1 and t["unpaired"] == 0)
        print(f"tool trace:        {complete}/{len(traced)} responses carry Microsoft-Decision-1 and policy tool calls, all with results "
              f"({sum(t['decision_calls'] for t in traced)} Microsoft-Decision-1 calls, {sum(t['policy_calls'] for t in traced)} policy calls)")
    if latencies:
        latencies.sort()
        print(f"round trip (incl. cold session start): p50 {latencies[len(latencies) // 2] / 1000:.1f} s, "
              f"p90 {latencies[int(len(latencies) * 0.9)] / 1000:.1f} s")
    for m in misses:
        print("  MISS", m)
    for g in invariant_gaps:
        print("  INVARIANT", g)
    for g in contract_gaps:
        print("  CONTRACT", g)
    for e in errors:
        print("  ERROR", e)


if __name__ == "__main__":
    main()
