"""チャンネルの型と、LLM の模擬実装のテスト。"""

import math

import pytest
from pydantic import ValidationError

from mycompanion.channel.types import SilenceAction, TurnInput, TurnOutput
from mycompanion.llm.mock import MockLLMClient


def test_turn_input_has_no_user_id():
    # user_id はクライアントが指定できない(§10.1)
    with pytest.raises(ValidationError):
        TurnInput(channel="cli", character_id="char_001", text="こんにちは", user_id="x")
    turn = TurnInput(channel="cli", character_id="char_001", text="こんにちは")
    assert turn.capabilities.streaming is False


def test_turn_output_parses_actions_by_type():
    output = TurnOutput.model_validate(
        {
            "actions": [
                {"type": "silence", "render": "dots"},
                {"type": "system_notice", "kind": "throttle_start", "text": "t"},
            ]
        }
    )
    assert isinstance(output.actions[0], SilenceAction)
    with pytest.raises(ValidationError):
        TurnOutput.model_validate({"actions": [{"type": "system_notice", "kind": "unknown", "text": "t"}]})


async def test_mock_embedding_is_deterministic_and_normalized():
    client = MockLLMClient(dimensions=1536)
    a, b, c = await client.embed(["ねこ", "ねこ", "りんご"])
    assert a == b != c and len(a) == 1536
    assert math.isclose(sum(v * v for v in a), 1.0, rel_tol=1e-9)


# --- Web の画面に出す文字(本体仕様書 §15.1 の M11 の手順2) ---


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("<script>alert(1)</script>", "＜script>alert(1)＜/script>"),
        ('<img src=x onerror="alert(1)">', '＜img src=x onerror="alert(1)">'),
        ("[ここ](javascript:alert(1))", "[ここ](javascript：alert(1))"),
        ("JavaScript : void(0)", "JavaScript： void(0)"),
        ("3 < 5 で、5 > 3。<3 も大丈夫", "3 < 5 で、5 > 3。<3 も大丈夫"),  # タグでない < はそのまま
        ("ふつうの返事だよ。", "ふつうの返事だよ。"),
    ],
)
def test_spoken_text_neutralizes_active_markup(text, expected):
    from mycompanion.channel.types import BackchannelAction, MessageAction

    assert MessageAction(text=text).text == expected
    assert BackchannelAction(text=text).text == expected


def test_azure_client_does_not_wait_minutes_when_the_model_is_busy(monkeypatch):
    """容量不足・障害のときに長く待たせない: 時間切れ 2分(接続 10秒)、再試行 2回(M11 の手順2)。"""
    import azure.identity.aio as identity

    from mycompanion.config import AzureOpenAIConfig, EngineSettings
    from mycompanion.llm.azure_openai import AzureOpenAILLMClient

    monkeypatch.setattr(identity, "DefaultAzureCredential", lambda: object())
    monkeypatch.setattr(identity, "get_bearer_token_provider", lambda credential, scope: lambda: "token")
    client = AzureOpenAILLMClient(AzureOpenAIConfig(endpoint="https://example.invalid", deployments={}), EngineSettings())
    assert client._client.max_retries == 2
    assert client._client.timeout.read == 120.0 and client._client.timeout.connect == 10.0
