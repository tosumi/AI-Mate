# API・ワーカー用のコンテナイメージ(本体仕様書 §11.5.2)
# 構成情報は、実行時の環境変数で渡す。イメージには含めない(§11.5.9)。
FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
# デザイナのサンプル(読み取り専用の参考。§15.1 の M10)
COPY personas ./personas
RUN uv sync --frozen --no-dev

FROM python:3.13-slim
# root で動かさない
RUN useradd --create-home --uid 10001 app
WORKDIR /app
COPY --from=build --chown=app:app /app /app
USER app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["uvicorn", "mycompanion.api.app:create_app_from_env", "--factory", "--host", "0.0.0.0", "--port", "8080"]
