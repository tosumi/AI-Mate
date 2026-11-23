"""テスト全体の設定。"""

import asyncio
import sys


def pytest_asyncio_loop_factories(config, item):
    """Windows では SelectorEventLoop を使う。

    Windows の既定(ProactorEventLoop)では、psycopg の非同期接続が動かないため。
    本番(Linux のコンテナ)には影響しない。
    """
    if sys.platform == "win32":
        return {"selector": asyncio.SelectorEventLoop}
    return {"default": asyncio.new_event_loop}
