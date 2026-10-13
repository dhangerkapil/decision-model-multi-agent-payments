"""Foundry Insights for the Payments Trust Fabric hosted agent.

  scan      create (or reuse) the agent's Insights monitor, analyse recent traces on demand, then list every Insight
  list      read-only: pull all generated Insights with evidence and proposed fixes (all pages)
  schedule  enable scheduled Insight generation (--hours N) or disable it (--off)

Usage:  python tools/foundry/insights.py scan --lookback-hours 6
"""

import argparse
import json
import sys
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from azure.ai.projects.models import AgentInsightMonitorCreate, AgentInsightMonitorUpdate, AgentInsightRunCreate  # noqa: E402

from foundry.common import AGENT_NAME, FOUNDRY_DIR, GENERATION_MODEL, azd_env, project_client, write_json  # noqa: E402


def monitor_for(ops, create: bool):
    existing = [m for m in ops.list(agent_name=AGENT_NAME)]
    if existing:
        monitor = ops.get(existing[0].id)
        print(f"monitor {monitor.id} (existing, judge={getattr(monitor, 'model_deployment_name', None)})")
        return monitor
    if not create:
        raise SystemExit(f"No Insights monitor exists for {AGENT_NAME}; run 'scan' first.")
    monitor = ops.create(AgentInsightMonitorCreate(agent_name=AGENT_NAME, model_deployment_name=GENERATION_MODEL, enabled=False))
    print(f"monitor {monitor.id} (created, judge={GENERATION_MODEL}, schedule off)")
    return monitor


def scan(ops, lookback_hours: float) -> None:
    monitor = monitor_for(ops, create=True)
    poller = ops.begin_create_run(monitor.id, AgentInsightRunCreate(lookback_hours=lookback_hours), operation_id=str(uuid.uuid4()))
    run_id = poller.details["run_id"]
    print(f"run {run_id}: analysing the last {lookback_hours} h of traces (asynchronous; several minutes)...", flush=True)
    result = poller.result()
    run = ops.get_run(monitor.id, run_id)
    summary = {"monitor_id": monitor.id, "run_id": run_id, "status": str(run.status),
               "traces_in_window": result.traces_in_window, "traces_analyzed": result.traces_analyzed,
               "insights_created": result.insights_created, "insights_updated": result.insights_updated,
               "insights_reopened": result.insights_reopened,
               "total_tokens": getattr(result.token_usage, "total_tokens", None)}
    print(json.dumps(summary, indent=2))
    list_all(ops, monitor, summary)


def list_all(ops, monitor=None, run_summary=None) -> None:
    monitor = monitor or monitor_for(ops, create=False)
    insights = [i.as_dict() for i in ops.list_insights(monitor.id, include_details=True, order="desc")]
    env = azd_env().get("AZURE_ENV_NAME", "dev")
    out = write_json(FOUNDRY_DIR / "results" / env / "insights" / f"insights-{datetime.now():%Y%m%d-%H%M%S}.json",
                     {"monitor_id": monitor.id, "run": run_summary, "count": len(insights), "insights": insights})
    print(f"{len(insights)} insight(s); saved {out}")
    for i in insights:
        details = i.get("details") or {}
        fix = ((details.get("recommended_actions") or {}).get("proposed_fix") or {}).get("text") or ""
        print(f"- [{i.get('severity')}/{i.get('category')}/{i.get('status')}] {i.get('title')} "
              f"(id={i.get('id')}, version={i.get('agent_version')}, traces={i.get('trace_count')})")
        if i.get("description"):
            print(f"    {i['description'][:400]}")
        if fix:
            print(f"    proposed fix: {fix[:400]}")


def schedule(ops, hours: float | None, off: bool) -> None:
    monitor = monitor_for(ops, create=True)
    update = AgentInsightMonitorUpdate(enabled=False) if off else AgentInsightMonitorUpdate(enabled=True, run_interval_hours=hours)
    updated = ops.update(monitor.id, update)
    print(f"schedule enabled={updated.enabled} interval={getattr(updated, 'run_interval_hours', None)} "
          f"next={getattr(updated, 'next_scheduled_run_at', None)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    p_scan = sub.add_parser("scan")
    p_scan.add_argument("--lookback-hours", type=float, default=6)
    sub.add_parser("list")
    p_sched = sub.add_parser("schedule")
    p_sched.add_argument("--hours", type=float, default=24)
    p_sched.add_argument("--off", action="store_true")
    args = parser.parse_args()
    with project_client() as client:
        ops = client.beta.agent_insight_monitors
        if args.command == "scan":
            scan(ops, args.lookback_hours)
        elif args.command == "list":
            list_all(ops)
        else:
            schedule(ops, args.hours, args.off)


if __name__ == "__main__":
    main()
