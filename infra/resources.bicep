@description('Azure region for all resources.')
param location string

@description('azd environment name (e.g. drift-detector-dev) - drives resource naming per azure-naming-conventions.md, and seeds the short token used only where global uniqueness is required.')
param environmentName string

param tags object

@description('Days to suppress repeat alerts while drift is ongoing.')
param alertCooldownDays int

@description('Email address that receives drift and setup-error notifications.')
param notificationEmail string

@description('Resource group holding the reference storage account this detector watches. The Function only ever reads from here.')
param targetResourceGroupName string

// Only storage accounts and Function Apps need azd's uniqueness token appended -
// both have globally-unique naming requirements the rest of these resources don't.
// See azure-naming-conventions.md.
var resourceToken = toLower(uniqueString(subscription().id, environmentName, location))
var shortToken = substring(resourceToken, 0, 6)
var stateContainerName = 'state'

// SHARED DERIVATION - must stay byte-identical to reference/reference.bicep's
// referenceStorageName. That template CREATES the reference storage account; here
// we only recompute its name to hand the Function as TARGET_STORAGE_ACCOUNT_NAME,
// so no deployment output has to be copied between the two deploys.
var referenceStorageName = 'stddref${substring(uniqueString(subscription().id, 'drift-detector-reference'), 0, 10)}'

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  // Storage account names: lowercase alphanumeric only, no hyphens, <=24 chars.
  name: 'st${toLower(replace(environmentName, '-', ''))}${shortToken}'
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    supportsHttpsTrafficOnly: true
  }

  resource blobServices 'blobServices' = {
    name: 'default'

    resource stateContainer 'containers' = {
      name: stateContainerName
      properties: {
        publicAccess: 'None'
      }
    }
  }
}

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-${environmentName}'
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
    workspaceCapping: {
      dailyQuotaGb: json('1')
    }
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-${environmentName}'
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
    RetentionInDays: 30
  }
}

resource plan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: 'plan-${environmentName}'
  location: location
  tags: tags
  kind: 'functionapp'
  sku: {
    name: 'Y1'
    tier: 'Dynamic'
  }
  properties: {
    reserved: true
  }
}

resource functionApp 'Microsoft.Web/sites@2023-12-01' = {
  // Web App hostnames are globally unique, so this gets the short token too.
  name: 'func-${environmentName}-${shortToken}'
  location: location
  tags: union(tags, { 'azd-service-name': 'function' })
  kind: 'functionapp,linux'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: 'Python|3.11'
      appSettings: [
        {
          name: 'AzureWebJobsStorage'
          value: 'DefaultEndpointsProtocol=https;AccountName=${storage.name};AccountKey=${storage.listKeys().keys[0].value};EndpointSuffix=${environment().suffixes.storage}'
        }
        {
          name: 'FUNCTIONS_EXTENSION_VERSION'
          value: '~4'
        }
        {
          name: 'FUNCTIONS_WORKER_RUNTIME'
          value: 'python'
        }
        {
          // Required for Python on Linux Consumption: without this, a zip deploy
          // never runs Oryx to pip install requirements.txt, so the worker can't
          // import the function and reports zero triggers - looks "deployed" but
          // is silently non-functional. Cost Sentinel hit this exact failure.
          name: 'SCM_DO_BUILD_DURING_DEPLOYMENT'
          value: 'true'
        }
        {
          name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
          value: appInsights.properties.ConnectionString
        }
        {
          // The subscription the reference storage account lives in. The identity
          // only holds Reader on the reference resource group, so there is nothing
          // to discover at runtime - passing it as config lets the code stay off
          // the azure-mgmt-resource SDK entirely.
          name: 'AZURE_SUBSCRIPTION_ID'
          value: subscription().subscriptionId
        }
        {
          name: 'TARGET_RESOURCE_GROUP'
          value: targetResourceGroupName
        }
        {
          name: 'TARGET_STORAGE_ACCOUNT_NAME'
          value: referenceStorageName
        }
        {
          name: 'ALERT_COOLDOWN_DAYS'
          value: string(alertCooldownDays)
        }
        {
          name: 'STATE_STORAGE_ACCOUNT_NAME'
          value: storage.name
        }
        {
          name: 'STATE_CONTAINER_NAME'
          value: stateContainerName
        }
      ]
    }
  }
}

resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: 'ag-${environmentName}'
  location: 'global'
  tags: tags
  properties: {
    groupShortName: 'driftdtct'
    enabled: true
    emailReceivers: [
      {
        name: 'primary'
        emailAddress: notificationEmail
        useCommonAlertSchema: true
      }
    ]
  }
}

// The Function's managed identity only ever holds Reader on the reference resource
// group - it has no permission to trigger an Action Group directly. Instead the
// Function logs a plain-English trace to Application Insights (needs only the
// connection string, no RBAC), and these Log Alerts watch for that trace and fire
// the Action Group. Scoped to the App Insights resource itself, not the underlying
// Log Analytics workspace: only the App Insights scope exposes the classic "traces"
// table alias with camelCase columns (Cost Sentinel's first provision failed here).

resource driftAlertRule 'Microsoft.Insights/scheduledQueryRules@2022-06-15' = {
  name: 'alert-drift-${environmentName}'
  location: location
  tags: tags
  properties: {
    displayName: 'Drift Detector - drift detected'
    description: 'Fires when the Function logs a DriftDetected trace to Application Insights.'
    severity: 3
    enabled: true
    evaluationFrequency: 'PT1H'
    windowSize: 'PT1H'
    scopes: [
      appInsights.id
    ]
    criteria: {
      allOf: [
        {
          query: 'traces | where message startswith "DriftDetected:"'
          timeAggregation: 'Count'
          operator: 'GreaterThan'
          threshold: 0
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    autoMitigate: true
    actions: {
      actionGroups: [
        actionGroup.id
      ]
    }
  }
}

resource setupErrorAlertRule 'Microsoft.Insights/scheduledQueryRules@2022-06-15' = {
  name: 'alert-setup-${environmentName}'
  location: location
  tags: tags
  properties: {
    displayName: 'Drift Detector - setup error (detector cannot see its target)'
    description: 'Fires when the Function logs a DriftDetectorSetupError trace - the reference resource group or storage account is missing.'
    severity: 2
    enabled: true
    evaluationFrequency: 'PT1H'
    windowSize: 'PT1H'
    scopes: [
      appInsights.id
    ]
    criteria: {
      allOf: [
        {
          query: 'traces | where message startswith "DriftDetectorSetupError:"'
          timeAggregation: 'Count'
          operator: 'GreaterThan'
          threshold: 0
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    autoMitigate: true
    actions: {
      actionGroups: [
        actionGroup.id
      ]
    }
  }
}

output functionAppName string = functionApp.name
output storageAccountName string = storage.name
output functionPrincipalId string = functionApp.identity.principalId
