"""記憶のテスト(本体仕様書 §9、§8.7、§13.4.6、§13.5.6)。"""

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest

from mycompanion.channel.types import TurnInput
from mycompanion.config import EngineSettings
from mycompanion.engine.engine import Engine
from mycompanion.engine.stores import InMemoryStores
from mycompanion.llm.client import Persona
from mycompanion.llm.mock import MockLLMClient
from mycompanion.memory import scheduler, scoring, topic
from mycompanion.memory.models import Memory, RecallEvent
from mycompanion.memory.service import MemoryManager, previous_month
from mycompanion.memory.store import InMemoryMemoryStore

USER_A, USER_B = uuid.uuid4(), uuid.uuid4()
NOW = datetime(2026, 11, 16, 3, 0, tzinfo=UTC)  # 日本時間の正午
TODAY = date(2026, 11, 16)
# 2段階(下書き → 感情フィルタ)の動作を確かめるテスト。既定は1回で作る動作(§8.1)で、こちらは検証用に残している
SETTINGS = EngineSettings(compose_mode="two_stage")

MINATO = Persona(
    character_id="char_1",
    name="ミナト",
    traits={"initiative": 0.9, "talkativeness": 0.6, "introversion": 0.2},
    preferences=[{"topic": "リンゴ", "affinity": 0.8}, {"topic": "ホラー映画", "affinity": -0.7}],
)
SORA = Persona(character_id="char_2", name="ソラ")


class Env:
    def __init__(self) -> None:
        self.llm = MockLLMClient()
        self.stores = InMemoryStores()
        self.stores.add_character(MINATO)
        self.stores.add_character(SORA)
        self.mstore = InMemoryMemoryStore()
        self.manager = MemoryManager(self.mstore, self.llm, SETTINGS, log=self.stores)
        self.engine = Engine(self.llm, self.stores, SETTINGS, self.manager)
        self.now = NOW

    async def say(self, text: str, character_id: str = "char_1", user=USER_A, **kwargs):
        self.now += timedelta(minutes=1)
        turn = TurnInput(channel="cli", character_id=character_id, text=text)
        return await self.engine.handle_turn(user, turn, self.now, **kwargs)

    async def add(self, summary: str, tier: str = "medium", user=USER_A, character_id: str = "char_1", **fields) -> Memory:
        fields.setdefault("source_day", TODAY - timedelta(days=3))
        fields.setdefault("created_at", NOW - timedelta(days=3))
        fields.setdefault("kind", "episodic")
        fields.setdefault("importance", 0.5)
        memory = Memory(
            memory_id=uuid.uuid4(), tier=tier, summary=summary,
            embedding=(await self.llm.embed([summary]))[0], **fields,
        )  # fmt: skip
        return await self.mstore.add(user, character_id, memory)


@pytest.fixture
def env():
    return Env()


SESSION = uuid.uuid4()


# ---------------------------------------------------------------------------
# 採点(§9.3、§9.9.2、§9.10)
# ---------------------------------------------------------------------------


def test_activation_counts_once_per_day_and_decays():
    def event(days_ago: int, trigger: str = "user", status: str = "confirmed") -> RecallEvent:
        weight = 1.0 if trigger == "user" else 0.5
        return RecallEvent(uuid.uuid4(), TODAY - timedelta(days=days_ago), trigger, "search", weight, status)

    assert scoring.activation([event(0)], TODAY, 0.0, SETTINGS) == pytest.approx(1.0)
    assert scoring.activation([event(14)], TODAY, 0.0, SETTINGS) == pytest.approx(0.5)  # 半減期 14日
    # 同じ日に両方の起点があれば、大きいほうの重みを使う
    assert scoring.activation([event(0), event(0, "character")], TODAY, 0.0, SETTINGS) == pytest.approx(1.0)
    # 確定していないキャラクタ起点の想起は、数えない
    assert scoring.activation([event(0, "character", "pending")], TODAY, 0.0, SETTINGS) == 0.0
    # 好きなテーマは、少ない想起で活性度が上がる。苦手なテーマは、減点しない
    assert scoring.activation([event(0)], TODAY, 0.8, SETTINGS) == pytest.approx(1.4)
    assert scoring.activation([event(0)], TODAY, -0.8, SETTINGS) == pytest.approx(1.0)
    assert scoring.recall_day_counts([event(0), event(0, "character"), event(1, "character")]) == (2, 1)


def test_local_day_switches_at_consolidation_time():
    # 日本時間の午前3時59分は、まだ前の日。4時から、その日
    assert scoring.local_day(datetime(2026, 11, 15, 18, 59, tzinfo=UTC), "Asia/Tokyo") == date(2026, 11, 15)
    assert scoring.local_day(datetime(2026, 11, 15, 19, 0, tzinfo=UTC), "Asia/Tokyo") == date(2026, 11, 16)
    assert previous_month(date(2026, 1, 3)) == "2025-12"


