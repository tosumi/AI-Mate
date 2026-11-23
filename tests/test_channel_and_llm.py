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
