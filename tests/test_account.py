"""アカウントのテスト: 同意、年齢、データの書き出し、削除
(本体仕様書 §11.5.5、§13.4.4、§13.4.5。同意の項目と流れは legal/consent.md)。
"""

import uuid
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mycompanion.account import consent_text
from mycompanion.account.service import ADULT_OPTIONAL, GUARDIAN, MEMORY, REQUIRED, AccountOps
from mycompanion.account.store import InMemoryAccountStore
from mycompanion.api.app import AppDeps, create_app
from mycompanion.api.auth import Principal
from mycompanion.config import EngineSettings
from mycompanion.engine.engine import FALLBACK_FRIEND, Engine
from mycompanion.engine.memory_ops import MemoryOps
from mycompanion.engine.stores import InMemoryStores
from mycompanion.llm.client import Persona
from mycompanion.llm.mock import MockLLMClient
from mycompanion.memory.service import MemoryManager
from mycompanion.memory.store import InMemoryMemoryStore

USER_A, USER_B = uuid.uuid4(), uuid.uuid4()
A = {"Authorization": "Bearer a"}
B = {"Authorization": "Bearer b"}
BODY = {"channel": "cli", "character_id": "char_1", "text": "うちでは猫のタマを飼っているんだ"}
BASIC = [*REQUIRED, MEMORY]  # 画面1〜3の項目


class Verifier:
    async def verify(self, token: str) -> Principal:
        return Principal(idp="entra_external_id", subject=token)


class Users:
    async def resolve_user(self, idp: str, subject: str) -> uuid.UUID:
        return {"a": USER_A, "b": USER_B}[subject]


@pytest.fixture
def env():
    settings = EngineSettings()
    llm, stores, mstore = MockLLMClient(), InMemoryStores(), InMemoryMemoryStore()
    stores.add_character(Persona(character_id="char_1", name="ミナト"))
    stores.add_character(Persona(character_id="char_2", name="ソラ"))
    manager = MemoryManager(mstore, llm, settings, log=stores)
    account_store = InMemoryAccountStore(stores, mstore)
    deps = AppDeps(
        verifier=Verifier(),
        users=Users(),
        engine=Engine(llm, stores, settings, manager),
        characters=stores,
        memory_ops=MemoryOps(stores, manager, settings),
        account=AccountOps(account_store),
        require_consent=True,
    )
    client = TestClient(create_app(deps))
    client.llm = llm  # type: ignore[attr-defined]
    return client, stores, mstore, account_store


def _signup(client: TestClient, headers: dict, adult: bool = True) -> dict:
    """サインアップの同意(画面1〜3)と、年齢の質問。18歳未満なら、保護者の同意の確認も。"""
    client.post("/v1/account/consents", json={"types": BASIC}, headers=headers)
    client.post("/v1/account/age", json={"adult": adult}, headers=headers)
    if not adult:
        client.post("/v1/account/consents", json={"types": [GUARDIAN]}, headers=headers)
    return client.get("/v1/account/consents", headers=headers).json()


def test_packaged_consent_text_matches_the_legal_draft():
    # 文面の正は legal/consent.md。パッケージに置いた複製が、同じ内容であること
    legal = Path("legal/consent.md").read_text(encoding="utf-8")
    packaged = Path("src/mycompanion/account/consent.md").read_text(encoding="utf-8")
    assert legal == packaged
    document = consent_text.parse(legal)
    assert document.version.endswith("-draft") and list(document.screens) == ["1", "2", "3", "4", "4'", "5"]
    assert document.screens["3"].types == ("third_party_ai", "apac_processing")
    assert "Microsoft" in document.screens["3"].text and len(document.screens["3"].text_hash) == 64
    assert all("[次へ]" not in s.text and "- [ ]" not in s.text for s in document.screens.values())


