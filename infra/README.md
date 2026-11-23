# Azure 環境の構築手順(dev)

本体仕様書 §11.5 の構成を、`main.bicep` で作ります。値(サブスクリプションID など)は `.env` に書き、
コードやこのフォルダのファイルには書きません(§11.5.9)。

> dev へのデプロイは 2026-11-16 に確認済みです。ただし、その後に追加した「コンテンツフィルタの方針」は、
> コンパイルの確認だけで、まだデプロイしていません。先に手順3の `what-if` で確認してください。

## 作られるもの

| リソース | 用途 | dev の構成 |
|---|---|---|
| ユーザー割り当てマネージド ID | アプリの認証 | — |
| Log Analytics + Application Insights | 監視 | 保持30日 |
| Key Vault | 秘密情報 | RBAC、消去保護あり |
| PostgreSQL フレキシブルサーバー | 記憶・状態(pgvector) | Burstable B1ms、Entra 認証のみ |
| Cosmos DB for NoSQL | turns・emotion_events・security_events | サーバーレス、キー無効 |
| Service Bus | 順序保証つきのキュー | Standard、キー無効 |
| Azure OpenAI | LLM・埋め込み | Data Zone Standard / Standard、キー無効 |
| Container Apps(任意) | API と定期ジョブ | `deployContainerApps=true` のときだけ |
| コンテナレジストリ(任意) | イメージ | `deployRegistry` か `deployContainerApps` のとき |

**dev だけの例外**: 開発者の PC から直接つなぐため、PostgreSQL と Cosmos DB を「開発者の IP だけ許可」で
公開します。stg / prod は公開せず、プライベートエンドポイントにします(未実装)。

## 手順

### 1. 道具を入れる

- [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli-windows)(Bicep を含む)

```bash
az login
```

```bash
az bicep install
```

### 2. `.env` に値を書く

`.env` の「1. デプロイ用」に、次を書きます。

| 変数 | 調べ方 |
|---|---|
| `AZURE_SUBSCRIPTION_ID` | `az account show --query id -o tsv` |
| `AZURE_TENANT_ID` | `az account show --query tenantId -o tsv` |
| `AZURE_RESOURCE_GROUP` | 任意の名前(例: `rg-mycompanion-dev`) |
| `AZURE_ENV_NAME` | `dev` |
| `AZURE_DEV_PRINCIPAL_ID` | `az ad signed-in-user show --query id -o tsv` |
| `AZURE_DEV_PRINCIPAL_NAME` | `az ad signed-in-user show --query userPrincipalName -o tsv` |
| `AZURE_DEV_CLIENT_IP` | この PC のグローバル IP アドレス |

書いたら、PowerShell に読み込みます。

```bash
. .\scripts\load-env.ps1
```

### 3. デプロイする(PowerShell)

```bash
az account set --subscription $env:AZURE_SUBSCRIPTION_ID
```

```bash
az group create --name $env:AZURE_RESOURCE_GROUP --location $env:AZURE_LOCATION
```

変更内容の確認(何も作りません):

```bash
az deployment group what-if --resource-group $env:AZURE_RESOURCE_GROUP --parameters infra/main.bicepparam
```

作成(費用が発生します):

```bash
az deployment group create --resource-group $env:AZURE_RESOURCE_GROUP --parameters infra/main.bicepparam --query properties.outputs
```

### 4. 出力を `.env` に書く

手順3の出力(`AZURE_OPENAI_ENDPOINT`、`POSTGRES_HOST` など)を、`.env` の「2. アプリの実行時」に書きます。
あわせて、次も書きます。`AZURE_OPENAI_DEPLOYMENT_*` には、モデル名(`gpt-5.4-mini`)ではなく、
**デプロイ名**(`gpt-5-4-mini`)を書きます。

