// デプロイのパラメータ。値は環境変数から読む(このファイルにアカウントの情報を書かない。§11.5.9)。
// 事前に `. .\scripts\load-env.ps1` で .env を読み込んでおく。
using './main.bicep'

param envName = readEnvironmentVariable('AZURE_ENV_NAME', 'dev')
param location = readEnvironmentVariable('AZURE_LOCATION', 'japaneast')
param devPrincipalId = readEnvironmentVariable('AZURE_DEV_PRINCIPAL_ID')
param devPrincipalName = readEnvironmentVariable('AZURE_DEV_PRINCIPAL_NAME')
param devClientIp = readEnvironmentVariable('AZURE_DEV_CLIENT_IP', '')
param deployRegistry = readEnvironmentVariable('AZURE_DEPLOY_REGISTRY', 'false') == 'true'
param deployContainerApps = readEnvironmentVariable('AZURE_DEPLOY_CONTAINER_APPS', 'false') == 'true'
param apiImage = readEnvironmentVariable('AZURE_APP_IMAGE', '')
param entraExternalTenantId = readEnvironmentVariable('ENTRA_EXTERNAL_TENANT_ID', '')
param entraExternalAuthority = readEnvironmentVariable('ENTRA_EXTERNAL_AUTHORITY', '')
param entraApiAudience = readEnvironmentVariable('ENTRA_API_AUDIENCE', '')
param entraApiScope = readEnvironmentVariable('ENTRA_API_SCOPE', '')
param entraDirectoryClientId = readEnvironmentVariable('ENTRA_DIRECTORY_CLIENT_ID', '')
param githubRepository = readEnvironmentVariable('AZURE_GITHUB_REPOSITORY', '')
param githubOidcSubject = readEnvironmentVariable('AZURE_GITHUB_OIDC_SUBJECT', '')
param alertEmail = readEnvironmentVariable('AZURE_ALERT_EMAIL', '')
param deployWeb = readEnvironmentVariable('AZURE_DEPLOY_WEB', 'false') == 'true'
param opsTenantId = readEnvironmentVariable('OPS_ENTRA_TENANT_ID', '')
param opsApiAudience = readEnvironmentVariable('OPS_ENTRA_API_AUDIENCE', '')
param opsApiScope = readEnvironmentVariable('OPS_ENTRA_API_SCOPE', '')
