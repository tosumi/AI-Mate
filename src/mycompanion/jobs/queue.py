"""ジョブのキュー(本体仕様書 §11.5.4、§15.1 の M6-1)。

- `ServiceBusJobQueue`: Azure Service Bus のキュー(セッションつき)。セッション ID は `user_id` で、
  同じユーザーのジョブは順に処理される。認証は Entra ID(キーは使わない)。
- `InMemoryJobQueue`: 自動テスト用。セッションごとの順序だけを再現する。

失敗したジョブも、メッセージは完了にする。どのジョブも DB の状態(未整理の日、無入力のセッション、
保留中のアカウントの削除など)から作られるので、スケジューラが次の回に計画し直す(キューの再配信には頼らない)。
形の不正なメッセージは、デッドレターに移す(運営者が確認する)。
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime
from typing import Any, Protocol

from mycompanion.jobs.runner import JobResult
from mycompanion.jobs.types import Job, JobFormatError

logger = logging.getLogger(__name__)

QUEUE_NAME = "jobs"
Handler = Callable[[Job, datetime], Awaitable[JobResult]]


class JobQueue(Protocol):
    async def send(self, jobs: Iterable[Job]) -> int: ...

    async def close(self) -> None: ...


class InMemoryJobQueue:
    """メモリ上のキュー(テスト用)。セッションごとに順に取り出す。"""

    def __init__(self) -> None:
        self.sessions: OrderedDict[str, deque[str]] = OrderedDict()
        self.dead_letters: list[tuple[str, str]] = []

    async def send(self, jobs: Iterable[Job]) -> int:
        sent = 0
        for job in jobs:
            self.sessions.setdefault(job.queue_session, deque()).append(job.to_json())
            sent += 1
        return sent

    async def drain(self, handler: Handler, now: datetime | None = None) -> dict[str, int]:
        """空になるまで、セッションごとに順に処理する。"""
        counts = {"done": 0, "failed": 0, "dead_lettered": 0}
        while any(self.sessions.values()):
            for queue in list(self.sessions.values()):
                if not queue:
                    continue
                body = queue.popleft()
                try:
                    job = Job.from_json(body)
                except JobFormatError:
                    self.dead_letters.append((body, "invalid_format"))
                    counts["dead_lettered"] += 1
                    continue
                result = await handler(job, now or datetime.now(UTC))
                counts["failed" if result.status == "failed" else "done"] += 1
        return counts

    async def close(self) -> None:
        return None


class ServiceBusJobQueue:
    """Azure Service Bus のキュー。`credential` は azure.identity.aio の資格情報。"""

    def __init__(
        self, namespace: str, credential: Any, queue_name: str = QUEUE_NAME, owns_credential: bool = False
    ) -> None:
        from azure.servicebus.aio import ServiceBusClient

        self._client = ServiceBusClient(fully_qualified_namespace=namespace, credential=credential)
        self._queue = queue_name
        self._credential = credential if owns_credential else None

    async def send(self, jobs: Iterable[Job]) -> int:
        from azure.servicebus import ServiceBusMessage

        messages = [
            ServiceBusMessage(job.to_json(), session_id=job.queue_session, content_type="application/json", subject=job.kind)
            for job in jobs
        ]
        if not messages:
            return 0
        async with self._client.get_queue_sender(self._queue) as sender:
            batch = await sender.create_message_batch()
            for message in messages:
                try:
                    batch.add_message(message)
                except ValueError:  # バッチが一杯になったら送って、次のバッチへ
                    await sender.send_messages(batch)
                    batch = await sender.create_message_batch()
                    batch.add_message(message)
            await sender.send_messages(batch)
        return len(messages)

    async def serve_one_session(
        self, handler: Handler, idle_seconds: float = 30.0, session_id: str | None = None
    ) -> int:
        """空いているセッション(`session_id` を指定したら、そのセッション)を1つ受け取り、
        メッセージがなくなるまで処理する。処理した件数を返す。

        どのセッションにもメッセージがなければ、`idle_seconds` 待って 0 を返す。
        """
        from azure.servicebus import NEXT_AVAILABLE_SESSION
        from azure.servicebus.aio import AutoLockRenewer
        from azure.servicebus.exceptions import OperationTimeoutError

        handled = 0
        renewer = AutoLockRenewer(max_lock_renewal_duration=3600)  # 長い整理(数日分)でも、ロックを保つ
        try:
            receiver = self._client.get_queue_receiver(
                self._queue, session_id=session_id or NEXT_AVAILABLE_SESSION, max_wait_time=idle_seconds,
                auto_lock_renewer=renewer,
            )
            async with receiver:
                async for message in receiver:
                    try:
                        job = Job.from_json(b"".join(message.body))
                    except JobFormatError:
                        await receiver.dead_letter_message(message, reason="invalid_format")
                        continue
                    await handler(job, datetime.now(UTC))  # 失敗も完了にする(次の回に計画し直される)
                    await receiver.complete_message(message)
                    handled += 1
        except OperationTimeoutError:
            return 0  # 空いているセッションがない
        finally:
            await renewer.close()
        return handled

    async def drain(self, handler: Handler, concurrency: int = 4, idle_seconds: float = 10.0) -> int:
        """空くまで処理して、戻る(定期実行のジョブから使う)。処理した件数を返す。"""

        async def loop() -> int:
            total = 0
            while True:
                handled = await self.serve_one_session(handler, idle_seconds=idle_seconds)
                if handled == 0:
                    return total
                total += handled

        return sum(await asyncio.gather(*(loop() for _ in range(concurrency))))

    async def serve(self, handler: Handler, concurrency: int = 4, stop: asyncio.Event | None = None) -> None:
        """`concurrency` 個のセッションを並行して処理し続ける。"""
        stop = stop or asyncio.Event()

        async def loop() -> None:
            while not stop.is_set():
                try:
                    await self.serve_one_session(handler)
                except Exception as exc:  # noqa: BLE001 - 種類だけをログに出して続ける
                    logger.warning("キューの受信で失敗しました: %s", type(exc).__name__)
                    await asyncio.sleep(5)

        await asyncio.gather(*(loop() for _ in range(concurrency)))

    async def close(self) -> None:
        await self._client.close()
        if self._credential is not None:
            await self._credential.close()
