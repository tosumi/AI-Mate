"""外部テナント(Entra External ID)のアカウントの削除(本体仕様書 §13.3、§15.1 の M6-3)。

アカウントの全データ削除のあと、ワーカーが Microsoft Graph で、外部テナントのユーザーを削除し、
ごみ箱(削除済みのアイテム)からも消す。

ユーザーを削除した直後は、ごみ箱にまだ現れない(404 になる)ことがある(2026-11-20 に dev で確認)。
そのため、**ユーザーを消したその回は完了にしない**。次の回(1時間後)に、ユーザーが無く(404)、
ごみ箱からの削除が済んだ(204、または 404)ことを確かめてから、完了にする(冪等)。

認証: 秘密情報を置かない方式。マネージド ID を信頼するフェデレーション資格情報を、アプリに付ける
(アプリの資格情報として、マネージド ID のトークンを渡す)。Microsoft の文書の条件:
アプリはマネージド ID と**同じテナント**(運用側のテナント)に登録し、別のテナント(外部テナント)を操作するには、
**マルチテナントのアプリ**にして、外部テナントに追加(管理者の同意)する。
権限は、アプリケーションの権限 `User.DeleteRestore.All`(ユーザーの削除と復元だけ)。
外部テナントが、別のテナントのマルチテナントのアプリを受け入れるかは、設定時に確かめる(本体仕様書 §15.1 の M6-3)。
設定がなければ、削除は動かない(保留のまま残る)。

環境変数:
- `ENTRA_EXTERNAL_TENANT_ID`: 外部テナント(トークンを取るテナント)
- `ENTRA_DIRECTORY_CLIENT_ID`: 削除用のアプリ(運用側のテナントに登録した、マルチテナントのアプリ)
- `AZURE_CLIENT_ID`: マネージド ID(Container Apps で設定済み)
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Mapping
from typing import Any, Protocol

import httpx

GRAPH = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
TOKEN_EXCHANGE_SCOPE = "api://AzureADTokenExchange/.default"


class IdentityDirectory(Protocol):
    async def delete_user(self, subject: str) -> bool: ...

    async def close(self) -> None: ...


class GraphIdentityDirectory:
    """Microsoft Graph で、外部テナントのユーザーを削除する。`credential` は azure.identity.aio の資格情報。"""

    def __init__(self, credential: Any, http: httpx.AsyncClient | None = None, owns_credential: bool = False) -> None:
        self._credential = credential
        self._http = http or httpx.AsyncClient(timeout=30.0)
        self._owns_credential = owns_credential

    async def delete_user(self, subject: str) -> bool:
        """ユーザーを削除し、ごみ箱からも消す。完全に消えたと確かめられたら True(まだなら False)。"""
        # オブジェクト ID は GUID だけを受け付ける(URL に入れるため)
        object_id = str(uuid.UUID(subject))
        token = (await self._credential.get_token(GRAPH_SCOPE)).token
        headers = {"Authorization": f"Bearer {token}"}
        statuses = []
        for url in (f"{GRAPH}/users/{object_id}", f"{GRAPH}/directory/deletedItems/{object_id}"):
            response = await self._http.delete(url, headers=headers)
            if response.status_code not in (204, 404):
                # 応答の本文は出さない(利用者の情報を含みうる)
                raise RuntimeError(f"Graph の削除に失敗しました: HTTP {response.status_code}")
            statuses.append(response.status_code)
        # この回にユーザーを消した(204)なら、ごみ箱への反映を待って、次の回に確かめる
        return statuses[0] == 404

    async def close(self) -> None:
        await self._http.aclose()
        if self._owns_credential:
            await self._credential.close()


def directory_from_env(env: Mapping[str, str] | None = None) -> GraphIdentityDirectory | None:
    """環境変数から、削除用の資格情報を作る。設定が足りなければ None(削除は保留のまま残る)。"""
    env = os.environ if env is None else env
    tenant = (env.get("ENTRA_EXTERNAL_TENANT_ID") or "").strip()
    client = (env.get("ENTRA_DIRECTORY_CLIENT_ID") or "").strip()
    managed = (env.get("AZURE_CLIENT_ID") or "").strip()
    if not (tenant and client and managed):
        return None
    from azure.identity.aio import ClientAssertionCredential, ManagedIdentityCredential

    identity = ManagedIdentityCredential(client_id=managed)

    async def assertion() -> str:
        return (await identity.get_token(TOKEN_EXCHANGE_SCOPE)).token

    credential = _AssertionCredential(ClientAssertionCredential, tenant, client, assertion, identity)
    return GraphIdentityDirectory(credential, owns_credential=True)


class _AssertionCredential:
    """マネージド ID のトークンを、アプリの資格情報(クライアントアサーション)として使う。

    `ClientAssertionCredential` のコールバックは同期関数なので、トークンを先に取ってから渡す。
    """

    def __init__(self, factory: Any, tenant: str, client: str, assertion: Any, identity: Any) -> None:
        self._factory = factory
        self._tenant = tenant
        self._client = client
        self._assertion = assertion
        self._identity = identity

    async def get_token(self, *scopes: str) -> Any:
        value = await self._assertion()
        credential = self._factory(self._tenant, self._client, lambda: value)
        try:
            return await credential.get_token(*scopes)
        finally:
            await credential.close()

    async def close(self) -> None:
        await self._identity.close()