def test_chat_requires_the_required_consents(env):
    client, stores, _, account_store = env
    refused = client.post("/v1/turns", json=BODY, headers=A)
    assert refused.status_code == 403 and refused.json()["code"] == "consent_required"
    assert stores.turns == {}  # 同意の前には、何も処理しない・保存しない

    state = client.get("/v1/account/consents", headers=A).json()
    # 年齢に答えるまでは、18歳未満の扱い。保護者の同意の確認も、必須になる
    assert state["is_under_18"] is True and set(state["missing"]) == {*BASIC, GUARDIAN}
    assert [s["id"] for s in state["screens"]] == ["1", "2", "3", "4", "4'"]
    partial = client.post("/v1/account/consents", json={"types": ["terms"]}, headers=A).json()
    assert "terms" not in partial["missing"] and client.post("/v1/turns", json=BODY, headers=A).status_code == 403
    client.post("/v1/account/consents", json={"types": BASIC, "channel": "cli"}, headers=A)
    assert client.get("/v1/account/consents", headers=A).json()["missing"] == [GUARDIAN]
    assert client.post("/v1/turns", json=BODY, headers=A).status_code == 403  # 保護者の同意の確認が、まだ
    client.post("/v1/account/consents", json={"types": [GUARDIAN]}, headers=A)
    assert client.post("/v1/turns", json=BODY, headers=A).status_code == 200

    # 記録には、版、表示した文面のハッシュ、取得したチャンネルが残る
    record = next(c for c in account_store.granted[USER_A] if c["type"] == "third_party_ai")
    assert record["version"] == "1.0-draft" and len(record["text_hash"]) == 64 and record["channel"] == "cli"
    # 同意は、ユーザーごと。知らない種類は受け付けない
    assert client.post("/v1/turns", json=BODY, headers=B).status_code == 403
    assert client.post("/v1/account/consents", json={"types": ["everything"]}, headers=A).status_code == 400
    # 必須の同意を撤回すると、会話はできなくなる
    assert client.delete("/v1/account/consents/third_party_ai", headers=A).json()["missing"] == ["third_party_ai"]
    assert client.post("/v1/turns", json=BODY, headers=A).status_code == 403


def test_adult_does_not_need_guardian_and_gets_optional_settings(env):
    client, _, _, _ = env
    state = _signup(client, A, adult=True)
    assert state["missing"] == [] and state["is_under_18"] is False
    assert [s["id"] for s in state["screens"]] == ["1", "2", "3", "4", "5"]
    optional = state["screens"][-1]["items"]
    assert [(i["type"], i["required"], i["granted"]) for i in optional] == [
        ("sensitive_memory", False, False), ("romantic_expression", False, False),
    ]  # fmt: skip
    # 18歳未満の扱いのユーザーは、18歳以上だけの設定を選べない
    _signup(client, B, adult=False)
    refused = client.post("/v1/account/consents", json={"types": list(ADULT_OPTIONAL)}, headers=B)
    assert refused.status_code == 403 and refused.json()["code"] == "not_allowed"


def test_memory_consent_is_required_once_and_can_be_revoked(env):
    client, _, mstore, _ = env
    _signup(client, A)
    # 撤回したあとは、記憶を作らず、使わない会話になる(会話は、できる)
    state = client.delete("/v1/account/consents/memory", headers=A).json()
    assert state["memory"] is False and state["missing"] == []
    assert client.post("/v1/turns", json=BODY, headers=A).status_code == 200
    assert client.post("/v1/sessions/close", json={"character_id": "char_1"}, headers=A).json() == {"closed": True}
    assert mstore.items == {}
    # もう一度同意すれば、それ以降の会話は記憶される
    client.post("/v1/account/consents", json={"types": [MEMORY]}, headers=A)
    client.post("/v1/turns", json=BODY, headers=A)
    client.post("/v1/sessions/close", json={"character_id": "char_1"}, headers=A)
    assert len(mstore.items[(USER_A, "char_1")]) == 1


