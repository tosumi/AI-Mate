"""利用者による記憶と感情履歴の操作(本体仕様書 §9.7、§10.3、§13.3)。

`/memory list|show|forget|mute|unmute|edit|promote`、`/emotion list|forget`、
`companion memory consolidate|status` の中身。API は、ここを呼ぶだけにする。

どの操作も、認証で決まった `user_id` と、選択中の `character_id` の中で行う。
別のキャラクタの記憶や感情履歴は、見えないし、操作できない(§13.4.6)。
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from mycompanion.config import EngineSettings
from mycompanion.engine.stores import EngineStores
from mycompanion.llm.client import Persona
from mycompanion.memory import scoring
from mycompanion.memory.models import KINDS, TIERS, Memory
from mycompanion.memory.service import MemoryManager, previous_month
from mycompanion.memory.topics import TopicManager

MIN_ID_PREFIX = 6  # 記憶の ID は、先頭の数文字でも指定できる


class MemoryOpError(Exception):
    """利用者に伝えるエラー。`code` は API のエラーの種類。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class MemoryOps:
    def __init__(self, stores: EngineStores, manager: MemoryManager, settings: EngineSettings) -> None:
        self._stores = stores
        self._manager = manager
        self._settings = settings

    async def _persona(self, character_id: str) -> Persona:
        persona = await self._stores.get_character(character_id)
        if persona is None:
            raise MemoryOpError("character_not_found", "指定されたキャラクタが見つかりません。")
        return persona

    async def _timezone(self, user_id: uuid.UUID, character_id: str) -> str:
        return (await self._stores.load_turn_state(user_id, character_id, uuid.UUID(int=0), 0.0)).timezone

    async def _find(self, user_id: uuid.UUID, character_id: str, memory_id: str) -> Memory:
        """ID(全体、または先頭の数文字)から記憶を探す。再活性化のコピーは、利用者には見せない。"""
        text = memory_id.strip().lower()
        if len(text) < MIN_ID_PREFIX:
            raise MemoryOpError("invalid_value", f"記憶の ID は、先頭の{MIN_ID_PREFIX}文字以上で指定してください。")
        found = [
            m
            for m in await self._manager.store.list(user_id, character_id)
            if str(m.memory_id).startswith(text) and not m.is_copy
        ]
        if not found:
            raise MemoryOpError("memory_not_found", "指定された記憶が見つかりません。")
        if len(found) > 1:
            raise MemoryOpError("invalid_value", "複数の記憶が当てはまります。ID を、もう少し長く指定してください。")
        return found[0]

    # --- 記憶 ---

    async def list(
        self, user_id: uuid.UUID, character_id: str, tier: str | None = None, kind: str | None = None, debug: bool = False
    ) -> list[dict[str, Any]]:
        if tier is not None and tier not in TIERS:
            raise MemoryOpError("invalid_value", "tier は short / medium / long のいずれかで指定してください。")
        if kind is not None and kind not in KINDS:
            raise MemoryOpError("invalid_value", f"kind は {' / '.join(KINDS)} のいずれかで指定してください。")
        await self._persona(character_id)
        memories = await self._manager.store.list(user_id, character_id, tier=tier, kind=kind)
        order = {name: index for index, name in enumerate(TIERS)}
        memories = sorted(
            (m for m in memories if not m.is_copy), key=lambda m: (order[m.tier], m.created_at or datetime.min.replace(tzinfo=UTC))
        )
        return [m.view(debug) for m in memories]

    async def show(self, user_id: uuid.UUID, character_id: str, memory_id: str, debug: bool = False) -> dict[str, Any]:
        memory = await self._find(user_id, character_id, memory_id)
        view = memory.view(debug)
        if debug:
            events = await self._manager.store.recalls(user_id, character_id, memory.memory_id)
            view["recalls"] = [
                {"day": e.day.isoformat(), "trigger": e.trigger, "source": e.source, "status": e.status} for e in events
            ]
        return view

    async def forget(self, user_id: uuid.UUID, character_id: str, memory_id: str) -> dict[str, Any]:
        memory = await self._find(user_id, character_id, memory_id)
        await self._manager.forget(user_id, character_id, memory.memory_id, datetime.now(UTC))
        return {"deleted": True, "id": str(memory.memory_id)}

    async def set_muted(self, user_id: uuid.UUID, character_id: str, memory_id: str, muted: bool) -> dict[str, Any]:
        memory = await self._find(user_id, character_id, memory_id)
        await self._manager.set_muted(user_id, character_id, memory.memory_id, muted)
        return {"id": str(memory.memory_id), "muted": muted}

    async def edit(self, user_id: uuid.UUID, character_id: str, memory_id: str, summary: str) -> dict[str, Any]:
        if not summary.strip():
            raise MemoryOpError("invalid_value", "訂正後の内容が空です。")
        persona = await self._persona(character_id)
        memory = await self._find(user_id, character_id, memory_id)
        if memory.kind == "monthly_summary":
            raise MemoryOpError("invalid_value", "月次の概要は、訂正できません(元の記憶を訂正・削除してください)。")
        await self._manager.edit(user_id, character_id, persona, memory.memory_id, summary)
        updated = await self._manager.store.get(user_id, character_id, memory.memory_id)
        assert updated is not None
        return updated.view()

    async def promote(self, user_id: uuid.UUID, character_id: str, memory_id: str) -> dict[str, Any]:
        memory = await self._find(user_id, character_id, memory_id)
        if not await self._manager.promote_manually(user_id, character_id, memory.memory_id, datetime.now(UTC)):
            raise MemoryOpError("invalid_value", "長期に昇格できるのは、中期の記憶だけです。")
        return {"id": str(memory.memory_id), "tier": "long"}

    async def consolidate(
        self,
        user_id: uuid.UUID,
        character_id: str,
        *,
        day: str | None = None,
        monthly: bool = False,
        month: str | None = None,
        dry_run: bool = False,
        now: datetime | None = None,
        keep_sensitive: bool = False,
    ) -> list[dict[str, Any]]:
        """日次整理・月次整理を手動で実行する。未整理の日があれば、古い日から順に処理する(§10.3)。"""
        now = now or datetime.now(UTC)
        persona = await self._persona(character_id)
        timezone = await self._timezone(user_id, character_id)
        today = scoring.local_day(now, timezone, self._settings.memory_consolidation_time)
        runs = []
        if monthly:
            period = month or previous_month(today)
            try:
                year, number = (int(part) for part in period.split("-"))
                date(year, number, 1)
            except ValueError:
                raise MemoryOpError("invalid_value", "month は YYYY-MM の形式で指定してください。") from None
            runs.append(await self._manager.consolidate_monthly(user_id, character_id, period, now, dry_run))
        else:
            if day is not None:
                try:
                    days = [date.fromisoformat(day)]
                except ValueError:
                    raise MemoryOpError("invalid_value", "day は YYYY-MM-DD の形式で指定してください。") from None
            else:
                shorts = await self._manager.store.list(user_id, character_id, tier="short")
                # 手動の実行では、今日の分も対象にする(確認・検証のため)
                days = sorted({m.source_day for m in shorts if m.source_day and m.source_day <= today})
                days = days[-self._settings.memory_catchup_max_days :]
            for target in days:
                runs.append(
                    await self._manager.consolidate_daily(
                        user_id, character_id, persona, target, now, dry_run, keep_sensitive, timezone
                    )
                )
        return [
            {"type": r.run_type, "target": r.target, "status": r.status, "counts": r.counts, "error": r.error}
            for r in runs
        ]

    # --- トピック(話題ごとの経過。§9.13) ---

    def _topics(self) -> TopicManager:
        if self._manager.topics is None:
            raise MemoryOpError("memory_unavailable", "トピックの機能が構成されていません。")
        return self._manager.topics

    async def topics(
        self, user_id: uuid.UUID, character_id: str, archived: bool = False, include_candidates: bool = False
    ) -> dict[str, Any]:
        """トピックの一覧。`archived` なら、削除(アーカイブ)したトピックの概要の一覧。"""
        await self._persona(character_id)
        if archived:
            return {"archived": await self._topics().archives(user_id, character_id)}
        return {"topics": await self._topics().list_topics(user_id, character_id, include_candidates)}

    async def topic_show(self, user_id: uuid.UUID, character_id: str, topic_id: str) -> dict[str, Any]:
        await self._persona(character_id)
        manager = self._topics()
        if topic_id.strip().lower().startswith("a-"):
            for item in await manager.archives(user_id, character_id):
                if item["id"] == topic_id.strip().lower():
                    return {"archived": True, **item}
            raise MemoryOpError("topic_not_found", "指定されたアーカイブが見つかりません。")
        doc = await manager.find(user_id, character_id, topic_id)
        if doc is None:
            raise MemoryOpError("topic_not_found", "指定されたトピックが見つかりません。")
        return manager.view(doc, detail=True)

    async def topic_forget(self, user_id: uuid.UUID, character_id: str, topic_id: str) -> dict[str, Any]:
        """トピックの削除。`a-` で始まる ID はアーカイブの完全な削除、それ以外はアーカイブにする。"""
        await self._persona(character_id)
        manager = self._topics()
        if topic_id.strip().lower().startswith("a-"):
            if not await manager.purge(user_id, character_id, topic_id):
                raise MemoryOpError("topic_not_found", "指定されたアーカイブが見つかりません。")
            return {"purged": True, "id": topic_id.strip().lower()}
        archive = await manager.archive(user_id, character_id, topic_id, datetime.now(UTC))
        if archive is None:
            raise MemoryOpError("topic_not_found", "指定されたトピックが見つかりません。")
        return {"archived": True, **archive.view()}

    async def topic_unlink(
        self, user_id: uuid.UUID, character_id: str, topic_id: str, relation_id: str
    ) -> dict[str, Any]:
        """トピック同士の関係を、取り消す(`/memory topic unlink`)。"""
        await self._persona(character_id)
        if not await self._topics().unlink(user_id, character_id, topic_id, relation_id, datetime.now(UTC)):
            raise MemoryOpError("topic_not_found", "指定された関係が見つかりません。")
        return {"id": topic_id.strip().lower(), "unlinked": relation_id}

    async def topic_set_muted(
        self, user_id: uuid.UUID, character_id: str, topic_id: str, muted: bool
    ) -> dict[str, Any]:
        await self._persona(character_id)
        if not await self._topics().set_muted(user_id, character_id, topic_id, muted, datetime.now(UTC)):
            raise MemoryOpError("topic_not_found", "指定されたトピックが見つかりません。")
        return {"id": topic_id.strip().lower(), "muted": muted}

    async def status(self, user_id: uuid.UUID, character_id: str) -> dict[str, Any]:
        await self._persona(character_id)
        return await self._manager.status(user_id, character_id)

    # --- 感情履歴(FR-10) ---

    async def emotion_list(self, user_id: uuid.UUID, character_id: str, days: int = 7) -> dict[str, Any]:
        """このキャラクタとの会話での、自分の感情の推定の履歴(日ごとの要約と、最近の記録)。"""
        if not 1 <= days <= 365:
            raise MemoryOpError("invalid_value", "days は 1〜365 で指定してください。")
        await self._persona(character_id)
        timezone = await self._timezone(user_id, character_id)
        since = datetime.now(UTC) - timedelta(days=days)
        events = await self._stores.emotion_history(user_id, character_id, since)
        by_day: dict[date, list[dict[str, Any]]] = {}
        for event in events:
            moment = datetime.fromisoformat(str(event["ts"]))
            by_day.setdefault(scoring.local_day(moment, timezone, self._settings.memory_consolidation_time), []).append(event)
        summaries = []
        for day in sorted(by_day, reverse=True):
            items = by_day[day]
            totals: dict[str, float] = {}
            for item in items:
                for name, value in (item.get("categories_json") or {}).items():
                    totals[name] = totals.get(name, 0.0) + float(value)
            top = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:2]
            summaries.append(
                {
                    "day": day.isoformat(),
                    "count": len(items),
                    "vad": scoring.average_vad([item.get("vad_json") or {} for item in items]),
                    "main": [name for name, value in top if value / len(items) >= 0.2],
                }
            )
        recent = [
            {"id": e["id"], "ts": e["ts"], "vad": e.get("vad_json"), "confidence": e.get("confidence")}
            for e in events[:20]
        ]
        return {"days": summaries, "recent": recent}

    async def emotion_forget(
        self, user_id: uuid.UUID, character_id: str, event_id: str | None = None, day: str | None = None
    ) -> dict[str, Any]:
        """感情の推定の記録を削除する。日を指定した場合は、その日の記憶の感情タグも消す(§9.7)。"""
        if (event_id is None) == (day is None):
            raise MemoryOpError("invalid_value", "event_id か day の、どちらか一方を指定してください。")
        await self._persona(character_id)
        cleared = 0
        if event_id is not None:
            ids = [event_id]
        else:
            try:
                target = date.fromisoformat(str(day))
            except ValueError:
                raise MemoryOpError("invalid_value", "day は YYYY-MM-DD の形式で指定してください。") from None
            timezone = await self._timezone(user_id, character_id)
            start = datetime(target.year, target.month, target.day, tzinfo=UTC) - timedelta(days=2)
            events = await self._stores.emotion_history(user_id, character_id, start)
            ids = [
                str(e["id"])
                for e in events
                if scoring.local_day(datetime.fromisoformat(str(e["ts"])), timezone, self._settings.memory_consolidation_time)
                == target
            ]
            for memory in await self._manager.store.list(user_id, character_id):
                if memory.source_day == target and memory.emotion_vad is not None:
                    await self._manager.store.update(
                        user_id, character_id, memory.memory_id, emotion_vad=None, emotion_categories=None
                    )
                    cleared += 1
        deleted = await self._stores.delete_emotion_events(user_id, character_id, ids) if ids else 0
        return {"deleted": deleted, "memory_tags_cleared": cleared}
