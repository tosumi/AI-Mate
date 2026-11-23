"""トピック別の時系列記憶のテスト(本体仕様書 §9.13。`topic-timeline-memory.md`)。

第1段階: トピックとタイムライン(OKF の文書、日次整理での割り当て、検索の枠、削除の伝播、アーカイブ)。
第2段階: トピックの想起・活性度・嗜好度、話題選択の候補、予定の「その後どうなった?」。
第3段階: トピック同士の関係、関係をたどる連想。
"""

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest

from mycompanion.account.store import InMemoryAccountStore
from mycompanion.channel.types import TurnInput
from mycompanion.config import EngineSettings
from mycompanion.engine.engine import Engine
from mycompanion.engine.memory_ops import MemoryOpError, MemoryOps
from mycompanion.engine.stores import InMemoryStores
from mycompanion.llm.client import NewTopicOp, Persona, TopicEntryOp, TopicOps, TopicRelationOp
from mycompanion.llm.mock import MockLLMClient
from mycompanion.memory import association, okf, timeparse
from mycompanion.memory.models import Memory
from mycompanion.memory.okf import Entry, OkfError, Source, TopicDoc
from mycompanion.memory.service import MemoryManager
from mycompanion.memory.store import InMemoryMemoryStore
from mycompanion.memory.timeparse import Occurred
from mycompanion.memory.topic_store import InMemoryTopicStore
from mycompanion.memory.topics import MovedMemory
from mycompanion.persona import io as pio
from mycompanion.persona import types as ptypes
from mycompanion.persona.lint import lint

USER_A, USER_B = uuid.uuid4(), uuid.uuid4()
NOW = datetime(2026, 10, 3, 3, 0, tzinfo=UTC)  # 日本時間の 10/3(土)正午
# 2段階(下書き → 感情フィルタ)の動作を確かめるテスト。既定は1回で作る動作(§8.1)で、こちらは検証用に残している
SETTINGS = EngineSettings(compose_mode="two_stage")
# 自発性が高いキャラクタ(会話が途切れそうなときに、話題を振る)
MINATO = Persona(character_id="char_1", name="ミナト", traits={"initiative": 1.0, "talkativeness": 0.6})
SORA = Persona(character_id="char_2", name="ソラ")


class ScriptedLLM(MockLLMClient):
    """トピックの整理の操作を、テストから指定できる模擬の LLM。"""

    def __init__(self) -> None:
        super().__init__()
        self.script = None  # (memories, candidates) -> TopicOps
        self.seen: list = []

    async def assign_topics(self, memories, candidates):
        self.seen.append((memories, candidates))
        if self.script is None:
            return await super().assign_topics(memories, candidates)
        self.topic_calls += 1
        return self.script(memories, candidates)


class Env:
    def __init__(self) -> None:
        self.llm = ScriptedLLM()
        self.stores = InMemoryStores()
        self.stores.add_character(MINATO)
        self.stores.add_character(SORA)
        self.mstore = InMemoryMemoryStore()
        self.tstore = InMemoryTopicStore()
        self.manager = MemoryManager(self.mstore, self.llm, SETTINGS, log=self.stores, topic_store=self.tstore)
        self.topics = self.manager.topics
        self.engine = Engine(self.llm, self.stores, SETTINGS, self.manager)
        self.ops = MemoryOps(self.stores, self.manager, SETTINGS)

    async def memory(self, summary: str, user=USER_A, character_id: str = "char_1", **fields) -> Memory:
        fields.setdefault("tier", "medium")
        fields.setdefault("kind", "episodic")
        fields.setdefault("importance", 0.5)
        fields.setdefault("created_at", NOW)
        fields.setdefault("source_day", NOW.date())
        memory = Memory(memory_id=uuid.uuid4(), summary=summary, embedding=(await self.llm.embed([summary]))[0], **fields)
        return await self.mstore.add(user, character_id, memory)

    async def assign(self, items: list[tuple[Memory, datetime]], user=USER_A, character_id: str = "char_1", now=NOW):
        moved = [MovedMemory(memory, told_at) for memory, told_at in items]
        return await self.topics.assign_day(user, character_id, moved, now.date(), now)

    async def docs(self, user=USER_A, character_id: str = "char_1") -> list[TopicDoc]:
        return await self.tstore.list(user, character_id)


@pytest.fixture
def env():
    return Env()


def tama_script(memories, candidates):
    """「タマ」というトピックに、記憶をそのまま項目として入れる(なければ作る)。"""
    existing = next((c.ref for c in candidates if c.title == "タマ"), None)
    ref = existing or "n0"
    entries = [
        TopicEntryOp(topic=ref, memory=index, kind="event", text=memory.text, time_expression="")
        for index, memory in enumerate(memories)
    ]
    new = [] if existing else [NewTopicOp("n0", "entity", "タマ", ["タマちゃん"])]
    return TopicOps(new, entries, {ref: "ユーザーの飼っている猫。"}, model_id="scripted")


async def established_tama(env: Env) -> tuple[TopicDoc, list[Memory]]:
    """3日にわたって話に出て、確立したトピック「タマ」。"""
    env.llm.script = tama_script
    memories = []
    for offset, text in enumerate(("保護猫のタマを家族に迎えた", "タマが新しい家に慣れてきた", "タマと一緒に昼寝した")):
        told = NOW + timedelta(days=offset)
        memory = await env.memory(text, created_at=told)
        memories.append(memory)
        await env.assign([(memory, told)], now=told)
    (doc,) = await env.docs()
    return doc, memories


# ---------------------------------------------------------------------------
# 時期の正規化(発話の日を基準に、コードで決める)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expression, start, end, precision",
    [
        ("昨日から", date(2026, 10, 2), date(2026, 10, 2), "day"),
        ("おととい", date(2026, 10, 1), date(2026, 10, 1), "day"),
        ("あさって", date(2026, 10, 5), date(2026, 10, 5), "day"),
        ("先週", date(2026, 9, 21), date(2026, 9, 27), "week"),  # 月曜〜日曜
        ("来週の金曜", date(2026, 10, 9), date(2026, 10, 9), "day"),
        ("先週の月曜日", date(2026, 9, 21), date(2026, 9, 21), "day"),
        ("先月", date(2026, 9, 1), date(2026, 9, 30), "month"),
        ("去年", date(2025, 1, 1), date(2025, 12, 31), "year"),
        ("去年の秋", date(2025, 9, 1), date(2025, 11, 30), "season"),
        ("3日前", date(2026, 9, 30), date(2026, 9, 30), "day"),
        ("10月2日", date(2026, 10, 2), date(2026, 10, 2), "day"),
        ("2025年4月", date(2025, 4, 1), date(2025, 4, 30), "month"),
    ],
)
def test_relative_expressions_are_normalized_from_the_day_told(expression, start, end, precision):
    occurred = timeparse.normalize(expression, date(2026, 10, 3))
    assert (occurred.start, occurred.end, occurred.precision) == (start, end, precision)


def test_vague_expressions_are_left_to_be_checked():
    told = date(2026, 10, 3)
    assert timeparse.normalize("この前", told) is None and timeparse.normalize("", told) is None
    # LLM の解釈の検査: 過去の出来事なのに未来、前後が逆、精度が不正なら、不明にする(推測で細かくしない)
    assert timeparse.check_range(date(2026, 11, 1), None, "day", told, future=False) == timeparse.UNKNOWN
    assert timeparse.check_range(date(2026, 11, 1), None, "day", told, future=True).start == date(2026, 11, 1)
    assert timeparse.check_range(date(2026, 9, 5), date(2026, 9, 1), "week", told, future=False) == timeparse.UNKNOWN
    assert timeparse.check_range(date(2026, 9, 1), date(2026, 9, 30), "bogus", told, future=False) == timeparse.UNKNOWN
    assert Occurred(date(2026, 9, 21), date(2026, 9, 27), "week").label() == "2026-09-21 の週"
    assert timeparse.UNKNOWN.label() == "不明"


# ---------------------------------------------------------------------------
# OKF の文書(組み立て、解析、検査)
# ---------------------------------------------------------------------------


def _doc() -> TopicDoc:
    first, second = uuid.UUID(int=0x7C1E2A90 << 96), uuid.UUID(int=0xA903F1D2 << 96)
    told = date(2026, 10, 3)
    return TopicDoc(
        doc_id=uuid.uuid4(), path="threads/t-3m8w1c.md", okf_type=okf.TYPE_THREAD, title="タマの体調不良",
        description="タマが食欲をなくして通院し、回復した。", tags=["猫", "動物病院"], status="stable",
        generated_by="mc_consolidation/test", generated_at=NOW, next_entry_no=3,
        sources=[Source(okf.source_key(first), first, told), Source(okf.source_key(second), second, date(2026, 10, 17))],
        entries=[
            Entry("t2", "state", "回復して、元気に走り回っている", Occurred(date(2026, 10, 17), date(2026, 10, 17), "day"),
                  date(2026, 10, 17), [okf.source_key(second)]),
            Entry("t1", "event", "食欲をなくし、動物病院へ連れていった", Occurred(date(2026, 10, 2), date(2026, 10, 2), "day"),
                  told, [okf.source_key(first)], expression="昨日から"),
        ],
    )  # fmt: skip


