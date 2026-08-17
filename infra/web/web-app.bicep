targetScope = 'resourceGroup'

param location string
param appServicePlanName string
param appServiceSkuName string
param appServiceSkuTier string
param webAppName string
param foundryAgentEndpoint string

@secure()
param appSessionSecret string

@secure()
param bootstrapPassword string

param tags object

var serviceName = 'my-chat-web'
var alwaysOn = !contains([
  'F1'
  'D1'
], appServiceSkuName)

resource appServicePlan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: appServicePlanName
  location: location
  kind: 'linux'
  tags: tags
  sku: {
    name: appServiceSkuName
    tier: appServiceSkuTier
    capacity: 1
  }
  properties: {
    reserved: true
  }
}

resource webApp 'Microsoft.Web/sites@2024-04-01' = {
  name: webAppName
  location: location
  kind: 'app,linux'
  tags: union(tags, {
    'azd-service-name': serviceName
  })
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: appServicePlan.id
    httpsOnly: true
    clientAffinityEnabled: false
    siteConfig: {
      linuxFxVersion: 'PYTHON|3.14'
      appCommandLine: 'python -m uvicorn main:app --host 0.0.0.0 --proxy-headers'
      alwaysOn: alwaysOn
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      scmMinTlsVersion: '1.2'
      http20Enabled: true
      healthCheckPath: '/healthz'
      appSettings: [
        {
          name: 'SCM_DO_BUILD_DURING_DEPLOYMENT'
          value: 'true'
        }
        {
          name: 'APP_ENV'
          value: 'production'
        }
        {
          name: 'APP_SESSION_SECRET'
          value: appSessionSecret
        }
        {
          name: 'MY_CHAT_BOOTSTRAP_PASSWORD'
          value: bootstrapPassword
        }
        {
          name: 'MY_CHAT_DATABASE_PATH'
          value: '/home/data/my-chat.db'
        }
        {
          name: 'MY_CHAT_UPLOAD_DIR'
          value: '/home/data/uploads'
        }
        {
          name: 'FOUNDRY_AGENT_ENDPOINT'
          value: foundryAgentEndpoint
        }
        {
          name: 'FOUNDRY_TOKEN_SCOPE'
          value: 'https://ai.azure.com/.default'
        }
        {
          name: 'ALLOWED_HOSTS'
          value: '${webAppName}.azurewebsites.net'
        }
        {
          name: 'WEBSITES_ENABLE_APP_SERVICE_STORAGE'
          value: 'true'
        }
        {
          name: 'FORWARDED_ALLOW_IPS'
          value: '*'
        }
        {
          name: 'PYTHONUNBUFFERED'
          value: '1'
        }
        {
          name: 'WEB_CONCURRENCY'
          value: '1'
        }
      ]
    }
  }
}

output appServicePlanName string = appServicePlan.name
output webAppName string = webApp.name
output webAppUrl string = 'https://${webApp.properties.defaultHostName}'
output principalId string = webApp.identity.principalId
