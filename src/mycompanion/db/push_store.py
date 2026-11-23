"""Web Push の購読の保存先(PostgreSQL。ユーザー単位の行レベルセキュリティ。本体仕様書 §15.1 の M8)。"""

from __future__ import annotations

import uuid
from datetime import datetime

from mycompanion.db.postgres import Database, Scope
from mycompanion.push.service import Subscription


class PgPushStore:
    def __init__(self, database: Database) -> None:
        self._db = database

    async def add_subscription(self, user_id: uuid.UUID, subscription: Subscription, now: datetime) -> None:
        async with self._db.scoped_transaction(Scope(user_id)) as conn:
            await conn.execute(
                "SELECT register_my_push_subscription(%s, %s, %s, %s)",
                (subscription.endpoint, subscription.p256dh, subscription.auth, now),
            )

    async def remove_subscription(self, user_id: uuid.UUID, endpoint: str) -> bool:
        async with self._db.scoped_transaction(Scope(user_id)) as conn:
            cursor = await conn.execute(
                "DELETE FROM push_subscriptions WHERE user_id = %s AND endpoint = %s", (user_id, endpoint)
            )
            return cursor.rowcount > 0

    async def subscriptions(self, user_id: uuid.UUID) -> list[Subscription]:
        async with self._db.scoped_transaction(Scope(user_id)) as conn:
            rows = await (
                await conn.execute(
                    "SELECT endpoint, p256dh, auth FROM push_subscriptions WHERE user_id = %s ORDER BY created_at",
                    (user_id,),
                )
            ).fetchall()
        return [Subscription(*row) for row in rows]

    async def subscription_count(self, user_id: uuid.UUID) -> int:
        async with self._db.scoped_transaction(Scope(user_id)) as conn:
            row = await (
                await conn.execute("SELECT count(*) FROM push_subscriptions WHERE user_id = %s", (user_id,))
            ).fetchone()
        return int(row[0]) if row else 0
