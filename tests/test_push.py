"""Web Push の通知のテスト(本体仕様書 §15.1 の M8 の「通知」。D5)。

送り先の制限(ブラウザの通知サービスだけ)、鍵、送信の結果ごとの扱い(届かない購読の削除)、API。
実際の通知サービスには送らない(暗号化と VAPID の署名までを、送らずに確かめる)。
"""

import base64
import os
import uuid
from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from mycompanion.api.app import AppDeps, create_app
from mycompanion.api.auth import Principal
from mycompanion.config import EngineSettings
from mycompanion.engine.engine import Engine
from mycompanion.engine.stores import InMemoryStores
from mycompanion.llm.mock import MockLLMClient
from mycompanion.push.service import (
    InMemoryPushStore,
    PushService,
    Subscription,
    VapidKeys,
    allowed_endpoint,
    push_subject,
)

USER, OTHER = uuid.uuid4(), uuid.uuid4()
NOW = datetime(2026, 11, 21, 3, 0, tzinfo=UTC)
KEYS = VapidKeys.generate()


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def browser_subscription(endpoint: str = "https://fcm.googleapis.com/fcm/send/abc") -> Subscription:
    """ブラウザが作る購読と同じ形(P-256 の公開鍵と、16バイトの認証の秘密)。"""
    key = ec.generate_private_key(ec.SECP256R1()).public_key()
    point = key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return Subscription(endpoint, b64(point), b64(os.urandom(16)))


@pytest.mark.parametrize(
    "url, ok",
    [
        ("https://fcm.googleapis.com/fcm/send/abc", True),
        ("https://updates.push.services.mozilla.com/wpush/v2/abc", True),
        ("https://wns2-par02p.notify.windows.com/w/?token=abc", True),
        ("https://web.push.apple.com/abc", True),
        ("http://fcm.googleapis.com/fcm/send/abc", False),  # https だけ
        ("https://example.com/push", False),  # 通知サービス以外へは送らない(任意の URL へ送らせない)
        ("https://fcm.googleapis.com.evil.example/x", False),
        ("https://user:pw@fcm.googleapis.com/x", False),
        ("https://fcm.googleapis.com:8443/x", False),
        ("https://169.254.169.254/metadata", False),
    ],
)
def test_only_browser_push_services_are_accepted(url, ok):
    assert allowed_endpoint(url) is ok


def test_subscription_keys_are_validated():
    browser_subscription().validate()
    with pytest.raises(ValueError):
        Subscription("https://fcm.googleapis.com/x", "short", b64(os.urandom(16))).validate()
    with pytest.raises(ValueError):
        Subscription("https://example.com/x", browser_subscription().p256dh, b64(os.urandom(16))).validate()


def test_public_key_is_derived_from_the_private_key():
    again = VapidKeys.from_private(KEYS.private_raw)
    assert again.public_key == KEYS.public_key
    raw = base64.urlsafe_b64decode(KEYS.public_key + "==")
    assert len(raw) == 65 and raw[0] == 4  # ブラウザの applicationServerKey の形


def test_payload_is_encrypted_and_signed_without_sending():
    from py_vapid import Vapid02
    from pywebpush import WebPusher

    # 本文は、購読の鍵で暗号化する(通知サービスには、平文の本文が見えない)
    encoded = WebPusher(browser_subscription().info()).encode(b'{"title":"FureAI"}')
    assert b"FureAI" not in encoded["body"]
    # VAPID の署名は、RFC 8292 の形。公開鍵(k)は、画面に渡すものと同じ
    headers = Vapid02.from_raw(KEYS.private_raw.encode()).sign(
        {"aud": "https://fcm.googleapis.com", "sub": "https://app.example", "exp": 2_000_000_000}
    )
    assert headers["Authorization"].startswith("vapid t=") and f"k={KEYS.public_key}" in headers["Authorization"]


