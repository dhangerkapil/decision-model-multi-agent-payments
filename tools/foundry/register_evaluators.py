"""Register the Payments Trust Fabric evaluators and dataset in Microsoft Foundry.

* Code evaluators (deterministic): ptf_disposition_accuracy, ptf_policy_invariants  (tools/foundry/evaluators/*.py)
* Dataset: ptf-golden (evals/foundry_golden.jsonl)
* Rubric evaluator (LLM judge): ptf_case_quality. Foundry auto-generates it from the policy context, the hosted agent and
  the golden dataset (evals/rubric_context.md); if generation is unavailable (for example when the project's storage
  account only accepts private-network traffic), the reviewed rubric in evals/rubric_dimensions.json is registered.

Full evaluator definitions are cached in src/payments-trust-fabric/.foundry/evaluators/.

Usage:  python tools/foundry/register_evaluators.py [--regenerate-rubric | --manual-rubric]
"""

import argparse
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from azure.ai.projects.models import (  # noqa: E402
    AgentEvaluatorGenerationJobSource,
    DatasetEvaluatorGenerationJobSource,
    EvaluatorCategory,
    EvaluatorDefinitionType,
    EvaluatorGenerationInputs,
    PromptEvaluatorGenerationJobSource,
)
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError  # noqa: E402

from foundry.common import (AGENT_NAME, EVALS_DIR, EVALS_PREVIEW, FOUNDRY_DIR, GENERATION_MODEL, project_client,  # noqa: E402
                            write_json)

EVALUATOR_DIR = Path(__file__).resolve().parent / "evaluators"
DATASET_NAME, DATASET_VERSION = "ptf-golden", "1"
RUBRIC_NAME = "ptf_case_quality"

CODE_EVALUATORS = {
    "ptf_disposition_accuracy": ("disposition_accuracy.py", "PTF disposition accuracy",
                                 "1.0 when the agent's final disposition (from the machine-readable case record) is one of the "
                                 "labelled acceptable dispositions, else 0.0."),
    "ptf_policy_invariants": ("policy_invariants.py", "PTF policy invariants",
                              "Share of the platform's safety guarantees that hold: adverse outcomes never automatic and always "
                              "owned by a human queue; no customer message on sanctions holds; quarantined events never reach an "
                              "LLM; drafts never ask for secrets; every case has a Microsoft-Decision-1 trail and reasons."),
    "ptf_writer_contract": ("writer_contract.py", "PTF writer contract",
                            "Share of the writer's delivery contract that holds: required fields present and every capped field "
                            "within its word limit (added after a Foundry Insight recommended deterministic last-mile checks)."),
}


def _latest(client, name):
    try:
        versions = list(client.evaluators.list_versions(name=name, headers=EVALS_PREVIEW))
    except ResourceNotFoundError:
        return None
    return max(versions, key=lambda v: int(v.version)) if versions else None


def register_code_evaluators(client) -> None:
    for name, (filename, display, description) in CODE_EVALUATORS.items():
        code = (EVALUATOR_DIR / filename).read_text(encoding="utf-8")
        latest = _latest(client, name)
        latest_def = (latest.as_dict().get("definition") or {}) if latest is not None else {}
        if latest is not None and latest_def.get("code_text") == code and "response" in (latest_def.get("data_schema") or {}).get("properties", {}):
            evaluator = latest
            print(f"{name}: up to date (v{evaluator.version})")
        else:
            evaluator = client.evaluators.create_version(  # type: ignore[call-overload]
                name=name,
                evaluator_version={
                    "name": name,
                    "categories": [EvaluatorCategory.QUALITY],
                    "display_name": display,
                    "description": description,
                    "definition": {
                        "type": EvaluatorDefinitionType.CODE,
                        "code_text": code,
                        "init_parameters": {"type": "object", "required": ["pass_threshold"],
                                            "properties": {"pass_threshold": {"type": "number"}}},
                        "metrics": {"result": {"type": "continuous", "desirable_direction": "increase",
                                               "min_value": 0.0, "max_value": 1.0}},
                        # Flat inputs, mapped per run (response <- sample.output_text, ground_truth <- item.ground_truth);
                        # the runtime passes them to grade(sample, item) as `item`.
                        "data_schema": {"type": "object", "required": ["response", "ground_truth"], "properties": {
                            "query": {"type": "string"}, "response": {"type": "string"}, "ground_truth": {"type": "string"}}},
                    },
                },
                headers=EVALS_PREVIEW,
            )
            print(f"{name}: registered v{evaluator.version}")
        write_json(FOUNDRY_DIR / "evaluators" / f"{name}-v{evaluator.version}.json", evaluator.as_dict())