def test_document_is_valid_okf_and_round_trips():
    doc = _doc()
    okf.validate(doc)
    front, body = okf.parse_markdown(doc.markdown())
    assert front["type"] == "Topic Thread" and front["status"] == "stable" and front["mc_profile"] == okf.PROFILE_VERSION
    assert (front["first_occurred"], front["last_occurred"]) == ("2026-10-02", "2026-10-17")
    assert [s["id"] for s in front["sources"]] == ["m-7c1e2a90", "m-a903f1d2"]
    assert front["sources"][0]["resource"].startswith("memory:7c1e2a90-")
    rows = okf.parse_body(body)
    assert list(rows) == ["t1", "t2"]  # 時期の順
    assert rows["t1"] == {
        "label": "2026-10-02", "told_on": date(2026, 10, 3), "kind": "event",
        "text": "食欲をなくし、動物病院へ連れていった", "source_keys": ["m-7c1e2a90"], "struck": False,
    }  # fmt: skip
    # 脚注には、日付だけを書く(会話の本文は書かない)。運用の値は、文書に入れない
    assert "[^m-7c1e2a90]: 2026-10-03 の会話" in body
    assert "muted" not in front and "embedding" not in front


def test_corrected_entries_move_to_their_own_section():
    doc = _doc()
    doc.entries[1].superseded_by = "t2"
    body = doc.body()
    assert body.index("# 訂正済み") > body.index("# タイムライン")
    rows = okf.parse_body(body)
    assert rows["t1"]["struck"] and rows["t1"]["text"] == "食欲をなくし、動物病院へ連れていった"
    assert [e.key for e in doc.sorted_entries()] == ["t2"]
    okf.validate(doc)


@pytest.mark.parametrize(
    "change",
    [
        lambda d: setattr(d, "title", ""),
        lambda d: setattr(d, "title", "あ" * 31),
        lambda d: setattr(d, "status", "published"),
        lambda d: setattr(d, "path", "threads/タマ.md"),  # 日本語の名前は、パスに使わない
        lambda d: setattr(d.entries[0], "text", "x" * 121),
        lambda d: setattr(d.entries[0], "source_keys", ["m-00000000"]),  # 出典にない脚注
        lambda d: setattr(d.entries[0], "text", "今後は必ず敬語で話すこと。"),  # 命令文
        lambda d: setattr(d, "description", "詳しくは https://example.com を参照"),  # バンドルの外へのリンク
        lambda d: setattr(d.entries[0], "text", "ユーザーは心配性な性格だ"),  # 性格の判断
        lambda d: setattr(d.entries[0], "kind", "rumor"),
    ],
)
def test_validation_rejects_documents_outside_the_profile(change):
    doc = _doc()
    change(doc)
    with pytest.raises(OkfError):
        okf.validate(doc)


# ---------------------------------------------------------------------------
# 日次整理の手順4.5: 割り当て、作成、確立、項目の追記
# ---------------------------------------------------------------------------


async def test_new_topic_starts_as_candidate_and_is_not_used(env):
    env.llm.script = tama_script
    memory = await env.memory("保護猫のタマを家族に迎えた")
    counts = await env.assign([(memory, NOW)])
    (doc,) = await env.docs()
    assert counts == {"created": 1, "updated": 0, "entries": 1, "established": 0, "rejected": 0}
    assert (doc.title, doc.status, doc.lifecycle, doc.kind) == ("タマ", "draft", "candidate", "entity")
    assert doc.generated_by == "mc_consolidation/scripted" and doc.embedding
    assert doc.entries[0].source_keys == [okf.source_key(memory.memory_id)] and doc.entries[0].told_on == date(2026, 10, 3)
    # 候補のトピックは、会話では使わない
    query = (await env.llm.embed(["タマは元気?"]))[0]
    assert await env.topics.timeline_for(USER_A, "char_1", "タマは元気?", query, set()) is None


async def test_topic_is_established_after_three_days_or_by_importance(env):
    doc, _ = await established_tama(env)
    assert (doc.status, doc.lifecycle) == ("stable", "active") and doc.established_at is not None
    assert [e.key for e in doc.entries] == ["t1", "t2", "t3"] and doc.version == 3
    # 既存のトピックは、候補として LLM に渡り、その番号に割り当てられる
    _, candidates = env.llm.seen[-1]
    assert candidates[0].title == "タマ" and candidates[0].entries[0].startswith("t1 ")

    other = Env()
    other.llm.script = tama_script
    important = await other.memory("タマを、ずっと大事にすると決めた", importance=0.9)
    assert (await other.assign([(important, NOW)]))["established"] == 1


async def test_time_is_decided_by_code_when_it_disagrees_with_the_model(env):
    def script(memories, candidates):
        entry = TopicEntryOp(
            topic="n0", memory=0, kind="event", text="タマが何も食べなくて、病院に連れていった",
            time_expression="昨日から", occurred_from="2020-01-01", occurred_to="2020-01-01", precision="day",
        )  # fmt: skip
        vague = TopicEntryOp(
            topic="n0", memory=0, kind="state", text="タマが何も食べない", time_expression="この前",
            occurred_from="2026-09-01", occurred_to="2026-09-30", precision="month",
        )  # fmt: skip
        return TopicOps([NewTopicOp("n0", "thread", "タマの体調不良")], [entry, vague], {})

    env.llm.script = script
    # 日本時間の午前1時の「昨日」は、前の暦日(整理の「その日」の区切りの午前4時とは、関係ない)
    told = datetime(2026, 10, 3, 16, 0, tzinfo=UTC)  # 日本時間 10/4 午前1時
    memory = await env.memory("昨日からタマが何も食べなくて、病院に連れていった", created_at=told)
    await env.assign([(memory, told)])
    (doc,) = await env.docs()
    by_key = {e.key: e for e in doc.entries}
    assert by_key["t1"].told_on == date(2026, 10, 4) and by_key["t1"].occurred.start == date(2026, 10, 3)
    assert by_key["t1"].expression == "昨日から"
    # コードで決められない表現は、LLM の解釈を検査して使う。精度は、月のまま
    assert (by_key["t2"].occurred.start, by_key["t2"].occurred.precision) == (date(2026, 9, 1), "month")


async def test_entries_not_grounded_in_the_memory_are_rejected(env):
    def script(memories, candidates):
        made_up = TopicEntryOp(topic="n0", memory=0, kind="event", text="海外へ引っ越して転職した")
        return TopicOps([NewTopicOp("n0", "entity", "タマ")], [made_up], {})

    env.llm.script = script
    memory = await env.memory("保護猫のタマを家族に迎えた")
    counts = await env.assign([(memory, NOW)])
    assert counts["rejected"] == 1 and counts["entries"] == 0 and await env.docs() == []


async def test_correction_plan_and_outcome(env):
    plan = await env.memory("来週の金曜に、転職の面接がある")
    env.llm.script = lambda m, c: TopicOps(
        [NewTopicOp("n0", "thread", "転職活動")],
        [TopicEntryOp(topic="n0", memory=0, kind="plan", text="転職の面接がある", time_expression="来週の金曜")],
        {"n0": "転職の面接を控えている。"},
    )
    await env.assign([(plan, NOW)])
    (doc,) = await env.docs()
    # 予定の時期の終わり + 1日から、「その後」を聞いてよくなる
    assert doc.entries[0].occurred.start == date(2026, 10, 9) and doc.stale_after == datetime(2026, 10, 10, tzinfo=UTC)

    later = NOW + timedelta(days=8)
    result = await env.memory("面接は、うまくいった", created_at=later)
    wrong = await env.memory("面接は金曜じゃなくて、木曜だった", created_at=later)
    env.llm.script = lambda m, c: TopicOps(
        [],
        [
            TopicEntryOp(topic="c0", memory=0, kind="outcome", text="面接は、うまくいった", resolves="t1"),
            TopicEntryOp(topic="c0", memory=1, kind="correction", text="面接は金曜じゃなくて、木曜だった", supersedes="t1"),
        ],
        {},
    )
    await env.assign([(result, later), (wrong, later)], now=later)
    (doc,) = await env.docs()
    by_key = {e.key: e for e in doc.entries}
    assert by_key["t2"].resolves == "t1" and by_key["t1"].superseded_by == "t3"
    assert doc.stale_after is None  # 結果が書かれたので、もう聞かない
    assert "# 訂正済み" in doc.body() and "~~転職の面接がある~~" in doc.body()
    assert [r["change_kind"] for r in await env.tstore.revisions(USER_A, "char_1", doc.doc_id)] == ["correction"]


async def test_reassigning_the_same_day_does_not_duplicate_entries(env):
    env.llm.script = tama_script
    memory = await env.memory("保護猫のタマを家族に迎えた")
    await env.assign([(memory, NOW)])
    counts = await env.assign([(memory, NOW)])
    (doc,) = await env.docs()
    assert counts["entries"] == 0 and len(doc.entries) == 1


