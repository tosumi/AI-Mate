"""記憶の整理のスケジューラ(本体仕様書 §9.4.2、§9.5、§11.5.4)。

毎時、次を検出して実行する。クライアント(CLI など)の起動や接続には依存しない。
  ① ローカルの整理時刻(既定は午前4時)を過ぎ、未整理の日がある(user, character)の日次整理
  ② 前月分の概要が未作成の(user, character)の月次整理
  ③ 無入力が続いたセッションの終了処理(短期記憶の書き込み)

止まっていて複数日たまった場合は、古い日から順に処理する(上限は `memory_catchup_max_days`)。
どの処理も冪等で、失敗したら次回に再試行する。

この関数は、1つのプロセスの中で順に実行する動作(`job_queue = inline`)。計画と実行は `mycompanion.jobs.runner` にあり、
Service Bus のキューを使う動作(§15.1 の M6-1)も、同じ計画と実行を使う。

    uv run python -m mycompanion.worker            # 毎時、繰り返す
    uv run python -m mycompanion.worker --once     # 1回だけ実行する
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from mycompanion.config import EngineSettings
from mycompanion.jobs.runner import JobRunner, PersonaLookup, SensitiveLookup, merge_counts, plan_due
from mycompanion.memory.service import MemoryManager

IdleCloser = Callable[[datetime], Awaitable[int]]


async def run_due(
    manager: MemoryManager,
    get_persona: PersonaLookup,
    settings: EngineSettings,
    now: datetime,
    close_idle: IdleCloser | None = None,
    sensitive_allowed: SensitiveLookup | None = None,
) -> dict[str, Any]:
    """期限の来た整理を、まとめて実行する。実行した件数を返す(内容は返さない)。"""
    counts = {"sessions_closed": 0, "daily": 0, "daily_failed": 0, "monthly": 0, "monthly_failed": 0}
    if close_idle is not None:
        counts["sessions_closed"] = await close_idle(now)
    runner = JobRunner(manager, get_persona, settings, sensitive_allowed=sensitive_allowed)
    for job in await plan_due(manager, settings, now):
        merge_counts(counts, await runner.run(job, now))
    return counts
