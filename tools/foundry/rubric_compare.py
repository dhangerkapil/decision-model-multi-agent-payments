"""Rubric calibration: score the same saved agent responses with the reviewed rubric (ptf_case_quality) and the
Foundry-generated rubric (ptf_case_quality_auto) in one dataset-target evaluation, then report agreement.

No agent calls are made: the responses come from a downloaded agent-target run.

Usage:  python tools/foundry/rubric_compare.py <agent-target-run.json>
"""

import argparse
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from azure.ai.projects.models import TestingCriterionAzureAIEvaluator  # noqa: E402
from openai.types.eval_create_params import DataSourceConfigCustom  # noqa: E402
from openai.types.evals.create_eval_jsonl_run_data_source_param import (  # noqa: E402
    CreateEvalJSONLRunDataSourceParam, SourceFileContent, SourceFileContentContent)

from foundry.analyze_eval import merged_results  # noqa: E402
from foundry.common import EVALS_PREVIEW, FOUNDRY_DIR, JUDGE_MODEL, azd_env, project_client, write_json  # noqa: E402

RUBRICS = ["ptf_case_quality", "ptf_case_quality_auto"]


def rows_from_run(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = []
    for item in data.get("output_items") or []:
        src = item.get("datasource_item") or {}
        response = src.get("sample.output_text") or ""
        if response:
            rows.append({"name": src.get("name", "?"), "lane": src.get("lane", "?"), "query": src.get("query", ""), "response": response})
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_json")
    parser.add_argument("--judge", default=JUDGE_MODEL)
    args = parser.parse_args()
    rows = rows_from_run(Path(args.run_json))
    with project_client() as client, client.get_openai_client(timeout=180, max_retries=8) as oc:
        pinned = {n: str(max(int(v.version) for v in client.evaluators.list_versions(name=n, headers=EVALS_PREVIEW))) for n in RUBRICS}
        print(f"rubrics: {pinned}; rows: {len(rows)}", flush=True)
        criteria = [TestingCriterionAzureAIEvaluator(type="azure_ai_evaluator", name=n, evaluator_name=n, evaluator_version=v,
                                                     initialization_parameters={"deployment_name": args.judge},
                                                     data_mapping={"query": "{{item.query}}", "response": "{{item.response}}"})
                    for n, v in pinned.items()]
        schema = {"type": "object", "properties": {k: {"type": "string"} for k in ("name", "lane", "query", "response")},
                  "required": ["query", "response"]}
        ev = oc.evals.create(name=f"ptf-rubric-calibration-{len(rows)}rows",
                             data_source_config=DataSourceConfigCustom(type="custom", item_schema=schema, include_sample_schema=False),
                             testing_criteria=criteria)  # type: ignore[arg-type]
        run = oc.evals.runs.create(eval_id=ev.id, name="rubric-calibration", data_source=CreateEvalJSONLRunDataSourceParam(
            type="jsonl", source=SourceFileContent(type="file_content", content=[SourceFileContentContent(item=r) for r in rows])))
        print(f"eval={ev.id} run={run.id}", flush=True)
        while run.status not in ("completed", "failed", "canceled"):
            time.sleep(45)
            try:
                run = oc.evals.runs.retrieve(run_id=run.id, eval_id=ev.id)
                print(f"[{datetime.now():%H:%M:%S}] {run.status} {run.result_counts}", flush=True)
            except Exception as exc:  # noqa: BLE001 - keep polling through transient network errors
                print(f"  poll retry ({type(exc).__name__})", flush=True)
        items = [i.model_dump() for i in oc.evals.runs.output_items.list(run_id=run.id, eval_id=ev.id)]
    env = azd_env().get("AZURE_ENV_NAME", "dev")
    out = write_json(FOUNDRY_DIR / "results" / env / ev.id / f"{run.id}.json", {"eval_id": ev.id, "run": run.model_dump(), "output_items": items})
    scores: dict[str, dict[str, tuple]] = {n: {} for n in RUBRICS}
    for item in items:
        name = (item.get("datasource_item") or {}).get("name", "?")
        for evaluator, res in merged_results(item).items():
            if evaluator in scores and res["score"] is not None:
                scores[evaluator][name] = (float(res["score"]), res["passed"])
    common = sorted(set(scores[RUBRICS[0]]) & set(scores[RUBRICS[1]]))
    summary = {"run_file": str(out), "status": run.status, "rows": len(rows), "both_judged": len(common)}
    for n in RUBRICS:
        vals = [scores[n][c][0] for c in common]
        summary[n] = {"mean": round(statistics.mean(vals), 4) if vals else None,
                      "pass_rate": round(sum(1 for c in common if scores[n][c][1]) / max(len(common), 1), 4)}
    agree = sum(1 for c in common if bool(scores[RUBRICS[0]][c][1]) == bool(scores[RUBRICS[1]][c][1]))
    summary["pass_fail_agreement"] = round(agree / max(len(common), 1), 4)
    if len(common) > 2:
        summary["score_correlation"] = round(statistics.correlation([scores[RUBRICS[0]][c][0] for c in common],
                                                                    [scores[RUBRICS[1]][c][0] for c in common]), 4)
    print(json.dumps(summary, indent=2))
    write_json(out.with_suffix(".summary.json"), summary)


if __name__ == "__main__":
    main()