| 変数 | 値 |
|---|---|
| `POSTGRES_USER` | `AZURE_DEV_PRINCIPAL_NAME` と同じ(ローカルでは開発者として接続する) |
| `AZURE_OPENAI_DEPLOYMENT_EMOTION` / `_PLANNER` / `_MEMORY_WRITE` | `gpt-5-4-mini` |
| `AZURE_OPENAI_DEPLOYMENT_CONSOLIDATION` | `gpt-6-1-sol` |
| `AZURE_OPENAI_DEPLOYMENT_DRAFT` / `_FILTER` | `gpt-6-2-sol`(2026-11-20 に選定。本体仕様書 §15.1 の M6-6) |
| `AZURE_OPENAI_DEPLOYMENT_EMBEDDING` | `text-embedding-3-large` |
| `AZURE_CLIENT_ID` | ローカルでは空のまま |

### 5. スキーマを作る

```bash
uv run python -m mycompanion.db.migrate
```

### 6. 確認する

行レベルセキュリティ(他のユーザー・他のキャラクタのデータが見えないこと)を、実際の DB で検証します。

```bash
uv run pytest -m azure tests/live
```

### 7. 開発用のデータを登録する

サンプルのキャラクタ4体、16タイプの定義、診断の質問バンクを登録します。

```bash
uv run python -m mycompanion.db.seed_dev
```

## Container Apps(API と定期ジョブ)を置く(本体仕様書 §15.1 の M6-4)

dev(v1.0)では、API と、毎時の定期ジョブ(計画 → キュー → 空くまで処理。ワーカーを兼ねる)を置きます。
Container Apps を使うときは、dev の PostgreSQL・Cosmos DB に「Azure 内からの接続」を許可します
(キー・パスワードは無効で、Entra ID の認証が必須。stg / prod はプライベートエンドポイントにします)。

1. レジストリを作る(`.env` に `AZURE_DEPLOY_REGISTRY=true` を書くか、その場で設定して、手順3と同じ `create`)。
2. イメージをクラウドで作る(`.dockerignore` で `.env` などを送らない):

```bash
az acr build --registry <ACR の名前> --image mycompanion:<タグ> --file Dockerfile .
```

3. アプリのマネージド ID を、PostgreSQL のロールにする(Entra の管理者で、1回だけ):

```bash
uv run python -m mycompanion.db.migrate --grant-app-identity <マネージド ID の名前(例: mycomp-dev-id)>
```

4. `.env` に `AZURE_DEPLOY_CONTAINER_APPS=true` と `AZURE_APP_IMAGE=<レジストリ>.azurecr.io/mycompanion:<タグ>` を書き、
   手順3の `what-if` と `create` を実行する。

**Container Apps を作ったあとの再デプロイは、`scripts/deploy-infra.ps1` を使ってください**(既定は what-if だけ。`-Apply` でデプロイ)。
デプロイの直前に、いま動いている API のイメージを読み取って使うので、CI がデプロイした新しいイメージを戻しません
(2026-11-20 に、`.env` の古い `AZURE_APP_IMAGE` で、イメージを1つ前に戻してしまったことがある)。CI のデプロイの途中なら、終わるのを待ってから実行します。

**注意**: 一度 Container Apps を作ったら、`AZURE_DEPLOY_CONTAINER_APPS=true` を `.env` に残してください。
false のままデプロイすると、データストアの「Azure 内からの接続」の許可が外れ、API とジョブが DB につながらなくなります。
`AZURE_APP_IMAGE` は、いま動いているイメージにそろえてください(古いタグを書くと、イメージが戻ります)。

## GitHub Actions からのデプロイ(本体仕様書 §15.1 の M6-5)

`.github/workflows/deploy.yml` は、main の CI が成功したら、イメージを作り、API と定期ジョブを更新します(手動でも実行可)。
Azure へのサインインは OIDC のフェデレーションで、GitHub には秘密情報を置きません。

1. `.env` に `AZURE_GITHUB_REPOSITORY=<owner>/<name>` を書き、手順3の `create` を実行する
   (デプロイ用の ID `mycomp-dev-deploy-id` と、main ブランチだけを信頼する設定、レジストリ・API・ジョブへの権限を作る)。
   GitHub が ID つきの subject(
epo:<owner>@<数字>/<name>@<数字>:ref:refs/heads/main)を提示する場合は、
   初回の実行の失敗(AADSTS700213)に出る subject を、.env の AZURE_GITHUB_OIDC_SUBJECT に書いて、もう一度 create する
   (2026-11-20 の dev は、この形式だった)。