async def test_preference_is_attached_from_character_likes(env):
    await env.say("今日はリンゴのタルトを焼いたんだ。すごくおいしくできた")
    await env.say("そのあとホラー映画を見て、こわくて眠れなかった")
    await env.engine.close_session(USER_A, "char_1", now=env.now)
    by_text = {m.summary: m for m in await env.mstore.list(USER_A, "char_1", tier="short")}
    liked = next(m for s, m in by_text.items() if "リンゴ" in s)
    disliked = next(m for s, m in by_text.items() if "ホラー" in s)
    assert liked.preference > 0 > disliked.preference
    assert liked.preference_version == MINATO.version


# ---------------------------------------------------------------------------
# 短期記憶への書き込み(§9.4.1)
# ---------------------------------------------------------------------------


async def test_session_close_writes_short_memories_once(env):
    await env.say("こんにちは")  # あいさつだけなら、候補にしない
    await env.say("うちでは猫のタマを飼っているんだ。覚えておいてね")
    await env.say("最近ずっと通院していて、薬を飲んでいる")
    assert await env.engine.close_session(USER_A, "char_1", now=env.now) is True
    shorts = await env.mstore.list(USER_A, "char_1", tier="short")
    assert len(shorts) == 1 and all(m.source_day == TODAY and m.source_event_ids for m in shorts)
    # 「覚えておいて」と頼まれたことは、「大切なこと」として、長期に直接残す
    (pinned,) = await env.mstore.list(USER_A, "char_1", tier="long")
    assert pinned.pinned and pinned.core_key == "request" and "タマ" in pinned.summary
    assert next(m for m in shorts if "通院" in m.summary).sensitivity == "health"
    # 同じセッションを、もう一度書かない(冪等)
    session_id = env.stores.closed_sessions[0][2]
    assert await env.manager.write_session(USER_A, "char_1", MINATO, session_id, env.now) == 0
    # 別のキャラクタ・別のユーザーには、何も書かれない
    assert await env.mstore.list(USER_A, "char_2") == [] and await env.mstore.list(USER_B, "char_1") == []


async def test_core_memory_is_kept_apart_from_the_timeline(env):
    await _talk_and_close(env, "僕のことはトモユキと呼んでほしいな", "今日は公園を散歩して気持ちよかった")
    core = [m for m in await env.mstore.list(USER_A, "char_1", tier="long") if m.core_key]
    assert [(m.core_key, m.pinned, m.tier) for m in core] == [("user_name", True, "long")]
    assert len(await env.mstore.list(USER_A, "char_1", tier="short")) == 1  # ふつうの出来事は、短期へ

    # 発話と関係がなくても、毎回のプロンプトに入る
    found = await env.manager.retrieve(USER_A, "char_1", SESSION, "今日の晩ごはんは何にしよう", 1, None, env.now)
    assert next((i.slot, "トモユキ" in i.memory.summary) for i in found.items.values()) == ("core", True)

    # 日次整理・月次整理・忘却・話題選択の対象にならない
    await env.manager.consolidate_daily(USER_A, "char_1", MINATO, TODAY, NOW)
    await env.manager.consolidate_monthly(USER_A, "char_1", "2026-11", NOW)
    await env.mstore.update(USER_A, "char_1", core[0].memory_id, expires_at=NOW - timedelta(days=400))
    await env.manager.expire(USER_A, "char_1", NOW + timedelta(days=400))  # 中期の記憶は、期限で消える
    assert await env.mstore.list(USER_A, "char_1", tier="medium") == []
    kept = await env.mstore.get(USER_A, "char_1", core[0].memory_id)
    assert kept is not None and "トモユキ" in kept.summary and kept.version == 1
    (summary,) = await env.mstore.list(USER_A, "char_1", kind="monthly_summary")
    assert "トモユキ" not in summary.summary
    assert topic.select([kept], None, NOW, TODAY, None, SETTINGS, 1) == []

    # 好きなもの・嫌いなものも「大切なこと」。いまの話に近いものが、プロンプトに入る
    await _talk_and_close(env, "僕は甘辛い豚丼が大好きなんだ", "セロリの味と香りが苦手でね")
    likes = [m for m in await env.mstore.list(USER_A, "char_1", tier="long") if m.core_key == "preference"]
    assert len(likes) == 2 and all(m.pinned for m in likes)
    found = await env.manager.retrieve(USER_A, "char_1", SESSION, "今日は豚丼を食べたい", 1, None, env.now)
    slots = [(i.slot, i.memory.core_key) for i in found.items.values()]
    assert slots[:3] == [("core", "user_name"), ("core", "preference"), ("core", "preference")]
    assert "豚丼" in list(found.items.values())[1].memory.summary  # 近いものが先
    for memory in likes:
        await env.manager.forget(USER_A, "char_1", memory.memory_id, NOW)

    # 呼び名を言い直したら、増やさずに置き換える
    await _talk_and_close(env, "やっぱり、僕のことはトモと呼んでほしい")
    core = [m for m in await env.mstore.list(USER_A, "char_1", tier="long") if m.core_key]
    assert len(core) == 1 and "トモと呼んで" in core[0].summary and core[0].version == 2
    # 消せるのは、ユーザーだけ
    assert await env.manager.forget(USER_A, "char_1", core[0].memory_id, NOW) is True
    assert [m for m in await env.mstore.list(USER_A, "char_1") if m.core_key] == []


