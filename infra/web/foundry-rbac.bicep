targetScope = 'resourceGroup'

param foundryAccountName string
param foundryProjectName string
param principalId string

var foundryUserRoleId = '53ca6127-db72-4b80-b1b0-d745d6d5456d'

resource foundryAccount 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: foundryAccountName
}

resource foundryProject 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' existing = {
  parent: foundryAccount
  name: foundryProjectName
}

resource foundryUserRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundryProject.id, principalId, foundryUserRoleId)
  scope: foundryProject
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      foundryUserRoleId
    )
    description: 'Allow the my-chat web app to invoke its Foundry Hosted Agent.'
  }
}

output roleAssignmentId string = foundryUserRole.id