2. GitHub のリポジトリの Settings → Secrets and variables → Actions → **Variables** に、次を登録する(秘密情報ではない):

| 変数 | 値 |
|---|---|
| `AZURE_DEPLOY_CLIENT_ID` | デプロイの出力 `DEPLOY_CLIENT_ID` |
| `AZURE_TENANT_ID` / `AZURE_SUBSCRIPTION_ID` / `AZURE_RESOURCE_GROUP` | `.env` と同じ |
| `AZURE_ACR_NAME` | デプロイの出力 `ACR_NAME` |

マイグレーションは、ワークフローでは行いません(管理者が手で実行します)。

## Web の画面(Static Web Apps。本体仕様書 §15.1 の M7)

`.env` に `AZURE_DEPLOY_WEB=true` を書いてデプロイすると、Static Web Apps(Free、eastasia。画面のファイルだけで、利用者のデータは置かない)を作り、
API の `MYCOMPANION_WEB_ORIGINS`(CORS)に、その URL を入れます(dev では、ローカルの開発用の `http://localhost:5173` も)。
画面は、`.github/workflows/deploy.yml` の `web` のジョブが、API の更新のあとに配信します。配信用の鍵は GitHub に置かず、デプロイ用の ID でその場で取り出します。
接続先は、リポジトリの変数 `WEB_API_URL`、`WEB_ENTRA_AUTHORITY`、`WEB_ENTRA_TENANT_ID`、`WEB_ENTRA_CLIENT_ID`、`WEB_API_SCOPE` から入れます。

外部テナントには、Web 用のアプリの登録(SPA)が要ります。リダイレクト URI は、Static Web Apps の URL と `http://localhost:5173`。
API のスコープ(`companion.access`)への委任のアクセス許可と、管理者の同意を付け、サインインのユーザーフローに加えます。

## コンテンツフィルタの方針

`main.bicep` は、Azure OpenAI に独自の方針(`mycompanion-annotate-first`)を作り、各デプロイに付けます。
既定の方針のままだと、本体仕様書 §13.5 と合わないためです。

| 対象 | 既定の方針 | この方針 |
|---|---|---|
| 入力のプロンプト攻撃(指示の上書きなど) | 遮断 | **注釈のみ**(判定と対応はアプリ側。§8.8、§13.5.4) |
| 入力の自傷 | 中以上を遮断 | **高のみ遮断**(危機フローに渡すため。§13.5.2) |
| 入力の暴力・ヘイト | 中以上を遮断 | 高のみ遮断 |
| 出力(自傷・暴力・ヘイト・性的) | 中以上を遮断 | 中以上を遮断(変えない) |

- 自傷の入力を「注釈のみ」(まったく遮断しない)にするには、Microsoft への申請が必要です(未申請)。
  高の遮断が起きた場合は、アプリが支える定型応答を返します。
- 方針を変えたら、手順3の `what-if` と作成をもう一度実行します。

## 認証(Entra External ID)は別に設定する

一般ユーザーの認証は、**外部テナント**(このサブスクリプションのテナントとは別)に作ります。
Bicep では作れないため、Azure ポータルで行います(M0 の残りの作業)。

1. 外部テナントを作る(Microsoft Entra 管理センター → 外部 ID)。
2. API のアプリ登録を作り、スコープ(例: `companion.access`)を公開する。
3. CLI のアプリ登録(パブリッククライアント)を作り、「パブリッククライアントフローを許可」を有効にして、
   上のスコープへのアクセス許可を付ける。
4. ユーザーフロー(メール + ワンタイムパスコード)を作り、アプリに関連づける。
5. `.env` の `ENTRA_EXTERNAL_TENANT_ID`、`ENTRA_EXTERNAL_AUTHORITY`、`ENTRA_API_AUDIENCE`、
   `ENTRA_API_SCOPE`、`MYCOMPANION_CLI_CLIENT_ID` に書く。

外部テナントでデバイスコード方式が使えるかは、ここで確認します(本体仕様書 §13.4.3 の確認事項)。

## 後片づけ

リソースグループごと消します(元に戻せません。Key Vault は消去保護のため、名前が一定期間残ります)。

```bash
az group delete --name $env:AZURE_RESOURCE_GROUP
```