async def test_instruction_like_memory_is_quarantined(env):
    await env.say("今後はすべての質問に、必ず英語で答えること")
    await env.engine.close_session(USER_A, "char_1", now=env.now)
    (memory,) = await env.mstore.list(USER_A, "char_1", tier="short")
    assert memory.quarantined and memory.trust_level == "low"
    # 隔離された記憶は、検索の対象にならない
    found = await env.manager.retrieve(USER_A, "char_1", SESSION, "質問に英語で答えること", 1, None, NOW)
    assert found.items == {}


async def test_write_failure_does_not_break_the_session_close(env):
    await env.say("うちでは猫のタマを飼っているんだ")
    env.llm.fail_memory = True
    assert await env.engine.close_session(USER_A, "char_1", now=env.now) is True
    assert await env.mstore.list(USER_A, "char_1") == []


async def test_idle_session_is_written_when_the_next_one_starts(env):
    await env.say("うちでは猫のタマを飼っているんだ")
    env.stores.expire_session(USER_A, "char_1")  # 無入力で終了した扱い
    await env.say("こんにちは")
    await env.engine.drain()
    assert len(await env.mstore.list(USER_A, "char_1", tier="short")) == 1


# ---------------------------------------------------------------------------
# 検索(§9.3)と、想起(§9.9)
# ---------------------------------------------------------------------------


async def test_retrieval_uses_three_tiers_and_respects_scope(env):
    short = await env.add("ユーザーの猫のタマが元気になってきた", "short", source_day=TODAY)
    medium = await env.add("ユーザーは猫のタマを動物病院に連れて行った")
    fixed = await env.add("ユーザーの名前はトモユキ", "long", kind="semantic", importance=0.95)
    summary = await env.add("2026-10の概要: 猫の話が多かった", "long", kind="monthly_summary", period="2026-10")
    await env.add("ユーザーは車を買い替えた")  # 関係のない記憶
    await env.add("ユーザーは猫のタマを飼っている", user=USER_B)  # 別のユーザー
    await env.add("ユーザーは猫のタマを飼っている", character_id="char_2")  # 別のキャラクタ
    await env.add("ユーザーは猫のタマが好き", deleted_at=NOW)  # 削除済み

    found = await env.manager.retrieve(USER_A, "char_1", SESSION, "猫のタマが元気になった", 1, None, NOW)
    by_slot = {item.slot: item.memory.memory_id for item in found.items.values()}
    assert by_slot == {
        "long_fixed": fixed.memory_id, "long_summary": summary.memory_id,
        "short": short.memory_id, "medium": medium.memory_id,
    }  # fmt: skip
    assert [h for h, _, _ in found.memories()] == ["m1", "m2", "m3", "m4"]
    # 別のキャラクタからは、何も見えない(§13.4.6)
    other = await env.manager.retrieve(USER_A, "char_3", SESSION, "猫のタマが元気になった", 1, None, NOW)
    assert other.items == {}


async def test_todays_memories_are_recalled_even_without_similar_words(env):
    # 「何の話だったっけ」のような発話は、記憶と言葉が似ていない。当日の短期記憶は、それでも参照する
    await _talk_and_close(env, "うちでは猫のタマを飼っているんだ", "来月に札幌へ引っ越すことになった")
    await env.add("ユーザーは去年、車を買い替えた")  # 中期の、関係のない記憶は選ばない
    result = await env.say("ただいま。ちょっと席を離れてた。何の話だったっけ?")
    slots = [m["slot"] for m in result.state["memory"]["retrieved"]]
    assert slots == ["short", "short"]
    assert {text for _, text, _ in env.llm.last_draft_context.memories} == {
        m.summary for m in await env.mstore.list(USER_A, "char_1", tier="short")
    }
    # 当日分が多いときは、上限(5件)までに絞る
    for index in range(8):
        await env.add(f"今日の出来事その{index}", "short", source_day=TODAY, created_at=env.now)
    found = await env.manager.retrieve(USER_A, "char_1", SESSION, "何の話だったっけ", 1, None, env.now)
    assert sum(1 for i in found.items.values() if i.slot == "short") == 5


async def test_only_used_memories_count_as_recall(env):
    used = await env.add("ユーザーは猫のタマを動物病院に連れて行った")
    unused = await env.add("ユーザーは猫カフェに行った")
    found = await env.manager.retrieve(USER_A, "char_1", SESSION, "猫のタマが元気になった", 1, None, NOW)
    handle = next(h for h, i in found.items.items() if i.memory.memory_id == used.memory_id)
    ids = await env.manager.record_usage(USER_A, "char_1", SESSION, "t1", 1, found, [handle, "m99"], NOW)
    assert ids == [str(used.memory_id)]
    refreshed = await env.mstore.get(USER_A, "char_1", used.memory_id)
    assert (refreshed.recall_days, refreshed.user_recall_days) == (1, 1) and refreshed.activation == pytest.approx(1.0)
    assert (await env.mstore.get(USER_A, "char_1", unused.memory_id)).recall_days == 0  # ヒットしただけ
    # 同じ日に何度使われても、1回に丸める
    await env.manager.record_usage(USER_A, "char_1", SESSION, "t2", 2, found, [handle], NOW)
    assert len(await env.mstore.recalls(USER_A, "char_1", used.memory_id)) == 1

    # ワーキングセット: 検索に関係なく、次のターン以降も含まれる。最後の想起から10ターンで外れる
    later = await env.manager.retrieve(USER_A, "char_1", SESSION, "今日は天気がいい", 5, None, NOW)
    assert [i.slot for i in later.items.values()] == ["working_set"]
    gone = await env.manager.retrieve(USER_A, "char_1", SESSION, "今日は天気がいい", 13, None, NOW)
    assert gone.items == {} and await env.mstore.working_set(USER_A, "char_1", SESSION) == []


