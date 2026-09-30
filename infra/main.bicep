// CR on Azure Container Apps.
//
// The shape follows from one fact: a code reviewer is idle almost all the time
// and expensive for a minute at a stretch. So the ingress is always-on and
// tiny, the worker scales to zero and is sized for a review, and they are
// separate revisions of the same image talking through Service Bus.
//
//   deploy:  az deployment group create -g <rg> -f infra/main.bicep -p @infra/params.json

targetScope = 'resourceGroup'

@description('Short name prefix for every resource. Lowercase letters and digits.')
@minLength(3)
@maxLength(12)
param name string = 'crreview'

param location string = resourceGroup().location

@description('PostgreSQL region. Can differ when this subscription restricts database provisioning in the app region.')
param pgLocation string = location

@description('Public HTTPS origin of the web app. Set after the first deployment returns the ingress FQDN.')
param publicUrl string = ''

@description('Container image, e.g. myacr.azurecr.io/cr:abc1234. Set by CI.')
param image string

@description('Administrator login for Postgres.')
param pgAdmin string = 'cradmin'

@secure()
@description('Administrator password for Postgres.')
param pgPassword string

@secure()
@description('GitHub App private key (PEM).')
param githubAppPrivateKey string

@description('GitHub App id.')
param githubAppId string

@secure()
@description('Webhook secret. Without it every delivery is rejected.')
param githubWebhookSecret string

@description('OAuth client id for dashboard sign-in.')
param githubClientId string

@secure()
param githubClientSecret string

@secure()
@description('Azure AI / Foundry key used for the model calls.')
param azureApiKey string

@description('Foundry resource name hosting the Claude deployments.')
param azureResource string

@description('Comma-separated GitHub logins seeded as administrators.')
param adminLogins string

var tags = { application: 'cr', component: 'code-review' }

// --- registry ---------------------------------------------------------------

resource acr 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  name: '${name}acr'
  location: location
  tags: tags
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: false }
}

// --- identity ---------------------------------------------------------------
// One user-assigned identity for both apps, so ACR pull is granted once.

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${name}-id'
  location: location
  tags: tags
}

// AcrPull, so Container Apps can pull without a stored registry password.
resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: acr
  name: guid(acr.id, identity.id, 'acrpull')
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '7f951dda-4ed3-4680-a7ca-43fe172d538d' // AcrPull
    )
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// --- storage ----------------------------------------------------------------
// Azure Files persists symbol graphs and review results across worker restarts.
// Git mirrors live on each worker's local filesystem because Git needs chmod,
// which is unsupported on the SMB mount. Blob would add another integration.

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: '${name}store'
  location: location
  tags: tags
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
  }
}

resource fileService 'Microsoft.Storage/storageAccounts/fileServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource cacheShare 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-05-01' = {
  parent: fileService
  name: 'cr-cache'
  properties: {
    // Mirrors of every reviewed repo live here. 100 GiB is generous now and
    // cheap to raise; running out looks like a clone failure, not a quota error.
    shareQuota: 100
    enabledProtocols: 'SMB'
  }
}

// --- queue ------------------------------------------------------------------

resource sb 'Microsoft.ServiceBus/namespaces@2022-10-01-preview' = {
  name: '${name}-bus'
  location: location
  tags: tags
  sku: { name: 'Basic', tier: 'Basic' }
}

resource jobsQueue 'Microsoft.ServiceBus/namespaces/queues@2022-10-01-preview' = {
  parent: sb
  name: 'cr-jobs'
  properties: {
    // A review can take minutes; the worker renews the lock while it runs, and
    // this is the ceiling for how long a dead worker can hold one.
    lockDuration: 'PT5M'
    maxDeliveryCount: 3
    // Dead-letter rather than drop: a job that fails three times is a bug
    // someone needs to see, not a message to lose quietly.
    deadLetteringOnMessageExpiration: true
    // A single review runs 30+ minutes, and a queue that backed up behind a
    // scaler outage takes longer still. An hour expired real work that nothing
    // would ever retry: the Postgres row stays 'queued' once the message is
    // gone, so the job is neither running nor recoverable.
    defaultMessageTimeToLive: 'PT6H'
  }
}

