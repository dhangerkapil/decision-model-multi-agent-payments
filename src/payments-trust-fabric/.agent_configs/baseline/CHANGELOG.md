# Writer prompt lineage (`.agent_configs/baseline/`)

`baseline/` is the prompt set that the hosted agent loads (`load_config()` with no candidate selected) and the starting
point for the next Agent Optimizer run (`eval.yaml` → `agent.config`).

| Release | Prompt set | How it was produced |
|---|---|---|
| 1.0 | Shared communications policy (`instructions.md`) and five writer skills (`skills/*/SKILL.md`) | Hand-written, then tuned with the Foundry Agent Optimizer and reviewed file by file. One optimizer rewrite was rejected because it let a writer choose outcomes, which breaks the rule that Microsoft-Decision-1 decides and LLMs only write. The protection-writer skill was refined after Foundry Insights findings: it maps each outcome to what actually happened, keeps callback questions open, and leaves `callback_script` empty for automatic warnings. |

Every draft records the prompt set that produced it as `prompt_config` (`baseline@<content hash>`) in the case record.
Add a row here whenever you change a file in `baseline/`.
