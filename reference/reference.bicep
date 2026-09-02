// Reference "intended state" for the Infrastructure Drift Detector.
//
// Deployed BY HAND into rg-drift-detector-reference-dev:
//   az group create -n rg-drift-detector-reference-dev -l eastus2 \
//     --tags portfolio=azure-devops-portfolio project=drift-detector environment=dev
//   az deployment group create -g rg-drift-detector-reference-dev \
//     --template-file reference/reference.bicep
//
// Deploy this BEFORE `azd up` on the detector - the detector's main.bicep creates
// a Reader role assignment scoped to this resource group and will fail if it
// doesn't exist yet.
//
// Every watched property below is an INLINE LITERAL on purpose: this template is
// compiled to function/reference_template.json and the Function reads these exact
// values as the "intended state". Do NOT hoist the tags or the watched properties
// into a `var` - Bicep compiles a `var` reference to an ARM expression
// (`[variables('tags')]`), which the runtime comparison can't read.

@description('Azure region for the reference storage account.')
param location string = resourceGroup().location

// SHARED DERIVATION - must stay byte-identical to the same expression in
// infra/resources.bicep, which recomputes this name to wire
// TARGET_STORAGE_ACCOUNT_NAME without copying an output between the two deploys.
// 'stddref' (7) + 10-char token = 17 chars, under the 24-char storage limit.
var referenceStorageName = 'stddref${substring(uniqueString(subscription().id, 'drift-detector-reference'), 0, 10)}'

resource referenceStorage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: referenceStorageName
  location: location
  tags: {
    portfolio: 'azure-devops-portfolio'
    project: 'drift-detector'
    environment: 'dev'
  }
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    supportsHttpsTrafficOnly: true
  }
}

output referenceStorageAccountName string = referenceStorage.name