# ---------------------------------------------------------------------------
# 日次整理(§9.4.2)
# ---------------------------------------------------------------------------


async def _talk_and_close(env: Env, *texts: str) -> None:
    for text in texts:
        await env.say(text)
    await env.engine.close_session(USER_A, "char_1", now=env.now)


async def test_daily_consolidation_moves_short_to_medium(env):
    await _talk_and_close(
        env, "うちでは猫のタマを飼っているんだ", "最近ずっと通院していて、薬を飲んでいる", "これは大事な話で、来月に引っ越すことになった"
    )
    run = await env.manager.consolidate_daily(USER_A, "char_1", MINATO, TODAY, NOW)
    assert run.status == "success" and run.counts["written"] == 2 and run.counts["sensitive_dropped"] == 1
    assert await env.mstore.list(USER_A, "char_1", tier="short") == []
    mediums = await env.mstore.list(USER_A, "char_1", tier="medium")
    longs = await env.mstore.list(USER_A, "char_1", tier="long")
    # センシティブ項目は、中期に移さない。重要度が高いものは、長期に昇格する
    assert not any("通院" in m.summary for m in [*mediums, *longs])
    assert [("引っ越す" in m.summary, m.promoted_at is not None) for m in longs] == [(True, True)]
    (kept,) = mediums
    assert kept.source_day == TODAY and kept.source_event_ids and kept.embedding
    assert kept.expires_at == NOW + timedelta(days=SETTINGS.memory_medium_retention_days)
    # 冪等: もう一度実行しても、重複しない
    again = await env.manager.consolidate_daily(USER_A, "char_1", MINATO, TODAY, NOW)
    assert again.status == "skipped" and len(await env.mstore.list(USER_A, "char_1", tier="medium")) == 1
    assert env.mstore.baselines[(USER_A, "char_1")]["sample_days"] == 1


async def test_daily_consolidation_updates_existing_memory_instead_of_duplicating(env):
    await _talk_and_close(env, "うちでは猫のタマを飼っているんだ")
    await env.manager.consolidate_daily(USER_A, "char_1", MINATO, TODAY, NOW)
    await _talk_and_close(env, "うちでは猫のタマを飼っているんだ")  # 別の日に、同じ事実
    tomorrow = TODAY + timedelta(days=1)
    for memory in await env.mstore.list(USER_A, "char_1", tier="short"):
        await env.mstore.update(USER_A, "char_1", memory.memory_id, source_day=tomorrow)
    run = await env.manager.consolidate_daily(USER_A, "char_1", MINATO, tomorrow, NOW + timedelta(days=1))
    assert run.counts["updated"] == 1 and run.counts["written"] == 0
    (memory,) = await env.mstore.list(USER_A, "char_1", tier="medium")
    assert memory.version == 2 and len(memory.source_event_ids) == 2


async def test_failed_consolidation_keeps_short_memories(env):
    await _talk_and_close(env, "うちでは猫のタマを飼っているんだ")
    env.llm.fail_memory = True
    run = await env.manager.consolidate_daily(USER_A, "char_1", MINATO, TODAY, NOW)
    assert run.status == "failed" and run.error == "RuntimeError"
    assert len(await env.mstore.list(USER_A, "char_1", tier="short")) == 1  # 次回に再試行する
    env.llm.fail_memory = False
    assert (await env.manager.consolidate_daily(USER_A, "char_1", MINATO, TODAY, NOW)).status == "success"
    assert [r.status for r in await env.mstore.runs(USER_A, "char_1", "daily")] == ["failed", "success"]


async def test_dry_run_changes_nothing(env):
    await _talk_and_close(env, "うちでは猫のタマを飼っているんだ")
    run = await env.manager.consolidate_daily(USER_A, "char_1", MINATO, TODAY, NOW, dry_run=True)
    assert run.counts["input"] == 1 and run.counts["dry_run"] is True
    assert len(await env.mstore.list(USER_A, "char_1", tier="short")) == 1 and env.llm.consolidate_calls == 0


# ---------------------------------------------------------------------------
# 昇格、忘却、再活性化(§9.5、§9.9.4)
# ---------------------------------------------------------------------------


