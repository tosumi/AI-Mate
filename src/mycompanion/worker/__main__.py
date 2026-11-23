"""ワーカーとスケジューラ(本体仕様書 §11.5.4、§15.1 の M6-1)。

期限の来た処理(無入力のセッションの終了と短期記憶の書き込み、日次整理、月次整理、期限切れの削除、
監査ログの期限切れの削除、外部テナントのアカウントの削除、無入力時の自発的な発話)を、ジョブとして実行する。動かし方は、環境変数 `MYCOMPANION_JOB_QUEUE` で選ぶ。

- `inline`(既定。ローカルの開発): 1つのプロセスの中で、計画して、そのまま順に実行する。
- `servicebus`(Container Apps): スケジューラが計画してキューに入れ、ワーカーがキューから受け取って実行する。
  dev(v1.0)では、Container Apps のジョブ(毎時)が `cycle` で、計画 → キュー → 空くまで処理、をまとめて行う
  (常駐のワーカーは置かない。本体仕様書 §15.1 の M6-4)。

    uv run python -m mycompanion.worker              # inline: 繰り返す / servicebus: キューを処理し続ける
    uv run python -m mycompanion.worker --once       # inline: 1回だけ実行する
    uv run python -m mycompanion.worker schedule     # servicebus: 計画してキューに入れる(1回)
    uv run python -m mycompanion.worker cycle        # servicebus: 計画してキューに入れ、空くまで処理して終わる

ログには、件数と種類だけを出す(記憶や会話の内容、ID は出さない)。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import sys
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime

from mycompanion import config as cfg
from mycompanion import telemetry
from mycompanion.account.identity import directory_from_env
from mycompanion.account.service import AccountOps
from mycompanion.account.store import PgAccountStore
from mycompanion.api.app import _web_origins as web_origins
from mycompanion.db.cosmos import open_cosmos
from mycompanion.db.memory_store import PgMemoryStore
from mycompanion.db.postgres import Database
from mycompanion.db.push_store import PgPushStore
from mycompanion.db.stores import PgCosmosStores
from mycompanion.db.topic_store import PgTopicStore
from mycompanion.engine.engine import Engine
from mycompanion.jobs.queue import ServiceBusJobQueue
from mycompanion.jobs.runner import JobResult, JobRunner, merge_counts, plan_due
from mycompanion.jobs.types import Job
from mycompanion.llm.azure_openai import AzureOpenAILLMClient
from mycompanion.memory.service import MemoryManager
from mycompanion.push.service import PushService, keyvault_loader, push_subject

logger = logging.getLogger("mycompanion.worker")


@dataclass
class Context:
    settings: cfg.EngineSettings
    manager: MemoryManager
    memory_store: PgMemoryStore
    account: AccountOps
    runner: JobRunner


@contextlib.asynccontextmanager
async def open_context() -> AsyncIterator[Context]:
    settings = cfg.EngineSettings()
    database = Database(cfg.PostgresConfig.from_env())
    await database.open(min_size=1, max_size=4)
    cosmos_handle, cosmos = open_cosmos(
        cfg.CosmosConfig.from_env(), settings.turns_retention_days, settings.security_events_retention_days
    )
    llm = AzureOpenAILLMClient(cfg.AzureOpenAIConfig.from_env(), settings)
    # 外部テナントのアカウントの削除(設定がなければ None。削除は保留のまま残る。§15.1 の M6-3)
    directory = directory_from_env()
    try:
        stores = PgCosmosStores(database, cosmos, settings)
        memory_store = PgMemoryStore(database, settings)
        topic_store = PgTopicStore(database, settings)
        manager = MemoryManager(memory_store, llm, settings, log=stores, topic_store=topic_store)
        account = AccountOps(PgAccountStore(database, cosmos, memory_store, topic_store))

        async def close_session(user_id: uuid.UUID, character_id: str, session_id: uuid.UUID, now: datetime) -> bool:
            """無入力が続いたセッションを終了し、会話から短期記憶を書く(冪等。終了済みなら何もしない)。"""
            persona = await stores.get_character(character_id)
            if persona is None:
                return False
            if not await stores.close_session_by_id(user_id, character_id, session_id, "idle_timeout", now):
                return False
            timezone = (await stores.load_turn_state(user_id, character_id, session_id, 0.0)).timezone
            # 記憶の保存に同意していないユーザーの会話は、記憶にしない
            if (await account.status(user_id))["memory"]:
                await manager.write_session(user_id, character_id, persona, session_id, now, timezone)
            return True

        engine = Engine(llm, stores, settings, manager)
        # 通知(Web Push)。VAPID の秘密鍵は Key Vault から読む(設定がなければ、通知は送らない)
        vault_uri = (os.environ.get("KEY_VAULT_URI") or "").strip()
        push = (
            PushService(PgPushStore(database), keyvault_loader(vault_uri), push_subject(web_origins()))
            if vault_uri
            else None
        )

        async def proactive(user_id: uuid.UUID, character_id: str, now: datetime) -> str:
            """自発発話を1組で判定し、条件を満たせば話しかけて、通知を送る(§15.1 の M8)。理由のコードを返す。"""
            consented = not (await account.status(user_id))["missing"]
            result = await engine.proactive_turn(user_id, character_id, consented=consented, now=now)
            if result.sent and push is not None:
                persona = await stores.get_character(character_id)
                try:
                    # 通知の文面には、会話の内容を入れない(キャラクタ名と、メッセージがあることだけ)
                    counts = await push.notify(user_id, "MyCompanion", f"{persona.name if persona else 'キャラクタ'}からメッセージがあります")
                    logger.info("通知: %s", counts)
                except Exception as exc:  # noqa: BLE001 - 通知に失敗しても、話しかけたことは取り消さない
                    logger.warning("通知に失敗しました: %s", type(exc).__name__)
            return result.reason

        async def purge(now: datetime) -> int:
            # 保持期間を過ぎた監査ログを消す(§13.4.5)
            return await account.purge_audit_logs(now, settings.audit_log_retention_days)

        async def delete_identity(subject: str, now: datetime) -> bool:
            assert directory is not None
            if not await directory.delete_user(subject):
                return False  # ごみ箱への反映を待つ(次の回に確かめる)
            await account.complete_identity_deletion(subject, now)
            return True

        runner = JobRunner(
            manager, stores.get_character, settings,
            close_session=close_session, sensitive_allowed=account.sensitive_allowed, purge_audit_logs=purge,
            delete_identity=delete_identity if directory is not None else None, proactive=proactive,
        )  # fmt: skip
        yield Context(settings, manager, memory_store, account, runner)
    finally:
        if directory is not None:
            await directory.close()
        await llm.close()
        await cosmos_handle.close()
        await database.close()


async def plan(ctx: Context, now: datetime) -> list[Job]:
    return await plan_due(
        ctx.manager, ctx.settings, now, ctx.memory_store.idle_sessions,
        include_audit_purge=True, pending_identities=ctx.account.pending_identity_deletions,
        proactive_candidates=ctx.memory_store.proactive_candidates,
    )  # fmt: skip


async def run_measured(ctx: Context, job: Job, now: datetime) -> JobResult:
    """ジョブを実行し、種類・結果・件数・所要時間を記録する(ID は記録しない)。"""
    started = time.perf_counter()
    result = await ctx.runner.run(job, now)
    telemetry.record_job(job.kind, result.status, time.perf_counter() - started, result.counts)
    logger.info("ジョブ %s: %s %s", job.label, result.status, result.counts)
    return result


async def run_inline(once: bool) -> int:
    async with open_context() as ctx:
        while True:
            now = datetime.now(UTC)
            counts: dict[str, int] = {}
            failed = 0
            for job in await plan(ctx, now):
                result = await run_measured(ctx, job, now)
                merge_counts(counts, result)
                failed += result.status == "failed"
            logger.info("実行しました: %s", counts)
            if once:
                return 1 if failed else 0
            await asyncio.sleep(ctx.settings.memory_scheduler_scan_interval_minutes * 60)


def _servicebus_queue() -> ServiceBusJobQueue:
    from azure.identity.aio import DefaultAzureCredential

    namespace = os.environ.get("SERVICEBUS_FULLY_QUALIFIED_NAMESPACE", "").strip()
    if not namespace:
        raise cfg.ConfigError("Service Bus の設定が不足しています: SERVICEBUS_FULLY_QUALIFIED_NAMESPACE")
    return ServiceBusJobQueue(namespace, DefaultAzureCredential(), owns_credential=True)


async def run_schedule() -> int:
    """計画してキューに入れる(Container Apps のジョブから、毎時1回)。"""
    queue = _servicebus_queue()
    try:
        async with open_context() as ctx:
            jobs = await plan(ctx, datetime.now(UTC))
            sent = await queue.send(jobs)
            kinds: dict[str, int] = {}
            for job in jobs:
                kinds[job.kind] = kinds.get(job.kind, 0) + 1
            logger.info("キューに入れました: %d件 %s", sent, kinds)
            return 0
    finally:
        await queue.close()


async def run_worker(drain: bool = False, schedule_first: bool = False) -> int:
    """キューから受け取って実行する。`drain` なら、空いたら終わる。`schedule_first` なら、先に計画して入れる。"""
    queue = _servicebus_queue()
    concurrency = int(os.environ.get("MYCOMPANION_WORKER_CONCURRENCY", "4") or "4")
    try:
        async with open_context() as ctx:

            async def handle(job: Job, now: datetime) -> JobResult:
                return await run_measured(ctx, job, now)

            if schedule_first:
                jobs = await plan(ctx, datetime.now(UTC))
                logger.info("キューに入れました: %d件", await queue.send(jobs))
            if drain:
                logger.info("処理しました: %d件", await queue.drain(handle, concurrency=concurrency))
            else:
                await queue.serve(handle, concurrency=concurrency)
            return 0
    finally:
        await queue.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="MyCompanion のワーカーとスケジューラ")
    parser.add_argument("command", nargs="?", choices=["run", "schedule", "cycle"], default="run")
    parser.add_argument("--once", action="store_true", help="inline: 1回だけ実行して終了する")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    telemetry.quiet_sdk_logs()
    telemetry.configure("mycompanion-worker")
    mode = (os.environ.get("MYCOMPANION_JOB_QUEUE") or "inline").strip()
    if mode not in ("inline", "servicebus"):
        print("エラー: MYCOMPANION_JOB_QUEUE は inline か servicebus です", file=sys.stderr)
        return 2
    if args.command == "schedule":
        coroutine = run_schedule() if mode == "servicebus" else run_inline(once=True)
    elif args.command == "cycle":
        coroutine = run_worker(drain=True, schedule_first=True) if mode == "servicebus" else run_inline(once=True)
    else:
        coroutine = run_worker() if mode == "servicebus" else run_inline(args.once)
    try:
        if sys.platform == "win32":
            # Windows では、psycopg の非同期接続に SelectorEventLoop が必要
            return asyncio.run(coroutine, loop_factory=asyncio.SelectorEventLoop)
        return asyncio.run(coroutine)
    except cfg.ConfigError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0
    finally:
        telemetry.flush()  # 定期ジョブは、すぐ終わるので、送り残しを送ってから終える


if __name__ == "__main__":
    sys.exit(main())
