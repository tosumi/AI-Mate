"""運営者の認証・権限・監査ログのテスト(本体仕様書 §15.1 の M9 の手順1)。"""

import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from mycompanion.api.app import AppDeps, create_app
from mycompanion.api.auth import (
    AuthError,
    EntraOperatorTokenVerifier,
    OperatorPrincipal,
    Principal,
    operator_from_claims,
)
from mycompanion.config import OperatorAuthConfig
from mycompanion.ops.audit import ALLOWED, DENIED, UNAUTHENTICATED, InMemoryOperatorAuditLog
from mycompanion.ops.roles import GRANTS, Permission, Role, permissions_for

TENANT = "ops-tenant"
CONFIG = OperatorAuthConfig(tenant_id=TENANT, audience="api://ops-api", scope="api://ops-api/ops.access")
ISSUER = f"https://login.microsoftonline.com/{TENANT}/v2.0"


# --- トークンの検証 -------------------------------------------------------------------------------


class _FakeJwks:
    def __init__(self, public_key):
        self._key = public_key

    def get_signing_key_from_jwt(self, token):
        return type("Key", (), {"key": self._key})()


@pytest.fixture(scope="module")
def keys():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _verifier(public_key, config=CONFIG) -> EntraOperatorTokenVerifier:
    verifier = EntraOperatorTokenVerifier(config)
    verifier._issuer = ISSUER
    verifier._jwks = _FakeJwks(public_key)
    return verifier


def _token(key, **overrides) -> str:
    claims = {
        "iss": ISSUER, "aud": "api://ops-api", "exp": int(time.time()) + 600, "tid": TENANT,
        "scp": "ops.access", "oid": "operator-1", "roles": ["support"], "amr": ["pwd", "mfa"],
    }  # fmt: skip
    claims.update(overrides)
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, key, algorithm="RS256")


async def test_operator_token_is_verified_with_roles(keys):
    principal = await _verifier(keys.public_key()).verify(_token(keys, roles=["support", "auditor"]))
    assert principal == OperatorPrincipal("operator-1", TENANT, frozenset({"support", "auditor"}))


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "api-aud"},  # 一般ユーザーの API のトークン
        {"iss": "https://tid.ciamlogin.com/tid/v2.0"},  # 外部テナント(一般ユーザー)の発行者
        {"tid": "other-tenant"},
        {"scp": "companion.access"},
        {"exp": int(time.time()) - 10},
        {"oid": None},
    ],
)
async def test_tokens_other_than_the_operator_tenant_are_rejected(keys, overrides):
    with pytest.raises(AuthError):
        await _verifier(keys.public_key()).verify(_token(keys, **overrides))


def test_mfa_claim_is_required_only_when_configured():
    claims = {"tid": TENANT, "scp": "ops.access", "oid": "o", "roles": ["admin"], "amr": ["pwd"]}
    assert operator_from_claims(claims, CONFIG).roles == frozenset({"admin"})
    strict = OperatorAuthConfig(tenant_id=TENANT, audience="a", scope="api://a/ops.access", require_mfa_claim=True)
    with pytest.raises(AuthError):
        operator_from_claims(claims, strict)
    assert operator_from_claims({**claims, "amr": ["pwd", "mfa"]}, strict).object_id == "o"


def test_config_is_optional_but_not_partial():
    from mycompanion.config import ConfigError

    assert OperatorAuthConfig.from_env({}) is None
    with pytest.raises(ConfigError):
        OperatorAuthConfig.from_env({"OPS_ENTRA_TENANT_ID": "t"})
    config = OperatorAuthConfig.from_env(
        {"OPS_ENTRA_TENANT_ID": "t", "OPS_ENTRA_API_AUDIENCE": "api://x", "OPS_ENTRA_API_SCOPE": "api://x/ops.access",
         "OPS_REQUIRE_MFA_CLAIM": "true"}
    )  # fmt: skip
    assert config and config.scope_name == "ops.access" and config.require_mfa_claim
    assert config.metadata_url == "https://login.microsoftonline.com/t/v2.0/.well-known/openid-configuration"


# --- ロールと許可 -------------------------------------------------------------------------------


def test_role_grants_match_the_spec_table():
    # 仕様書 §15.1 の M9 の表(M10 でキャラクタの設計・公開を追加)と同じであること(表を変えたら、ここも変える)
    expected = {
        Role.DESIGNER: {"view_self", "view_ratings", "view_personas", "submit_personas", "preview_personas"},
        Role.APPROVER: {"view_self", "view_ratings", "view_personas", "publish_personas", "preview_personas"},
        Role.SUPPORT: {"view_self", "view_reports", "handle_reports", "view_ratings"},
        Role.ADMIN: {
            "view_self", "view_reports", "handle_reports", "view_ratings", "view_audit", "view_system",
            "view_personas", "submit_personas", "publish_personas", "preview_personas",
        },
        Role.AUDITOR: {"view_self", "view_audit"},
    }
    assert {role: {p.value for p in grants} for role, grants in GRANTS.items()} == expected


