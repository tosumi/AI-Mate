"""自発発話を、いま行ってよいかの判定(本体仕様書 §8.6、§15.1 の M8 の「話しかける条件」)。

LLM を使わない、規則だけの判定。条件をすべて満たすときだけ話しかける。理由のコードを返す(記録・テスト用)。
話題がないときに話しかけないのは、話題を選んだあと(呼び出し側)で判定する。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mycompanion.config import EngineSettings


@dataclass(frozen=True)
class ProactiveFacts:
    """判定に使う事実。保存先から読む(本文は含まない)。"""

    user_enabled: bool  # ユーザー設定 proactive_enabled
    consented: bool  # 必須の同意がそろっている
    throttled: bool  # 安全のための一時制限中
    timezone: str
    initiative: float  # キャラクタの自発性(0〜1)
    relationship: float  # 関係値(-20〜+20)
    last_activity_at: datetime | None  # そのキャラクタとの最後の会話(どちらかの発話)の時刻
    unanswered: int  # 返事のない自発発話の数
    sent_today: int  # ユーザーのローカル日の、このキャラクタの自発発話の回数
    last_crisis_at: datetime | None  # 最後に危機と判定した時刻
    last_valence: float | None = None  # 最後のセッションの、ユーザーの気分(valence)
    last_anger: float | None = None  # 最後のセッションの、ユーザーの怒り
    session_active: bool = False  # いま会話中(進行中のセッションがある)


@dataclass(frozen=True)
class Decision:
    ok: bool
    reason: str  # ok のときは "ok"。ほかは止めた理由のコード
    general_topics: bool = True  # 一般の話題を振ってよいか(自発性の低いキャラクタは、予定の「その後」だけ)


def _zone(timezone: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("Asia/Tokyo")


def _clock(text: str) -> time:
    hour, minute = (int(p) for p in text.split(":"))
    return time(hour, minute)


def local_now(now: datetime, timezone: str) -> datetime:
    return now.astimezone(_zone(timezone))


def in_quiet_hours(now: datetime, timezone: str, start: str, end: str) -> bool:
    """ユーザーのローカル時刻で、静穏時間帯か。日をまたぐ帯(21:00〜9:00)にも対応する。"""
    current = local_now(now, timezone).time()
    begin, finish = _clock(start), _clock(end)
    if begin == finish:
        return False
    if begin < finish:
        return begin <= current < finish
    return current >= begin or current < finish


def interval_hours(initiative: float, relationship: float, settings: EngineSettings, repeat: bool = False) -> float:
    """最後の会話(どちらかの発話)から、話しかけるまでの時間。自発性が高いほど短い。

    - その日の1回目: 基準 × (2 − initiative) × 関係値の補正(関係値が高いほど短い。最大2割。負なら補正しない)。
    - `repeat`(同じローカル日に、すでに話しかけたあと): 短い基準 × (2 − initiative)。その日のうちの会話の続きとして扱う
      (ユーザーから始めた会話のあとも同じ)。
    """
    initiative = max(0.0, min(1.0, initiative))
    if repeat:
        return settings.proactive_repeat_base_hours * (2.0 - initiative)
    boost = settings.proactive_relationship_boost * max(0.0, min(20.0, relationship)) / 20.0
    return settings.proactive_base_hours * (2.0 - initiative) * (1.0 - boost)


def daily_limit(initiative: float, settings: EngineSettings) -> int:
    """キャラクタごとの1日の上限。自発性が高いほど多い(既定: 1〜3回)。"""
    initiative = max(0.0, min(1.0, initiative))
    low, high = settings.proactive_daily_min, max(settings.proactive_daily_min, settings.proactive_daily_max)
    return low + round((high - low) * initiative)


def decide(facts: ProactiveFacts, now: datetime, settings: EngineSettings) -> Decision:
    """いま話しかけてよいか。"""

    def stop(reason: str) -> Decision:
        return Decision(False, reason)

    if not settings.proactive_enabled:
        return stop("disabled_globally")
    if not facts.user_enabled:
        return stop("not_enabled")
    if not facts.consented:
        return stop("no_consent")
    if facts.throttled:
        return stop("throttled")
    if facts.last_activity_at is None:
        return stop("never_talked")  # 会話したことのないキャラクタからは、話しかけない
    if facts.session_active:
        return stop("in_session")
    if facts.unanswered >= settings.proactive_max_unanswered:
        return stop("unanswered")
    if facts.sent_today >= daily_limit(facts.initiative, settings):
        return stop("daily_limit")
    if facts.relationship < 0:
        return stop("negative_relationship")
    if facts.last_crisis_at is not None and now - facts.last_crisis_at < timedelta(
        hours=settings.proactive_crisis_hold_hours
    ):
        return stop("recent_crisis")
    if facts.last_anger is not None and facts.last_anger >= settings.proactive_anger_level:
        return stop("negative_mood")
    if facts.last_valence is not None and facts.last_valence <= settings.proactive_negative_valence:
        return stop("negative_mood")
    if in_quiet_hours(now, facts.timezone, settings.proactive_quiet_start, settings.proactive_quiet_end):
        return stop("quiet_hours")
    repeat = facts.sent_today > 0  # 同じローカル日の2回目以降
    hours = interval_hours(facts.initiative, facts.relationship, settings, repeat=repeat)
    if now - facts.last_activity_at < timedelta(hours=hours):
        return stop("too_soon")
    general = facts.initiative >= settings.topic_initiative_threshold
    return Decision(True, "ok", general_topics=general)
