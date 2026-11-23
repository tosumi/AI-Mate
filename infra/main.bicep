// =====================================================================
// MyCompanion の Azure リソース(本体仕様書 §11.5)
//
// - 対象はリソースグループ。サブスクリプションID・リソースグループ名は、ここに書かない
//   (デプロイのコマンドの引数で渡す。§11.5.9)。
// - 認証は、すべて Entra ID(マネージド ID / 開発者のアカウント)。キーやパスワードは使わない。
// - dev では、開発者の PC から直接つなぐため、データストアを「開発者の IP だけ許可」で公開する。
//   stg / prod では、プライベートエンドポイントと VNet 統合にする(§11.5.6。未実装。M5 までに追加)。
// =====================================================================

targetScope = 'resourceGroup'

@description('環境名(dev / stg / prod)')
@allowed(['dev', 'stg', 'prod'])
param envName string

@description('リージョン。所在地の方針により、日本のリージョンに限る(§11.5.6)')
@allowed(['japaneast', 'japanwest'])
param location string = 'japaneast'

@description('リソース名の接頭辞')
@minLength(3)
@maxLength(10)
param namePrefix string = 'mycomp'

@description('開発者(またはデプロイを実行する主体)の Entra のオブジェクトID。dev のデータアクセス権と PostgreSQL の管理者に使う')
param devPrincipalId string

@description('開発者の Entra のサインイン名(UPN)。PostgreSQL の管理者ロール名になる')
param devPrincipalName string

@description('開発者の PC のグローバル IP アドレス。dev でだけ使う。空なら、ファイアウォールの許可を作らない')
param devClientIp string = ''

@description('Azure OpenAI のデプロイ。役割ごとのデプロイ名は、環境変数 AZURE_OPENAI_DEPLOYMENT_* と一致させる')
param openAiDeployments array = [
  // チャット系は Data Zone Standard(APAC 域内)。Global Standard は使わない(方針B)
  { name: 'gpt-5-4-mini', model: 'gpt-5.4-mini', version: '2026-03-17', sku: 'DataZoneStandard', capacity: 50 }
  { name: 'gpt-6-1-sol', model: 'gpt-6.1-sol', version: '2026-09-29', sku: 'DataZoneStandard', capacity: 50 }
  // 応答の生成の候補(§15.1 の M6-6 で評価)
  { name: 'gpt-6-2-sol', model: 'gpt-6.2-sol', version: '2026-11-17', sku: 'DataZoneStandard', capacity: 50 }
  // 埋め込みは Standard(リージョナル。東京内で処理)
  { name: 'text-embedding-3-large', model: 'text-embedding-3-large', version: '1', sku: 'Standard', capacity: 50 }
]

@description('Container Apps(API と定期ジョブ)を作るか。コンテナイメージを用意してから true にする(§15.1 の M6-4)')
param deployContainerApps bool = false

@description('アプリのコンテナイメージ(API と定期ジョブで共通。deployContainerApps が true のとき)')
param apiImage string = ''

@description('Container Apps のレジストリ(ACR)だけを先に作るか。イメージを作る前の1回目のデプロイで true にする')
param deployRegistry bool = false

// 一般ユーザーの認証(外部テナント)。秘密情報ではないが、アカウントの情報なので、環境変数から渡す(§11.5.9)
param entraExternalTenantId string = ''
param entraExternalAuthority string = ''
param entraApiAudience string = ''
param entraApiScope string = ''
@description('外部テナントのアカウントの削除に使うアプリ(未設定なら、削除は保留のまま。§15.1 の M6-3)')
param entraDirectoryClientId string = ''

@description('GitHub のリポジトリ(owner/name)。空でなければ、GitHub Actions からデプロイする ID を作る(§15.1 の M6-5)')
param githubRepository string = ''

@description('GitHub の OIDC のトークンの subject。GitHub が ID つきの形式(repo:owner@数字/name@数字:ref:...)を提示する場合に、その値を書く。空なら repo:owner/name:ref:refs/heads/main')
param githubOidcSubject string = ''

@description('Web の画面(Static Web Apps)を作るか(§15.1 の M7)')
param deployWeb bool = false

@description('Static Web Apps の場所。日本のリージョンは選べないため、アジア太平洋の eastasia(画面のファイルだけ。利用者のデータは置かない)')
@allowed(['eastasia'])
param webLocation string = 'eastasia'

