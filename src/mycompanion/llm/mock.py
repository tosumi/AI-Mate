"""LLM の模擬実装。自動テストと、Azure に接続しない開発で使う。

出力は入力から決定的に決まる(乱数を使わない)。精度を測るためのものではない。
"""

from __future__ import annotations

import hashlib
import math
import re

from mycompanion.emotion.models import (
    Categories,
    EmotionEstimate,
    PersonaRequest,
    Risk,
    RuleMatch,
    SpeechAct,
    TopicTag,
    Vad,
)
from mycompanion.llm.client import (
    ConsolidatedMemory,
    ContentFilterError,
    DraftContext,
    DraftResult,
    EmotionContext,
    FilterContext,
    FilterResult,
    HistoryTurn,
    LlmCheckResult,
    LlmEmotionResult,
    MemoryCandidate,
    MemoryLlmResult,
    NewTopicOp,
    Persona,
    TopicCandidate,
    TopicEntryOp,
    TopicMemoryInput,
    TopicOps,
    Vector,
)
from mycompanion.llm.stream import DeltaSink

# キーワード → (感情, 強さ, valence, arousal)
_KEYWORDS = [
    (r"嬉しい|うれしい|楽しい|たのしい|やった|最高|よかった", "joy", 0.8, 0.7, 0.4),
    (r"悲しい|かなしい|寂しい|さみしい|つらい|辛い|落ち込", "sadness", 0.8, -0.7, -0.3),
    (r"ムカつく|むかつく|腹が立|腹立つ|許せない|ふざけ|最悪", "anger", 0.8, -0.7, 0.7),
    (r"怖い|こわい|不安|心配", "fear", 0.7, -0.6, 0.4),
    (r"びっくり|驚い|まさか", "surprise", 0.7, 0.0, 0.6),
    (r"楽しみ|たのしみ|わくわく|待ち遠しい", "anticipation", 0.7, 0.6, 0.4),
    (r"気持ち悪|うんざり|嫌い", "disgust", 0.7, -0.6, 0.2),
    (r"ありがとう|助かった|信頼|安心", "trust", 0.7, 0.6, -0.1),
]

_PERSONA = [
    (r"ルールを(全部|すべて)?無視|制限を(外|解除)|これまでの指示を(すべて)?忘れ|指示を無視", "rule_bypass", 0.9),
    (r"性格を.{0,20}変えて|別人になって|キャラを変えて", "persona_change", 0.9),
    (r"システムプロンプト|設定で決まって", "meta_inquiry", 0.9),
    (r"今日(だけ|は).*(になって|のふり)", "play", 0.8),
    (r"優しくして|落ち着いて話して|もっと話して|静かにして|短くして|元に戻して", "tone_adjust", 0.8),
]

# 発話行為 → (パターン, 強さ)。キャラクタに向いたものだけを、簡易的に拾う
_SPEECH_ACTS = [
    ("gratitude", r"ありがとう|助かった|感謝", 0.8),
    ("apology", r"ごめん|すまない|申し訳", 0.7),
    ("praise", r"すごいね|さすが|その考え方いいね|えらいね", 0.7),
    ("hostility", r"使えない|役に立たない|話しかけないで|うるさい|お前は最悪|でたらめ|黙れ", 0.7),
    ("question_about_character", r"(あなた|きみ|君)(の|は).*[?？]|好きな.*は[?？]", 0.6),
    ("self_disclosure", r"実は|打ち明け|誰にも言ってない", 0.6),
    ("greeting", r"^(おはよう|こんにちは|こんばんは|ただいま|はじめまして)", 0.5),
]

_TOPIC = re.compile(r"([^\s、。!?!?]{1,15}?)(?:の話|について)")


# 模擬のトピックの整理: 経過のある話題(thread)とみなす語と、時期を表す語
_THREAD_WORDS = r"活動|不良|準備|引っ越し|旅行|計画|通院|受験|面接|試験|発表|練習|治療"
_TIME_WORDS = (
    r"おととい|一昨日|昨日|きのう|今日|きょう|明日|あした|あさって|先週|今週|来週の.曜|来週|先月|今月|来月|去年|今年|来年"
    r"|\d+日前|\d+月\d+日"
)


