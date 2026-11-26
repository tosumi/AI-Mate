"""トークン検証のテスト(本体仕様書 §13.4.5)。"""

import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from mycompanion.api.auth import AuthError, AuthUnavailable, EntraTokenVerifier, principal_from_claims
from mycompanion.config import AuthConfig

CONFIG = AuthConfig(
    tenant_id="tid",
    authority="https://sub.ciamlogin.com/",
    audience="api-aud",
    scope="api://api-aud/companion.access",
)
ISSUER = "https://tid.ciamlogin.com/tid/v2.0"


class _FakeJwks:
    def __init__(self, public_key):
        self._key = public_key

    def get_signing_key_from_jwt(self, token):
        return type("Key", (), {"key": self._key})()


@pytest.fixture(scope="module")
def keys():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private, other


def _verifier(public_key) -> EntraTokenVerifier:
    verifier = EntraTokenVerifier(CONFIG)
    verifier._issuer = ISSUER
    verifier._jwks = _FakeJwks(public_key)
    return verifier


def _token(key, **overrides) -> str:
    claims = {
        "iss": ISSUER,
        "aud": "api-aud",
        "exp": int(time.time()) + 600,
        "scp": "companion.access",
        "oid": "object-1",
        "sub": "sub-1",
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm="RS256")


async def test_valid_token(keys):
    private, _ = keys
    principal = await _verifier(private.public_key()).verify(_token(private))
    assert (principal.idp, principal.subject) == ("entra_external_id", "object-1")


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "other-api"},
        {"iss": "https://evil.example.invalid/"},
        {"exp": int(time.time()) - 10},
        {"scp": "other.scope"},
        {"scp": None},
        {"oid": None, "sub": None},
    ],
)
async def test_invalid_claims_are_rejected(keys, overrides):
    private, _ = keys
    with pytest.raises(AuthError):
        await _verifier(private.public_key()).verify(_token(private, **overrides))


async def test_wrong_signature_is_rejected(keys):
    private, other = keys
    with pytest.raises(AuthError):
        await _verifier(private.public_key()).verify(_token(other))


async def test_unsigned_token_is_rejected(keys):
    private, _ = keys
    unsigned = jwt.encode(
        {"iss": ISSUER, "aud": "api-aud", "exp": int(time.time()) + 600, "scp": "companion.access", "oid": "o"},
        None,
        algorithm="none",
    )
    with pytest.raises(AuthError):
        await _verifier(private.public_key()).verify(unsigned)


def test_subject_falls_back_to_sub():
    principal = principal_from_claims({"scp": "a companion.access", "sub": "sub-1"}, "companion.access")
    assert principal.subject == "sub-1"


# --- 検証の仕組みに届かないとき(M11 の手順2: 障害時の動作) ---


async def test_metadata_outage_is_unavailable_not_invalid(keys):
    """Entra のメタデータが取れない(障害・ネットワーク)のは、トークンの不正ではない(401 でなく 503 にするため)。"""

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable", request=request)

    verifier = EntraTokenVerifier(CONFIG, http_client=httpx.AsyncClient(transport=httpx.MockTransport(down)))
    with pytest.raises(AuthUnavailable):
        await verifier.verify(_token(keys[0]))

    def error(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    verifier = EntraTokenVerifier(CONFIG, http_client=httpx.AsyncClient(transport=httpx.MockTransport(error)))
    with pytest.raises(AuthUnavailable):
        await verifier.verify(_token(keys[0]))


async def test_signing_key_outage_is_unavailable(keys):
    class _DownJwks:
        def get_signing_key_from_jwt(self, token):
            raise jwt.PyJWKClientConnectionError("unreachable")

    verifier = EntraTokenVerifier(CONFIG)
    verifier._issuer = ISSUER
    verifier._jwks = _DownJwks()
    with pytest.raises(AuthUnavailable):
        await verifier.verify(_token(keys[0]))


def test_api_answers_503_while_sign_in_cannot_be_checked():
    import uuid

    from fastapi.testclient import TestClient

    from mycompanion.api.app import AppDeps, create_app

    class _Down:
        async def verify(self, token):
            raise AuthUnavailable("down")

    class _Users:
        async def resolve_user(self, idp, subject):
            return uuid.uuid4()

    client = TestClient(create_app(AppDeps(verifier=_Down(), users=_Users())))
    response = client.get("/v1/designer/choices", headers={"Authorization": "Bearer t"})
    assert response.status_code == 503
    assert "少し待って" in response.json()["detail"]
