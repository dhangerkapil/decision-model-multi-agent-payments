"""Microsoft-Decision-1 client and Decision Ledger.

Calls the Foundry decision endpoint (`/providers/microsoft/v1/systemone`) with a JSON `state` and typed
questions (`noul`, `choice`, `score`), and records every decision for audit: question, answer,
probabilities, latency, tokens and cost.
"""

import asyncio
import os
import random
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx
from azure.identity import DefaultAzureCredential
from opentelemetry import trace

from .config import DECISION_PRICE_PER_M_INPUT

TOKEN_SCOPE = os.environ.get("DECISION_TOKEN_SCOPE", "https://ai.azure.com/.default")
_RETRYABLE = {408, 429, 500, 502, 503, 504}
_tracer = trace.get_tracer("payments_trust_fabric.decision")


class DecisionError(RuntimeError):
    """Raised when Microsoft-Decision-1 cannot return a usable decision."""


@dataclass
class DecisionRecord:
    stage: str
    agent: str
    questions: dict[str, dict[str, str]]
    answers: dict[str, dict[str, Any]]
    latency_ms: float
    input_tokens: int
    cost_usd: float
    group: str | None = None
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _compact_answer(raw: dict[str, Any]) -> dict[str, Any]:
    kind = raw.get("type")
    if kind == "noul":
        return {"type": "noul", "p": round(float(raw.get("noul", 0.0)), 4)}
    if kind == "choice":
        probs = {k: round(float(v), 4) for k, v in (raw.get("probabilities") or {}).items()}
        return {"type": "choice", "choice": raw.get("choice"), "confidence": round(float(raw.get("confidence", 0.0)), 4),
                "probabilities": probs}
    if kind == "score":
        legend = raw.get("legend") or {}
        value = float(raw.get("score", 0.0))
        label = legend.get(str(int(round(value))), "")
        probs = {legend.get(k, k): round(float(v), 4) for k, v in (raw.get("probabilities") or {}).items()}
        return {"type": "score", "score": round(value, 3), "label": label, "confidence": round(float(raw.get("confidence", 0.0)), 4),
                "levels": len(legend), "probabilities": probs}
    return {"type": kind or "unknown", "refused": True}


class _Bucket:
    """Token bucket that keeps one deployment under its requests-per-minute limit."""

    def __init__(self, rpm: int) -> None:
        self.rate = rpm / 60.0
        self.capacity = max(2.0, rpm / 6.0)
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self.cooldown_until = 0.0

    def _refill(self) -> None:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now

    def wait_time(self) -> float:
        self._refill()
        now = time.monotonic()
        if now < self.cooldown_until:
            return self.cooldown_until - now
        return 0.0 if self.tokens >= 1 else (1 - self.tokens) / self.rate


