"""Cosmos DB のデータアクセス層(本体仕様書 §11.5.3、§13.4.6)。

- 保存するのは `turns`、`emotion_events`、`security_events`(追記中心、TTL つき)。
- パーティションキーは `/userId`。すべての項目に `userId` と `characterId` を持たせる。
- **すべての読み書きで `user_id` と `character_id` の両方を必須にする。**
  キャラクタの分離は、この層で強制する(ほかの経路で Cosmos DB に直接アクセスしない)。
  例外は、アカウントの全データ削除と書き出しだけ(`delete_all_for_user` / `iter_all_for_user`)。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any, Protocol

from mycompanion.config import CosmosConfig

# コンテナ名と、既定の TTL を決める設定キー(EngineSettings の属性名)
CONTAINERS = {
    "turns": "turns_retention_days",
    "emotion_events": None,  # 感情履歴は、ユーザーの削除要求か、アカウント削除で消す
    "security_events": "security_events_retention_days",
}


class ScopeError(ValueError):
    """user_id / character_id が指定されていないアクセス。"""


def _check_scope(user_id: uuid.UUID | None, character_id: str | None) -> tuple[str, str]:
    if user_id is None or not character_id:
        raise ScopeError("Cosmos DB へのアクセスには user_id と character_id の両方が必要です")
    return str(user_id), character_id


class ContainerLike(Protocol):
    """azure.cosmos.aio の ContainerProxy のうち、この層が使う部分。"""

    async def upsert_item(self, body: dict[str, Any]) -> dict[str, Any]: ...

    def query_items(self, query: str, *, parameters: list[dict[str, Any]], partition_key: str) -> Any: ...

    async def delete_item(self, item: str, partition_key: str) -> None: ...


class EventStore:
    """1つのコンテナに対する、スコープつきの読み書き。"""

    def __init__(self, container: ContainerLike, ttl_seconds: int | None = None) -> None:
        self._container = container
        self._ttl_seconds = ttl_seconds

    async def put(
        self, user_id: uuid.UUID, character_id: str, item: dict[str, Any]
    ) -> dict[str, Any]:
        """項目を保存する。userId と characterId は、引数の値で必ず上書きする。"""
        uid, cid = _check_scope(user_id, character_id)
        body = dict(item)
        body.setdefault("id", str(uuid.uuid4()))
        body["userId"] = uid
        body["characterId"] = cid
        if self._ttl_seconds is not None:
            body.setdefault("ttl", self._ttl_seconds)
        return await self._container.upsert_item(body)

    async def query(
        self,
        user_id: uuid.UUID,
        character_id: str,
        where: str = "",
        parameters: dict[str, Any] | None = None,
        order_by: str = "",
        limit: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """スコープ内の項目を検索する。`where` は `c.` で始まる追加の条件(AND で結合する)。

        `where` と `order_by` には、コード中の固定の文字列だけを渡す。値は `parameters` で渡す。
        """
        uid, cid = _check_scope(user_id, character_id)
        sql = "SELECT * FROM c WHERE c.userId = @userId AND c.characterId = @characterId"
        if where:
            sql += f" AND ({where})"
        if order_by:
            sql += f" ORDER BY {order_by}"
        if limit is not None:
            sql += f" OFFSET 0 LIMIT {int(limit)}"
        params = [{"name": "@userId", "value": uid}, {"name": "@characterId", "value": cid}]
        for name, value in (parameters or {}).items():
            params.append({"name": f"@{name.lstrip('@')}", "value": value})
        async for item in self._container.query_items(sql, parameters=params, partition_key=uid):
            # 念のため、返ってきた項目のスコープも確認する(多重の防御)
            if item.get("userId") == uid and item.get("characterId") == cid:
                yield item

    async def delete(self, user_id: uuid.UUID, character_id: str, item_id: str) -> bool:
        """スコープ内の項目を1件削除する。スコープ外・存在しない場合は False。"""
        uid, _ = _check_scope(user_id, character_id)
        found = False
        async for _ in self.query(user_id, character_id, "c.id = @id", {"id": item_id}):
            found = True
        if not found:
            return False
        await self._container.delete_item(item_id, partition_key=uid)
        return True

    # --- アカウント単位の処理(キャラクタをまたぐ。削除と書き出しのためだけに使う) ---

    async def iter_all_for_user(self, user_id: uuid.UUID) -> AsyncIterator[dict[str, Any]]:
        """ユーザーの全項目(データの書き出し用。§13.4.5)。会話の処理には使わない。"""
        if user_id is None:
            raise ScopeError("user_id が必要です")
        uid = str(user_id)
        async for item in self._container.query_items(
            "SELECT * FROM c WHERE c.userId = @userId",
            parameters=[{"name": "@userId", "value": uid}],
            partition_key=uid,
        ):
            if item.get("userId") == uid:
                yield item

    async def delete_all_for_user(self, user_id: uuid.UUID) -> int:
        """ユーザーの全項目を削除する(アカウントの全データ削除。§11.5.5)。冪等。"""
        count = 0
        async for item in self.iter_all_for_user(user_id):
            await self._container.delete_item(item["id"], partition_key=str(user_id))
            count += 1
        return count


class CosmosStores:
    """3つのコンテナの EventStore をまとめて持つ。"""

    def __init__(self, turns: EventStore, emotion_events: EventStore, security_events: EventStore) -> None:
        self.turns = turns
        self.emotion_events = emotion_events
        self.security_events = security_events

    def all(self) -> list[EventStore]:
        return [self.turns, self.emotion_events, self.security_events]


class _CosmosHandle:
    """Cosmos DB のクライアントと資格情報をまとめて閉じる。"""

    def __init__(self, client: Any, credential: Any) -> None:
        self._client = client
        self._credential = credential

    async def close(self) -> None:
        await self._client.close()
        await self._credential.close()


def open_cosmos(
    config: CosmosConfig, turns_ttl_days: int, security_ttl_days: int
) -> tuple[_CosmosHandle, CosmosStores]:
    """Cosmos DB に接続する。認証は Entra ID。戻り値のハンドルは、終了時に close する。"""
    from azure.cosmos.aio import CosmosClient
    from azure.identity.aio import DefaultAzureCredential

    credential = DefaultAzureCredential()
    client = CosmosClient(config.endpoint, credential=credential)
    database = client.get_database_client(config.database)
    day = 24 * 60 * 60
    stores = CosmosStores(
        turns=EventStore(database.get_container_client("turns"), ttl_seconds=turns_ttl_days * day),
        emotion_events=EventStore(database.get_container_client("emotion_events")),
        security_events=EventStore(
            database.get_container_client("security_events"), ttl_seconds=security_ttl_days * day
        ),
    )
    return _CosmosHandle(client, credential), stores
