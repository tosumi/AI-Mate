"""運営者の API(`/ops/v1`。本体仕様書 §13.7、§15.1 の M9)。

- 一般ユーザーの API(`/v1`)とは、経路とトークンを分ける。運営者のテナントのトークンだけを受け付ける。
- 要求ごとに、認証 → 許可の判定 → **監査ログの記録** → 操作、の順に行う。記録できなければ、操作を行わない(503)。
- 会話の本文は、どのロールにも返さない。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from mycompanion.api.auth import AuthError, OperatorPrincipal
from mycompanion.ops import views
from mycompanion.ops.audit import ALLOWED, DENIED, UNAUTHENTICATED, OperatorAuditEntry
from mycompanion.ops.roles import Permission, known_roles, permissions_for
from mycompanion.ops.store import OperatorStore
from mycompanion.ops.views import OPS_MEMO_MAX, REPORT_STATUSES

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

    return router
