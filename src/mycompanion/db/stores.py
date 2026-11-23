"""エンジンの保存先の本番実装(PostgreSQL + Cosmos DB。本体仕様書 §11.5.3)。

- セッション、関係値、感情状態、`user_safety_state` は PostgreSQL(行レベルセキュリティの下)。
- 会話の原文(`turns`)と感情の推定(`emotion_events`)は Cosmos DB(TTL つき)。
- ターンと状態の保存は、(user_id, character_id) のアドバイザリロックで直列化する(§11.5.4)。
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from mycompanion.config import EngineSettings
from mycompanion.db.cosmos import CosmosStores
from mycompanion.db.postgres import Database, Scope, lock_user_character
from mycompanion.engine.persona_request import Adjustment, PressureState
from mycompanion.engine.stores import (
    CommitBuilder,
    EmotionRecord,
    ProactiveSnapshot,
    SessionInfo,
    TurnCommit,
    TurnRecord,
    TurnState,
    UserContext,
    history_entries,
)
from mycompanion.llm.client import HistoryTurn, Persona
from mycompanion.proactive import policy as proactive_policy
from mycompanion.safety.screening import Finding
from mycompanion.state.emotion_state import EmotionVector
from mycompanion.state.events import RuleCounter

_CHARACTER_COLUMNS = (
    "character_id, name, persona_prompt, speech_style, version, initial_relationship, "
    "base_personality_json, traits_json, temperament_json, appraisal_rules_json, adaptation_json, preferences_json, "
    "event_rules_json"
)


def _persona(row: tuple[Any, ...]) -> Persona:
    base = row[6] or {}
    return Persona(
        character_id=row[0],
        name=row[1],
        persona_prompt=row[2],
        speech_style=row[3],
        version=row[4],
        initial_relationship=float(row[5]),
        axes=base.get("axes"),
        traits=row[7] or {},
        temperament=row[8] or {},
        appraisal_rules=row[9] or [],
        adaptation=row[10] or {},
        preferences=row[11] if isinstance(row[11], list) else [],
        event_rules=row[12] if isinstance(row[12], list) else [],
    )


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


class PgCosmosStores:
    def __init__(self, database: Database, cosmos: CosmosStores, settings: EngineSettings) -> None:
        self._db = database
        self._cosmos = cosmos
        self._settings = settings

    async def get_character(self, character_id: str) -> Persona | None:
        # キャラクタ定義は全ユーザー共通。RLS により、公開済みのものだけが読める
        async with self._db.pool.connection() as conn:
            cursor = await conn.execute(
                f"SELECT {_CHARACTER_COLUMNS} FROM characters "
                "WHERE character_id = %s AND status = 'published' ORDER BY version DESC LIMIT 1",
                (character_id,),
            )
            row = await cursor.fetchone()
        return _persona(row) if row else None

    async def list_characters(self) -> list[Persona]:
        """公開済みのキャラクタの一覧(各キャラクタの最新の版)。"""
        async with self._db.pool.connection() as conn:
            cursor = await conn.execute(
                f"SELECT DISTINCT ON (character_id) {_CHARACTER_COLUMNS} "
                "FROM characters WHERE status = 'published' ORDER BY character_id, version DESC"
            )
            rows = await cursor.fetchall()
        return [_persona(row) for row in rows]

    async def get_or_create_session(
        self, user_id: uuid.UUID, character_id: str, channel: str, now: datetime
    ) -> SessionInfo:
        scope = Scope(user_id, character_id)
        idle_limit = now - timedelta(minutes=self._settings.session_idle_timeout_minutes)
        async with self._db.scoped_transaction(scope) as conn:
            await lock_user_character(conn, scope)
            # 無入力が続いたセッションは、ここでも終了する(スケジューラも行う。§11.5.4)。
            # 終了させたセッションは、呼び出し側が、短期記憶への書き込みを行う
            expired = await (
                await conn.execute(
                    "UPDATE sessions SET status = 'closed', ended_at = last_activity_at, "
                    "closed_reason = 'idle_timeout' "
                    "WHERE user_id = %s AND character_id = %s AND status = 'active' AND last_activity_at < %s "
                    # 返事を待っている自発発話のセッションは、期限まで閉じない(§15.1 の M8)
                    "AND (awaiting_reply_until IS NULL OR awaiting_reply_until < %s) "
                    "RETURNING session_id",
                    (user_id, character_id, idle_limit, now),
                )
            ).fetchone()
            cursor = await conn.execute(
                "SELECT session_id FROM sessions "
                "WHERE user_id = %s AND character_id = %s AND status = 'active' "
                "ORDER BY started_at DESC LIMIT 1",
                (user_id, character_id),
            )
            row = await cursor.fetchone()
            if row is not None:
                return SessionInfo(row[0])
            cursor = await conn.execute(
                "INSERT INTO sessions (user_id, character_id, channel, started_at, last_activity_at) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING session_id",
                (user_id, character_id, channel, now, now),
            )
            created = await cursor.fetchone()
            assert created is not None
            return SessionInfo(created[0], is_new=True, expired_session_id=expired[0] if expired else None)

    async def close_session(
        self, user_id: uuid.UUID, character_id: str, reason: str, now: datetime
    ) -> bool:
        scope = Scope(user_id, character_id)
        async with self._db.scoped_transaction(scope) as conn:
            await lock_user_character(conn, scope)
            cursor = await conn.execute(
                "UPDATE sessions SET status = 'closed', ended_at = %s, closed_reason = %s "
                "WHERE user_id = %s AND character_id = %s AND status = 'active'",
                (now, reason, user_id, character_id),
            )
            return cursor.rowcount > 0

    async def current_session_id(self, user_id: uuid.UUID, character_id: str) -> uuid.UUID | None:
        async with self._db.scoped_transaction(Scope(user_id, character_id)) as conn:
            row = await (
                await conn.execute(
                    "SELECT session_id FROM sessions WHERE user_id = %s AND character_id = %s AND status = 'active' "
                    "ORDER BY started_at DESC LIMIT 1",
                    (user_id, character_id),
                )
            ).fetchone()
        return row[0] if row else None

    async def close_session_by_id(
        self, user_id: uuid.UUID, character_id: str, session_id: uuid.UUID, reason: str, now: datetime
    ) -> bool:
        """指定のセッションを終了する(スケジューラが、無入力のセッションに使う)。"""
        scope = Scope(user_id, character_id)
        async with self._db.scoped_transaction(scope) as conn:
            await lock_user_character(conn, scope)
            cursor = await conn.execute(
                "UPDATE sessions SET status = 'closed', ended_at = %s, closed_reason = %s "
                "WHERE session_id = %s AND status = 'active'",
                (now, reason, session_id),
            )
            return cursor.rowcount > 0

    async def session_turns(
        self, user_id: uuid.UUID, character_id: str, session_id: uuid.UUID
    ) -> list[dict[str, Any]]:
        items = [
            item
            async for item in self._cosmos.turns.query(
                user_id,
                character_id,
                where="c.sessionId = @sessionId",
                parameters={"sessionId": str(session_id)},
                order_by="c.turnNo ASC",
            )
        ]
        return [
            {
                "turn_id": item["id"],
                "user_input": item.get("user_input", ""),
                "reply_text": item.get("reply_text") or item.get("filtered_text") or item.get("draft_text"),
                "response_mode": item.get("response_mode", "full"),
                "turn_no": item.get("turnNo", 0),
                "safety_flags": item.get("safety_flags") or [],
                "character_version": item.get("character_version"),
                "compose_mode": item.get("compose_mode", "two_stage"),  # この項目より前の記録は、2段階
                "reply_model": item.get("reply_model", ""),
                "reply_prompt_version": item.get("reply_prompt_version", ""),
                "ts": _parse_ts(item.get("ts")),
            }
            for item in items
        ]

    async def delete_turns(self, user_id: uuid.UUID, character_id: str, turn_ids: list[str]) -> int:
        count = 0
        for turn_id in turn_ids:
            found = [
                item async for item in self._cosmos.turns.query(user_id, character_id, "c.id = @id", {"id": turn_id})
            ]
            for item in found:
                # 紐づく感情の推定(同じセッションの、同じターン番号)も削除する
                async for event in self._cosmos.emotion_events.query(
                    user_id,
                    character_id,
                    "c.sessionId = @sessionId AND c.turnNo = @turnNo",
                    {"sessionId": item.get("sessionId"), "turnNo": item.get("turnNo")},
                ):
                    await self._cosmos.emotion_events.delete(user_id, character_id, event["id"])
                if await self._cosmos.turns.delete(user_id, character_id, turn_id):
                    count += 1
        return count

    async def emotion_history(
        self, user_id: uuid.UUID, character_id: str, since: datetime | None = None
    ) -> list[dict[str, Any]]:
        where, parameters = "", {}
        if since is not None:
            where, parameters = "c.ts >= @since", {"since": since.isoformat()}
        return [
            {key: item.get(key) for key in ("id", "ts", "categories_json", "vad_json", "confidence")}
            async for item in self._cosmos.emotion_events.query(
                user_id, character_id, where=where, parameters=parameters, order_by="c.ts DESC", limit=2000
            )
        ]

    async def delete_emotion_events(self, user_id: uuid.UUID, character_id: str, event_ids: list[str]) -> int:
        count = 0
        for event_id in event_ids:
            if await self._cosmos.emotion_events.delete(user_id, character_id, event_id):
                count += 1
        return count

    async def recent_turns(
        self, user_id: uuid.UUID, character_id: str, session_id: uuid.UUID, limit: int
    ) -> list[HistoryTurn]:
        items = [
            item
            async for item in self._cosmos.turns.query(
                user_id,
                character_id,
                where="c.sessionId = @sessionId",
                parameters={"sessionId": str(session_id)},
                order_by="c.turnNo DESC",
                limit=limit,
            )
        ]
        history: list[HistoryTurn] = []
        for item in reversed(items):  # 古い順に並べ直す
            mode = item.get("response_mode", "full")
            # M2 までの記録には reply_text がないので、フィルタ後・下書きの順に使う
            reply = item.get("reply_text") or item.get("filtered_text") or item.get("draft_text")
            history.extend(history_entries(item.get("user_input", ""), reply, mode))
        return history

    async def load_turn_state(
        self, user_id: uuid.UUID, character_id: str, session_id: uuid.UUID, initial_relationship: float
    ) -> TurnState:
        async with self._db.scoped_transaction(Scope(user_id, character_id)) as conn:
            return await self._load_state(conn, user_id, character_id, session_id, initial_relationship)

    async def _load_state(
        self,
        conn: psycopg.AsyncConnection,
        user_id: uuid.UUID,
        character_id: str,
        session_id: uuid.UUID,
        initial_relationship: float,
    ) -> TurnState:
        """状態を読む。呼び出し側のトランザクション(スコープ設定済み)の中で実行する。"""
        key = (user_id, character_id)
        row = await (
            await conn.execute("SELECT value FROM relationships WHERE user_id = %s AND character_id = %s", key)
        ).fetchone()
        state = TurnState(relationship=float(row[0]) if row else initial_relationship)

        row = await (
            await conn.execute(
                "SELECT coalesce(sum(delta), 0) FROM relationship_events "
                "WHERE user_id = %s AND character_id = %s AND session_id = %s",
                (*key, session_id),
            )
        ).fetchone()
        state.session_delta = float(row[0]) if row else 0.0

        row = await (
            await conn.execute(
                "SELECT max(last_activity_at) FROM sessions "
                "WHERE user_id = %s AND character_id = %s AND session_id <> %s",
                (*key, session_id),
            )
        ).fetchone()
        state.last_interaction_at = row[0] if row else None

        # ユーザー単位の設定(RLS は user_id で制限する)
        row = await (
            await conn.execute("SELECT timezone FROM user_settings WHERE user_id = %s", (user_id,))
        ).fetchone()
        if row:
            state.timezone = row[0]

        row = await (
            await conn.execute(
                "SELECT vad_json, categories_json FROM user_state "
                "WHERE user_id = %s AND character_id = %s AND layer = 'session_mood' AND session_id = %s",
                (*key, session_id),
            )
        ).fetchone()
        if row:
            state.user_mood = EmotionVector.from_json(row[1], row[0])

        row = await (
            await conn.execute(
                "SELECT layer, vad_json, categories_json, last_triggers_json FROM companion_state "
                "WHERE user_id = %s AND character_id = %s AND layer IN ('instant', 'session_mood') AND session_id = %s",
                (*key, session_id),
            )
        ).fetchall()
        layers = {r[0]: r for r in row}
        # instant 層が、ターンをまたいで持ち越す内部状態 c。古い記録(instant がない)は、session_mood から読む
        current = layers.get("instant") or layers.get("session_mood")
        if current:
            state.companion = EmotionVector.from_json(current[2], current[1])
            triggers = current[3] if isinstance(current[3], dict) else {}
            state.previous_rule_indexes = {int(r["index"]) for r in triggers.get("rules", [])}
            state.event_session_total = {k: float(v) for k, v in (triggers.get("event_session_total") or {}).items()}
            state.last_negative_act_turn = triggers.get("last_negative_act_turn")
            sheet_state = triggers.get("sheet_state")
            state.sheet_state = sheet_state if isinstance(sheet_state, dict) else {}
            topic_state = triggers.get("topic_state")
            state.topic_state = topic_state if isinstance(topic_state, dict) else {}
            if "instant" in layers and "session_mood" in layers:
                mood = layers["session_mood"]
                state.companion_mood = EmotionVector.from_json(mood[2], mood[1])

        rows = await (
            await conn.execute(
                "SELECT rule_id, session_id, fired_count_session, saturation_n, last_fired_turn, last_fired_at, "
                "fired_day, fired_count_day FROM companion_rule_counters WHERE user_id = %s AND character_id = %s",
                key,
            )
        ).fetchall()
        state.rule_counters = {r[0]: RuleCounter(*r[1:]) for r in rows}

        row = await (
            await conn.execute(
                "SELECT turn_count, consecutive_silence FROM sessions WHERE session_id = %s", (session_id,)
            )
        ).fetchone()
        if row:
            state.turn_count, state.consecutive_silence = int(row[0]), int(row[1])

        row = await (
            await conn.execute(
                "SELECT window_started_at, count, stage, notice_issued_at, count_since_notice, cooldown_until "
                "FROM persona_pressure WHERE user_id = %s AND character_id = %s",
                key,
            )
        ).fetchone()
        if row:
            state.pressure = PressureState(*row)

        # ユーザーが望む接し方(§8.8.2)。記憶の検索(M4)とは別に、常に読む
        rows = await (
            await conn.execute(
                "SELECT summary, topics_json FROM memory WHERE user_id = %s AND character_id = %s "
                "AND kind = 'interaction_preference' AND deleted_at IS NULL AND NOT quarantined "
                "ORDER BY created_at",
                key,
            )
        ).fetchall()
        for summary, topics in rows:
            tag = topics[0] if isinstance(topics, list) and topics and isinstance(topics[0], dict) else {}
            state.preferences.append(
                {"element": tag.get("element"), "direction": int(tag.get("direction", 0)), "summary": summary}
            )
        return state

    async def commit_turn(
        self,
        user_id: uuid.UUID,
        character_id: str,
        session_id: uuid.UUID,
        initial_relationship: float,
        build: CommitBuilder,
    ) -> TurnCommit:
        scope = Scope(user_id, character_id)
        key = (user_id, character_id)
        async with self._db.scoped_transaction(scope) as conn:
            # 同じユーザー × キャラクタのターンを直列化する。最新の状態は、ロックの中で読み直す
            await lock_user_character(conn, scope)
            state = await self._load_state(conn, user_id, character_id, session_id, initial_relationship)
            commit = build(state)
            turn, emotion, update = commit.turn, commit.emotion, commit.state

            latest = [
                item
                async for item in self._cosmos.turns.query(
                    user_id,
                    character_id,
                    where="c.sessionId = @sessionId",
                    parameters={"sessionId": str(session_id)},
                    order_by="c.turnNo DESC",
                    limit=1,
                )
            ]
            turn_no = (latest[0].get("turnNo", 0) if latest else 0) + 1
            turn.turn_no = emotion.turn_no = turn_no

            # 関係値と、その変化の記録
            change = update.relationship
            await conn.execute(
                "INSERT INTO relationships (user_id, character_id, value, updated_at) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (user_id, character_id) DO UPDATE SET value = EXCLUDED.value, "
                "updated_at = EXCLUDED.updated_at",
                (*key, change.value_after, turn.ts),
            )
            if change.delta:
                await conn.execute(
                    "INSERT INTO relationship_events (user_id, character_id, session_id, turn_no, ts, delta, "
                    "value_after, reason_code, source_event_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        *key,
                        session_id,
                        turn_no,
                        turn.ts,
                        change.delta,
                        change.value_after,
                        "+".join(code for code, _ in change.reasons),
                        emotion.event_id,
                    ),
                )

            # ユーザーの感情状態(キャラクタごとに分離。§4.2)
            for layer, vector, layer_session in (
                ("instant", update.user_instant, None),
                ("session_mood", update.user_mood, session_id),
            ):
                await conn.execute(
                    "INSERT INTO user_state (user_id, character_id, layer, session_id, vad_json, "
                    "categories_json, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (user_id, character_id, layer) DO UPDATE SET session_id = EXCLUDED.session_id, "
                    "vad_json = EXCLUDED.vad_json, categories_json = EXCLUDED.categories_json, "
                    "updated_at = EXCLUDED.updated_at",
                    (*key, layer, layer_session, Jsonb(vector.vad), Jsonb(vector.categories), turn.ts),
                )

            # キャラクタの感情状態: instant(内部状態 c)と、session_mood(c の移動平均)
            for layer, vector in (("instant", update.companion), ("session_mood", update.companion_mood or update.companion)):
                await conn.execute(
                    "INSERT INTO companion_state (user_id, character_id, layer, session_id, vad_json, "
                    "categories_json, last_triggers_json, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (user_id, character_id, layer) DO UPDATE SET session_id = EXCLUDED.session_id, "
                    "vad_json = EXCLUDED.vad_json, categories_json = EXCLUDED.categories_json, "
                    "last_triggers_json = EXCLUDED.last_triggers_json, updated_at = EXCLUDED.updated_at",
                    (
                        *key,
                        layer,
                        session_id,
                        Jsonb(vector.vad),
                        Jsonb(vector.categories),
                        Jsonb(update.triggers),
                        turn.ts,
                    ),
                )
            # イベントルールのカウンタ(逓減と、回数の上限のためだけに使う)
            for rule_id, counter in update.rule_counters.items():
                await conn.execute(
                    "INSERT INTO companion_rule_counters (user_id, character_id, rule_id, session_id, "
                    "fired_count_session, saturation_n, last_fired_turn, last_fired_at, fired_day, fired_count_day) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (user_id, character_id, rule_id) DO UPDATE SET session_id = EXCLUDED.session_id, "
                    "fired_count_session = EXCLUDED.fired_count_session, saturation_n = EXCLUDED.saturation_n, "
                    "last_fired_turn = EXCLUDED.last_fired_turn, last_fired_at = EXCLUDED.last_fired_at, "
                    "fired_day = EXCLUDED.fired_day, fired_count_day = EXCLUDED.fired_count_day",
                    (
                        *key, rule_id, counter.session_id, counter.fired_count_session, counter.saturation_n,
                        counter.last_fired_turn, counter.last_fired_at, counter.fired_day, counter.fired_count_day,
                    ),
                )
            await conn.execute(
                "UPDATE sessions SET last_activity_at = %s, turn_count = turn_count + 1, "
                "consecutive_silence = %s, awaiting_reply_until = NULL WHERE session_id = %s",
                (turn.ts, min(2, update.consecutive_silence), session_id),
            )
            # ユーザーが話した: 返事のない自発発話は、なくなる(§15.1 の M8)
            await conn.execute(
                "UPDATE proactive_state SET unanswered = 0, updated_at = %s "
                "WHERE user_id = %s AND character_id = %s AND unanswered > 0",
                (turn.ts, *key),
            )
            if update.pressure is not None:
                await _save_pressure(conn, user_id, character_id, update.pressure, turn.ts)

            # Cosmos DB への書き込みは最後に行う(失敗したら、PostgreSQL の更新も取り消される)
            await self._cosmos.turns.put(user_id, character_id, _turn_item(turn))
            await self._cosmos.emotion_events.put(user_id, character_id, _emotion_item(emotion))
        return commit

    async def set_relationship(
        self, user_id: uuid.UUID, character_id: str, value: float, reason: str, now: datetime
    ) -> float:
        scope = Scope(user_id, character_id)
        value = max(-20.0, min(20.0, value))
        async with self._db.scoped_transaction(scope) as conn:
            await lock_user_character(conn, scope)
            row = await (
                await conn.execute(
                    "SELECT value FROM relationships WHERE user_id = %s AND character_id = %s",
                    (user_id, character_id),
                )
            ).fetchone()
            before = float(row[0]) if row else 0.0
            await conn.execute(
                "INSERT INTO relationships (user_id, character_id, value, updated_at) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (user_id, character_id) DO UPDATE SET value = EXCLUDED.value, "
                "updated_at = EXCLUDED.updated_at",
                (user_id, character_id, value, now),
            )
            await conn.execute(
                "INSERT INTO relationship_events (user_id, character_id, ts, delta, value_after, reason_code) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (user_id, character_id, now, value - before, value, reason),
            )
        return value

    async def mark_crisis(self, user_id: uuid.UUID, now: datetime, exempt_until: datetime) -> None:
        # キャラクタをまたぐ唯一の状態。時刻と期限だけを書く(§4.2、§13.4.6)
        async with self._db.scoped_transaction(Scope(user_id)) as conn:
            await conn.execute(
                "INSERT INTO user_safety_state (user_id, last_crisis_signal_at, exempt_until, updated_at) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (user_id) DO UPDATE SET last_crisis_signal_at = EXCLUDED.last_crisis_signal_at, "
                "exempt_until = GREATEST(user_safety_state.exempt_until, EXCLUDED.exempt_until), "
                "updated_at = EXCLUDED.updated_at",
                (user_id, now, exempt_until, now),
            )


    # ------------------------------------------------------------------
    # 無入力時の自発的な発話(§15.1 の M8)
    # ------------------------------------------------------------------

    async def proactive_snapshot(self, user_id: uuid.UUID, character_id: str, now: datetime) -> ProactiveSnapshot:
        key = (user_id, character_id)
        async with self._db.scoped_transaction(Scope(user_id, character_id)) as conn:

            async def one(sql: str, params: tuple[Any, ...]) -> tuple[Any, ...] | None:
                return await (await conn.execute(sql, params)).fetchone()

            relationship = await one("SELECT value FROM relationships WHERE user_id = %s AND character_id = %s", key)
            sessions = await one(
                "SELECT max(last_activity_at), bool_or(status = 'active') FROM sessions "
                "WHERE user_id = %s AND character_id = %s",
                key,
            )
            state = await one(
                "SELECT unanswered, local_day, sent_today FROM proactive_state WHERE user_id = %s AND character_id = %s",
                key,
            )
            settings = await one("SELECT timezone FROM user_settings WHERE user_id = %s", (user_id,))
            timezone = settings[0] if settings else "Asia/Tokyo"
            safety = await one("SELECT last_crisis_signal_at FROM user_safety_state WHERE user_id = %s", (user_id,))
            # 最後のセッションの、ユーザーの気分(session_mood は、最後のセッションの値だけが残っている)
            mood = await one(
                "SELECT vad_json, categories_json FROM user_state "
                "WHERE user_id = %s AND character_id = %s AND layer = 'session_mood'",
                key,
            )
        today = proactive_policy.local_now(now, timezone).date()
        return ProactiveSnapshot(
            relationship=float(relationship[0]) if relationship else None,
            last_activity_at=sessions[0] if sessions else None,
            session_active=bool(sessions[1]) if sessions else False,
            unanswered=int(state[0]) if state else 0,
            sent_today=int(state[2]) if state and state[1] == today else 0,
            last_crisis_at=safety[0] if safety else None,
            last_valence=float((mood[0] or {}).get("valence", 0.0)) if mood else None,
            last_anger=float((mood[1] or {}).get("anger", 0.0)) if mood else None,
            timezone=timezone,
        )

    async def record_proactive(
        self, user_id: uuid.UUID, character_id: str, record: TurnRecord, now: datetime, wait_until: datetime
    ) -> TurnRecord:
        scope = Scope(user_id, character_id)
        key = (user_id, character_id)
        async with self._db.scoped_transaction(scope) as conn:
            await lock_user_character(conn, scope)
            created = await (
                await conn.execute(
                    "INSERT INTO sessions (user_id, character_id, channel, started_at, last_activity_at, turn_count, "
                    "awaiting_reply_until) VALUES (%s, %s, 'proactive', %s, %s, 1, %s) RETURNING session_id",
                    (*key, now, now, wait_until),
                )
            ).fetchone()
            assert created is not None
            record.session_id, record.turn_no = created[0], 1
            settings = await (
                await conn.execute("SELECT timezone FROM user_settings WHERE user_id = %s", (user_id,))
            ).fetchone()
            today = proactive_policy.local_now(now, settings[0] if settings else "Asia/Tokyo").date()
            # 返事のない数と、このキャラクタのローカル日の回数(日が変われば 1 から数え直す)
            await conn.execute(
                "INSERT INTO proactive_state (user_id, character_id, unanswered, last_sent_at, last_session_id, "
                "last_turn_no, seen_at, local_day, sent_today, updated_at) VALUES (%s, %s, 1, %s, %s, 1, NULL, %s, 1, %s) "
                "ON CONFLICT (user_id, character_id) DO UPDATE SET unanswered = proactive_state.unanswered + 1, "
                "last_sent_at = EXCLUDED.last_sent_at, last_session_id = EXCLUDED.last_session_id, "
                "last_turn_no = 1, seen_at = NULL, "
                "sent_today = CASE WHEN proactive_state.local_day = EXCLUDED.local_day "
                "THEN proactive_state.sent_today + 1 ELSE 1 END, local_day = EXCLUDED.local_day, "
                "updated_at = EXCLUDED.updated_at",
                (*key, now, record.session_id, today, now),
            )
            # Cosmos DB への書き込みは最後に行う(失敗したら、PostgreSQL の更新も取り消される)
            await self._cosmos.turns.put(user_id, character_id, _turn_item(record))
        return record

    async def unseen_proactive(self, user_id: uuid.UUID, character_id: str) -> dict[str, Any] | None:
        async with self._db.scoped_transaction(Scope(user_id, character_id)) as conn:
            row = await (
                await conn.execute(
                    "SELECT last_session_id, last_turn_no FROM proactive_state "
                    "WHERE user_id = %s AND character_id = %s AND seen_at IS NULL AND last_session_id IS NOT NULL",
                    (user_id, character_id),
                )
            ).fetchone()
        if row is None:
            return None
        items = [
            item
            async for item in self._cosmos.turns.query(
                user_id, character_id, where="c.sessionId = @sessionId AND c.turnNo = @turnNo",
                parameters={"sessionId": str(row[0]), "turnNo": int(row[1] or 1)}, limit=1,
            )
        ]  # fmt: skip
        if not items:
            return None  # 会話の記録の保持期間が過ぎた、または削除された
        return {
            "session_id": row[0], "turn_no": int(row[1] or 1), "text": items[0].get("reply_text"),
            "ts": items[0].get("ts"),
        }  # fmt: skip

    async def mark_proactive_seen(
        self, user_id: uuid.UUID, character_id: str, session_id: uuid.UUID, now: datetime
    ) -> bool:
        async with self._db.scoped_transaction(Scope(user_id, character_id)) as conn:
            cursor = await conn.execute(
                "UPDATE proactive_state SET seen_at = %s, updated_at = %s "
                "WHERE user_id = %s AND character_id = %s AND last_session_id = %s AND seen_at IS NULL",
                (now, now, user_id, character_id, session_id),
            )
            return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # ユーザー単位の設定と状態(キャラクタをまたぐ。RLS は user_id で制限する)
    # ------------------------------------------------------------------

    async def load_user_context(self, user_id: uuid.UUID) -> UserContext:
        async with self._db.scoped_transaction(Scope(user_id)) as conn:
            return await self._user_context(conn, user_id)

    async def _user_context(self, conn: psycopg.AsyncConnection, user_id: uuid.UUID) -> UserContext:
        row = await (
            await conn.execute(
                "SELECT s.silence_enabled, s.filter_mode, s.silence_render, s.is_under_18, "
                "f.exempt_until, t.throttled_until, s.association_enabled, s.proactive_enabled FROM user_settings s "
                "LEFT JOIN user_safety_state f ON f.user_id = s.user_id "
                "LEFT JOIN user_throttle t ON t.user_id = s.user_id WHERE s.user_id = %s",
                (user_id,),
            )
        ).fetchone()
        if row is None:
            return UserContext()  # 設定がなければ、既定値(年齢フラグは安全側)
        return UserContext(
            silence_enabled=row[0],
            filter_mode=row[1],
            silence_render=row[2],
            is_under_18=row[3],
            exempt_until=row[4],
            throttled_until=row[5],
            association_enabled=row[6],
            proactive_enabled=row[7],
        )

    async def update_user_settings(self, user_id: uuid.UUID, changes: dict[str, Any]) -> UserContext:
        # 列名は、ここで決めた固定の一覧からだけ選ぶ(値は、パラメータで渡す)
        names = (
            "silence_enabled", "filter_mode", "silence_render", "is_under_18", "association_enabled", "proactive_enabled",
        )  # fmt: skip
        columns = [c for c in names if c in changes]
        async with self._db.scoped_transaction(Scope(user_id)) as conn:
            if columns:
                assignments = ", ".join(f"{c} = %s" for c in columns)
                if "is_under_18" in columns:
                    assignments += ", age_flag_source = 'self_declared', age_flag_updated_at = now()"
                await conn.execute(
                    f"UPDATE user_settings SET {assignments}, updated_at = now() WHERE user_id = %s",
                    (*(changes[c] for c in columns), user_id),
                )
            return await self._user_context(conn, user_id)

    async def hit_rate_limit(
        self, user_id: uuid.UUID, now: datetime, per_minute: int, per_day: int
    ) -> str | None:
        async with self._db.scoped_transaction(Scope(user_id)) as conn:
            exceeded: str | None = None
            counts: dict[str, tuple[datetime, int]] = {}
            for bucket, length, limit in (
                ("minute", timedelta(minutes=1), per_minute),
                ("day", timedelta(days=1), per_day),
            ):
                row = await (
                    await conn.execute(
                        "SELECT window_start, count FROM rate_limits WHERE user_id = %s AND bucket = %s FOR UPDATE",
                        (user_id, bucket),
                    )
                ).fetchone()
                start, count = (row[0], row[1]) if row and now - row[0] < length else (now, 0)
                if count >= limit:
                    exceeded = exceeded or bucket
                counts[bucket] = (start, count)
            if exceeded:
                return exceeded
            for bucket, (start, count) in counts.items():
                await conn.execute(
                    "INSERT INTO rate_limits (user_id, bucket, window_start, count) VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (user_id, bucket) DO UPDATE SET window_start = EXCLUDED.window_start, "
                    "count = EXCLUDED.count",
                    (user_id, bucket, start, count + 1),
                )
        return None

    async def register_serious_event(
        self, user_id: uuid.UUID, now: datetime, threshold: int, duration_minutes: int
    ) -> datetime | None:
        async with self._db.scoped_transaction(Scope(user_id)) as conn:
            await conn.execute(
                "INSERT INTO user_throttle (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING", (user_id,)
            )
            row = await (
                await conn.execute(
                    "SELECT serious_event_times FROM user_throttle WHERE user_id = %s FOR UPDATE", (user_id,)
                )
            ).fetchone()
            times = [datetime.fromisoformat(t) for t in (row[0] if row else [])]
            times = [t for t in times if now - t < timedelta(days=1)] + [now]
            until = now + timedelta(minutes=duration_minutes) if len(times) >= threshold else None
            if until is not None:
                times = []  # 制限をかけたら、数えなおす
            await conn.execute(
                "UPDATE user_throttle SET serious_event_times = %s, "
                "throttled_until = coalesce(%s, throttled_until), updated_at = %s WHERE user_id = %s",
                (Jsonb([t.isoformat() for t in times]), until, now, user_id),
            )
            return until

    async def clear_throttle(self, user_id: uuid.UUID) -> None:
        async with self._db.scoped_transaction(Scope(user_id)) as conn:
            await conn.execute(
                "UPDATE user_throttle SET throttled_until = NULL, updated_at = now() WHERE user_id = %s",
                (user_id,),
            )

    async def record_security_events(
        self,
        user_id: uuid.UUID,
        character_id: str,
        session_id: uuid.UUID | None,
        turn_id: str | None,
        findings: list[Finding],
        now: datetime,
    ) -> None:
        # 本文は保存しない。分類結果、種別、スコア、動作だけ(§13.5.7)
        for finding in findings:
            item = finding.as_item()
            item["details_json"] = item.pop("details")
            await self._cosmos.security_events.put(
                user_id,
                character_id,
                {
                    "sessionId": str(session_id) if session_id else None,
                    "turn_id": turn_id,
                    "ts": now.isoformat(),
                    **item,
                },
            )

    # ------------------------------------------------------------------
    # 人格への要求(§8.8)
    # ------------------------------------------------------------------

    async def save_pressure(
        self, user_id: uuid.UUID, character_id: str, pressure: PressureState, now: datetime
    ) -> None:
        scope = Scope(user_id, character_id)
        async with self._db.scoped_transaction(scope) as conn:
            await lock_user_character(conn, scope)
            await _save_pressure(conn, user_id, character_id, pressure, now)

    async def set_interaction_preferences(
        self, user_id: uuid.UUID, character_id: str, adjustments: list[Adjustment], now: datetime
    ) -> None:
        scope = Scope(user_id, character_id)
        async with self._db.scoped_transaction(scope) as conn:
            await lock_user_character(conn, scope)
            for item in adjustments:
                # 同じ要素の、前の希望は置き換える
                await conn.execute(
                    "UPDATE memory SET deleted_at = %s WHERE user_id = %s AND character_id = %s "
                    "AND kind = 'interaction_preference' AND deleted_at IS NULL "
                    "AND topics_json @> %s",
                    (now, user_id, character_id, Jsonb([{"element": item.element}])),
                )
                await conn.execute(
                    "INSERT INTO memory (user_id, character_id, tier, kind, summary, topics_json, importance, "
                    "source_day, created_at) VALUES (%s, %s, 'short', 'interaction_preference', %s, %s, 0.6, %s, %s)",
                    (
                        user_id,
                        character_id,
                        item.summary,
                        Jsonb([{"element": item.element, "direction": item.direction}]),
                        now.date(),
                        now,
                    ),
                )

    async def clear_interaction_preferences(self, user_id: uuid.UUID, character_id: str, now: datetime) -> None:
        async with self._db.scoped_transaction(Scope(user_id, character_id)) as conn:
            await conn.execute(
                "UPDATE memory SET deleted_at = %s WHERE user_id = %s AND character_id = %s "
                "AND kind = 'interaction_preference' AND deleted_at IS NULL",
                (now, user_id, character_id),
            )


async def _save_pressure(
    conn: psycopg.AsyncConnection, user_id: uuid.UUID, character_id: str, pressure: PressureState, now: datetime
) -> None:
    await conn.execute(
        "INSERT INTO persona_pressure (user_id, character_id, window_started_at, count, stage, notice_issued_at, "
        "count_since_notice, cooldown_until, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (user_id, character_id) DO UPDATE SET window_started_at = EXCLUDED.window_started_at, "
        "count = EXCLUDED.count, stage = EXCLUDED.stage, notice_issued_at = EXCLUDED.notice_issued_at, "
        "count_since_notice = EXCLUDED.count_since_notice, cooldown_until = EXCLUDED.cooldown_until, "
        "updated_at = EXCLUDED.updated_at",
        (
            user_id,
            character_id,
            pressure.window_started_at,
            pressure.count,
            max(1, min(4, pressure.stage)),
            pressure.notice_issued_at,
            pressure.count_since_notice,
            pressure.cooldown_until,
            now,
        ),
    )


def _turn_item(turn: TurnRecord) -> dict[str, Any]:
    data = asdict(turn)
    return {
        "id": data.pop("turn_id"),
        "sessionId": str(data.pop("session_id")),
        "turnNo": data.pop("turn_no"),
        "ts": data.pop("ts").isoformat(),
        **data,
    }


def _emotion_item(emotion: EmotionRecord) -> dict[str, Any]:
    data = asdict(emotion)
    estimate = data.pop("estimate_json")
    return {
        "id": data.pop("event_id"),
        "sessionId": str(data.pop("session_id")),
        "turnNo": data.pop("turn_no"),
        "ts": data.pop("ts").isoformat(),
        # §12 の列名に合わせて展開する
        "categories_json": estimate["categories"],
        "vad_json": estimate["vad"],
        "confidence": estimate["confidence"],
        "gap": estimate["gap"],
        "cause": estimate["cause"],
        "needs_json": estimate["needs"],
        "topics_json": estimate["topics"],
        "target": estimate["target"],
        "persona_request_json": estimate["persona_request"],
        "risk_json": estimate["risk"],
        "rule_hits_json": data.pop("rule_hits"),
        **data,
    }
