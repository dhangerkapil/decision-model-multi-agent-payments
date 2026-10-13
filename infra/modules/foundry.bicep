// Microsoft Foundry resource and project, model deployments (Microsoft-Decision-1 and the writer LLMs), the Application
// Insights connection used for tracing, and data-plane role assignments.

param location string
param tags object
param accountName string
param projectName string
param appInsightsName string

@description('Model deployments: name, format, model, version, sku, capacity. Created one at a time.')
param deployments array

@description('Object ID of the user or service principal running the deployment. Empty to skip.')
param principalId string = ''

@allowed([
  'User'
  'ServicePrincipal'
])
param principalType string = 'User'

@description('Principal ID of the console managed identity.')
param consolePrincipalId string

// Foundry User (formerly Azure AI User): model inference and agents. The person deploying gets Cognitive Services User,
// a different role than the Foundry User assignment the platform can create for a project's creator, to avoid a conflict.
var foundryUserRoleId = '53ca6127-db72-4b80-b1b0-d745d6d5456d'
var cognitiveServicesUserRoleId = 'a97b65f3-24c7-4388-baec-2e87135dc908'

resource appInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: appInsightsName
}

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: accountName
  location: location
  tags: tags
  kind: 'AIServices'
  sku: {
    name: 'S0'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    customSubDomainName: accountName
    allowProjectManagement: true
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: true
  }
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: account
  name: projectName
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    displayName: 'Payments Trust Fabric'
    description: 'Hosted multi-agent workflow: Microsoft-Decision-1 makes the decisions, LLM agents write, people own adverse actions.'
  }
}

@batchSize(1)
resource modelDeployments 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = [for d in deployments: {
  parent: account
  name: d.name
  sku: {
    name: d.sku
    capacity: d.capacity
  }
  properties: {
    model: {
      format: d.format
      name: d.model
      version: d.version
    }
  }
  dependsOn: [
    project
  ]
}]

resource appInsightsConnection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-06-01' = {
  parent: project
  name: 'appinsights'
  properties: {
    category: 'AppInsights'
    target: appInsights.id
    authType: 'ApiKey'
    isSharedToAll: true
    credentials: {
      key: appInsights.properties.ConnectionString
    }
    metadata: {
      ApiType: 'Azure'
      ResourceId: appInsights.id
    }
  }
}

resource consoleFoundryUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(account.id, consolePrincipalId, foundryUserRoleId)
  scope: account
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', foundryUserRoleId)
    principalId: consolePrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource deployerDataAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(principalId)) {
  name: guid(account.id, principalId, cognitiveServicesUserRoleId)
  scope: account
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesUserRoleId)
    principalId: principalId
    principalType: principalType
  }
}

output accountId string = account.id
output accountName string = account.name
output accountEndpoint string = 'https://${account.properties.customSubDomainName}.services.ai.azure.com'
output projectId string = project.id
output projectName string = project.name
output projectEndpoint string = 'https://${account.properties.customSubDomainName}.services.ai.azure.com/api/projects/${project.name}'