@description('失敗の通知の宛先(メール)。空なら、警告の規則だけを作り、通知は送らない。値は .env に置き、コードに書かない')
param alertEmail string = ''

@description('応答の生成・感情フィルタのデプロイ名(2026-11-20 に gpt-6.2-sol を選定。§15.1 の M6-6)')
param draftDeployment string = 'gpt-6-2-sol'
param filterDeployment string = 'gpt-6-2-sol'

var suffix = uniqueString(resourceGroup().id)
var base = '${namePrefix}-${envName}'
var compact = '${namePrefix}${envName}${suffix}'
var isDev = envName == 'dev'
var tags = { app: 'mycompanion', env: envName }
var useRegistry = deployContainerApps || deployRegistry
// dev で Container Apps を使うときは、データストアへの「Azure 内からの接続」を許可する(2026-11-19 の決定。
// キー・パスワードは無効で、Entra ID の認証が必須。stg / prod はプライベートエンドポイントにする(M11))
var allowAzureServices = isDev && deployContainerApps

// 組み込みロールのID
var roles = {
  keyVaultSecretsUser: '4633458b-17de-408a-b874-0445c86b69e6'
  keyVaultSecretsOfficer: 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7'
  openAiUser: '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
  serviceBusDataOwner: '090c5cfd-751d-490a-894a-3ce6f1109419'
  acrPull: '7f951dda-4ed3-4680-a7ca-43fe172d538d'
  contributor: 'b24988ac-6180-42a0-ab88-20f7382dd24c'
  managedIdentityOperator: 'f1a07417-d97a-45cb-824c-7a7467783830'
}
// データへのアクセスを与える主体: アプリのマネージド ID と、dev では開発者。
// ループの対象は、デプロイの開始時に決まる値だけにする(マネージド ID の principalId は
// 作成後にしか決まらないので、ここには入れず、各リソースの中で参照する)
var dataPrincipals = concat(
  [{ key: 'app', type: 'ServicePrincipal' }],
  isDev ? [{ key: 'dev', type: 'User' }] : []
)

// ---------------------------------------------------------------------
// マネージド ID・監視
// ---------------------------------------------------------------------

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${base}-id'
  location: location
  tags: tags
}

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${base}-logs'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: '${base}-appi'
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logs.id
  }
}

// ---------------------------------------------------------------------
// Key Vault(秘密情報。§11.5.6)
// ---------------------------------------------------------------------

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: take('kv${compact}', 24)
  location: location
  tags: tags
  properties: {
    tenantId: subscription().tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    enableSoftDelete: true
    enablePurgeProtection: true
  }
}

resource kvAppRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: keyVault
  name: guid(keyVault.id, identity.id, roles.keyVaultSecretsUser)
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.keyVaultSecretsUser)
  }
}

resource kvDevRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (isDev) {
  scope: keyVault
  name: guid(keyVault.id, devPrincipalId, roles.keyVaultSecretsOfficer)
  properties: {
    principalId: devPrincipalId
    principalType: 'User'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.keyVaultSecretsOfficer)
  }
}

// Application Insights の接続文字列は、Key Vault 参照で注入する(§11.5.9)
resource appInsightsSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'applicationinsights-connection-string'
  properties: {
    value: appInsights.properties.ConnectionString
  }
}

// ---------------------------------------------------------------------
// PostgreSQL + pgvector(記憶・状態。§11.5.3)
// ---------------------------------------------------------------------

resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: '${base}-pg-${suffix}'
  location: location
  tags: tags
  sku: isDev ? { name: 'Standard_B1ms', tier: 'Burstable' } : { name: 'Standard_D2ds_v5', tier: 'GeneralPurpose' }
  properties: {
    version: '17'
    storage: { storageSizeGB: 32 }
    // Entra ID の認証だけを使う(パスワード認証は無効)
    authConfig: {
      activeDirectoryAuth: 'Enabled'
      passwordAuth: 'Disabled'
      tenantId: subscription().tenantId
    }
    backup: { backupRetentionDays: isDev ? 7 : 14, geoRedundantBackup: 'Disabled' }
    highAvailability: { mode: 'Disabled' }
    network: { publicNetworkAccess: isDev ? 'Enabled' : 'Disabled' }
  }
}

