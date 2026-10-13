"""Runtime settings for the Payments Trust Fabric hosted agent."""

import os
from dataclasses import dataclass
from urllib.parse import urlparse

# Microsoft-Decision-1 list price: $0.042 per 1M input tokens, output tokens are free.
DECISION_PRICE_PER_M_INPUT = 0.042


def _account_endpoint_from_project(project_endpoint: str) -> str:
    """The decision API is served by the Foundry resource; use the same host as the project endpoint."""
    parsed = urlparse(project_endpoint)
    return f"{parsed.scheme}://{parsed.hostname}" if parsed.hostname else ""


@dataclass(frozen=True)
class Settings:
    project_endpoint: str
    decision_endpoint: str
    decision_deployment: str
    llm_fast: str
    llm_standard: str
    llm_premium: str
    decision_rpm_per_deployment: int = 60

    @property
    def tiers(self) -> dict[str, str]:
        return {"fast": self.llm_fast, "standard": self.llm_standard, "premium": self.llm_premium}


def load_settings() -> Settings:
    project_endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT") or os.environ.get("AZURE_AI_PROJECT_ENDPOINT", "")
    if not project_endpoint:
        raise RuntimeError("FOUNDRY_PROJECT_ENDPOINT is not set.")
    decision_endpoint = (os.environ.get("DECISION_ENDPOINT") or _account_endpoint_from_project(project_endpoint)).rstrip("/")
    fast = os.environ.get("AZURE_AI_MODEL_DEPLOYMENT_NAME") or "gpt-5.4-mini"
    return Settings(
        project_endpoint=project_endpoint,
        decision_endpoint=decision_endpoint,
        decision_deployment=os.environ.get("DECISION_MODEL_DEPLOYMENT", "decision-model"),
        llm_fast=fast,
        llm_standard=os.environ.get("LLM_STANDARD_DEPLOYMENT") or fast,
        llm_premium=os.environ.get("LLM_PREMIUM_DEPLOYMENT") or os.environ.get("LLM_STANDARD_DEPLOYMENT") or fast,
        decision_rpm_per_deployment=int(os.environ.get("DECISION_RPM_PER_DEPLOYMENT", "60")),
    )
