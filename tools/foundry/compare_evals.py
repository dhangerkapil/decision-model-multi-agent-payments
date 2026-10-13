"""Compare two downloaded Foundry evaluation runs (for example agent v5 vs v6): per-evaluator pass rate and mean score,
and the items whose verdict changed.

Usage:  python tools/foundry/compare_evals.py <baseline-run.json> <candidate-run.json> [--labels v5 v6] [--json out.json]
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from foundry.analyze_eval import analyse, merged_results  # noqa: E402
from foundry.common import write_json  # noqa: E402


def verdicts(path: Path) -> dict[tuple[str, str], bool | None]:
    import json

    data = json.loads(path.read_text(encoding="utf-8"))
    out = {}
    for item in data.get("output_items") or []:
        name = (item.get("datasource_item") or {}).get("name", "?")
        for evaluator, res in merged_results(item).items():
            out[(evaluator, name)] = res["passed"]
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("baseline")
    parser.add_argument("candidate")
    parser.add_argument("--labels", nargs=2, default=["baseline", "candidate"])
    parser.add_argument("--json")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # the report prints "Δ", which legacy Windows code pages cannot encode
    a, b = analyse(Path(args.baseline)), analyse(Path(args.candidate))
    la, lb = args.labels
    rows = []
    print(f"{'evaluator':24} {la + ' pass':>11} {lb + ' pass':>11} {'Δ pass':>8} {la + ' mean':>10} {lb + ' mean':>10}")
    for name in sorted(set(a["evaluators"]) | set(b["evaluators"])):
        ea, eb = a["evaluators"].get(name, {}), b["evaluators"].get(name, {})
        pa, pb = ea.get("pass_rate"), eb.get("pass_rate")
        delta = (pb - pa) if pa is not None and pb is not None else None
        rows.append({"evaluator": name, f"{la}_pass_rate": pa, f"{lb}_pass_rate": pb, "delta": delta,
                     f"{la}_mean": ea.get("mean_score"), f"{lb}_mean": eb.get("mean_score")})
        fmt = lambda v: "—" if v is None else f"{v:.1%}"
        print(f"{name:24} {fmt(pa):>11} {fmt(pb):>11} {('—' if delta is None else f'{delta:+.1%}'):>8} "
              f"{str(ea.get('mean_score')):>10} {str(eb.get('mean_score')):>10}")
    va, vb = verdicts(Path(args.baseline)), verdicts(Path(args.candidate))
    changes = [(e, n, va.get((e, n)), vb.get((e, n))) for (e, n) in sorted(set(va) & set(vb)) if va.get((e, n)) != vb.get((e, n))]
    print(f"\n{len(changes)} verdict change(s):")
    for evaluator, name, before, after in changes:
        arrow = "fixed" if after and not before else ("REGRESSED" if before and not after else "changed")
        print(f"  {arrow:9} {evaluator:22} {name}: {before} -> {after}")
    if args.json:
        write_json(Path(args.json), {"labels": args.labels, "evaluators": rows,
                                     "verdict_changes": [dict(zip(("evaluator", "case", "before", "after"), c)) for c in changes]})
        print(f"\nsaved {args.json}")


if __name__ == "__main__":
    main()