// マイグレーションを実行する管理者(開発者)。アプリは、このロールでは接続しない
resource postgresAdmin 'Microsoft.DBforPostgreSQL/flexibleServers/administrators@2024-08-01' = {
  parent: postgres
  name: devPrincipalId
  properties: {
    principalName: devPrincipalName
    principalType: 'User'
    tenantId: subscription().tenantId
  }
}

// 使える拡張の許可(pgvector、pgcrypto)
resource postgresExtensions 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2024-08-01' = {
  parent: postgres
  name: 'azure.extensions'
  dependsOn: [postgresAdmin]
  properties: {
    value: 'VECTOR,PGCRYPTO'
    source: 'user-override'
  }
}

resource postgresDb 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: postgres
  name: 'mycompanion'
  dependsOn: [postgresExtensions]
  properties: { charset: 'UTF8', collation: 'en_US.utf8' }
}

resource postgresDevFirewall 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = if (isDev && !empty(devClientIp)) {
  parent: postgres
  name: 'dev-client'
  dependsOn: [postgresDb]
  properties: { startIpAddress: devClientIp, endIpAddress: devClientIp }
}

resource postgresAzureFirewall 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = if (allowAzureServices) {
  parent: postgres
  name: 'allow-azure-services'
  dependsOn: [postgresDb, postgresDevFirewall]
  properties: { startIpAddress: '0.0.0.0', endIpAddress: '0.0.0.0' } // Azure 内からの接続(Azure の規約の書き方)
}

// ---------------------------------------------------------------------
// Cosmos DB for NoSQL(turns・emotion_events・security_events。§11.5.3)
// ---------------------------------------------------------------------

resource cosmos 'Microsoft.DocumentDB/databaseAccounts@2024-11-15' = {
  name: '${base}-cosmos-${suffix}'
  location: location
  tags: tags
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    locations: [{ locationName: location, failoverPriority: 0 }]
    capabilities: isDev ? [{ name: 'EnableServerless' }] : []
    disableLocalAuth: true // キーでの接続を無効にする
    enableAutomaticFailover: true // 既存の設定に合わせる(単一リージョンなので、動作には影響しない)
    minimalTlsVersion: 'Tls12'
    publicNetworkAccess: isDev ? 'Enabled' : 'Disabled'
    // 0.0.0.0 は「Azure のデータセンター内からの接続を許可」(Container Apps を使う dev だけ)
    ipRules: concat(
      isDev && !empty(devClientIp) ? [{ ipAddressOrRange: devClientIp }] : [],
      allowAzureServices ? [{ ipAddressOrRange: '0.0.0.0' }] : []
    )
    backupPolicy: { type: 'Continuous', continuousModeProperties: { tier: 'Continuous7Days' } }
  }
}

resource cosmosDb 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-11-15' = {
  parent: cosmos
  name: 'mycompanion'
  properties: { resource: { id: 'mycompanion' } }
}

// パーティションキーは /userId。TTL は項目ごとに指定する(defaultTtl: -1)
resource cosmosContainers 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-11-15' = [
  for name in ['turns', 'emotion_events', 'security_events']: {
    parent: cosmosDb
    name: name
    properties: {
      resource: {
        id: name
        partitionKey: { paths: ['/userId'], kind: 'Hash' }
        defaultTtl: -1
      }
    }
  }
]

// データプレーンの権限(組み込みの「Cosmos DB Built-in Data Contributor」)
resource cosmosRoles 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-11-15' = [
  for p in dataPrincipals: {
    parent: cosmos
    name: guid(cosmos.id, p.key, 'data-contributor')
    properties: {
      principalId: p.key == 'app' ? identity.properties.principalId : devPrincipalId
      roleDefinitionId: '${cosmos.id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002'
      scope: cosmos.id
    }
  }
]

// ---------------------------------------------------------------------
// Service Bus(順序保証つきのキュー。§11.5.4)
// ---------------------------------------------------------------------

resource serviceBus 'Microsoft.ServiceBus/namespaces@2022-10-01-preview' = {
  name: '${base}-sb-${suffix}'
  location: location
  tags: tags
  sku: { name: 'Standard', tier: 'Standard' } // セッション機能に Standard 以上が必要
  properties: { disableLocalAuth: true }
}

