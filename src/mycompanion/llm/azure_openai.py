"""Azure OpenAI を使う LLMClient の実装(本体仕様書 §11.1)。

- 認証は Entra ID(マネージド ID、またはローカルの `az login`)。API キーは使わない。
- 役割ごとのデプロイ名は環境変数で、モデル名・次元は設定で持つ(§11.5.9)。
- チャット系は Responses API を基本とする。
- 会話の内容を API 側に保存させない(`store=False`)。
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from mycompanion import telemetry
from mycompanion.config import LLM_ROLES, AzureOpenAIConfig, EngineSettings
from mycompanion.emotion.models import EmotionEstimate, RuleMatch
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
    TopicRelationOp,
    Vector,
)
from mycompanion.llm.prompts import draft as draft_prompt
from mycompanion.llm.prompts import emotion as emotion_prompt
from mycompanion.llm.prompts import filter as filter_prompt
from mycompanion.llm.prompts import memory as memory_prompt
from mycompanion.llm.prompts import topics as topic_prompt
from mycompanion.llm.stream import DeltaSink, JsonTextExtractor

AZURE_OPENAI_SCOPE = "https://cognitiveservices.azure.com/.default"


# 1回の呼び出しの時間切れと、再試行の回数(M11 の手順2: 障害時の動作)
LLM_TIMEOUT = httpx.Timeout(120.0, connect=10.0)
LLM_MAX_RETRIES = 2


class AzureOpenAILLMClient:
    def __init__(
        self, config: AzureOpenAIConfig, settings: EngineSettings, client: Any | None = None
    ) -> None:
        self._config = config
        self._settings = settings
        self._credential: Any | None = None
        self._client = client or self._build_client()

    def _build_client(self) -> Any:
        from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider
        from openai import AsyncOpenAI

        self._credential = DefaultAzureCredential()
        token_provider = get_bearer_token_provider(self._credential, AZURE_OPENAI_SCOPE)
        return AsyncOpenAI(
            base_url=f"{self._config.endpoint.rstrip('/')}/openai/v1/",
            api_key=token_provider,
            # SDK の既定(時間切れ 10分)では、容量不足・障害のときに利用者を長く待たせる。
            # 時間切れは 2分(接続は 10秒)、再試行は 2回(429・5xx。待ち時間の指示に従う)。それでも駄目なら、
            # エンジンが「いま応答を作れませんでした」(llm_unavailable。503)にする(本体仕様書 §11.2、§15.1 の M11 の手順2)
            timeout=LLM_TIMEOUT,
            max_retries=LLM_MAX_RETRIES,
        )

    async def estimate_emotion(self, text: str, context: EmotionContext) -> LlmEmotionResult:
        deployment = self._config.deployment("emotion")
        response = await self._respond(
            model=deployment,
            instructions=emotion_prompt.SYSTEM_PROMPT,
            input=emotion_prompt.user_prompt(
                text, list(context.history), list(context.rule_topics), list(context.rule_notes)
            ),
            # 応答経路なので、推論の量を抑えて遅延を小さくする(§11.1)
            reasoning={"effort": "low"},
            text={
                "format": {
                    "type": "json_schema",
                    "name": "emotion_estimate",
                    "schema": emotion_prompt.OUTPUT_SCHEMA,
                    "strict": True,
                }
            },
            max_output_tokens=1500,
            store=False,
        )
        data = json.loads(response.output_text)
        data["topics"] = _clean_topics(data.get("topics", []))
        data["needs"] = list(dict.fromkeys(data.get("needs", [])))[:2]
        # 発話行為: 同じ行為は1件にまとめ、確信度の高い順に3件まで
        acts: dict[str, dict[str, Any]] = {}
        for item in sorted(data.get("speech_acts", []), key=lambda a: -float(a.get("confidence", 0.0))):
            acts.setdefault(str(item.get("act")), item)
        data["speech_acts"] = list(acts.values())[:3]
        raw_matches = data.pop("rule_matches", [])
        estimate = EmotionEstimate.model_validate(data)
        # 範囲外の番号は捨てる(モデルの出力を、そのまま信用しない)
        details: dict[int, RuleMatch] = {}
        for m in raw_matches:
            index = int(m["index"])
            if not 0 <= index < len(context.rule_topics):
                continue
            involvement = m.get("involvement")
            details[index] = RuleMatch(
                relevance=max(0.0, min(1.0, float(m["relevance"]))),
                polarity=max(-1.0, min(1.0, float(m.get("polarity", 0.0)))),
                involvement=involvement if involvement in ("with_character", "user", "third_party") else "generic",
                person_attribute_ref=bool(m.get("person_attribute_ref", False)),
                idiom=bool(m.get("idiom", False)),
            )
        matches = {index: detail.relevance for index, detail in details.items()}
        return LlmEmotionResult(
            estimate=estimate,
            model_id=self._config.model_id("emotion"),
            prompt_version=emotion_prompt.PROMPT_VERSION,
            rule_matches=matches,
            match_details=details,
        )

    async def draft(
        self,
        persona: Persona,
        history: list[HistoryTurn],
        context: DraftContext | None = None,
        on_delta: DeltaSink | None = None,
    ) -> DraftResult:
        deployment = self._config.deployment("draft")
        context = context or DraftContext()
        structured = bool(context.memories or context.topic_candidates)
        extra: dict[str, Any] = {}
        if structured:
            # 記憶を渡したときだけ、使った記憶の番号も返させる(想起の記録に使う。§9.9.1)
            extra["text"] = {
                "format": {"type": "json_schema", "name": "reply", "schema": draft_prompt.OUTPUT_SCHEMA, "strict": True}
            }
        request = dict(
            model=deployment,
            instructions=draft_prompt.system_prompt(
                persona.name,
                persona.persona_prompt,
                persona.speech_style,
                style_card=context.style_card,
                relationship=context.relationship,
                articulation=context.articulation,
                length=context.length,
                constraints=list(context.constraints),
                wishes=list(context.wishes),
                caution=context.caution,
                hint=context.hint,
                canary=context.canary,
                memories=list(context.memories),
                topic_candidates=list(context.topic_candidates),
                timeline=context.timeline,
                directive=context.directive,
            )
            + (draft_prompt.OUTPUT_RULES if structured else ""),
            input=[{"role": role, "content": content} for role, content in history],
            reasoning={"effort": "low"},
            max_output_tokens=1200,
            store=False,
            **extra,
        )
        if on_delta is None:
            response = await self._respond(**request)
        else:
            # 生成中の文字を渡す。記憶を渡したとき(JSON)は、text の値だけを取り出す
            extractor = JsonTextExtractor() if structured else None

            async def forward(chunk: str) -> None:
                piece = extractor.feed(chunk) if extractor is not None else chunk
                if piece:
                    await on_delta(piece)

            response = await self._respond_stream(forward, **request)
        text = (response.output_text or "").strip()
        used: list[str] = []
        if structured:
            data = json.loads(text)
            text = str(data.get("text", "")).strip()
            # 渡していない番号は捨てる(モデルの出力を、そのまま信用しない)
            known = {handle for handle, *_ in context.memories} | {handle for handle, _ in context.topic_candidates}
            used = [str(h) for h in data.get("used_memory_ids", []) if str(h) in known]
        if not text:
            raise RuntimeError("下書きが空でした")
        return DraftResult(
            text=text,
            used_memory_ids=used,
            model_id=self._config.model_id("draft"),
            prompt_version=draft_prompt.PROMPT_VERSION + (f"+{draft_prompt.SINGLE_VERSION}" if context.directive else ""),
        )

    async def filter(self, persona: Persona, draft: str, context: FilterContext) -> FilterResult:
        deployment = self._config.deployment("filter")
        response = await self._respond(
            model=deployment,
            instructions=filter_prompt.system_prompt(
                persona.name,
                persona.persona_prompt,
                persona.speech_style,
                style_card=context.style_card,
                articulation=context.articulation,
                constraints=list(context.constraints),
                canary=context.canary,
            ),
            input=filter_prompt.user_prompt(
                draft,
                context.user_text,
                strategy=context.strategy,
                tone=context.tone,
                length=context.length,
                avoid=list(context.avoid),
                ask_clarify=context.ask_clarify,
                user_state=context.user_state,
                companion_expression=context.companion_expression,
                wishes=list(context.wishes),
                crisis=context.crisis,
                memories=list(context.memories),
            ),
            reasoning={"effort": "low"},
            text={
                "format": {
                    "type": "json_schema",
                    "name": "filtered_reply",
                    "schema": filter_prompt.OUTPUT_SCHEMA,
                    "strict": True,
                }
            },
            max_output_tokens=1500,
            store=False,
        )
        data = json.loads(response.output_text)
        text = str(data.get("text", "")).strip()
        if not text:
            raise RuntimeError("感情フィルタの出力が空でした")
        return FilterResult(
            text=text,
            edit_summary=str(data.get("edit_summary", "")).strip(),
            divergence="large" if data.get("divergence") == "large" else "small",
            model_id=self._config.model_id("filter"),
            prompt_version=filter_prompt.PROMPT_VERSION,
        )

    async def _memory_call(
        self, role: str, system: str, user: str, schema: dict[str, Any], name: str, version: str
    ) -> tuple[dict[str, Any], dict[str, str]]:
        deployment = self._config.deployment(role)  # type: ignore[arg-type]
        response = await self._respond(
            model=deployment,
            instructions=system,
            input=user,
            reasoning={"effort": "low"},
            text={"format": {"type": "json_schema", "name": name, "schema": schema, "strict": True}},
            max_output_tokens=4000,
            store=False,
        )
        meta = {"model_id": self._config.model_id(role), "prompt_version": version}  # type: ignore[arg-type]
        return json.loads(response.output_text), meta

    async def write_memories(self, lines: list[tuple[int, str, str]]) -> MemoryLlmResult:
        data, meta = await self._memory_call(
            "memory_write",
            memory_prompt.WRITE_SYSTEM,
            memory_prompt.conversation_block(lines),
            memory_prompt.WRITE_SCHEMA,
            "memory_candidates",
            memory_prompt.WRITE_VERSION,
        )
        numbers = {number for number, _, _ in lines}
        candidates = []
        for item in data.get("memories", [])[:8]:
            sources = [int(n) for n in item.get("source_turns", []) if int(n) in numbers]
            summary = str(item.get("summary", "")).strip()
            if not summary or not sources:
                continue  # 根拠のない候補は捨てる(事実の創作を防ぐ。§9.8)
            candidates.append(
                MemoryCandidate(
                    kind=item["kind"],
                    summary=summary[:400],
                    topics=_clean_labels(item.get("topics", [])),
                    importance=_unit(item.get("importance", 0.5)),
                    sensitivity=item.get("sensitivity", "none"),
                    pinned=bool(item.get("pinned", False)),
                    core_key=str(item.get("core", "none")),
                    valence=_signed(item.get("valence", 0.0)),
                    arousal=_signed(item.get("arousal", 0.0)),
                    source_turns=sources,
                )
            )
        return MemoryLlmResult(candidates=candidates, **meta)

    async def consolidate_day(self, shorts: list[str], existing: list[str]) -> MemoryLlmResult:
        user = memory_prompt.numbered_block("short", shorts) + "\n" + memory_prompt.numbered_block("existing", existing)
        data, meta = await self._memory_call(
            "consolidation",
            memory_prompt.DAILY_SYSTEM,
            user,
            memory_prompt.DAILY_SCHEMA,
            "daily_consolidation",
            memory_prompt.DAILY_VERSION,
        )
        items = []
        for item in data.get("memories", []):
            sources = [int(n) for n in item.get("source_ids", []) if 0 <= int(n) < len(shorts)]
            summary = str(item.get("summary", "")).strip()
            if not summary or not sources:
                continue
            update = str(item.get("update_of", "")).strip()
            update_of = int(update) if update.isdigit() and int(update) < len(existing) else None
            items.append(
                ConsolidatedMemory(
                    kind=item["kind"],
                    summary=summary[:400],
                    topics=_clean_labels(item.get("topics", [])),
                    importance=_unit(item.get("importance", 0.5)),
                    sensitivity=item.get("sensitivity", "none"),
                    valence=_signed(item.get("valence", 0.0)),
                    arousal=_signed(item.get("arousal", 0.0)),
                    source_ids=sources,
                    update_of=update_of,
                )
            )
        return MemoryLlmResult(consolidated=items, **meta)

    async def summarize_month(
        self, period: str, memories: list[str], promoted: list[str], stats: str
    ) -> MemoryLlmResult:
        user = "\n".join(
            [
                f"対象月: {period}",
                memory_prompt.numbered_block("memories", memories),
                memory_prompt.numbered_block("promoted", promoted),
                f"<stats>\n{stats}\n</stats>",
            ]
        )
        data, meta = await self._memory_call(
            "consolidation",
            memory_prompt.MONTHLY_SYSTEM,
            user,
            memory_prompt.MONTHLY_SCHEMA,
            "monthly_summary",
            memory_prompt.MONTHLY_VERSION,
        )
        summary = str(data.get("summary", "")).strip()
        if not summary:
            raise RuntimeError("月次の概要が空でした")
        sources = [int(n) for n in data.get("source_ids", []) if 0 <= int(n) < len(memories)]
        return MemoryLlmResult(
            summary=summary[:1200], topics=_clean_labels(data.get("topics", []), 5), source_ids=sources, **meta
        )

    async def assign_topics(
        self, memories: list[TopicMemoryInput], candidates: list[TopicCandidate]
    ) -> TopicOps:
        topic_lines: list[str] = []
        for candidate in candidates:
            names = f"(別名: {'、'.join(candidate.aliases)})" if candidate.aliases else ""
            topic_lines.append(f"{candidate.ref} [{candidate.kind}] {candidate.title}{names}")
            topic_lines += [f"    {line}" for line in candidate.entries]
        memory_lines = [
            f"{index} [told={m.told_on} kind={m.kind} topics={'、'.join(m.topics)}] {m.text}"
            for index, m in enumerate(memories)
        ]
        data, meta = await self._memory_call(
            "consolidation",
            topic_prompt.ASSIGN_SYSTEM,
            topic_prompt.assign_prompt(memory_lines, topic_lines),
            topic_prompt.ASSIGN_SCHEMA,
            "topic_operations",
            topic_prompt.ASSIGN_VERSION,
        )
        # モデルの出力を、そのまま信用しない: 知らない番号・範囲外の記憶の番号は、捨てる
        known = {c.ref for c in candidates}
        new_topics = []
        for item in data.get("new_topics", [])[:5]:
            ref, title = str(item.get("ref", "")).strip(), str(item.get("title", "")).strip()
            if ref and title and ref not in known:
                new_topics.append(NewTopicOp(ref, item["kind"], title[:30], _clean_labels(item.get("aliases", []))))
                known.add(ref)
        entries = [
            TopicEntryOp(
                topic=str(item["topic"]),
                memory=int(item["memory"]),
                kind=item["kind"],
                text=str(item.get("text", "")).strip(),
                time_expression=str(item.get("time_expression", "")).strip(),
                occurred_from=str(item.get("occurred_from", "")).strip(),
                occurred_to=str(item.get("occurred_to", "")).strip(),
                precision=str(item.get("precision", "unknown")),
                resolves=str(item.get("resolves", "")).strip(),
                supersedes=str(item.get("supersedes", "")).strip(),
            )
            for item in data.get("entries", [])[:20]
            if str(item.get("topic")) in known and 0 <= int(item.get("memory", -1)) < len(memories)
        ]
        descriptions = {
            str(item["topic"]): str(item.get("text", "")).strip()
            for item in data.get("descriptions", [])
            if str(item.get("topic")) in known
        }
        relations = [
            TopicRelationOp(
                source=str(item["source"]), target=str(item["target"]), kind=str(item["kind"]),
                memory=int(item["memory"]), role=str(item.get("role", "")),
            )
            for item in data.get("relations", [])[:10]
            if str(item.get("source")) in known and str(item.get("target")) in known
            and 0 <= int(item.get("memory", -1)) < len(memories)
        ]  # fmt: skip
        return TopicOps(
            new_topics=new_topics, entries=entries, descriptions=descriptions, relations=relations, **meta
        )

    async def same_topic(self, first: TopicCandidate, second: TopicCandidate) -> bool:
        def describe(candidate: TopicCandidate) -> list[str]:
            names = f"(別名: {'、'.join(candidate.aliases)})" if candidate.aliases else ""
            return [f"[{candidate.kind}] {candidate.title}{names}", *candidate.entries]

        data, _ = await self._memory_call(
            "consolidation",
            topic_prompt.MERGE_SYSTEM,
            topic_prompt.merge_prompt(describe(first), describe(second)),
            topic_prompt.MERGE_SCHEMA,
            "topic_merge_judgement",
            topic_prompt.MERGE_VERSION,
        )
        return bool(data.get("same", False))

    async def summarize_topic(self, title: str, lines: list[str]) -> str:
        data, _ = await self._memory_call(
            "consolidation",
            topic_prompt.ARCHIVE_SYSTEM,
            topic_prompt.archive_prompt(title, lines),
            topic_prompt.ARCHIVE_SCHEMA,
            "topic_archive_summary",
            topic_prompt.ARCHIVE_VERSION,
        )
        return str(data.get("summary", "")).strip()[:200]

    async def _respond(self, **kwargs: Any) -> Any:
        """Responses API を呼ぶ。ガードレールによる遮断は、ContentFilterError に変換する。"""
        from openai import BadRequestError

        purpose = str(kwargs.get("model", "unknown"))  # デプロイ名(用途の区別に使う。本文は送らない)
        try:
            response = await self._client.responses.create(**kwargs)
        except BadRequestError as exc:
            if getattr(exc, "code", None) == "content_filter":
                telemetry.record_llm(purpose, "content_filter")
                raise ContentFilterError("prompt", _filtered_categories(exc.body)) from None
            telemetry.record_llm(purpose, "error")
            raise
        except Exception:
            telemetry.record_llm(purpose, "error")
            raise
        details = getattr(response, "incomplete_details", None)
        if getattr(response, "status", None) == "incomplete" and getattr(details, "reason", None) == "content_filter":
            # 出力が途中で遮断された。途中までの文面は使わない
            telemetry.record_llm(purpose, "content_filter", getattr(response, "usage", None))
            raise ContentFilterError("completion")
        telemetry.record_llm(purpose, "ok", getattr(response, "usage", None))
        return response

    async def _respond_stream(self, on_text: Any, **kwargs: Any) -> Any:
        """Responses API を、逐次で呼ぶ。届いた文字を `on_text` に渡し、最後に応答の全体を返す。

        ガードレールによる遮断は、`_respond` と同じく ContentFilterError に変換する。
        """
        from openai import BadRequestError

        purpose = str(kwargs.get("model", "unknown"))
        final = None
        try:
            stream = await self._client.responses.create(stream=True, **kwargs)
            async for event in stream:
                kind = getattr(event, "type", "")
                if kind == "response.output_text.delta":
                    await on_text(getattr(event, "delta", "") or "")
                elif kind in ("response.completed", "response.incomplete", "response.failed"):
                    final = getattr(event, "response", None)
                elif kind == "error":
                    raise RuntimeError("Responses API の逐次の応答で、エラーが返りました")
        except BadRequestError as exc:
            if getattr(exc, "code", None) == "content_filter":
                telemetry.record_llm(purpose, "content_filter")
                raise ContentFilterError("prompt", _filtered_categories(exc.body)) from None
            telemetry.record_llm(purpose, "error")
            raise
        except Exception:
            telemetry.record_llm(purpose, "error")
            raise
        if final is None:
            telemetry.record_llm(purpose, "error")
            raise RuntimeError("Responses API の逐次の応答が、完了しませんでした")
        details = getattr(final, "incomplete_details", None)
        if getattr(final, "status", None) == "incomplete" and getattr(details, "reason", None) == "content_filter":
            telemetry.record_llm(purpose, "content_filter", getattr(final, "usage", None))
            raise ContentFilterError("completion")
        if getattr(final, "status", None) == "failed":
            telemetry.record_llm(purpose, "error", getattr(final, "usage", None))
            raise RuntimeError("Responses API の逐次の応答が、失敗しました")
        telemetry.record_llm(purpose, "ok", getattr(final, "usage", None))
        return final

    async def embed(self, texts: list[str]) -> list[Vector]:
        if not texts:
            return []
        response = await self._client.embeddings.create(
            model=self._config.deployment("embedding"),
            input=texts,
            dimensions=self._settings.embedding_dimensions,
        )
        vectors = [item.embedding for item in sorted(response.data, key=lambda d: d.index)]
        for vector in vectors:
            if len(vector) != self._settings.embedding_dimensions:
                raise RuntimeError("埋め込みの次元が設定と一致しません(embedding_dimensions)")
        return vectors

    async def check(self) -> list[LlmCheckResult]:
        results: list[LlmCheckResult] = []
        for role in LLM_ROLES:
            if role not in self._config.deployments:
                results.append(LlmCheckResult(role=role, ok=False, detail="デプロイ名が未設定"))
                continue
            try:
                if role == "embedding":
                    await self.embed(["疎通確認"])
                else:
                    await self._client.responses.create(
                        model=self._config.deployments[role],
                        input="「はい」とだけ答えてください。",
                        max_output_tokens=16,
                        store=False,
                    )
                results.append(LlmCheckResult(role=role, ok=True))
            except Exception as exc:  # noqa: BLE001 - 疎通確認なので、種類だけを報告して続ける
                results.append(LlmCheckResult(role=role, ok=False, detail=type(exc).__name__))
        return results

    async def close(self) -> None:
        await self._client.close()
        if self._credential is not None:
            await self._credential.close()


def _clean_topics(topics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """話題タグを出力規約に合わせる(最大3件、関連度の高い順、長すぎるラベルは切る。§5.2)。"""
    cleaned = [
        {"label": str(t.get("label", "")).strip()[:40], "relevance": float(t.get("relevance", 0.0))}
        for t in topics
        if str(t.get("label", "")).strip()
    ]
    cleaned.sort(key=lambda t: t["relevance"], reverse=True)
    return cleaned[:3]


def _filtered_categories(body: Any) -> frozenset[str]:
    """エラーの本文から、遮断の理由になったカテゴリの名前を取り出す。"""
    names: set[str] = set()
    if isinstance(body, dict):
        for entry in body.get("content_filters") or []:
            results = entry.get("content_filter_results") or {}
            for name, result in results.items():
                if isinstance(result, dict) and result.get("filtered"):
                    names.add(name)
    return frozenset(names)


def _clean_labels(labels: list[Any], limit: int = 3) -> list[str]:
    cleaned = [str(label).strip()[:40] for label in labels if str(label).strip()]
    return list(dict.fromkeys(cleaned))[:limit]


def _unit(value: Any) -> float:
    return max(0.0, min(1.0, float(value)))


def _signed(value: Any) -> float:
    return max(-1.0, min(1.0, float(value)))