def test_permissions_combine_roles_and_ignore_unknown_names():
    assert permissions_for(set()) == frozenset()
    assert permissions_for({"unknown", "Admin"}) == frozenset()  # 大文字・知らない名前は認めない
    assert permissions_for({"support", "auditor"}) == GRANTS[Role.SUPPORT] | GRANTS[Role.AUDITOR]


# --- API(経路・判定・監査ログ) --------------------------------------------------------------------

OPERATORS = {
    "tok-" + role.value: OperatorPrincipal(f"oid-{role.value}", TENANT, frozenset({role.value})) for role in Role
}
OPERATORS["tok-none"] = OperatorPrincipal("oid-none", TENANT, frozenset())


class FakeOperatorVerifier:
    async def verify(self, token: str) -> OperatorPrincipal:
        if token not in OPERATORS:
            raise AuthError("bad")
        return OPERATORS[token]


class FakeUserVerifier:
    async def verify(self, token: str) -> Principal:
        if not token.startswith("user-"):
            raise AuthError("bad")
        return Principal(idp="entra_external_id", subject=token)


class FakeUsers:
    async def resolve_user(self, idp: str, subject: str) -> uuid.UUID:
        return uuid.uuid5(uuid.NAMESPACE_URL, subject)


@pytest.fixture
def ops():
    audit = InMemoryOperatorAuditLog()
    deps = AppDeps(
        verifier=FakeUserVerifier(), users=FakeUsers(), ops_verifier=FakeOperatorVerifier(), ops_audit=audit
    )
    return TestClient(create_app(deps)), audit


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("role", list(Role))
def test_every_role_can_see_itself_and_it_is_audited(ops, role):
    client, audit = ops
    response = client.get("/ops/v1/me", headers=_bearer(f"tok-{role.value}"))
    assert response.status_code == 200
    body = response.json()
    assert body == {"roles": [role.value], "permissions": sorted(p.value for p in GRANTS[role])}
    (entry,) = audit.entries
    assert (entry.actor_id, entry.action, entry.decision, entry.roles) == (
        f"oid-{role.value}", "GET /ops/v1/me", ALLOWED, (role.value,)
    )


def test_operator_without_roles_is_denied_and_audited(ops):
    client, audit = ops
    assert client.get("/ops/v1/me", headers=_bearer("tok-none")).status_code == 403
    (entry,) = audit.entries
    assert entry.decision == DENIED and entry.actor_id == "oid-none" and entry.details == {"permission": "view_self"}


@pytest.mark.parametrize("headers", [{}, _bearer("garbage"), _bearer("user-abc")])
def test_missing_invalid_or_general_user_tokens_are_rejected_and_audited(ops, headers):
    # 一般ユーザーのトークンは、運営者の API では通らない(経路とトークンを混ぜない)
    client, audit = ops
    assert client.get("/ops/v1/me", headers=headers).status_code == 401
    (entry,) = audit.entries
    assert entry.decision == UNAUTHENTICATED and entry.actor_id is None


def test_operator_tokens_are_not_accepted_by_the_general_user_api(ops):
    client, _ = ops
    assert client.get("/v1/characters", headers=_bearer("tok-admin")).status_code == 401


def test_nothing_happens_when_the_audit_log_cannot_be_written(ops):
    client, audit = ops
    audit.fail = True
    response = client.get("/ops/v1/me", headers=_bearer("tok-admin"))
    assert response.status_code == 503 and "roles" not in response.json()


def test_ops_api_is_unavailable_when_not_configured():
    client = TestClient(create_app(AppDeps(verifier=FakeUserVerifier(), users=FakeUsers())))
    assert client.get("/ops/v1/me", headers=_bearer("tok-admin")).status_code == 503


def test_every_ops_route_requires_a_permission():
    # 運営者の経路は、すべて許可の判定(と監査ログ)を通る。判定のない経路を足したら、ここで気づく
    from mycompanion.api.ops import create_ops_router

    routes = create_ops_router().routes
    assert routes and all(r.path.startswith("/ops/v1/") for r in routes)
    for route in routes:
        calls = [d.call for d in route.dependant.dependencies]
        assert any(getattr(c, "__qualname__", "").startswith("require.") for c in calls), route.path


def test_permissions_enum_covers_the_table():
    assert {p.value for p in Permission} == set().union(*({p.value for p in g} for g in GRANTS.values()))