resource jobsQueue 'Microsoft.ServiceBus/namespaces/queues@2022-10-01-preview' = {
  parent: serviceBus
  name: 'jobs'
  properties: {
    requiresSession: true // user_id をセッションにして、ユーザーごとに順序を保証する
    lockDuration: 'PT5M'
    maxDeliveryCount: 5
    deadLetteringOnMessageExpiration: true
  }
}

resource serviceBusRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for p in dataPrincipals: {
    scope: serviceBus
    name: guid(serviceBus.id, p.key, roles.serviceBusDataOwner)
    properties: {
      principalId: p.key == 'app' ? identity.properties.principalId : devPrincipalId
      principalType: p.type
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.serviceBusDataOwner)
    }
  }
]

// ---------------------------------------------------------------------
// Azure OpenAI(§11.1)
// ---------------------------------------------------------------------

resource openAi 'Microsoft.CognitiveServices/accounts@2024-10-01' = {
  name: '${base}-aoai-${suffix}'
  location: location
  tags: tags
  kind: 'OpenAI'
  sku: { name: 'S0' }
  properties: {
    customSubDomainName: '${base}-aoai-${suffix}'
    disableLocalAuth: true // API キーを無効にする
    publicNetworkAccess: 'Enabled'
  }
}

// ガードレール(コンテンツフィルタ)の方針(§13.5「注釈中心」)。
// - 入力のプロンプト攻撃(Jailbreak / Indirect Attack)は、遮断せず注釈だけにする。
//   既定の方針では遮断され、モデルに届かない。判定と段階的な対応は、アプリ側で行う(§8.8、§13.5.4)。
// - 入力の自傷は、遮断をできるだけ弱める(High のみ)。危機フローに渡すため(§13.5.2)。
//   「注釈のみ」にするには Microsoft への申請が必要(未申請)。High の遮断は、アプリ側の定型応答で受ける。
// - 出力は厳格にする(Medium 以上を遮断)。
resource raiPolicy 'Microsoft.CognitiveServices/accounts/raiPolicies@2024-10-01' = {
  parent: openAi
  name: 'mycompanion-annotate-first'
  properties: {
    mode: 'Default'
    basePolicyName: 'Microsoft.DefaultV2'
    contentFilters: [
      { name: 'Hate', source: 'Prompt', enabled: true, blocking: true, severityThreshold: 'High' }
      { name: 'Violence', source: 'Prompt', enabled: true, blocking: true, severityThreshold: 'High' }
      { name: 'Selfharm', source: 'Prompt', enabled: true, blocking: true, severityThreshold: 'High' }
      { name: 'Sexual', source: 'Prompt', enabled: true, blocking: true, severityThreshold: 'Medium' }
      { name: 'Jailbreak', source: 'Prompt', enabled: true, blocking: false }
      { name: 'Indirect Attack', source: 'Prompt', enabled: true, blocking: false }
      { name: 'Hate', source: 'Completion', enabled: true, blocking: true, severityThreshold: 'Medium' }
      { name: 'Violence', source: 'Completion', enabled: true, blocking: true, severityThreshold: 'Medium' }
      { name: 'Selfharm', source: 'Completion', enabled: true, blocking: true, severityThreshold: 'Medium' }
      { name: 'Sexual', source: 'Completion', enabled: true, blocking: true, severityThreshold: 'Medium' }
      { name: 'Protected Material Text', source: 'Completion', enabled: true, blocking: true }
    ]
  }
}

@batchSize(1) // デプロイは同時に作れないため、1つずつ作る
resource openAiModels 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = [
  for d in openAiDeployments: {
    parent: openAi
    name: d.name
    sku: { name: d.sku, capacity: d.capacity }
    properties: {
      model: { format: 'OpenAI', name: d.model, version: d.version }
      versionUpgradeOption: 'NoAutoUpgrade' // バージョンを固定し、評価してから切り替える(§11.1)
      raiPolicyName: raiPolicy.name
    }
  }
]

resource openAiRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for p in dataPrincipals: {
    scope: openAi
    name: guid(openAi.id, p.key, roles.openAiUser)
    properties: {
      principalId: p.key == 'app' ? identity.properties.principalId : devPrincipalId
      principalType: p.type
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.openAiUser)
    }
  }
]

// ---------------------------------------------------------------------
// Container Apps(API と定期ジョブ。§11.5.2、§15.1 の M6-4)
// ---------------------------------------------------------------------

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = if (useRegistry) {
  name: take('acr${compact}', 50)
  location: location
  tags: tags
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: false }
}

