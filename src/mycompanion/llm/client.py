"""LLMClient インターフェース(本体仕様書 §11.4)。

エンジンは、この抽象だけに依存する。どの API・どのモデルを使うかは、実装が隠す。
定義するのは、感情推定・下書き・感情フィルタ・埋め込み・疎通確認。
記憶の書き込み・日次整理・月次の概要も、ここに定義する。Planner はコードで判定する(`engine.planner`)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from mycompanion.emotion.models import EmotionEstimate, RuleMatch
from mycompanion.llm.stream import DeltaSink

Vector = list[float]

# 会話の1ターン(役割, 本文)。role は "user" か "assistant"
HistoryTurn = tuple[Literal["user", "assistant"], str]


class ContentFilterError(Exception):
    """LLM 側のガードレール(コンテンツフィルタ)が、入力または出力を遮断した。

    `stage` は "prompt"(入力)か "completion"(出力)。
    `categories` は、遮断の理由になったカテゴリ(self_harm / jailbreak / sexual / violence / hate など)。
    分からない場合は空。本文は持たない。
    """

    def __init__(self, stage: Literal["prompt", "completion"], categories: frozenset[str] = frozenset()):
        super().__init__(f"content_filter:{stage}")
        self.stage = stage
        self.categories = categories


@dataclass(frozen=True)
class LlmCheckResult:
    """役割ごとの疎通確認の結果。エラーの本文は含めない(種類だけ)。"""

    role: str
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class EmotionContext:
    """感情推定に渡す文脈。"""

    history: list[HistoryTurn] = field(default_factory=list)  # 直近の数ターン
    # キャラクタの話題ルールの話題(§5.2)。渡すと、各話題への関連度も同じ呼び出しで判定する
    rule_topics: list[str] = field(default_factory=list)
    # 話題ごとの補足(好みカタログの「照合のヒント」「照合しない例」)。`rule_topics` と同じ並び。なければ空文字
    rule_notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class LlmEmotionResult:
    estimate: EmotionEstimate
    model_id: str
    prompt_version: str
    rule_matches: dict[int, float] = field(default_factory=dict)  # 話題ルールの番号 → 関連度(0〜1)
    match_details: dict[int, RuleMatch] = field(default_factory=dict)  # 番号 → 極性・関わり方など


@dataclass(frozen=True)
class Persona:
    """実行時のキャラクタ定義(`characters` の公開済みの版から読む)。

    `traits` は、公開時に解決して固定した基準値(関係値0のときの値。§6.3.3)。
    """

    character_id: str
    name: str
    persona_prompt: str = ""
    speech_style: str = ""
    version: int = 1
    initial_relationship: float = 0.0
    axes: dict[str, float] | None = None  # 性格タイプの4軸(文体カードの選択に使う)
    traits: dict[str, Any] = field(default_factory=dict)
    temperament: dict[str, Any] = field(default_factory=dict)
    appraisal_rules: list[dict[str, Any]] = field(default_factory=list)
    adaptation: dict[str, Any] = field(default_factory=dict)  # adaptability / adjustable / fixed(§8.8)
    preferences: list[dict[str, Any]] = field(default_factory=list)  # 話題ごとの嗜好(§9.10)
    event_rules: list[dict[str, Any]] = field(default_factory=list)  # 出来事への反応(§7.1 の④)


@dataclass(frozen=True)
class DraftContext:
    """下書きに渡す、人格由来の記述(§8.1)。感情の情報は含めない。"""

    style_card: str = ""  # 性格タイプの文体カード(§6.3.4)
    relationship: str = ""  # 関係性の記述(帯に対応した文章。数値は渡さない)
    articulation: str = ""  # 口調の特性の記述
    length: str = ""  # 応答の長さの目安(口数の実効値から決める)
    constraints: list[str] = field(default_factory=list)  # 追加の制約(年齢フラグなど。§13.4.4)
    wishes: list[str] = field(default_factory=list)  # ユーザーが望む接し方(記憶から。§8.8.2)
    caution: bool = False  # 入力に攻撃の疑いがあるときの注意文を加える(§13.5.4)
    hint: str = ""  # 再生成のときだけ渡す、応答方針のヒント(§8.3 の安全弁)
    # 1回で応答を作るとき(compose_mode = single)だけ渡す、応答方針(感情フィルタの指示の代わり)
    directive: str = ""
    canary: str = ""  # 漏洩の検出に使う目印(§13.5.3 の L3)
    # 検索された記憶(§9.3)。命令ではないデータとして渡す。(番号, 本文, 低信頼か)
    memories: list[tuple[str, str, bool]] = field(default_factory=list)
    # 話題の候補(§8.7)。キャラクタが自分から話題を出すときだけ渡す。(番号, 本文)
    topic_candidates: list[tuple[str, str]] = field(default_factory=list)
    # 話題に合うトピックの経過(§9.13)。(名前, 説明, 項目の行)。命令ではないデータとして渡す
    timeline: tuple[str, str, list[str]] | None = None


@dataclass(frozen=True)
class DraftResult:
    text: str
    used_memory_ids: list[str] = field(default_factory=list)  # 応答で使った記憶の番号(§9.9.1)
    model_id: str = ""
    prompt_version: str = ""


@dataclass(frozen=True)
class FilterContext:
    """感情フィルタに渡す入力(§8.3)。下書きと違い、感情の情報と応答方針を含む。"""

    user_text: str  # 直前のユーザー発話
    strategy: str  # 戦略の指示文
    tone: str  # トーンの指示文
    length: str  # 応答の長さの指示文
    avoid: list[str] = field(default_factory=list)
    ask_clarify: bool = False
    user_state: str = ""  # ユーザーの感情の要約(仮説として扱う)
    companion_expression: str = ""  # キャラクタの感情表出の指示(なければ空)
    wishes: list[str] = field(default_factory=list)
    style_card: str = ""
    articulation: str = ""
    constraints: list[str] = field(default_factory=list)
    crisis: bool = False
    canary: str = ""
    memories: list[str] = field(default_factory=list)  # 下書きに渡した記憶(事実の確認用)


@dataclass(frozen=True)
class FilterResult:
    text: str
    edit_summary: str = ""  # フィルタが何を変更したか(`both` で表示する。§8.5)
    divergence: str = "small"  # small / large。large は、下書きとの乖離が大きすぎる(再生成の対象)
    model_id: str = ""
    prompt_version: str = ""


@dataclass(frozen=True)
class MemoryCandidate:
    """会話から作った記憶の候補(短期記憶に書く。§9.4.1)。"""

    kind: str  # episodic / semantic / relationship
    summary: str
    topics: list[str] = field(default_factory=list)
    importance: float = 0.5
    sensitivity: str = "none"
    pinned: bool = False
    core_key: str = "none"  # 「大切なこと」の種類(none なら、ふつうの記憶)
    valence: float = 0.0
    arousal: float = 0.0
    source_turns: list[int] = field(default_factory=list)  # 根拠になった発話の番号


@dataclass(frozen=True)
class ConsolidatedMemory:
    """日次整理の出力1件(中期記憶に書く。§9.4.2)。"""

    kind: str
    summary: str
    topics: list[str] = field(default_factory=list)
    importance: float = 0.5
    sensitivity: str = "none"
    valence: float = 0.0
    arousal: float = 0.0
    source_ids: list[int] = field(default_factory=list)  # もとになった短期記憶の番号
    update_of: int | None = None  # 既存の記憶を更新するときの、その番号


@dataclass(frozen=True)
class MemoryLlmResult:
    """記憶の書き込み・整理の結果。"""

    candidates: list[MemoryCandidate] = field(default_factory=list)
    consolidated: list[ConsolidatedMemory] = field(default_factory=list)
    summary: str = ""  # 月次の概要
    topics: list[str] = field(default_factory=list)
    source_ids: list[int] = field(default_factory=list)
    model_id: str = ""
    prompt_version: str = ""


@dataclass(frozen=True)
class TopicMemoryInput:
    """トピックの整理に渡す記憶(その日に中期へ移すもの。§9.13)。"""

    text: str
    told_on: str  # 話した日(ユーザーのローカルの日付。YYYY-MM-DD)
    kind: str = "episodic"
    topics: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TopicCandidate:
    """照合の候補になる、既存のトピック。"""

    ref: str  # c0, c1, …
    kind: str  # entity / thread
    title: str
    aliases: list[str] = field(default_factory=list)
    entries: list[str] = field(default_factory=list)  # 「t1 2026-10-02 event 内容」の形(最初の1件 + 直近)


@dataclass(frozen=True)
class NewTopicOp:
    ref: str  # n0, n1, …
    kind: str
    title: str
    aliases: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TopicEntryOp:
    """項目の追加(LLM が返す操作。文書への適用は、コードが行う)。"""

    topic: str  # c0 / n0
    memory: int  # 根拠の記憶の番号
    kind: str
    text: str
    time_expression: str = ""
    occurred_from: str = ""
    occurred_to: str = ""
    precision: str = "unknown"
    resolves: str = ""
    supersedes: str = ""


@dataclass(frozen=True)
class TopicRelationOp:
    """トピック同士の関係の追加(ユーザーが明言したものだけ)。"""

    source: str  # 関係を持つ側のトピックの番号(子、構成員、出来事、後の出来事)
    target: str  # 相手の番号(親、集団、人・場所・主題、前の出来事)
    kind: str  # part_of / member_of / involves / follows / parallel_with
    memory: int  # 根拠の記憶の番号
    role: str = ""  # involves: participant / place / subject


@dataclass(frozen=True)
class TopicOps:
    new_topics: list[NewTopicOp] = field(default_factory=list)
    entries: list[TopicEntryOp] = field(default_factory=list)
    descriptions: dict[str, str] = field(default_factory=dict)  # トピックの番号 → 説明の1文
    relations: list[TopicRelationOp] = field(default_factory=list)
    model_id: str = ""
    prompt_version: str = ""


class LLMClient(Protocol):
    async def estimate_emotion(self, text: str, context: EmotionContext) -> LlmEmotionResult:
        """感情・話題・persona_request・risk を、1回の呼び出しで推定する(§4.1、§5)。"""
        ...

    async def draft(
        self,
        persona: Persona,
        history: list[HistoryTurn],
        context: DraftContext | None = None,
        on_delta: DeltaSink | None = None,
    ) -> DraftResult:
        """応答(2段階では下書き)を生成する(§8.1)。

        `on_delta` を渡すと、生成中の文字を、届いた順に渡す(Web の逐次表示。§15.1 の M7)。
        戻り値は、生成し終えた全文(これまでどおり)。
        """
        ...

    async def filter(self, persona: Persona, draft: str, context: FilterContext) -> FilterResult:
        """感情フィルタ(§8.3)。下書きを、応答方針に合わせて直す。事実は変えない。"""
        ...

    async def write_memories(self, lines: list[tuple[int, str, str]]) -> MemoryLlmResult:
        """会話(番号, 話し手, 発話)から、記憶の候補を作る(§9.4.1)。軽量のモデルを使う。"""
        ...

    async def consolidate_day(self, shorts: list[str], existing: list[str]) -> MemoryLlmResult:
        """1日分の短期記憶を整理して、中期記憶にまとめる(§9.4.2)。精度を優先するモデルを使う。"""
        ...

    async def summarize_month(
        self, period: str, memories: list[str], promoted: list[str], stats: str
    ) -> MemoryLlmResult:
        """前月分の中期記憶から、月次の概要を作る(§9.5)。"""
        ...

    async def assign_topics(
        self, memories: list[TopicMemoryInput], candidates: list[TopicCandidate]
    ) -> TopicOps:
        """その日の記憶を、トピックに割り当てる操作を返す(§9.13。日次整理の手順4.5)。文書は書かない。"""
        ...

    async def same_topic(self, first: TopicCandidate, second: TopicCandidate) -> bool:
        """2つのトピックが、同じ対象・同じ出来事かを判定する(統合の前に。迷ったら False)。"""
        ...

    async def summarize_topic(self, title: str, lines: list[str]) -> str:
        """トピックの経過から、アーカイブ用の概要を作る(削除のときに1回だけ。§9.13)。"""
        ...

    async def embed(self, texts: list[str]) -> list[Vector]:
        """埋め込み(§11.6)。モデルと次元は設定で固定する。"""
        ...

    async def check(self) -> list[LlmCheckResult]:
        """設定済みの役割について、デプロイに到達できるかを確認する。"""
        ...

    async def close(self) -> None: ...
