# Coding agent instructions

This repository is **Payments Trust Fabric**: a **Microsoft Foundry hosted agent** (code deploy, Python 3.13, Microsoft
Agent Framework workflow) where **Microsoft-Decision-1** makes the decisions and LLM agents only write text. It runs in
[Foundry Agent Service](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agents). Before working on Foundry
agents, read the Microsoft Foundry skill (see below).

## Key files

- `azure.yaml` — azd project: the hosted agent service `decision-payments-agent`, the `console` service (Azure Container
  Apps), and the `postdeploy` hook
- `infra/` — Bicep: resource group, Foundry resource and project, model deployments, monitoring, console, role
  assignments. Outputs become azd environment values that `azure.yaml` and the tools read.
- `scripts/grant_agent_access.py` — postdeploy hook: grants the agent identity Foundry User on the Foundry resource
- `src/payments-trust-fabric/main.py` — entry point (`ResponsesHostServer` with a per-request workflow factory)
- `src/payments-trust-fabric/trust_fabric/` — workflow graph, Microsoft-Decision-1 client and ledger, decision catalog,
  confidence policy, LLM writers
- `src/payments-trust-fabric/.agent_configs/baseline/` — writer prompts for the Agent Optimizer, loaded with `load_config()`
  in `trust_fabric/prompts.py`; lineage in `baseline/CHANGELOG.md`. Never deploy with `OPTIMIZATION_CANDIDATE_ID` or
  `OPTIMIZATION_LOCAL_DIR` set: azd then copies the candidate over `baseline/`.
- `src/payments-trust-fabric/trust_fabric/tooltrace.py` — returns every Microsoft-Decision-1 call (`microsoft_decision_1`)
  and policy step (`apply_trust_policy`) as a function call and output pair in the response
- `console/` — web console (FastAPI); its Dockerfile builds from the repository root and reuses `trust_fabric`
- `tools/` — synthetic data generator, evaluation and benchmark harnesses, smoke test
- `tools/foundry/` — Foundry evaluators (`evaluators/*.py`; Python-grader sandbox rules apply: no `re.compile`, read
  `item["sample.output_text"]`), batch evaluation, Insights, optimizer helpers
- `evals/`, `eval.yaml` — evaluation datasets, rubric definition, Agent Optimizer configuration (paths in `eval.yaml`
  resolve from the agent service folder)

## Development workflow

The **Azure Developer CLI (`azd`)** manages the lifecycle:

```bash
azd up                                                      # provision, deploy both services, run the postdeploy hook
azd ai agent run --no-client                                # run the agent locally on http://localhost:8088
azd ai agent invoke --local decision-payments-agent "AP-02" # test the local agent with a demo event
azd deploy decision-payments-agent                          # deploy a new agent version
azd ai agent invoke decision-payments-agent "AP-02"         # invoke the deployed agent
python tools/e2e_cloud.py --heroes 3 --firehose 0 --skip-calibrate   # smoke test
```

Keep `src/payments-trust-fabric/.env` (local only) in sync with `azd env get-values` for `FOUNDRY_PROJECT_ENDPOINT`,
`DECISION_*`, and `LLM_*`. After editing `tools/datagen/`, run `python tools/generate_data.py`.

## Rules for changes

- Microsoft-Decision-1 decides; LLMs only write. Don't let an LLM choose an outcome.
- Automatic outcomes must stay in the customer's favor; adverse outcomes go to a named human queue (`HUMAN_QUEUES` in
  `trust_fabric/policy.py`).
- Every `choice` question about a case keeps a `cannot_tell` option.
- Use full product names in user-facing text (Microsoft-Decision-1, Microsoft Foundry, Foundry Agent Service).
- Use only fictitious names in data and examples.

## Microsoft Foundry skill

Install the **Microsoft Foundry skill** for guided deployment, evaluation, and troubleshooting workflows:

```bash
npx skills add https://github.com/microsoft/azure-skills --skill microsoft-foundry
```

## References

- [Hosted agents overview](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agents)
- [Deploy and use Microsoft-Decision-1](https://learn.microsoft.com/azure/foundry/foundry-models/how-to/use-foundry-models-microsoft-decision)