resource registryPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (useRegistry) {
  scope: registry
  name: guid(resourceGroup().id, identity.id, roles.acrPull)
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.acrPull)
  }
}

resource containerEnv 'Microsoft.App/managedEnvironments@2024-03-01' = if (deployContainerApps) {
  name: '${base}-cae'
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

// API と定期ジョブで共通の環境変数(値は、リソースの出力とパラメータから。秘密情報は Key Vault 参照)
var deploymentNames = {
  emotion: 'gpt-5-4-mini'
  planner: 'gpt-5-4-mini'
  memoryWrite: 'gpt-5-4-mini'
  consolidation: 'gpt-6-1-sol'
  embedding: 'text-embedding-3-large'
}
var appEnv = [
  { name: 'MYCOMPANION_ENV', value: envName }
  { name: 'MYCOMPANION_JOB_QUEUE', value: 'servicebus' }
  { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
  { name: 'AZURE_OPENAI_ENDPOINT', value: openAi.properties.endpoint }
  { name: 'AZURE_OPENAI_DEPLOYMENT_EMOTION', value: deploymentNames.emotion }
  { name: 'AZURE_OPENAI_DEPLOYMENT_PLANNER', value: deploymentNames.planner }
  { name: 'AZURE_OPENAI_DEPLOYMENT_MEMORY_WRITE', value: deploymentNames.memoryWrite }
  { name: 'AZURE_OPENAI_DEPLOYMENT_DRAFT', value: draftDeployment }
  { name: 'AZURE_OPENAI_DEPLOYMENT_FILTER', value: filterDeployment }
  { name: 'AZURE_OPENAI_DEPLOYMENT_CONSOLIDATION', value: deploymentNames.consolidation }
  { name: 'AZURE_OPENAI_DEPLOYMENT_EMBEDDING', value: deploymentNames.embedding }
  // デプロイ名 → モデル名と版。評価・ターンの記録に、実際に使ったモデルを残す(§11.1)
  { name: 'AZURE_OPENAI_MODELS', value: join(map(openAiDeployments, d => '${d.name}=${d.model}:${d.version}'), ',') }
  { name: 'POSTGRES_HOST', value: postgres.properties.fullyQualifiedDomainName }
  { name: 'POSTGRES_DATABASE', value: postgresDb.name }
  { name: 'POSTGRES_USER', value: identity.name }
  { name: 'COSMOS_ENDPOINT', value: cosmos.properties.documentEndpoint }
  { name: 'COSMOS_DATABASE', value: cosmosDb.name }
  { name: 'SERVICEBUS_FULLY_QUALIFIED_NAMESPACE', value: '${serviceBus.name}.servicebus.windows.net' }
  { name: 'KEY_VAULT_URI', value: keyVault.properties.vaultUri }
  { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', secretRef: 'appinsights' }
  { name: 'ENTRA_EXTERNAL_TENANT_ID', value: entraExternalTenantId }
  { name: 'ENTRA_EXTERNAL_AUTHORITY', value: entraExternalAuthority }
  { name: 'ENTRA_API_AUDIENCE', value: entraApiAudience }
  { name: 'ENTRA_API_SCOPE', value: entraApiScope }
  { name: 'ENTRA_DIRECTORY_CLIENT_ID', value: entraDirectoryClientId }
  // Web の画面の配信元(CORS。§15.1 の M7)。dev では、ローカルの開発用の画面も許す
  {
    name: 'MYCOMPANION_WEB_ORIGINS'
    value: deployWeb ? join(concat(['https://${web!.properties.defaultHostname}'], isDev ? ['http://localhost:5173'] : []), ',') : ''
  }
]
var appSecrets = [
  {
    name: 'appinsights'
    keyVaultUrl: appInsightsSecret.properties.secretUri
    identity: identity.id
  }
]

resource apiApp 'Microsoft.App/containerApps@2024-03-01' = if (deployContainerApps) {
  name: '${base}-api'
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    managedEnvironmentId: containerEnv.id
    configuration: {
      ingress: { external: true, targetPort: 8080, transport: 'auto' }
      registries: [{ server: registry!.properties.loginServer, identity: identity.id }]
      secrets: appSecrets
    }
    template: {
      containers: [
        {
          name: 'api'
          image: apiImage
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: appEnv
          probes: [
            { type: 'Liveness', httpGet: { path: '/healthz', port: 8080 } }
            { type: 'Readiness', httpGet: { path: '/readyz', port: 8080 } }
          ]
        }
      ]
      // 本番はコールドスタートを避けるため、最小1レプリカ(§11.5.2)
      scale: { minReplicas: isDev ? 0 : 1, maxReplicas: isDev ? 1 : 5 }
    }
  }
}

// 定期ジョブ(毎時): 計画 → キュー → 空くまで処理(§15.1 の M6-1、M6-4)。
// dev(v1.0)では、常駐のワーカーを置かず、このジョブがワーカーを兼ねる
resource cycleJob 'Microsoft.App/jobs@2024-03-01' = if (deployContainerApps) {
  name: '${base}-cycle'
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    environmentId: containerEnv.id
    configuration: {
      triggerType: 'Schedule'
      scheduleTriggerConfig: { cronExpression: '5 * * * *', parallelism: 1, replicaCompletionCount: 1 }
      replicaTimeout: 3300 // 次の回(1時間後)と重ならないように
      replicaRetryLimit: 0 // 失敗しても、次の回に計画し直す
      registries: [{ server: registry!.properties.loginServer, identity: identity.id }]
      secrets: appSecrets
    }
    template: {
      containers: [
        {
          name: 'cycle'
          image: apiImage
          command: ['python', '-m', 'mycompanion.worker', 'cycle']
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: appEnv
        }
      ]
    }
  }
}

// ---------------------------------------------------------------------
// GitHub Actions からのデプロイ(§15.1 の M6-5)
// - 秘密情報を GitHub に置かない: OIDC のフェデレーションで、main ブランチのワークフローだけを信頼する。
// - 権限は、イメージの作成(レジストリ)と、API・定期ジョブの更新に限る(データには触れない)。
// - マイグレーションは、ここでは行わない(管理者が手で実行する)。
// ---------------------------------------------------------------------

var useGithubDeploy = deployContainerApps && !empty(githubRepository)

resource deployIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = if (useGithubDeploy) {
  name: '${base}-deploy-id'
  location: location
  tags: tags
}

resource deployFederation 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = if (useGithubDeploy) {
  parent: deployIdentity
  name: 'github-main'
  properties: {
    issuer: 'https://token.actions.githubusercontent.com'
    subject: empty(githubOidcSubject) ? 'repo:${githubRepository}:ref:refs/heads/main' : githubOidcSubject
    audiences: ['api://AzureADTokenExchange']
  }
}

// イメージの作成(az acr build)に、レジストリの共同作成者が要る
resource deployRegistryRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (useGithubDeploy) {
  scope: registry
  name: guid(registry.id, '${base}-deploy-id', roles.contributor)
  properties: {
    principalId: deployIdentity!.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.contributor)
  }
}

