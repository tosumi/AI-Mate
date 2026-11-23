"""トークン検証のテスト(本体仕様書 §13.4.5)。"""

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from mycompanion.api.auth import AuthError, EntraTokenVerifier, principal_from_claims
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
