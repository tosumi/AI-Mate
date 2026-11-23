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

from mycompanion.config import AuthConfig

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


class EntraTokenVerifier:
    """Entra External ID(外部テナント)のアクセストークンを検証する。"""

    def __init__(self, config: AuthConfig, http_client: httpx.AsyncClient | None = None) -> None:
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

    async def verify(self, token: str) -> Principal:
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
        return principal_from_claims(claims, self._config.scope_name)


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
