<#
.SYNOPSIS
  MyCompanion の .env を、現在の PowerShell セッションの環境変数に読み込む(本体仕様書 §11.5.9)。

.DESCRIPTION
  値はこのセッションだけに設定し、ユーザーやシステムの環境変数には書き込まない。
  値は画面に表示しない(変数名と、値の有無だけを表示する)。

.EXAMPLE
  . .\scripts\load-env.ps1
  . .\scripts\load-env.ps1 -Path .env.stg
#>
param(
    [string]$Path = (Join-Path $PSScriptRoot '..\.env')
)

if (-not (Test-Path $Path)) {
    Write-Error ".env が見つかりません: $Path  (.env.example をコピーして値を入れてください)"
    return
}

foreach ($line in Get-Content -Path $Path -Encoding UTF8) {
    $trimmed = $line.Trim()
    if ($trimmed -eq '' -or $trimmed.StartsWith('#')) { continue }

    $idx = $trimmed.IndexOf('=')
    if ($idx -lt 1) { continue }

    $name  = $trimmed.Substring(0, $idx).Trim()
    $value = $trimmed.Substring($idx + 1)

    # 行末のコメント( # 以降)を除く。値に # を含める場合は引用符で囲む
    if ($value -match '^\s*"(.*)"') { $value = $Matches[1] }
    elseif ($value -match "^\s*'(.*)'") { $value = $Matches[1] }
    else { $value = ($value -replace '\s+#.*$', '').Trim() }

    Set-Item -Path "Env:$name" -Value $value
    $state = if ($value -eq '') { '(空)' } else { '設定済み' }
    Write-Host ("{0,-40} {1}" -f $name, $state)
}