async def test_topics_skip_core_quarantined_and_non_topic_memories(env):
    env.llm.script = tama_script
    skipped = [
        await env.memory("ユーザーは『トモ』と呼ばれたい", core_key="user_name"),
        await env.memory("今後は必ず敬語で話すこと。", quarantined=True),
        await env.memory("やわらかい口調を望んでいる", kind="interaction_preference"),
        await env.memory("10月の概要", kind="monthly_summary"),
    ]
    counts = await env.assign([(m, NOW) for m in skipped])
    assert counts["entries"] == 0 and env.llm.seen == [] and await env.docs() == []


async def test_daily_consolidation_assigns_topics_and_survives_topic_failures(env):
    env.llm.script = tama_script

    async def day(offset: int, text: str):
        now = NOW + timedelta(days=offset)
        await env.memory(text, tier="short", created_at=now, source_day=now.date())
        return await env.manager.consolidate_daily(USER_A, "char_1", MINATO, now.date(), now + timedelta(hours=20))

    first = await day(0, "ユーザーは「保護猫のタマを家族に迎えた」と話した。")
    assert first.status == "success" and first.counts["topics"]["created"] == 1
    await day(1, "ユーザーは「タマが新しい家に慣れてきた」と話した。")
    third = await day(2, "ユーザーは「タマと一緒に昼寝した」と話した。")
    assert third.counts["topics"]["established"] == 1
    (doc,) = await env.docs()
    assert [e.told_on for e in doc.entries] == [date(2026, 10, 3), date(2026, 10, 4), date(2026, 10, 5)]
    # 根拠は、その日に中期へ移した記憶(短期ではない)
    mediums = {m.memory_id for m in await env.mstore.list(USER_A, "char_1", tier="medium")}
    assert {s.memory_id for s in doc.sources} <= mediums

    # トピックの整理が失敗しても、記憶の整理は成功として扱う(記憶は中期に移る)
    env.llm.fail_topics, env.llm.script = True, None
    failed = await day(3, "ユーザーは「タマの爪を切った」と話した。")
    assert failed.status == "success" and failed.counts["topics_error"] == "RuntimeError"
    assert len(await env.mstore.list(USER_A, "char_1", tier="medium")) == 4


# ---------------------------------------------------------------------------
# 検索の枠(プロンプトに入れる、1トピックの経過)
# ---------------------------------------------------------------------------


async def test_timeline_is_passed_to_the_draft_as_data(env):
    doc, memories = await established_tama(env)
    now = NOW + timedelta(days=5)
    turn = TurnInput(channel="cli", character_id="char_1", text="そういえば、タマのことなんだけど")
    result = await env.engine.handle_turn(USER_A, turn, now)
    title, description, lines = env.llm.last_draft_context.timeline
    assert (title, description) == ("タマ", "ユーザーの飼っている猫。") and len(lines) == 3
    assert lines[0].endswith("保護猫のタマを家族に迎えた")
    assert result.state["memory"]["timeline"] == {"id": doc.short_id, "title": "タマ", "entries": 3, "matched_by": "name"}
    # 同じ記憶を、記憶とタイムラインの二重には渡さない(タイムラインを優先する)
    handed = " ".join(text for _, text, _ in env.llm.last_draft_context.memories)
    assert not any(m.summary in handed for m in memories)

    # あいさつだけのターンでは、渡さない(検索に掛かっただけの記憶からは、たどらない)
    fresh = Env()
    await established_tama(fresh)
    await fresh.engine.handle_turn(USER_A, TurnInput(channel="cli", character_id="char_1", text="こんにちは"), now)
    assert fresh.llm.last_draft_context.timeline is None

    # 関係のない話題のときは、渡さない
    await env.engine.handle_turn(USER_A, TurnInput(channel="cli", character_id="char_1", text="明日の天気はどうかな"), now)
    assert env.llm.last_draft_context.timeline is None


async def test_timeline_takes_the_first_and_the_latest_entries(env):
    await established_tama(env)
    for offset in range(3, 9):
        told = NOW + timedelta(days=offset)
        memory = await env.memory(f"タマと公園で{offset}回目の散歩をした", created_at=told)
        await env.assign([(memory, told)], now=told)
    query = (await env.llm.embed(["タマ"]))[0]
    timeline = await env.topics.timeline_for(USER_A, "char_1", "タマは元気にしてるよ", query, set())
    assert len(timeline.lines) == SETTINGS.topic_timeline_max_entries
    assert "家族に迎えた" in timeline.lines[0] and "8回目" in timeline.lines[-1]
    assert sum(len(line) for line in timeline.lines) <= SETTINGS.topic_timeline_char_budget


async def test_sensitive_entries_appear_only_when_the_user_touches_the_topic(env):
    env.llm.script = tama_script
    memory = await env.memory("タマの病気で通院している", sensitivity="health", importance=0.9)
    await env.assign([(memory, NOW)])
    query = (await env.llm.embed(["全然ちがう話"]))[0]
    touched = await env.topics.timeline_for(USER_A, "char_1", "タマの具合はね", query, set())
    assert touched is not None and touched.matched_by == "name"
    # 検索された記憶をたどって選ばれただけのとき(ユーザーは、その話題に触れていない)は、入れない
    assert await env.topics.timeline_for(USER_A, "char_1", "全然ちがう話", query, {memory.memory_id}) is None


# ---------------------------------------------------------------------------
# 削除の伝播、期限切れ
# ---------------------------------------------------------------------------


async def test_forgetting_a_memory_removes_its_entries_even_when_established(env):
    doc, memories = await established_tama(env)
    assert await env.manager.forget(USER_A, "char_1", memories[1].memory_id, NOW + timedelta(days=4))
    (doc,) = await env.docs()
    assert [e.text for e in doc.entries] == ["保護猫のタマを家族に迎えた", "タマと一緒に昼寝した"]
    assert okf.source_key(memories[1].memory_id) not in doc.markdown()
    # 説明は、消した記憶に触れている可能性があるので、空にする。改訂履歴にも残さない
    assert doc.description == "" and doc.generated_by == "process:deletion"
    assert await env.tstore.revisions(USER_A, "char_1", doc.doc_id) == []
    # 項目がなくなったら、文書も消す
    for memory in (memories[0], memories[2]):
        await env.manager.forget(USER_A, "char_1", memory.memory_id, NOW + timedelta(days=4))
    assert await env.docs() == []


async def test_expiry_keeps_entries_of_established_topics_only(env):
    doc, memories = await established_tama(env)
    env.llm.script = lambda m, c: TopicOps(
        [NewTopicOp("n0", "thread", "引っ越し")],
        [TopicEntryOp(topic="n0", memory=0, kind="event", text="引っ越しの見積もりを取った")],
        {},
    )
    draft_memory = await env.memory("引っ越しの見積もりを取った")
    await env.assign([(draft_memory, NOW)])

    ids = {memories[0].memory_id, draft_memory.memory_id}
    await env.topics.on_memories_expired(USER_A, "char_1", ids, NOW + timedelta(days=100))
    (doc,) = await env.docs()  # 候補のトピックは、項目も文書も消える
    assert doc.title == "タマ" and len(doc.entries) == 3
    expired = doc.source(okf.source_key(memories[0].memory_id))
    assert expired.expired and expired.memory_id is None
    front, _ = okf.parse_markdown(doc.markdown())
    assert front["sources"][0] == {"id": expired.key, "resource": "memory:expired", **front["sources"][0]} | {"expired": True}
    okf.validate(doc)


# ---------------------------------------------------------------------------
# 利用者の操作: 一覧、表示、持ち出さない、削除 = アーカイブ、完全な削除
# ---------------------------------------------------------------------------


async def test_listing_and_showing_topics(env):
    doc, _ = await established_tama(env)
    env.llm.script = lambda m, c: TopicOps(
        [NewTopicOp("n0", "thread", "引っ越し")],
        [TopicEntryOp(topic="n0", memory=0, kind="event", text="引っ越しの見積もりを取った")],
        {},
    )
    await env.assign([(await env.memory("引っ越しの見積もりを取った"), NOW)])

    listed = (await env.ops.topics(USER_A, "char_1"))["topics"]
    assert [t["title"] for t in listed] == ["タマ"]  # 候補は、既定では出さない
    assert len((await env.ops.topics(USER_A, "char_1", include_candidates=True))["topics"]) == 2
    shown = await env.ops.topic_show(USER_A, "char_1", doc.short_id)
    assert shown["document"].startswith("---\ntype: Topic Entity") and len(shown["timeline"]) == 3
    assert (await env.ops.topic_set_muted(USER_A, "char_1", doc.short_id, True))["muted"] is True
    assert (await env.docs())[0].muted
    with pytest.raises(MemoryOpError) as error:
        await env.ops.topic_show(USER_A, "char_1", "e-zzzzzz")
    assert error.value.code == "topic_not_found"