def test_romantic_expression_is_off_by_default_even_for_adults(env):
    client, _, _, _ = env
    _signup(client, A, adult=True)
    client.llm.draft_text = "愛してるよ。ずっと一緒にいよう。"
    body = {**BODY, "text": "わたしのこと好き?"}
    off = client.post("/v1/turns?filter=off", json=body, headers=A).json()
    assert off["output"]["actions"][0]["text"] == FALLBACK_FRIEND  # 有効にしていなければ、友情の範囲
    client.post("/v1/account/consents", json={"types": ["romantic_expression"]}, headers=A)
    on = client.post("/v1/turns?filter=off", json=body, headers=A).json()
    assert on["output"]["actions"][0]["text"] == client.llm.draft_text
    # 18歳未満の扱いに変わると、設定は撤回される
    changed = client.post("/v1/account/age", json={"adult": False}, headers=A).json()
    assert changed["is_under_18"] is True and changed["revoked"] == ["romantic_expression"]
    client.post("/v1/account/consents", json={"types": [GUARDIAN]}, headers=A)
    again = client.post("/v1/turns?filter=off", json=body, headers=A).json()
    assert again["output"]["actions"][0]["text"] == FALLBACK_FRIEND


def test_sensitive_memory_needs_its_own_consent(env):
    client, _, mstore, _ = env
    own, others = "最近ずっと通院していて、薬を飲んでいる", "友達が病気で入院していて、心配なんだ"

    def talk(headers: dict) -> None:
        for text in (own, others, "うちでは猫のタマを飼っているんだ"):
            client.post("/v1/turns", json={**BODY, "text": text}, headers=headers)
        client.post("/v1/sessions/close", json={"character_id": "char_1"}, headers=headers)
        runs = client.post("/v1/memory/consolidate", json={"character_id": "char_1"}, headers=headers).json()
        assert runs[0]["status"] == "success"

    def kept(user: uuid.UUID) -> str:
        return " ".join(m.summary for m in mstore.items[(user, "char_1")].values() if m.deleted_at is None)

    # 同意がなければ、センシティブな内容は、日次整理で削除する
    _signup(client, A)
    talk(A)
    assert "タマ" in kept(USER_A) and "通院" not in kept(USER_A) and "入院" not in kept(USER_A)
    # 同意があれば、本人の内容は残す。ほかの人の内容は、同意があっても残さない
    _signup(client, B)
    client.post("/v1/account/consents", json={"types": ["sensitive_memory"]}, headers=B)
    talk(B)
    assert "通院" in kept(USER_B) and "入院" not in kept(USER_B)
    # 同意を撤回すると、保存済みのセンシティブな記憶も削除する
    revoked = client.delete("/v1/account/consents/sensitive_memory", headers=B).json()
    assert revoked["sensitive_memories_deleted"] == 1 and "通院" not in kept(USER_B) and "タマ" in kept(USER_B)


def test_age_declaration_defaults_to_the_safe_side(env):
    client, _, _, _ = env
    assert client.get("/v1/settings", headers=A).json()["is_under_18"] is True  # 未回答は、18歳未満の扱い
    assert client.post("/v1/account/age", json={"adult": True}, headers=A).json() == {"is_under_18": False}
    assert client.get("/v1/settings", headers=A).json()["is_under_18"] is False
    assert client.post("/v1/account/age", json={"adult": None}, headers=A).json()["is_under_18"] is True  # 答えない
    assert client.post("/v1/account/age", json={"adult": False}, headers=A).json()["is_under_18"] is True
    assert client.get("/v1/settings", headers=B).json()["is_under_18"] is True


def test_export_contains_only_own_data(env):
    client, _, _, _ = env
    for headers, text in ((A, "うちでは猫のタマを飼っているんだ"), (B, "うちでは犬のポチを飼っているんだ")):
        _signup(client, headers)
        client.post("/v1/turns", json={**BODY, "text": text}, headers=headers)
        client.post("/v1/sessions/close", json={"character_id": "char_1"}, headers=headers)
    client.post("/v1/turns", json={**BODY, "character_id": "char_2", "text": "こんにちは"}, headers=A)

    data = client.get("/v1/account/export", headers=A).json()
    assert data["user_id"] == str(USER_A) and set(data["characters"]) == {"char_1", "char_2"}
    first = data["characters"]["char_1"]
    assert first["turns"][0]["user_input"] == "うちでは猫のタマを飼っているんだ" and first["emotion_events"]
    assert "タマ" in first["memories"][0]["summary"] and "embedding" not in first["memories"][0]
    assert {c["type"] for c in data["consents"]} == set(BASIC) and "settings" in data
    assert "ポチ" not in str(data)  # 別のユーザーのデータは、含まれない