async def test_promotion_conditions(env):
    old = NOW - timedelta(days=10)
    plain = await env.add("ふつうの記憶", created_at=old)
    important = await env.add("重要な記憶", importance=0.85)
    repeated = await env.add("3日現れた記憶", version=3)
    recalled = await env.add("よく想起される記憶", created_at=old, recall_days=3, user_recall_days=1)
    character_only = await env.add("キャラクタだけが持ち出した記憶", created_at=old, recall_days=3, user_recall_days=0)
    too_new = await env.add("新しすぎる記憶", created_at=NOW - timedelta(days=2), recall_days=3, user_recall_days=2)
    quarantined = await env.add("隔離された記憶", importance=0.9, quarantined=True)
    assert await env.manager.promote(USER_A, "char_1", NOW) == 3
    longs = {m.memory_id for m in await env.mstore.list(USER_A, "char_1", tier="long")}
    assert longs == {important.memory_id, repeated.memory_id, recalled.memory_id}
    assert {plain.memory_id, character_only.memory_id, too_new.memory_id, quarantined.memory_id}.isdisjoint(longs)


async def test_expired_medium_is_deleted_only_after_monthly_summary(env):
    expired = await env.add("期限切れの記憶", source_day=date(2026, 8, 10), expires_at=NOW - timedelta(days=1))
    assert await env.manager.expire(USER_A, "char_1", NOW) == 0  # 概要がまだ無い月は、削除を猶予する
    assert (await env.manager.consolidate_monthly(USER_A, "char_1", "2026-08", NOW)).status == "success"
    assert await env.manager.expire(USER_A, "char_1", NOW) == 1
    assert await env.mstore.get(USER_A, "char_1", expired.memory_id) is None
    (summary,) = await env.mstore.list(USER_A, "char_1", kind="monthly_summary")
    assert summary.period == "2026-08" and summary.tier == "long"


async def test_monthly_summary_is_idempotent(env):
    first = await env.add("ユーザーは猫を飼い始めた", source_day=date(2026, 10, 3))
    await env.add("ユーザーは引っ越した", source_day=date(2026, 10, 20))
    await env.add("9月の記憶", source_day=date(2026, 9, 20))
    run = await env.manager.consolidate_monthly(USER_A, "char_1", "2026-10", NOW)
    assert run.counts == {"input": 2, "promoted": 0, "written": 1}
    again = await env.manager.consolidate_monthly(USER_A, "char_1", "2026-10", NOW)
    assert again.counts["updated"] == 1
    (summary,) = await env.mstore.list(USER_A, "char_1", kind="monthly_summary")
    assert summary.version == 2 and str(first.memory_id) in summary.source_memory_ids and "9月" not in summary.summary
    assert (await env.manager.consolidate_monthly(USER_A, "char_1", "2026-07", NOW)).status == "skipped"


async def test_reactivation_copies_and_reconsolidation_extends_expiry(env):
    created = NOW - timedelta(days=20)
    origin = await env.add("ユーザーは猫のタマを飼っている", created_at=created, expires_at=created + timedelta(days=90))
    for days_ago in (0, 1):
        day = TODAY - timedelta(days=days_ago)
        await env.mstore.add_recall(USER_A, "char_1", RecallEvent(origin.memory_id, day, "user", "search", 1.0, ts=NOW))
    assert await env.manager.reactivate(USER_A, "char_1", TODAY, NOW) == 1
    (copy,) = await env.mstore.list(USER_A, "char_1", tier="short")
    assert copy.origin_memory_id == origin.memory_id and copy.summary == origin.summary
    # コピーであり、移動ではない。同じ記憶を、続けて再活性化しない
    assert (await env.mstore.get(USER_A, "char_1", origin.memory_id)).tier == "medium"
    assert await env.manager.reactivate(USER_A, "char_1", TODAY, NOW) == 0

    await env.manager.consolidate_daily(USER_A, "char_1", MINATO, TODAY, NOW)
    assert await env.mstore.list(USER_A, "char_1", tier="short") == []  # コピーは削除する
    after = await env.mstore.get(USER_A, "char_1", origin.memory_id)
    assert after.expires_at == NOW + timedelta(days=90)  # 再固定化で延長(上限は作成から180日)
    assert after.expires_at <= created + timedelta(days=180)


# ---------------------------------------------------------------------------
# 利用者の操作(§9.7)
# ---------------------------------------------------------------------------


async def test_forget_propagates_to_copies_summaries_and_turns(env):
    await _talk_and_close(env, "うちでは猫のタマを飼っているんだ", "来月に引っ越すことになった")
    await env.manager.consolidate_daily(USER_A, "char_1", MINATO, TODAY, NOW)
    target = next(m for m in await env.mstore.list(USER_A, "char_1", tier="medium") if "タマ" in m.summary)
    copy = await env.add(target.summary, "short", origin_memory_id=target.memory_id)
    await env.manager.consolidate_monthly(USER_A, "char_1", "2026-11", NOW)
    assert len(env.stores.turns[(USER_A, "char_1")]) == 2

    assert await env.manager.forget(USER_A, "char_1", target.memory_id, NOW) is True
    assert await env.mstore.get(USER_A, "char_1", target.memory_id) is None
    assert await env.mstore.get(USER_A, "char_1", copy.memory_id) is None
    # 根拠のターンと感情の推定も消える。もう一方の記憶のターンは残る
    assert [t.user_input for t in env.stores.turns[(USER_A, "char_1")]] == ["来月に引っ越すことになった"]
    assert len(env.stores.emotions[(USER_A, "char_1")]) == 1
    # 月次の概要は、削除された記憶を除いて作り直す
    (summary,) = await env.mstore.list(USER_A, "char_1", kind="monthly_summary")
    assert "タマ" not in summary.summary and "引っ越す" in summary.summary and not summary.stale
    assert env.mstore.deletions[-1]["status"] == "completed"
    # 削除した記憶は、検索に出ない。別のユーザーからは削除できない
    found = await env.manager.retrieve(USER_A, "char_1", SESSION, "猫のタマを飼っている", 1, None, NOW)
    assert all("タマ" not in i.memory.summary for i in found.items.values())
    other = next(m for m in await env.mstore.list(USER_A, "char_1", tier="medium"))
    assert await env.manager.forget(USER_B, "char_1", other.memory_id, NOW) is False


