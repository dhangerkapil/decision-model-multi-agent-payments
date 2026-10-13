"""Shared helpers for the Foundry evaluation, Insights and optimizer tooling."""

import json
import os
from pathlib import Path

from azure.identity import AzureCliCredential, AzureDeveloperCliCredential, ChainedTokenCredential

ROOT = Path(__file__).resolve().parents[2]
AGENT_ROOT = ROOT / "src" / "payments-trust-fabric"
FOUNDRY_DIR = AGENT_ROOT / ".foundry"
EVALS_DIR = ROOT / "evals"
DOCS_DIR = ROOT / "docs"

AGENT_NAME = os.environ.get("AGENT_NAME", "decision-payments-agent")  # the hosted agent's name in azure.yaml
JUDGE_MODEL = "gpt-5.4-mini"        # recommended rubric judge (best balance of quality and cost)
GENERATION_MODEL = "gpt-5.4"        # rubric generation, Insights judge and Agent Optimizer model
EVALS_PREVIEW = {"Foundry-Features": "Evaluations=V1Preview"}  # opt-in header for custom (code/rubric) evaluators


def azd_env() -> dict[str, str]:
    """Values from the active azd environment (.azure/<env>/.env)."""
    config = json.loads((ROOT / ".azure" / "config.json").read_text(encoding="utf-8"))
    env_file = ROOT / ".azure" / config["defaultEnvironment"] / ".env"
    values: dict[str, str] = {}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"')
    return values


def project_endpoint() -> str:
    return os.environ.get("FOUNDRY_PROJECT_ENDPOINT") or azd_env()["AZURE_AI_PROJECT_ENDPOINT"]


def agent_version() -> str:
    # azd records the deployed version as AGENT_<SERVICE>_VERSION, for example AGENT_DECISION_PAYMENTS_AGENT_VERSION.
    return os.environ.get("AGENT_VERSION") or azd_env()[f"AGENT_{AGENT_NAME.upper().replace('-', '_')}_VERSION"]


def credential() -> ChainedTokenCredential:
    # Pin the tenant of the azd environment in case the signed-in account belongs to several tenants.
    tenant = os.environ.get("AZURE_TENANT_ID") or azd_env().get("AZURE_TENANT_ID")
    return ChainedTokenCredential(AzureDeveloperCliCredential(tenant_id=tenant, process_timeout=60),
                                  AzureCliCredential(tenant_id=tenant, process_timeout=60))


def project_client(allow_preview: bool = True):
    """Rubric/code evaluators, generation jobs and Insights are preview features (sends the Foundry-Features header)."""
    from azure.ai.projects import AIProjectClient
    from azure.core.pipeline.transport import RequestsTransport

    # Explicit timeouts so a dropped connection is retried instead of hanging forever.
    return AIProjectClient(endpoint=project_endpoint(), credential=credential(), allow_preview=allow_preview,
                           transport=RequestsTransport(connection_timeout=30, read_timeout=180), retry_total=10,
                           retry_backoff_factor=2)


def write_json(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return path
