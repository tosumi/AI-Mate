<#
.SYNOPSIS
  Azure の構成(infra/main.bicep)を、dev にデプロイする(本体仕様書 §11.5、infra/README.md)。

.DESCRIPTION
  Bicep は、Container Apps のイメージも指定する。.env の AZURE_APP_IMAGE が古いと、
  CI がデプロイした新しいイメージを、古いものに戻してしまう(2026-11-20 に実際に起きた)。
  このスクリプトは、デプロイの直前に、いま動いている API のイメージを読み取って使う。

  既定は what-if(変更内容の確認)だけ。-Apply を付けたときだけ、作成・変更する。

.EXAMPLE
  .\scripts\deploy-infra.ps1            # 変更内容の確認だけ
  .\scripts\deploy-infra.ps1 -Apply     # 確認のあと、デプロイする
#>
param(
    [switch]$Apply,
    [string]$Name = "infra-$(Get-Date -Format yyyyMMdd-HHmmss)"
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "load-env.ps1") | Out-Null

# いま動いている API のイメージ(CI のデプロイの途中なら、終わるのを待ってから実行すること)
$running = az containerapp show -g $env:AZURE_RESOURCE_GROUP -n "mycomp-$($env:AZURE_ENV_NAME)-api" `
    --query "properties.template.containers[0].image" -o tsv 2>$null
if ($running) {
    $env:AZURE_APP_IMAGE = $running
    Write-Host "使うイメージ(いま動いているもの): $running"
} else {
    Write-Host "API がまだないので、.env の AZURE_APP_IMAGE を使います"
}

$changes = az deployment group what-if --resource-group $env:AZURE_RESOURCE_GROUP `
    --parameters infra/main.bicepparam --no-pretty-print | ConvertFrom-Json
$changes.changes | Group-Object changeType | ForEach-Object { Write-Host "$($_.Name): $($_.Count)" }
$changes.changes | Where-Object { $_.changeType -in "Create", "Delete" } | ForEach-Object {
    Write-Host "  $($_.changeType) $($_.resourceId -replace '^.*/providers/', '')"
}

if (-not $Apply) {
    Write-Host "確認だけ行いました。デプロイするときは -Apply を付けてください。"
    return
}
az deployment group create --resource-group $env:AZURE_RESOURCE_GROUP --name $Name `
    --parameters infra/main.bicepparam --query "properties.provisioningState" -o tsv