async def test_notify_sends_to_every_device_and_drops_gone_ones():
    store = InMemoryPushStore()
    devices = [browser_subscription(f"https://fcm.googleapis.com/fcm/send/{n}") for n in range(4)]
    for device in devices:
        await store.add_subscription(USER, device, NOW)
    results = {devices[0].endpoint: 201, devices[1].endpoint: 410, devices[2].endpoint: 500}
    sent: list[str] = []

    async def sender(subscription, data, keys, subject):
        assert subject == "https://app.example" and "ハルからメッセージがあります" in data
        sent.append(subscription.endpoint)
        if subscription.endpoint == devices[3].endpoint:
            raise ConnectionError("network")
        return results[subscription.endpoint]

    async def load():
        return KEYS

    service = PushService(store, load, "https://app.example", sender)
    counts = await service.notify(USER, "FureAI ペルソナ", "ハルからメッセージがあります")
    assert counts == {"sent": 1, "gone": 1, "failed": 2}
    assert len(sent) == 4
    assert devices[1].endpoint not in [s.endpoint for s in await store.subscriptions(USER)]  # 届かない購読は消す


async def test_no_keys_or_subject_means_no_notification():
    store = InMemoryPushStore()
    await store.add_subscription(USER, browser_subscription(), NOW)

    async def sender(*_):
        raise AssertionError("送らない")

    async def none():
        return None

    async def broken():
        raise RuntimeError("Key Vault に届かない")

    assert (await PushService(store, none, "https://app.example", sender).notify(USER, "t", "b"))["sent"] == 0
    service = PushService(store, broken, "https://app.example", sender)
    assert await service.config() == {"enabled": False, "public_key": None}  # 会話は止めない
    assert (await PushService(store, lambda: _keys(), "", sender).notify(USER, "t", "b"))["sent"] == 0


async def _keys():
    return KEYS


async def test_same_browser_moves_to_the_new_account():
    store = InMemoryPushStore()
    device = browser_subscription()
    await store.add_subscription(USER, device, NOW)
    await store.add_subscription(OTHER, device, NOW)
    assert await store.subscriptions(USER) == [] and await store.subscriptions(OTHER) == [device]


def test_subject_is_the_web_url_not_an_email():
    assert push_subject(("http://localhost:5173", "https://app.example")) == "https://app.example"
    assert push_subject(()) == ""


def test_api_subscribe_and_unsubscribe():
    class Verifier:
        async def verify(self, token: str) -> Principal:
            return Principal(idp="entra_external_id", subject=token)

    class Users:
        async def resolve_user(self, idp: str, subject: str) -> uuid.UUID:
            return USER if subject == "me" else OTHER

    stores = InMemoryStores()
    engine = Engine(MockLLMClient(), stores, EngineSettings())
    off = TestClient(create_app(AppDeps(verifier=Verifier(), users=Users(), engine=engine)))
    me = {"Authorization": "Bearer me"}
    assert off.get("/v1/push/config", headers=me).json() == {"enabled": False, "public_key": None}
    device = browser_subscription()
    body = {"endpoint": device.endpoint, "expirationTime": None, "keys": {"p256dh": device.p256dh, "auth": device.auth}}
    assert off.post("/v1/push/subscriptions", json=body, headers=me).status_code == 503

    store = InMemoryPushStore()
    push = PushService(store, _keys, "https://app.example")
    client = TestClient(create_app(AppDeps(verifier=Verifier(), users=Users(), engine=engine, push=push)))
    assert client.get("/v1/push/config", headers=me).json() == {"enabled": True, "public_key": KEYS.public_key}
    assert client.post("/v1/push/subscriptions", json=body, headers=me).json() == {"subscribed": True}
    bad = {**body, "endpoint": "https://example.com/collect"}
    response = client.post("/v1/push/subscriptions", json=bad, headers=me)
    assert response.status_code == 400 and response.json()["code"] == "invalid_value"
    assert client.post("/v1/push/subscriptions", json=body).status_code == 401
    # 自分の購読だけを取り消せる
    other = {"Authorization": "Bearer other"}
    assert client.post("/v1/push/unsubscribe", json={"endpoint": device.endpoint}, headers=other).json() == {
        "unsubscribed": False
    }
    assert client.post("/v1/push/unsubscribe", json={"endpoint": device.endpoint}, headers=me).json() == {
        "unsubscribed": True
    }
