"""Cosmos DB のデータアクセス層のテスト(分離の強制。本体仕様書 §13.4.6)。"""

import uuid

import pytest

from mycompanion.db.cosmos import EventStore, ScopeError


class FakeContainer:
    """メモリ上の模擬コンテナ。クエリは userId / characterId / id の等値条件だけを解釈する。"""

    def __init__(self):
        self.items: dict[tuple[str, str], dict] = {}

    async def upsert_item(self, body):
        self.items[(body["userId"], body["id"])] = dict(body)
        return body

    def query_items(self, query, *, parameters, partition_key):
        params = {p["name"]: p["value"] for p in parameters}

        async def gen():
            for (uid, _), item in list(self.items.items()):
                if uid != partition_key:
                    continue
                if "@characterId" in query and item["characterId"] != params["@characterId"]:
                    continue
                if "@id" in query and item["id"] != params["@id"]:
                    continue
                yield dict(item)

        return gen()

    async def delete_item(self, item, partition_key):
        del self.items[(partition_key, item)]


USER_A, USER_B = uuid.uuid4(), uuid.uuid4()


async def _collect(aiter):
    return [item async for item in aiter]


async def test_put_forces_scope_fields_and_ttl():
    store = EventStore(FakeContainer(), ttl_seconds=60)
    saved = await store.put(USER_A, "char_1", {"userId": str(USER_B), "characterId": "char_x", "text": "t"})
    assert saved["userId"] == str(USER_A) and saved["characterId"] == "char_1"
    assert saved["ttl"] == 60 and saved["id"]


async def test_query_is_isolated_by_user_and_character():
    store = EventStore(FakeContainer())
    await store.put(USER_A, "char_1", {"id": "1"})
    await store.put(USER_A, "char_2", {"id": "2"})
    await store.put(USER_B, "char_1", {"id": "3"})
    assert [i["id"] for i in await _collect(store.query(USER_A, "char_1"))] == ["1"]
    assert [i["id"] for i in await _collect(store.query(USER_A, "char_2"))] == ["2"]
    assert [i["id"] for i in await _collect(store.query(USER_B, "char_1"))] == ["3"]


@pytest.mark.parametrize("user_id, character_id", [(None, "char_1"), (USER_A, ""), (USER_A, None)])
async def test_scope_is_mandatory(user_id, character_id):
    store = EventStore(FakeContainer())
    with pytest.raises(ScopeError):
        await store.put(user_id, character_id, {})
    with pytest.raises(ScopeError):
        await _collect(store.query(user_id, character_id))
    with pytest.raises(ScopeError):
        await store.delete(user_id, character_id, "1")


async def test_delete_cannot_cross_character():
    container = FakeContainer()
    store = EventStore(container)
    await store.put(USER_A, "char_1", {"id": "1"})
    # 同じユーザーの別のキャラクタからは削除できない
    assert await store.delete(USER_A, "char_2", "1") is False
    assert len(container.items) == 1
    assert await store.delete(USER_A, "char_1", "1") is True
    assert container.items == {}


async def test_account_deletion_removes_only_that_user():
    container = FakeContainer()
    store = EventStore(container)
    await store.put(USER_A, "char_1", {"id": "1"})
    await store.put(USER_A, "char_2", {"id": "2"})
    await store.put(USER_B, "char_1", {"id": "3"})
    assert await store.delete_all_for_user(USER_A) == 2
    assert await store.delete_all_for_user(USER_A) == 0  # 冪等
    assert [k[1] for k in container.items] == ["3"]
