// Payments Trust Fabric: hosted multi-agent workflow with Microsoft-Decision-1 in Microsoft Foundry.
// Creates a new resource group with a Foundry resource and project, model deployments, monitoring and the console.
// Deploy with `azd up` (see README.md). The hosted agent itself is deployed by `azd deploy` (azure.ai.agent service).

targetScope = 'subscription'

@minLength(1)
@maxLength(40)
@description('Name of the azd environment. Names the resource group and seeds the unique suffix of every resource.')
param environmentName string

@minLength(1)
@description('Region for all resources. Choose one that offers Microsoft-Decision-1, the GPT models and Foundry hosted agents.')
param location string

@description('Object ID of the user or service principal running the deployment (azd sets AZURE_PRINCIPAL_ID).')
param principalId string = ''

@allowed([
  'User'
  'ServicePrincipal'
])
param principalType string = 'User'

@description('Name of the hosted agent. Must match the agent service name and name in azure.yaml.')
param agentName string = 'decision-payments-agent'

@allowed([
  'GlobalStandard'
  'DataZoneStandard'
])
@description('Deployment type for Microsoft-Decision-1.')
param decisionSku string = 'GlobalStandard'

@description('Capacity of each Microsoft-Decision-1 deployment. The agent rate-limits itself to this many requests per minute per deployment.')
param decisionCapacity string = '60'

@allowed([
  ''
  'GlobalStandard'
  'DataZoneStandard'
])
@description('Optional second Microsoft-Decision-1 deployment for more throughput; the agent load-balances across both. Empty for none.')
param secondDecisionSku string = ''

@allowed([
  'GlobalStandard'
  'DataZoneStandard'
])
@description('Deployment type for the three GPT writer models.')
param llmSku string = 'GlobalStandard'

@description('Capacity (thousands of tokens per minute) of the fast, standard and premium writer models.')
param llmFastCapacity string = '50'
param llmStandardCapacity string = '50'
param llmPremiumCapacity string = '20'

param decisionModelVersion string = '1'
param llmFastModel string = 'gpt-5.4-mini'
param llmFastModelVersion string = '2026-03-17'
param llmStandardModel string = 'gpt-5.6-luna'
param llmStandardModelVersion string = '2026-07-09'
param llmPremiumModel string = 'gpt-6-sol'
param llmPremiumModelVersion string = '2026-09-22'

@description('Set by azd (SERVICE_CONSOLE_RESOURCE_EXISTS) so re-provisioning keeps the deployed console image.')
param consoleExists bool = false

var tags = {
  'azd-env-name': environmentName
  solution: 'payments-trust-fabric'
}
var resourceToken = toLower(uniqueString(subscription().id, environmentName, location))
var accountName = 'aif-${resourceToken}'
var projectName = 'proj-${resourceToken}'
var accountEndpoint = 'https://${accountName}.services.ai.azure.com'
var projectEndpoint = '${accountEndpoint}/api/projects/${projectName}'

var decisionDeployments = concat([
  {
    name: 'decision-model'
    format: 'Microsoft'
    model: 'Microsoft-Decision-1'
    version: decisionModelVersion
    sku: decisionSku
    capacity: int(decisionCapacity)
  }
], empty(secondDecisionSku) ? [] : [
  {
    name: 'decision-model-2'
    format: 'Microsoft'
    model: 'Microsoft-Decision-1'
    version: decisionModelVersion
    sku: secondDecisionSku
    capacity: int(decisionCapacity)
  }
])
var llmDeployments = [
  {
    name: llmFastModel
    format: 'OpenAI'
    model: llmFastModel
    version: llmFastModelVersion
    sku: llmSku
    capacity: int(llmFastCapacity)
  }
  {
    name: llmStandardModel
    format: 'OpenAI'
    model: llmStandardModel
    version: llmStandardModelVersion
    sku: llmSku
    capacity: int(llmStandardCapacity)
  }
  {
    name: llmPremiumModel
    format: 'OpenAI'
    model: llmPremiumModel
    version: llmPremiumModelVersion
    sku: llmSku
    capacity: int(llmPremiumCapacity)
  }
]
var decisionDeploymentNames = join(map(decisionDeployments, d => d.name), ',')