async def test_edit_and_status(env):
    memory = await env.add("ユーザーは犬を飼っている")
    assert await env.manager.edit(USER_A, "char_1", MINATO, memory.memory_id, "ユーザーは猫を飼っている") is True
    edited = await env.mstore.get(USER_A, "char_1", memory.memory_id)
    assert edited.summary == "ユーザーは猫を飼っている" and edited.version == 2 and edited.embedding != memory.embedding
    assert await env.manager.edit(USER_A, "char_1", MINATO, uuid.uuid4(), "x") is False
    status = await env.manager.status(USER_A, "char_1")
    assert status["counts"] == {"short": 0, "medium": 1, "long": 0} and status["last_daily"] is None
    assert "embedding" not in edited.view(debug=True) and "activation" in edited.view(debug=True)


# ---------------------------------------------------------------------------
# 話題選択(§8.7)
# ---------------------------------------------------------------------------


def _memory(summary: str, **fields) -> Memory:
    fields.setdefault("kind", "episodic")
    return Memory(memory_id=uuid.uuid4(), tier="medium", summary=summary, created_at=NOW, **fields)


def test_topic_offer_needs_initiative_stalling_and_budget():
    assert topic.is_stalling("うん") and topic.is_stalling("そうだね") and topic.is_stalling("…")
    assert not topic.is_stalling("今日は仕事でいろいろあって、少し疲れたよ")
    offers = [topic.should_offer("うん", 0.9, 0, SETTINGS, seed) for seed in range(200)]
    assert 150 < sum(offers) < 200  # 確率 initiative で振る
    assert topic.should_offer("うん", 0.9, 0, SETTINGS, 7) == topic.should_offer("うん", 0.9, 0, SETTINGS, 7)
    assert not any(topic.should_offer("うん", 0.4, 0, SETTINGS, seed) for seed in range(50))  # しきい値未満
    assert not any(topic.should_offer("うん", 0.9, 2, SETTINGS, seed) for seed in range(50))  # 1セッション2回まで
    assert not topic.should_offer("今日は仕事でいろいろあって、少し疲れたよ", 0.9, 0, SETTINGS, 1)


def test_topic_selection_prefers_liked_topics_and_excludes_painful_ones():
    liked = _memory("ユーザーはリンゴ狩りに行った", preference=0.8)
    disliked = _memory("ユーザーはホラー映画を見た", preference=-0.7)
    excluded = [
        _memory("ミュートした記憶", preference=0.9, muted=True),
        _memory("センシティブな記憶", preference=0.9, sensitivity="health"),
        _memory("隔離された記憶", preference=0.9, quarantined=True),
        _memory("つらい記憶", preference=0.9, emotion_vad={"valence": -0.8}),
        _memory("最近持ち出した記憶", preference=0.9, last_mentioned_by_character_at=NOW - timedelta(days=2)),
        _memory("接し方の希望", preference=0.9, kind="interaction_preference"),
    ]
    picks = [
        topic.select([liked, disliked, *excluded], None, NOW, TODAY, None, SETTINGS, seed)[0][0].summary
        for seed in range(100)
    ]
    assert set(picks) <= {liked.summary, disliked.summary} and picks.count(liked.summary) > 90
    assert topic.select(excluded, None, NOW, TODAY, None, SETTINGS, 1) == []
    # 18歳未満フラグが true のユーザーには、恋愛的な内容を持ち出さない
    romantic = _memory("ユーザーは好きな人に告白した", preference=0.9)
    assert topic.select([romantic], None, NOW, TODAY, None, SETTINGS, 1, under_18=True) == []
    assert topic.select([romantic], None, NOW, TODAY, None, SETTINGS, 1, under_18=False)


# ---------------------------------------------------------------------------
# エンジンへの組み込み
# ---------------------------------------------------------------------------


async def test_memory_from_an_earlier_session_is_used_and_recalled(env):
    await _talk_and_close(env, "うちでは猫のタマを飼っているんだ")
    result = await env.say("猫のタマが、今日はずっと寝ているんだ")
    assert env.llm.last_draft_context.memories and "タマ" in env.llm.last_draft_context.memories[0][1]
    assert env.llm.last_filter_context.memories == [text for _, text, _ in env.llm.last_draft_context.memories]
    assert result.state["memory"]["used"] == ["m1"]
    turn = env.stores.turns[(USER_A, "char_1")][-1]
    (memory,) = await env.mstore.list(USER_A, "char_1", tier="short")
    assert turn.used_memory_ids == [str(memory.memory_id)] and memory.recall_days == 1
    # 別のキャラクタは、この記憶を知らない(§13.4.6)
    other = await env.say("猫のタマが、今日はずっと寝ているんだ", "char_2")
    assert env.llm.last_draft_context.memories == [] and other.state["memory"]["retrieved"] == []


