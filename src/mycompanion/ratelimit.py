"""回数の上限(ユーザー・運営者ごと。プロセスの中だけ)。"""

from __future__ import annotations

import time
import uuid
from collections import deque
from collections.abc import Callable


class SlidingWindowLimiter:
    """ユーザーごとの回数の上限(プロセスの中だけ。レプリカが増えれば、その数だけ緩くなる)。"""

    def __init__(self, limit: int, window_seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        self._hits: dict[uuid.UUID, deque[float]] = {}

    def allow(self, key: uuid.UUID) -> bool:
        now = self._clock()
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] >= self._window:
            hits.popleft()
        if len(hits) >= self._limit:
            return False
        hits.append(now)
        return True