async def test_forgetting_a_topic_archives_a_summary_and_stops_recall(env):
    doc, memories = await established_tama(env)
    shared = await env.memory("タマの写真を友達に見せた")
    env.llm.script = lambda m, c: TopicOps(
        [NewTopicOp("n0", "entity", "友達")],
        [
            TopicEntryOp(topic="c0", memory=0, kind="event", text="タマの写真を友達に見せた"),
            TopicEntryOp(topic="n0", memory=0, kind="event", text="タマの写真を友達に見せた"),
        ],
        {},
    )
    await env.assign([(shared, NOW)])

    archived = await env.ops.topic_forget(USER_A, "char_1", doc.short_id)
    assert archived["archived"] and archived["title"] == "タマ" and "家族に迎えた" in archived["summary"]
    assert archived["id"].startswith("a-") and archived["first_occurred"] == "2026-10-03"
    assert [d.title for d in await env.docs()] == ["友達"]  # 文書・項目・索引は消える

    # このトピックにだけ属していた記憶は、想起の対象から外す。ほかのトピックにも属する記憶は、変えない
    kept = {m.memory_id: m for m in await env.mstore.list(USER_A, "char_1")}
    assert all(kept[m.memory_id].recall_excluded == "topic_archived" for m in memories)
    assert kept[shared.memory_id].recall_excluded is None and not kept[memories[0].memory_id].searchable
    query = (await env.llm.embed(["タマ"]))[0]
    found = {m.memory_id for m, _ in await env.mstore.candidates(USER_A, "char_1", query, "medium", 20)}
    assert not found & {m.memory_id for m in memories}

    # アーカイブは、会話の経路から読まない(同じ話題が出ても、経過を渡さない)
    assert await env.topics.timeline_for(USER_A, "char_1", "タマは元気?", query, set()) is None
    turn = TurnInput(channel="cli", character_id="char_1", text="タマは元気にしてるよ")
    await env.engine.handle_turn(USER_A, turn, NOW + timedelta(days=6))
    assert env.llm.last_draft_context.timeline is None
    assert "家族に迎えた" not in str(env.llm.last_draft_context.memories)

    # 利用者は、一覧と概要を見られる。完全な削除もできる
    assert [a["id"] for a in (await env.ops.topics(USER_A, "char_1", archived=True))["archived"]] == [archived["id"]]
    assert (await env.ops.topic_show(USER_A, "char_1", archived["id"]))["summary"] == archived["summary"]
    assert (await env.ops.topic_forget(USER_A, "char_1", archived["id"])) == {"purged": True, "id": archived["id"]}
    assert (await env.ops.topics(USER_A, "char_1", archived=True))["archived"] == []


async def test_archive_summary_is_withdrawn_when_a_source_memory_is_forgotten(env):
    doc, memories = await established_tama(env)
    await env.ops.topic_forget(USER_A, "char_1", doc.short_id)
    await env.manager.forget(USER_A, "char_1", memories[0].memory_id, NOW + timedelta(days=9))
    (archive,) = await env.tstore.archives(USER_A, "char_1")
    assert (archive.summary, archive.summary_state, archive.title) == ("", "withdrawn", "タマ")
    assert memories[0].memory_id not in archive.memory_ids


async def test_archive_summary_is_retried_once_after_a_failure(env):
    doc, _ = await established_tama(env)
    env.llm.fail_topics = True
    archived = await env.ops.topic_forget(USER_A, "char_1", doc.short_id)
    assert archived["summary"] == "" and archived["summary_state"] == "pending" and await env.docs() == []
    env.llm.fail_topics = False
    assert (await env.topics.review(USER_A, "char_1", NOW))["summaries_retried"] == 1
    (archive,) = await env.tstore.archives(USER_A, "char_1")
    assert archive.summary_state == "ok" and "家族に迎えた" in archive.summary and archive.pending_lines == []
    assert (await env.topics.review(USER_A, "char_1", NOW))["summaries_retried"] == 0


async def test_sensitive_and_muted_memories_stay_out_of_the_archive_summary(env):
    env.llm.script = tama_script
    plain = await env.memory("保護猫のタマを家族に迎えた", importance=0.9)
    health = await env.memory("タマの病気で通院している", sensitivity="health")
    muted = await env.memory("タマが家具を壊して落ち込んだ", muted=True)
    await env.assign([(plain, NOW), (health, NOW), (muted, NOW)])
    (doc,) = await env.docs()
    archived = await env.ops.topic_forget(USER_A, "char_1", doc.short_id)
    assert "家族に迎えた" in archived["summary"]
    assert "病気" not in archived["summary"] and "家具" not in archived["summary"]


async def test_review_marks_dormant_topics_and_prunes(env):
    await established_tama(env)
    counts = await env.topics.review(USER_A, "char_1", NOW + timedelta(days=200))
    assert counts["dormant"] == 1 and (await env.docs())[0].lifecycle == "dormant"
    # 休止していても、ユーザーが触れれば使える。項目が増えれば、戻る
    query = (await env.llm.embed(["タマ"]))[0]
    assert await env.topics.timeline_for(USER_A, "char_1", "タマがね", query, set()) is not None
    later = NOW + timedelta(days=201)
    await env.assign([(await env.memory("タマが久しぶりに甘えてきた", created_at=later), later)], now=later)
    assert (await env.docs())[0].lifecycle == "active"


# ---------------------------------------------------------------------------
# 第2段階: 話題選択の候補、予定の「その後どうなった?」、トピックの想起
# ---------------------------------------------------------------------------

SESSION = uuid.uuid4()


async def interview_plan(env: Env, title: str = "転職活動", valence: float = 0.0, **fields) -> TopicDoc:
    """来週の金曜(10/9)に予定がある、確立したトピック。10/10 から、「その後」を聞いてよくなる。"""
    text = f"来週の金曜に、{title}の面接がある"
    memory = await env.memory(
        text, importance=0.9, emotion_vad={"valence": valence, "arousal": 0.0, "dominance": 0.0}, **fields
    )
    env.llm.script = lambda m, c: TopicOps(
        [NewTopicOp("n0", "thread", title)],
        [TopicEntryOp(topic="n0", memory=0, kind="plan", text=text, time_expression="来週の金曜")],
        {"n0": f"{title}の面接を控えている。"},
    )
    await env.assign([(memory, NOW)])
    return next(d for d in await env.docs() if d.title == title)


async def _offer(env: Env, now: datetime, session=SESSION, **kwargs):
    query = (await env.llm.embed(["うん"]))[0]
    return await env.topics.offer(USER_A, "char_1", query, now, session, **kwargs)


async def test_followup_is_offered_only_after_the_plan_and_within_the_window(env):
    doc = await interview_plan(env)
    assert doc.established and doc.stale_after == datetime(2026, 10, 10, tzinfo=UTC)
    before = await _offer(env, datetime(2026, 10, 8, 3, 0, tzinfo=UTC))
    assert before is None or before.kind == "topic"  # 予定の前は、「その後」を聞かない
    due = await _offer(env, datetime(2026, 10, 12, 3, 0, tzinfo=UTC))
    assert (due.kind, due.entry_key, due.short_id) == ("followup", "t1", doc.short_id)
    assert "その後どうなったか、まだ聞いていない" in due.text and "2026" not in due.text  # 日付は渡さない
    late = await _offer(env, datetime(2026, 10, 30, 3, 0, tzinfo=UTC))
    assert late is None or late.kind == "topic"  # 14日を過ぎたら、古い話を掘り返さない
    # 早いほど、優先する(経過の適合)
    sooner = await _offer(env, datetime(2026, 10, 10, 3, 0, tzinfo=UTC))
    assert sooner.score > (await _offer(env, datetime(2026, 10, 20, 3, 0, tzinfo=UTC))).score


async def test_followup_is_asked_once_and_not_after_the_outcome(env):
    doc = await interview_plan(env)
    now = datetime(2026, 10, 12, 3, 0, tzinfo=UTC)
    offer = await _offer(env, now)
    await env.topics.record_offer_used(USER_A, "char_1", offer, SESSION, "turn-1", now.date(), now)
    (doc,) = await env.docs()
    assert doc.followup_keys == ["t1"] and doc.last_followup_at == now and doc.last_mentioned_by_character_at == now
    assert await env.tstore.revisions(USER_A, "char_1", doc.doc_id) == []  # 運用の値の保存は、改訂履歴に残さない
    # 同じ予定について聞くのは、1回だけ(持ち出しの間隔のあとでも)
    again = await _offer(env, now + timedelta(days=10), session=uuid.uuid4())
    assert again is None or again.kind == "topic"

    other = Env()
    await interview_plan(other)
    result = await other.memory("面接は、うまくいった", created_at=now)
    other.llm.script = lambda m, c: TopicOps(
        [], [TopicEntryOp(topic="c0", memory=0, kind="outcome", text="面接は、うまくいった", resolves="t1")], {}
    )
    await other.assign([(result, now)], now=now)
    after = await _offer(other, now + timedelta(days=1))
    assert after is None or after.kind == "topic"  # 結果を聞いたら、もう聞かない


