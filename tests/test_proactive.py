"""無入力時の自発的な発話のテスト(本体仕様書 §8.6、§15.1 の M8)。

頻度(静穏時間帯・間隔・1日の上限・追撃の上限)、抑制(危機・気分・関係値)、話題の選び方
(予定の「その後」、負の懸念の「その後」、類似による連想)、禁止表現の検査、記録と返事のつながりを確かめる。
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from mycompanion.channel.types import TurnInput
from mycompanion.config import EngineSettings
from mycompanion.engine.engine import Engine
from mycompanion.engine.stores import InMemoryStores, UserContext
from mycompanion.jobs.runner import JobRunner, plan_due
from mycompanion.jobs.types import Job
from mycompanion.llm.client import NewTopicOp, Persona, TopicEntryOp, TopicOps
from mycompanion.llm.mock import MockLLMClient
from mycompanion.memory.models import Memory
from mycompanion.memory.service import MemoryManager
from mycompanion.memory.store import InMemoryMemoryStore
from mycompanion.memory.topic_store import InMemoryTopicStore
from mycompanion.memory.topics import MovedMemory
from mycompanion.proactive import policy
from mycompanion.proactive.policy import ProactiveFacts
from mycompanion.safety import screening

SETTINGS = EngineSettings()
USER = uuid.uuid4()
# 日本時間の 10/12(月)正午。静穏時間帯(21:00〜9:00)の外
NOON = datetime(2026, 10, 12, 3, 0, tzinfo=UTC)
TOLD = datetime(2026, 10, 3, 3, 0, tzinfo=UTC)
HARU = Persona(character_id="haru", name="ハル", traits={"initiative": 0.87})  # 自発性が高い
SHIZUKU = Persona(character_id="shizuku", name="シズク", traits={"initiative": 0.13})  # 自発性が低い


def facts(**changes) -> ProactiveFacts:
    values = {
        "user_enabled": True, "consented": True, "throttled": False, "timezone": "Asia/Tokyo", "initiative": 0.87,
        "relationship": 0.0, "last_activity_at": NOON - timedelta(hours=40), "unanswered": 0, "sent_today": 0,
        "last_crisis_at": None,
    }  # fmt: skip
    values.update(changes)
    return ProactiveFacts(**values)


# ---------------------------------------------------------------------------
# 判定(規則だけ)
# ---------------------------------------------------------------------------


def test_speaks_when_every_condition_is_met():
    decision = policy.decide(facts(), NOON, SETTINGS)
    assert decision.ok and decision.general_topics


@pytest.mark.parametrize(
    "changes, reason",
    [
        ({"user_enabled": False}, "not_enabled"),  # 既定オフ。ユーザーが許可したときだけ
        ({"consented": False}, "no_consent"),
        ({"throttled": True}, "throttled"),
        ({"last_activity_at": None}, "never_talked"),
        ({"session_active": True}, "in_session"),
        ({"unanswered": 1}, "unanswered"),  # 追撃しない(ユーザーが話すまで止める)
        ({"sent_today": 3}, "daily_limit"),  # キャラクタごと。自発性 0.87 のキャラクタは1日3回まで
        ({"relationship": -0.5}, "negative_relationship"),
        ({"last_crisis_at": NOON - timedelta(hours=71)}, "recent_crisis"),
        ({"last_anger": 0.6}, "negative_mood"),
        ({"last_valence": -0.6}, "negative_mood"),
        ({"last_activity_at": NOON - timedelta(hours=10)}, "too_soon"),
    ],
)
def test_stops_with_a_reason(changes, reason):
    decision = policy.decide(facts(**changes), NOON, SETTINGS)
    assert (decision.ok, decision.reason) == (False, reason)


def test_crisis_hold_ends_and_global_switch_stops_everything():
    assert policy.decide(facts(last_crisis_at=NOON - timedelta(hours=73)), NOON, SETTINGS).ok
    off = EngineSettings(proactive_enabled=False)
    assert policy.decide(facts(), NOON, off).reason == "disabled_globally"


@pytest.mark.parametrize(
    "hour_utc, quiet",
    [(11, False), (12, True), (23, True), (0, False), (3, False)],  # 日本時間の 20時・21時・8時・9時・12時
)
def test_quiet_hours_cross_midnight_in_local_time(hour_utc, quiet):
    now = datetime(2026, 10, 12, hour_utc, 0, tzinfo=UTC)
    assert policy.in_quiet_hours(now, "Asia/Tokyo", "21:00", "09:00") is quiet
    if quiet:
        assert policy.decide(facts(last_activity_at=now - timedelta(days=3)), now, SETTINGS).reason == "quiet_hours"


def test_interval_depends_on_initiative_and_relationship():
    # 自発性が高いほど短い。関係値が高いほど短い(+20 で 2割)。負の関係値は短くしない。基準は12時間(2026-11-21)
    assert policy.interval_hours(0.87, 0.0, SETTINGS) == pytest.approx(13.56)
    assert policy.interval_hours(0.13, 0.0, SETTINGS) == pytest.approx(22.44)
    assert policy.interval_hours(0.87, 20.0, SETTINGS) == pytest.approx(13.56 * 0.8)
    assert policy.interval_hours(0.87, 10.0, SETTINGS) == pytest.approx(13.56 * 0.9)
    shy = facts(initiative=0.13, last_activity_at=NOON - timedelta(hours=20))
    assert policy.decide(shy, NOON, SETTINGS).reason == "too_soon"
    assert policy.decide(facts(initiative=0.13, last_activity_at=NOON - timedelta(hours=23)), NOON, SETTINGS).ok


def test_second_and_later_use_the_shorter_interval_of_the_same_day():
    # その日すでに話しかけたあとは、短い間隔(3時間 × (2 − initiative))。関係値の補正はしない
    assert policy.interval_hours(0.87, 20.0, SETTINGS, repeat=True) == pytest.approx(3.39)
    assert policy.interval_hours(0.67, 0.0, SETTINGS, repeat=True) == pytest.approx(3.99)
    second = facts(sent_today=1, last_activity_at=NOON - timedelta(hours=3.5))
    assert policy.decide(second, NOON, SETTINGS).ok
    assert policy.decide(facts(sent_today=1, last_activity_at=NOON - timedelta(hours=3)), NOON, SETTINGS).reason == "too_soon"
    # その日の1回目は、長い間隔のまま
    assert policy.decide(facts(sent_today=0, last_activity_at=NOON - timedelta(hours=3.5)), NOON, SETTINGS).reason == "too_soon"


@pytest.mark.parametrize(
    "initiative, limit", [(0.87, 3), (0.67, 2), (0.23, 1), (0.13, 1), (0.0, 1), (1.0, 3)]  # ハル・ココ・ミナト・シズク
)
def test_daily_limit_is_per_character_and_follows_initiative(initiative, limit):
    assert policy.daily_limit(initiative, SETTINGS) == limit
    assert policy.decide(facts(initiative=initiative, sent_today=limit - 1), NOON, SETTINGS).ok
    assert policy.decide(facts(initiative=initiative, sent_today=limit), NOON, SETTINGS).reason == "daily_limit"


def test_low_initiative_characters_only_follow_up():
    decision = policy.decide(facts(initiative=0.13, last_activity_at=NOON - timedelta(hours=50)), NOON, SETTINGS)
    assert decision.ok and not decision.general_topics


# ---------------------------------------------------------------------------
# 禁止表現(罪悪感・催促・引き留め)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "最近話してくれなくて、寂しかったよ。",
        "ずっと待ってたのに。",
        "どうして連絡くれないの?",
        "早く返事してね。",
        "返事ちょうだい!",
        "もしかして、忘れちゃったの?",
        "もう話してくれないの?",
        "かまってほしいな。",
    ],
)
def test_pressure_is_blocked_in_proactive_speech(text):
    findings = screening.screen_output(text, canary_text="MC-x", proactive=True)
    assert "proactive_pressure" in [f.category for f in findings]
    # ふつうの応答には、この規則をかけない(相手の「寂しかった」への共感などを止めない)
    normal = [f.category for f in screening.screen_output(text, canary_text="MC-x")]
    assert "proactive_pressure" not in normal


@pytest.mark.parametrize(
    "text", ["そういえば、面接どうだった?", "ハルだよ。タマは元気にしてる?", "返事はいつでも大丈夫だよ。"]
)
def test_light_proactive_speech_passes(text):
    assert screening.screen_output(text, canary_text="MC-x", proactive=True) == []


# ---------------------------------------------------------------------------
# エンジン: 話題の選択、生成、記録、返事のつながり
# ---------------------------------------------------------------------------


class RecordingLLM(MockLLMClient):
    def __init__(self) -> None:
        super().__init__()
        self.histories: list = []
        self.script = None
        self.texts: list[str] = []  # 順に返す下書き(なければ既定)

    async def draft(self, persona, history, context=None, on_delta=None):
        self.histories.append(list(history))
        if self.texts:
            self.draft_text = self.texts.pop(0)
        return await super().draft(persona, history, context, on_delta)

    async def assign_topics(self, memories, candidates):
        if self.script is None:
            return await super().assign_topics(memories, candidates)
        return self.script(memories, candidates)


class Env:
    def __init__(self, settings: EngineSettings = SETTINGS) -> None:
        self.llm = RecordingLLM()
        self.stores = InMemoryStores()
        for persona in (HARU, SHIZUKU):
            self.stores.add_character(persona)
        self.mstore = InMemoryMemoryStore()
        self.tstore = InMemoryTopicStore()
        self.manager = MemoryManager(self.mstore, self.llm, settings, log=self.stores, topic_store=self.tstore)
        self.engine = Engine(self.llm, self.stores, settings, self.manager)

    def talked(self, character_id: str, at: datetime) -> None:
        """以前に会話した(セッションは終わっている)。"""
        self.stores.last_activity[(USER, character_id)] = (uuid.uuid4(), at)

    def allow(self, **fields) -> None:
        self.stores.user_contexts[USER] = UserContext(proactive_enabled=True, is_under_18=False, **fields)

    async def memory(
        self, summary: str, character_id: str, told: datetime, valence: float = 0.0, importance: float = 0.9, **fields
    ) -> Memory:
        memory = Memory(
            memory_id=uuid.uuid4(), summary=summary, embedding=(await self.llm.embed([summary]))[0], tier="medium",
            kind="episodic", importance=importance, created_at=told, source_day=told.date(),
            emotion_vad={"valence": valence, "arousal": 0.0, "dominance": 0.0}, **fields,
        )  # fmt: skip
        return await self.mstore.add(USER, character_id, memory)

    async def topic(
        self, character_id: str, title: str, items: list[tuple[str, datetime, str, str, float]], importance: float = 0.9
    ):
        """トピックを作る。items: (文, 話した時刻, 種類, 時期の表現, valence)。"""
        for text, told, kind, expression, valence in items:
            memory = await self.memory(text, character_id, told, valence, importance)
            existing = [d for d in await self.tstore.list(USER, character_id) if d.title == title]
            ref = "c0" if existing else "n0"
            new = [] if existing else [NewTopicOp("n0", "thread", title)]
            self.llm.script = lambda m, c, ref=ref, new=new, text=text, kind=kind, expression=expression: TopicOps(
                new, [TopicEntryOp(topic=ref, memory=0, kind=kind, text=text, time_expression=expression)],
                {ref: f"{title}のこと。"},
            )  # fmt: skip
            await self.manager.topics.assign_day(USER, character_id, [MovedMemory(memory, told)], told.date(), told)
        return next(d for d in await self.tstore.list(USER, character_id) if d.title == title)

    async def speak(self, character_id: str = "haru", now: datetime = NOON, consented: bool = True):
        return await self.engine.proactive_turn(USER, character_id, consented=consented, now=now, seed=7)


async def interview(env: Env, character_id: str = "haru"):
    """10/9 に面接の予定がある、確立したトピック(10/12 には「その後」を聞いてよい)。"""
    return await env.topic(
        character_id, "転職活動", [("来週の金曜に、転職の面接がある", TOLD, "plan", "来週の金曜", 0.0)]
    )


async def test_does_nothing_unless_the_user_allowed_it():
    env = Env()
    env.talked("haru", NOON - timedelta(days=3))
    await interview(env)
    result = await env.speak()
    assert (result.sent, result.reason) == (False, "not_enabled")
    assert env.llm.histories == [] and env.stores.turns.get((USER, "haru"), []) == []


async def test_follow_up_is_sent_recorded_and_continued_by_the_reply():
    env = Env()
    env.allow()
    env.talked("haru", NOON - timedelta(days=3))
    doc = await interview(env)
    env.llm.draft_text = "そういえば、面接どうだった?"
    result = await env.speak()
    assert (result.sent, result.reason, result.topic_kind, result.turn_no) == (True, "ok", "followup", 1)
    # 話題は1つだけ渡し、自分から話しかける方針を入れる。相手の発話の代わりの入力は、記録しない
    context = env.llm.last_draft_context
    assert len(context.topic_candidates) == 1 and "あなたから話しかけます" in context.directive
    (turn,) = env.stores.turns[(USER, "haru")]
    assert (turn.response_mode, turn.user_input, turn.reply_text) == ("proactive", "", "そういえば、面接どうだった?")
    assert turn.reply_prompt_version.endswith("+proactive-v1") and turn.session_id == result.session_id
    # 同じ予定について聞くのは1回だけ(持ち出しの記録)
    (doc,) = await env.tstore.list(USER, "haru")
    assert doc.followup_keys == ["t1"] and doc.last_mentioned_by_character_at == NOON

    # 返事を待つあいだは、話しかけない(会話中・追撃の上限)
    assert (await env.speak(now=NOON + timedelta(days=2))).reason == "in_session"
    env.stores.expire_session(USER, "haru")
    assert (await env.speak(now=NOON + timedelta(days=2))).reason == "unanswered"

    # ユーザーの返事は、同じ会話の続き(自発発話が、生成の履歴に入る)
    env.stores.sessions[(USER, "haru")] = result.session_id
    env.llm.draft_text = None
    reply = await env.engine.handle_turn(USER, TurnInput(channel="cli", character_id="haru", text="うまくいったよ!"), now=NOON + timedelta(hours=1))
    assert reply.session_id == result.session_id and reply.turn_no == 2
    assert ("assistant", "そういえば、面接どうだった?") in env.llm.histories[-1]
    assert env.stores.proactive[(USER, "haru")]["unanswered"] == 0


async def test_daily_limit_is_counted_per_character():
    env = Env()
    env.allow()
    for character_id in ("haru", "shizuku"):
        env.talked(character_id, NOON - timedelta(days=3))
        await interview(env, character_id)
    # ほかのキャラクタが話しかけても、それぞれの上限で数える
    assert (await env.speak("haru")).sent
    first = await env.speak("shizuku")
    assert first.sent
    # シズク(自発性 0.13)は1日1回: 返事のあと、間隔が過ぎても、同じローカル日にはもう話しかけない
    await env.engine.handle_turn(
        USER, TurnInput(channel="cli", character_id="shizuku", text="うまくいったよ"), now=NOON + timedelta(minutes=5)
    )
    env.stores.expire_session(USER, "shizuku")
    env.talked("shizuku", NOON - timedelta(days=1))  # 間隔は満たしている
    assert (await env.speak("shizuku", now=NOON + timedelta(hours=1))).reason == "daily_limit"
    # ローカル日が変わると、また数え直す
    assert env.stores.proactive[(USER, "shizuku")]["sent"] == {"2026-10-12": 1}


async def test_same_day_repeats_follow_the_conversation_up_to_the_limit():
    env = Env()
    env.allow()
    env.talked("haru", NOON - timedelta(days=1))
    for text in ("駅前のカフェ", "週末のキャンプ", "新しい自転車", "庭のトマト"):
        # 会話で思い出した記憶は「新しさ」が下がるので、嗜好度を高くしておく
        await env.memory(f"ユーザーは{text}の話をよくする", "haru", TOLD, preference=1.0)

    async def reply(at: datetime) -> None:
        # 自発発話への返事でも、ユーザーから始めた会話でも、同じ(会話のあと、短い間隔で数える)
        # 短い相づちだと、会話の中でもハルが話題を振る(記憶を使う)ので、ふつうの長さの返事にする
        text = "今日は仕事が忙しかったけど、なんとか終わったよ"
        await env.engine.handle_turn(USER, TurnInput(channel="cli", character_id="haru", text=text), now=at)
        env.stores.expire_session(USER, "haru")

    day = datetime(2026, 10, 12, 0, 0, tzinfo=UTC)  # 日本時間の 9:00
    assert (await env.speak(now=day)).sent  # 1回目(前日の会話から長い間隔が過ぎている)
    await reply(day + timedelta(minutes=30))
    assert (await env.speak(now=day + timedelta(hours=3))).reason == "too_soon"  # 返事から2.5時間
    assert (await env.speak(now=day + timedelta(hours=4))).sent  # 2回目(返事から3.5時間)
    await reply(day + timedelta(hours=4, minutes=30))
    assert (await env.speak(now=day + timedelta(hours=8))).sent  # 3回目
    await reply(day + timedelta(hours=8, minutes=30))
    assert (await env.speak(now=day + timedelta(hours=11, minutes=30))).reason == "daily_limit"  # ハルは1日3回まで


async def test_no_topic_means_no_speech():
    env = Env()
    env.allow()
    env.talked("haru", NOON - timedelta(days=3))
    result = await env.speak()
    assert (result.sent, result.reason) == (False, "no_topic")  # 中身のない声かけはしない
    assert env.llm.histories == []


async def test_low_initiative_character_speaks_only_to_follow_up():
    env = Env()
    env.allow()
    env.talked("shizuku", NOON - timedelta(days=3))
    await env.memory("ユーザーは週末にキャンプへ行くのが好き", "shizuku", TOLD)
    assert (await env.speak("shizuku")).reason == "no_topic"  # 一般の話題は振らない
    await interview(env, "shizuku")
    result = await env.speak("shizuku")
    assert (result.sent, result.topic_kind) == (True, "followup")


async def test_high_initiative_character_brings_up_a_memory():
    env = Env()
    env.allow()
    env.talked("haru", NOON - timedelta(days=3))
    # いまの会話がないので、関連度は使えない。ユーザーが好んで話す記憶(嗜好度)が選ばれる
    await env.memory("ユーザーは週末にキャンプへ行くのが好き", "haru", TOLD, preference=0.6)
    result = await env.speak()
    assert (result.sent, result.topic_kind) == (True, "memory")
    (turn,) = env.stores.turns[(USER, "haru")]
    assert len(turn.used_memory_ids) == 1


async def test_pressure_is_regenerated_once_then_not_sent():
    env = Env()
    env.allow()
    env.talked("haru", NOON - timedelta(days=3))
    await interview(env)
    env.llm.texts = ["最近話してくれなくて寂しかったよ。面接どうだった?", "そういえば、面接どうだった?"]
    assert (await env.speak()).sent  # 1回だけ作り直す

    other = Env()
    other.allow()
    other.talked("haru", NOON - timedelta(days=3))
    await interview(other)
    other.llm.draft_text = "ずっと待ってたのに、どうして連絡くれないの?"
    result = await other.speak()
    assert (result.sent, result.reason) == (False, "blocked")  # 定型の文面に置き換えず、話しかけない
    assert other.stores.turns.get((USER, "haru"), []) == []
    assert [e["category"] for e in other.stores.security_events].count("proactive_pressure") == 2


async def test_negative_concern_is_asked_once_after_two_weeks():
    env = Env()
    env.allow()
    first, second = TOLD, TOLD + timedelta(days=1)
    await env.topic(
        "haru", "母の入院",
        [("母が入院して心配", first, "event", "", -0.7), ("母の検査の結果が気になって眠れない", second, "event", "", -0.8)],
    )  # fmt: skip
    env.talked("haru", second)
    early = second + timedelta(days=10)
    assert (await env.speak(now=early)).reason == "no_topic"  # 14日より前は聞かない
    due = second + timedelta(days=15)
    result = await env.speak(now=due)
    assert (result.sent, result.topic_kind) == (True, "concern")
    assert "そっと気にかける" in env.llm.last_draft_context.topic_candidates[0][1]
    (doc,) = await env.tstore.list(USER, "haru")
    assert doc.followup_keys == ["concern:t2"]
    # 同じ懸念について聞くのは1回だけ
    env.stores.proactive[(USER, "haru")]["unanswered"] = 0
    env.stores.expire_session(USER, "haru")
    env.talked("haru", due - timedelta(days=3))
    assert (await env.speak(now=due + timedelta(days=1))).reason in ("no_topic", "daily_limit")
    assert (await env.speak(now=due + timedelta(days=2))).reason == "no_topic"


async def lower_importance(env: Env) -> None:
    for memory in await env.mstore.list(USER, "haru"):
        await env.mstore.update(USER, "haru", memory.memory_id, importance=0.1)


async def test_similar_topic_is_offered_without_a_stated_relation():
    env = Env()
    env.allow()
    await env.topic("haru", "商店街のパン屋さん", [("商店街のパン屋さんのクロワッサンがおいしい", TOLD, "event", "", 0.4)])
    recent = TOLD + timedelta(days=5)
    await env.topic("haru", "駅前のパン屋さん", [("駅前のパン屋さんのクロワッサンを買った", recent, "event", "", 0.4)])
    env.talked("haru", recent)
    await lower_importance(env)  # 元の記憶そのものではなく、似たトピックへの連想を確かめる
    result = await env.speak(now=recent + timedelta(days=2))
    assert (result.sent, result.topic_kind) == (True, "similar")
    # 最近話したトピック(駅前)から、似ているトピック(商店街)へ移る
    assert env.llm.last_draft_context.topic_candidates[0][1].startswith("(似た話: 駅前のパン屋さん → 商店街のパン屋さん)")

    off = Env(EngineSettings(proactive_similar_enabled=False))
    off.allow()
    await off.topic("haru", "商店街のパン屋さん", [("商店街のパン屋さんのクロワッサンがおいしい", TOLD, "event", "", 0.4)])
    await off.topic("haru", "駅前のパン屋さん", [("駅前のパン屋さんのクロワッサンを買った", recent, "event", "", 0.4)])
    off.talked("haru", recent)
    await lower_importance(off)
    assert (await off.speak(now=recent + timedelta(days=2))).reason == "no_topic"


async def test_ordinary_topic_comes_before_a_negative_concern():
    env = Env()
    env.allow()
    second = TOLD + timedelta(days=1)
    await env.topic(
        "haru", "母の入院",
        [("母が入院して心配", TOLD, "event", "", -0.7), ("母の検査の結果が気になって眠れない", second, "event", "", -0.8)],
    )  # fmt: skip
    await env.memory("ユーザーは週末にキャンプへ行くのが好き", "haru", TOLD, preference=0.6)
    env.talked("haru", second)
    result = await env.speak(now=second + timedelta(days=15))
    assert (result.sent, result.topic_kind) == (True, "memory")  # つらい話は、ほかに話題がないときだけ
    (doc,) = await env.tstore.list(USER, "haru")
    assert doc.followup_keys == []


async def test_follow_up_comes_before_an_ordinary_topic():
    env = Env()
    env.allow()
    env.talked("haru", NOON - timedelta(days=3))
    await env.memory("ユーザーは週末にキャンプへ行くのが好き", "haru", TOLD, preference=0.9)
    await interview(env)
    assert (await env.speak()).topic_kind == "followup"


async def test_concern_told_only_once_is_not_asked():
    env = Env()
    env.allow()
    await env.topic("haru", "母の入院", [("母が入院して心配", TOLD, "event", "", -0.7)])
    env.talked("haru", TOLD)
    assert (await env.speak(now=TOLD + timedelta(days=15))).reason == "no_topic"


async def test_proactive_only_session_is_not_written_to_memory():
    env = Env()
    env.allow()
    env.talked("haru", NOON - timedelta(days=3))
    await interview(env)
    result = await env.speak()
    written = await env.manager.write_session(USER, "haru", HARU, result.session_id, NOON + timedelta(days=3))
    assert written == 0 and env.llm.memory_write_calls == 0


# ---------------------------------------------------------------------------
# API: 設定、まだ見ていない自発発話の取得と既読(手順2)
# ---------------------------------------------------------------------------


def test_api_settings_and_unseen_messages():
    from fastapi.testclient import TestClient

    from mycompanion.api.app import AppDeps, create_app
    from mycompanion.api.auth import Principal

    class Verifier:
        async def verify(self, token: str) -> Principal:
            return Principal(idp="entra_external_id", subject=token)

    class Users:
        async def resolve_user(self, idp: str, subject: str) -> uuid.UUID:
            return USER if subject == "me" else uuid.uuid5(uuid.NAMESPACE_OID, subject)

    env = Env()
    client = TestClient(create_app(AppDeps(verifier=Verifier(), users=Users(), engine=env.engine, characters=env.stores)))
    me, other = {"Authorization": "Bearer me"}, {"Authorization": "Bearer other"}

    # 既定はオフ。設定で変えられる
    assert client.get("/v1/settings", headers=me).json()["proactive_enabled"] is False
    assert client.put("/v1/settings", json={"proactive_enabled": True}, headers=me).json()["proactive_enabled"] is True
    assert client.get("/v1/proactive", headers=me).json() == {"messages": []}

    import asyncio

    env.talked("haru", NOON - timedelta(days=3))
    asyncio.run(interview(env))
    env.llm.draft_text = "そういえば、面接どうだった?"
    result = asyncio.run(env.speak())
    (message,) = client.get("/v1/proactive", headers=me).json()["messages"]
    assert (message["character_id"], message["name"], message["text"], message["turn_no"]) == (
        "haru", "ハル", "そういえば、面接どうだった?", 1,
    )  # fmt: skip
    assert message["session_id"] == str(result.session_id)
    assert client.get("/v1/proactive", headers=other).json() == {"messages": []}  # ほかのユーザーには見えない

    # 既読にすると、もう返さない。ほかのセッションの ID では既読にならない
    wrong = client.post("/v1/proactive/haru/seen", json={"session_id": str(uuid.uuid4())}, headers=me).json()
    assert wrong == {"seen": False}
    seen = client.post("/v1/proactive/haru/seen", json={"session_id": message["session_id"]}, headers=me).json()
    assert seen == {"seen": True}
    assert client.get("/v1/proactive", headers=me).json() == {"messages": []}


# ---------------------------------------------------------------------------
# ジョブ: 計画と実行
# ---------------------------------------------------------------------------


async def test_plan_skips_quiet_hours_and_runner_counts_reasons():
    env = Env()

    async def candidates(before):
        assert before == NOON - timedelta(hours=SETTINGS.proactive_repeat_base_hours)
        return [(USER, "haru", "Asia/Tokyo"), (USER, "shizuku", "America/New_York")]  # NY は 10/11 の 23時

    jobs = await plan_due(env.manager, SETTINGS, NOON, proactive_candidates=candidates)
    assert [(j.kind, j.character_id) for j in jobs if j.kind == "proactive"] == [("proactive", "haru")]

    async def speaker(user_id, character_id, now):
        return "ok" if character_id == "haru" else "quiet_hours"

    runner = JobRunner(env.manager, env.stores.get_character, SETTINGS, proactive=speaker)
    done = await runner.run(Job("proactive", user_id=USER, character_id="haru"), NOON)
    skipped = await runner.run(Job("proactive", user_id=USER, character_id="shizuku"), NOON)
    assert (done.status, done.counts) == ("done", {"proactive_sent": 1})
    assert skipped.counts == {"proactive_quiet_hours": 1}
    assert Job.from_json(Job("proactive", user_id=USER, character_id="haru").to_json()).kind == "proactive"


async def test_important_memory_is_a_topic_even_without_character_preference():
    # 自発発話では、いまの会話との関連度の代わりに、記憶の重要度を使う(2026-11-22 のユーザーの決定)
    env = Env()
    env.allow()
    env.talked("haru", NOON - timedelta(days=3))
    await env.memory("ユーザーは来月、引っ越しを控えている", "haru", TOLD, importance=0.6, preference=0.0)
    result = await env.speak()
    assert (result.sent, result.topic_kind) == (True, "memory")

    trivial = Env()
    trivial.allow()
    trivial.talked("haru", NOON - timedelta(days=3))
    await trivial.memory("ユーザーは朝にコーヒーを飲んだ", "haru", TOLD, importance=0.2, preference=0.0)
    assert (await trivial.speak()).reason == "no_topic"  # 好みにも合わず、大事でもない話題は振らない