resource sbSend 'Microsoft.ServiceBus/namespaces/AuthorizationRules@2022-10-01-preview' = {
  parent: sb
  name: 'cr-app'
  properties: { rights: ['Send', 'Listen'] }
}

// KEDA queries queue runtime metadata, which needs Manage. Scope that key to
// this one queue, and keep it out of the application's environment variables.
resource sbScale 'Microsoft.ServiceBus/namespaces/queues/authorizationRules@2022-10-01-preview' = {
  parent: jobsQueue
  name: 'cr-scaler'
  properties: { rights: ['Manage', 'Send', 'Listen'] }
}

// --- database ---------------------------------------------------------------

resource pg 'Microsoft.DBforPostgreSQL/flexibleServers@2023-12-01-preview' = {
  name: '${name}-pg'
  location: pgLocation
  tags: tags
  sku: {
    // Burstable: the store sees a handful of queries per review, not a load.
    name: 'Standard_B1ms'
    tier: 'Burstable'
  }
  properties: {
    version: '16'
    administratorLogin: pgAdmin
    administratorLoginPassword: pgPassword
    storage: { storageSizeGB: 32 }
    backup: { backupRetentionDays: 7, geoRedundantBackup: 'Disabled' }
    highAvailability: { mode: 'Disabled' }
  }
}

resource pgDb 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2023-12-01-preview' = {
  parent: pg
  name: 'cr'
  properties: { charset: 'UTF8', collation: 'en_US.utf8' }
}

// Container Apps egress IPs are not fixed, so the alternative to this is a VNet
// integration. Kept explicit and narrow-ish rather than hidden.
resource pgAllowAzure 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2023-12-01-preview' = {
  parent: pg
  name: 'allow-azure-services'
  properties: { startIpAddress: '0.0.0.0', endIpAddress: '0.0.0.0' }
}

// --- container apps ---------------------------------------------------------

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${name}-logs'
  location: location
  tags: tags
  properties: { sku: { name: 'PerGB2018' }, retentionInDays: 30 }
}

resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${name}-env'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

resource envStorage 'Microsoft.App/managedEnvironments/storages@2024-03-01' = {
  parent: env
  name: 'crcache'
  properties: {
    azureFile: {
      accountName: storage.name
      accountKey: storage.listKeys().keys[0].value
      shareName: cacheShare.name
      // Web and worker can read the persisted graphs and review cache.
      accessMode: 'ReadWrite'
    }
  }
}

var pgHost = '${pg.name}.postgres.database.azure.com'
var dbUrl = 'postgresql+psycopg://${pgAdmin}:${uriComponent(pgPassword)}@${pgHost}:5432/cr?sslmode=require'
var sbConn = sbSend.listKeys().primaryConnectionString
var sbScaleConn = sbScale.listKeys().primaryConnectionString

// Environment shared by both revisions. Anything secret is referenced through
// the container app's own secret store rather than inlined here.
var sharedEnv = [
  { name: 'CR_PROVIDER', value: 'foundry' }
  { name: 'CR_AZURE_RESOURCE', value: azureResource }
  { name: 'CR_CACHE_DIR', value: '/cache' }
  { name: 'CR_SERVICEBUS_QUEUE', value: 'cr-jobs' }
  { name: 'CR_APP_REQUIRE_APPROVAL', value: 'true' }
  { name: 'CR_APP_ALLOW_SETUP', value: 'false' }
  { name: 'CR_GITHUB_APP_ID', value: githubAppId }
  { name: 'CR_GITHUB_CLIENT_ID', value: githubClientId }
  { name: 'CR_ADMIN_LOGINS', value: adminLogins }
  { name: 'CR_DB_URL', secretRef: 'db-url' }
  { name: 'CR_SERVICEBUS_CONNECTION', secretRef: 'sb-conn' }
  { name: 'CR_AZURE_API_KEY', secretRef: 'azure-api-key' }
  { name: 'CR_GITHUB_APP_PRIVATE_KEY', secretRef: 'gh-private-key' }
  { name: 'CR_GITHUB_WEBHOOK_SECRET', secretRef: 'gh-webhook-secret' }
  { name: 'CR_GITHUB_CLIENT_SECRET', secretRef: 'gh-client-secret' }
]

