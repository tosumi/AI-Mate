"""実際の PostgreSQL で、Web Push の購読の保存を確かめるテスト(§15.1 の M8)。通知サービスには送らない。

    uv run pytest -m azure tests/live/test_push_live.py
"""

import base64
import os
import uuid
from datetime import UTC, datetime

import pytest

from mycompanion.config import PostgresConfig
from mycompanion.db.postgres import Database
from mycompanion.db.push_store import PgPushStore
from mycompanion.push.service import Subscription

pytestmark = pytest.mark.azure


@pytest.fixture
async def env():
    database = Database(PostgresConfig.from_env())
    await database.open(min_size=1, max_size=2)
    users = [await database.resolve_user("test", f"push-live-{uuid.uuid4()}") for _ in range(2)]
    try:
        yield PgPushStore(database), users
    finally:
        async with database.pool.connection() as conn:
            await conn.execute("RESET ROLE")
            for user_id in users:
                await conn.execute("DELETE FROM users WHERE user_id = %s", (user_id,))
            await conn.execute("SET ROLE mycompanion_app")
        await database.close()


def _subscription() -> Subscription:
    key = base64.urlsafe_b64encode(b"\x04" + os.urandom(64)).decode().rstrip("=")
    auth = base64.urlsafe_b64encode(os.urandom(16)).decode().rstrip("=")
    return Subscription(f"https://fcm.googleapis.com/fcm/send/live-{uuid.uuid4()}", key, auth)


async def test_subscriptions_are_isolated_and_move_with_the_browser(env):
    store, (first, second) = env
    now = datetime.now(UTC)
    device = _subscription()
    await store.add_subscription(first, device, now)
    assert await store.subscriptions(first) == [device]
    assert await store.subscriptions(second) == []  # ほかのユーザーからは見えない
    assert await store.remove_subscription(second, device.endpoint) is False  # ほかのユーザーは消せない

    # 同じブラウザで、別のアカウントが登録し直すと、そちらへ移る
    await store.add_subscription(second, device, now)
    assert (await store.subscription_count(first), await store.subscription_count(second)) == (0, 1)
    assert await store.remove_subscription(second, device.endpoint) is True
    assert await store.subscriptions(second) == []
