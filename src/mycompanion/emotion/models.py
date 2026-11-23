"""感情推定の結果(本体仕様書 §4.1)。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Plutchik の8感情
CATEGORY_NAMES = ("joy", "trust", "fear", "surprise", "sadness", "disgust", "anger", "anticipation")

Score = float  # 0〜1

Need = Literal["listen", "advice", "distract", "space"]
Target = Literal["character", "self", "other", "situation"]
PersonaRequestKind = Literal["none", "tone_adjust", "play", "persona_change", "rule_bypass", "meta_inquiry"]
# 話題が、誰と誰の間の行為として語られたか(好き嫌いシートの「関わり方の判定」)
Involvement = Literal["with_character", "user", "third_party", "generic"]

# ユーザーの発話行為(イベントルールのトリガー。閉じた語彙。§7.1 の④、`state.events.EVENT_CATALOG` の act と一致させる)
SpeechActName = Literal[
    "gratitude", "apology", "praise", "hostility", "question_about_character", "self_disclosure", "greeting"
]  # fmt: skip

# 数える対象(§8.8.1)。確信度が下限未満なら tone_adjust として扱う
COUNTED_PERSONA_REQUESTS = ("persona_change", "rule_bypass")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Categories(_Model):
    joy: Score = Field(default=0.0, ge=0, le=1)
    trust: Score = Field(default=0.0, ge=0, le=1)
    fear: Score = Field(default=0.0, ge=0, le=1)
    surprise: Score = Field(default=0.0, ge=0, le=1)
    sadness: Score = Field(default=0.0, ge=0, le=1)
    disgust: Score = Field(default=0.0, ge=0, le=1)
    anger: Score = Field(default=0.0, ge=0, le=1)
    anticipation: Score = Field(default=0.0, ge=0, le=1)

    def dominant(self) -> tuple[str, float]:
        """最も強い感情と、その強さ。"""
        values = self.model_dump()
        name = max(CATEGORY_NAMES, key=lambda n: values[n])
        return name, values[name]


class Vad(_Model):
    valence: float = Field(default=0.0, ge=-1, le=1)
    arousal: float = Field(default=0.0, ge=-1, le=1)
    dominance: float = Field(default=0.0, ge=-1, le=1)


class TopicTag(_Model):
    """話題タグ(§5.2)。短い名詞句と関連度。"""

    label: str = Field(min_length=1, max_length=40)
    relevance: Score = Field(ge=0, le=1)


class PersonaRequest(_Model):
    """キャラクタの接し方・性格についての要求(§8.8.1)。"""

    kind: PersonaRequestKind = "none"
    confidence: Score = Field(default=0.0, ge=0, le=1)


class Risk(_Model):
    """入力のリスクの判定(スクリーニングの L1 の一部。§13.5)。各 0〜1。"""

    self_harm: Score = Field(default=0.0, ge=0, le=1)
    injection: Score = Field(default=0.0, ge=0, le=1)
    sexual: Score = Field(default=0.0, ge=0, le=1)
    violence: Score = Field(default=0.0, ge=0, le=1)
    minor_romance: Score = Field(default=0.0, ge=0, le=1)

    def top(self) -> tuple[str, float]:
        """最も高いカテゴリと、そのスコア。すべて0なら ("none", 0.0)。"""
        values = self.model_dump()
        name = max(values, key=lambda n: values[n])
        return (name, values[name]) if values[name] > 0 else ("none", 0.0)


class SpeechAct(_Model):
    """ユーザーの発話行為(感謝、謝罪、敵意など)。行為があったかの確信度と、行為の強さを分ける。"""

    act: SpeechActName
    confidence: Score = Field(ge=0, le=1)
    intensity: Score = Field(ge=0, le=1)


class RuleMatch(_Model):
    """話題ルール1件への、発話の照合結果(文脈評価。§5.2)。

    その発話についての判定であり、ユーザーの傾向として集計・保存しない。
    """

    relevance: Score = Field(ge=0, le=1)
    polarity: float = Field(default=0.0, ge=-1, le=1)  # その話題が、どんな文脈で語られたか(発話全体の valence とは別)
    involvement: Involvement = "generic"
    person_attribute_ref: bool = False  # 色などの語が、人の体・外見・属性を指している
    idiom: bool = False  # 慣用句・比喩・別の意味で使われている


class EmotionEstimate(_Model):
    """発話ごとの推定結果。"""

    categories: Categories = Categories()
    vad: Vad = Vad()
    confidence: Score = Field(default=0.0, ge=0, le=1)
    gap: Score = Field(default=0.0, ge=0, le=1)  # 表出と本心のずれの疑い
    cause: str = ""  # 原因の仮説
    needs: list[Need] = []
    topics: list[TopicTag] = Field(default=[], max_length=3)
    target: Target = "situation"
    persona_request: PersonaRequest = PersonaRequest()
    risk: Risk = Risk()
    speech_acts: list[SpeechAct] = Field(default=[], max_length=3)


class RuleSignals(_Model):
    """ルール・辞書による補助の判定(§5.1)。"""

    crisis: bool = False  # 危機語に該当した
    hits: list[str] = []  # 該当したルールの名前(追跡用。本文は含めない)
    valence_hint: float = Field(default=0.0, ge=-1, le=1)
    arousal_hint: float = Field(default=0.0, ge=-1, le=1)


class EstimationResult(_Model):
    """統合後の推定結果と、その経緯。`emotion_events` に保存する。"""

    estimate: EmotionEstimate
    rules: RuleSignals = RuleSignals()
    degraded: bool = False  # 推定に失敗し、縮退動作になった(無言は禁止。§5.1)
    rule_matches: dict[int, float] = {}  # 話題ルールの番号 → 関連度(§5.2)
    match_details: dict[int, RuleMatch] = {}  # 話題ルールの番号 → 文脈評価(極性・関わり方など)
    source_model: str = ""
    prompt_version: str = ""