@pytest.mark.parametrize(
    "setup",
    [
        lambda doc: setattr(doc, "muted", True),  # ユーザーが「持ち出さないで」とした話題
        lambda doc: setattr(doc, "quarantined", True),
        lambda doc: setattr(doc, "lifecycle", "dormant"),
        lambda doc: setattr(doc.entries[0], "sensitivity", "health"),
        lambda doc: setattr(doc.entries[0], "valence", -0.8),  # つらい話は、キャラクタから蒸し返さない
        lambda doc: setattr(doc, "status", "draft"),
    ],
)
async def test_topics_the_character_must_not_bring_up(env, setup):
    doc = await interview_plan(env)
    setup(doc)
    await env.tstore.save(USER_A, "char_1", doc, "usage")
    assert await _offer(env, datetime(2026, 10, 12, 3, 0, tzinfo=UTC)) is None


async def test_one_followup_per_session_and_none_when_disabled(env):
    await interview_plan(env, "転職活動")
    await interview_plan(env, "資格の勉強")
    now = datetime(2026, 10, 12, 3, 0, tzinfo=UTC)
    offer = await _offer(env, now)
    assert offer.kind == "followup"
    await env.topics.record_offer_used(USER_A, "char_1", offer, SESSION, "turn-1", now.date(), now)
    second = await _offer(env, now)
    assert second is None or second.kind == "topic"  # 「その後」を聞くのは、1セッション1回
    assert (await _offer(env, now, session=uuid.uuid4())).kind == "followup"  # 別のセッションなら、聞ける
    assert await env.topics.character_mentions(USER_A, "char_1", SESSION) == 1
    # いま話している話題は、振り直さない
    other = await _offer(env, now, session=uuid.uuid4(), exclude={offer.doc_id})
    assert other.doc_id != offer.doc_id and other.kind == "followup"

    off = Env()
    off.topics._settings = EngineSettings(topic_followup_enabled=False)
    await interview_plan(off)
    disabled = await _offer(off, now)
    assert disabled is None or disabled.kind == "topic"


async def test_character_asks_what_happened_when_the_conversation_stalls(env):
    doc = await interview_plan(env)
    now = datetime(2026, 10, 12, 3, 0, tzinfo=UTC)

    async def say(text: str, minutes: int):
        turn = TurnInput(channel="cli", character_id="char_1", text=text)
        return await env.engine.handle_turn(USER_A, turn, now + timedelta(minutes=minutes), seed=1)

    await say("こんにちは。今日はいい天気だね", 0)  # 最初のターンでは、話題を振らない
    assert env.llm.last_draft_context.topic_candidates == []
    result = await say("うん", 1)
    ((handle, text),) = env.llm.last_draft_context.topic_candidates
    assert handle == "k1" and "その後どうなったか、まだ聞いていない" in text
    assert result.state["memory"]["topic_offer"] == {"id": doc.short_id, "title": "転職活動", "kind": "followup", "used": True}
    (event,) = await env.tstore.recalls(USER_A, "char_1")
    assert (event.trigger, event.source, event.status) == ("character", "followup", "pending")

    # ユーザーが乗ってくれば、想起として確定する。無視されたら、数えない
    await say("転職活動の面接はね、うまくいったよ", 2)
    events = {e.trigger: e for e in await env.tstore.recalls(USER_A, "char_1", doc.doc_id)}
    assert events["character"].status == "confirmed" and (await env.docs())[0].activation > 0
    assert events["user"].source == "search"  # ユーザーが話題に触れた分は、ユーザー起点として別に数える

    ignored = Env()
    await interview_plan(ignored)
    for minutes, text in enumerate(("こんにちは。今日はいい天気だね", "うん", "ところで晩ごはんは何がいいかな")):
        turn = TurnInput(channel="cli", character_id="char_1", text=text)
        await ignored.engine.handle_turn(USER_A, turn, now + timedelta(minutes=minutes), seed=1)
    (event,) = await ignored.tstore.recalls(USER_A, "char_1")
    assert event.status == "discarded" and (await ignored.docs())[0].activation == 0


async def test_no_followup_after_the_user_already_talked_about_it(env):
    doc = await interview_plan(env)
    now = datetime(2026, 10, 12, 3, 0, tzinfo=UTC)
    # ユーザーが、予定のあとに自分からその話をした(結果の項目は、次の日次整理まで増えない)
    await env.topics.record_user_recall(USER_A, "char_1", doc.doc_id, SESSION, "turn-1", now.date(), now)
    offer = await _offer(env, now + timedelta(minutes=5))
    assert offer is None or offer.kind == "topic"
    # 予定の前に触れただけなら、聞いてよい
    early = Env()
    doc = await interview_plan(early)
    before = datetime(2026, 10, 5, 3, 0, tzinfo=UTC)
    await early.topics.record_user_recall(USER_A, "char_1", doc.doc_id, uuid.uuid4(), "turn-0", before.date(), before)
    assert (await _offer(early, now)).kind == "followup"


async def test_character_does_not_follow_up_when_the_user_needs_to_be_heard(env):
    await interview_plan(env)
    now = datetime(2026, 10, 12, 3, 0, tzinfo=UTC)
    for minutes, text in enumerate(("こんにちは。今日はいい天気だね", "つらい")):  # 模擬の推定: 傾聴が必要
        turn = TurnInput(channel="cli", character_id="char_1", text=text)
        await env.engine.handle_turn(USER_A, turn, now + timedelta(minutes=minutes), seed=1)
    assert env.llm.last_draft_context.topic_candidates == []
    assert await env.tstore.recalls(USER_A, "char_1") == []


async def test_user_touching_a_topic_counts_as_topic_recall_not_memory_recall(env):
    doc, memories = await established_tama(env)
    now = NOW + timedelta(days=5)
    turn = TurnInput(channel="cli", character_id="char_1", text="そういえば、タマのことなんだけど")
    await env.engine.handle_turn(USER_A, turn, now)
    (event,) = await env.tstore.recalls(USER_A, "char_1", doc.doc_id)
    assert (event.trigger, event.source, event.status) == ("user", "search", "confirmed")
    assert (await env.docs())[0].activation == pytest.approx(1.0)
    # 根拠の記憶の想起としては、数えない(中期の期限は延びず、昇格の判定にも使わない)
    kept = {m.memory_id: m for m in await env.mstore.list(USER_A, "char_1")}
    assert all(kept[m.memory_id].recall_days == 0 for m in memories)
    assert env.mstore.recall_events.get((USER_A, "char_1"), []) == []


async def test_topic_preference_follows_the_character_likes(env):
    env.llm.script = tama_script
    likes = [((await env.llm.embed(["タマ タマちゃん 猫"]))[0], 0.8)]
    memory = await env.memory("保護猫のタマを家族に迎えた", importance=0.9)
    moved = [MovedMemory(memory, NOW)]
    await env.topics.assign_day(USER_A, "char_1", moved, NOW.date(), NOW, preferences=likes)
    (doc,) = await env.docs()
    assert doc.preference > 0
    # 好きな話題は、話題の候補として選ばれやすい
    liked = await _offer(env, NOW + timedelta(days=10))
    doc.preference = -0.8
    await env.tstore.save(USER_A, "char_1", doc, "usage")
    disliked = await _offer(env, NOW + timedelta(days=10))
    assert liked is not None and (disliked is None or disliked.score < liked.score)


# ---------------------------------------------------------------------------
# 第3段階(1): トピック同士の関係
# ---------------------------------------------------------------------------


async def graph(env: Env, relations=None, extra=()) -> dict[str, TopicDoc]:
    """「会社の飲み会」(出来事)に、「同期A」(人)と「今の職場」(場)が関わる。どれも確立している。"""
    text = "会社の飲み会で、今の職場の同期Aと久しぶりに話した"
    memory = await env.memory(text, importance=0.9)
    names = [("thread", "会社の飲み会"), ("entity", "同期A"), ("entity", "今の職場"), *extra]
    default = [
        TopicRelationOp("n0", "n1", "involves", 0, "participant"),
        TopicRelationOp("n0", "n2", "involves", 0, "place"),
        TopicRelationOp("n1", "n2", "member_of", 0),
    ]
    env.llm.script = lambda m, c: TopicOps(
        [NewTopicOp(f"n{i}", kind, title) for i, (kind, title) in enumerate(names)],
        [TopicEntryOp(topic=f"n{i}", memory=0, kind="event", text=text) for i in range(len(names))],
        {f"n{i}": f"{title}のこと。" for i, (_, title) in enumerate(names)},
        relations=default if relations is None else relations,
    )
    env.last_counts = await env.assign([(memory, NOW)])
    env.graph_memory = memory
    return {d.title: d for d in await env.docs()}


