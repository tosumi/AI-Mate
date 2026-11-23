"""生成中の文字の取り出しのテスト(本体仕様書 §15.1 の M7)。"""

import json

import pytest

from mycompanion.llm.stream import JsonTextExtractor


@pytest.mark.parametrize("size", [1, 2, 3, 7, 1000])
def test_extracts_the_text_value_from_any_chunking(size):
    value = 'えっと…\n「猫」の話、\\ちょっと"照れる" 😀 だね'
    body = json.dumps({"text": value, "used_memory_ids": ["m1"]}, ensure_ascii=True)
    extractor = JsonTextExtractor()
    out = "".join(extractor.feed(body[i : i + size]) for i in range(0, len(body), size))
    assert out == value


def test_stops_after_the_value_and_ignores_text_elsewhere():
    extractor = JsonTextExtractor()
    body = '{"note": "\\"text\\" is a key", "text": "こんにちは", "used_memory_ids": ["text"]}'
    assert extractor.feed(body) == "こんにちは"
    assert extractor.feed(', "text": "もう一度"}') == ""