// Settings shared by the console container and the hosted agent (through azd environment values).
var appSettings = [
  {
    name: 'FOUNDRY_PROJECT_ENDPOINT'
    value: projectEndpoint
  }
  {
    name: 'DECISION_ENDPOINT'
    value: accountEndpoint
  }
  {
    name: 'DECISION_MODEL_DEPLOYMENT'
    value: decisionDeploymentNames
  }
  {
    name: 'DECISION_RPM_PER_DEPLOYMENT'
    value: decisionCapacity
  }
  {
    name: 'AZURE_AI_MODEL_DEPLOYMENT_NAME'
    value: llmFastModel
  }
  {
    name: 'LLM_STANDARD_DEPLOYMENT'
    value: llmStandardModel
  }
  {
    name: 'LLM_PREMIUM_DEPLOYMENT'
    value: llmPremiumModel
  }
  {
    name: 'AGENT_NAME'
    value: agentName
  }
]

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'rg-${environmentName}'
  location: location
  tags: tags
}

module monitoring 'modules/monitoring.bicep' = {
  name: 'monitoring'
  scope: rg
  params: {
    location: location
    tags: tags
    logAnalyticsName: 'log-${resourceToken}'
    appInsightsName: 'appi-${resourceToken}'
  }
}

module consoleIdentity 'modules/identity.bicep' = {
  name: 'console-identity'
  scope: rg
  params: {
    location: location
    tags: tags
    name: 'id-console-${resourceToken}'
  }
}

module foundry 'modules/foundry.bicep' = {
  name: 'foundry'
  scope: rg
  params: {
    location: location
    tags: tags
    accountName: accountName
    projectName: projectName
    appInsightsName: monitoring.outputs.appInsightsName
    deployments: concat(decisionDeployments, llmDeployments)
    principalId: principalId
    principalType: principalType
    consolePrincipalId: consoleIdentity.outputs.principalId
  }
}

module console 'modules/console.bicep' = {
  name: 'console'
  scope: rg
  params: {
    location: location
    tags: tags
    registryName: 'cr${resourceToken}'
    environmentName: 'cae-${resourceToken}'
    containerAppName: 'ca-console-${resourceToken}'
    logAnalyticsName: monitoring.outputs.logAnalyticsName
    identityId: consoleIdentity.outputs.id
    identityClientId: consoleIdentity.outputs.clientId
    identityPrincipalId: consoleIdentity.outputs.principalId
    env: concat(appSettings, [
      {
        name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
        value: monitoring.outputs.appInsightsConnectionString
      }
    ])
    exists: consoleExists
  }
}

// azd writes these outputs to the environment (.azure/<env>/.env); azure.yaml and the tools read them from there.
output AZURE_LOCATION string = location
output AZURE_TENANT_ID string = tenant().tenantId
output AZURE_RESOURCE_GROUP string = rg.name
output AZURE_AI_ACCOUNT_NAME string = foundry.outputs.accountName
output AZURE_AI_ACCOUNT_ID string = foundry.outputs.accountId
output AZURE_AI_PROJECT_NAME string = foundry.outputs.projectName
output AZURE_AI_PROJECT_ID string = foundry.outputs.projectId
output AZURE_AI_PROJECT_ENDPOINT string = foundry.outputs.projectEndpoint
output FOUNDRY_PROJECT_ENDPOINT string = foundry.outputs.projectEndpoint
output DECISION_ENDPOINT string = foundry.outputs.accountEndpoint
output DECISION_MODEL_DEPLOYMENT string = decisionDeploymentNames
output DECISION_RPM_PER_DEPLOYMENT string = decisionCapacity
output AZURE_AI_MODEL_DEPLOYMENT_NAME string = llmFastModel
output LLM_STANDARD_DEPLOYMENT string = llmStandardModel
output LLM_PREMIUM_DEPLOYMENT string = llmPremiumModel
output AZURE_CONTAINER_REGISTRY_NAME string = console.outputs.registryName
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = console.outputs.registryEndpoint
output APPLICATIONINSIGHTS_CONNECTION_STRING string = monitoring.outputs.appInsightsConnectionString
output CONSOLE_URL string = console.outputs.consoleUrl
