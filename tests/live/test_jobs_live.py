"""実際の Service Bus に接続するテスト(本体仕様書 §11.5.4、§15.1 の M6-1)。

`.env` を読み込み、`az login` 済みであること。既定では実行しない。
疎通確認のジョブ(`ping`)を、このテストだけのセッションに送り、同じセッションから受け取る。

    uv run pytest -m azure tests/live/test_jobs_live.py
"""

import os
import uuid

import pytest

from mycompanion.jobs.queue import ServiceBusJobQueue
from mycompanion.jobs.runner import JobResult
from mycompanion.jobs.types import Job

pytestmark = pytest.mark.azure


async def test_ping_job_round_trip_through_service_bus():
    from azure.identity.aio import DefaultAzureCredential

    credential = DefaultAzureCredential()
    queue = ServiceBusJobQueue(os.environ["SERVICEBUS_FULLY_QUALIFIED_NAMESPACE"], credential)
    job = Job("ping", user_id=uuid.uuid4())  # このテストだけのセッション
    received: list[Job] = []

    async def handler(got: Job, now) -> JobResult:
        received.append(got)
        return JobResult("done")

    try:
        assert await queue.send([job]) == 1
        handled = await queue.serve_one_session(handler, idle_seconds=20, session_id=job.queue_session)
        assert handled == 1 and received == [job]
    finally:
        await queue.close()
        await credential.close()
