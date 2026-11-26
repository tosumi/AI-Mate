"""一般ユーザーの認証(エンドポイントの依存。§13.4)。トークンを検証し、内部の user_id を決める。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from mycompanion.api.auth import AuthError, AuthUnavailable


@dataclass(frozen=True)
class CurrentUser:
    user_id: uuid.UUID


_bearer = HTTPBearer(auto_error=False)


async def current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> CurrentUser:
    """トークンを検証し、内部の user_id を決める。失敗の理由は返さない。"""
    deps = request.app.state.deps
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="認証が必要です",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if credentials is None:
        raise unauthorized
    try:
        principal = await deps.verifier.verify(credentials.credentials)
    except AuthError:
        raise unauthorized from None
    except AuthUnavailable:
        # サインインのやり直しでは直らない(Entra の障害など)。少し待ってもらう
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="いまサインインを確かめられません。少し待って、もう一度お試しください。",
        ) from None
    user_id = await deps.users.resolve_user(principal.idp, principal.subject)
    return CurrentUser(user_id=user_id)


# 認証済みのユーザー(エンドポイントの引数に使う)
AuthedUser = Annotated[CurrentUser, Depends(current_user)]
