# MyCompanion Web の画面

本体仕様書 §15.1 の M7(Web チャット)の画面です。TypeScript + React(Vite)。サインインは MSAL(Entra External ID。認可コード + PKCE)。

- トークンは、ブラウザのメモリだけに置きます(localStorage・sessionStorage に置かない)。
- 第三者の解析・広告のタグや、外部のフォントは使いません。
- 応答は、逐次表示(`POST /v1/turns/stream`。SSE)で受け取ります。確認に引っかかった文字は、取り消して置き換えます。

## 準備

```bash
npm ci
```

`.env.example` をコピーして `.env.local` を作り、接続先を入れます(Git の対象外)。

## 開発

```bash
npm run dev
```

API の側で、`MYCOMPANION_WEB_ORIGINS` に `http://localhost:5173` を入れておきます(CORS)。

## 検査

```bash
npm run typecheck
npm run lint
npm test
npm run build
```