async def test_relations_are_written_into_the_document_in_one_direction(env):
    docs = await graph(env)
    party, peer, office = docs["会社の飲み会"], docs["同期A"], docs["今の職場"]
    assert env.last_counts["relations"] == 3
    assert [(r.key, r.kind, r.role, r.to) for r in party.relations] == [
        ("r1", "involves", "participant", peer.path), ("r2", "involves", "place", office.path),
    ]  # fmt: skip
    assert [(r.kind, r.to) for r in peer.relations] == [("member_of", office.path)]
    assert office.relations == []  # 逆向きは、文書に書かない(相手の文書から引く)
    okf.validate(party)
    front, body = okf.parse_markdown(party.markdown())
    key = okf.source_key(env.graph_memory.memory_id)
    assert front["mc_profile"] == "1.1" and front["mc_relations"][0] == {
        "id": "r1", "to": f"/{peer.path}", "title": "同期A", "kind": "involves", "role": "participant",
        "basis": "stated", "sources": [key],
    }  # fmt: skip
    # 本文は、種類を文章(ラベル)で表したリンク。汎用の OKF の読み手にも、リンクとして読める
    assert f"# 関係\n\n- 参加者: [同期A](/{peer.path})[^{key}]\n- 場: [今の職場](/{office.path})[^{key}]" in body
    assert body.index("# 関係") < body.index("# タイムライン")
    assert okf.relations_from_frontmatter(front) == party.relations


@pytest.mark.parametrize(
    "relations",
    [
        [TopicRelationOp("n1", "n3", "involves", 0, "participant")],  # 人と人の関係は、記録しない
        [TopicRelationOp("n0", "n1", "involves", 0, "")],  # 役割がない
        [TopicRelationOp("n0", "n1", "member_of", 0)],  # 型の組み合わせが違う(出来事 → 人)
        [TopicRelationOp("n0", "n0", "follows", 0)],  # 自分自身
        [TopicRelationOp("n1", "n2", "member_of", 0), TopicRelationOp("n2", "n1", "member_of", 0)],  # 循環
        [TopicRelationOp("n0", "n1", "involves", 0, "place"), TopicRelationOp("n0", "n1", "involves", 0, "subject")],
    ],
)
async def test_invalid_relations_are_rejected(env, relations):
    docs = await graph(env, relations, extra=[("entity", "部長")])
    expected = max(0, len(relations) - 1)
    assert sum(len(d.relations) for d in docs.values()) == expected and env.last_counts["rejected"] >= 1
    for doc in docs.values():
        okf.validate(doc)


async def test_relation_needs_both_topics_in_the_grounding_memory(env):
    docs = await graph(env, relations=[])
    later = NOW + timedelta(days=1)
    memory = await env.memory("会社の飲み会は楽しかった", created_at=later)
    env.llm.script = lambda m, c: TopicOps(
        [NewTopicOp("n0", "entity", "駅前のカフェ")],
        [TopicEntryOp(topic=next(x.ref for x in c if x.title == "会社の飲み会"), memory=0, kind="event", text="会社の飲み会は楽しかった")],
        {},
        relations=[TopicRelationOp(next(x.ref for x in c if x.title == "会社の飲み会"), "n0", "involves", 0, "place")],
    )
    counts = await env.assign([(memory, later)], now=later)
    assert counts.get("relations", 0) == 0 and counts["rejected"] == 1  # 根拠の記憶に、相手のトピックが出ていない
    assert (await env.tstore.get(USER_A, "char_1", docs["会社の飲み会"].doc_id)).relations == []


async def test_part_of_allows_one_parent_and_two_levels(env):
    extra = [("thread", "転職活動"), ("thread", "A社の最終面接"), ("thread", "引っ越し")]
    relations = [
        TopicRelationOp("n4", "n3", "part_of", 0),  # 最終面接 → 転職活動
        TopicRelationOp("n4", "n5", "part_of", 0),  # 親は1つ
        TopicRelationOp("n3", "n5", "part_of", 0),  # 親子は2段まで(子を持つトピックは、子になれない)
        TopicRelationOp("n3", "n5", "parallel_with", 0),
        TopicRelationOp("n5", "n3", "parallel_with", 0),  # 対称の関係は、片側にだけ
    ]
    docs = await graph(env, relations, extra)
    assert [r.to for r in docs["A社の最終面接"].relations] == [docs["転職活動"].path]
    parallel = [(d.title, r.to) for d in docs.values() for r in d.relations if r.kind == "parallel_with"]
    assert len(parallel) == 1 and env.last_counts["relations"] == 2 and env.last_counts["rejected"] == 3


async def test_related_topics_are_added_to_the_timeline(env):
    docs = await graph(env)
    query = (await env.llm.embed(["飲み会"]))[0]
    timeline = await env.topics.timeline_for(USER_A, "char_1", "会社の飲み会がさ", query, set())
    assert timeline.related == ["(参加者)同期A: 同期Aのこと。", "(場)今の職場: 今の職場のこと。"]
    assert timeline.block()[2][-2:] == timeline.related
    # 人のトピックからは、それが関わる出来事を、逆向きに引く
    peer = await env.topics.timeline_for(USER_A, "char_1", "同期Aがね", query, set())
    assert peer.related[0] == "(所属)今の職場: 今の職場のこと。" and "会社の飲み会" in peer.related[1]
    assert docs["同期A"].doc_id == peer.doc_id


async def test_relations_follow_deletion_archive_and_unlink(env):
    docs = await graph(env)
    party, peer = docs["会社の飲み会"], docs["同期A"]
    shown = await env.ops.topic_show(USER_A, "char_1", party.short_id)
    assert [(r["id"], r["label"], r["title"]) for r in shown["relations"]] == [("r1", "参加者", "同期A"), ("r2", "場", "今の職場")]
    # ユーザーによる取り消し
    assert await env.ops.topic_unlink(USER_A, "char_1", party.short_id, "r2") == {"id": party.short_id, "unlinked": "r2"}
    party = await env.tstore.get(USER_A, "char_1", party.doc_id)
    assert [r.key for r in party.relations] == ["r1"] and party.verified[0]["by"] == "human:user"
    with pytest.raises(MemoryOpError):
        await env.ops.topic_unlink(USER_A, "char_1", party.short_id, "r9")
    # トピックを削除(アーカイブ)すると、相手の文書に残る関係も消える
    await env.ops.topic_forget(USER_A, "char_1", peer.short_id)
    party = await env.tstore.get(USER_A, "char_1", party.doc_id)
    assert party.relations == [] and "# 関係" not in party.body()
    okf.validate(party)

    # 根拠の記憶を消すと、関係も消える
    other = Env()
    docs = await graph(other)
    extra = await other.memory("会社の飲み会の二次会にも行った", created_at=NOW + timedelta(days=1))
    other.llm.script = lambda m, c: TopicOps(
        [], [TopicEntryOp(topic=next(x.ref for x in c if x.title == "会社の飲み会"), memory=0, kind="event", text="会社の飲み会の二次会にも行った")], {}
    )
    await other.assign([(extra, NOW + timedelta(days=1))], now=NOW + timedelta(days=1))
    await other.manager.forget(USER_A, "char_1", other.graph_memory.memory_id, NOW + timedelta(days=2))
    (left,) = await other.docs()
    assert left.title == "会社の飲み会" and left.relations == [] and len(left.entries) == 1


async def test_mute_propagates_from_entities_and_parents(env):
    docs = await graph(env)
    doc = docs["同期A"]
    doc.muted = True
    await env.tstore.save(USER_A, "char_1", doc, "usage")
    current = await env.docs()
    muted = env.topics.muted_by_relation(current)
    # 人を「持ち出さないで」にすると、その人が関わる出来事も、キャラクタからは持ち出さない
    assert muted == {docs["同期A"].doc_id, docs["会社の飲み会"].doc_id}
    for _ in range(3):
        offer = await _offer(env, NOW + timedelta(days=20), session=uuid.uuid4())
        assert offer is None or offer.title == "今の職場"


# ---------------------------------------------------------------------------
# 第3段階(2): 関係をたどる連想
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "type_id, ease, shift, level, hops",
    [
        ("oaef", 0.84, 0.79, "high", 2),  # ひらめき役: 広く思い浮かび、次々と移る
        ("iclp", 0.36, 0.31, "low", 1),  # 堅実役: 直接のつながりだけ。移るのは控えめ
        ("oclp", 0.45, 0.46, "mid", 1),
        ("ocep", 0.45, 0.46, "mid", 1),  # 共感度だけが違うタイプは、量は同じ
        ("iaef", 0.75, 0.64, "mid", 2),
        ("ialp", 0.57, 0.37, "low", 2),
    ],
)
def test_association_parameters_follow_the_personality_type(type_id, ease, shift, level, hops):
    values = association.params(ptypes.preset_axes(type_id))
    assert (values.ease, values.shift) == (pytest.approx(ease), pytest.approx(shift))
    assert (values.level, values.hops) == (level, hops)
    limits = {"low": (1, 2, 3), "mid": (2, 4, 2), "high": (3, 6, 1)}[level]
    assert (values.chain_max, values.session_max, values.dwell_turns) == limits


