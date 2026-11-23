"""ペルソナ定義ファイルの構造(ペルソナデザイナ仕様書 §4.2、§4.3)。

編集用の形式は YAML。ここでは、その構造と値の範囲を定める。
範囲外の値は、読み込み時に検査で見つける(lint の E 系。`persona.lint`)。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from mycompanion.emotion.models import CATEGORY_NAMES

# 差分を指定できる表出パラメータ(introversion は軸 i そのものなので対象外)
DELTA_KEYS = ("talkativeness", "articulation", "expressiveness", "initiative", "relationship_sensitivity")
# 連想のしやすさ・話題の移りやすさ(本体仕様書 §9.13)。差分で調整できる。既定値は、性格タイプの軸から決まる
ASSOCIATION_KEYS = ("association_ease", "topic_shift")
# 解決済みの表出パラメータ(6項目)
TRAIT_KEYS = ("introversion", *DELTA_KEYS)

# 接し方の調整(§5.6)の選択肢
ADJUSTABLE_CHOICES = ("tone_softness", "distance", "talk_amount", "question_frequency", "humor")
FIXED_CHOICES = ("first_person", "speech_endings", "catchphrases", "core_values", "character_name")

CHARACTER_ID_PATTERN = r"^[A-Za-z0-9_]+$"

Unit = float  # 0〜1
Signed = float  # -1〜1


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Axes(_Model):
    i: Unit = Field(ge=0, le=1)  # 内向度
    a: Unit = Field(ge=0, le=1)  # 抽象度
    e: Unit = Field(ge=0, le=1)  # 共感度
    f: Unit = Field(ge=0, le=1)  # 柔軟度


class QuizRecord(_Model):
    """診断の記録(§7.2.3)。回答者の識別情報は持たない。"""

    bank_version: int
    answers: dict[str, int]
    scores: dict[str, float] = {}
    strength: dict[str, float] = {}
    borderline: list[str] = []
    tiebreak_choice: dict[str, str] = {}
    adopt: Literal["preset", "scores"] = "preset"
    completed_at: datetime | None = None


class BasePersonality(_Model):
    type: str | None = None  # 16タイプのID。custom で軸に 0.5 ちょうどを含む場合は null 可
    axes: Axes
    source: Literal["preset", "quiz", "custom"] = "preset"
    quiz: QuizRecord | None = None


class Profile(_Model):
    preset: str | None = None  # 名前つきプリセット(kuchibeta など)
    deltas: dict[str, float] = {}


class Adaptation(_Model):
    adaptability: Unit = Field(default=0.5, ge=0, le=1)
    adjustable: list[str] = list(ADJUSTABLE_CHOICES)
    fixed: list[str] = list(FIXED_CHOICES)


class Preference(_Model):
    topic: str = Field(min_length=1)
    affinity: Signed = Field(ge=-1, le=1)


class BaselineVad(_Model):
    v: Signed = Field(default=0.0, ge=-1, le=1)
    a: Signed = Field(default=0.0, ge=-1, le=1)
    d: Signed = Field(default=0.0, ge=-1, le=1)


class Temperament(_Model):
    baseline_vad: BaselineVad = BaselineVad()
    # 感情カテゴリの基準値(回帰の先)。省略した感情は 0(§7.1)
    baseline_categories: dict[str, float] = {}
    reactivity: Unit = Field(default=0.5, ge=0, le=1)
    recovery_rate: Unit = Field(default=0.3, ge=0, le=1)


class SilencePolicy(_Model):
    allow_anger_silence: bool = False
    # 絶対ルール R1: 0〜2 のみ。3 以上はエラー
    max_consecutive: int = Field(default=2, ge=0, le=2, strict=True)


class AppraisalRule(_Model):
    """話題ルール(§5.4)。その話題が出たとき、キャラクタの感情がどう動くか。"""

    topic: str = Field(min_length=1)
    match: Literal["semantic"] = "semantic"
    effects: dict[str, float]
    gain: Unit = Field(ge=0, le=1)


class EventTrigger(_Model):
    kind: Literal["act", "state"]
    name: str  # `state.events.EVENT_CATALOG` の名前(カタログにないものは、lint の E12)
    min_days: int | None = Field(default=None, ge=1, le=90)  # session_gap のみ


class Bounds(_Model):
    min: float | None = None
    max: float | None = None


class EventWhen(_Model):
    """イベントルールの条件。すべて AND。任意の式は書けない(予測しやすさと、検査のため)。"""

    relationship: Bounds | None = None  # 関係値の範囲(-20〜+20)
    target: Literal["character", "any"] | None = None
    companion: dict[str, Bounds] = {}  # 前のターンのキャラクタ感情の範囲
    min_act_intensity: float | None = Field(default=None, ge=0, le=1)
    session_turn: Literal["first", "any"] | None = None


class EventRule(_Model):
    """イベントルール(本体仕様書 §7.1 の④)。出来事(発話行為、久しぶりの会話)への、キャラクタの感情の反応。"""

    id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,40}$")
    trigger: EventTrigger
    when: EventWhen = EventWhen()
    effects: dict[str, float] = {}  # 8感情。各 -1〜+1
    vad_effects: dict[str, float] = {}  # v / a / d。各 -1〜+1
    gain: Unit = Field(ge=0, le=1)
    cooldown_turns: int = Field(default=0, ge=0, le=50)
    max_per_session: int = Field(default=5, ge=1, le=20)
    max_per_day: int | None = Field(default=None, ge=1, le=10)


Strength = Literal["weak", "medium", "strong"]


class LikedItem(_Model):
    """好きなもの・こと(好みカタログから選ぶ)。ポジティブな文脈のときだけ、喜び・期待・信頼が上がる。"""

    id: str
    strength: Strength = "medium"
    # with_character は、ユーザーとキャラクタの間の行為のときだけ反応する(関わり方の判定を使う項目だけ)
    scope: Literal["any", "with_character"] = "any"


class DislikedThing(_Model):
    """嫌いなもの(色・食べ物・イベント)。文脈によらず、選んだ感情が上がる。"""

    id: str
    emotions: list[str] = ["disgust"]  # fear / sadness / disgust / anger / surprise から 1〜3つ
    strength: Strength = "medium"
    repeat: Literal["habituate", "sensitize", "flat"] = "habituate"  # 慣れる / 敏感になる / 毎回同じ


class DislikedActivity(_Model):
    """嫌いなこと(生活・趣味・動作)。文脈によらず、毎回上がる。一定量で、話題を変えようとする。"""

    id: str
    emotion: Literal["disgust", "fear"] = "disgust"
    strength: Strength = "medium"
    avoid: bool = True


class LikedSheet(_Model):
    color: LikedItem | None = None
    food: LikedItem | None = None
    event: LikedItem | None = None
    life: LikedItem | None = None
    hobby: LikedItem | None = None
    action: LikedItem | None = None


class DislikedSheet(_Model):
    color: DislikedThing | None = None
    food: DislikedThing | None = None
    event: DislikedThing | None = None
    life: DislikedActivity | None = None
    hobby: DislikedActivity | None = None
    action: DislikedActivity | None = None


class LikesDislikes(_Model):
    """好き嫌いシート(デザイナ仕様書 §5.4.0)。区分ごとに1つ、好みカタログから選ぶ(null = なし)。"""

    catalog_version: int = 2
    liked: LikedSheet = LikedSheet()
    disliked: DislikedSheet = DislikedSheet()


class Review(_Model):
    reviewer: str | None = None
    reviewed_at: datetime | None = None
    notes: str | None = None


class PersonaDefinition(_Model):
    schema_version: Literal[1] = 1
    character_id: str = Field(pattern=CHARACTER_ID_PATTERN)
    version: str = "0.1.0"
    status: Literal["draft", "review", "published", "deprecated"] = "draft"
    name: str = Field(min_length=1)
    concept: dict[str, Any] = {}
    persona_prompt: str = Field(min_length=1, max_length=2000)
    speech_style: str = Field(min_length=1, max_length=1000)
    base_personality: BasePersonality
    profile: Profile = Profile()
    overrides: dict[str, float] = {}
    initial_relationship: float = Field(default=0.0, ge=-20, le=20)
    adaptation: Adaptation = Adaptation()
    preferences: list[Preference] = []
    temperament: Temperament = Temperament()
    silence_policy: SilencePolicy = SilencePolicy()
    appraisal_rules: list[AppraisalRule] = []
    event_rules: list[EventRule] = Field(default=[], max_length=30)
    likes_dislikes: LikesDislikes | None = None
    review: Review = Review()


def is_emotion_name(name: str) -> bool:
    return name in CATEGORY_NAMES
