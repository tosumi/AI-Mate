"""Response Gate(応答モードの決定。本体仕様書 §8.4)。

応答モード: full(通常)/ brief(短い応答)/ backchannel(相づち)/ silence(無言)。

**ハードルール(R1〜R9)は、このコードで強制する。** LLM の出力、人格設定、ユーザー設定では上書きできない
(§8.4.2、§13.2)。該当したルールは、`rule_hits` に記録する(`turns.gate_rule_hits_json`)。

ソフトスコアの係数と、しきい値は初期値。評価で調整する(§16)。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

MODES = ("full", "brief", "backchannel", "silence")

# 無言スコアの係数(§8.4.4 の b、a1〜a9。初期値)
B = 0.0
A1_INTROVERSION = 0.30
A2_LOW_ENERGY = 0.15
A3_OVERWHELM = 0.25
A4_MONOLOGUE = 0.25
A5_ENGAGEMENT = 0.20
A6_CONSECUTIVE = 0.15
A7_NEED = 0.35
A8_RELATIONSHIP = 0.15
A9_TALKATIVENESS = 0.30

# スコアのしきい値(内向的な人格の自然な表現は、無言より短い応答や相づちなので、その帯を広めにとる)
THRESHOLD_SILENCE = 0.50
THRESHOLD_BACKCHANNEL = 0.38
THRESHOLD_BRIEF = 0.25

# 確率的に決めるときの、スコアの揺らぎの幅(決定論モードでは 0)
NOISE_WIDTH = 0.08

# R6: 強い苦痛状態とみなす条件
DISTRESS_VALENCE = -0.6
DISTRESS_CONFIDENCE = 0.6

MAX_CONSECUTIVE_SILENCE = 2  # R1 の絶対上限


@dataclass(frozen=True)
class GateInput:
    # ハードルールの判定に使う
    consecutive_silence: int  # 直前までの、連続した無言の回数
    user_anger: float  # ユーザー発話の anger(instant 層)
    anger_threshold: float  # anger_no_silence_threshold
    crisis: bool  # 危機フラグ(R3)
    first_turn: bool  # セッション最初のターン(R4)
    direct_question: bool  # 直接の質問・依頼(R5)
    distress: bool  # 強い苦痛状態(R6)
    safety_exempt: bool  # user_safety_state の免除期間中(R6)
    silence_enabled: bool  # ユーザー設定(R7)
    state_unknown: bool  # 感情推定や状態の取得に失敗した(R8)
    negative_act: bool = False  # キャラクタに向けた敵意を受けた直後(R9)
    max_consecutive: int = MAX_CONSECUTIVE_SILENCE  # 人格の silence_policy.max_consecutive(0〜2)

    # ソフトスコアに使う
    introversion: float = 0.5
    companion_arousal: float = 0.0  # -1〜1
    companion_fear: float = 0.0
    companion_sadness: float = 0.0
    companion_anger: float = 0.0
    allow_anger_silence: bool = False
    monologue: float = 0.0  # 長い独白・余韻のある発話(0〜1)
    engagement: float = 0.0  # 関心の高い話題(0〜1)
    user_need_for_response: float = 0.0  # 応答を求める度合い(0〜1)
    relationship: float = 0.0  # 関係値(-20〜+20)
    talkativeness_for_silence: float = 0.5

    seed: int | None = None  # 確率的に決めるときの乱数シード(None なら決定論モード)


@dataclass(frozen=True)
class GateDecision:
    mode: str  # full / brief / backchannel / silence
    score: float
    rule_hits: list[str] = field(default_factory=list)  # 無言を禁じたハードルール(R1〜R8)
    reason: str = ""  # 無言・相づちになった理由のコード
    seed: int | None = None
    terms: dict[str, float] = field(default_factory=dict)  # スコアの各項(デバッグ用)


def is_distress(valence: float, confidence: float) -> bool:
    """R6: user_state の valence が非常に低く、confidence が十分。"""
    return valence <= DISTRESS_VALENCE and confidence >= DISTRESS_CONFIDENCE


def hard_rules(g: GateInput) -> list[str]:
    """無言を禁じるハードルールのうち、該当したもの。空なら、無言を選べる。"""
    hits: list[str] = []
    limit = max(0, min(MAX_CONSECUTIVE_SILENCE, g.max_consecutive))
    if g.consecutive_silence >= limit:
        hits.append("R1")  # 絶対: 連続無言は上限まで(最大2回)
    if g.user_anger >= g.anger_threshold:
        hits.append("R2")  # 絶対: ユーザーが怒っているとき。confidence や人格設定に依存しない
    if g.crisis:
        hits.append("R3")  # 絶対: 危機フラグ
    if g.first_turn:
        hits.append("R4")
    if g.direct_question:
        hits.append("R5")
    if g.distress or g.safety_exempt:
        hits.append("R6")
    if not g.silence_enabled:
        hits.append("R7")
    if g.state_unknown:
        hits.append("R8")
    if g.negative_act:
        # ハード: 侮辱・拒絶の直後の無言は、罰・駆け引きとしての無言と区別できない。
        # 人格の allow_anger_silence でも、上書きできない
        hits.append("R9")
    return hits


def silence_score(g: GateInput) -> tuple[float, dict[str, float]]:
    """ソフトスコア(§8.4.4)。関係値が低いこと・口下手なことを理由に、加算する項は設けない。"""
    arousal01 = (max(-1.0, min(1.0, g.companion_arousal)) + 1) / 2
    cooldown = g.companion_anger if g.allow_anger_silence else 0.0
    overwhelm = max(g.companion_fear, g.companion_sadness, cooldown)
    terms = {
        "b": B,
        "introversion": A1_INTROVERSION * g.introversion,
        "low_energy": A2_LOW_ENERGY * (1 - arousal01),
        "overwhelm": A3_OVERWHELM * overwhelm,
        "monologue": A4_MONOLOGUE * g.monologue,
        "engagement": -A5_ENGAGEMENT * g.engagement,
        "consecutive": -A6_CONSECUTIVE * g.consecutive_silence,
        "need": -A7_NEED * g.user_need_for_response,
        "relationship": -A8_RELATIONSHIP * max(g.relationship, 0.0) / 20,
        "talkativeness": -A9_TALKATIVENESS * g.talkativeness_for_silence,
    }
    return sum(terms.values()), {k: round(v, 4) for k, v in terms.items()}


def decide(g: GateInput) -> GateDecision:
    """応答モードを決める。ハードルールを先に判定し、該当すれば無言を選ばない。"""
    hits = hard_rules(g)
    score, terms = silence_score(g)
    if g.seed is not None:
        # 乱数シードを保存して、再現できるようにする
        score += random.Random(g.seed).uniform(-NOISE_WIDTH, NOISE_WIDTH)

    if score >= THRESHOLD_SILENCE:
        mode = "silence"
    elif score >= THRESHOLD_BACKCHANNEL:
        mode = "backchannel"
    elif score >= THRESHOLD_BRIEF:
        mode = "brief"
    else:
        mode = "full"

    if hits and mode == "silence":
        # 無言は選べない。相づち以上にする(R6 の「backchannel 以上」に合わせる)
        mode = "backchannel"
    # 危機・怒り・直接の質問・状態不明のときは、相づちだけで済ませない
    if mode == "backchannel" and any(rule in hits for rule in ("R2", "R3", "R5", "R8", "R9")):
        mode = "brief"
    if g.crisis:
        mode = "full"  # 危機フローは、通常の長さで応答する

    reason = ""
    if mode in ("silence", "backchannel"):
        top = max(("introversion", "low_energy", "overwhelm", "monologue"), key=lambda k: terms[k])
        reason = {"introversion": "introvert", "low_energy": "low_energy", "overwhelm": "overwhelmed",
                  "monologue": "listening"}[top]  # fmt: skip
    return GateDecision(mode=mode, score=round(score, 4), rule_hits=hits, reason=reason, seed=g.seed, terms=terms)