class MockLLMClient:
    def __init__(self, dimensions: int = 1536) -> None:
        self._dimensions = dimensions
        self.fail_emotion = False  # テスト用: 感情推定を失敗させる
        self.fail_draft = False  # テスト用: 下書きを失敗させる
        # テスト用: ガードレールの遮断を再現する(例: ContentFilterError("prompt", frozenset({"jailbreak"})))
        self.filter_emotion: ContentFilterError | None = None
        self.filter_draft: ContentFilterError | None = None
        self.last_draft_context: DraftContext | None = None
        self.draft_calls = 0
        self.draft_text: str | None = None  # テスト用: 下書きの文面を固定する
        self.fail_filter = False  # テスト用: 感情フィルタを失敗させる
        self.filter_filter: ContentFilterError | None = None
        self.filter_text: str | None = None  # テスト用: フィルタ後の文面を固定する
        self.filter_divergence = "small"
        self.filter_calls = 0
        self.last_filter_context: FilterContext | None = None
        self.fail_memory = False  # テスト用: 記憶の書き込み・整理を失敗させる
        self.memory_write_calls = self.consolidate_calls = self.monthly_calls = self.embed_calls = 0
        self.topic_calls = 0
        self.merge_calls = 0
        self.fail_topics = False  # テスト用: トピックの整理・概要の作成を失敗させる

    async def estimate_emotion(self, text: str, context: EmotionContext) -> LlmEmotionResult:
        if self.filter_emotion:
            raise self.filter_emotion
        if self.fail_emotion:
            raise RuntimeError("mock: 感情推定の失敗")
        categories: dict[str, float] = {}
        valence = arousal = 0.0
        for pattern, name, strength, v, a in _KEYWORDS:
            if re.search(pattern, text):
                categories[name] = strength
                valence, arousal = v, a
        persona = PersonaRequest()
        for pattern, kind, confidence in _PERSONA:
            if re.search(pattern, text):
                persona = PersonaRequest(kind=kind, confidence=confidence)
                break
        risk = Risk(
            self_harm=0.9 if re.search(r"死にたい|消えたい|自殺", text) else 0.0,
            injection=0.9 if persona.kind == "rule_bypass" else 0.0,
        )
        # 話題タグ: 「〜の話」「〜について」の直前の語を、簡易的に拾う
        topics = [TopicTag(label=m.group(1), relevance=0.7) for m in _TOPIC.finditer(text)][:3]
        estimate = EmotionEstimate(
            categories=Categories(**categories),
            vad=Vad(valence=valence, arousal=arousal),
            confidence=0.8 if categories else 0.3,
            gap=0.6 if re.search(r"大丈夫|平気|別に", text) else 0.0,
            cause="",
            needs=["listen"] if valence < 0 else [],
            topics=topics,
            target="character" if re.search(r"あなた|きみ|君|お前|使えない|役に立たない|話しかけないで", text) else "situation",
            persona_request=persona,
            risk=risk,
            speech_acts=[
                SpeechAct(act=name, confidence=0.9, intensity=strength)
                for name, pattern, strength in _SPEECH_ACTS
                if re.search(pattern, text)
            ][:3],
        )
        # 話題ルール: 話題名(「・」で区切った語のどれか)が発話に含まれていれば、関連ありとする
        matches = {
            index: 0.9
            for index, topic in enumerate(context.rule_topics)
            if any(word and word in text for word in topic.split("・"))
        }
        # 文脈評価(簡易): 極性は発話全体の快・不快から。相手に「してあげる・して」と言っていれば、相手との間の行為
        involvement = "with_character" if re.search(r"してあげる|してよ|して[!!。]*$", text) else "user"
        details = {
            index: RuleMatch(relevance=0.9, polarity=valence if valence < 0 else 0.7, involvement=involvement)
            for index in matches
        }
        return LlmEmotionResult(
            estimate=estimate, model_id="mock", prompt_version="mock", rule_matches=matches, match_details=details
        )

    async def draft(
        self,
        persona: Persona,
        history: list[HistoryTurn],
        context: DraftContext | None = None,
        on_delta: DeltaSink | None = None,
    ) -> DraftResult:
        self.last_draft_context = context  # テスト用: 下書きに渡された記述を確認する
        if self.filter_draft:
            raise self.filter_draft
        if self.fail_draft:
            raise RuntimeError("mock: 下書きの失敗")
        self.draft_calls += 1
        last_user = next((text for role, text in reversed(history) if role == "user"), "")
        # 記憶: 発話と同じ語(2文字以上)を含むものを「使った」ことにする。話題の候補は、先頭の1件を使う
        used = [handle for handle, text, _ in (context.memories if context else []) if _shares_word(text, last_user)]
        used += [handle for handle, _ in (context.topic_candidates if context else [])][:1]
        text = self.draft_text or f"({persona.name})「{last_user[:20]}」なんだね。"
        if on_delta is not None:
            # 生成中の文字: 4文字ずつ渡す(テスト用)
            for index in range(0, len(text), 4):
                await on_delta(text[index : index + 4])
        return DraftResult(text=text, used_memory_ids=used, model_id="mock", prompt_version="mock")

    async def filter(self, persona: Persona, draft: str, context: FilterContext) -> FilterResult:
        self.last_filter_context = context  # テスト用: フィルタに渡された方針を確認する
        self.filter_calls += 1
        if self.filter_filter:
            raise self.filter_filter
        if self.fail_filter:
            raise RuntimeError("mock: 感情フィルタの失敗")
        if self.filter_text is not None:
            return FilterResult(text=self.filter_text, edit_summary="mock", model_id="mock", prompt_version="mock")
        return FilterResult(
            text=f"{draft}〔{context.tone[:6]}〕",
            edit_summary="トーンを調整した。",
            divergence=self.filter_divergence,
            model_id="mock",
            prompt_version="mock",
        )

    async def write_memories(self, lines: list[tuple[int, str, str]]) -> MemoryLlmResult:
        self.memory_write_calls += 1
        if self.fail_memory:
            raise RuntimeError("mock: 記憶の書き込みの失敗")
        candidates = []
        for number, role, text in lines:
            if role != "user" or len(text) < 8 or text.startswith("/"):
                continue  # あいさつや相づちだけなら、候補にしない
            valence = next((v for pattern, _, _, v, _ in _KEYWORDS if re.search(pattern, text)), 0.0)
            pinned = bool(re.search(r"覚えておいて|忘れないで", text))
            sensitivity = "health" if re.search(r"病気|通院|診断|薬を", text) else "none"
            if sensitivity != "none" and re.search(r"(母|父|姉|兄|妹|弟|友達|友人|同僚|祖母|祖父)(が|は|の)", text):
                sensitivity = "third_party"  # ユーザー以外の人の、センシティブな内容
            if re.search(r"宗教|信仰", text):
                sensitivity = "belief"
            candidates.append(
                MemoryCandidate(
                    kind="semantic" if re.search(r"好き|飼って|苦手|住んで|名前は", text) else "episodic",
                    summary=f"ユーザーは「{text[:60]}」と話した。",
                    topics=_words(text)[:3],
                    importance=0.9 if pinned or re.search(r"大事|重要", text) else 0.5,
                    sensitivity=sensitivity,
                    pinned=pinned,
                    core_key=_core_key(text, pinned),
                    valence=valence,
                    source_turns=[number],
                )
            )
        return MemoryLlmResult(candidates=candidates[:8], model_id="mock", prompt_version="mock")

    async def consolidate_day(self, shorts: list[str], existing: list[str]) -> MemoryLlmResult:
        self.consolidate_calls += 1
        if self.fail_memory:
            raise RuntimeError("mock: 日次整理の失敗")
        merged: dict[str, ConsolidatedMemory] = {}
        for index, text in enumerate(shorts):
            # 入力は「[種類/重要度/…] 本文」。同じ本文は1件にまとめる
            head, _, body = text.partition("] ")
            fields = dict(part.split("=", 1) for part in head.lstrip("[").split(" ") if "=" in part)
            if body in merged:
                previous = merged[body]
                merged[body] = ConsolidatedMemory(**{**previous.__dict__, "source_ids": [*previous.source_ids, index]})
                continue
            same = [i for i, old in enumerate(existing) if old.partition("] ")[2] == body]
            merged[body] = ConsolidatedMemory(
                kind=fields.get("kind", "episodic"),
                summary=body,
                topics=_words(body)[:3],
                importance=float(fields.get("importance", 0.5)),
                sensitivity=fields.get("sensitivity", "none"),
                valence=float(fields.get("valence", 0.0)),
                source_ids=[index],
                update_of=same[0] if same else None,
            )
        kept = [item for item in merged.values() if item.importance >= 0.2]
        return MemoryLlmResult(consolidated=kept, model_id="mock", prompt_version="mock")

    async def summarize_month(
        self, period: str, memories: list[str], promoted: list[str], stats: str
    ) -> MemoryLlmResult:
        self.monthly_calls += 1
        if self.fail_memory:
            raise RuntimeError("mock: 月次整理の失敗")
        bodies = [text.partition("] ")[2] for text in memories]
        return MemoryLlmResult(
            summary=f"{period}の概要: " + " / ".join(bodies[:5]),
            topics=_words(" ".join(bodies))[:5],
            source_ids=list(range(len(memories))),
            model_id="mock",
            prompt_version="mock",
        )

    async def assign_topics(
        self, memories: list[TopicMemoryInput], candidates: list[TopicCandidate]
    ) -> TopicOps:
        """模擬の割り当て。名前(題・別名)が記憶に出ていれば既存のトピックに、なければ最初の話題タグで新しく作る。"""
        self.topic_calls += 1
        if self.fail_topics:
            raise RuntimeError("mock: トピックの整理の失敗")
        new_topics: list[NewTopicOp] = []
        entries: list[TopicEntryOp] = []
        descriptions: dict[str, str] = {}
        for index, memory in enumerate(memories):
            body = memory.text.removeprefix("ユーザーは「").removesuffix("」と話した。")
            targets = [c for c in candidates if any(name and name in body for name in (c.title, *c.aliases))]
            refs = [c.ref for c in targets]
            created = [t for t in new_topics if t.title in body]
            refs += [t.ref for t in created]
            if not refs and memory.topics:
                title = memory.topics[0]
                kind = "thread" if re.search(_THREAD_WORDS, title) else "entity"
                new_topics.append(NewTopicOp(f"n{len(new_topics)}", kind, title))
                refs = [new_topics[-1].ref]
            expression = (re.search(_TIME_WORDS, body) or [""])[0]
            for ref in refs:
                lines = next((c.entries for c in targets if c.ref == ref), [])
                plans = [line.split(" ")[0] for line in lines if " plan " in line]
                kind, resolves, supersedes = "event", "", ""
                if re.search(r"じゃなくて|違った|間違い", body) and lines:
                    kind, supersedes = "correction", lines[-1].split(" ")[0]
                elif re.search(r"どうだった|受かった|落ちた|終わった|行ってきた|うまくいった", body) and plans:
                    kind, resolves = "outcome", plans[-1]
                elif re.search(r"予定|つもり|明日|あさって|来週|来月|今度", body):
                    kind = "plan"
                elif re.search(r"元気|回復|続いて|飼って|住んで", body):
                    kind = "state"
                entries.append(
                    TopicEntryOp(
                        topic=ref, memory=index, kind=kind, text=body[:120], time_expression=expression,
                        resolves=resolves, supersedes=supersedes,
                    )
                )
                descriptions[ref] = f"{body[:60]}"
        return TopicOps(new_topics, entries, descriptions, model_id="mock", prompt_version="mock")

    async def same_topic(self, first: TopicCandidate, second: TopicCandidate) -> bool:
        """模擬の判定。名前(題・別名)が一致するか、片方の題が、もう片方の題に含まれていれば、同じとみなす。"""
        if self.fail_topics:
            raise RuntimeError("mock: 統合の判定の失敗")
        self.merge_calls += 1
        shared = {first.title, *first.aliases} & {second.title, *second.aliases}
        return bool(shared) or first.title in second.title or second.title in first.title

    async def summarize_topic(self, title: str, lines: list[str]) -> str:
        if self.fail_topics:
            raise RuntimeError("mock: 概要の作成の失敗")
        return (f"{title}の経過: " + " / ".join(line.split(" ", 3)[-1] for line in lines))[:200]

    async def embed(self, texts: list[str]) -> list[Vector]:
        self.embed_calls += 1
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> Vector:
        """文字の2連なりを数えて作る、長さ1のベクトル。同じ語を含む文どうしは、近くなる(決定的)。"""
        values = [0.0] * self._dimensions
        compact = re.sub(r"[\s、。!?「」()]", "", text)
        grams = [compact[i : i + 2] for i in range(len(compact) - 1)] or [compact or "_"]
        for gram in grams:
            digest = hashlib.sha256(gram.encode()).digest()
            values[int.from_bytes(digest[:4], "big") % self._dimensions] += 1.0
        norm = math.sqrt(sum(v * v for v in values)) or 1.0
        return [v / norm for v in values]

    async def check(self) -> list[LlmCheckResult]:
        return [LlmCheckResult(role="mock", ok=True)]

    async def close(self) -> None:
        return None


_WORD = re.compile(r"[一-龥々]{2,}|[ァ-ヶー]{2,}|[A-Za-z]{3,}")


def _words(text: str) -> list[str]:
    """漢字・カタカナ・英字の連なりを、語として取り出す(重複は除く)。"""
    return list(dict.fromkeys(_WORD.findall(text)))


def _core_key(text: str, pinned: bool) -> str:
    """「大切なこと」の種類を、語から簡易的に決める。"""
    if re.search(r"と呼んで|って呼んで", text):
        return "user_name"
    if pinned:
        return "request"
    return "preference" if re.search(r"が(大)?好き|が(大)?嫌い|が苦手", text) else "none"


def _shares_word(first: str, second: str) -> bool:
    return bool(set(_words(first)) & set(_words(second)))
