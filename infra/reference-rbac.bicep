// Deployed as a module from main.bicep, SCOPED TO rg-drift-detector-reference-dev
// (which must already exist - deploy reference/reference.bicep first). Grants the
// detector Function's managed identity Reader on the reference resource group and
// nothing else: it can read the target storage account's properties, and cannot
// modify anything anywhere.

@description('Principal ID of the detector Function App system-assigned identity.')
param functionPrincipalId string

@description('Built-in "Reader" role (verified via: az role definition list --name Reader --query "[0].name").')
var readerRoleId = 'acdd72a7-3385-48ef-bd42-f606fba81ae7'

resource readerAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, functionPrincipalId, readerRoleId)
  scope: resourceGroup()
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', readerRoleId)
    principalId: functionPrincipalId
    principalType: 'ServicePrincipal'
  }
}