var sharedSecrets = [
  { name: 'db-url', value: dbUrl }
  { name: 'sb-conn', value: sbConn }
  { name: 'azure-api-key', value: azureApiKey }
  { name: 'gh-private-key', value: githubAppPrivateKey }
  { name: 'gh-webhook-secret', value: githubWebhookSecret }
  { name: 'gh-client-secret', value: githubClientSecret }
]

var cacheVolume = [
  {
    name: 'cache'
    storageType: 'AzureFile'
    storageName: envStorage.name
  }
]

var cacheMount = [
  { volumeName: 'cache', mountPath: '/cache' }
]

// The ingress. Small, always-on, and it never consumes the queue: a web replica
// that ran reviews would compete with the workers and do it on a box whose
// cache is cold.
resource web 'Microsoft.App/containerApps@2024-03-01' = {
  name: '${name}-web'
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  dependsOn: [acrPull]
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        // GitHub posts webhooks from the public internet; nothing else here
        // needs to be reachable, and the allowlist guards what gets acted on.
        allowInsecure: false
      }
      registries: [
        { server: acr.properties.loginServer, identity: identity.id }
      ]
      secrets: sharedSecrets
    }
    template: {
      containers: [
        {
          name: 'web'
          image: image
          command: ['cr']
          args: concat(
            ['app', 'serve', '--host', '0.0.0.0', '--port', '8000', '--role', 'web'],
            empty(publicUrl) ? [] : ['--public-url', publicUrl]
          )
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: concat(sharedEnv, [{ name: 'CR_APP_ROLE', value: 'web' }])
          volumeMounts: cacheMount
          probes: [
            {
              type: 'Liveness'
              httpGet: { path: '/api/health', port: 8000 }
              initialDelaySeconds: 15
              periodSeconds: 30
            }
          ]
        }
      ]
      volumes: cacheVolume
      scale: {
        // Always one: a cold start during GitHub's ten-second webhook window
        // is a failed delivery.
        minReplicas: 1
        maxReplicas: 3
        rules: [
          {
            name: 'http'
            http: { metadata: { concurrentRequests: '20' } }
          }
        ]
      }
    }
  }
}

// The worker. Sized for a review, and asleep between them.
resource worker 'Microsoft.App/containerApps@2024-03-01' = {
  name: '${name}-worker'
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  dependsOn: [acrPull]
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      activeRevisionsMode: 'Single'
      registries: [
        { server: acr.properties.loginServer, identity: identity.id }
      ]
      secrets: concat(sharedSecrets, [{ name: 'sb-scale-conn', value: sbScaleConn }])
    }
    template: {
      containers: [
        {
          name: 'worker'
          image: image
          command: ['cr']
          args: ['app', 'worker']
          // Reviews hold a whole PR diff, a symbol graph slice and several
          // concurrent model responses in memory at once.
          resources: { cpu: json('1.0'), memory: '2Gi' }
          env: concat(sharedEnv, [
            { name: 'CR_APP_ROLE', value: 'worker' }
            // Azure Files is SMB and Git needs chmod during clone. Keep mirrors
            // on the worker's local filesystem; graphs and reviews stay on the share.
            { name: 'CR_MIRROR_DIR', value: '/tmp/cr-mirrors' }
          ])
          volumeMounts: cacheMount
        }
      ]
      volumes: cacheVolume
      scale: {
        // Zero when nobody has opened a pull request. This is where the cost
        // model lives: the expensive container only exists while it is working.
        minReplicas: 0
        maxReplicas: 5
        rules: [
          {
            name: 'queue-depth'
            custom: {
              type: 'azure-servicebus'
              metadata: {
                queueName: 'cr-jobs'
                messageCount: '1'
              }
              auth: [
                { secretRef: 'sb-scale-conn', triggerParameter: 'connection' }
              ]
            }
          }
        ]
      }
    }
  }
}

output webUrl string = 'https://${web.properties.configuration.ingress.fqdn}'
output webhookUrl string = 'https://${web.properties.configuration.ingress.fqdn}/webhook'
output acrLoginServer string = acr.properties.loginServer
output postgresHost string = pgHost
