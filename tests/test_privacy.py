"""個人情報・ストア要件の残りのテスト(本体仕様書 §11.3、§13.3、§13.4.5、§13.5.8)。

連絡先のマスク、AI の応答の報告、監査ログ(ID の置き換えと保持期間)、書き出し。
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from mycompanion.account.service import AccountError, AccountOps
from mycompanion.account.store import InMemoryAccountStore
from mycompanion.channel.types import TurnInput
from mycompanion.config import EngineSettings
from mycompanion.engine.engine import Engine
from mycompanion.engine.stores import InMemoryStores
from mycompanion.llm.client import Persona
from mycompanion.llm.mock import MockLLMClient
from mycompanion.memory.service import MemoryManager
from mycompanion.memory.store import InMemoryMemoryStore
from mycompanion.safety import pii

USER = uuid.uuid4()
NOW = datetime(2026, 11, 17, 3, 0, tzinfo=UTC)
FULLWIDTH = "".join(chr(0xFF10 + int(d)) if d.isdigit() else chr(0xFF0D) for d in "090-1234-5678")


@pytest.mark.parametrize(
    "text, expected, counts",
    [
        ("電話は090-1234-5678だよ", "電話は[電話番号]だよ", {"phone": 1}),
        ("03-1234-5678 にかけて", "[電話番号] にかけて", {"phone": 1}),
        ("09012345678に連絡して", "[電話番号]に連絡して", {"phone": 1}),
        ("+81 90-1234-5678 が番号", "[電話番号] が番号", {"phone": 1}),
        (f"番号は{FULLWIDTH}", "番号は[電話番号]", {"phone": 1}),  # 全角の数字
        ("メールは taro.yamada+x@example.co.jp です", "メールは [メールアドレス] です", {"email": 1}),
        ("〒100-0001 に住んでる", "[郵便番号] に住んでる", {"postal_code": 1}),
        ("郵便番号は150-0002", "郵便番号は[郵便番号]", {"postal_code": 1}),
        ("a@example.com と 080-1111-2222", "[メールアドレス] と [電話番号]", {"email": 1, "phone": 1}),
    ],
)
def test_contacts_are_masked(text, expected, counts):
    assert pii.mask_contacts(text) == (expected, counts)


@pytest.mark.parametrize(
    "text",
    [
        "2026-11-17 に会おう",  # 日付
        "12000円だった",
        "3時から5時まで",
        "10月2日の面接、うまくいった",
        "100-200人くらい来てた",
        "タマ(猫)と、友達のユキの話",  # 名前は、マスクしない
        "東京都渋谷区に住んでる",  # 住所の文字列も、マスクしない
        "受験番号は 0123 だった",
    ],
)
def test_ordinary_numbers_and_names_are_not_masked(text):
    assert pii.mask_contacts(text) == (text, {})


# --- 住所の番地(郵便番号データの町名を手がかりにする) -----------------------------

BANCHI = [
    ("東京都渋谷区桜丘町1-2-3に住んでる", "東京都渋谷区桜丘町[番地]に住んでる"),
    ("住所は東京都千代田区丸の内1丁目9番1号です", "住所は東京都千代田区丸の内1丁目[番地]です"),
    ("横浜市西区みなとみらい2-3-1 ランドマークタワー45階で働いてる", "横浜市西区みなとみらい[番地]で働いてる"),
    ("大阪府大阪市北区梅田3丁目1番3号 梅田ビル501号室", "大阪府大阪市北区梅田3丁目[番地]"),
    ("札幌市中央区北1条西2丁目1番地", "札幌市中央区北1条西2丁目[番地]"),  # 町名の数字(北一条西)
    ("世田谷区三軒茶屋二丁目十一番二十号に引っ越した", "世田谷区三軒茶屋二丁目[番地]に引っ越した"),  # 漢数字
    ("那覇市字安里123番地", "那覇市字安里[番地]"),
    ("京都市中京区河原町通三条上る恵比須町427", "京都市中京区河原町通三条上る恵比須町[番地]"),
    ("名古屋市中区栄3-6-1に店がある", "名古屋市中区栄[番地]に店がある"),  # 1文字の町名(直前が区)
    ("仙台市青葉区一番町4丁目6-1", "仙台市青葉区一番町4丁目[番地]"),
    ("うちの住所、練馬区豊玉北6の12の1だよ", "うちの住所、練馬区豊玉北[番地]だよ"),
    ("北海道虻田郡ニセコ町字曽我888", "北海道虻田郡ニセコ町字曽我[番地]"),
    ("東京都港区芝公園4丁目2−8 東京タワーの近く", "東京都港区芝公園4丁目[番地] 東京タワーの近く"),
    ("島原市1234番地に住んでいる", "島原市[番地]に住んでいる"),  # 町名のない市(「次に番地がくる場合」)
]


@pytest.mark.parametrize("text, expected", BANCHI)
def test_banchi_after_a_town_name_is_masked(text, expected):
    assert pii.mask_contacts(text) == (expected, {"address_banchi": 1})


@pytest.mark.parametrize(
    "text",
    [
        # 番地のない住所・住所の一部は、個人を特定しないので、マスクしない
        "東京都渋谷区に住んでる", "東京都渋谷区桜丘町に住んでる", "千代田区丸の内1丁目のオフィス",
        "札幌市中央区北1条西2丁目", "新宿3丁目のカフェに行った", "東京", "横浜市", "大阪府大阪市北区梅田",
        "うちは3丁目4-5",  # 町名を省いた番地(個人を特定しない)
        # 住所でない数字
        "文京区まで電車で2-3駅", "豊島区役所に3番窓口がある", "市川市と船橋市で1-0だった", "中野区で2-3日過ごした",
        "品川区の物件は家賃12-13万円", "試合は3-2で勝った", "この町2番目に古い店",
        # 短い・一般的な町名(中央、大手、栄 など)のあとの数字
        "大手3社と取引がある", "中央1-0で勝った", "大手2-3社が撤退", "上1-2の成績", "栄の店は10時から",
        "1-2-3で数えて", "2025-11-19の記録", "港で12時に",
    ],
)
def test_partial_addresses_and_other_numbers_are_not_masked(text):
    assert pii.mask_contacts(text) == (text, {})


def test_banchi_masking_is_strict_and_can_be_turned_off():
    # 短い町名の直前に市区町村がなく、番地が2つ続きの形のときは、取りこぼす(厳しめにした結果。受け入れる)
    assert pii.mask_contacts("実家は新潟市西区の小針3丁目12-5") == ("実家は新潟市西区の小針3丁目12-5", {})
    assert pii.mask_contacts("東京都渋谷区桜丘町1-2-3", banchi=False) == ("東京都渋谷区桜丘町1-2-3", {})
    # 連絡先と番地が、同じ発話にあるとき
    text, counts = pii.mask_contacts("住所は那覇市字安里123番地、電話は098-123-4567")
    assert text == "住所は那覇市字安里[番地]、電話は[電話番号]" and counts == {"phone": 1, "address_banchi": 1}


@pytest.fixture
def env():
    llm, stores, mstore = MockLLMClient(), InMemoryStores(), InMemoryMemoryStore()
    stores.add_character(Persona(character_id="char_1", name="ミナト"))
    manager = MemoryManager(mstore, llm, EngineSettings(), log=stores)
    store = InMemoryAccountStore(stores, mstore)
    return Engine(llm, stores, EngineSettings(), manager), stores, mstore, llm, store, AccountOps(store)


async def test_contacts_never_reach_the_model_the_log_or_memory(env):
    engine, stores, mstore, llm, _, _ = env
    seen: list[str] = []
    original = llm.estimate_emotion

    async def spy(text, context):
        seen.append(text)
        return await original(text, context)

    llm.estimate_emotion = spy
    turn = TurnInput(
        channel="cli", character_id="char_1",
        text="私の電話番号は090-1234-5678、メールは a@example.com、住所は渋谷区桜丘町1-2-3だから覚えておいて",
    )  # fmt: skip
    result = await engine.handle_turn(USER, turn, NOW)
    assert seen == ["私の電話番号は[電話番号]、メールは [メールアドレス]、住所は渋谷区桜丘町[番地]だから覚えておいて"]
    assert "contact_masked" in result.state["safety_flags"]
    (record,) = stores.turns[(USER, "char_1")]
    assert "090" not in record.user_input and "example.com" not in record.user_input and "1-2-3" not in record.user_input
    await engine.close_session(USER, "char_1")
    memories = await mstore.list(USER, "char_1")
    assert memories and all("090" not in m.summary and "example.com" not in m.summary for m in memories)

    off = Engine(llm, stores, EngineSettings(pii_mask_contacts=False), None)
    await off.handle_turn(uuid.uuid4(), TurnInput(channel="cli", character_id="char_1", text="番号は090-1234-5678"), NOW)
    assert seen[-1] == "番号は090-1234-5678"


async def test_response_report_is_recorded_exported_and_deleted(env):
    _, stores, _, _, store, ops = env
    await stores.set_relationship(USER, "char_1", 1.0, "test", NOW)
    session_id = uuid.uuid4()
    done = await ops.report(USER, "char_1", "harmful", "  危ないことを勧められた  ", session_id, 3)
    assert done["reported"] and done["id"]
    with pytest.raises(AccountError) as error:
        await ops.report(USER, "char_1", "boring")
    assert error.value.code == "invalid_value"
    (report,) = store.reports
    # 会話の本文は、持たない(どのターンか、理由の種類、本人のメモだけ)
    assert set(report) == {"id", "user_id", "character_id", "ts", "reason", "note", "session_id", "turn_no"}
    assert (report["reason"], report["note"], report["turn_no"]) == ("harmful", "危ないことを勧められた", 3)
    assert len((await ops.report(USER, "char_1", "other", "x" * 900))["id"]) == 36 and len(store.reports[1]["note"]) == 500

    exported = (await ops.export(USER))["characters"]["char_1"]
    assert [r["reason"] for r in exported["reports"]] == ["harmful", "other"]
    assert "security_events" in exported and all("user_id" not in r for r in exported["reports"])

    deleted = await ops.delete(USER)
    assert deleted["deleted"] and deleted["completed_at"]
    assert store.reports == []


async def test_audit_log_keeps_no_user_id_after_deletion_and_expires(env):
    _, stores, _, _, store, ops = env
    await stores.set_relationship(USER, "char_1", 1.0, "test", NOW)
    store.audit.append({"action": "consent_granted", "target_user_id": USER, "ts": NOW - timedelta(days=400)})
    store.audit.append({"action": "consent_granted", "target_user_id": USER, "ts": NOW - timedelta(days=10)})
    await ops.delete(USER)
    # 削除の記録は残るが、user_id は、元に戻せない別の値(同じ人の行は、同じ値)に置き換わる
    targets = {entry["target_user_id"] for entry in store.audit}
    assert len(store.audit) == 3 and USER not in targets and len(targets) == 1
    # 保持期間(既定 1年)を過ぎた行は、消える
    assert await ops.purge_audit_logs(datetime.now(UTC), EngineSettings().audit_log_retention_days) == 1
    assert len(store.audit) == 2
