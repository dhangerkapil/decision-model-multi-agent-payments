"""Writer-agent prompts, resolved through the Agent Optimizer config.

`load_config()` returns the baseline in `.agent_configs/baseline/` (beside main.py), an applied candidate folder, or the
candidate the optimizer is evaluating. The shared communications policy (instructions.md) is prepended to the skill that
belongs to each LLM writer role, so the optimizer can tune the global policy and every role prompt independently.
"""

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

from azure.ai.agentserver.optimization import Skill, load_config, load_skills_from_dir

logger = logging.getLogger(__name__)

ROLE_SKILLS: dict[str, str] = {
    "consumer_notification_writer": "consumer-notification-writer",
    "dispute_case_writer": "dispute-case-writer",
    "underwriting_memo_writer": "underwriting-memo-writer",
    "protection_writer": "protection-writer",
    "general_assistant": "general-assistant",
}

_BASELINE_SKILLS = Path(__file__).resolve().parents[1] / ".agent_configs" / "baseline" / "skills"


@dataclass
class PromptBook:
    label: str
    source: str
    model: str | None
    shared: str
    skills: dict[str, Skill]
    extra: list[Skill] = field(default_factory=list)

    def instructions(self, role: str) -> str:
        skill = self.skills[ROLE_SKILLS[role]]
        parts = [self.shared, f"## Your role: {skill.name}\n{skill.body.strip()}"]
        # Skills the optimizer discovers (not tied to one role) apply to every writer.
        parts += [f"## Additional skill: {s.name}\n{s.description}\n\n{s.body.strip()}".strip() for s in self.extra]
        return "\n\n".join(p for p in parts if p)


def _label(source: str, candidate_id: str | None) -> str:
    if candidate_id:
        return candidate_id
    if source.startswith("env:"):
        return "optimizer-candidate"
    return Path(source.split(":", 1)[-1]).name or source


def load_prompt_book() -> PromptBook:
    config = load_config()
    if config is None:
        raise RuntimeError("No Agent Optimizer config found; expected .agent_configs/baseline/ beside main.py.")
    skills = list(config.skills) or (load_skills_from_dir(Path(config.skills_dir)) if config.skills_dir else [])
    by_name = {s.name: s for s in skills if s.body.strip()}
    missing = [name for name in ROLE_SKILLS.values() if name not in by_name]
    if missing:
        # A candidate may carry only the skills it changed; the others keep their baseline text.
        baseline = {s.name: s for s in load_skills_from_dir(_BASELINE_SKILLS) if s.body.strip()}
        for name in missing:
            if name not in baseline:
                raise RuntimeError(f"Skill '{name}' is missing from the optimizer config and the baseline.")
            by_name[name] = baseline[name]
    role_skill_names = set(ROLE_SKILLS.values())
    extra = [s for s in skills if s.name not in role_skill_names and s.body.strip()]
    digest = hashlib.sha256("\n\x00".join([(config.instructions or "").strip()]
                                          + [f"{n}:{by_name[n].body.strip()}" for n in sorted(role_skill_names)]
                                          + [f"{s.name}:{s.body.strip()}" for s in extra]).encode("utf-8")).hexdigest()[:8]
    book = PromptBook(label=f"{_label(config.source, config.candidate_id)}@{digest}", source=config.source, model=config.model,
                      shared=(config.instructions or "").strip(), skills=by_name, extra=extra)
    logger.info("Prompt config=%s source=%s model=%s shared_len=%d role_skills=%d extra_skills=%d baseline_fallbacks=%s",
                book.label, book.source, book.model, len(book.shared), len(role_skill_names), len(book.extra), missing or "none")
    return book
