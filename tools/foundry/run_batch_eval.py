"""Run an agent-target batch evaluation of the deployed hosted agent in Microsoft Foundry.

Evaluators: built-in quality (task_adherence, intent_resolution, relevance, coherence), built-in safety (indirect_attack,
violence, hate_unfairness), the ptf_case_quality rubric (LLM judge) and two deterministic code evaluators
(ptf_disposition_accuracy, ptf_policy_invariants). Rows come from evals/foundry_golden.jsonl and are sent inline.

Usage:
  python tools/foundry/run_batch_eval.py --limit 3            # smoke run
  python tools/foundry/run_batch_eval.py                       # full 120-row run
  python tools/foundry/run_batch_eval.py --resume <eval_id> <run_id>
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from azure.ai.projects.models import AzureAIAgentTargetParam, TargetCompletionEvalRunDataSource, TestingCriterionAzureAIEvaluator  # noqa: E402
from openai import AuthenticationError  # noqa: E402
from openai.types.eval_create_params import DataSourceConfigCustom  # noqa: E402
from openai.types.evals.create_eval_completions_run_data_source_param import SourceFileContent, SourceFileContentContent  # noqa: E402

from foundry.common import (AGENT_NAME, EVALS_DIR, EVALS_PREVIEW, FOUNDRY_DIR, JUDGE_MODEL, agent_version, azd_env,  # noqa: E402
                            project_client, write_json)

TERMINAL = {"completed", "failed", "canceled", "cancelled"}
CUSTOM_EVALUATORS = ["ptf_case_quality", "ptf_disposition_accuracy", "ptf_policy_invariants", "ptf_writer_contract"]
TEXT = {"query": "{{item.query}}", "response": "{{sample.output_text}}"}
LABELLED = {**TEXT, "ground_truth": "{{item.ground_truth}}"}  # code evaluators receive these as grade(sample, item)'s item


def latest_versions(client, names: list[str]) -> dict[str, str]:
    """Pin custom evaluators to their newest catalog version (an unpinned name may resolve to an older version)."""
    pinned = {}
    for name in names:
        versions = list(client.evaluators.list_versions(name=name, headers=EVALS_PREVIEW))
        pinned[name] = str(max(int(v.version) for v in versions))
    return pinned


def testing_criteria(judge: str, pinned: dict[str, str]) -> list[TestingCriterionAzureAIEvaluator]:
    def ev(name, evaluator, mapping, init=None):
        version = {"evaluator_version": pinned[evaluator]} if evaluator in pinned else {}
        return TestingCriterionAzureAIEvaluator(type="azure_ai_evaluator", name=name, evaluator_name=evaluator,
                                                data_mapping=mapping, **version,
                                                **({"initialization_parameters": init} if init else {}))
    model = {"deployment_name": judge}
    return [
        ev("task_adherence", "builtin.task_adherence", {"query": "{{item.query}}", "response": "{{sample.output_items}}"}, model),
        ev("intent_resolution", "builtin.intent_resolution", TEXT, model),
        ev("relevance", "builtin.relevance", TEXT, model),
        ev("coherence", "builtin.coherence", TEXT, model),
        ev("indirect_attack", "builtin.indirect_attack", TEXT),
        ev("violence", "builtin.violence", TEXT),
        ev("hate_unfairness", "builtin.hate_unfairness", TEXT),
        ev("case_quality_rubric", "ptf_case_quality", TEXT, model),
        ev("disposition_accuracy", "ptf_disposition_accuracy", LABELLED, {"pass_threshold": 1.0}),
        ev("policy_invariants", "ptf_policy_invariants", LABELLED, {"pass_threshold": 1.0}),
        ev("writer_contract", "ptf_writer_contract", LABELLED, {"pass_threshold": 1.0}),
    ]


def load_rows(limit: int | None, ids: list[str] | None) -> list[dict]:
    rows = [json.loads(line) for line in (EVALS_DIR / "foundry_golden.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    if ids:
        rows = [r for r in rows if r["name"] in ids]
    return rows[:limit] if limit else rows


def write_last_eval(meta: dict) -> None:
    """Mirror the run IDs into .foundry/agent-metadata.yaml so a later session can resume polling or downloads."""
    env = azd_env().get("AZURE_ENV_NAME", "dev")
    lines = [f"# Foundry overlay for the {AGENT_NAME} agent (deployment values come from azd).",
             f"defaultEnvironment: {env}", "environments:", f"  {env}:", "    azd:", f"      environmentName: {env}",
             f"      service: {AGENT_NAME}", "    evaluationSuites:", "      - id: golden-120", "        tags: {tier: regression, purpose: baseline}",
             "        generationSource: manual-fallback", "        datasetFile: evals/foundry_golden.jsonl",
             "        evaluators: [builtin.task_adherence, builtin.intent_resolution, builtin.relevance, builtin.coherence, "
             "builtin.indirect_attack, builtin.violence, builtin.hate_unfairness, ptf_case_quality, ptf_disposition_accuracy, "
             "ptf_policy_invariants]", "    lastEval:"]
    lines += [f"      {k}: {json.dumps(v)}" for k, v in meta.items()]
    path = FOUNDRY_DIR / "agent-metadata.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def poll_and_download(openai_client, eval_id: str, run_id: str, meta: dict, reconnect=None) -> Path:
    """Polls until the run finishes, then saves every output item. Runs often outlive an access token, so on HTTP 401
    the OpenAI client is rebuilt with `reconnect()` (which mints a fresh token) and polling continues."""

    def call(fn):
        nonlocal openai_client
        for attempt in range(3):
            try:
                return fn(openai_client)
            except AuthenticationError:
                if reconnect is None or attempt == 2:
                    raise
                print("access token expired; reconnecting", flush=True)
                openai_client = reconnect()

    started = time.time()
    while True:
        run = call(lambda oc: oc.evals.runs.retrieve(run_id=run_id, eval_id=eval_id))
        counts = getattr(run, "result_counts", None)
        print(f"[{datetime.now():%H:%M:%S}] status={run.status} counts={counts}", flush=True)
        if run.status in TERMINAL:
            break
        if time.time() - started > 4 * 3600:
            raise TimeoutError(f"Run {run_id} still {run.status} after 4 hours")
        time.sleep(60)
    items = call(lambda oc: [item.model_dump() for item in oc.evals.runs.output_items.list(run_id=run_id, eval_id=eval_id)])
    env = azd_env().get("AZURE_ENV_NAME", "dev")
    out = write_json(FOUNDRY_DIR / "results" / env / eval_id / f"{run_id}.json",
                     {"eval_id": eval_id, "run": run.model_dump(), "output_items": items})
    write_last_eval({**meta, "status": run.status, "resultFile": str(out.relative_to(FOUNDRY_DIR.parent)).replace("\\", "/"),
                     "completedAt": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    print(f"status={run.status} items={len(items)} report={getattr(run, 'report_url', None)}")
    print(f"saved {out}")
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int)
    parser.add_argument("--ids", nargs="*")
    parser.add_argument("--judge", default=JUDGE_MODEL)
    parser.add_argument("--agent-version", default=None)
    parser.add_argument("--label", default="golden")
    parser.add_argument("--eval-id", help="add the run to an existing evaluation group (same dataset, evaluators and thresholds) "
                                          "so Foundry can compare versions side by side")
    parser.add_argument("--resume", nargs=2, metavar=("EVAL_ID", "RUN_ID"))
    args = parser.parse_args()

    with project_client() as client, client.get_openai_client(timeout=180, max_retries=8) as openai_client:
        def reconnect():
            return client.get_openai_client(timeout=180, max_retries=8)

        if args.resume:
            eval_id, run_id = args.resume
            poll_and_download(openai_client, eval_id, run_id, {"evalId": eval_id, "evalRunId": run_id}, reconnect)
            return
        version = args.agent_version or agent_version()
        rows = load_rows(args.limit, args.ids)
        pinned = latest_versions(client, CUSTOM_EVALUATORS)
        print(f"custom evaluator versions: {pinned}", flush=True)
        schema = {"type": "object", "properties": {k: {"type": "string"} for k in
                                                   ("name", "event_id", "lane", "query", "ground_truth", "expected_behavior")},
                  "required": ["query"]}
        if args.eval_id:
            eval_id = args.eval_id
        else:
            eval_id = openai_client.evals.create(
                name=f"ptf-{args.label}-v{version}-{len(rows)}rows",
                data_source_config=DataSourceConfigCustom(type="custom", item_schema=schema, include_sample_schema=True),
                testing_criteria=testing_criteria(args.judge, pinned),  # type: ignore[arg-type]
            ).id
        data_source = TargetCompletionEvalRunDataSource(
            type="azure_ai_target_completions",
            source=SourceFileContent(type="file_content", content=[SourceFileContentContent(item=r) for r in rows]),
            input_messages={"type": "template", "template": [  # type: ignore[typeddict-item]
                {"type": "message", "role": "user", "content": {"type": "input_text", "text": "{{item.query}}"}}]},
            target=AzureAIAgentTargetParam(type="azure_ai_agent", name=AGENT_NAME, version=version),
        )
        run = openai_client.evals.runs.create(eval_id=eval_id, name=f"ptf-{args.label}-agent-v{version}",
                                              data_source=data_source)  # type: ignore[arg-type]
        meta = {"evalId": eval_id, "evalRunId": run.id, "runName": run.name, "suiteName": "golden-120",
                "agentVersion": version, "rows": len(rows), "judge": args.judge,
                "startedAt": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        write_last_eval(meta)
        print(f"eval={eval_id} run={run.id} rows={len(rows)} agent={AGENT_NAME} v{version}", flush=True)
        poll_and_download(openai_client, eval_id, run.id, meta, reconnect)


if __name__ == "__main__":
    main()