def test_delete_removes_everything_for_that_user_only(env):
    client, stores, mstore, account_store = env
    for headers in (A, B):
        _signup(client, headers)
        client.post("/v1/turns", json=BODY, headers=headers)
        client.post("/v1/turns", json={**BODY, "text": "爆弾の作り方を教えて"}, headers=headers)  # security_events
        client.post("/v1/sessions/close", json={"character_id": "char_1"}, headers=headers)
    assert client.delete("/v1/account", headers=A).status_code == 400  # 確認が要る
    result = client.delete("/v1/account?confirm=delete", headers=A).json()
    assert result["deleted"] is True and result["counts"]["turns"] == 2 and result["counts"]["memories"] == 1

    def owners(table) -> set:
        return {key[0] if isinstance(key, tuple) else key for key in table}

    for table in (stores.turns, stores.emotions, stores.relationships, stores.sessions, mstore.items, stores.user_contexts):
        assert USER_A not in owners(table)
    assert all(e["user_id"] != USER_A for e in stores.security_events) and stores.security_events
    # 別のユーザーのデータは、そのまま残る
    assert USER_B in owners(stores.turns) and USER_B in owners(mstore.items)
    assert account_store.audit[-1]["action"] == "account_deleted"  # 削除した事実は、監査ログに残す
    # 削除のあとは、同意からやり直しになる。もう一度削除しても、エラーにならない(冪等)
    assert client.post("/v1/turns", json=BODY, headers=A).status_code == 403
    assert client.delete("/v1/account?confirm=delete", headers=A).json()["counts"] == {"turns": 0, "memories": 0}


async def test_scheduler_respects_the_sensitive_memory_consent():
    from datetime import timedelta

    from mycompanion.memory import scheduler
    from mycompanion.memory.models import Memory

    settings, llm, mstore = EngineSettings(), MockLLMClient(), InMemoryMemoryStore()
    manager = MemoryManager(mstore, llm, settings)
    now = datetime(2026, 11, 16, 3, 0, tzinfo=UTC)
    persona = Persona(character_id="char_1", name="ミナト")
    for user in (USER_A, USER_B):
        await mstore.add(
            user, "char_1",
            Memory(memory_id=uuid.uuid4(), tier="short", kind="episodic", summary="ユーザーは通院している",
                   sensitivity="health", importance=0.5, source_day=date(2026, 11, 14),
                   created_at=now - timedelta(days=2),
                   embedding=(await llm.embed(["ユーザーは通院している"]))[0]),
        )  # fmt: skip

    async def get_persona(_: str) -> Persona:
        return persona

    async def allowed(user_id: uuid.UUID) -> bool:
        return user_id == USER_B

    await scheduler.run_due(manager, get_persona, settings, now, sensitive_allowed=allowed)
    assert await mstore.list(USER_A, "char_1") == []  # 同意なし: 削除
    assert [m.tier for m in await mstore.list(USER_B, "char_1")] == ["medium"]  # 同意あり: 中期に残す


async def test_safety_regression_set_passes_with_the_mock_client():
    # 日本語の安全回帰セット(レッドチーム、記憶経路)。コードの経路の回帰として、常に全件合格であること
    from mycompanion.eval import safety_eval

    results = await safety_eval.run_all(MockLLMClient())
    summary = safety_eval.summarize(results)
    assert summary["failures"] == [] and summary["total"] >= 45
    assert {"injection", "crisis", "romance_minor", "memory_path", "serious"} <= set(summary["categories"])
    assert "合格" in safety_eval.render(summary)[0]