resource deployApiRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (useGithubDeploy) {
  scope: apiApp
  name: guid(apiApp.id, '${base}-deploy-id', roles.contributor)
  properties: {
    principalId: deployIdentity!.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.contributor)
  }
}

resource deployJobRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (useGithubDeploy) {
  scope: cycleJob
  name: guid(cycleJob.id, '${base}-deploy-id', roles.contributor)
  properties: {
    principalId: deployIdentity!.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.contributor)
  }
}

// アプリの ID を付けたまま更新するために、ID の割り当ての権限が要る
resource deployIdentityRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (useGithubDeploy) {
  scope: identity
  name: guid(identity.id, '${base}-deploy-id', roles.managedIdentityOperator)
  properties: {
    principalId: deployIdentity!.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.managedIdentityOperator)
  }
}

// ---------------------------------------------------------------------
// Web の画面(Static Web Apps。§15.1 の M7)
// - 画面のファイルだけを置く(利用者のデータは置かない)。Free プラン。
// - 配信は GitHub Actions から。配信用の鍵は GitHub に置かず、デプロイ用の ID でその場で取り出す。
// ---------------------------------------------------------------------

resource web 'Microsoft.Web/staticSites@2023-12-01' = if (deployWeb) {
  name: '${base}-web'
  location: webLocation
  tags: tags
  sku: { name: 'Free', tier: 'Free' }
  properties: {
    allowConfigFileUpdates: true
    stagingEnvironmentPolicy: 'Disabled' // プルリクエストごとの公開の環境は作らない
  }
}