def upload_dataset(client) -> bool:
    """Register the golden set as a Foundry dataset (lineage + rubric source). Best effort: the project's storage account
    may only accept private-network uploads, in which case evaluations still run with inline rows."""
    try:
        dataset = client.datasets.get(name=DATASET_NAME, version=DATASET_VERSION)
        print(f"dataset {DATASET_NAME} v{DATASET_VERSION}: exists")
    except ResourceNotFoundError:
        try:
            dataset = client.datasets.upload_file(name=DATASET_NAME, version=DATASET_VERSION,
                                                  file_path=str(EVALS_DIR / "foundry_golden.jsonl"))
            print(f"dataset {DATASET_NAME} v{DATASET_VERSION}: uploaded")
        except HttpResponseError as exc:
            print(f"dataset {DATASET_NAME}: upload refused by the project storage account ({exc.reason or exc.message}); "
                  "continuing with inline evaluation rows")
            return False
    write_json(FOUNDRY_DIR / "datasets" / f"{AGENT_NAME}-{DATASET_NAME}-v{DATASET_VERSION}.ref.json",
               {**dataset.as_dict(), "localFile": "evals/foundry_golden.jsonl"})
    return True


def generate_rubric(client, regenerate: bool, with_dataset: bool, name: str = RUBRIC_NAME) -> None:
    latest = _latest(client, name)
    if latest is not None and not regenerate:
        print(f"{name}: exists (v{latest.version}); pass --regenerate-rubric to regenerate")
        write_json(FOUNDRY_DIR / "evaluators" / f"{name}-v{latest.version}.json", latest.as_dict())
        return
    sources = [
        PromptEvaluatorGenerationJobSource(description="Payments Trust Fabric policy and what a good case report looks like.",
                                           prompt=(EVALS_DIR / "rubric_context.md").read_text(encoding="utf-8")),
        AgentEvaluatorGenerationJobSource(description="The deployed hosted agent (description and metadata).", agent_name=AGENT_NAME),
    ]
    if with_dataset:
        sources.append(DatasetEvaluatorGenerationJobSource(description="120 labelled inbound payments events with expected behaviour.",
                                                           name=DATASET_NAME, version=DATASET_VERSION))
    print(f"{name}: generating with {GENERATION_MODEL} from {len(sources)} sources (several minutes)...", flush=True)
    poller = client.evaluators.begin_create_generation_job(
        job=EvaluatorGenerationInputs(model=GENERATION_MODEL, evaluator_name=name,
                                      evaluator_display_name="PTF case quality (Foundry-generated rubric)",
                                      evaluator_description="Rubric for Payments Trust Fabric case reports, generated by Foundry from "
                                                            "the policy context, the hosted agent and the labelled dataset.",
                                      sources=sources),
        operation_id=f"ptf-rubric-{uuid.uuid4().hex[:8]}", polling_interval=20, headers=EVALS_PREVIEW)
    evaluator = poller.result()
    print(f"{name}: generated v{evaluator.version}")
    path = write_json(FOUNDRY_DIR / "evaluators" / f"{name}-v{evaluator.version}.json", evaluator.as_dict())
    for dim in evaluator.as_dict().get("definition", {}).get("dimensions", []):
        print(f"  - {dim.get('id')} (weight {dim.get('weight')}{', always' if dim.get('always_applicable') else ''}): "
              f"{dim.get('description')}")
    print(f"cached {path}")


def register_manual_rubric(client) -> None:
    """Register the reviewed rubric in evals/rubric_dimensions.json (used when auto-generation is unavailable)."""
    spec = json.loads((EVALS_DIR / "rubric_dimensions.json").read_text(encoding="utf-8"))
    definition = {"type": EvaluatorDefinitionType.RUBRIC, "dimensions": spec["dimensions"], "pass_threshold": spec["pass_threshold"]}
    latest = _latest(client, RUBRIC_NAME)
    current = (latest.as_dict().get("definition") or {}) if latest is not None else {}
    if latest is not None and current.get("dimensions") == spec["dimensions"] and current.get("pass_threshold") == spec["pass_threshold"]:
        evaluator = latest
        print(f"{RUBRIC_NAME}: up to date (v{evaluator.version})")
    else:
        evaluator = client.evaluators.create_version(  # type: ignore[call-overload]
            name=RUBRIC_NAME,
            evaluator_version={"name": RUBRIC_NAME, "categories": [EvaluatorCategory.QUALITY], "display_name": spec["display_name"],
                               "description": spec["description"], "definition": definition},
            headers=EVALS_PREVIEW,
        )
        print(f"{RUBRIC_NAME}: registered v{evaluator.version} (manual rubric, {len(spec['dimensions'])} dimensions)")
    write_json(FOUNDRY_DIR / "evaluators" / f"{RUBRIC_NAME}-v{evaluator.version}.json", evaluator.as_dict())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--regenerate-rubric", action="store_true", help="auto-generate the rubric again with Foundry")
    parser.add_argument("--manual-rubric", action="store_true", help="register evals/rubric_dimensions.json instead of generating")
    parser.add_argument("--auto-rubric-name", help="generate a Foundry rubric under this separate name (keeps the reviewed one)")
    args = parser.parse_args()
    with project_client() as client:
        register_code_evaluators(client)
        if args.manual_rubric:
            register_manual_rubric(client)
            return
        with_dataset = upload_dataset(client)
        if args.auto_rubric_name:
            generate_rubric(client, True, with_dataset, name=args.auto_rubric_name)
            return
        try:
            generate_rubric(client, args.regenerate_rubric, with_dataset)
        except HttpResponseError as exc:
            print(f"{RUBRIC_NAME}: auto-generation failed ({str(exc.message or exc)[:160]}); registering the reviewed rubric instead")
            register_manual_rubric(client)


if __name__ == "__main__":
    main()
