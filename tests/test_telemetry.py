"""計測のテスト(本体仕様書 §15.1 の M6-2)。

1ターンと API の呼び出しを実行し、送る内容(メトリクス・スパン)に、会話の本文・user_id・
実際の URL の ID が入らないことを確かめる。OpenTelemetry の送信先は、メモリ上の読み取りに置き換える。
"""

import json
import uuid
from datetime import UTC, datetime

import pytest
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from mycompanion import telemetry
from mycompanion.channel.types import TurnInput
from mycompanion.config import EngineSettings
from mycompanion.engine.engine import Engine
from mycompanion.engine.stores import InMemoryStores
from mycompanion.llm.client import Persona
from mycompanion.llm.mock import MockLLMClient

SECRET_TEXT = "ひみつの合言葉はアオサギ"


@pytest.fixture(scope="module")
def captured():
    reader = InMemoryMetricReader()
    exporter = InMemorySpanExporter()
    metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return reader, exporter


def _dump(reader, exporter) -> str:
    data = reader.get_metrics_data().to_json()
    spans = [json.loads(span.to_json()) for span in exporter.get_finished_spans()]
    return data + json.dumps(spans, ensure_ascii=False)


async def test_turn_metrics_carry_no_text_or_user_id(captured):
    reader, exporter = captured
    stores = InMemoryStores()
    stores.add_character(Persona(character_id="char_1", name="ミナト"))
    engine = Engine(MockLLMClient(), stores, EngineSettings(), None)
    user = uuid.uuid4()
    await engine.handle_turn(user, TurnInput(channel="cli", character_id="char_1", text=SECRET_TEXT), datetime.now(UTC))
    dumped = _dump(reader, exporter)
    assert "mycompanion.turn.stage_seconds" in dumped and "mycompanion.turns" in dumped
    (span,) = [s for s in exporter.get_finished_spans() if s.name == "turn"]
    assert set(span.attributes) <= {"turn_id", "prompt_version", "response_mode", "filter_mode", "env"}
    assert SECRET_TEXT not in dumped and str(user) not in dumped and "char_1" not in dumped


def test_http_metrics_use_the_route_template_not_the_actual_id(captured):
    reader, exporter = captured
    memory_id = str(uuid.uuid4())
    telemetry.record_http("/v1/memory/{memory_id}", "DELETE", 204, 0.01)
    dumped = _dump(reader, exporter)
    assert "/v1/memory/{memory_id}" in dumped and memory_id not in dumped
    # 許していない属性は、送る前に落とす
    telemetry.record_turn({"total_sec": 1.0}, {"turn_id": "t1", "user_id": "u-should-not-appear", "text": SECRET_TEXT})
    dumped = _dump(reader, exporter)
    assert "u-should-not-appear" not in dumped and SECRET_TEXT not in dumped


def test_configure_does_nothing_without_a_connection_string():
    assert telemetry.configure("mycompanion-test", {}) is False
