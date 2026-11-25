"""デザイナの API(`/v1/designer`。本体仕様書 §15.1 の M10 の手順2)。

- 一般ユーザー(サインイン済み)に開く。状態を持たず、DB に書かない(下書きは、利用者の端末の YAML ファイル)。
- 実際のモデルは使わない(模擬のシミュレーションだけ)。
- 定義の本文は、ログに出さない。要求の大きさと、重い処理(シミュレーション)の回数に上限を設ける。
- 重い処理は、別のスレッドで1つずつ動かす(会話の応答を止めない)。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from mycompanion.api.users import AuthedUser
from mycompanion.persona import designer, wizard
from mycompanion.persona.designer import DesignerError
from mycompanion.ratelimit import SlidingWindowLimiter

# 要求の本文の上限(YAML の上限と、JSON にしたときの増え方の余裕)
MAX_BODY_BYTES = 512 * 1024
# 模擬のシミュレーションの回数(1人あたり。時間の窓)
SIMULATE_LIMIT = 20
SIMULATE_WINDOW_SECONDS = 600
# 同時に動かす重い処理の数と、空きを待つ時間
HEAVY_CONCURRENCY = 1
HEAVY_WAIT_SECONDS = 20.0


class DesignerState:
    """ルーターの中で共有するもの(回数の上限、重い処理の順番待ち、全数確認の結果)。"""

    def __init__(self, samples_root: Path | None = None) -> None:
        self.samples_root = samples_root
        self.limiter = SlidingWindowLimiter(SIMULATE_LIMIT, SIMULATE_WINDOW_SECONDS)
        self.heavy = asyncio.Semaphore(HEAVY_CONCURRENCY)
        self.sweep: list[dict[str, Any]] | None = None
        self.sweep_lock = asyncio.Lock()


async def _limit_body(request: Request) -> None:
    length = request.headers.get("content-length")
    if length is not None and length.isdigit() and int(length) > MAX_BODY_BYTES:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "大きすぎます")


def _fail(exc: DesignerError) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))


class DefinitionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    definition: dict[str, Any]


class YamlBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(max_length=designer.YAML_MAX_CHARS)


class NewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    character_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,40}$")
    name: str = Field(min_length=1, max_length=40)
    type_id: str = Field(min_length=4, max_length=4)
    profile: str | None = None


class QuizBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answers: dict[str, int]
    tiebreak_choice: dict[str, str] = {}


class WizardBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    character_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,40}$")
    name: str = Field(min_length=1, max_length=40)
    # 質問 ID → 回答(5段階の数、選択肢の値、好き嫌いの区分 → 項目 ID)
    answers: dict[str, int | str | dict[str, str | None]] = Field(default={}, max_length=80)
    finish: bool = False  # 残りの質問を、既定の回答にして組み立てる(デザイナに切り替えるとき)


class SimulateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    definition: dict[str, Any]
    # 開始時の関係値(省略: キャラクタの初期値)。比べるときは複数
    relationships: list[float | None] = Field(default=[None], min_length=1, max_length=5)
    under_18: bool = False


def create_designer_router(samples_root: Path | None = None) -> APIRouter:
    router = APIRouter(prefix="/v1/designer", tags=["designer"], dependencies=[Depends(_limit_body)])
    state = DesignerState(samples_root)

    async def heavy(job: Callable[[], Any]) -> Any:
        try:
            await asyncio.wait_for(state.heavy.acquire(), HEAVY_WAIT_SECONDS)
        except TimeoutError:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "混み合っています。少し待ってから、もう一度お試しください。") from None
        try:
            return await asyncio.to_thread(job)
        finally:
            state.heavy.release()

    @router.get("/choices")
    async def choices(_: AuthedUser) -> dict[str, Any]:
        """性格タイプ・軸・表出プロファイル・好みカタログ・イベントのトリガー。"""
        return designer.choices()

    @router.get("/samples")
    async def samples(_: AuthedUser) -> dict[str, Any]:
        """サンプルのキャラクタ(参考。読み取り専用)。"""
        return {"samples": designer.samples(state.samples_root)}

    @router.get("/samples/{character_id}")
    async def sample(character_id: str, _: AuthedUser) -> dict[str, Any]:
        try:
            return designer.sample(character_id, state.samples_root)
        except DesignerError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None

    @router.post("/new")
    async def new(body: NewBody, _: AuthedUser) -> dict[str, Any]:
        """新しい定義のひな形(CLI の `persona new` と同じ)。"""
        try:
            return {"definition": designer.new_definition(body.character_id, body.name, body.type_id, body.profile)}
        except DesignerError as exc:
            raise _fail(exc) from None

    @router.post("/yaml/parse")
    async def parse_yaml(body: YamlBody, _: AuthedUser) -> dict[str, Any]:
        """YAML のファイルを読む(画面の「YAML を開く」)。"""
        try:
            return {"definition": designer.parse_yaml(body.text)}
        except DesignerError as exc:
            raise _fail(exc) from None

    @router.post("/yaml/dump")
    async def dump_yaml(body: DefinitionBody, _: AuthedUser) -> dict[str, Any]:
        """定義を YAML にする(画面の「YAML を保存」。CLI と同じ書式)。"""
        return {"yaml": designer.dump_yaml(body.definition)}

    @router.post("/check")
    async def check(body: DefinitionBody, _: AuthedUser) -> dict[str, Any]:
        """lint と、解決済みの値・派生指標(軽い処理。編集のたびに呼ぶ)。"""
        return designer.check(body.definition, root=state.samples_root)

    @router.get("/quiz")
    async def quiz_questions(_: AuthedUser) -> dict[str, Any]:
        return designer.quiz_questions()

    @router.post("/quiz/score")
    async def quiz_score(body: QuizBody, _: AuthedUser) -> dict[str, Any]:
        try:
            return designer.quiz_score(body.answers, body.tiebreak_choice)
        except DesignerError as exc:
            raise _fail(exc) from None

    @router.post("/wizard")
    async def wizard_step(body: WizardBody, _: AuthedUser) -> dict[str, Any]:
        """ウィザード: 次の質問か、すべて答えた(または finish)ときは組み立てた定義と検査の結果(状態を持たない)。"""
        if body.character_id.startswith(designer.SAMPLE_PREFIX):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "sample_ で始まる ID は使えません。")
        try:
            out = wizard.step(body.character_id, body.name.strip(), body.answers, body.finish)
        except wizard.WizardError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
        if out["done"]:
            out["check"] = designer.check(out["definition"], root=state.samples_root)
        return out

    @router.post("/simulate")
    async def simulate(body: SimulateBody, user: AuthedUser) -> dict[str, Any]:
        """標準シナリオの模擬のシミュレーション(LLM を呼ばない)。"""
        if not state.limiter.allow(user.user_id):
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS, "シミュレーションの回数が多すぎます。しばらく待ってから、もう一度お試しください。"
            )
        try:
            return await heavy(lambda: designer.simulate(body.definition, body.relationships, body.under_18))
        except DesignerError as exc:
            raise _fail(exc) from None

    @router.get("/sweep")
    async def sweep(_: AuthedUser) -> dict[str, Any]:
        """16タイプ × 表出プロファイルの全数確認(全員で同じ結果。1回だけ計算して保持する)。"""
        async with state.sweep_lock:
            if state.sweep is None:
                state.sweep = await heavy(designer.sweep)
        return {"rows": state.sweep}

    return router
