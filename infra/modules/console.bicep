// Console: Azure Container Registry, a Container Apps environment and the console container app.
// azd builds the image remotely in the registry and deploys it to the container app tagged azd-service-name: console.

param location string
param tags object
param registryName string
param environmentName string
param containerAppName string
param logAnalyticsName string

@description('Resource ID, client ID and principal ID of the console user-assigned identity.')
param identityId string
param identityClientId string
param identityPrincipalId string

@description('Environment variables for the console container.')
param env array

@description('True when the container app already exists, so re-provisioning keeps the deployed image.')
param exists bool = false

var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'
var placeholderImage = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: logAnalyticsName
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: registryName
  location: location
  tags: tags
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
  }
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, identityId, acrPullRoleId)
  scope: registry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
    principalId: identityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource containerAppsEnvironment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: environmentName
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
  }
}

module currentImage 'container-image.bicep' = {
  name: '${containerAppName}-image'
  params: {
    exists: exists
    name: containerAppName
  }
}

resource containerApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: containerAppName
  location: location
  tags: union(tags, { 'azd-service-name': 'console' })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identityId}': {}
    }
  }
  properties: {
    managedEnvironmentId: containerAppsEnvironment.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
      }
      registries: [
        {
          server: registry.properties.loginServer
          identity: identityId
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'console'
          image: empty(currentImage.outputs.image) ? placeholderImage : currentImage.outputs.image
          resources: {
            cpu: json('1.0')
            memory: '2Gi'
          }
          env: concat(env, [
            {
              name: 'AZURE_CLIENT_ID'
              value: identityClientId
            }
          ])
        }
      ]
      // The review queue and the rate limiter live in memory, so the console runs a single replica.
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
    }
  }
  dependsOn: [
    acrPull
  ]
}

output registryName string = registry.name
output registryEndpoint string = registry.properties.loginServer
output consoleUrl string = 'https://${containerApp.properties.configuration.ingress.fqdn}'
