// SAHAYA — azd entry point (subscription scope).
// Creates one resource group and provisions everything SAHAYA needs into it.
// The Python app itself runs locally (python web_demo.py ...); this template
// provisions the Azure side only.
targetScope = 'subscription'

@minLength(1)
@maxLength(32)
@description('azd environment name. Used to name and tag every resource.')
param environmentName string

@minLength(1)
@description('Region for all resources. Pick one where both model deployments below are available.')
param location string

@description('Object ID of the person running azd, so they can call the agents with their own Entra ID sign-in. azd fills this automatically.')
param principalId string = ''

@description('Chat model for the five agents. Must accept image input (the Evidence agent uses vision).')
param chatModelName string = 'gpt-4o'

@description('Chat model version. Check availability for your region in the Foundry model catalog.')
param chatModelVersion string = '2024-11-20'

@description('Deployment type for the chat model.')
@allowed([ 'GlobalStandard', 'Standard', 'DataZoneStandard' ])
param chatDeploymentSku string = 'GlobalStandard'

@description('Chat model capacity in thousands of tokens per minute. The concurrent fan-out makes three calls at once, so keep this above the minimum.')
@minValue(1)
param chatCapacity int = 30

@description('Embedding model used to index the norms documents.')
param embeddingModelName string = 'text-embedding-3-large'

@description('Embedding model version.')
param embeddingModelVersion string = '1'

@description('Deployment type for the embedding model.')
@allowed([ 'GlobalStandard', 'Standard' ])
param embeddingDeploymentSku string = 'Standard'

@description('Embedding model capacity in thousands of tokens per minute.')
@minValue(1)
param embeddingCapacity int = 30

@description('Azure AI Search tier. Basic is the lowest tier that supports semantic ranking for this template.')
@allowed([ 'basic', 'standard' ])
param searchSku string = 'basic'

var tags = { 'azd-env-name': environmentName }
var resourceToken = toLower(uniqueString(subscription().id, environmentName, location))

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'rg-${environmentName}'
  location: location
  tags: tags
}

module resources 'resources.bicep' = {
  name: 'sahaya-resources'
  scope: rg
  params: {
    location: location
    tags: tags
    resourceToken: resourceToken
    principalId: principalId
    chatModelName: chatModelName
    chatModelVersion: chatModelVersion
    chatDeploymentSku: chatDeploymentSku
    chatCapacity: chatCapacity
    embeddingModelName: embeddingModelName
    embeddingModelVersion: embeddingModelVersion
    embeddingDeploymentSku: embeddingDeploymentSku
    embeddingCapacity: embeddingCapacity
    searchSku: searchSku
  }
}

// azd stores every output below as an environment value (azd env get-values).
// No keys are output here: the postprovision hook fetches them with az CLI
// and writes them only into your local, gitignored .env file.
output AZURE_LOCATION string = location
output AZURE_RESOURCE_GROUP string = rg.name
output AZURE_AI_ACCOUNT_NAME string = resources.outputs.aiAccountName
output AZURE_SEARCH_SERVICE_NAME string = resources.outputs.searchServiceName

// Names below match the variables SAHAYA's Python code already reads.
output FOUNDRY_PROJECT_ENDPOINT string = resources.outputs.projectEndpoint
output FOUNDRY_MODEL string = resources.outputs.chatDeploymentName
output FOUNDRY_MODELS_ENDPOINT string = resources.outputs.modelsEndpoint
output FOUNDRY_EMBEDDING_MODEL string = resources.outputs.embeddingDeploymentName
output SEARCH_ENDPOINT string = resources.outputs.searchEndpoint
output APPLICATIONINSIGHTS_CONNECTION_STRING string = resources.outputs.appInsightsConnectionString