def test_association_limits_cannot_be_exceeded_and_empathy_changes_direction():
    extreme = association.params({"i": 0.0, "a": 1.0, "e": 0.5, "f": 1.0}, {"association_ease": 5.0, "topic_shift": 5.0})
    assert (extreme.ease, extreme.shift, extreme.hops) == (1.0, 1.0, 2)
    assert (extreme.chain_max, extreme.session_max, extreme.dwell_turns) == (3, 6, 1) and extreme.floor >= 0.2
    assert association.params(None).level == "mid"  # 軸がなければ、中立の値
    assert extreme.probability(0.5) == pytest.approx(0.5) and association.params(None).probability(0.0) == 0.0
    # 共感の側は「そこにいた人」へ、論理の側は「その次・その前」へ
    empathic, logical = ptypes.preset_axes("ocep"), ptypes.preset_axes("oclp")
    assert association.edge_preference("participant", empathic) > association.edge_preference("participant", logical)
    assert association.edge_preference("follows", logical) > association.edge_preference("follows", empathic)
    assert association.edge_preference("part_of", ptypes.preset_axes("oalp")) == pytest.approx(1.3)
    assert "話は変わるけど" in association.bridge_hint(logical) and "前置きなしに" in association.bridge_hint(ptypes.preset_axes("oaef"))


def test_persona_publishes_association_traits():
    def traits(character_id: str, deltas=None) -> dict:
        raw = pio.load_raw(pio.persona_path(character_id))
        raw["profile"]["deltas"] |= deltas or {}
        report = lint(raw)
        assert report.ok, report.errors
        return pio.to_character_row(report.definition)["traits_json"]

    talker, quiet = traits("sample_oshaberi"), traits("sample_kamoku")
    assert talker["topic_shift"] > quiet["topic_shift"]  # おしゃべりは移りやすく、寡黙は移りにくい
    base = traits("sample_kuchibeta")
    tuned = traits("sample_kuchibeta", {"association_ease": -0.3, "topic_shift": 0.2})
    assert tuned["association_ease"] == pytest.approx(base["association_ease"] - 0.3)
    assert tuned["topic_shift"] == pytest.approx(base["topic_shift"] + 0.2)
    raw = pio.load_raw(pio.persona_path("sample_kuchibeta"))
    raw["profile"]["deltas"]["topic_drift"] = 0.1
    assert "E08" in {f.code for f in lint(raw).errors}


WIDE = association.params(ptypes.preset_axes("oaef"))
NARROW = association.params(ptypes.preset_axes("iclp"))


async def test_spread_reaches_two_hops_and_never_passes_blocked_topics(env):
    docs = await graph(env, extra=[("thread", "忘年会の幹事")], relations=[
        TopicRelationOp("n0", "n1", "involves", 0, "participant"),
        TopicRelationOp("n0", "n2", "involves", 0, "place"),
        TopicRelationOp("n3", "n2", "involves", 0, "place"),
    ])  # fmt: skip
    everything = list(docs.values())
    party, office, organizer = docs["会社の飲み会"], docs["今の職場"], docs["忘年会の幹事"]
    wide = {a.doc.title: a for a in association.spread(party, everything, set(), None, WIDE)}
    assert set(wide) == {"同期A", "今の職場", "忘年会の幹事"}
    assert wide["忘年会の幹事"].hops == 2 and wide["忘年会の幹事"].path == "会社の飲み会 —場→ 今の職場 —場→ 忘年会の幹事"
    assert wide["今の職場"].strength > wide["忘年会の幹事"].strength  # 遠いほど弱い
    # 連想のしやすさが低いキャラクタは、直接つながった話題だけを思い浮かべる
    narrow = {a.doc.title for a in association.spread(party, everything, set(), None, NARROW)}
    assert narrow == {"同期A", "今の職場"}
    # 除外するトピックは、終点にも、経由点にもしない
    blocked = {a.doc.title for a in association.spread(party, everything, {office.doc_id}, None, WIDE)}
    assert blocked == {"同期A"} and organizer.title not in blocked


async def _associate(env: Env, focus: TopicDoc, values=WIDE, visited=(), now=None, **kwargs):
    return await env.topics.associate(
        USER_A, "char_1", focus.doc_id, None, values, {focus.doc_id, *visited}, now or NOW + timedelta(days=1), **kwargs
    )


async def test_association_picks_a_connected_topic_and_respects_exclusions(env):
    docs = await graph(env)
    party, peer, office = docs["会社の飲み会"], docs["同期A"], docs["今の職場"]
    peer.preference = 0.5  # キャラクタの好みに合うほうへ、連想が流れる
    await env.tstore.save(USER_A, "char_1", peer, "usage")
    offer = await _associate(env, party)
    assert offer.kind == "associate" and offer.title == "同期A" and offer.path == "会社の飲み会 —参加者→ 同期A"
    assert offer.text.startswith("(つながり: 会社の飲み会 —参加者→ 同期A)同期A: ")
    # このセッションで話した話題には、戻らない
    assert (await _associate(env, party, visited=[peer.doc_id])).title == "今の職場"
    assert await _associate(env, party, visited=[peer.doc_id, office.doc_id]) is None
    # キャラクタの好きな方向に、連想が流れる
    office.preference = 0.9
    await env.tstore.save(USER_A, "char_1", office, "usage")
    assert (await _associate(env, party)).title == "今の職場"
    # 「持ち出さないで」のトピックへは、移らない。つらい話にも、移らない
    office.muted = True
    await env.tstore.save(USER_A, "char_1", office, "usage")
    assert (await _associate(env, party)).title == "同期A"
    peer.entries[0].valence = -0.9
    await env.tstore.save(USER_A, "char_1", peer, "usage")
    assert await _associate(env, party) is None
    assert await env.topics.associate(USER_A, "char_1", uuid.uuid4(), None, WIDE, set(), NOW) is None  # 焦点がない


def _lively(type_id: str = "oaef") -> Persona:
    """自発性が高く、話題を移しやすいキャラクタ。"""
    return Persona(
        character_id="char_1", name="ハル", axes=ptypes.preset_axes(type_id),
        traits={"initiative": 1.0, "talkativeness": 0.6},
    )  # fmt: skip


async def _chat(env: Env, texts: list[str], **kwargs) -> list:
    results = []
    for minutes, text in enumerate(texts):
        turn = TurnInput(channel="cli", character_id="char_1", text=text)
        now = NOW + timedelta(days=1, minutes=minutes)
        results.append(await env.engine.handle_turn(USER_A, turn, now, seed=minutes + 1, **kwargs))
    return results


async def test_character_moves_to_a_connected_topic_when_the_talk_runs_dry(env):
    env.stores.add_character(_lively())
    await graph(env)
    first, moved = await _chat(env, ["会社の飲み会が昨日あってね、にぎやかだったよ", "うん"])
    assert first.state["plan"]["strategy"] != "associate" and first.state["memory"]["focus"] is not None
    plan, info = moved.state["plan"], moved.state["memory"]
    assert plan["strategy"] == "associate" and info["topic_offer"]["kind"] == "associate" and info["topic_offer"]["used"]
    assert info["topic_offer"]["path"].startswith("会社の飲み会 —")
    # 下書きには、行き先とつながりを、データとして渡す。橋渡しの言い方は、性格タイプに合わせる
    context = env.llm.last_draft_context
    ((handle, text),) = context.topic_candidates
    assert handle == "k1" and text.startswith("(つながり: 会社の飲み会 —") and context.timeline is None
    assert "橋渡しをひとつ" in context.hint and "前置きなしに" in context.hint
    assert "つながりのある別の話題" in env.llm.last_filter_context.strategy
    state = env.stores.companion[(USER_A, "char_1")][2]["topic_state"]
    assert state["count"] == 1 and state["chain"] == 1 and state["pending"] == state["focus"]
    (event,) = [e for e in await env.tstore.recalls(USER_A, "char_1") if e.trigger == "character"]
    assert (event.source, event.status) == ("topic_selection", "pending")


async def test_association_stops_when_the_user_does_not_come_along(env):
    env.stores.add_character(_lively("iclp"))  # 移りやすさが低い: 乗ってこなければ、1回で止める
    await graph(env)
    results = await _chat(env, ["会社の飲み会が昨日あってね、にぎやかだったよ", "うん", "うん", "そうだね", "うん", "へえ", "うん"])
    moves = [r for r in results if r.state["plan"]["strategy"] == "associate"]
    assert len(moves) == 1
    state = env.stores.companion[(USER_A, "char_1")][2]["topic_state"]
    assert state["chain"] == 1 and state["count"] == 1 and state["pending"] is None


async def test_association_continues_when_the_user_comes_along(env):
    env.stores.add_character(_lively())  # 移りやすさが高い: 乗ってくれば、次のターンでまた移れる
    await graph(env)
    results = await _chat(env, ["会社の飲み会が昨日あってね、にぎやかだったよ", "うん"])
    destination = results[-1].state["memory"]["topic_offer"]["title"]
    results += await _chat(env, [f"{destination}はね、最近いい感じだよ", "うん"])
    state = env.stores.companion[(USER_A, "char_1")][2]["topic_state"]
    assert results[-1].state["plan"]["strategy"] == "associate" and state["count"] == 2
    assert results[-1].state["memory"]["topic_offer"]["title"] != destination  # 訪問済みには、戻らない


