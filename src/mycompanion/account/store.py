"""アカウントの保存先(メモリ上の実装と、PostgreSQL + Cosmos DB の実装)。

書き出しと削除は、本人のデータに対してだけ行う。PostgreSQL では、行レベルセキュリティの下で、
本人のキャラクタを1つずつ読む(キャラクタの一覧と、全削除は、本人に限定した専用の関数を使う。0007)。
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime
from typing import Any

from mycompanion.db.cosmos import CosmosStores
from mycompanion.db.postgres import Database, Scope
from mycompanion.engine.stores import InMemoryStores, UserContext
from mycompanion.memory import okf
from mycompanion.memory.store import InMemoryMemoryStore, MemoryStore
from mycompanion.memory.topic_store import InMemoryTopicStore, TopicStore


async def _topic_bundle(topics: TopicStore | None, user_id: uuid.UUID, character_id: str) -> dict[str, str]:
    """トピックの OKF バンドル(パス → 文書)。キャラクタごとに別。アーカイブ(概要)は `archive/` に入れる。"""
    if topics is None:
        return {}
    docs = [d for d in await topics.list(user_id, character_id) if d.status != "deprecated"]
    archives = await topics.archives(user_id, character_id)
    if not docs and not archives:
        return {}
    files = {"index.md": okf.index_markdown(docs)}
    files |= {doc.path: doc.markdown() for doc in docs}
    files |= {archive.path: archive.markdown() for archive in archives}
    return files


SUMMARY_KEYS = ("character_id", "character_version", "compose_mode", "reply_model", "reply_prompt_version", "response_mode")
SUMMARY_MEANS = ("overall", "persona_fit", "naturalness", "empathy")


def summarize_ratings(ratings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """評価を、作り方・モデルなどの組ごとに集計する(件数と平均だけ。0016 の rating_summary と同じ形)。"""
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for rating in ratings:
        groups.setdefault(tuple(rating.get(k) for k in SUMMARY_KEYS), []).append(rating)
    rows = []
    for key in sorted(groups, key=lambda k: tuple(str(v) for v in k)):
        items = groups[key]
        row: dict[str, Any] = dict(zip(SUMMARY_KEYS, key, strict=True))
        row["raters"] = len({r["user_id"] for r in items})
        row["n"] = len(items)
        for axis in SUMMARY_MEANS:
            values = [r[axis] for r in items if r.get(axis) is not None]
            row[axis] = round(sum(values) / len(values), 2) if values else None
        row["low"] = sum(1 for r in items if r["overall"] <= 2)
        rows.append(row)
    return rows


def mask_small_groups(row: dict[str, Any], min_n: int) -> dict[str, Any]:
    """件数が `min_n` 未満の組は、平均と低い評価の件数を消す(0017 の save_rating_snapshot と同じ)。"""
    if row["n"] >= min_n:
        return dict(row)
    return {**row, **{k: None for k in (*SUMMARY_MEANS, "low")}}


def _memory_views(memories: list[Any]) -> list[dict[str, Any]]:
    return [m.view(debug=True) for m in memories if not m.is_copy]


class InMemoryAccountStore:
    def __init__(
        self, stores: InMemoryStores, memory: InMemoryMemoryStore, topics: InMemoryTopicStore | None = None
    ) -> None:
        self._stores = stores
        self._memory = memory
        self._topics = topics
        self.granted: dict[uuid.UUID, list[dict[str, Any]]] = {}
        self.reports: list[dict[str, Any]] = []
        self.ratings: list[dict[str, Any]] = []
        self.snapshots: list[dict[str, Any]] = []  # 集計の保存(ユーザーのデータを持たない)
        self.audit: list[dict[str, Any]] = []
        # 外部テナントのアカウント(user_id → オブジェクト ID)と、保留中の削除
        self.identity_subjects: dict[uuid.UUID, list[str]] = {}
        self.pending_identity: list[str] = []

    async def consents(self, user_id: uuid.UUID) -> list[dict[str, Any]]:
        return [dict(c) for c in self.granted.get(user_id, [])]

    async def grant(
        self, user_id: uuid.UUID, items: list[tuple[str, str]], version: str, channel: str, now: datetime
    ) -> None:
        records = self.granted.setdefault(user_id, [])
        for kind, text_hash in items:
            if not any(c["type"] == kind and c["version"] == version and c["revoked_at"] is None for c in records):
                records.append(
                    {"type": kind, "version": version, "granted_at": now.isoformat(), "revoked_at": None,
                     "text_hash": text_hash, "channel": channel}
                )

    async def is_under_18(self, user_id: uuid.UUID) -> bool:
        return self._stores.user_contexts.get(user_id, UserContext()).is_under_18

    async def delete_sensitive_memories(self, user_id: uuid.UUID, now: datetime) -> int:
        count = 0
        for (owner, character_id), scope in list(self._memory.items.items()):
            if owner != user_id:
                continue
            ids = [m.memory_id for m in scope.values() if m.sensitivity and m.deleted_at is None]
            count += await self._memory.soft_delete(user_id, character_id, ids, now)
        return count

    async def revoke(self, user_id: uuid.UUID, kind: str, now: datetime) -> int:
        count = 0
        for item in self.granted.get(user_id, []):
            if item["type"] == kind and item["revoked_at"] is None:
                item["revoked_at"] = now.isoformat()
                count += 1
        return count

    async def set_age_flag(self, user_id: uuid.UUID, under_18: bool, now: datetime) -> None:
        current = self._stores.user_contexts.get(user_id, UserContext())
        self._stores.user_contexts[user_id] = replace(current, is_under_18=under_18)

    def _characters(self, user_id: uuid.UUID) -> list[str]:
        keys = [*self._stores.turns, *self._stores.relationships, *self._memory.items]
        return sorted({character for user, character in keys if user == user_id})

    async def export_data(self, user_id: uuid.UUID) -> dict[str, Any]:
        context = await self._stores.load_user_context(user_id)
        characters = {}
        for character_id in self._characters(user_id):
            key = (user_id, character_id)
            characters[character_id] = {
                "relationship": self._stores.relationships.get(key),
                "memories": _memory_views(await self._memory.list(user_id, character_id)),
                "turns": [
                    {"ts": t.ts.isoformat(), "user_input": t.user_input, "reply": t.reply_text}
                    for t in self._stores.turns.get(key, [])
                ],
                "emotion_events": await self._stores.emotion_history(user_id, character_id),
                "topics": await _topic_bundle(self._topics, user_id, character_id),
                "security_events": [
                    {k: v for k, v in e.items() if k != "user_id"}
                    for e in self._stores.security_events
                    if e["user_id"] == user_id and e.get("character_id") == character_id
                ],
                "reports": [
                    {k: v for k, v in r.items() if k not in ("user_id", "character_id")}
                    for r in self.reports
                    if r["user_id"] == user_id and r["character_id"] == character_id
                ],
                "ratings": [
                    {k: v for k, v in r.items() if k not in ("user_id", "character_id")}
                    for r in self.ratings
                    if r["user_id"] == user_id and r["character_id"] == character_id
                ],
            }
        return {
            "settings": {"silence_enabled": context.silence_enabled, "filter_mode": context.filter_mode,
                         "is_under_18": context.is_under_18},
            "consents": list(self.granted.get(user_id, [])),
            "characters": characters,
        }

    async def delete_all(self, user_id: uuid.UUID, now: datetime) -> dict[str, int]:
        counts = {"turns": 0, "memories": 0}
        for name in ("turns", "emotions", "relationships", "relationship_events", "user_mood", "companion",
                     "sessions", "last_activity", "previous_activity", "pressure", "preferences", "silence_counts"):  # fmt: skip
            table = getattr(self._stores, name)
            for key in [k for k in table if k[0] == user_id]:
                if name == "turns":
                    counts["turns"] += len(table[key])
                del table[key]
        for name in ("safety", "user_contexts", "rate_hits", "serious_events", "timezones"):
            getattr(self._stores, name).pop(user_id, None)
        self._stores.security_events[:] = [e for e in self._stores.security_events if e["user_id"] != user_id]
        for key in [k for k in self._memory.items if k[0] == user_id]:
            counts["memories"] += len(self._memory.items.pop(key))
        for table in (self._memory.recall_events, self._memory.run_records, self._memory.baselines):
            for key in [k for k in table if k[0] == user_id]:
                del table[key]
        for key in [k for k in self._memory.working if k[0] == user_id]:
            del self._memory.working[key]
        if self._topics is not None:
            for table in (self._topics.docs, self._topics.history, self._topics.archived):
                for key in [k for k in table if k[0] == user_id]:
                    del table[key]
        self.reports[:] = [r for r in self.reports if r["user_id"] != user_id]
        self.ratings[:] = [r for r in self.ratings if r["user_id"] != user_id]
        # 監査ログに残る user_id は、元に戻せない別の値に置き換える
        alias = uuid.uuid4()
        for entry in self.audit:
            if entry.get("target_user_id") == user_id:
                entry["target_user_id"] = alias
        user_id_for_audit = alias
        self.granted.pop(user_id, None)
        # サインインの情報の削除は、ワーカーが行う(保留中として記録する)
        self.pending_identity += self.identity_subjects.pop(user_id, [])
        self.audit.append({"action": "account_deleted", "target_user_id": user_id_for_audit, "ts": now})
        return counts

    async def add_report(self, user_id: uuid.UUID, character_id: str, report: dict[str, Any], now: datetime) -> str:
        report_id = str(uuid.uuid4())
        self.reports.append({"id": report_id, "user_id": user_id, "character_id": character_id, "ts": now, **report})
        return report_id

    async def purge_audit_logs(self, before: datetime) -> int:
        kept = [entry for entry in self.audit if entry["ts"] >= before]
        removed = len(self.audit) - len(kept)
        self.audit[:] = kept
        return removed

    async def pending_identity_deletions(self) -> list[str]:
        return sorted(set(self.pending_identity))

    async def upsert_rating(
        self, user_id: uuid.UUID, character_id: str, rating: dict[str, Any], now: datetime
    ) -> str:
        key = (user_id, character_id, rating["session_id"], rating["turn_no"])
        for existing in self.ratings:
            if (existing["user_id"], existing["character_id"], existing["session_id"], existing["turn_no"]) == key:
                existing.update(rating, ts=now)
                return str(existing["id"])
        rating_id = str(uuid.uuid4())
        self.ratings.append({"id": rating_id, "user_id": user_id, "character_id": character_id, "ts": now, **rating})
        return rating_id

    async def rating_summary(self, since: datetime) -> list[dict[str, Any]]:
        return summarize_ratings([r for r in self.ratings if r["ts"] >= since])

    async def save_rating_snapshot(self, label: str, since: datetime, min_n: int, now: datetime) -> str:
        rows = [mask_small_groups(row, min_n) for row in await self.rating_summary(since)]
        snapshot_id = str(uuid.uuid4())
        self.snapshots.append(
            {"snapshot_id": snapshot_id, "label": label, "since": since, "until": now, "min_n": min_n, "rows": rows}
        )
        return snapshot_id

    async def list_rating_snapshots(self) -> list[dict[str, Any]]:
        return [dict(s) for s in sorted(self.snapshots, key=lambda s: s["until"])]

    async def complete_identity_deletion(self, subject: str, now: datetime) -> int:
        count = self.pending_identity.count(subject)
        self.pending_identity[:] = [s for s in self.pending_identity if s != subject]
        return count


class PgAccountStore:
    def __init__(
        self, database: Database, cosmos: CosmosStores, memory: MemoryStore, topics: TopicStore | None = None
    ) -> None:
        self._db = database
        self._cosmos = cosmos
        self._memory = memory
        self._topics = topics

    async def consents(self, user_id: uuid.UUID) -> list[dict[str, Any]]:
        async with self._db.scoped_transaction(Scope(user_id)) as conn:
            rows = await (
                await conn.execute(
                    "SELECT type, version, granted_at, revoked_at, text_hash, channel FROM consents "
                    "WHERE user_id = %s ORDER BY granted_at",
                    (user_id,),
                )
            ).fetchall()
        return [
            {
                "type": r[0],
                "version": r[1],
                "granted_at": r[2].isoformat(),
                "revoked_at": r[3].isoformat() if r[3] else None,
                "text_hash": r[4],
                "channel": r[5],
            }
            for r in rows
        ]

    async def grant(
        self, user_id: uuid.UUID, items: list[tuple[str, str]], version: str, channel: str, now: datetime
    ) -> None:
        async with self._db.scoped_transaction(Scope(user_id)) as conn:
            for kind, text_hash in items:
                await conn.execute(
                    "INSERT INTO consents (user_id, type, version, granted_at, text_hash, channel) "
                    "SELECT %s, %s, %s, %s, %s, %s WHERE NOT EXISTS (SELECT 1 FROM consents WHERE user_id = %s "
                    "AND type = %s AND version = %s AND revoked_at IS NULL)",
                    (user_id, kind, version, now, text_hash, channel, user_id, kind, version),
                )

    async def is_under_18(self, user_id: uuid.UUID) -> bool:
        async with self._db.scoped_transaction(Scope(user_id)) as conn:
            row = await (
                await conn.execute("SELECT is_under_18 FROM user_settings WHERE user_id = %s", (user_id,))
            ).fetchone()
        return bool(row[0]) if row else True  # 設定がなければ、安全側

    async def delete_sensitive_memories(self, user_id: uuid.UUID, now: datetime) -> int:
        async with self._db.scoped_transaction(Scope(user_id)) as conn:
            names = [r[0] for r in await (await conn.execute("SELECT character_id FROM list_my_characters()")).fetchall()]
        count = 0
        for character_id in names:
            memories = await self._memory.list(user_id, character_id)
            ids = [m.memory_id for m in memories if m.sensitivity]
            count += await self._memory.soft_delete(user_id, character_id, ids, now)
        return count

    async def revoke(self, user_id: uuid.UUID, kind: str, now: datetime) -> int:
        async with self._db.scoped_transaction(Scope(user_id)) as conn:
            cursor = await conn.execute(
                "UPDATE consents SET revoked_at = %s WHERE user_id = %s AND type = %s AND revoked_at IS NULL",
                (now, user_id, kind),
            )
            return cursor.rowcount

    async def set_age_flag(self, user_id: uuid.UUID, under_18: bool, now: datetime) -> None:
        async with self._db.scoped_transaction(Scope(user_id)) as conn:
            await conn.execute(
                "UPDATE user_settings SET is_under_18 = %s, age_flag_source = 'self_declared', "
                "age_flag_updated_at = %s, updated_at = %s WHERE user_id = %s",
                (under_18, now, now, user_id),
            )

    async def export_data(self, user_id: uuid.UUID) -> dict[str, Any]:
        async with self._db.scoped_transaction(Scope(user_id)) as conn:
            settings = await (
                await conn.execute(
                    "SELECT silence_enabled, filter_mode, silence_render, timezone, locale, is_under_18 "
                    "FROM user_settings WHERE user_id = %s",
                    (user_id,),
                )
            ).fetchone()
            names = [r[0] for r in await (await conn.execute("SELECT character_id FROM list_my_characters()")).fetchall()]
        keys = ("silence_enabled", "filter_mode", "silence_render", "timezone", "locale", "is_under_18")
        characters: dict[str, Any] = {}
        for character_id in sorted(names):
            async with self._db.scoped_transaction(Scope(user_id, character_id)) as conn:
                relation = await (await conn.execute("SELECT value FROM relationships")).fetchone()
                reports = await (
                    await conn.execute(
                        "SELECT report_id, session_id, turn_no, reason, note, created_at FROM response_reports "
                        "ORDER BY created_at"
                    )
                ).fetchall()
                ratings = await (
                    await conn.execute(
                        "SELECT rating_id, session_id, turn_no, overall, persona_fit, naturalness, empathy, note, "
                        "compose_mode, reply_model, updated_at FROM response_ratings ORDER BY updated_at"
                    )
                ).fetchall()
            characters[character_id] = {
                "relationship": float(relation[0]) if relation else None,
                "memories": _memory_views(await self._memory.list(user_id, character_id)),
                "topics": await _topic_bundle(self._topics, user_id, character_id),
                "turns": [],
                "emotion_events": [],
                "security_events": [],
                "reports": [
                    {
                        "id": str(r[0]), "session_id": str(r[1]) if r[1] else None, "turn_no": r[2],
                        "reason": r[3], "note": r[4], "ts": r[5].isoformat(),
                    }
                    for r in reports
                ],
                "ratings": [
                    {
                        "id": str(r[0]), "session_id": str(r[1]), "turn_no": r[2], "overall": r[3],
                        "persona_fit": r[4], "naturalness": r[5], "empathy": r[6], "note": r[7],
                        "compose_mode": r[8], "reply_model": r[9], "ts": r[10].isoformat(),
                    }
                    for r in ratings
                ],
            }
        # Cosmos DB: 本人の全項目を、キャラクタごとに振り分ける(本文は、会話の原文の保持期間のあいだだけ残っている)
        async for item in self._cosmos.turns.iter_all_for_user(user_id):
            bucket = characters.setdefault(item.get("characterId", ""), {"turns": [], "emotion_events": []})
            reply = item.get("reply_text") or item.get("filtered_text") or item.get("draft_text")
            bucket["turns"].append({"ts": item.get("ts"), "user_input": item.get("user_input"), "reply": reply})
        async for item in self._cosmos.emotion_events.iter_all_for_user(user_id):
            bucket = characters.setdefault(item.get("characterId", ""), {"turns": [], "emotion_events": []})
            bucket["emotion_events"].append(
                {k: item.get(k) for k in ("id", "ts", "categories_json", "vad_json", "confidence", "topics_json")}
            )
        # スクリーニングの記録(種類・処置・時刻。本文は、もともと保存していない)
        async for item in self._cosmos.security_events.iter_all_for_user(user_id):
            bucket = characters.setdefault(item.get("characterId", ""), {"turns": [], "emotion_events": []})
            bucket.setdefault("security_events", []).append(
                {k: item.get(k) for k in ("id", "ts", "direction", "category", "severity", "detector", "action")}
            )
        for bucket in characters.values():
            bucket["turns"].sort(key=lambda t: t["ts"] or "")
        return {
            "settings": dict(zip(keys, settings, strict=True)) if settings else {},
            "consents": await self.consents(user_id),
            "characters": characters,
        }

    async def delete_all(self, user_id: uuid.UUID, now: datetime) -> dict[str, int]:
        # 1. Cosmos DB(会話の原文、感情の推定、セキュリティの記録)。TTL を待たずに消す(§11.5.5)
        counts = {
            "turns": await self._cosmos.turns.delete_all_for_user(user_id),
            "emotion_events": await self._cosmos.emotion_events.delete_all_for_user(user_id),
            "security_events": await self._cosmos.security_events.delete_all_for_user(user_id),
        }
        # 2. PostgreSQL(記憶、状態、設定、同意、連携)。削除の記録と監査ログを残す
        async with self._db.scoped_transaction(Scope(user_id)) as conn:
            row = await (await conn.execute("SELECT delete_my_account()")).fetchone()
        counts["account"] = 1 if row and row[0] else 0
        return counts

    async def add_report(self, user_id: uuid.UUID, character_id: str, report: dict[str, Any], now: datetime) -> str:
        report_id = uuid.uuid4()
        async with self._db.scoped_transaction(Scope(user_id, character_id)) as conn:
            await conn.execute(
                "INSERT INTO response_reports (report_id, user_id, character_id, session_id, turn_no, reason, note, "
                "created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    report_id, user_id, character_id, report.get("session_id"), report.get("turn_no"),
                    report["reason"], report.get("note"), now,
                ),
            )  # fmt: skip
        return str(report_id)

    async def purge_audit_logs(self, before: datetime) -> int:
        async with self._db.pool.connection() as conn:
            row = await (await conn.execute("SELECT purge_audit_logs(%s)", (before,))).fetchone()
        return int(row[0]) if row else 0

    async def upsert_rating(
        self, user_id: uuid.UUID, character_id: str, rating: dict[str, Any], now: datetime
    ) -> str:
        columns = ("overall", "persona_fit", "naturalness", "empathy", "note", "character_version",
                   "response_mode", "compose_mode", "reply_model", "reply_prompt_version")  # fmt: skip
        values = [rating.get(c) for c in columns]
        updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in columns)
        async with self._db.scoped_transaction(Scope(user_id, character_id)) as conn:
            row = await (
                await conn.execute(
                    f"INSERT INTO response_ratings (user_id, character_id, session_id, turn_no, {', '.join(columns)}, "
                    f"created_at, updated_at) VALUES (%s, %s, %s, %s, {', '.join(['%s'] * len(columns))}, %s, %s) "
                    f"ON CONFLICT (user_id, character_id, session_id, turn_no) DO UPDATE SET {updates}, "
                    "updated_at = EXCLUDED.updated_at RETURNING rating_id",
                    (user_id, character_id, rating["session_id"], rating["turn_no"], *values, now, now),
                )
            ).fetchone()
        return str(row[0]) if row else ""

    async def rating_summary(self, since: datetime) -> list[dict[str, Any]]:
        async with self._db.pool.connection() as conn:
            cursor = await conn.execute("SELECT * FROM rating_summary(%s)", (since,))
            names = [d.name for d in cursor.description or []]
            rows = await cursor.fetchall()
        return [
            {k: (float(v) if k in SUMMARY_MEANS and v is not None else v) for k, v in zip(names, row, strict=True)}
            for row in rows
        ]

    async def save_rating_snapshot(self, label: str, since: datetime, min_n: int, now: datetime) -> str:
        async with self._db.pool.connection() as conn:
            row = await (
                await conn.execute("SELECT save_rating_snapshot(%s, %s, %s)", (label, since, min_n))
            ).fetchone()
        return str(row[0]) if row else ""

    async def list_rating_snapshots(self) -> list[dict[str, Any]]:
        async with self._db.pool.connection() as conn:
            rows = await (await conn.execute("SELECT * FROM list_rating_snapshots()")).fetchall()
        return [
            {"snapshot_id": str(r[0]), "label": r[1], "since": r[2], "until": r[3], "min_n": r[4], "rows": list(r[5])}
            for r in rows
        ]

    async def pending_identity_deletions(self) -> list[str]:
        async with self._db.pool.connection() as conn:
            rows = await (await conn.execute("SELECT idp_subject FROM pending_identity_deletions()")).fetchall()
        return [row[0] for row in rows if row[0]]

    async def complete_identity_deletion(self, subject: str, now: datetime) -> int:
        async with self._db.pool.connection() as conn:
            row = await (await conn.execute("SELECT complete_identity_deletion(%s)", (subject,))).fetchone()
        return int(row[0]) if row else 0
