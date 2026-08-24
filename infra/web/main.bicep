targetScope = 'subscription'

@description('Short environment name used in all my-chat resource names.')
@minLength(1)
@maxLength(16)
param environmentName string = 'dev'

@description('Azure region for the Python web application.')
param webLocation string = 'swedencentral'

@description('Resource group that contains the Foundry account and project.')
param foundryResourceGroupName string

@description('Existing Microsoft Foundry account name.')
param foundryAccountName string

@description('Existing Microsoft Foundry project name.')
param foundryProjectName string

@description('Deployed my-chat Hosted Agent Responses endpoint.')
param foundryAgentEndpoint string

@secure()
@minLength(32)
@description('Cookie signing secret. Supply only at deployment time.')
param appSessionSecret string

@secure()
@minLength(10)
@description('Temporary first-login password. Supply only at deployment time.')
param bootstrapPassword string

@description('App Service plan SKU name. F1 reproduces the cost-optimized deployment.')
param appServiceSkuName string = 'F1'

@description('App Service plan SKU tier.')
param appServiceSkuTier string = 'Free'

param tags object = {
  application: 'my-chat'
  environment: environmentName
  managedBy: 'bicep'
}

var webResourceGroupName = 'rg-my-chat-web-${environmentName}-swc'
var appServicePlanName = 'asp-my-chat-web-${environmentName}-swc'
var webAppName = take(
  'my-chat-web-${environmentName}-${uniqueString(subscription().subscriptionId, environmentName)}',
  60
)

resource webResourceGroup 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: webResourceGroupName
  location: webLocation
  tags: tags
}

module webApp './web-app.bicep' = {
  name: 'my-chat-web-${environmentName}'
  scope: webResourceGroup
  params: {
    location: webLocation
    appServicePlanName: appServicePlanName
    appServiceSkuName: appServiceSkuName
    appServiceSkuTier: appServiceSkuTier
    webAppName: webAppName
    foundryAgentEndpoint: foundryAgentEndpoint
    appSessionSecret: appSessionSecret
    bootstrapPassword: bootstrapPassword
    tags: tags
  }
}

module foundryRbac './foundry-rbac.bicep' = {
  name: 'my-chat-foundry-rbac-${environmentName}'
  scope: resourceGroup(foundryResourceGroupName)
  params: {
    foundryAccountName: foundryAccountName
    foundryProjectName: foundryProjectName
    principalId: webApp.outputs.principalId
  }
}

output webResourceGroupName string = webResourceGroup.name
output appServicePlanName string = webApp.outputs.appServicePlanName
output webAppName string = webApp.outputs.webAppName
output webAppUrl string = webApp.outputs.webAppUrl
output webPrincipalId string = webApp.outputs.principalId
output foundryRoleAssignmentId string = foundryRbac.outputs.roleAssignmentId
