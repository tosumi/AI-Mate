"""API の認証(本体仕様書 §13.4.5)。

- アクセストークンの署名、`iss`、`aud`、有効期限、スコープを検証する。
- `user_id` は常にトークンから決める。リクエスト本文の `user_id` は信用しない。
- アプリの DB には、メールアドレスや氏名を保存しない(サブジェクトだけを使う。§13.4.2)。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
import jwt

from mycompanion.config import AuthConfig, OperatorAuthConfig

IDP_ENTRA_EXTERNAL_ID = "entra_external_id"


class AuthError(Exception):
    """認証の失敗。利用者に返す文言は一律にし、理由の詳細は返さない。"""


@dataclass(frozen=True)
class Principal:
    """検証済みのトークンの持ち主。"""

    idp: str
    subject: str


class TokenVerifier(Protocol):
    async def verify(self, token: str) -> Principal: ...


class _JwtVerifier:
    """OpenID Connect のメタデータから発行者と署名鍵を取り、アクセストークンの署名・iss・aud・期限を検証する。"""

    def __init__(self, config: AuthConfig | OperatorAuthConfig, http_client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._http = http_client
        self._issuer: str | None = None
        self._jwks: jwt.PyJWKClient | None = None
        self._lock = asyncio.Lock()

    async def _ensure_metadata(self) -> tuple[str, jwt.PyJWKClient]:
        async with self._lock:
            if self._issuer is None or self._jwks is None:
                client = self._http or httpx.AsyncClient(timeout=10)
                try:
                    response = await client.get(self._config.metadata_url)
                    response.raise_for_status()
                    metadata = response.json()
                finally:
                    if self._http is None:
                        await client.aclose()
                self._issuer = metadata["issuer"]
                # 署名鍵は PyJWKClient がキャッシュし、未知の kid のときに取り直す
                self._jwks = jwt.PyJWKClient(metadata["jwks_uri"], cache_keys=True, lifespan=3600)
            return self._issuer, self._jwks

    async def _claims(self, token: str) -> dict[str, Any]:
        try:
            issuer, jwks = await self._ensure_metadata()
            # 鍵の取得は同期の HTTP 呼び出しなので、スレッドで実行する
            signing_key = await asyncio.to_thread(jwks.get_signing_key_from_jwt, token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self._config.audience,
                issuer=issuer,
                options={"require": ["exp", "iss", "aud"]},
            )
        except AuthError:
            raise
        except Exception as exc:
            raise AuthError("トークンを検証できません") from exc
        return claims


class EntraTokenVerifier(_JwtVerifier):
    """Entra External ID(外部テナント)のアクセストークンを検証する。"""

    async def verify(self, token: str) -> Principal:
        return principal_from_claims(await self._claims(token), self._config.scope_name)


def principal_from_claims(claims: dict[str, Any], required_scope: str) -> Principal:
    """検証済みのクレームから、スコープを確認して Principal を作る。"""
    scopes = str(claims.get("scp", "")).split()
    if required_scope not in scopes:
        raise AuthError("必要なスコープがありません")
    # oid(テナント内で一意)を優先し、なければ sub を使う
    subject = claims.get("oid") or claims.get("sub")
    if not subject:
        raise AuthError("サブジェクトがありません")
    return Principal(idp=IDP_ENTRA_EXTERNAL_ID, subject=str(subject))


@dataclass(frozen=True)
class OperatorPrincipal:
    """検証済みの運営者(社内向けの Entra ID。§15.1 の M9)。氏名・メールアドレスは持たない。"""

    object_id: str  # 運営者のテナントのオブジェクト ID
    tenant_id: str
    roles: frozenset[str]  # アプリのロール(トークンの roles)


class OperatorTokenVerifier(Protocol):
    async def verify(self, token: str) -> OperatorPrincipal: ...


class EntraOperatorTokenVerifier(_JwtVerifier):
    """運営者のテナントのアクセストークンを検証する(一般ユーザーのトークンは、発行者・対象が違うので通らない)。"""

    def __init__(self, config: OperatorAuthConfig, http_client: httpx.AsyncClient | None = None) -> None:
        super().__init__(config, http_client)
        self._operator_config = config

    async def verify(self, token: str) -> OperatorPrincipal:
        return operator_from_claims(await self._claims(token), self._operator_config)


def operator_from_claims(claims: dict[str, Any], config: OperatorAuthConfig) -> OperatorPrincipal:
    """検証済みのクレームから、運営者を作る。テナント・スコープ・(設定により)MFA を確かめる。"""
    if str(claims.get("tid", "")) != config.tenant_id:
        raise AuthError("テナントが違います")
    if config.scope_name not in str(claims.get("scp", "")).split():
        raise AuthError("必要なスコープがありません")
    if config.require_mfa_claim and "mfa" not in [str(m) for m in claims.get("amr") or []]:
        raise AuthError("多要素認証が必要です")
    object_id = claims.get("oid")
    if not object_id:
        raise AuthError("サブジェクトがありません")
    roles = claims.get("roles") or []
    return OperatorPrincipal(
        object_id=str(object_id), tenant_id=config.tenant_id, roles=frozenset(str(r) for r in roles)
    )