class DecisionClient:
    """Async client for Microsoft-Decision-1 with token caching, client-side rate limiting, retries and
    load balancing across one or more deployments (for example GlobalStandard + DataZoneStandard)."""

    def __init__(self, endpoint: str, deployment: str | list[str], credential: DefaultAzureCredential | None = None,
                 timeout_s: float = 30.0, max_retries: int = 6, rpm_per_deployment: int = 60) -> None:
        self._url = f"{endpoint.rstrip('/')}/providers/microsoft/v1/systemone"
        names = deployment if isinstance(deployment, list) else [d.strip() for d in deployment.split(",") if d.strip()]
        self._buckets = {name: _Bucket(rpm_per_deployment) for name in names}
        self._pick_lock = asyncio.Lock()
        self._credential = credential or DefaultAzureCredential()
        self._max_retries = max_retries
        self._token: str | None = None
        self._token_expires = 0.0
        self._token_lock = asyncio.Lock()
        self._http = httpx.AsyncClient(timeout=timeout_s, limits=httpx.Limits(max_connections=64, max_keepalive_connections=32))

    @property
    def deployment(self) -> str:
        return ",".join(self._buckets)

    async def _acquire(self) -> str:
        """Pick the deployment that can serve soonest and consume one token from it."""
        while True:
            async with self._pick_lock:
                name, wait = min(((n, b.wait_time()) for n, b in self._buckets.items()), key=lambda x: x[1])
                if wait <= 0:
                    self._buckets[name].tokens -= 1
                    return name
            await asyncio.sleep(min(wait, 2.0))

    def _cool_down(self, name: str, seconds: float) -> None:
        self._buckets[name].cooldown_until = time.monotonic() + seconds

    async def _bearer(self) -> str:
        if self._token and time.time() < self._token_expires - 300:
            return self._token
        async with self._token_lock:
            if self._token and time.time() < self._token_expires - 300:
                return self._token
            tok = await asyncio.to_thread(self._credential.get_token, TOKEN_SCOPE)
            self._token, self._token_expires = tok.token, float(tok.expires_on)
            return self._token

    async def decide_raw(self, state: Any, questions: dict[str, dict[str, Any]]) -> tuple[dict[str, Any], int, float]:
        """Return (answers, input_tokens, latency_ms) for one request."""
        attempt = 0
        while True:
            deployment = await self._acquire()
            body = {"model": deployment, "state": state, "questions": questions}
            headers = {"Authorization": f"Bearer {await self._bearer()}", "Content-Type": "application/json"}
            started = time.perf_counter()
            try:
                resp = await self._http.post(self._url, json=body, headers=headers)
            except httpx.TransportError as exc:
                if attempt >= self._max_retries:
                    raise DecisionError(f"Decision endpoint unreachable: {exc}") from exc
                attempt += 1
                await asyncio.sleep(min(8.0, 0.5 * 2 ** attempt) + random.random() * 0.2)
                continue
            latency_ms = (time.perf_counter() - started) * 1000
            if resp.status_code == 401 and attempt == 0:
                self._token = None
                attempt += 1
                continue
            if resp.status_code in _RETRYABLE and attempt < self._max_retries:
                attempt += 1
                retry_after = resp.headers.get("retry-after")
                delay = float(retry_after) if retry_after and retry_after.replace(".", "", 1).isdigit() else 0.5 * 2 ** attempt
                if resp.status_code == 429:
                    self._cool_down(deployment, min(30.0, delay))
                    continue
                await asyncio.sleep(min(10.0, delay) + random.random() * 0.2)
                continue
            if resp.status_code >= 400:
                raise DecisionError(f"Microsoft-Decision-1 returned HTTP {resp.status_code}: {resp.text[:300]}")
            payload = resp.json()
            answers = payload.get("answers")
            if not isinstance(answers, dict):
                raise DecisionError("Microsoft-Decision-1 response has no answers object.")
            missing = [q for q in questions if q not in answers]
            if missing:
                raise DecisionError(f"Microsoft-Decision-1 did not answer: {missing}")
            tokens = int((payload.get("usage") or {}).get("input_tokens", 0))
            return answers, tokens, latency_ms

    async def decide(self, *, stage: str, agent: str, state: Any, questions: dict[str, dict[str, Any]],
                     group: str | None = None) -> DecisionRecord:
        with _tracer.start_as_current_span(f"decision-1 {stage}") as span:
            span.set_attribute("gen_ai.system", "microsoft.decision")
            span.set_attribute("gen_ai.request.model", self.deployment)
            span.set_attribute("decision.stage", stage)
            span.set_attribute("decision.agent", agent)
            span.set_attribute("decision.questions", ",".join(questions))
            answers, tokens, latency_ms = await self.decide_raw(state, questions)
            record = DecisionRecord(
                stage=stage,
                agent=agent,
                questions={k: {"type": v["type"], "instructions": v["instructions"]} for k, v in questions.items()},
                answers={k: _compact_answer(v) for k, v in answers.items()},
                latency_ms=round(latency_ms, 1),
                input_tokens=tokens,
                cost_usd=tokens * DECISION_PRICE_PER_M_INPUT / 1_000_000,
                group=group,
            )
            span.set_attribute("gen_ai.usage.input_tokens", tokens)
            span.set_attribute("decision.latency_ms", record.latency_ms)
            for name, ans in record.answers.items():
                value = ans.get("choice", ans.get("p", ans.get("score")))
                span.set_attribute(f"decision.answer.{name}", str(value))
            return record

    async def aclose(self) -> None:
        await self._http.aclose()


@dataclass
class Ledger:
    """All decisions and LLM calls made for one case."""

    decisions: list[DecisionRecord] = field(default_factory=list)
    llm_calls: list[dict[str, Any]] = field(default_factory=list)

    def add(self, record: DecisionRecord) -> DecisionRecord:
        self.decisions.append(record)
        return record

    @property
    def decision_count(self) -> int:
        return sum(len(r.answers) for r in self.decisions)

    @property
    def decision_cost_usd(self) -> float:
        return sum(r.cost_usd for r in self.decisions)

    @property
    def decision_tokens(self) -> int:
        return sum(r.input_tokens for r in self.decisions)

    def decision_wall_ms(self) -> float:
        """Latency on the critical path: parallel groups count once (their slowest call)."""
        total, groups = 0.0, {}
        for r in self.decisions:
            if r.group:
                groups[r.group] = max(groups.get(r.group, 0.0), r.latency_ms)
            else:
                total += r.latency_ms
        return total + sum(groups.values())


def noul(record: DecisionRecord, name: str) -> float:
    return float(record.answers[name].get("p", 0.0))


def choice(record: DecisionRecord, name: str) -> tuple[str, float]:
    a = record.answers[name]
    return str(a.get("choice")), float(a.get("confidence", 0.0))


def score(record: DecisionRecord, name: str) -> tuple[float, str]:
    a = record.answers[name]
    return float(a.get("score", 0.0)), str(a.get("label", ""))
