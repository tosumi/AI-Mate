"""API(エンジンの入口。本体仕様書 §10.1、§11.5)。

範囲: ヘルスチェック、`/v1/me`、キャラクタの一覧、会話のターン、セッションの終了、関係値、ユーザー設定、
記憶(一覧・詳細・削除・非表示・訂正・昇格・整理)、感情履歴(一覧・削除)、
アカウント(同意、年齢、データの書き出し、削除)。

    uv run python -m mycompanion.api --port 8080
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from mycompanion import config as cfg
from mycompanion import telemetry
from mycompanion.account.service import AccountError, AccountOps
from mycompanion.api.auth import OperatorTokenVerifier, TokenVerifier
from mycompanion.api.designer import create_designer_router
from mycompanion.api.ops import create_ops_router
from mycompanion.api.users import AuthedUser, CurrentUser, current_user
from mycompanion.channel.types import TurnInput, TurnOutput
from mycompanion.engine.engine import Engine, EngineError
from mycompanion.engine.memory_ops import MemoryOpError, MemoryOps
from mycompanion.llm.client import Persona
from mycompanion.ops.audit import OperatorAuditLog
from mycompanion.ops.metrics import SystemMetrics
from mycompanion.ops.personas import LivePreview, PersonaPublisher, PersonaStore
from mycompanion.ops.store import OperatorStore
from mycompanion.push.service import PushService, Subscription, keyvault_loader, push_subject

HealthCheck = Callable[[], Awaitable[bool]]

# エンジンのエラーの種類 → HTTP の状態コード
logger = logging.getLogger(__name__)

_ERROR_STATUS = {
    "empty_input": status.HTTP_400_BAD_REQUEST,
    "input_too_long": 413,
    "character_not_found": status.HTTP_404_NOT_FOUND,
    "llm_unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
    "invalid_value": status.HTTP_400_BAD_REQUEST,
    "not_allowed": status.HTTP_403_FORBIDDEN,
    "rate_limited": status.HTTP_429_TOO_MANY_REQUESTS,
    "memory_not_found": status.HTTP_404_NOT_FOUND,
    "topic_not_found": status.HTTP_404_NOT_FOUND,
    "turn_not_found": status.HTTP_404_NOT_FOUND,
    "consent_required": status.HTTP_403_FORBIDDEN,
    "account_unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
    "memory_unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
    "not_available": status.HTTP_503_SERVICE_UNAVAILABLE,
}


class UserDirectory(Protocol):
    """認証の主体から、内部の user_id を引く(§13.4.2)。"""

    async def resolve_user(self, idp: str, subject: str) -> uuid.UUID: ...


class CharacterCatalog(Protocol):
    async def list_characters(self) -> list[Persona]: ...


@dataclass
class AppDeps:
    """API が使う依存先。テストでは模擬実装に差し替える。"""

    verifier: TokenVerifier
    users: UserDirectory
    engine: Engine | None = None
    memory_ops: MemoryOps | None = None  # 記憶と感情履歴の操作(記憶なしで動かすときは None)
    account: AccountOps | None = None  # 同意、年齢、書き出し、削除
    push: PushService | None = None  # Web Push の購読(§15.1 の M8。設定していなければ None)
    require_consent: bool = False  # 必須の同意がそろうまで、会話を受け付けない(本番・開発の API では True)
    characters: CharacterCatalog | None = None
    settings: cfg.EngineSettings = field(default_factory=cfg.EngineSettings)
    env_name: str = "dev"
    health_checks: dict[str, HealthCheck] = field(default_factory=dict)  # 名前 → 疎通確認
    on_startup: list[Callable[[], Awaitable[None]]] = field(default_factory=list)
    on_shutdown: list[Callable[[], Awaitable[None]]] = field(default_factory=list)
    # Web の画面の配信元(CORS で許す。空なら許さない。§15.1 の M7)
    web_origins: tuple[str, ...] = ()
    # 運営者の認証と監査ログ(§15.1 の M9。設定していなければ None で、運営者の API は 503)
    ops_verifier: OperatorTokenVerifier | None = None
    ops_audit: OperatorAuditLog | None = None
    ops_store: OperatorStore | None = None
    ops_metrics: SystemMetrics | None = None
    # キャラクタの提出・公開と、実際のモデルでの試し(§15.1 の M10)
    ops_personas: PersonaPublisher | None = None
    ops_personas_store: PersonaStore | None = None
    ops_preview: LivePreview | None = None
    # デザイナのサンプルの置き場所(§15.1 の M10。既定: personas/)
    personas_root: Path | None = None


class TurnResponse(BaseModel):
    session_id: uuid.UUID
    turn_no: int
    output: TurnOutput
    # デバッグ表示のときだけ返す。自分の発話に対する推定結果(§10.4)
    debug: dict[str, Any] | None = None


class SetRelationshipRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    character_id: str = Field(min_length=1)
    value: float


class UpdateSettingsRequest(BaseModel):
    """ユーザー設定の変更。指定した項目だけを変える。"""

    model_config = ConfigDict(extra="forbid")

    silence_enabled: bool | None = None
    filter_mode: Literal["on", "off", "both"] | None = None
    silence_render: Literal["dots", "none"] | None = None
    association_enabled: bool | None = None  # キャラクタが、連想で話題を移してよいか
    proactive_enabled: bool | None = None  # キャラクタから話しかけてよいか(§8.6。既定オフ)
    is_under_18: bool | None = None  # 開発用。本番では、サインアップの手順だけが設定する(§13.4.4)


class ProactiveSeenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: uuid.UUID


class ReportRequest(BaseModel):
    """AI の応答の報告(不適切な応答など)。"""

    model_config = ConfigDict(extra="forbid")

    character_id: str = Field(min_length=1)
    reason: Literal["inappropriate", "harmful", "incorrect", "other"] = "inappropriate"
    note: str = Field(default="", max_length=500)
    session_id: uuid.UUID | None = None
    turn_no: int | None = Field(default=None, ge=1)


class RatingRequest(BaseModel):
    """応答への評価(人間評価。§14、§15.1 の M6-7)。会話の本文は送らない。"""

    model_config = ConfigDict(extra="forbid")

    character_id: str = Field(min_length=1)
    session_id: uuid.UUID
    turn_no: int = Field(ge=1)
    overall: int = Field(ge=1, le=5)  # 総合
    persona_fit: int | None = Field(default=None, ge=1, le=5)  # キャラクタらしさ
    naturalness: int | None = Field(default=None, ge=1, le=5)  # 自然さ
    empathy: int | None = Field(default=None, ge=1, le=5)  # 気持ちへの合い方
    note: str = Field(default="", max_length=500)


class ConsentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    types: list[str] = Field(min_length=1)
    channel: Literal["cli", "web", "ios", "android", "line"] = "cli"  # 同意を取得したチャンネル


class AgeRequest(BaseModel):
    """「18歳以上ですか」への答え。true = はい、false = いいえ、null = 答えない(§13.4.4)。"""

    model_config = ConfigDict(extra="forbid")

    adult: bool | None = None


class EditMemoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    character_id: str = Field(min_length=1)
    summary: str = Field(min_length=1, max_length=400)


class ConsolidateRequest(BaseModel):
    """記憶の整理を、手動で実行する(確認・再実行・検証用。§10.3)。"""

    model_config = ConfigDict(extra="forbid")

    character_id: str = Field(min_length=1)
    day: str | None = None  # YYYY-MM-DD(省略時は、未整理の日を古い順に)
    monthly: bool = False
    month: str | None = None  # YYYY-MM(省略時は、前月)
    dry_run: bool = False


class PushKeys(BaseModel):
    model_config = ConfigDict(extra="ignore")

    p256dh: str = Field(min_length=1, max_length=200)
    auth: str = Field(min_length=1, max_length=100)


class PushSubscribeRequest(BaseModel):
    """ブラウザの購読(PushSubscription.toJSON() の形)。"""

    model_config = ConfigDict(extra="ignore")  # expirationTime などは使わない

    endpoint: str = Field(min_length=1, max_length=1000)
    keys: PushKeys


class PushUnsubscribeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    endpoint: str = Field(min_length=1, max_length=1000)


class CloseSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    character_id: str = Field(min_length=1)


def create_app(deps: AppDeps) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        for start in deps.on_startup:
            await start()
        try:
            yield
        finally:
            for close in deps.on_shutdown:
                await close()

    app = FastAPI(title="MyCompanion API", version="0.2.0", lifespan=lifespan)
    app.state.deps = deps
    if deps.web_origins:
        # Web の画面(Static Web Apps)だけを許す。Cookie は使わない(トークンは Authorization ヘッダー)ので、
        # 資格情報つきの CORS は許さない。§15.1 の M7
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(deps.web_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],  # PATCH: 運営者の報告の更新(M9)
            allow_headers=["Authorization", "Content-Type"],
            max_age=600,
        )

    @app.middleware("http")
    async def measure(request: Request, call_next):
        # 経路のテンプレートだけを記録する(実際の ID・クエリ・IP アドレスは送らない。§15.1 の M6-2)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # 処理されなかった例外も、500 として数える(警告の規則が見る。§15.1 の M6-8)
            route = getattr(request.scope.get("route"), "path", "unmatched")
            telemetry.record_http(route, request.method, 500, time.perf_counter() - started)
            raise
        route = getattr(request.scope.get("route"), "path", "unmatched")
        telemetry.record_http(route, request.method, response.status_code, time.perf_counter() - started)
        return response

    @app.exception_handler(EngineError)
    async def engine_error(_: Request, exc: EngineError) -> JSONResponse:
        code = _ERROR_STATUS.get(exc.code, status.HTTP_400_BAD_REQUEST)
        return JSONResponse(status_code=code, content={"code": exc.code, "message": exc.message})

    @app.exception_handler(MemoryOpError)
    async def memory_error(_: Request, exc: MemoryOpError) -> JSONResponse:
        code = _ERROR_STATUS.get(exc.code, status.HTTP_400_BAD_REQUEST)
        return JSONResponse(status_code=code, content={"code": exc.code, "message": exc.message})

    @app.exception_handler(AccountError)
    async def account_error(_: Request, exc: AccountError) -> JSONResponse:
        code = _ERROR_STATUS.get(exc.code, status.HTTP_400_BAD_REQUEST)
        return JSONResponse(status_code=code, content={"code": exc.code, "message": exc.message})

    def account() -> AccountOps:
        if deps.account is None:
            raise AccountError("account_unavailable", "アカウントの機能が構成されていません。")
        return deps.account

    async def consent_state(user_id: uuid.UUID, check_required: bool = True) -> dict[str, Any]:
        """必須の同意を確かめ、任意の同意の状況(記憶、センシティブな記憶、恋愛的な表現)を返す。"""
        if deps.account is None or not deps.require_consent:
            return {"memory": True, "sensitive_memory": False, "romantic_expression": None}
        state = await deps.account.status(user_id)
        if check_required and state["missing"]:
            raise AccountError(
                "consent_required", "会話を始める前に、同意が必要です。`companion account consent` を実行してください。"
            )
        return state

    def memory() -> MemoryOps:
        if deps.memory_ops is None:
            raise MemoryOpError("memory_unavailable", "記憶の機能が構成されていません。")
        return deps.memory_ops

    def engine() -> Engine:
        if deps.engine is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="エンジンが構成されていません")
        return deps.engine

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """生存確認。依存先には触れない。"""
        return {"status": "ok", "env": deps.env_name}

    @app.get("/readyz")
    async def readyz() -> dict[str, object]:
        """依存先への疎通確認。エラーの詳細は返さない(名前と可否だけ)。"""
        results: dict[str, bool] = {}
        for name, check in deps.health_checks.items():
            try:
                results[name] = bool(await check())
            except Exception:  # noqa: BLE001 - 可否だけを返す
                results[name] = False
        if not all(results.values()):
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=results)
        return {"status": "ok", "checks": results}

    @app.get("/v1/me")
    async def me(user: Annotated[CurrentUser, Depends(current_user)]) -> dict[str, str]:
        """サインイン中のアカウントの内部ID(`companion whoami` が使う)。"""
        return {"user_id": str(user.user_id)}

    @app.get("/v1/characters")
    async def characters(_: Annotated[CurrentUser, Depends(current_user)]) -> list[dict[str, str]]:
        """公開済みのキャラクタの一覧。人格プロンプトなどの中身は返さない。"""
        if deps.characters is None:
            return []
        return [
            {"character_id": p.character_id, "name": p.name} for p in await deps.characters.list_characters()
        ]

    @app.post("/v1/turns")
    async def turns(
        turn: TurnInput,
        user: Annotated[CurrentUser, Depends(current_user)],
        debug: bool = False,
        filter: Literal["on", "off", "both"] | None = None,  # 仕様のパラメータ名(§10.3)
        compose: Literal["single", "two_stage"] | None = None,
    ) -> TurnResponse:
        """会話の1ターン。user_id はトークンから決める(本文では指定できない。§10.1)。

        `filter` を省略すると、ユーザー設定の `filter_mode` を使う(§8.5)。
        `compose` は応答の作り方(§8.1)。省略時は既定(single)。two_stage は検証・デバッグ用。
        """
        consent = await consent_state(user.user_id)
        result = await engine().handle_turn(
            user.user_id,
            turn,
            filter_mode=filter,
            compose_mode=compose,
            use_memory=bool(consent["memory"]),
            romance_allowed=consent["romantic_expression"],
        )
        debug_info = None
        if debug:
            estimation = result.estimation
            debug_info = {
                "estimate": estimation.estimate.model_dump(),
                "rule_hits": estimation.rules.hits,
                "degraded": estimation.degraded,
                "source_model": estimation.source_model,
                "prompt_version": estimation.prompt_version,
                "state": result.state,
            }
        return TurnResponse(
            session_id=result.session_id, turn_no=result.turn_no, output=result.output, debug=debug_info
        )

    @app.post("/v1/turns/stream")
    async def turns_stream(
        turn: TurnInput,
        user: Annotated[CurrentUser, Depends(current_user)],
        filter: Literal["on", "off", "both"] | None = None,
        compose: Literal["single", "two_stage"] | None = None,
    ) -> StreamingResponse:
        """会話の1ターンを、逐次表示のイベント(SSE)で返す(§15.1 の M7)。

        イベント: accepted(既読)→ typing(入力中)→ delta(生成中の文字)/ retract(流した文字の取り消し)→
        message / silence / backchannel / notice(確認を通った最終の出力)→ done(セッションとターン番号)。
        失敗は error。同意がない・認証できないときは、逐次表示を始める前に、ふつうのエラーで返す。
        """
        consent = await consent_state(user.user_id)
        queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue()

        async def sink(kind: str, data: dict[str, Any]) -> None:
            await queue.put((kind, data))

        async def run() -> None:
            try:
                result = await engine().handle_turn(
                    user.user_id, turn, filter_mode=filter, compose_mode=compose,
                    use_memory=bool(consent["memory"]), romance_allowed=consent["romantic_expression"],
                    events=sink,
                )  # fmt: skip
                kinds = {"message": "message", "silence": "silence", "backchannel": "backchannel",
                         "system_notice": "notice", "delayed_message": "message"}  # fmt: skip
                for action in result.output.actions:
                    await queue.put((kinds.get(action.type, action.type), action.model_dump(mode="json")))
                await queue.put(
                    ("done", {"session_id": str(result.session_id), "turn_no": result.turn_no,
                              "debug_marker": result.output.debug_marker})
                )  # fmt: skip
            except EngineError as exc:
                code = _ERROR_STATUS.get(exc.code, status.HTTP_400_BAD_REQUEST)
                await queue.put(("error", {"code": exc.code, "message": exc.message, "status": code}))
            except Exception as exc:  # noqa: BLE001 - 中身は出さず、種類だけを記録する
                logger.warning("逐次表示のターンで失敗しました: %s", type(exc).__name__)
                await queue.put(("error", {"code": "internal", "message": "いま応答を作れませんでした。", "status": 500}))
            finally:
                await queue.put(None)

        async def stream() -> AsyncIterator[str]:
            yield _sse("accepted", {})
            task = asyncio.create_task(run())
            try:
                while (item := await queue.get()) is not None:
                    yield _sse(*item)
            finally:
                # 画面が途中で切れても、ターンの処理と記録は最後まで行う
                await task

        headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        return StreamingResponse(stream(), media_type="text/event-stream", headers=headers)

    @app.get("/v1/conversation")
    async def conversation(character_id: str, user: AuthedUser) -> dict[str, Any]:
        """これまでの会話(直近のターン。終わった会話も含む)。画面を開いたときに、続きとして表示する。"""
        current = await engine().current_conversation(user.user_id, character_id)
        return current or {"session_id": None, "turns": []}

    @app.post("/v1/sessions/close")
    async def close_session(
        body: CloseSessionRequest, user: Annotated[CurrentUser, Depends(current_user)]
    ) -> dict[str, bool]:
        """進行中のセッションを終了する(CLI の `/exit`)。"""
        consent = await consent_state(user.user_id, check_required=False)
        closed = await engine().close_session(user.user_id, body.character_id, use_memory=bool(consent["memory"]))
        return {"closed": closed}

    @app.get("/v1/relationship")
    async def relationship(
        character_id: str, user: Annotated[CurrentUser, Depends(current_user)], debug: bool = False
    ) -> dict[str, Any]:
        """現在の関係性の概要(`/relation show`)。数値は、デバッグ表示のときだけ返す。"""
        view = await engine().relationship(user.user_id, character_id)
        if not debug:
            return {"band": view["band"], "description": view["description"]}
        return view

    @app.put("/v1/relationship")
    async def set_relationship(
        body: SetRelationshipRequest, user: Annotated[CurrentUser, Depends(current_user)]
    ) -> dict[str, Any]:
        """関係値を設定する(テスト・調整用。`/relation set`)。本番環境では使えない。"""
        if deps.env_name == "prod":
            raise EngineError("not_allowed", "この操作は、本番環境では使えません。")
        return await engine().set_relationship(user.user_id, body.character_id, body.value)

    @app.get("/v1/memory")
    async def memory_list(
        character_id: str, user: AuthedUser, tier: str | None = None, kind: str | None = None, debug: bool = False
    ) -> list[dict[str, Any]]:
        """記憶の一覧(`/memory list`)。このキャラクタとの記憶だけを返す。"""
        return await memory().list(user.user_id, character_id, tier, kind, debug)

    @app.get("/v1/memory/status")
    async def memory_status(character_id: str, user: AuthedUser) -> dict[str, Any]:
        """最後の日次・月次整理の結果と、層ごとの件数(`companion memory status`)。"""
        return await memory().status(user.user_id, character_id)

    @app.post("/v1/memory/consolidate")
    async def memory_consolidate(body: ConsolidateRequest, user: AuthedUser) -> list[dict[str, Any]]:
        """日次整理・月次整理を、手動で実行する(`companion memory consolidate`)。"""
        consent = await consent_state(user.user_id, check_required=False)
        return await memory().consolidate(
            user.user_id,
            body.character_id,
            day=body.day,
            monthly=body.monthly,
            month=body.month,
            dry_run=body.dry_run,
            keep_sensitive=bool(consent["sensitive_memory"]),
        )

    @app.get("/v1/topics")
    async def topic_list(
        character_id: str, user: AuthedUser, archived: bool = False, candidates: bool = False
    ) -> dict[str, Any]:
        """トピック(話題ごとの経過)の一覧(`/memory topics`)。`archived` で、削除したトピックの概要の一覧。"""
        return await memory().topics(user.user_id, character_id, archived, candidates)

    @app.get("/v1/topics/{topic_id}")
    async def topic_show(topic_id: str, character_id: str, user: AuthedUser) -> dict[str, Any]:
        """トピックの文書(`/memory topic show`)。"""
        return await memory().topic_show(user.user_id, character_id, topic_id)

    @app.delete("/v1/topics/{topic_id}")
    async def topic_forget(topic_id: str, character_id: str, user: AuthedUser) -> dict[str, Any]:
        """トピックの削除(アーカイブ。概要だけを残す)。アーカイブの ID なら、完全な削除(§9.13)。"""
        return await memory().topic_forget(user.user_id, character_id, topic_id)

    @app.delete("/v1/topics/{topic_id}/relations/{relation_id}")
    async def topic_unlink(topic_id: str, relation_id: str, character_id: str, user: AuthedUser) -> dict[str, Any]:
        """トピック同士の関係の取り消し(`/memory topic unlink`)。"""
        return await memory().topic_unlink(user.user_id, character_id, topic_id, relation_id)

    @app.post("/v1/topics/{topic_id}/{action}")
    async def topic_action(topic_id: str, action: str, character_id: str, user: AuthedUser) -> dict[str, Any]:
        if action not in ("mute", "unmute"):
            raise MemoryOpError("invalid_value", "操作は mute / unmute のいずれかで指定してください。")
        return await memory().topic_set_muted(user.user_id, character_id, topic_id, action == "mute")

    @app.get("/v1/memory/{memory_id}")
    async def memory_show(memory_id: str, character_id: str, user: AuthedUser, debug: bool = False) -> dict[str, Any]:
        """記憶の詳細(`/memory show`)。デバッグ表示のときは、活性度・嗜好度・想起の履歴も返す。"""
        return await memory().show(user.user_id, character_id, memory_id, debug)

    @app.delete("/v1/memory/{memory_id}")
    async def memory_forget(memory_id: str, character_id: str, user: AuthedUser) -> dict[str, Any]:
        """記憶の削除(`/memory forget`)。派生物にも伝播する(§9.7)。"""
        return await memory().forget(user.user_id, character_id, memory_id)

    @app.put("/v1/memory/{memory_id}")
    async def memory_edit(memory_id: str, body: EditMemoryRequest, user: AuthedUser) -> dict[str, Any]:
        """記憶の訂正(`/memory edit`)。"""
        return await memory().edit(user.user_id, body.character_id, memory_id, body.summary)

    @app.post("/v1/memory/{memory_id}/{action}")
    async def memory_action(memory_id: str, action: str, character_id: str, user: AuthedUser) -> dict[str, Any]:
        """`mute` / `unmute`(キャラクタが自分から持ち出さない)、`promote`(中期から長期へ)。"""
        if action in ("mute", "unmute"):
            return await memory().set_muted(user.user_id, character_id, memory_id, action == "mute")
        if action == "promote":
            return await memory().promote(user.user_id, character_id, memory_id)
        raise MemoryOpError("invalid_value", "操作は mute / unmute / promote のいずれかです。")

    @app.get("/v1/emotion")
    async def emotion_list(character_id: str, user: AuthedUser, days: int = 7) -> dict[str, Any]:
        """このキャラクタとの会話での、自分の感情の推定の履歴(`/emotion list`。FR-10)。"""
        return await memory().emotion_list(user.user_id, character_id, days)

    @app.delete("/v1/emotion")
    async def emotion_forget(
        character_id: str, user: AuthedUser, event_id: str | None = None, day: str | None = None
    ) -> dict[str, Any]:
        """感情の推定の記録を削除する(`/emotion forget`)。"""
        return await memory().emotion_forget(user.user_id, character_id, event_id, day)

    @app.get("/v1/account/consents")
    async def consents(user: AuthedUser) -> dict[str, Any]:
        """同意の項目と、同意の状況。"""
        return await account().status(user.user_id)

    @app.post("/v1/account/consents")
    async def grant_consents(body: ConsentRequest, user: AuthedUser) -> dict[str, Any]:
        """同意を記録する(種類と版つき)。"""
        return await account().grant(user.user_id, body.types, body.channel)

    @app.delete("/v1/account/consents/{kind}")
    async def revoke_consent(kind: str, user: AuthedUser) -> dict[str, Any]:
        """同意を撤回する。必須の同意を撤回すると、会話はできなくなる。"""
        return await account().revoke(user.user_id, kind)

    @app.post("/v1/account/age")
    async def declare_age(body: AgeRequest, user: AuthedUser) -> dict[str, Any]:
        """年齢の質問への答えを記録する。「いいえ」「答えない」は、18歳未満の扱い(安全側)。"""
        return await account().declare_age(user.user_id, body.adult)

    @app.post("/v1/reports")
    async def report_response(body: ReportRequest, user: AuthedUser) -> dict[str, Any]:
        """AI の応答の報告(`/report`)。どのターンか、理由の種類、本人のメモだけを記録する。"""
        return await account().report(
            user.user_id, body.character_id, body.reason, body.note, body.session_id, body.turn_no
        )

    @app.post("/v1/ratings")
    async def rate_response(body: RatingRequest, user: AuthedUser) -> dict[str, Any]:
        """応答への評価(`/rate`)。対象のターンの作り方の文脈は、本人のターンの記録から引く。"""
        context = await engine().turn_context(user.user_id, body.character_id, body.session_id, body.turn_no)
        scores = {"overall": body.overall, "persona_fit": body.persona_fit,
                  "naturalness": body.naturalness, "empathy": body.empathy}  # fmt: skip
        return await account().rate(
            user.user_id, body.character_id, body.session_id, body.turn_no, scores, body.note, context
        )

    @app.get("/v1/account/export")
    async def export_account(user: AuthedUser) -> dict[str, Any]:
        """自分のデータの書き出し(設定、同意、関係値、記憶、会話、感情の推定)。"""
        return await account().export(user.user_id)

    @app.delete("/v1/account")
    async def delete_account(user: AuthedUser, confirm: str = "") -> dict[str, Any]:
        """アカウントの全データを削除する。取り消せない。`confirm=delete` が必要。"""
        if confirm != "delete":
            raise AccountError("invalid_value", "削除するには、confirm=delete を指定してください。")
        return await account().delete(user.user_id)

    @app.get("/v1/proactive")
    async def proactive_messages(user: AuthedUser) -> dict[str, Any]:
        """まだ表示していない、キャラクタからの自発発話(キャラクタごとに最後の1件。§15.1 の M8)。"""
        characters = await deps.characters.list_characters() if deps.characters is not None else []
        messages = await engine().unseen_proactive(user.user_id, characters)
        return {
            "messages": [
                {**m, "session_id": str(m["session_id"]), "ts": m["ts"].isoformat() if hasattr(m["ts"], "isoformat") else m["ts"]}
                for m in messages
            ]
        }

    @app.post("/v1/proactive/{character_id}/seen")
    async def proactive_seen(character_id: str, body: ProactiveSeenRequest, user: AuthedUser) -> dict[str, bool]:
        """自発発話を表示した(既読にする)。"""
        return {"seen": await engine().mark_proactive_seen(user.user_id, character_id, body.session_id)}

    @app.get("/v1/push/config")
    async def push_config(_: AuthedUser) -> dict[str, Any]:
        """通知(Web Push)が使えるかと、購読に使う公開鍵。"""
        if deps.push is None:
            return {"enabled": False, "public_key": None}
        return await deps.push.config()

    @app.post("/v1/push/subscriptions")
    async def push_subscribe(body: PushSubscribeRequest, user: AuthedUser) -> dict[str, bool]:
        """この端末(ブラウザ)で、通知を受け取る。送り先は、ブラウザの通知サービスだけを受け付ける。"""
        if deps.push is None or not (await deps.push.config())["enabled"]:
            raise AccountError("not_available", "通知は、いまは使えません。")
        subscription = Subscription(body.endpoint, body.keys.p256dh, body.keys.auth)
        try:
            await deps.push.subscribe(user.user_id, subscription, datetime.now(UTC))
        except ValueError as exc:
            raise AccountError("invalid_value", str(exc)) from None
        return {"subscribed": True}

    @app.post("/v1/push/unsubscribe")
    async def push_unsubscribe(body: PushUnsubscribeRequest, user: AuthedUser) -> dict[str, bool]:
        """この端末(ブラウザ)で、通知を受け取るのをやめる。"""
        if deps.push is None:
            return {"unsubscribed": False}
        return {"unsubscribed": await deps.push.unsubscribe(user.user_id, body.endpoint)}

    @app.get("/v1/settings")
    async def get_settings(user: Annotated[CurrentUser, Depends(current_user)]) -> dict[str, Any]:
        """ユーザー設定(無言の有効・無効、filter_mode など)。"""
        return await engine().user_settings(user.user_id)

    @app.put("/v1/settings")
    async def put_settings(
        body: UpdateSettingsRequest, user: Annotated[CurrentUser, Depends(current_user)]
    ) -> dict[str, Any]:
        """ユーザー設定を変更する。年齢フラグは、本番環境ではこの経路で変更できない。"""
        changes = body.model_dump(exclude_none=True)
        return await engine().update_user_settings(
            user.user_id, changes, allow_age_flag=deps.env_name != "prod"
        )

    # デザイナの API(§15.1 の M10。一般ユーザー。状態を持たない)
    app.include_router(create_designer_router(deps.personas_root))
    # 運営者の API(§15.1 の M9。一般ユーザーの API とは、経路とトークンを分ける)
    app.include_router(create_ops_router())
    return app


def _sse(kind: str, data: dict[str, Any]) -> str:
    """SSE の1件。本文は JSON(日本語はそのまま)。"""
    return f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _web_origins() -> tuple[str, ...]:
    """環境変数 MYCOMPANION_WEB_ORIGINS(カンマ区切り)。https の配信元だけを受け付ける(dev の localhost は例外)。"""
    origins = []
    for raw in (os.environ.get("MYCOMPANION_WEB_ORIGINS") or "").split(","):
        origin = raw.strip().rstrip("/")
        if origin.startswith(("https://", "http://localhost:")):
            origins.append(origin)
    return tuple(origins)


def create_app_from_env() -> FastAPI:
    """環境変数から依存先を組み立てて、アプリを作る(本番・開発用)。"""
    from mycompanion.account.store import PgAccountStore
    from mycompanion.api.auth import EntraOperatorTokenVerifier, EntraTokenVerifier
    from mycompanion.db.cosmos import open_cosmos
    from mycompanion.db.memory_store import PgMemoryStore
    from mycompanion.db.ops_store import PgOperatorAuditLog, PgOperatorStore, PgPersonaStore
    from mycompanion.db.postgres import Database
    from mycompanion.db.push_store import PgPushStore
    from mycompanion.db.stores import PgCosmosStores
    from mycompanion.db.topic_store import PgTopicStore
    from mycompanion.llm.azure_openai import AzureOpenAILLMClient
    from mycompanion.memory.service import MemoryManager
    from mycompanion.ops.metrics import ArmMonitorClient

    settings = cfg.EngineSettings()
    telemetry.quiet_sdk_logs()
    telemetry.configure("mycompanion-api")
    # 必要な環境変数が欠けていれば、ここで起動エラーにする(§11.5.9)
    verifier = EntraTokenVerifier(cfg.AuthConfig.from_env())
    # 運営者の認証(社内向けの Entra ID。設定がなければ、運営者の API は使えない。§15.1 の M9)
    ops_config = cfg.OperatorAuthConfig.from_env()
    database = Database(cfg.PostgresConfig.from_env())
    cosmos_config = cfg.CosmosConfig.from_env()
    openai_config = cfg.AzureOpenAIConfig.from_env()

    deps = AppDeps(
        verifier=verifier,
        users=database,
        settings=settings,
        env_name=cfg.environment_name(),
        health_checks={"postgres": database.ping},
        web_origins=_web_origins(),
        ops_verifier=EntraOperatorTokenVerifier(ops_config) if ops_config else None,
    )

    async def startup() -> None:
        # 接続は、イベントループが動き始めてから開く
        await database.open()
        cosmos_client, cosmos_stores = open_cosmos(
            cosmos_config, settings.turns_retention_days, settings.security_events_retention_days
        )
        llm = AzureOpenAILLMClient(openai_config, settings)
        stores = PgCosmosStores(database, cosmos_stores, settings)
        memory_store = PgMemoryStore(database, settings)
        topic_store = PgTopicStore(database, settings)
        manager = MemoryManager(memory_store, llm, settings, log=stores, topic_store=topic_store)
        deps.account = AccountOps(PgAccountStore(database, cosmos_stores, memory_store, topic_store))
        deps.require_consent = True
        deps.engine = Engine(llm, stores, settings, manager)
        deps.memory_ops = MemoryOps(stores, manager, settings)
        deps.characters = stores
        # 運営者の監査ログ(運営者の認証を設定しているときだけ)
        if deps.ops_verifier is not None:
            deps.ops_audit = PgOperatorAuditLog(database)
            deps.ops_store = PgOperatorStore(database, cosmos_stores)
            # キャラクタの提出・公開(0022 の関数)と、実際のモデルでの試し(DB に書かない)
            deps.ops_personas_store = PgPersonaStore(database)
            deps.ops_personas = PersonaPublisher(deps.ops_personas_store)
            deps.ops_preview = LivePreview(llm, settings)
            # ダッシュボード(対象のリソースの設定がなければ、すべて「未設定」と表示する)
            monitor_config = cfg.OperatorMonitorConfig.from_env()
            monitor_client = ArmMonitorClient() if monitor_config else None
            deps.ops_metrics = SystemMetrics(monitor_config, monitor_client)
            if monitor_client is not None:
                deps.on_shutdown.append(monitor_client.close)
        # 通知(Web Push)。VAPID の秘密鍵は Key Vault から読む。Key Vault の設定がなければ、通知は使わない
        vault_uri = (os.environ.get("KEY_VAULT_URI") or "").strip()
        if vault_uri:
            deps.push = PushService(PgPushStore(database), keyvault_loader(vault_uri), push_subject(deps.web_origins))
        # 終了時: 応答を待たせずに始めた処理(短期記憶の書き込み)が終わるのを待ってから、接続を閉じる
        deps.on_shutdown.extend([deps.engine.drain, llm.close, cosmos_client.close, database.close])

    deps.on_startup.append(startup)
    return create_app(deps)
