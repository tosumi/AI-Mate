"""エンジンが使う保存先の抽象と、メモリ上の実装(本体仕様書 §10.1「エンジンはステートレス」)。

エンジンは状態を持たない。状態はすべて、この抽象を通して保存先に置く。
本番の実装は `mycompanion.db.stores`(PostgreSQL + Cosmos DB)。
メモリ上の実装は、自動テストと評価で使う。

**すべての操作に `user_id` と `character_id` を渡す**(§13.4.6)。
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Protocol

from mycompanion.engine.persona_request import Adjustment, PressureState
from mycompanion.llm.client import HistoryTurn, Persona
from mycompanion.safety.screening import Finding
from mycompanion.state.emotion_state import EmotionVector
from mycompanion.state.events import RuleCounter
from mycompanion.state.relationship import RelationshipChange


@dataclass(frozen=True)
class SessionInfo:
    session_id: uuid.UUID
    is_new: bool = False
    # 無入力が続いて、いま終了させたセッション(短期記憶の書き込みが、まだのもの)
    expired_session_id: uuid.UUID | None = None


@dataclass(frozen=True)
class UserContext:
    """ユーザー単位の設定と状態(キャラクタをまたぐ。§12 の `user_settings`、§4.2 の `user_safety_state`)。"""

    silence_enabled: bool = True
    filter_mode: str = "on"
    silence_render: str = "dots"
    is_under_18: bool = True  # 既定は安全側(§13.4.4)
    association_enabled: bool = True  # キャラクタが、連想で話題を移してよいか(§9.13)
    proactive_enabled: bool = False  # キャラクタから話しかけてよいか(§8.6。既定オフ)
    exempt_until: datetime | None = None  # 危機の兆候による、制限の免除と無言の禁止の期限
    throttled_until: datetime | None = None  # 一時制限の期限(§13.5.5)。解除の通知を出すまで残す


# 無言だったターンを、履歴の中で表す文面(§8.4.5 の擬似ターン)
SILENCE_PLACEHOLDER = "[無言]"


def history_entries(user_input: str, reply: str | None, mode: str) -> list[HistoryTurn]:
    """1ターンの記録を、下書きに渡す履歴に変換する。

    - 無言のターンは、「アシスタントは無言だった」という擬似ターンにする(§8.1、§8.4.5)。
    - システム通知だけのターンは、履歴に混ぜない(キャラクタの発話ではない。§8.8.7)。
    """
    if mode == "notice":
        return []
    if mode == "proactive":
        # キャラクタから話しかけたターン(ユーザーの発話はない。§8.6)
        return [("assistant", reply)] if reply else []
    entries: list[HistoryTurn] = [("user", user_input)]
    if mode == "silence":
        entries.append(("assistant", SILENCE_PLACEHOLDER))
    elif reply:
        entries.append(("assistant", reply))
    return entries


@dataclass(frozen=True)
class ProactiveSnapshot:
    """自発発話の判定に使う、保存先の事実(§15.1 の M8)。本文は含まない。"""

    relationship: float | None  # まだ関係値がなければ None(キャラクタの初期値を使う)
    last_activity_at: datetime | None  # そのキャラクタとの最後の会話(どちらかの発話)
    session_active: bool  # 進行中のセッションがある
    unanswered: int  # 返事のない自発発話の数
    sent_today: int  # ユーザーのローカル日の、このキャラクタの自発発話の回数
    last_crisis_at: datetime | None
    last_valence: float | None  # 最後のセッションの、ユーザーの気分
    last_anger: float | None
    timezone: str = "Asia/Tokyo"


@dataclass
class TurnState:
    """ターンの計算に使う、(user_id, character_id) の状態。"""

    relationship: float  # 関係値(まだ無ければ、キャラクタの initial_relationship)
    session_delta: float = 0.0  # このセッションでの、関係値の変化の合計
    last_interaction_at: datetime | None = None  # このセッションより前の、最後の会話の時刻
    timezone: str = "Asia/Tokyo"  # ユーザーのタイムゾーン(日の区切りに使う)
    user_mood: EmotionVector | None = None  # このセッションの session_mood
    companion: EmotionVector | None = None  # このセッションのキャラクタ感情(無ければ基準から始める)
    previous_rule_indexes: set[int] = field(default_factory=set)  # 直前のターンで発火した話題ルール
    turn_count: int = 0  # このセッションの、これまでのターン数(0 なら、最初のターン。R4)
    consecutive_silence: int = 0  # 直前までの、連続した無言の回数(R1)
    pressure: PressureState = field(default_factory=PressureState)  # 人格への要求の段階(§8.8.4)
    preferences: list[dict[str, Any]] = field(default_factory=list)  # ユーザーが望む接し方(§8.8.2)
    companion_mood: EmotionVector | None = None  # キャラクタの session_mood(instant の移動平均)
    rule_counters: dict[str, RuleCounter] = field(default_factory=dict)  # イベントルールの発火のカウンタ
    event_session_total: dict[str, float] = field(default_factory=dict)  # イベント項の、セッションの累計
    last_negative_act_turn: int | None = None  # このセッションで、最後に敵意を受けたターン(R9)
    # 好き嫌いシート由来のルールの、セッション内の状態(回数・累計・回避)。`state.appraisal`
    sheet_state: dict[str, Any] = field(default_factory=dict)
    # 話題の焦点・訪問済み・連想の回数(セッションの中だけ。§9.13 の第3段階)
    topic_state: dict[str, Any] = field(default_factory=dict)


@dataclass
class TurnRecord:
    """`turns` に保存する1ターン(§12)。"""

    turn_id: str
    session_id: uuid.UUID
    ts: datetime
    user_input: str
    draft_text: str | None
    filtered_text: str | None
    response_mode: str  # full / brief / backchannel / silence / notice
    filter_mode: str
    character_version: int
    debug_marker: str | None = None
    speculative_draft_used: bool = True
    used_memory_ids: list[str] = field(default_factory=list)
    latency_json: dict[str, float] = field(default_factory=dict)
    safety_flags: list[str] = field(default_factory=list)
    relationship_value: float | None = None  # そのターンで使った関係値
    sync_weight: float | None = None  # w(r) × confidence
    talkativeness_eff: float | None = None  # 関係値による変調後の実効値(§6.3.7)
    expressiveness_eff: float | None = None
    reply_text: str | None = None  # 実際に返した文面(無言・通知だけのターンは None)
    silence_reason: str | None = None  # 無言・相づちになった理由のコード
    gate_rule_hits_json: list[str] = field(default_factory=list)  # 該当したハードルール(§13.2)
    gate_score: float | None = None
    gate_seed: int | None = None  # 確率的に決めたときの乱数シード(再現用)
    plan_json: dict[str, Any] = field(default_factory=dict)  # Planner のディレクティブ
    edit_summary: str | None = None  # 感情フィルタが変更した内容
    system_notices: list[str] = field(default_factory=list)  # このターンで出したシステム通知の種類
    emotion_rule_hits_json: list[dict[str, Any]] = field(default_factory=list)  # 話題ルール・イベントルールの発火と抑止
    # 応答の作り方と、生成に使ったモデル・プロンプトの版(人間評価・A/B の集計に使う。§14、§15.1 の M6-7)
    compose_mode: str = "single"
    reply_model: str = ""
    reply_prompt_version: str = ""
    turn_no: int = 0  # 保存時に、保存先が採番する


@dataclass
class EmotionRecord:
    """`emotion_events` に保存する推定結果(§12)。本文は持たない。"""

    event_id: str
    session_id: uuid.UUID
    ts: datetime
    estimate_json: dict[str, Any]
    rule_hits: list[str]  # ルール・辞書の該当と、発火した話題ルール(追跡用)
    degraded: bool
    source_model: str
    prompt_version: str
    modality: str = "text"
    turn_no: int = 0


@dataclass
class StateUpdate:
    """1ターンの状態の更新(ターンの記録と同じ処理の中で保存する)。"""

    relationship: RelationshipChange
    user_instant: EmotionVector
    user_mood: EmotionVector
    companion: EmotionVector
    triggers: dict[str, Any]  # キャラクタ感情の各項の寄与(`last_triggers_json`)
    consecutive_silence: int = 0  # このターンのあとの、連続した無言の回数
    pressure: PressureState | None = None  # 変化があったときだけ保存する
    companion_mood: EmotionVector | None = None  # キャラクタの session_mood
    rule_counters: dict[str, RuleCounter] = field(default_factory=dict)  # 発火したルールの、更新後のカウンタ


@dataclass
class TurnCommit:
    turn: TurnRecord
    emotion: EmotionRecord
    state: StateUpdate


# 最新の状態から、保存する内容を作る関数(保存先が、直列化した処理の中で呼ぶ)
CommitBuilder = Callable[[TurnState], TurnCommit]


class EngineStores(Protocol):
    async def get_character(self, character_id: str) -> Persona | None:
        """公開済みのキャラクタ(最新の版)。なければ None。"""
        ...

    async def get_or_create_session(
        self, user_id: uuid.UUID, character_id: str, channel: str, now: datetime
    ) -> SessionInfo:
        """進行中のセッションを返す。なければ作る。セッションは1つのキャラクタとの会話(§12)。"""
        ...

    async def close_session(
        self, user_id: uuid.UUID, character_id: str, reason: str, now: datetime
    ) -> bool:
        """進行中のセッションを終了する。なければ False。"""
        ...

    async def recent_turns(
        self, user_id: uuid.UUID, character_id: str, session_id: uuid.UUID, limit: int
    ) -> list[HistoryTurn]:
        """セッション内の直近の会話(古い順)。"""
        ...

    async def current_session_id(self, user_id: uuid.UUID, character_id: str) -> uuid.UUID | None:
        """進行中のセッション。なければ None。"""
        ...

    async def session_turns(
        self, user_id: uuid.UUID, character_id: str, session_id: uuid.UUID
    ) -> list[dict[str, Any]]:
        """セッションのターン(古い順)。短期記憶の書き込みに使う(§9.4.1)。"""
        ...

    async def latest_turns(self, user_id: uuid.UUID, character_id: str, limit: int) -> list[dict[str, Any]]:
        """このキャラクタとの直近のターン(セッションをまたぐ。古い順に最大 limit 件)。画面を開いたときに表示する。"""
        ...

    async def delete_turns(self, user_id: uuid.UUID, character_id: str, turn_ids: list[str]) -> int:
        """指定のターンと、それに紐づく感情の推定を削除する(記憶の削除の伝播。§9.7)。"""
        ...

    async def emotion_history(
        self, user_id: uuid.UUID, character_id: str, since: datetime | None = None
    ) -> list[dict[str, Any]]:
        """このキャラクタとの会話での、感情の推定の記録(新しい順。FR-10)。本文は含まない。"""
        ...

    async def delete_emotion_events(self, user_id: uuid.UUID, character_id: str, event_ids: list[str]) -> int: ...

    async def load_turn_state(
        self, user_id: uuid.UUID, character_id: str, session_id: uuid.UUID, initial_relationship: float
    ) -> TurnState:
        """ターンの計算に使う状態を読む(下書きの前に使う。ロックは取らない)。"""
        ...

    async def commit_turn(
        self,
        user_id: uuid.UUID,
        character_id: str,
        session_id: uuid.UUID,
        initial_relationship: float,
        build: CommitBuilder,
    ) -> TurnCommit:
        """ターンと状態の更新を保存する。

        (user_id, character_id) 単位で直列化し、その中で最新の状態を読み直して `build` を呼ぶ
        (同時に届いたターンで、更新が失われないようにする。§11.5.4)。
        `turn_no` は、ここで採番する。
        """
        ...

    async def set_relationship(
        self, user_id: uuid.UUID, character_id: str, value: float, reason: str, now: datetime
    ) -> float:
        """関係値を直接設定する(デバッグ・調整用。§10.3 の `/relation set`)。"""
        ...

    async def mark_crisis(self, user_id: uuid.UUID, now: datetime, exempt_until: datetime) -> None:
        """危機の兆候を `user_safety_state` に記録する(§4.2)。時刻と期限だけを持つ。"""
        ...

    async def load_user_context(self, user_id: uuid.UUID) -> UserContext:
        """ユーザー単位の設定と、安全・一時制限の状態。"""
        ...

    async def update_user_settings(self, user_id: uuid.UUID, changes: dict[str, Any]) -> UserContext:
        """ユーザー設定を更新する(silence_enabled / filter_mode / silence_render / is_under_18)。"""
        ...

    async def hit_rate_limit(
        self, user_id: uuid.UUID, now: datetime, per_minute: int, per_day: int
    ) -> str | None:
        """ターンを1回数える。上限を超えたら、超えた窓("minute" / "day")を返す(§13.5.3 の L0)。"""
        ...

    async def register_serious_event(
        self, user_id: uuid.UUID, now: datetime, threshold: int, duration_minutes: int
    ) -> datetime | None:
        """重大な検出を1回数える。24時間の回数が `threshold` に達したら、一時制限をかけて期限を返す。"""
        ...

    async def clear_throttle(self, user_id: uuid.UUID) -> None:
        """一時制限を解除する(期限が過ぎたあとの通知のとき、または危機の検出のとき)。"""
        ...

    async def record_security_events(
        self,
        user_id: uuid.UUID,
        character_id: str,
        session_id: uuid.UUID | None,
        turn_id: str | None,
        findings: list[Finding],
        now: datetime,
    ) -> None:
        """スクリーニングの検出を `security_events` に記録する。本文は保存しない(§13.5.7)。"""
        ...

    async def save_pressure(
        self, user_id: uuid.UUID, character_id: str, pressure: PressureState, now: datetime
    ) -> None:
        """`persona_pressure` を保存する(ターンの記録を伴わない更新。クールダウンの解除)。"""
        ...

    async def set_interaction_preferences(
        self, user_id: uuid.UUID, character_id: str, adjustments: list[Adjustment], now: datetime
    ) -> None:
        """ユーザーが望む接し方を、`interaction_preference` の記憶として残す(§8.8.2)。同じ要素は置き換える。"""
        ...

    async def clear_interaction_preferences(self, user_id: uuid.UUID, character_id: str, now: datetime) -> None:
        """「元に戻して」: 望む接し方の記憶を消す。"""
        ...

    async def proactive_snapshot(self, user_id: uuid.UUID, character_id: str, now: datetime) -> ProactiveSnapshot:
        """自発発話の判定に使う事実(§15.1 の M8)。"""
        ...

    async def record_proactive(
        self, user_id: uuid.UUID, character_id: str, record: TurnRecord, now: datetime, wait_until: datetime
    ) -> TurnRecord:
        """自発発話を、キャラクタが始める新しいセッションの最初のターンとして保存する。

        返事のない数と、ユーザーのローカル日の回数を1つ増やす。`wait_until` まで、セッションを無入力で閉じない
        (ユーザーの返事が、同じセッションの続きになるように)。`session_id` と `turn_no` は、ここで決める。
        """
        ...

    async def unseen_proactive(self, user_id: uuid.UUID, character_id: str) -> dict[str, Any] | None:
        """まだ画面に表示していない、最後の自発発話(session_id / turn_no / text / ts)。なければ None。"""
        ...

    async def mark_proactive_seen(
        self, user_id: uuid.UUID, character_id: str, session_id: uuid.UUID, now: datetime
    ) -> bool:
        """自発発話を表示した(既読)。そのセッションの自発発話でなければ False。"""
        ...


class InMemoryStores:
    """メモリ上の実装。スコープの分離も再現する。"""

    def __init__(self) -> None:
        self.characters: dict[str, Persona] = {}
        self.sessions: dict[tuple[uuid.UUID, str], uuid.UUID] = {}
        self.closed_sessions: list[tuple[uuid.UUID, str, uuid.UUID, str]] = []
        self.last_activity: dict[tuple[uuid.UUID, str], tuple[uuid.UUID, datetime]] = {}
        self.previous_activity: dict[tuple[uuid.UUID, str], datetime] = {}
        self.turns: dict[tuple[uuid.UUID, str], list[TurnRecord]] = {}
        self.emotions: dict[tuple[uuid.UUID, str], list[EmotionRecord]] = {}
        self.relationships: dict[tuple[uuid.UUID, str], float] = {}
        self.relationship_events: dict[tuple[uuid.UUID, str], list[dict[str, Any]]] = {}
        self.user_mood: dict[tuple[uuid.UUID, str], tuple[uuid.UUID, EmotionVector]] = {}
        self.companion: dict[tuple[uuid.UUID, str], tuple[uuid.UUID, EmotionVector, dict[str, Any]]] = {}
        self.safety: dict[uuid.UUID, tuple[datetime, datetime]] = {}
        self.timezones: dict[uuid.UUID, str] = {}
        self.user_contexts: dict[uuid.UUID, UserContext] = {}
        self.expired: dict[tuple[uuid.UUID, str], uuid.UUID] = {}
        self.rate_hits: dict[uuid.UUID, list[datetime]] = {}
        self.serious_events: dict[uuid.UUID, list[datetime]] = {}
        self.security_events: list[dict[str, Any]] = []
        self.pressure: dict[tuple[uuid.UUID, str], PressureState] = {}
        self.preferences: dict[tuple[uuid.UUID, str], dict[str, dict[str, Any]]] = {}
        self.silence_counts: dict[tuple[uuid.UUID, str], tuple[uuid.UUID, int]] = {}
        self.companion_mood: dict[tuple[uuid.UUID, str], tuple[uuid.UUID, EmotionVector]] = {}
        self.rule_counters: dict[tuple[uuid.UUID, str], dict[str, RuleCounter]] = {}
        # 自発発話: (user, character) → 返事のない数・最後の送信・返事を待つ期限・ローカル日ごとの回数
        self.proactive: dict[tuple[uuid.UUID, str], dict[str, Any]] = {}

    def add_character(self, persona: Persona) -> None:
        self.characters[persona.character_id] = persona

    async def get_character(self, character_id: str) -> Persona | None:
        return self.characters.get(character_id)

    async def list_characters(self) -> list[Persona]:
        return list(self.characters.values())

    async def get_or_create_session(
        self, user_id: uuid.UUID, character_id: str, channel: str, now: datetime
    ) -> SessionInfo:
        key = (user_id, character_id)
        if key in self.sessions:
            return SessionInfo(self.sessions[key])
        self.sessions[key] = uuid.uuid4()
        # 前のセッションの最後の会話を、「このセッションより前の最後の会話」として覚えておく
        if key in self.last_activity:
            self.previous_activity[key] = self.last_activity[key][1]
        return SessionInfo(self.sessions[key], is_new=True, expired_session_id=self.expired.pop(key, None))

    def expire_session(self, user_id: uuid.UUID, character_id: str) -> None:
        """テスト用: 無入力でセッションが終了した状態にする(次のターンで、新しいセッションになる)。"""
        key = (user_id, character_id)
        if key in self.sessions:
            self.expired[key] = self.sessions.pop(key)

    async def close_session(
        self, user_id: uuid.UUID, character_id: str, reason: str, now: datetime
    ) -> bool:
        session_id = self.sessions.pop((user_id, character_id), None)
        if session_id is None:
            return False
        self.closed_sessions.append((user_id, character_id, session_id, reason))
        return True

    async def current_session_id(self, user_id: uuid.UUID, character_id: str) -> uuid.UUID | None:
        return self.sessions.get((user_id, character_id))

    async def close_session_by_id(
        self, user_id: uuid.UUID, character_id: str, session_id: uuid.UUID, reason: str, now: datetime
    ) -> bool:
        if self.sessions.get((user_id, character_id)) != session_id:
            return False
        return await self.close_session(user_id, character_id, reason, now)

    async def session_turns(
        self, user_id: uuid.UUID, character_id: str, session_id: uuid.UUID
    ) -> list[dict[str, Any]]:
        return [
            {"turn_id": t.turn_id, "user_input": t.user_input, "reply_text": t.reply_text,
             "response_mode": t.response_mode, "turn_no": t.turn_no, "safety_flags": list(t.safety_flags),
             "character_version": t.character_version, "compose_mode": t.compose_mode,
             "reply_model": t.reply_model, "reply_prompt_version": t.reply_prompt_version, "ts": t.ts}
            for t in self.turns.get((user_id, character_id), [])
            if t.session_id == session_id
        ]  # fmt: skip

    async def latest_turns(self, user_id: uuid.UUID, character_id: str, limit: int) -> list[dict[str, Any]]:
        turns = sorted(self.turns.get((user_id, character_id), []), key=lambda t: (t.ts, t.turn_no))[-limit:]
        return [
            {"session_id": t.session_id, "turn_no": t.turn_no, "user_input": t.user_input,
             "reply_text": t.reply_text, "response_mode": t.response_mode, "ts": t.ts}
            for t in turns
        ]  # fmt: skip

    async def delete_turns(self, user_id: uuid.UUID, character_id: str, turn_ids: list[str]) -> int:
        key = (user_id, character_id)
        gone = [t for t in self.turns.get(key, []) if t.turn_id in turn_ids]
        pairs = {(t.session_id, t.turn_no) for t in gone}
        self.turns[key] = [t for t in self.turns.get(key, []) if t.turn_id not in turn_ids]
        self.emotions[key] = [e for e in self.emotions.get(key, []) if (e.session_id, e.turn_no) not in pairs]
        return len(gone)

    async def emotion_history(
        self, user_id: uuid.UUID, character_id: str, since: datetime | None = None
    ) -> list[dict[str, Any]]:
        events = [e for e in self.emotions.get((user_id, character_id), []) if since is None or e.ts >= since]
        return [
            {"id": e.event_id, "ts": e.ts.isoformat(), "categories_json": e.estimate_json["categories"],
             "vad_json": e.estimate_json["vad"], "confidence": e.estimate_json["confidence"]}
            for e in sorted(events, key=lambda e: e.ts, reverse=True)
        ]  # fmt: skip

    async def delete_emotion_events(self, user_id: uuid.UUID, character_id: str, event_ids: list[str]) -> int:
        key = (user_id, character_id)
        before = len(self.emotions.get(key, []))
        self.emotions[key] = [e for e in self.emotions.get(key, []) if e.event_id not in event_ids]
        return before - len(self.emotions[key])

    async def recent_turns(
        self, user_id: uuid.UUID, character_id: str, session_id: uuid.UUID, limit: int
    ) -> list[HistoryTurn]:
        history: list[HistoryTurn] = []
        turns = [t for t in self.turns.get((user_id, character_id), []) if t.session_id == session_id]
        for turn in turns[-limit:]:
            history.extend(history_entries(turn.user_input, turn.reply_text, turn.response_mode))
        return history

    async def load_turn_state(
        self, user_id: uuid.UUID, character_id: str, session_id: uuid.UUID, initial_relationship: float
    ) -> TurnState:
        key = (user_id, character_id)
        events = [e for e in self.relationship_events.get(key, []) if e["session_id"] == session_id]
        mood = self.user_mood.get(key)
        companion = self.companion.get(key)
        in_session = companion is not None and companion[0] == session_id
        silence = self.silence_counts.get(key)
        return TurnState(
            turn_count=sum(1 for t in self.turns.get(key, []) if t.session_id == session_id),
            consecutive_silence=silence[1] if silence and silence[0] == session_id else 0,
            pressure=self.pressure.get(key, PressureState()),
            preferences=list(self.preferences.get(key, {}).values()),
            relationship=self.relationships.get(key, initial_relationship),
            session_delta=sum(e["delta"] for e in events),
            last_interaction_at=self.previous_activity.get(key),
            timezone=self.timezones.get(user_id, "Asia/Tokyo"),
            user_mood=mood[1].copy() if mood and mood[0] == session_id else None,
            companion=companion[1].copy() if in_session else None,
            companion_mood=(
                self.companion_mood[key][1].copy()
                if key in self.companion_mood and self.companion_mood[key][0] == session_id
                else None
            ),
            rule_counters=dict(self.rule_counters.get(key, {})),
            event_session_total=dict(companion[2].get("event_session_total", {})) if in_session else {},
            last_negative_act_turn=companion[2].get("last_negative_act_turn") if in_session else None,
            sheet_state=copy.deepcopy(companion[2].get("sheet_state") or {}) if in_session else {},
            topic_state=copy.deepcopy(companion[2].get("topic_state") or {}) if in_session else {},
            previous_rule_indexes=(
                {r["index"] for r in companion[2].get("rules", [])} if in_session else set()
            ),
        )

    async def commit_turn(
        self,
        user_id: uuid.UUID,
        character_id: str,
        session_id: uuid.UUID,
        initial_relationship: float,
        build: CommitBuilder,
    ) -> TurnCommit:
        key = (user_id, character_id)
        commit = build(await self.load_turn_state(user_id, character_id, session_id, initial_relationship))
        turns = self.turns.setdefault(key, [])
        turn_no = 1 + sum(1 for t in turns if t.session_id == session_id)
        commit.turn.turn_no = commit.emotion.turn_no = turn_no
        turns.append(commit.turn)
        self.emotions.setdefault(key, []).append(commit.emotion)

        change = commit.state.relationship
        self.relationships[key] = change.value_after
        if change.delta:
            self.relationship_events.setdefault(key, []).append(
                {
                    "session_id": session_id,
                    "turn_no": turn_no,
                    "delta": change.delta,
                    "value_after": change.value_after,
                    "reason_code": "+".join(code for code, _ in change.reasons),
                }
            )
        self.user_mood[key] = (session_id, commit.state.user_mood)
        self.companion[key] = (session_id, commit.state.companion, commit.state.triggers)
        self.last_activity[key] = (session_id, commit.turn.ts)
        self.silence_counts[key] = (session_id, commit.state.consecutive_silence)
        if commit.state.companion_mood is not None:
            self.companion_mood[key] = (session_id, commit.state.companion_mood)
        self.rule_counters.setdefault(key, {}).update(commit.state.rule_counters)
        if commit.state.pressure is not None:
            self.pressure[key] = commit.state.pressure
        if key in self.proactive:
            # ユーザーが話した: 返事のない自発発話は、なくなる
            self.proactive[key].update(unanswered=0, wait_until=None)
        return commit

    async def set_relationship(
        self, user_id: uuid.UUID, character_id: str, value: float, reason: str, now: datetime
    ) -> float:
        key = (user_id, character_id)
        value = max(-20.0, min(20.0, value))
        before = self.relationships.get(key, 0.0)
        self.relationships[key] = value
        self.relationship_events.setdefault(key, []).append(
            {"session_id": None, "turn_no": None, "delta": value - before, "value_after": value, "reason_code": reason}
        )
        return value

    async def mark_crisis(self, user_id: uuid.UUID, now: datetime, exempt_until: datetime) -> None:
        self.safety[user_id] = (now, exempt_until)

    async def load_user_context(self, user_id: uuid.UUID) -> UserContext:
        context = self.user_contexts.get(user_id, UserContext())
        safety = self.safety.get(user_id)
        return replace(context, exempt_until=safety[1] if safety else None)

    async def update_user_settings(self, user_id: uuid.UUID, changes: dict[str, Any]) -> UserContext:
        self.user_contexts[user_id] = replace(self.user_contexts.get(user_id, UserContext()), **changes)
        return await self.load_user_context(user_id)

    async def hit_rate_limit(
        self, user_id: uuid.UUID, now: datetime, per_minute: int, per_day: int
    ) -> str | None:
        hits = [t for t in self.rate_hits.get(user_id, []) if now - t < timedelta(days=1)]
        if sum(1 for t in hits if now - t < timedelta(minutes=1)) >= per_minute:
            return "minute"
        if len(hits) >= per_day:
            return "day"
        self.rate_hits[user_id] = [*hits, now]
        return None

    async def register_serious_event(
        self, user_id: uuid.UUID, now: datetime, threshold: int, duration_minutes: int
    ) -> datetime | None:
        events = [t for t in self.serious_events.get(user_id, []) if now - t < timedelta(days=1)]
        events.append(now)
        self.serious_events[user_id] = events
        if len(events) < threshold:
            return None
        until = now + timedelta(minutes=duration_minutes)
        self.serious_events[user_id] = []  # 制限をかけたら、数えなおす
        self.user_contexts[user_id] = replace(
            self.user_contexts.get(user_id, UserContext()), throttled_until=until
        )
        return until

    async def clear_throttle(self, user_id: uuid.UUID) -> None:
        if user_id in self.user_contexts:
            self.user_contexts[user_id] = replace(self.user_contexts[user_id], throttled_until=None)

    async def record_security_events(
        self,
        user_id: uuid.UUID,
        character_id: str,
        session_id: uuid.UUID | None,
        turn_id: str | None,
        findings: list[Finding],
        now: datetime,
    ) -> None:
        for finding in findings:
            scope = {"user_id": user_id, "character_id": character_id, "session_id": session_id}
            self.security_events.append({**scope, "turn_id": turn_id, "ts": now, **finding.as_item()})

    async def save_pressure(
        self, user_id: uuid.UUID, character_id: str, pressure: PressureState, now: datetime
    ) -> None:
        self.pressure[(user_id, character_id)] = pressure

    async def set_interaction_preferences(
        self, user_id: uuid.UUID, character_id: str, adjustments: list[Adjustment], now: datetime
    ) -> None:
        items = self.preferences.setdefault((user_id, character_id), {})
        for item in adjustments:
            items[item.element] = {"element": item.element, "direction": item.direction, "summary": item.summary}

    async def clear_interaction_preferences(self, user_id: uuid.UUID, character_id: str, now: datetime) -> None:
        self.preferences.pop((user_id, character_id), None)

    async def proactive_snapshot(self, user_id: uuid.UUID, character_id: str, now: datetime) -> ProactiveSnapshot:
        key = (user_id, character_id)
        timezone = self.timezones.get(user_id, "Asia/Tokyo")
        state = self.proactive.get(key, {})
        last = self.last_activity.get(key)
        mood = self.user_mood.get(key)
        safety = self.safety.get(user_id)
        day = _local_day_text(now, timezone)
        return ProactiveSnapshot(
            relationship=self.relationships.get(key),
            last_activity_at=last[1] if last else None,
            session_active=key in self.sessions,
            unanswered=int(state.get("unanswered", 0)),
            sent_today=int(state.get("sent", {}).get(day, 0)),
            last_crisis_at=safety[0] if safety else None,
            last_valence=mood[1].vad.get("valence") if mood else None,
            last_anger=mood[1].categories.get("anger") if mood else None,
            timezone=timezone,
        )

    async def record_proactive(
        self, user_id: uuid.UUID, character_id: str, record: TurnRecord, now: datetime, wait_until: datetime
    ) -> TurnRecord:
        key = (user_id, character_id)
        session_id = uuid.uuid4()
        self.sessions[key] = session_id
        if key in self.last_activity:
            self.previous_activity[key] = self.last_activity[key][1]
        record.session_id, record.turn_no = session_id, 1
        self.turns.setdefault(key, []).append(record)
        self.last_activity[key] = (session_id, now)
        state = self.proactive.setdefault(key, {})
        state.update(
            unanswered=int(state.get("unanswered", 0)) + 1, last_sent_at=now, wait_until=wait_until,
            session_id=session_id, turn_no=1, seen=False,
        )  # fmt: skip
        day = _local_day_text(now, self.timezones.get(user_id, "Asia/Tokyo"))
        sent = state.setdefault("sent", {})
        sent[day] = sent.get(day, 0) + 1
        return record


    async def unseen_proactive(self, user_id: uuid.UUID, character_id: str) -> dict[str, Any] | None:
        key = (user_id, character_id)
        state = self.proactive.get(key)
        if not state or state.get("seen") or state.get("session_id") is None:
            return None
        turn = next(
            (t for t in self.turns.get(key, []) if t.session_id == state["session_id"] and t.turn_no == 1), None
        )
        if turn is None:
            return None
        return {"session_id": turn.session_id, "turn_no": 1, "text": turn.reply_text, "ts": turn.ts}

    async def mark_proactive_seen(
        self, user_id: uuid.UUID, character_id: str, session_id: uuid.UUID, now: datetime
    ) -> bool:
        state = self.proactive.get((user_id, character_id))
        if not state or state.get("session_id") != session_id:
            return False
        state.update(seen=True, seen_at=now)
        return True


def _local_day_text(moment: datetime, timezone: str) -> str:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo("Asia/Tokyo")
    return moment.astimezone(zone).date().isoformat()
