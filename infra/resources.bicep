// SAHAYA — resource group scope.
// Foundry resource + project, chat and embedding deployments, Azure AI Search,
// Log Analytics + Application Insights (connected to the Foundry project for
// tracing), and a role assignment so the person running azd can call the agents.

param location string
param tags object
param resourceToken string
param principalId string

param chatModelName string
param chatModelVersion string
param chatDeploymentSku string
param chatCapacity int
param embeddingModelName string
param embeddingModelVersion string
param embeddingDeploymentSku string
param embeddingCapacity int
param searchSku string

var aiAccountName = 'ais-${resourceToken}'
var projectName = 'sahaya'
var searchServiceName = 'srch-${resourceToken}'

// ---------------------------------------------------------------------------
// Observability
// ---------------------------------------------------------------------------

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-${resourceToken}'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-${resourceToken}'
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
  }
}

// ---------------------------------------------------------------------------
// Microsoft Foundry resource and project
// ---------------------------------------------------------------------------

resource aiAccount 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: aiAccountName
  location: location
  tags: tags
  kind: 'AIServices'
  sku: { name: 'S0' }
  identity: { type: 'SystemAssigned' }
  properties: {
    customSubDomainName: aiAccountName
    allowProjectManagement: true
    publicNetworkAccess: 'Enabled'
    // Local (key) auth stays on because the embedding client and build_index.py
    // use an API key. See README "Known limitations" for the keyless roadmap.
    disableLocalAuth: false
  }
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: aiAccount
  name: projectName
  location: location
  tags: tags
  identity: { type: 'SystemAssigned' }
  properties: {
    displayName: 'SAHAYA'
    description: 'Multi-agent disaster relief claim adjudication (sample).'
  }
}

// Every change to the Foundry account (project, both deployments, the App
// Insights connection) runs one after another. Azure allows only one
// operation at a time on an account; running them in parallel fails with
// "RequestConflict: Another operation is in progress".
resource chatDeployment 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: aiAccount
  name: chatModelName
  sku: {
    name: chatDeploymentSku
    capacity: chatCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: chatModelName
      version: chatModelVersion
    }
  }
  dependsOn: [ project ]
}

resource embeddingDeployment 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: aiAccount
  name: embeddingModelName
  sku: {
    name: embeddingDeploymentSku
    capacity: embeddingCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: embeddingModelName
      version: embeddingModelVersion
    }
  }
  dependsOn: [ chatDeployment ]
}

// Connects Application Insights to the Foundry resource so traces appear in the
// project's Tracing page.
resource appInsightsConnection 'Microsoft.CognitiveServices/accounts/connections@2025-06-01' = {
  parent: aiAccount
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
  dependsOn: [ embeddingDeployment ]
}

// ---------------------------------------------------------------------------
// Azure AI Search (norms index)
// ---------------------------------------------------------------------------

resource search 'Microsoft.Search/searchServices@2025-05-01' = {
  name: searchServiceName
  location: location
  tags: tags
  sku: { name: searchSku }
  properties: {
    replicaCount: 1
    partitionCount: 1
    hostingMode: 'Default'
    semanticSearch: 'free'
    // build_index.py and search_norms use the admin key today.
    authOptions: {
      aadOrApiKey: {
        aadAuthFailureMode: 'http401WithBearerChallenge'
      }
    }
  }
}

// ---------------------------------------------------------------------------
// Access for the person running azd
// ---------------------------------------------------------------------------

// Foundry User (formerly "Azure AI User"; same role ID): lets your az login
// identity call the agents through the Foundry project endpoint
// (FoundryChatClient + AzureCliCredential). Least-privilege role for
// building and testing agents.
var azureAiUserRoleId = '53ca6127-db72-4b80-b1b0-d745d6d5456d'

resource userAiRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(principalId)) {
  name: guid(aiAccount.id, principalId, azureAiUserRoleId)
  scope: aiAccount
  properties: {
    principalId: principalId
    principalType: 'User'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', azureAiUserRoleId)
  }
}

// ---------------------------------------------------------------------------
// Outputs (no secrets)
// ---------------------------------------------------------------------------

output aiAccountName string = aiAccount.name
output searchServiceName string = search.name
output projectEndpoint string = 'https://${aiAccountName}.services.ai.azure.com/api/projects/${project.name}'
output modelsEndpoint string = 'https://${aiAccountName}.services.ai.azure.com'
output chatDeploymentName string = chatDeployment.name
output embeddingDeploymentName string = embeddingDeployment.name
output searchEndpoint string = 'https://${search.name}.search.windows.net'
output appInsightsConnectionString string = appInsights.properties.ConnectionString
