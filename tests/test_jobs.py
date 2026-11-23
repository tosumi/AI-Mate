"""ジョブのキューのテスト(本体仕様書 §11.5.4、§15.1 の M6-1)。"""

import uuid
from datetime import date, timedelta

import pytest

from mycompanion.jobs.queue import InMemoryJobQueue
from mycompanion.jobs.runner import JobResult, JobRunner, plan_due
from mycompanion.jobs.types import Job, JobFormatError
from tests.test_memory import NOW, SETTINGS, TODAY, USER_A, Env

SESSION = uuid.uuid4()


def test_job_round_trip_carries_only_ids():
    job = Job("consolidate_daily", user_id=USER_A, character_id="char_1", timezone="Asia/Tokyo", day="2026-11-13")
    assert Job.from_json(job.to_json()) == job
    assert job.queue_session == str(USER_A)
    assert set(__import__("json").loads(job.to_json())) == {"v", "kind", "user_id", "character_id", "timezone", "day"}
    assert Job("purge_audit_logs").queue_session == "system"


@pytest.mark.parametrize(
    "text",
    ['{"v":1,"kind":"rm -rf"}', '{"v":2,"kind":"ping"}', '{"v":1,"kind":"close_session","user_id":"x"}', "not json",
     '{"v":1,"kind":"consolidate_daily","user_id":"' + str(uuid.uuid4()) + '","character_id":"c"}',
     '{"v":1,"kind":"delete_identity"}'],
)  # fmt: skip
def test_invalid_messages_are_rejected(text):
    with pytest.raises(JobFormatError):
        Job.from_json(text)


async def _env_with_backlog() -> Env:
    env = Env()
    for days_ago in (3, 1, 0):
        await env.add(f"{days_ago}日前の出来事。猫の話", "short", source_day=TODAY - timedelta(days=days_ago))
    await env.add("10月の出来事", source_day=date(2026, 10, 5))
    return env


async def test_plan_puts_session_close_first_and_skips_today():
    env = await _env_with_backlog()

    async def idle(cutoff):
        return [(USER_A, "char_1", SESSION)]

    jobs = await plan_due(env.manager, SETTINGS, NOW, idle, include_audit_purge=True)
    assert [j.kind for j in jobs] == ["close_session", "consolidate_daily", "consolidate_monthly", "purge_audit_logs"]
    assert jobs[1].day == (TODAY - timedelta(days=3)).isoformat() and jobs[2].period == "2026-10"
    # 同じユーザーのジョブは、同じセッション(ユーザーごとに順に処理される)
    assert {j.queue_session for j in jobs[:3]} == {str(USER_A)}


async def test_queue_runs_the_same_work_as_inline_and_is_idempotent():
    env = await _env_with_backlog()
    runner = JobRunner(env.manager, env.stores.get_character, SETTINGS)
    queue = InMemoryJobQueue()
    await queue.send(await plan_due(env.manager, SETTINGS, NOW))
    counts = await queue.drain(runner.run, NOW)
    assert counts == {"done": 2, "failed": 0, "dead_lettered": 0}
    targets = [r.target for r in await env.mstore.runs(USER_A, "char_1", "daily")]
    assert targets == [(TODAY - timedelta(days=3)).isoformat(), (TODAY - timedelta(days=1)).isoformat()]
    # 計画し直しても、残っているのは今日の分だけなので、日次・月次のジョブは出ない
    assert await plan_due(env.manager, SETTINGS, NOW) == []


async def test_failures_are_not_redelivered_but_replanned():
    queue = InMemoryJobQueue()
    calls: list[str] = []

    async def handler(job, now):
        calls.append(job.kind)
        return JobResult("failed")

    await queue.send([Job("purge_audit_logs"), Job("delete_identity", subject="oid-1")])
    counts = await queue.drain(handler, NOW)
    # 失敗しても、メッセージは1回で終わる(DB の状態から、次の回に計画し直される)
    assert calls == ["purge_audit_logs", "delete_identity"] and counts == {"done": 0, "failed": 2, "dead_lettered": 0}


