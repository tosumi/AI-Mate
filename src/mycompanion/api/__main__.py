"""API をローカルで起動する。

    uv run python -m mycompanion.api [--port 8080]

Windows の既定のイベントループ(ProactorEventLoop)では、psycopg の非同期接続が動かない。
そのため、Windows では SelectorEventLoop を指定して起動する。
本番(Linux のコンテナ)は、Dockerfile のとおり uvicorn を直接起動する。
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="MyCompanion の API をローカルで起動する")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    config = uvicorn.Config(
        "mycompanion.api.app:create_app_from_env", factory=True, host=args.host, port=args.port
    )
    server = uvicorn.Server(config)
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    with asyncio.Runner(loop_factory=loop_factory) as runner:
        runner.run(server.serve())


if __name__ == "__main__":
    main()
