"""キャラクタ起点の話題選択(Topic Selector。本体仕様書 §8.7)。

キャラクタが自分から話題を出す場面で、記憶から話題の候補を選ぶ。
v1.0 で動くのは、`initiative` が高く、会話が途切れそうなときだけ(応答の中で話題を振る)。

- 辛い記憶を、キャラクタから蒸し返さない。センシティブ項目、ユーザーが `mute` した記憶、隔離された記憶は選ばない。
- 記憶を過剰に持ち出さない(1セッションの回数と、同じ記憶の間隔に上限)。
- 乱数シードを保存して、再現できるようにする。

係数 b1〜b5 と、しきい値は初期値。評価で調整する。
"""

from __future__ import annotations

import math
import random
import re
from datetime import date, datetime, timedelta

from mycompanion.config import EngineSettings
from mycompanion.memory import scoring
from mycompanion.memory.models import Memory

# 話題のスコアの係数(§8.7)
B1_PREFERENCE = 0.30  # 嗜好度(符号つき。苦手な話題は選ばれにくい)
B2_ACTIVATION = 0.20
B3_RELEVANCE = 0.25  # 現在の会話との関連度
B4_FRESHNESS = 0.15  # 最近言及されていないほど高い
B5_EMOTION_FIT = 0.10  # ユーザーの現在の感情に合う
MIN_SCORE = 0.25  # これに満たなければ、話題を出さない
TOP_N = 5
NEGATIVE_VALENCE = -0.5  # これ未満の感情タグの記憶は、持ち出さない
STALL_CHARS = 10  # ユーザー発話が、この文字数以下なら「途切れそう」

_BACKCHANNEL = re.compile(r"^(うん|はい|そう(だね|なんだ|か)?|へえ|ふーん|なるほど|そっか|ええ|まあね?|別に|[\s…・.。、ー〜~!?w]+)+$")
# 恋愛的な内容(18歳未満フラグが true のユーザーには、キャラクタから持ち出さない。§13.4.4)
_ROMANTIC = re.compile(r"恋|好きな人|彼氏|彼女|デート|キス|告白|片思い|両思い")


def is_romantic(text: str) -> bool:
    return bool(_ROMANTIC.search(text))


def is_stalling(text: str) -> bool:
    """会話が途切れそうか(発話が短い、または相づちだけ)。"""
    stripped = text.strip()
    return len(stripped) <= STALL_CHARS or bool(_BACKCHANNEL.match(stripped))


def should_offer(
    text: str, initiative: float, mentions_in_session: int, settings: EngineSettings, seed: int
) -> bool:
    """話題を振るか。`initiative` がしきい値以上のとき、確率 `initiative` で振る(シードで再現できる)。"""
    if initiative < settings.topic_initiative_threshold or not is_stalling(text):
        return False
    if mentions_in_session >= settings.proactive_mention_max_per_session:
        return False
    return random.Random(seed).random() < initiative


def eligible(memory: Memory, now: datetime, settings: EngineSettings, under_18: bool) -> bool:
    if memory.kind not in ("episodic", "semantic", "relationship") or not memory.searchable:
        return False
    if memory.muted or memory.sensitivity or memory.core_key:  # 呼び名などは、話題として持ち出さない
        return False
    if memory.emotion_vad and float(memory.emotion_vad.get("valence", 0.0)) < NEGATIVE_VALENCE:
        return False
    cooldown = timedelta(days=settings.proactive_mention_cooldown_days)
    if memory.last_mentioned_by_character_at and now - memory.last_mentioned_by_character_at < cooldown:
        return False
    return not (under_18 and _ROMANTIC.search(memory.summary))


def topic_score(
    memory: Memory,
    query: list[float] | None,
    today: date,
    user_vad: dict[str, float] | None,
    use_importance: bool = False,
) -> float:
    """話題のスコア(§8.7)。`use_importance` なら、いまの会話との関連度の代わりに、記憶の重要度を使う。

    自発発話(§15.1 の M8)では、いまの会話がないので関連度が 0 になり、キャラクタの好みに合わない記憶は
    しきい値に届かなかった(2026-11-22 の dev での確認)。ユーザーにとって大事な話題を選べるように、重要度で置き換える。
    """
    if memory.last_recalled_at is None:
        freshness = 1.0
    else:
        days = max(0, (today - memory.last_recalled_at.date()).days)
        freshness = 1.0 - 2.0 ** (-days / 14.0)
    return (
        B1_PREFERENCE * memory.preference
        + B2_ACTIVATION * scoring.clip(memory.activation / scoring.ACTIVATION_NORMALIZER, 0.0, 1.0)
        + B3_RELEVANCE * (
            scoring.clip(memory.importance, 0.0, 1.0) if use_importance else max(scoring.cosine(query, memory.embedding), 0.0)
        )
        + B4_FRESHNESS * freshness
        + B5_EMOTION_FIT * scoring.emotion_closeness(memory, user_vad)
    )


def select(
    memories: list[Memory],
    query: list[float] | None,
    now: datetime,
    today: date,
    user_vad: dict[str, float] | None,
    settings: EngineSettings,
    seed: int,
    under_18: bool = False,
    use_importance: bool = False,
) -> list[tuple[Memory, float]]:
    """話題の候補を1件選ぶ。上位から、温度つきのソフトマックスでサンプリングする。なければ空。"""
    scored = [
        (memory, topic_score(memory, query, today, user_vad, use_importance))
        for memory in memories
        if eligible(memory, now, settings, under_18)
    ]
    scored = sorted((pair for pair in scored if pair[1] >= MIN_SCORE), key=lambda pair: pair[1], reverse=True)[:TOP_N]
    if not scored:
        return []
    temperature = settings.topic_selection_temperature
    # スコアの差が 0.1 で、選ばれやすさがおおよそ e^(1/temperature) 倍になるようにする
    weights = [math.exp((score - scored[0][1]) * 10.0 / temperature) for _, score in scored]
    chosen = random.Random(seed).choices(scored, weights=weights, k=1)[0]
    return [chosen]
