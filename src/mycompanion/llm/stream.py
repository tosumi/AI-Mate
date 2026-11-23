"""生成中の文字の受け渡し(本体仕様書 §15.1 の M7。Web の逐次表示)。

応答の生成は、記憶を渡したときに JSON(`{"text": "...", "used_memory_ids": [...]}`)で返る。
生成中に届くのは JSON の断片なので、`text` の値の部分だけを、少しずつ取り出して渡す。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

# 生成中の文字を受け取る関数(取り出せた分だけを渡す)
DeltaSink = Callable[[str], Awaitable[None]]

_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


class JsonTextExtractor:
    """JSON の断片から、最上位の `"text"` の値(文字列)だけを、届いた順に取り出す。

    `feed` は、新しく確定した文字だけを返す。エスケープ(`\\n`、`\\"`、`\\uXXXX`)は、元の文字に戻す。
    値の文字列が閉じたら、それ以降は何も返さない。
    """

    def __init__(self, key: str = "text") -> None:
        self._needle = f'"{key}"'
        self._buffer = ""
        self._state = "seek"  # seek(キーを探す)→ colon → open(値の始まりの " を待つ)→ value → done
        self._pending = ""  # エスケープの途中

    def feed(self, chunk: str) -> str:
        if self._state == "done":
            return ""
        self._buffer += chunk
        out: list[str] = []
        while self._buffer:
            if self._state == "seek":
                index = self._buffer.find(self._needle)
                if index < 0:
                    # キーが断片の境目で切れていることがあるので、末尾だけを残す
                    self._buffer = self._buffer[-(len(self._needle) - 1) :]
                    break
                self._buffer = self._buffer[index + len(self._needle) :]
                self._state = "colon"
            elif self._state == "colon":
                stripped = self._buffer.lstrip()
                if not stripped:
                    self._buffer = ""
                    break
                if stripped[0] != ":":
                    self._state = "seek"  # 値ではなく、別の場所に現れた "text" だった
                    self._buffer = stripped
                    continue
                self._buffer = stripped[1:]
                self._state = "open"
            elif self._state == "open":
                stripped = self._buffer.lstrip()
                if not stripped:
                    self._buffer = ""
                    break
                if stripped[0] != '"':
                    self._state = "done"
                    break
                self._buffer = stripped[1:]
                self._state = "value"
            else:  # value
                text, self._buffer = self._pending + self._buffer, ""
                self._pending = ""
                index = 0
                while index < len(text):
                    char = text[index]
                    if char == '"':
                        self._state = "done"
                        return "".join(out)
                    if char == "\\":
                        if index + 1 >= len(text):
                            self._pending = text[index:]
                            break
                        code = text[index + 1]
                        if code == "u":
                            if index + 6 > len(text):
                                self._pending = text[index:]
                                break
                            point = int(text[index + 2 : index + 6], 16)
                            if 0xD800 <= point <= 0xDBFF:
                                # サロゲートペア(絵文字など): 後半の \uXXXX がそろうまで待つ
                                if index + 12 > len(text):
                                    self._pending = text[index:]
                                    break
                                low = int(text[index + 8 : index + 12], 16)
                                point = 0x10000 + ((point - 0xD800) << 10) + (low - 0xDC00)
                                index += 6
                            out.append(chr(point))
                            index += 6
                            continue
                        out.append(_ESCAPES.get(code, code))
                        index += 2
                        continue
                    out.append(char)
                    index += 1
                break
        return "".join(out)
