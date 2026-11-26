"""チャンネルの抽象化(本体仕様書 §10.1)。

CLI・Web・LINE のアダプタは、どれも `TurnInput` をエンジンに渡し、`TurnOutput` を受け取る。
`user_id` は入力に含めない(API が認証から決める)。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

# 動く形のマークアップ(HTML のタグの始まり、javascript: の URL)。画面は文字として出すので動かないが、
# 念のため、キャラクタの発話からは、全角に置き換えて無害にする(本体仕様書 §15.1 の M11 の手順2)
_TAG_START = re.compile(r"<(?=\s*/?\s*[A-Za-z!])")
_JS_URL = re.compile(r"(java\s*script|vbscript|data)\s*:", re.IGNORECASE)


def neutralize_markup(text: str) -> str:
    return _JS_URL.sub(lambda m: m.group(1) + "：", _TAG_START.sub("＜", text))


SpokenText = Annotated[str, AfterValidator(neutralize_markup)]


class Capabilities(BaseModel):
    """チャンネルができること。表示形式はアダプタが決める。"""

    model_config = ConfigDict(extra="forbid")

    streaming: bool = False
    delayed_push: bool = False
    reaction: bool = False
    max_len: int | None = None


class TurnInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel: Literal["cli", "web", "line"]
    channel_user_id: str | None = None  # 記録用。認可には使わない
    character_id: str = Field(min_length=1)
    text: str
    ts: datetime | None = None
    capabilities: Capabilities = Capabilities()


class MessageAction(BaseModel):
    type: Literal["message"] = "message"
    text: SpokenText
    variant: Literal["filtered", "draft"] = "filtered"


class SilenceAction(BaseModel):
    type: Literal["silence"] = "silence"
    render: Literal["dots", "none", "read_only"] = "dots"


class BackchannelAction(BaseModel):
    type: Literal["backchannel"] = "backchannel"
    text: SpokenText


class DelayedMessageAction(BaseModel):  # v1.9
    type: Literal["delayed_message"] = "delayed_message"
    text: SpokenText
    delay_sec: int


class SystemNoticeAction(BaseModel):
    """システム通知(§8.8.7)。キャラクタの発話ではない。"""

    type: Literal["system_notice"] = "system_notice"
    kind: Literal[
        "persona_notice",
        "persona_cooldown_start",
        "persona_cooldown_end",
        "throttle_start",
        "throttle_end",
        "crisis_resources",  # 危機対応の、相談窓口の案内(§13.1)
    ]
    text: str


Action = Annotated[
    MessageAction | SilenceAction | BackchannelAction | DelayedMessageAction | SystemNoticeAction,
    Field(discriminator="type"),
]


class TurnOutput(BaseModel):
    actions: list[Action] = []
    debug_marker: str | None = None  # デバッグ表示のマーカー(§10.4)。表示時に整形する
    edit_summary: str | None = None  # 感情フィルタが変更した内容(filter_mode=both のとき。§8.5)