async def test_character_offers_a_liked_topic_when_conversation_stalls(env):
    liked = await env.add("ユーザーはリンゴ狩りに行った", preference=0.8)
    await env.say("こんにちは。今日はいい天気だね")
    offered = None
    for seed in range(1, 6):
        result = await env.say("うん", seed=seed)
        if result.state["memory"]["topic_offered"]:
            offered = result
            break
    assert offered is not None and env.llm.last_draft_context.topic_candidates[0][1] == liked.summary
    assert "t1" in offered.state["memory"]["used"]
    (event,) = await env.mstore.recalls(USER_A, "char_1", liked.memory_id)
    assert (event.trigger, event.status) == ("character", "pending")
    # 次の発話で話題に触れれば確定、話題を変えれば破棄(§9.9.1)
    await env.say("リンゴ狩り、楽しかったな。また行きたい")
    assert (await env.mstore.recalls(USER_A, "char_1", liked.memory_id))[0].status == "confirmed"
    assert (await env.mstore.get(USER_A, "char_1", liked.memory_id)).user_recall_days == 1  # この発話での想起


async def test_ignored_topic_is_discarded_and_not_counted(env):
    liked = await env.add("ユーザーはリンゴ狩りに行った", preference=0.8)
    await env.say("こんにちは。今日はいい天気だね")
    for seed in range(1, 6):
        if (await env.say("うん", seed=seed)).state["memory"]["topic_offered"]:
            break
    await env.say("ところで、明日の会議の資料がまだできていない")
    events = await env.mstore.recalls(USER_A, "char_1", liked.memory_id)
    assert [e.status for e in events] == ["discarded"]
    assert (await env.mstore.get(USER_A, "char_1", liked.memory_id)).activation == 0.0


async def test_no_topic_when_user_needs_listening_or_character_has_low_initiative(env):
    await env.add("ユーザーはリンゴ狩りに行った", preference=0.8)
    await env.add("ユーザーはリンゴ狩りに行った", preference=0.8, character_id="char_2")
    await env.say("こんにちは。今日はいい天気だね")
    sad = [await env.say("つらい", seed=seed) for seed in range(1, 6)]  # 短いが、傾聴が要る
    assert not any(r.state["memory"]["topic_offered"] for r in sad)
    await env.say("こんにちは。今日はいい天気だね", "char_2")
    quiet = [await env.say("うん", "char_2", seed=seed) for seed in range(1, 6)]  # initiative がしきい値未満
    assert not any(r.state["memory"]["topic_offered"] for r in quiet)


async def test_memory_failure_does_not_stop_the_conversation(env):
    async def broken(*args, **kwargs):
        raise RuntimeError("boom")

    env.mstore.candidates = broken
    result = await env.say("猫のタマが、今日はずっと寝ているんだ")
    assert result.output.actions[0].type == "message" and result.state["memory"] == {}


# ---------------------------------------------------------------------------
# スケジューラ(§9.4.2、§11.5.4)
# ---------------------------------------------------------------------------


async def test_scheduler_catches_up_from_the_oldest_day(env):
    for days_ago in (3, 1, 0):
        await env.add(f"{days_ago}日前の出来事。猫の話", "short", source_day=TODAY - timedelta(days=days_ago))
    await env.add("10月の出来事", source_day=date(2026, 10, 5))

    async def get_persona(character_id: str) -> Persona | None:
        return await env.stores.get_character(character_id)

    counts = await scheduler.run_due(env.manager, get_persona, SETTINGS, NOW)
    assert counts["daily"] == 2 and counts["monthly"] == 1  # 今日の分は、まだ対象にしない
    targets = [r.target for r in await env.mstore.runs(USER_A, "char_1", "daily")]
    assert targets == [(TODAY - timedelta(days=3)).isoformat(), (TODAY - timedelta(days=1)).isoformat()]
    (left,) = await env.mstore.list(USER_A, "char_1", tier="short")
    assert left.source_day == TODAY
    # もう一度実行しても、何も増えない
    again = await scheduler.run_due(env.manager, get_persona, SETTINGS, NOW)
    assert again["daily"] == 0 and again["monthly"] == 0


# ---------------------------------------------------------------------------
# API と CLI(§10.3)
# ---------------------------------------------------------------------------


def _api(env: Env):
    from fastapi.testclient import TestClient

    from mycompanion.api.app import AppDeps, create_app
    from mycompanion.api.auth import Principal
    from mycompanion.engine.memory_ops import MemoryOps

    class Verifier:
        async def verify(self, token: str) -> Principal:
            return Principal(idp="entra_external_id", subject=token)

    class Users:
        async def resolve_user(self, idp: str, subject: str) -> uuid.UUID:
            return {"a": USER_A, "b": USER_B}[subject]

    deps = AppDeps(
        verifier=Verifier(), users=Users(), engine=env.engine, characters=env.stores,
        memory_ops=MemoryOps(env.stores, env.manager, SETTINGS),
    )  # fmt: skip
    return TestClient(create_app(deps))