// デプロイ用の ID が、配信用の鍵を取り出して配信できるように
resource deployWebRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployWeb && useGithubDeploy) {
  scope: web
  name: guid(web.id, '${base}-deploy-id', roles.contributor)
  properties: {
    principalId: deployIdentity!.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.contributor)
  }
}

// ---------------------------------------------------------------------
// 失敗の通知(§15.1 の M6-8)。条件は、自前の計測(App Insights の件数と状態)だけを見る。本文は扱わない
// ---------------------------------------------------------------------

resource alertGroup 'Microsoft.Insights/actionGroups@2023-01-01' = if (deployContainerApps && !empty(alertEmail)) {
  name: '${base}-alerts'
  location: 'global'
  tags: tags
  properties: {
    groupShortName: take('mc${envName}', 12)
    enabled: true
    emailReceivers: [{ name: 'operator', emailAddress: alertEmail, useCommonAlertSchema: true }]
  }
}

var alertRules = [
  {
    name: 'cycle-not-running'
    description: '定期ジョブ(毎時)が、3時間動いていない(監査ログの削除のジョブの記録がない)'
    query: 'AppMetrics | where Name == "mycompanion.job.seconds" and tostring(Properties.kind) == "purge_audit_logs"'
    operator: 'LessThan'
    threshold: 1
    window: 'PT3H'
    frequency: 'PT1H'
    severity: 2
  }
  {
    name: 'job-failed'
    description: '直近1時間に、失敗したジョブがある(整理、セッションの終了、サインイン情報の削除など)'
    query: 'AppMetrics | where Name == "mycompanion.job.seconds" and tostring(Properties.status) == "failed"'
    operator: 'GreaterThan'
    threshold: 0
    window: 'PT1H'
    frequency: 'PT1H'
    severity: 2
  }
  {
    name: 'api-5xx'
    description: 'API の 500番台の応答が、15分間に5件を超えた'
    query: 'AppMetrics | where Name == "mycompanion.http.seconds" and toint(Properties.status) >= 500 | summarize n = sum(ItemCount) | where n > 5'
    operator: 'GreaterThan'
    threshold: 0
    window: 'PT15M'
    frequency: 'PT5M'
    severity: 1
  }
]

resource alerts 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = [
  for rule in alertRules: if (deployContainerApps) {
    name: '${base}-${rule.name}'
    location: location
    tags: tags
    properties: {
      displayName: '${base}-${rule.name}'
      description: rule.description
      severity: rule.severity
      enabled: true
      scopes: [logs.id]
      evaluationFrequency: rule.frequency
      windowSize: rule.window
      criteria: {
        allOf: [
          {
            query: rule.query
            timeAggregation: 'Count'
            operator: rule.operator
            threshold: rule.threshold
            failingPeriods: { numberOfEvaluationPeriods: 1, minFailingPeriodsToAlert: 1 }
          }
        ]
      }
      autoMitigate: true
      actions: { actionGroups: empty(alertEmail) ? [] : [alertGroup.id] }
    }
  }
]

// ---------------------------------------------------------------------
// 出力(.env に書く値。秘密情報は含まない)
// ---------------------------------------------------------------------

output AZURE_CLIENT_ID string = identity.properties.clientId
output APP_IDENTITY_NAME string = identity.name
output AZURE_OPENAI_ENDPOINT string = openAi.properties.endpoint
output POSTGRES_HOST string = postgres.properties.fullyQualifiedDomainName
output POSTGRES_DATABASE string = postgresDb.name
output COSMOS_ENDPOINT string = cosmos.properties.documentEndpoint
output COSMOS_DATABASE string = cosmosDb.name
output SERVICEBUS_FULLY_QUALIFIED_NAMESPACE string = '${serviceBus.name}.servicebus.windows.net'
output KEY_VAULT_URI string = keyVault.properties.vaultUri
output API_URL string = deployContainerApps ? 'https://${apiApp!.properties.configuration.ingress.fqdn}' : ''
output ACR_NAME string = useRegistry ? registry!.name : ''
output DEPLOY_CLIENT_ID string = useGithubDeploy ? deployIdentity!.properties.clientId : ''
output WEB_URL string = deployWeb ? 'https://${web!.properties.defaultHostname}' : ''
output WEB_NAME string = deployWeb ? web!.name : ''
output ACR_LOGIN_SERVER string = useRegistry ? registry!.properties.loginServer : ''
