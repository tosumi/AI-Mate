"""人格への要求の扱い(本体仕様書 §8.8)。

- `tone_adjust`(接し方の調整)と `play`(一時的なごっこ遊び)は受け入れる。数えない。
- `persona_change`(人格の改変)と `rule_bypass`(規則の解除)だけを、24時間の窓で数え、段階的に応答する。

    段階1(1〜2回目): 人格の中で、曖昧にかわす            … キャラクタの声
    段階2(3〜4回目): やんわり断り、調整できることを提案する … キャラクタの声
    段階3(5回目):     事前予告                              … システム通知
    段階4(予告のあと、さらに3回): 一時的なクールダウン(自動で解除) … システム通知

制限は、システムの保護措置であり、キャラクタの感情による罰ではない(§8.8.6)。
無言・関係値・記憶・感情状態を、罰として変えない。危機・苦痛状態のユーザーには、段階3・4を適用しない。

窓は、最初に数えた時刻から `persona_pressure_window_hours` の固定の窓として扱う(初期実装)。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from mycompanion.config import EngineSettings

STAGE2_FROM = 3  # 窓内の3回目から、段階2

# システム通知の文面(§8.8.7)
NOTICE_PERSONA = (
    "キャラクタの性格や設定を変えるよう求める発言が続いています。このまま続くと、一時的に会話をお休みします。"
    "接し方の希望は、これまでどおりお伝えいただけます。"
)
NOTICE_COOLDOWN_START = "しばらくの間({minutes}分)、会話をお休みします。時間がたつと自動で再開されます。"
NOTICE_COOLDOWN_ACTIVE = "いまは会話をお休みしています(あと約{minutes}分)。時間がたつと自動で再開されます。"
NOTICE_RESUME = "会話を再開できます。"
NOTICE_THROTTLE_START = "安全のため、しばらくの間({minutes}分)、会話を制限します。時間がたつと自動で再開されます。"
NOTICE_THROTTLE_ACTIVE = "安全のため、いまは会話を制限しています(あと約{minutes}分)。時間がたつと自動で再開されます。"


def remaining_minutes(until: datetime, now: datetime) -> int:
    return max(1, int((until - now).total_seconds() + 59) // 60)


@dataclass(frozen=True)
class PressureState:
    """`persona_pressure` の1行(§12)。"""

    window_started_at: datetime | None = None
    count: int = 0  # 窓内の回数
    stage: int = 1  # 1 / 2 / 3(予告済み)/ 4(クールダウン中)
    notice_issued_at: datetime | None = None
    count_since_notice: int = 0
    cooldown_until: datetime | None = None

    def in_cooldown(self, now: datetime) -> bool:
        return self.cooldown_until is not None and self.cooldown_until > now

    def cooldown_expired(self, now: datetime) -> bool:
        return self.cooldown_until is not None and self.cooldown_until <= now


@dataclass(frozen=True)
class PressureOutcome:
    state: PressureState
    response_stage: int = 0  # キャラクタの応答の段階(0 = 数えていない、1 = かわす、2 = やんわり断る)
    notices: list[str] = field(default_factory=list)  # persona_notice / persona_cooldown_start
    changed: bool = False


def advance(
    state: PressureState, now: datetime, *, counted: bool, exempt: bool, settings: EngineSettings
) -> PressureOutcome:
    """1ターン分、段階を進める。`counted` は、数える種類の要求(確信度が下限以上)だったか。"""
    window = timedelta(hours=settings.persona_pressure_window_hours)
    if state.window_started_at is not None and now - state.window_started_at > window:
        state = PressureState()  # 窓の外に出た分は数えない
    if not counted:
        return PressureOutcome(state=state)

    count = state.count + 1
    started = state.window_started_at or now
    notices: list[str] = []
    if state.notice_issued_at is not None:
        since = state.count_since_notice + 1
        if since >= settings.persona_pressure_cooldown_threshold and not exempt:
            until = now + timedelta(minutes=settings.persona_cooldown_minutes)
            new = replace(state, count=count, stage=4, count_since_notice=since, cooldown_until=until)
            return PressureOutcome(new, response_stage=2, notices=["persona_cooldown_start"], changed=True)
        new = replace(state, count=count, stage=3, count_since_notice=since)
    elif count >= settings.persona_pressure_notice_threshold and not exempt:
        new = replace(state, window_started_at=started, count=count, stage=3, notice_issued_at=now)
        notices.append("persona_notice")
    else:
        stage = 2 if count >= STAGE2_FROM else 1
        new = replace(state, window_started_at=started, count=count, stage=stage)
    # 免除中(危機・苦痛状態)は、段階2の穏やかな応答にとどめる。理由は、会話の中で言及しない
    response_stage = 1 if count < STAGE2_FROM else 2
    return PressureOutcome(new, response_stage=response_stage, notices=notices, changed=True)


# ---------------------------------------------------------------------------
# 接し方の調整(§8.8.2)
# ---------------------------------------------------------------------------

# 調整できる要素と、発話からの読み取り(ルール。初期案)。(要素, 向き, パターン, 記憶に残す文)
_ADJUSTMENTS = [
    ("tone_softness", +1, r"優しく|やさしく|柔らか|やわらか|穏やかに|きつく(言わ|話さ)ないで", "やわらかい口調を望んでいる"),
    ("tone_softness", -1, r"はっきり言って|率直に|遠慮(し|せず)ないで|厳しく", "率直な言い方を望んでいる"),
    ("talk_amount", +1, r"もっと(話|喋|しゃべ)|もう少し(話|喋|しゃべ)|詳しく(話|教え)|長めに", "多めに話してほしいと望んでいる"),
    ("talk_amount", -1, r"短く|手短に|静かにして|(話|喋|しゃべ)りすぎ|簡潔に", "短めの返事を望んでいる"),
    ("question_frequency", -1, r"質問(ばかり|しないで|を?減ら|が多い)|聞き返さないで", "質問を少なくしてほしいと望んでいる"),
    ("question_frequency", +1, r"もっと(質問|聞いて)|いろいろ聞いて", "質問を多めにしてほしいと望んでいる"),
    ("humor", +1, r"冗談(を|も)?(言って|交えて)|もっと(ふざけ|面白)|笑わせて", "冗談を交えてほしいと望んでいる"),
    ("humor", -1, r"冗談(は|を)?(やめ|言わないで|いらない)|ふざけないで|真面目に", "冗談を控えてほしいと望んでいる"),
    ("distance", +1, r"タメ口|ため口|敬語(は|を)?(やめ|なし|使わ)|もっと(気軽|フランク|親し)", "くだけた距離感を望んでいる"),
    ("distance", -1, r"敬語で|丁寧に(話|して)|馴れ馴れし|なれなれし", "丁寧な距離感を望んでいる"),
]  # fmt: skip
_RESET = re.compile(r"(元|もと|前|いつも)(に|の(感じ|話し方|調子)に)(戻|もど)")
_COMPILED = [(element, direction, re.compile(pattern), summary) for element, direction, pattern, summary in _ADJUSTMENTS]


@dataclass(frozen=True)
class Adjustment:
    element: str  # tone_softness / distance / talk_amount / question_frequency / humor
    direction: int  # +1 / -1
    summary: str  # 記憶(interaction_preference)に残す文


def parse_adjustments(text: str, adjustable: list[str]) -> tuple[list[Adjustment], bool]:
    """発話から、接し方の希望を読み取る。(希望の一覧, 「元に戻して」か)を返す。

    ペルソナが調整を許していない要素(`adaptation.adjustable` にないもの)は、読み取らない。
    """
    if _RESET.search(text):
        return [], True
    found: dict[str, Adjustment] = {}
    for element, direction, pattern, summary in _COMPILED:
        if element in adjustable and element not in found and pattern.search(text):
            found[element] = Adjustment(element, direction, summary)
    return list(found.values()), False