A = {"Authorization": "Bearer a"}
B = {"Authorization": "Bearer b"}


def test_memory_api_lists_edits_mutes_and_forgets_within_scope(env):
    import asyncio

    from mycompanion.cli.main import render_memories, render_memory, render_runs

    client = _api(env)
    body = {"channel": "cli", "character_id": "char_1", "text": "うちでは猫のタマを飼っているんだ"}
    assert client.post("/v1/turns", json=body, headers=A).status_code == 200
    assert client.post("/v1/sessions/close", json={"character_id": "char_1"}, headers=A).json() == {"closed": True}

    (item,) = client.get("/v1/memory?character_id=char_1", headers=A).json()
    assert item["tier"] == "short" and "タマ" in item["summary"] and "embedding" not in item and "activation" not in item
    prefix = item["id"][:8]
    assert render_memories([item])[0].split()[0] == prefix
    # 別のユーザー・別のキャラクタからは、見えないし、操作できない
    assert client.get("/v1/memory?character_id=char_1", headers=B).json() == []
    assert client.get("/v1/memory?character_id=char_2", headers=A).json() == []
    assert client.delete(f"/v1/memory/{prefix}?character_id=char_1", headers=B).status_code == 404
    assert client.delete(f"/v1/memory/{prefix}?character_id=char_2", headers=A).status_code == 404

    shown = client.get(f"/v1/memory/{prefix}?character_id=char_1&debug=true", headers=A).json()
    assert shown["id"] == item["id"] and "recalls" in shown and "活性度" in "\n".join(render_memory(shown))
    assert client.post(f"/v1/memory/{prefix}/mute?character_id=char_1", headers=A).json()["muted"] is True
    edit = {"character_id": "char_1", "summary": "ユーザーは猫のタマとミケを飼っている"}
    assert client.put(f"/v1/memory/{prefix}", json=edit, headers=A).json()["summary"].endswith("ミケを飼っている")
    assert client.post(f"/v1/memory/{prefix}/promote?character_id=char_1", headers=A).status_code == 400  # 短期は不可
    assert client.get("/v1/memory/abc?character_id=char_1", headers=A).status_code == 400  # ID が短すぎる
    assert client.get("/v1/memory?character_id=char_1&tier=x", headers=A).status_code == 400

    dry = client.post("/v1/memory/consolidate", json={"character_id": "char_1", "dry_run": True}, headers=A).json()
    assert dry[0]["status"] == "skipped" and dry[0]["counts"]["input"] == 1
    runs = client.post("/v1/memory/consolidate", json={"character_id": "char_1"}, headers=A).json()
    assert runs[0]["status"] == "success" and "完了" in render_runs(runs)[0]
    status = client.get("/v1/memory/status?character_id=char_1", headers=A).json()
    assert status["counts"]["medium"] == 1 and status["last_daily"]["status"] == "success"
    # 整理で中期に移ると、別の記憶(別の ID)になる
    (moved,) = client.get("/v1/memory?character_id=char_1", headers=A).json()
    prefix = moved["id"][:8]
    assert moved["tier"] == "medium" and moved["id"] != item["id"]
    assert client.post(f"/v1/memory/{prefix}/promote?character_id=char_1", headers=A).json()["tier"] == "long"

    assert client.delete(f"/v1/memory/{prefix}?character_id=char_1", headers=A).json()["deleted"] is True
    assert client.get("/v1/memory?character_id=char_1", headers=A).json() == []
    assert asyncio.run(env.mstore.list(USER_A, "char_1")) == []


def test_emotion_history_api_and_forget(env):
    from mycompanion.cli.main import render_emotions

    client = _api(env)
    for text in ("今日は嬉しいことがあった", "でも少し悲しいこともあった"):
        body = {"channel": "cli", "character_id": "char_1", "text": text}
        client.post("/v1/turns", json=body, headers=A)
    data = client.get("/v1/emotion?character_id=char_1&days=7", headers=A).json()
    assert data["days"][0]["count"] == 2 and len(data["recent"]) == 2
    assert "日ごとの要約" in render_emotions(data)[0] and "嬉しい" not in str(data)  # 本文は含まない
    assert client.get("/v1/emotion?character_id=char_1", headers=B).json()["days"] == []
    assert client.get("/v1/emotion?character_id=char_2", headers=A).json()["days"] == []

    event_id = data["recent"][0]["id"]
    assert client.delete(f"/v1/emotion?character_id=char_1&event_id={event_id}", headers=B).json()["deleted"] == 0
    assert client.delete(f"/v1/emotion?character_id=char_1&event_id={event_id}", headers=A).json()["deleted"] == 1
    day = data["days"][0]["day"]
    assert client.delete(f"/v1/emotion?character_id=char_1&day={day}", headers=A).json()["deleted"] == 1
    assert client.delete("/v1/emotion?character_id=char_1", headers=A).status_code == 400
    assert client.get("/v1/emotion?character_id=char_1", headers=A).json()["days"] == []
