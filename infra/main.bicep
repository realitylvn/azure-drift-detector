targetScope = 'subscription'

@minLength(1)
@maxLength(64)
@description('Name of the azd environment; used for resource naming and as the resource group name suffix.')
param environmentName string

@minLength(1)
@description('Azure region for all resources.')
param location string

@description('Days to suppress repeat alerts while drift is ongoing.')
param alertCooldownDays int = 3

@description('Email address that receives drift and setup-error notifications.')
param notificationEmail string

@description('Portfolio project slug, used only for the "project" tag value - see azure-naming-conventions.md.')
param projectSlug string = 'drift-detector'

@description('Environment tag value - see azure-naming-conventions.md. Distinct from the azd environment name.')
param environmentTag string = 'dev'

@description('Resource group holding the reference storage account. Must already exist - deploy reference/reference.bicep before this.')
param referenceResourceGroupName string = 'rg-drift-detector-reference-dev'

var tags = {
  'azd-env-name': environmentName
  portfolio: 'azure-devops-portfolio'
  project: projectSlug
  environment: environmentTag
}

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'rg-${environmentName}'
  location: location
  tags: tags
}

module resources 'resources.bicep' = {
  name: 'resources'
  scope: rg
  params: {
    location: location
    environmentName: environmentName
    tags: tags
    alertCooldownDays: alertCooldownDays
    notificationEmail: notificationEmail
    targetResourceGroupName: referenceResourceGroupName
  }
}

// Cross-resource-group: the Function's identity gets Reader on the *reference*
// resource group, not this one. That group is created outside azd (by hand, via
// reference/reference.bicep), so this deployment must run after it - the module
// resolves the existing group by name.
module referenceReader 'reference-rbac.bicep' = {
  name: 'reference-reader-assignment'
  scope: resourceGroup(referenceResourceGroupName)
  params: {
    functionPrincipalId: resources.outputs.functionPrincipalId
  }
}

output AZURE_RESOURCE_GROUP string = rg.name
output FUNCTION_APP_NAME string = resources.outputs.functionAppName
output STORAGE_ACCOUNT_NAME string = resources.outputs.storageAccountName
