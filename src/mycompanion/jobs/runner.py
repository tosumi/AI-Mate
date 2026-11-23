"""ジョブの計画(スケジューラ)と実行(ワーカー)(本体仕様書 §9.4.2、§9.5、§11.5.4、§15.1 の M6-1)。

- `plan_due`: 期限の来た処理を、ジョブの一覧にする。DB を読むだけで、整理は実行しない。
- `JobRunner.run`: ジョブを1つ実行する。どのジョブも冪等で、失敗したら `failed` を返す(キューでは再配信に任せる)。

ログには、件数と種類だけを出す(記憶や会話の内容、ID は出さない)。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Literal

from mycompanion.config import EngineSettings
from mycompanion.jobs.types import Job
from mycompanion.llm.client import Persona
from mycompanion.memory import scoring
from mycompanion.memory.service import MemoryManager, previous_month
from mycompanion.proactive import policy as proactive_policy

logger = logging.getLogger(__name__)

PersonaLookup = Callable[[str], Awaitable[Persona | None]]
SensitiveLookup = Callable[[Any], Awaitable[bool]]  # user_id → センシティブな内容を残す同意があるか
IdleLister = Callable[[datetime], Awaitable[list[tuple[uuid.UUID, str, uuid.UUID]]]]
SessionCloser = Callable[[uuid.UUID, str, uuid.UUID, datetime], Awaitable[bool]]
AuditPurger = Callable[[datetime], Awaitable[int]]
IdentityDeleter = Callable[[str, datetime], Awaitable[bool]]  # 削除する。完全に消えて、記録を完了にしたら True
PendingIdentities = Callable[[], Awaitable[list[str]]]
# 自発発話の候補: 最後の会話がこの時刻より前の (user_id, character_id, timezone)。許可したユーザーだけ
ProactiveLister = Callable[[datetime], Awaitable[list[tuple[uuid.UUID, str, str]]]]
# 自発発話を1組で行う。結果の理由のコード(ok / quiet_hours / no_topic …)を返す
ProactiveSpeaker = Callable[[uuid.UUID, str, datetime], Awaitable[str]]


@dataclass
class JobResult:
    status: Literal["done", "failed", "skipped"]
    counts: dict[str, int] = field(default_factory=dict)


async def plan_due(
    manager: MemoryManager,
    settings: EngineSettings,
    now: datetime,
    idle_sessions: IdleLister | None = None,
    include_audit_purge: bool = False,
    pending_identities: PendingIdentities | None = None,
    proactive_candidates: ProactiveLister | None = None,
) -> list[Job]:
    """期限の来た処理を、ジョブの一覧にする。同じユーザーのジョブは、実行してほしい順に並べる。

    セッションの終了を先に置く(終了で書いた短期記憶を、同じ回の日次整理で扱えるように)。
    """
    jobs: list[Job] = []
    if idle_sessions is not None:
        cutoff = now - timedelta(minutes=settings.session_idle_timeout_minutes)
        for user_id, character_id, session_id in await idle_sessions(cutoff):
            jobs.append(Job("close_session", user_id=user_id, character_id=character_id, session_id=session_id))

    for target in await manager.store.consolidation_targets():
        if target.oldest_day is None:
            continue
        today = scoring.local_day(now, target.timezone, settings.memory_consolidation_time)
        # 「その日」がまだ終わっていない分(today)は、対象にしない
        if target.oldest_day < today:
            jobs.append(
                Job(
                    "consolidate_daily", user_id=target.user_id, character_id=target.character_id,
                    timezone=target.timezone, day=target.oldest_day.isoformat(),
                )
            )  # fmt: skip

    if settings.memory_monthly_summary_enabled:
        for scope in await manager.store.all_scopes():
            today = scoring.local_day(now, scope.timezone, settings.memory_consolidation_time)
            if today.day < settings.memory_monthly_day:
                continue
            period = previous_month(today)
            summaries = await manager.store.list(scope.user_id, scope.character_id, kind="monthly_summary")
            if any(m.period == period and not m.stale for m in summaries):
                continue
            jobs.append(
                Job(
                    "consolidate_monthly", user_id=scope.user_id, character_id=scope.character_id,
                    timezone=scope.timezone, period=period,
                )
            )  # fmt: skip

    if proactive_candidates is not None and settings.proactive_enabled:
        # 候補だけを絞る(最短の間隔より前に会話した組で、静穏時間帯でないもの)。細かい条件は、ジョブの中で判定する
        shortest = min(
            settings.proactive_base_hours * (1.0 - settings.proactive_relationship_boost),
            settings.proactive_repeat_base_hours,  # 2回目以降の最短(initiative = 1)
        )
        for user_id, character_id, timezone in await proactive_candidates(now - timedelta(hours=shortest)):
            if proactive_policy.in_quiet_hours(
                now, timezone, settings.proactive_quiet_start, settings.proactive_quiet_end
            ):
                continue
            jobs.append(Job("proactive", user_id=user_id, character_id=character_id, timezone=timezone))

    if include_audit_purge:
        jobs.append(Job("purge_audit_logs"))
    if pending_identities is not None:
        jobs += [Job("delete_identity", subject=subject) for subject in await pending_identities()]
    return jobs


class JobRunner:
    """ジョブを1つずつ実行する。必要な処理は、呼び出し側から関数で渡す(テストで差し替えるため)。"""

    def __init__(
        self,
        manager: MemoryManager,
        get_persona: PersonaLookup,
        settings: EngineSettings,
        close_session: SessionCloser | None = None,
        sensitive_allowed: SensitiveLookup | None = None,
        purge_audit_logs: AuditPurger | None = None,
        delete_identity: IdentityDeleter | None = None,
        proactive: ProactiveSpeaker | None = None,
    ) -> None:
        self._manager = manager
        self._get_persona = get_persona
        self._settings = settings
        self._close_session = close_session
        self._sensitive_allowed = sensitive_allowed
        self._purge = purge_audit_logs
        self._delete_identity = delete_identity
        self._proactive = proactive

    async def run(self, job: Job, now: datetime) -> JobResult:
        handler = getattr(self, f"_run_{job.kind}")
        return await handler(job, now)

    async def _run_ping(self, job: Job, now: datetime) -> JobResult:
        return JobResult("done")

    async def _run_close_session(self, job: Job, now: datetime) -> JobResult:
        if self._close_session is None:
            return JobResult("skipped")
        assert job.user_id and job.character_id and job.session_id
        closed = await self._close_session(job.user_id, job.character_id, job.session_id, now)
        return JobResult("done", {"sessions_closed": int(closed)})

    async def _run_consolidate_daily(self, job: Job, now: datetime) -> JobResult:
        assert job.user_id and job.character_id and job.day
        persona = await self._get_persona(job.character_id)
        if persona is None:
            return JobResult("skipped")
        timezone = job.timezone or "Asia/Tokyo"
        settings = self._settings
        today = scoring.local_day(now, timezone, settings.memory_consolidation_time)
        keep = await self._sensitive_allowed(job.user_id) if self._sensitive_allowed is not None else False
        day = max(date.fromisoformat(job.day), today - timedelta(days=settings.memory_catchup_max_days))
        counts = {"daily": 0, "daily_failed": 0}
        # 古い日から順に処理する。失敗したら、後の日を先に処理しない(次回に再試行する)
        while day < today:
            run = await self._manager.consolidate_daily(
                job.user_id, job.character_id, persona, day, now, keep_sensitive=keep, timezone=timezone
            )
            if run.status == "success":
                counts["daily"] += 1
            elif run.status == "failed":
                counts["daily_failed"] += 1
                return JobResult("failed", counts)
            day += timedelta(days=1)
        return JobResult("done", counts)

    async def _run_consolidate_monthly(self, job: Job, now: datetime) -> JobResult:
        assert job.user_id and job.character_id and job.period
        run = await self._manager.consolidate_monthly(job.user_id, job.character_id, job.period, now)
        if run.status == "failed":
            return JobResult("failed", {"monthly_failed": 1})
        await self._manager.expire(job.user_id, job.character_id, now)
        return JobResult("done", {"monthly": int(run.status == "success")})

    async def _run_purge_audit_logs(self, job: Job, now: datetime) -> JobResult:
        if self._purge is None:
            return JobResult("skipped")
        return JobResult("done", {"audit_logs_purged": await self._purge(now)})

    async def _run_delete_identity(self, job: Job, now: datetime) -> JobResult:
        if self._delete_identity is None:
            # 設定がないまま「完了」にしない(保留のまま残し、毎回の警告で、運営者が気づけるようにする)
            logger.warning("外部テナントのアカウントの削除が、設定されていません")
            return JobResult("failed", {"identity_failed": 1})
        try:
            finished = await self._delete_identity(job.subject or "", now)
        except Exception as exc:  # noqa: BLE001 - 種類だけをログに出し、次の回に再試行する
            logger.warning("外部テナントのアカウントの削除に失敗しました: %s", type(exc).__name__)
            return JobResult("failed", {"identity_failed": 1})
        if not finished:
            # ユーザーは消したが、ごみ箱への反映を待つ(保留のまま。次の回に完全に消して、完了にする)
            return JobResult("done", {"identity_deleted": 1})
        return JobResult("done", {"identity_completed": 1})

    async def _run_proactive(self, job: Job, now: datetime) -> JobResult:
        if self._proactive is None:
            return JobResult("skipped")
        assert job.user_id and job.character_id
        try:
            reason = await self._proactive(job.user_id, job.character_id, now)
        except Exception as exc:  # noqa: BLE001 - 種類だけをログに出す(次の回に、もう一度判定する)
            logger.warning("自発発話に失敗しました: %s", type(exc).__name__)
            return JobResult("failed", {"proactive_failed": 1})
        # 話しかけなかった理由も数える(頻度の調整に使う。ID は残さない)
        return JobResult("done", {"proactive_sent" if reason == "ok" else f"proactive_{reason}": 1})


def merge_counts(total: dict[str, int], result: JobResult) -> None:
    for name, value in result.counts.items():
        total[name] = total.get(name, 0) + value