async def test_invalid_message_goes_to_dead_letter_and_unset_identity_deleter_fails():
    queue = InMemoryJobQueue()
    queue.sessions["system"] = __import__("collections").deque(['{"v":1,"kind":"nope"}'])
    env = Env()
    runner = JobRunner(env.manager, env.stores.get_character, SETTINGS)
    counts = await queue.drain(runner.run, NOW)
    assert counts["dead_lettered"] == 1 and queue.dead_letters[0][1] == "invalid_format"
    # 削除の設定がないまま「完了」にしない
    assert (await runner.run(Job("delete_identity", subject="oid-1"), NOW)).status == "failed"
    assert (await runner.run(Job("ping"), NOW)).status == "done"


# --- 外部テナントのアカウントの削除(§15.1 の M6-3) ---------------------------------


async def test_account_deletion_leaves_a_pending_identity_deletion_until_the_worker_completes_it():
    from mycompanion.account.service import AccountOps
    from mycompanion.account.store import InMemoryAccountStore

    env = Env()
    store = InMemoryAccountStore(env.stores, env.mstore)
    ops = AccountOps(store)
    subject = str(uuid.uuid4())
    store.identity_subjects[USER_A] = [subject]
    await env.stores.set_relationship(USER_A, "char_1", 1.0, "test", NOW)
    await ops.delete(USER_A)

    jobs = await plan_due(env.manager, SETTINGS, NOW, pending_identities=ops.pending_identity_deletions)
    assert jobs == [Job("delete_identity", subject=subject)] and jobs[0].queue_session == "system"

    deleted: list[str] = []
    fail = {"on": True}

    async def delete_identity(got: str, now) -> bool:
        if fail["on"]:
            raise RuntimeError("HTTP 503")
        deleted.append(got)
        if len(deleted) == 1:
            return False  # ユーザーを消した回(ごみ箱への反映を待つ)
        await ops.complete_identity_deletion(got, now)
        return True

    runner = JobRunner(env.manager, env.stores.get_character, SETTINGS, delete_identity=delete_identity)
    assert (await runner.run(jobs[0], NOW)).status == "failed"
    assert await ops.pending_identity_deletions() == [subject]  # 失敗したら、保留のまま(次の回に再試行)
    fail["on"] = False
    first = await runner.run(jobs[0], NOW)
    assert first.counts == {"identity_deleted": 1} and await ops.pending_identity_deletions() == [subject]
    second = await runner.run(jobs[0], NOW)
    assert second.counts == {"identity_completed": 1} and await ops.pending_identity_deletions() == []


class _FakeCredential:
    async def get_token(self, *scopes):
        return type("T", (), {"token": "t0ken"})()

    async def close(self):
        return None


async def test_graph_directory_deletes_the_user_and_the_recycle_bin_entry_and_accepts_404():
    import httpx

    from mycompanion.account.identity import GraphIdentityDirectory, directory_from_env

    seen: list[tuple[str, str]] = []
    status = {"users": 204, "deletedItems": 404}

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        assert request.headers["Authorization"] == "Bearer t0ken"
        return httpx.Response(status["users" if "/users/" in request.url.path else "deletedItems"])

    directory = GraphIdentityDirectory(_FakeCredential(), httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    oid = str(uuid.uuid4())
    # ユーザーを消した回(204)は、ごみ箱が 404 でも、まだ完了にしない
    assert await directory.delete_user(oid) is False
    assert seen == [("DELETE", f"/v1.0/users/{oid}"), ("DELETE", f"/v1.0/directory/deletedItems/{oid}")]
    # 次の回: ユーザーは無く(404)、ごみ箱から消せた(204)→ 完了
    status["users"], status["deletedItems"] = 404, 204
    assert await directory.delete_user(oid) is True
    status["users"] = 403
    with pytest.raises(RuntimeError, match="HTTP 403"):
        await directory.delete_user(oid)
    with pytest.raises(ValueError):
        await directory.delete_user("../me")  # GUID 以外は、URL に入れない
    await directory.close()
    # 設定が足りなければ、作らない
    assert directory_from_env({"ENTRA_EXTERNAL_TENANT_ID": "t"}) is None
