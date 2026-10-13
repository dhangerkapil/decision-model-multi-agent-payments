"""LLM agents (Microsoft Agent Framework) that do the generative work: draft and explain.

Role prompts are not hard-coded: they come from the Agent Optimizer config (see prompts.py and .agent_configs/).
"""

import json
import os
import time
from dataclasses import dataclass
from typing import Any

from agent_framework import Agent
from agent_framework.foundry import FoundryChatClient
from agent_framework.openai import OpenAIChatOptions
from pydantic import BaseModel

from .config import Settings
from .prompts import PromptBook


class ConsumerNotification(BaseModel):
    title: str
    body: str
    approve_label: str
    decline_label: str


class DisputeLetter(BaseModel):
    customer_message: str
    analyst_summary: str


class UnderwritingMemo(BaseModel):
    memo: str
    conditions: list[str]


class ProtectionMessage(BaseModel):
    in_app_message: str
    callback_script: str


class GeneralAnswer(BaseModel):
    answer: str


ROLES: dict[str, dict[str, Any]] = {
    "consumer_notification_writer": {"name": "ConsumerConfirmationAgent", "schema": ConsumerNotification},
    "dispute_case_writer": {"name": "DisputeCaseWriterAgent", "schema": DisputeLetter},
    "underwriting_memo_writer": {"name": "UnderwritingMemoAgent", "schema": UnderwritingMemo},
    "protection_writer": {"name": "CustomerProtectionAgent", "schema": ProtectionMessage},
    "general_assistant": {"name": "GeneralAssistantAgent", "schema": GeneralAnswer},
}


@dataclass
class LLMResult:
    role: str
    agent_name: str
    tier: str
    model: str
    parsed: BaseModel
    latency_ms: float
    input_tokens: int
    output_tokens: int
    prompt_config: str = "baseline"

    def as_record(self) -> dict[str, Any]:
        return {"role": self.role, "agent": self.agent_name, "tier": self.tier, "model": self.model, "latency_ms": round(self.latency_ms, 1),
                "input_tokens": self.input_tokens, "output_tokens": self.output_tokens, "prompt_config": self.prompt_config}


def _usage(resp: Any) -> tuple[int, int]:
    usage = getattr(resp, "usage_details", None)
    if usage is None:
        return 0, 0
    get = usage.get if isinstance(usage, dict) else lambda k, d=None: getattr(usage, k, d)
    return int(get("input_token_count", 0) or 0), int(get("output_token_count", 0) or 0)


class LLMPool:
    """Creates one Agent Framework agent per (role, model tier) and reuses it across requests."""

    def __init__(self, settings: Settings, credential: Any, prompts: PromptBook) -> None:
        self._settings = settings
        self._credential = credential
        self.prompts = prompts
        self._effort = os.environ.get("LLM_REASONING_EFFORT", "low").strip().lower()
        self._clients: dict[str, FoundryChatClient] = {}
        self._agents: dict[tuple[str, str], Agent] = {}

    def model_for(self, tier: str) -> str:
        return self._settings.tiers.get(tier, self._settings.llm_fast)

    def _client(self, tier: str) -> FoundryChatClient:
        if tier not in self._clients:
            self._clients[tier] = FoundryChatClient(project_endpoint=self._settings.project_endpoint, model=self.model_for(tier),
                                                    credential=self._credential)
        return self._clients[tier]

    def agent(self, role: str, tier: str, tuned: bool = True) -> Agent:
        key = (role, tier if tuned else f"{tier}-plain")
        if key not in self._agents:
            spec = ROLES[role]
            options: dict[str, Any] = {"response_format": spec["schema"]}
            if tuned and self._effort:
                options.update({"reasoning": {"effort": self._effort}, "verbosity": "low"})
            self._agents[key] = Agent(client=self._client(tier), instructions=self.prompts.instructions(role), name=spec["name"],
                                      default_options=OpenAIChatOptions[Any](**options))
        return self._agents[key]

    async def run(self, role: str, tier: str, prompt: str) -> LLMResult:
        spec = ROLES[role]
        started = time.perf_counter()
        try:
            resp = await self.agent(role, tier).run(prompt)
        except Exception as exc:  # noqa: BLE001 - some models reject reasoning/verbosity options
            if "reasoning" not in str(exc).lower() and "verbosity" not in str(exc).lower() and "unsupported" not in str(exc).lower():
                raise
            resp = await self.agent(role, tier, tuned=False).run(prompt)
        latency = (time.perf_counter() - started) * 1000
        parsed = resp.value if isinstance(getattr(resp, "value", None), BaseModel) else None
        if parsed is None:
            text = resp.text or "{}"
            try:
                parsed = spec["schema"].model_validate_json(text)
            except Exception:  # noqa: BLE001 - keep the workflow alive with a best-effort parse
                parsed = spec["schema"].model_validate(json.loads(text[text.find("{"): text.rfind("}") + 1] or "{}"))
        tin, tout = _usage(resp)
        return LLMResult(role, spec["name"], tier, self.model_for(tier), parsed, latency, tin, tout, self.prompts.label)
