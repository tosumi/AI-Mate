"""運営者の API(`/ops/v1`。本体仕様書 §13.7、§15.1 の M9)。

- 一般ユーザーの API(`/v1`)とは、経路とトークンを分ける。運営者のテナントのトークンだけを受け付ける。
- 要求ごとに、認証 → 許可の判定 → **監査ログの記録** → 操作、の順に行う。記録できなければ、操作を行わない(503)。
- 会話の本文は、どのロールにも返さない。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from mycompanion.api.auth import AuthError, OperatorPrincipal
from mycompanion.ops import views
from mycompanion.ops.audit import ALLOWED, DENIED, UNAUTHENTICATED, OperatorAuditEntry
from mycompanion.ops.personas import NOTES_MAX, PersonaError, PersonaPublisher, inspect, scenario_list
from mycompanion.ops.roles import Permission, known_roles, permissions_for
from mycompanion.ops.store import OperatorStore
from mycompanion.ops.views import OPS_MEMO_MAX, REPORT_STATUSES
from mycompanion.persona import designer
from mycompanion.persona.designer import DesignerError

logger = logging.getLogger(__name__)

_bearer = HTTPBearer(auto_error=False)
# 評価の集計で、平均を出す組の件数の下限の、さらに下限(少数の評価から、個人の点が推測されないように)
RATING_MIN_N_FLOOR = 3


@dataclass(frozen=True)
class Operator:
    """許可の判定を通った運営者。"""

    principal: OperatorPrincipal
    permissions: frozenset[Permission]


def _action(request: Request) -> str:
    # 経路のテンプレート(実際の ID・クエリは入れない)
    route = getattr(request.scope.get("route"), "path", request.url.path)
    return f"{request.method} {route}"


async def _audit(request: Request, entry: OperatorAuditEntry) -> None:
    deps = request.app.state.deps
    try:
        await deps.ops_audit.record(entry)
    except Exception as exc:  # noqa: BLE001 - どの失敗でも、操作を止める
        # 記録できなければ、操作を行わない(理由の詳細は返さない)
        logger.warning("運営者の監査ログに記録できませんでした: %s", type(exc).__name__)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "いまは操作できません") from None


def _details(request: Request, permission: Permission) -> dict[str, Any]:
    details: dict[str, Any] = {"permission": permission.value}
    # 経路の中の対象(報告の ID など。本文は入らない)
    if request.path_params:
        details["target"] = {k: str(v) for k, v in request.path_params.items()}
    return details


def require(permission: Permission) -> Callable[..., Any]:
    """許可が要るエンドポイントの依存。認証・判定・監査ログの記録まで行う。"""

    async def dependency(
        request: Request,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    ) -> Operator:
        deps = request.app.state.deps
        if deps.ops_verifier is None or deps.ops_audit is None:
            # 運営者の認証を設定していない環境では、使えない
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "運営者の機能は使えません")
        now = datetime.now(UTC)
        action = _action(request)
        principal: OperatorPrincipal | None = None
        if credentials is not None:
            try:
                principal = await deps.ops_verifier.verify(credentials.credentials)
            except AuthError:
                principal = None
        if principal is None:
            await _audit(request, OperatorAuditEntry(ts=now, actor_id=None, action=action, decision=UNAUTHENTICATED))
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, "認証が必要です", headers={"WWW-Authenticate": "Bearer"}
            )
        roles = tuple(r.value for r in known_roles(principal.roles))
        allowed = permissions_for(principal.roles)
        decision = ALLOWED if permission in allowed else DENIED
        await _audit(
            request,
            OperatorAuditEntry(
                ts=now, actor_id=principal.object_id, action=action, decision=decision, roles=roles,
                details=_details(request, permission),
            ),
        )  # fmt: skip
        if decision != ALLOWED:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "この操作の権限がありません")
        return Operator(principal=principal, permissions=allowed)

    return dependency


class ReportUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["open", "in_progress", "resolved"] | None = None
    # 対応メモ(会話の本文を書かない運用)。空文字で消す
    memo: Annotated[str | None, Field(max_length=OPS_MEMO_MAX)] = None


def _store(request: Request) -> OperatorStore:
    store = request.app.state.deps.ops_store
    if store is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "運営者の機能は使えません")
    return store



class DefinitionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    definition: dict[str, Any]


class YamlBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(max_length=designer.YAML_MAX_CHARS)


class DecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    notes: Annotated[str | None, Field(max_length=NOTES_MAX)] = None


class RollbackBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to: str = Field(pattern=r"^\d+\.\d+\.\d+$")


class PreviewChatBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    definition: dict[str, Any]
    session_id: str | None = None
    text: str = Field(min_length=1, max_length=1000)


class PreviewSimulateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    definition: dict[str, Any]
    scenarios: list[str] = Field(min_length=1, max_length=3)


_PERSONA_ERROR_STATUS = {
    "invalid": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "not_found": status.HTTP_404_NOT_FOUND,
    "forbidden": status.HTTP_403_FORBIDDEN,
    "conflict": status.HTTP_409_CONFLICT,
    "limited": status.HTTP_429_TOO_MANY_REQUESTS,
}


def _persona_error(exc: PersonaError) -> HTTPException:
    return HTTPException(_PERSONA_ERROR_STATUS.get(exc.kind, status.HTTP_400_BAD_REQUEST), exc.message)


def _publisher(request: Request) -> PersonaPublisher:
    publisher = request.app.state.deps.ops_personas
    if publisher is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "運営者の機能は使えません")
    return publisher


async def _record(request: Request, operator: Operator, action: str, details: dict[str, Any]) -> None:
    """操作の内容を、操作の前に監査ログに残す(定義の本文は入れない)。"""
    await _audit(
        request,
        OperatorAuditEntry(
            ts=datetime.now(UTC), actor_id=operator.principal.object_id, action=action, decision=ALLOWED,
            roles=tuple(r.value for r in known_roles(operator.principal.roles)), details=details,
        ),
    )  # fmt: skip



async def _persona_operation(
    request: Request, operator: Operator, action: str, details: dict[str, Any], run: Callable[[], Awaitable[Any]]
) -> Any:
    """キャラクタの操作: 要求を操作の前に記録し(記録できなければ操作しない)、結果(済み・断った理由)も記録する。"""
    await _record(request, operator, action, {**details, "outcome": "requested"})
    try:
        result = await run()
    except PersonaError as exc:
        await _record_result(request, operator, action, {**details, "outcome": f"refused:{exc.kind}"})
        raise _persona_error(exc) from None
    await _record_result(request, operator, action, {**details, "outcome": "done"})
    return result


async def _record_result(request: Request, operator: Operator, action: str, details: dict[str, Any]) -> None:
    # 結果の記録は、操作のあと。失敗しても、済んだ操作の応答は返す(要求の記録は、操作の前に残っている)
    try:
        await _record(request, operator, action, details)
    except HTTPException:
        logger.warning("運営者の操作の結果を、監査ログに記録できませんでした: %s", action)


def create_ops_router() -> APIRouter:
    router = APIRouter(prefix="/ops/v1", tags=["ops"])

    @router.get("/me")
    async def me(operator: Annotated[Operator, Depends(require(Permission.VIEW_SELF))]) -> dict[str, Any]:
        """自分のロールと、使える操作(画面の表示の切り替えに使う)。氏名・メールアドレスは返さない。"""
        return {
            "roles": [r.value for r in known_roles(operator.principal.roles)],
            "permissions": sorted(p.value for p in operator.permissions),
        }

    # --- 報告の確認(サポート・管理者) ------------------------------------------------------------

    @router.get("/reports")
    async def list_reports(
        request: Request,
        _: Annotated[Operator, Depends(require(Permission.VIEW_REPORTS))],
        report_status: Annotated[str | None, Query(alias="status")] = None,
        before: datetime | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        """報告の一覧(新しい順)。本人のメモ・会話の本文は返さない。"""
        if report_status is not None and report_status not in REPORT_STATUSES:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "status の値が正しくありません")
        rows = await _store(request).list_reports(report_status, before, limit)
        return {"reports": [views.report_summary(r) for r in rows]}

    @router.get("/reports/{report_id}")
    async def get_report(
        request: Request, report_id: uuid.UUID, _: Annotated[Operator, Depends(require(Permission.VIEW_REPORTS))]
    ) -> dict[str, Any]:
        """報告の詳細と、対象のターンのガードレールの記録(応答の種類・安全のフラグ・スクリーニングの分類)。"""
        store = _store(request)
        row = await store.get_report(report_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "報告が見つかりません")
        turn, findings = None, []
        if row.get("session_id") is not None and row.get("turn_no") is not None:
            turn, findings = await store.turn_record(
                row["user_id"], row["character_id"], row["session_id"], row["turn_no"]
            )
        return views.report_detail(row, turn, findings)

    @router.patch("/reports/{report_id}")
    async def update_report(
        request: Request,
        report_id: uuid.UUID,
        body: ReportUpdate,
        operator: Annotated[Operator, Depends(require(Permission.HANDLE_REPORTS))],
    ) -> dict[str, Any]:
        """対応状況・対応メモの更新。メモの本文は、監査ログに入れない(変えたかどうかだけ)。"""
        if body.status is None and body.memo is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "変更する項目がありません")
        store = _store(request)
        now = datetime.now(UTC)
        # 変更の内容(状況と、メモを変えたか)を、操作の前に記録する
        await _audit(
            request,
            OperatorAuditEntry(
                ts=now, actor_id=operator.principal.object_id, action="report_updated", decision=ALLOWED,
                roles=tuple(r.value for r in known_roles(operator.principal.roles)),
                details={"report_id": str(report_id), "status": body.status, "memo_changed": body.memo is not None},
            ),
        )  # fmt: skip
        if not await store.update_report(report_id, body.status, body.memo, operator.principal.object_id, now):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "報告が見つかりません")
        row = await store.get_report(report_id)
        assert row is not None
        return views.report_detail(row, None, [])

    # --- 評価の集計(設計者・承認者・サポート・管理者) ---------------------------------------------

    @router.get("/ratings")
    async def ratings(
        request: Request,
        _: Annotated[Operator, Depends(require(Permission.VIEW_RATINGS))],
        since: datetime | None = None,
        min_n: Annotated[int, Query(ge=RATING_MIN_N_FLOOR, le=100)] = 5,
    ) -> dict[str, Any]:
        """作り方・モデル・版ごとの件数と平均。件数が `min_n` 未満の組は、平均を出さない。"""
        since = since or datetime.now(UTC) - timedelta(days=30)
        rows = await _store(request).rating_summary(since)
        return {"since": since.isoformat(), "min_n": min_n, "rows": views.rating_rows(rows, min_n)}

    # --- 監査ログの閲覧(管理者・監査) -------------------------------------------------------------

    @router.get("/audit-logs")
    async def audit_logs(
        request: Request,
        _: Annotated[Operator, Depends(require(Permission.VIEW_AUDIT))],
        since: datetime | None = None,
        until: datetime | None = None,
        actor_type: Annotated[str | None, Query(pattern="^(user|system|operator)$")] = None,
        before: datetime | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> dict[str, Any]:
        """監査ログ(新しい順)。ユーザーは仮の ID で示す。"""
        rows = await _store(request).list_audit_logs(since, until, actor_type, before, limit)
        return {"entries": [views.audit_entry(r) for r in rows]}

    # --- システムの状況(管理者) ------------------------------------------------------------------

    @router.get("/system/metrics")
    async def system_metrics(
        request: Request,
        _: Annotated[Operator, Depends(require(Permission.VIEW_SYSTEM))],
        period: Annotated[str, Query(pattern="^(24h|7d|30d)$")] = "24h",
    ) -> dict[str, Any]:
        """Azure のリソースの状況(数値だけ。リソースの ID・名前は返さない)。"""
        metrics = request.app.state.deps.ops_metrics
        if metrics is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "運営者の機能は使えません")
        return await metrics.read(period)

    # --- キャラクタの設計・公開(設計者・承認者・管理者。§15.1 の M10) -----------------------------

    @router.get("/personas")
    async def personas(request: Request, _: Annotated[Operator, Depends(require(Permission.VIEW_PERSONAS))]) -> dict[str, Any]:
        """キャラクタごとの版の一覧と、承認待ちの提出。"""
        publisher = _publisher(request)
        pending = await request.app.state.deps.ops_personas_store.list_submissions("pending", 100)
        return {"characters": await publisher.versions(), "pending": [_plain(r) for r in pending]}

    @router.get("/personas/submissions")
    async def submissions(
        request: Request, _: Annotated[Operator, Depends(require(Permission.VIEW_PERSONAS))],
        submission_status: Annotated[str | None, Query(alias="status", pattern="^(pending|approved|rejected)$")] = None,
    ) -> dict[str, Any]:
        _publisher(request)
        rows = await request.app.state.deps.ops_personas_store.list_submissions(submission_status, 100)
        return {"submissions": [_plain(r) for r in rows]}

    @router.get("/personas/submissions/{submission_id}")
    async def submission(
        request: Request, submission_id: uuid.UUID, _: Annotated[Operator, Depends(require(Permission.VIEW_PERSONAS))]
    ) -> dict[str, Any]:
        try:
            return await _publisher(request).detail(submission_id)
        except PersonaError as exc:
            raise _persona_error(exc) from None

    @router.post("/personas/yaml")
    async def persona_yaml(body: YamlBody, _: Annotated[Operator, Depends(require(Permission.VIEW_PERSONAS))]) -> dict[str, Any]:
        """YAML を読む(運営者の画面の「YAML を開く」)。"""
        try:
            return {"definition": designer.parse_yaml(body.text)}
        except DesignerError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None

    @router.post("/personas/inspect")
    async def persona_inspect(
        request: Request, body: DefinitionBody, _: Annotated[Operator, Depends(require(Permission.VIEW_PERSONAS))]
    ) -> dict[str, Any]:
        """提出の前の検査(lint と、標準シナリオ・専用シナリオの模擬)。"""
        _publisher(request)
        return await inspect(body.definition, request.app.state.deps.personas_root)

    @router.post("/personas/submissions")
    async def submit_persona(
        request: Request, body: DefinitionBody, operator: Annotated[Operator, Depends(require(Permission.SUBMIT_PERSONAS))]
    ) -> dict[str, Any]:
        publisher = _publisher(request)
        details = {
            "character_id": str(body.definition.get("character_id", ""))[:64],
            "version_label": str(body.definition.get("version", ""))[:20],
        }
        return await _persona_operation(
            request, operator, "persona_submit", details,
            lambda: publisher.submit(body.definition, operator.principal.object_id, datetime.now(UTC)),
        )  # fmt: skip

    @router.post("/personas/submissions/{submission_id}/approve")
    async def approve_persona(
        request: Request, submission_id: uuid.UUID, body: DecisionBody,
        operator: Annotated[Operator, Depends(require(Permission.PUBLISH_PERSONAS))],
    ) -> dict[str, Any]:
        publisher = _publisher(request)
        return await _persona_operation(
            request, operator, "persona_approve", {"submission_id": str(submission_id)},
            lambda: publisher.approve(submission_id, operator.principal.object_id, body.notes, datetime.now(UTC)),
        )  # fmt: skip

    @router.post("/personas/submissions/{submission_id}/reject")
    async def reject_persona(
        request: Request, submission_id: uuid.UUID, body: DecisionBody,
        operator: Annotated[Operator, Depends(require(Permission.PUBLISH_PERSONAS))],
    ) -> dict[str, Any]:
        publisher = _publisher(request)
        await _persona_operation(
            request, operator, "persona_reject", {"submission_id": str(submission_id)},
            lambda: publisher.reject(submission_id, operator.principal.object_id, body.notes, datetime.now(UTC)),
        )  # fmt: skip
        return {"rejected": True}

    @router.get("/personas/scenarios")
    async def persona_scenarios(
        request: Request, _: Annotated[Operator, Depends(require(Permission.PREVIEW_PERSONAS))], character_id: str | None = None
    ) -> dict[str, Any]:
        return {"scenarios": scenario_list(character_id, request.app.state.deps.personas_root)}

    @router.post("/personas/preview/chat")
    async def preview_chat(
        request: Request, body: PreviewChatBody, operator: Annotated[Operator, Depends(require(Permission.PREVIEW_PERSONAS))]
    ) -> dict[str, Any]:
        """実際のモデルで話してみる(状態はメモリ上だけ。DB・会話の記録には書かない)。"""
        preview = request.app.state.deps.ops_preview
        if preview is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "運営者の機能は使えません")
        try:
            return await preview.chat(body.definition, operator.principal.object_id, body.session_id, body.text)
        except PersonaError as exc:
            raise _persona_error(exc) from None

    @router.post("/personas/preview/simulate")
    async def preview_simulate(
        request: Request, body: PreviewSimulateBody, operator: Annotated[Operator, Depends(require(Permission.PREVIEW_PERSONAS))]
    ) -> dict[str, Any]:
        """実際のモデルでのシミュレーション(選んだシナリオだけ。1〜3件)。"""
        preview = request.app.state.deps.ops_preview
        if preview is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "運営者の機能は使えません")
        try:
            return await preview.simulate(
                body.definition, operator.principal.object_id, body.scenarios, request.app.state.deps.personas_root
            )
        except PersonaError as exc:
            raise _persona_error(exc) from None

    @router.get("/personas/{character_id}/diff")
    async def persona_diff(
        request: Request, character_id: str, first: int, second: int,
        _: Annotated[Operator, Depends(require(Permission.VIEW_PERSONAS))],
    ) -> dict[str, Any]:
        try:
            return {"changes": await _publisher(request).diff(character_id, first, second)}
        except PersonaError as exc:
            raise _persona_error(exc) from None

    @router.post("/personas/{character_id}/rollback")
    async def rollback_persona(
        request: Request, character_id: str, body: RollbackBody,
        operator: Annotated[Operator, Depends(require(Permission.PUBLISH_PERSONAS))],
    ) -> dict[str, Any]:
        publisher = _publisher(request)
        version = await _persona_operation(
            request, operator, "persona_rollback", {"character_id": character_id[:64], "version_label": body.to},
            lambda: publisher.rollback(character_id, body.to, operator.principal.object_id, datetime.now(UTC)),
        )  # fmt: skip
        return {"published_version": version}

    @router.post("/personas/{character_id}/deprecate")
    async def deprecate_persona(
        request: Request, character_id: str, operator: Annotated[Operator, Depends(require(Permission.PUBLISH_PERSONAS))]
    ) -> dict[str, Any]:
        publisher = _publisher(request)
        count = await _persona_operation(
            request, operator, "persona_deprecate", {"character_id": character_id[:64]},
            lambda: publisher.deprecate(character_id, operator.principal.object_id, datetime.now(UTC)),
        )  # fmt: skip
        return {"deprecated": count}

    return router


def _plain(row: dict[str, Any]) -> dict[str, Any]:
    return {k: (v.isoformat() if isinstance(v, datetime) else str(v) if isinstance(v, uuid.UUID) else v) for k, v in row.items()}
