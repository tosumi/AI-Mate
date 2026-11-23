"""計測(OpenTelemetry → Application Insights。本体仕様書 §11.5.7、§15.1 の M6-2)。

送るもの: ターン ID、`prompt_version`、応答モード、段ごとの所要時間、LLM の呼び出し回数とトークン数、
HTTP の経路(テンプレート)・状態・所要時間、ジョブの種類・結果・件数・所要時間、エラーの種類、
件数だけのログ(`mycompanion` のロガー)。

送らないもの: 会話・記憶の本文、プロンプト、`user_id`・メールアドレス・Entra のサブジェクト、IP アドレス、
URL の実際の値(ID を含む)、環境変数の値。そのため、ライブラリの自動計測(HTTP・DB・Azure SDK)は、すべて無効にし、
ここで決めた項目だけを、手で記録する。

接続文字列(`APPLICATIONINSIGHTS_CONNECTION_STRING`)がなければ、何も送らない(ローカル・自動テスト)。
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import Any

from opentelemetry import metrics, trace

logger = logging.getLogger(__name__)

# ライブラリの自動計測は、すべて無効にする(URL・SQL・IP アドレスを送らないため)
_DISABLED = ("azure_sdk", "django", "fastapi", "flask", "psycopg2", "requests", "urllib", "urllib3")

_meter = metrics.get_meter("mycompanion")
_tracer = trace.get_tracer("mycompanion")
_stage_seconds = _meter.create_histogram("mycompanion.turn.stage_seconds", unit="s", description="ターンの段ごとの所要時間")
_turns = _meter.create_counter("mycompanion.turns", description="ターンの数(応答モード別)")
_llm_calls = _meter.create_counter("mycompanion.llm.calls", description="LLM の呼び出し(用途・結果別)")
_llm_tokens = _meter.create_counter("mycompanion.llm.tokens", unit="token", description="LLM のトークン数")
_http_seconds = _meter.create_histogram("mycompanion.http.seconds", unit="s", description="API の所要時間(経路別)")
_job_seconds = _meter.create_histogram("mycompanion.job.seconds", unit="s", description="ジョブの所要時間")
_job_items = _meter.create_counter("mycompanion.job.items", description="ジョブで処理した件数(種類別)")

# 属性として許す値(これ以外のキーは、送る前に落とす)
_TURN_KEYS = ("turn_id", "prompt_version", "response_mode", "filter_mode", "env")


def configure(service: str, env: Mapping[str, str] | None = None) -> bool:
    """計測を有効にする。接続文字列がなければ何もしない。有効にしたら True。"""
    env = os.environ if env is None else env
    connection = (env.get("APPLICATIONINSIGHTS_CONNECTION_STRING") or "").strip()
    if not connection:
        return False
    from azure.monitor.opentelemetry import configure_azure_monitor
    from opentelemetry.sdk.resources import Resource

    configure_azure_monitor(
        connection_string=connection,
        logger_name="mycompanion",  # 自分のロガーだけを送る(件数と種類だけを出している)
        instrumentation_options={name: {"enabled": False} for name in _DISABLED},
        enable_live_metrics=False,
        resource=Resource.create(
            {"service.name": service, "deployment.environment": (env.get("MYCOMPANION_ENV") or "dev").strip()}
        ),
    )
    logger.info("計測を有効にしました: %s", service)
    return True


def flush(timeout_millis: int = 10_000) -> None:
    """送り残しを送る(定期ジョブのように、すぐ終わるプロセスの最後に呼ぶ)。"""
    for provider in (metrics.get_meter_provider(), trace.get_tracer_provider()):
        force = getattr(provider, "force_flush", None)
        if force is not None:
            try:
                force(timeout_millis)
            except Exception as exc:  # noqa: BLE001 - 送れなくても、処理の結果は変えない
                logger.warning("計測の送信に失敗しました: %s", type(exc).__name__)


def quiet_sdk_logs() -> None:
    """Azure SDK と HTTP ライブラリの詳細ログ(ヘッダー、URL など)を、警告以上に絞る。"""
    # httpx は、URL(オブジェクト ID を含みうる)を INFO で出すので、これも絞る
    for name in ("azure", "azure.core.pipeline.policies.http_logging_policy", "azure.servicebus", "uamqp", "httpx"):
        logging.getLogger(name).setLevel(logging.WARNING)


def _clean(attributes: Mapping[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {k: v for k, v in attributes.items() if k in keys and isinstance(v, (str, int, float, bool))}


def record_turn(latency: Mapping[str, float], attributes: Mapping[str, Any]) -> None:
    """1ターンの所要時間と応答モードを記録する。本文・ID(ターン ID 以外)は受け取らない。"""
    attrs = _clean(attributes, _TURN_KEYS)
    common = {k: attrs[k] for k in ("response_mode", "filter_mode") if k in attrs}
    for stage, seconds in latency.items():
        _stage_seconds.record(float(seconds), {**common, "stage": stage.removesuffix("_sec")})
    _turns.add(1, common)
    span = _tracer.start_span("turn", attributes=attrs)
    span.end()


def record_llm(purpose: str, outcome: str, usage: Any = None) -> None:
    """LLM の呼び出しを数える。`usage` は、応答の usage(入力・出力のトークン数)。"""
    _llm_calls.add(1, {"purpose": purpose, "outcome": outcome})
    if usage is None:
        return
    for kind, name in (("input", "input_tokens"), ("output", "output_tokens")):
        value = getattr(usage, name, None)
        if isinstance(value, int):
            _llm_tokens.add(value, {"purpose": purpose, "kind": kind})


def record_http(route: str, method: str, status: int, seconds: float) -> None:
    """API の所要時間。`route` は経路のテンプレート(「/v1/memory/{memory_id}」。実際の ID は入れない)。"""
    _http_seconds.record(seconds, {"route": route, "method": method, "status": status})


def record_job(kind: str, status: str, seconds: float, counts: Mapping[str, int]) -> None:
    _job_seconds.record(seconds, {"kind": kind, "status": status})
    for name, value in counts.items():
        if value:
            _job_items.add(int(value), {"kind": kind, "item": name})