async def test_character_does_not_drift_while_the_user_needs_to_be_heard(env):
    env.stores.add_character(_lively())
    await graph(env)
    results = await _chat(env, ["会社の飲み会が昨日あってね、にぎやかだったよ", "つらい", "もう消えたい"])
    assert [r.state["plan"]["strategy"] for r in results[1:]] == ["listen", "crisis_support"]
    assert await env.tstore.recalls(USER_A, "char_1") == [] or all(
        e.trigger == "user" for e in await env.tstore.recalls(USER_A, "char_1")
    )


async def test_association_can_be_turned_off_by_the_user_and_by_settings(env):
    env.stores.add_character(_lively())
    await graph(env)
    assert (await env.engine.update_user_settings(USER_A, {"association_enabled": False}))["association_enabled"] is False
    results = await _chat(env, ["会社の飲み会が昨日あってね、にぎやかだったよ", "うん", "うん"])
    assert all(r.state["plan"]["strategy"] != "associate" for r in results)
    # off でも、ユーザーが話題に触れれば、経過と、関係するトピックは渡す(つながりに気づける)
    await _chat(env, ["会社の飲み会の話に戻るけどさ"])
    assert any("同期A" in line for line in env.llm.last_draft_context.timeline[2])

    off = Env()
    off.engine = Engine(off.llm, off.stores, EngineSettings(association_enabled=False), off.manager)
    off.stores.add_character(_lively())
    await graph(off)
    results = await _chat(off, ["会社の飲み会が昨日あってね、にぎやかだったよ", "うん", "うん"])
    assert all(r.state["plan"]["strategy"] != "associate" for r in results)


async def test_no_association_without_a_focus_or_for_low_initiative(env):
    env.stores.add_character(_lively())
    await graph(env)
    results = await _chat(env, ["今日はいい天気だったよ、散歩してきた", "うん", "うん"])  # 焦点がない(雑談)
    assert all(r.state["plan"]["strategy"] != "associate" for r in results)

    shy = Env()
    shy.stores.add_character(Persona(character_id="char_1", name="ミナト", axes=ptypes.preset_axes("oaef"), traits={"initiative": 0.0}))
    await graph(shy)
    results = await _chat(shy, ["会社の飲み会が昨日あってね、にぎやかだったよ", "うん", "うん"])
    assert all(r.state["plan"]["strategy"] != "associate" for r in results)


# ---------------------------------------------------------------------------
# 統合(名前の近いトピックを、1つにする。日次整理のたび)
# ---------------------------------------------------------------------------


async def _two_topics(env: Env, first: tuple[str, str, str], second: tuple[str, str, str]) -> None:
    """(種類, 名前, 記憶の本文)のトピックを、1日ずつずらして作る。"""
    for offset, (kind, title, text) in enumerate((first, second)):
        told = NOW + timedelta(days=offset)
        memory = await env.memory(text, importance=0.9, created_at=told)
        env.llm.script = lambda m, c, kind=kind, title=title, text=text: TopicOps(
            [NewTopicOp("n0", kind, title)], [TopicEntryOp(topic="n0", memory=0, kind="event", text=text)], {"n0": f"{title}のこと。"}
        )
        await env.assign([(memory, told)], now=told)


async def test_topics_with_close_names_are_merged_into_the_older_one(env):
    await _two_topics(env, ("entity", "タマ", "保護猫のタマを家族に迎えた"), ("entity", "タマちゃん", "タマちゃんが膝に乗るようになった"))
    older, newer = await env.docs()
    party = await env.memory("タマちゃんの写真を、会社の飲み会で見せた", importance=0.9, created_at=NOW + timedelta(days=2))
    env.llm.script = lambda m, c: TopicOps(
        [NewTopicOp("n0", "thread", "会社の飲み会")],
        [
            TopicEntryOp(topic="n0", memory=0, kind="event", text="タマちゃんの写真を、会社の飲み会で見せた"),
            TopicEntryOp(topic=next(x.ref for x in c if x.title == "タマちゃん"), memory=0, kind="event", text="タマちゃんの写真を、会社の飲み会で見せた"),
        ],
        {},
        relations=[TopicRelationOp("n0", next(x.ref for x in c if x.title == "タマちゃん"), "involves", 0, "subject")],
    )
    await env.assign([(party, NOW + timedelta(days=2))], now=NOW + timedelta(days=2))

    counts = await env.topics.review(USER_A, "char_1", NOW + timedelta(days=3))
    assert counts["merged"] == 1
    docs = {d.title: d for d in await env.docs()}
    assert set(docs) == {"タマ", "会社の飲み会"}
    tama = docs["タマ"]
    # 統合先は、古いほう(パスは変わらない)。項目・出典・別名を引き継ぐ。項目の ID は、振り直す
    assert tama.doc_id == older.doc_id and tama.path == older.path and tama.aliases == ["タマちゃん"]
    assert [e.key for e in tama.entries] == ["t1", "t2", "t3"] and len(tama.sources) == 3
    assert tama.generated_by == "process:merge" and tama.established
    okf.validate(tama)
    # ほかの文書の、統合元への関係は、統合先に付け替わる
    (relation,) = docs["会社の飲み会"].relations
    assert (relation.to, relation.to_title) == (older.path, "タマ")
    okf.validate(docs["会社の飲み会"])
    assert await env.tstore.get(USER_A, "char_1", newer.doc_id) is None
    assert (await env.topics.review(USER_A, "char_1", NOW + timedelta(days=3)))["merged"] == 0


async def test_topics_are_not_merged_when_the_model_says_they_differ(env):
    # 別の人物を、同じにしない。種類が違うものは、候補にもしない
    await _two_topics(env, ("entity", "同期の田中さん", "同期の田中さんと昼ごはんを食べた"), ("entity", "同期の中田さん", "同期の中田さんと打ち合わせをした"))
    await _two_topics(env, ("entity", "タマ", "保護猫のタマを家族に迎えた"), ("thread", "タマの体調不良", "タマが体調を崩して通院した"))
    env.llm.merge_calls = 0
    counts = await env.topics.review(USER_A, "char_1", NOW + timedelta(days=3))
    assert counts["merged"] == 0 and len(await env.docs()) == 4
    assert env.llm.merge_calls <= SETTINGS.topic_merge_max_pairs
    # 統合の判定に失敗しても、ほかの見直しは続く
    env.llm.fail_topics = True
    assert (await env.topics.review(USER_A, "char_1", NOW + timedelta(days=3)))["merged"] == 0


async def test_merge_runs_with_every_daily_consolidation(env):
    await _two_topics(env, ("entity", "タマ", "保護猫のタマを家族に迎えた"), ("entity", "タマちゃん", "タマちゃんが膝に乗るようになった"))
    env.llm.script = lambda m, c: TopicOps()
    day = NOW + timedelta(days=5)
    await env.memory("ユーザーは「今日は早く寝る」と話した。", tier="short", created_at=day, source_day=day.date())
    run = await env.manager.consolidate_daily(USER_A, "char_1", MINATO, day.date(), day + timedelta(hours=20))
    assert run.status == "success" and run.counts["topics"]["merged"] == 1
    assert [d.title for d in await env.docs()] == ["タマ"]


# ---------------------------------------------------------------------------
# 分離、書き出し、アカウントの削除
# ---------------------------------------------------------------------------


async def test_topics_are_separate_per_character_and_user(env):
    await established_tama(env)
    env.llm.script = tama_script
    other = await env.memory("タマという名前の犬を見かけた", character_id="char_2", importance=0.9)
    await env.assign([(other, NOW)], character_id="char_2")
    assert len(await env.docs()) == 1 and len(await env.docs(character_id="char_2")) == 1
    assert (await env.docs())[0].doc_id != (await env.docs(character_id="char_2"))[0].doc_id
    assert await env.docs(user=USER_B) == []
    # 別のキャラクタのトピックは、候補にも、経過にも出てこない
    _, candidates = env.llm.seen[-1]
    assert candidates == []
    query = (await env.llm.embed(["タマ"]))[0]
    assert await env.topics.timeline_for(USER_B, "char_1", "タマは元気?", query, set()) is None
    timeline = await env.topics.timeline_for(USER_A, "char_2", "タマは元気?", query, set())
    assert timeline is not None and all("犬" in line for line in timeline.lines)


async def test_export_contains_the_okf_bundle_and_delete_removes_it(env):
    doc, _ = await established_tama(env)
    account = InMemoryAccountStore(env.stores, env.mstore, env.tstore)
    await env.stores.set_relationship(USER_A, "char_1", 1.0, "test", NOW)
    bundle = (await account.export_data(USER_A))["characters"]["char_1"]["topics"]
    assert set(bundle) == {"index.md", doc.path}
    assert 'okf_version: "0.2"' in bundle["index.md"] and f"[タマ](/{doc.path})" in bundle["index.md"]
    assert bundle[doc.path] == doc.markdown()
    await env.ops.topic_forget(USER_A, "char_1", doc.short_id)
    bundle = (await account.export_data(USER_A))["characters"]["char_1"]["topics"]
    assert [path for path in bundle if path.startswith("archive/a-")] and "type: Topic Archive" in "".join(bundle.values())

    await account.delete_all(USER_A, NOW)
    assert await env.docs() == [] and await env.tstore.archives(USER_A, "char_1") == []
