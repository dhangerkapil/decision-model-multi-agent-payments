# Deployment guide

> [!NOTE]
> These steps deploy a demonstration environment with synthetic data. It isn't built for production; see
> [From demo to production](../README.md#from-demo-to-production).

- [Before you start](#before-you-start)
- [Deploy with azd](#deploy-with-azd)
- [Parameters](#parameters)
- [What gets deployed and who gets access](#what-gets-deployed-and-who-gets-access)
- [Deploy step by step](#deploy-step-by-step)
- [Update the agent or the console](#update-the-agent-or-the-console)
- [Clean up](#clean-up)
- [Troubleshooting](#troubleshooting)

## Before you start

1. **Permissions.** You need to create a resource group and role assignments in the subscription: **Owner**, or
   **Contributor** plus **Role Based Access Control Administrator** (or User Access Administrator).
2. **Region.** Pick a region that offers Microsoft-Decision-1, `gpt-5.4-mini`, `gpt-5.6-luna`, `gpt-6-sol`, and
   [hosted agents](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agents). Check model availability:

   ```bash
   az cognitiveservices model list --location <region> \
     --query "[?model.name=='Microsoft-Decision-1' || starts_with(model.name, 'gpt-')].{model:model.name, version:model.version}" -o table
   ```

3. **Quota.** Check that you have quota for each deployment type you choose. Global Standard quota is shared across
   regions in a subscription; Data Zone Standard quota is per data zone:

   ```bash
   az cognitiveservices usage list --location <region> \
     --query "[?contains(name.value, 'Microsoft-Decision-1') || contains(name.value, 'gpt-')].{name:name.value, used:currentValue, limit:limit}" -o table
   ```

   If quota is short, lower the capacities, switch the deployment type (see [Parameters](#parameters)), or request more
   quota in the Foundry portal.
4. **Tools.** Azure Developer CLI 1.27.1 or later with `azd extension install azure.ai.agents`, Azure CLI, and Python 3.10
   or later. Sign in to both CLIs with the same account: `azd auth login` and `az login`.

## Deploy with azd

```bash
azd env new <environment-name> --location <region>
azd env set AZURE_SUBSCRIPTION_ID <subscription-id>     # optional; azd prompts otherwise
azd up
```

`azd up` runs `azd provision` (Bicep in `infra/`), `azd deploy` (the hosted agent and the console), and the
`postdeploy` hook (`scripts/grant_agent_access.py`). Each step can also run on its own. Each environment gets its own
resource group, `rg-<environment-name>`, and every resource name includes a suffix derived from the subscription,
environment name, and region, so environments never collide.

Deployment outputs are written to the azd environment. The most useful ones:

| Value | Example |
|---|---|
| `CONSOLE_URL` | `https://ca-console-<suffix>.<region>.azurecontainerapps.io` |
| `AZURE_AI_PROJECT_ENDPOINT` | `https://aif-<suffix>.services.ai.azure.com/api/projects/proj-<suffix>` |
| `DECISION_ENDPOINT`, `DECISION_MODEL_DEPLOYMENT` | Foundry resource endpoint, Microsoft-Decision-1 deployment names |
| `AGENT_DECISION_PAYMENTS_AGENT_VERSION` | Deployed agent version (written by `azd deploy`) |

Read one with `azd env get-value <NAME>`, or all with `azd env get-values`.

## Parameters

Set with `azd env set <NAME> <value>` before `azd provision` or `azd up`. They map to parameters in
`infra/main.parameters.json`.

| azd environment value | Bicep parameter | Default | Notes |
|---|---|---|---|
| `AZURE_LOCATION` | `location` | (required) | Region for all resources |
| `DECISION_DEPLOYMENT_SKU` | `decisionSku` | `GlobalStandard` | `DataZoneStandard` keeps inference processing within the data zone (selected regions) |
| `DECISION_DEPLOYMENT_CAPACITY` | `decisionCapacity` | `60` | Also sets the agent's per-deployment rate limit (`DECISION_RPM_PER_DEPLOYMENT`) |
| `DECISION_SECOND_DEPLOYMENT_SKU` | `secondDecisionSku` | empty | Adds `decision-model-2`; the agent load-balances across both |
| `LLM_DEPLOYMENT_SKU` | `llmSku` | `GlobalStandard` | Deployment type of the three GPT writer models |
| `LLM_FAST_CAPACITY` | `llmFastCapacity` | `50` | `gpt-5.4-mini`, thousands of tokens per minute |
| `LLM_STANDARD_CAPACITY` | `llmStandardCapacity` | `50` | `gpt-5.6-luna` |
| `LLM_PREMIUM_CAPACITY` | `llmPremiumCapacity` | `20` | `gpt-6-sol` |

Model names and versions are parameters in `infra/main.bicep` (`llmFastModel`, `llmFastModelVersion`, and so on). If you
change a model, the agent and console pick up the new deployment names from the azd environment on the next deploy.

## What gets deployed and who gets access

| Resource | Notes |
|---|---|
| Foundry resource `aif-<suffix>` | `AIServices`, project management enabled, local (key) authentication disabled |
| Foundry project `proj-<suffix>` | Hosts the agent; Application Insights connection `appinsights` for tracing |
| Model deployments | Created one at a time to avoid deployment conflicts |
| Application Insights `appi-<suffix>`, Log Analytics `log-<suffix>` | Workspace-based Application Insights |
| Container registry `cr<suffix>` | Basic tier, admin user disabled |
| Managed identity `id-console-<suffix>` | Console identity |
| Container Apps environment `cae-<suffix>`, app `ca-console-<suffix>` | Port 8000, one replica |
| Hosted agent `decision-payments-agent` | Created by `azd deploy`; Foundry builds the code (`codeConfiguration` with `remote_build`) |

| Principal | Role | Scope | Why |
|---|---|---|---|
| Console identity | Foundry User | Foundry resource | Calls Microsoft-Decision-1 and invokes the hosted agent |
| Console identity | AcrPull | Container registry | Pulls the console image |
| Deploying user (`AZURE_PRINCIPAL_ID`) | Cognitive Services User | Foundry resource | Runs the tools, local agent, and invocations. Creating the project can also give you Foundry User automatically. |
| Hosted agent identity | Foundry User | Foundry resource | Calls Microsoft-Decision-1 on the resource endpoint (granted by the post-deploy hook) |

Model calls that go through the project endpoint are allowed for the agent identity by default; Microsoft-Decision-1 is
called on the resource endpoint, which is why the extra role is needed. See
[Hosted agent permissions](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agent-permissions).

## Deploy step by step

Use this path to review each stage, or to deploy into a pipeline.

1. **Provision the infrastructure** with azd (`azd provision`) or the Azure CLI:

   ```bash
   az deployment sub create --location <region> --template-file infra/main.bicep \
     --parameters environmentName=<environment-name> location=<region> principalId=$(az ad signed-in-user show --query id -o tsv)
   ```

   When you use the Azure CLI, copy the deployment outputs into the azd environment (`azd env set <NAME> <value>`) so the
   next steps can read them.
2. **Deploy the hosted agent**: `azd deploy decision-payments-agent`. Check it with
   `azd ai agent show decision-payments-agent`.
3. **Grant the agent access**: `python scripts/grant_agent_access.py`. It's idempotent, so you can run it any time.
4. **Deploy the console**: `azd deploy console`.
5. **Test**: `azd ai agent invoke decision-payments-agent "AP-02"`, then open `CONSOLE_URL`.

## Update the agent or the console

- **Agent code or prompts:** `azd deploy decision-payments-agent`. Each deploy creates a new immutable agent version; the
  post-deploy hook re-checks the agent's role assignment.
- **Console:** `azd deploy console`.
- **Synthetic data:** edit `tools/datagen/`, run `python tools/generate_data.py`, then deploy both services.
- **Baseline and benchmark tabs:** run `python tools/evaluate.py` and `python tools/benchmark.py` (they write
  `console/data/firehose_baseline.json` and `console/data/benchmark.json`), then `azd deploy console`.
- **Capacity or deployment types:** change the azd values and run `azd provision`.

## Clean up

```bash
azd down --purge
```

`--purge` permanently deletes the soft-deleted Foundry resource, which frees its name and quota right away.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `InsufficientQuota` or `QuotaExceeded` during provisioning | Not enough quota for a model deployment. Lower the capacity, switch `DECISION_DEPLOYMENT_SKU` or `LLM_DEPLOYMENT_SKU`, use another region, or request more quota. |
| `InvalidCapacity`: capacity should be at least 15 | Some models have a minimum deployment capacity. Raise the capacity value (for example `azd env set LLM_PREMIUM_CAPACITY 15`) and provision again. |
| A model or SKU isn't available in the region | Check `az cognitiveservices model list --location <region>` and choose another region or deployment type. |
| Cases come back `HUMAN_TRIAGE` with a Microsoft-Decision-1 401 or 403 error | The agent or console identity doesn't have its role yet. Run `python scripts/grant_agent_access.py` and wait 5 to 10 minutes for the role assignment to take effect. |
| `RoleAssignmentExists` during provisioning | The same principal already has the same role at that scope under another assignment. Remove the duplicate or the role assignment from the template, then provision again. |
| The post-deploy hook can't find `az` or `python` | Install the Azure CLI and Python, sign in with `az login`, and run `python scripts/grant_agent_access.py`. |
| The console shows a placeholder page | The console hasn't been deployed yet. Run `azd deploy console`. |
| The console's Baseline or Benchmark tab asks you to run a tool | Those tabs read optional result files. Run `python tools/evaluate.py` or `python tools/benchmark.py`, then `azd deploy console`. |
| The agent responds slowly the first time | Hosted agent sessions start on demand. Retry after 15 to 30 seconds. |
| Logs | `azd ai agent monitor decision-payments-agent` for the agent; Log Analytics or `az containerapp logs show` for the console. |
