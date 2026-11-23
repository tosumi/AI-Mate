"""通知(Web Push)の試験: 最近登録された端末に、試験の通知を1回だけ送る(dev 専用。本体仕様書 §15.1 の M8)。

会話の内容は送らない(「通知の試験です」だけ)。登録の検索は管理者の接続で行い、ID・送り先は表示しない(件数だけ)。

    . .\\scripts\\load-env.ps1
    uv run python scripts/send_test_push.py              # 30分以内に登録された端末へ
    uv run python scripts/send_test_push.py --minutes 10
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime, timedelta

from mycompanion.config import PostgresConfig, environment_name
from mycompanion.db.postgres import Database
from mycompanion.db.push_store import PgPushStore
from mycompanion.push.service import PushService, keyvault_loader, push_subject


async def main() -> int:
    parser = argparse.ArgumentParser(description="試験の通知を、最近登録された端末へ送る(dev 専用)")
    parser.add_argument("--minutes", type=int, default=30, help="この分数以内に登録された端末へ送る")
    args = parser.parse_args()
    if environment_name() != "dev":
        print("エラー: dev でだけ使えます", file=sys.stderr)
        return 2
    vault_uri = (os.environ.get("KEY_VAULT_URI") or "").strip()
    origins = [o.strip() for o in (os.environ.get("MYCOMPANION_WEB_ORIGINS") or "").split(",") if o.strip()]
    subject = push_subject(origins) or "https://localhost"  # 試験の送信だけ。本番の送信は、ワーカーが画面の URL を使う

    database = Database(PostgresConfig.from_env())
    await database.open(min_size=1, max_size=2)
    try:
        since = datetime.now(UTC) - timedelta(minutes=args.minutes)
        async with database.pool.connection() as conn:
            await conn.execute("RESET ROLE")  # ユーザーをまたいで、最近の登録を探す(管理者の接続)
            rows = await (
                await conn.execute(
                    "SELECT DISTINCT user_id FROM push_subscriptions WHERE updated_at >= %s", (since,)
                )
            ).fetchall()
            await conn.execute("SET ROLE mycompanion_app")
        push = PushService(PgPushStore(database), keyvault_loader(vault_uri), subject)
        total = {"users": len(rows), "sent": 0, "gone": 0, "failed": 0}
        for (user_id,) in rows:
            counts = await push.notify(user_id, "MyCompanion", "通知の試験です(dev)")
            for name, value in counts.items():
                total[name] += value
        print("結果:", total)
    finally:
        await database.close()
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.exit(asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop))
    sys.exit(asyncio.run(main()))
