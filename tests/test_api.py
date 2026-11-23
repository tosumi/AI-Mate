"""API のテスト(ヘルスチェックと認証)。"""

import uuid

from fastapi.testclient import TestClient

from mycompanion.api.app import AppDeps, create_app
from mycompanion.api.auth import AuthError, Principal


class FakeVerifier:
    async def verify(self, token: str) -> Principal:
        if not token.startswith("good-"):
            raise AuthError("invalid")
        return Principal(idp="entra_external_id", subject=token)


class FakeUsers:
    def __init__(self):
        self.users: dict[tuple[str, str], uuid.UUID] = {}

    async def resolve_user(self, idp: str, subject: str) -> uuid.UUID:
        return self.users.setdefault((idp, subject), uuid.uuid4())


def _client(**kwargs) -> TestClient:
    return TestClient(create_app(AppDeps(verifier=FakeVerifier(), users=FakeUsers(), **kwargs)))


def test_healthz_needs_no_auth():
    response = _client(env_name="test").get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "env": "test"}


def test_readyz_reports_failures_without_details():
    async def ok() -> bool:
        return True

    async def broken() -> bool:
        raise RuntimeError("接続文字列などの内部情報")

    assert _client(health_checks={"postgres": ok}).get("/readyz").status_code == 200
    response = _client(health_checks={"postgres": ok, "cosmos": broken}).get("/readyz")
    assert response.status_code == 503
    assert response.json()["detail"] == {"postgres": True, "cosmos": False}
    assert "内部情報" not in response.text


def test_me_requires_valid_token():
    client = _client()
    assert client.get("/v1/me").status_code == 401
    assert client.get("/v1/me", headers={"Authorization": "Bearer bad"}).status_code == 401


def test_me_maps_token_to_stable_user_id():
    client = _client()
    a1 = client.get("/v1/me", headers={"Authorization": "Bearer good-a"}).json()["user_id"]
    a2 = client.get("/v1/me", headers={"Authorization": "Bearer good-a"}).json()["user_id"]
    b = client.get("/v1/me", headers={"Authorization": "Bearer good-b"}).json()["user_id"]
    assert a1 == a2 != b


def test_user_id_in_request_is_ignored():
    # user_id は常にトークンから決める(クエリや本文の値は使わない)
    client = _client()
    forged = str(uuid.uuid4())
    body = client.get(f"/v1/me?user_id={forged}", headers={"Authorization": "Bearer good-a"}).json()
    assert body["user_id"] != forged
