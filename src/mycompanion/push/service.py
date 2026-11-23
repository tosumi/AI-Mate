"""Web Push の購読の保存と、通知の送信(本体仕様書 §15.1 の M8。D5 = 自前の送信、VAPID)。

- 通知の文面には、会話の内容を入れない(キャラクタ名と、メッセージがあることだけ)。
- 送り先(購読のエンドポイント)は、ブラウザの通知サービスのホストだけを受け付ける(任意の URL へ送らない)。
- 送り先から「もうない」(404・410)と返ったら、購読を削除する。
- VAPID の秘密鍵は Key Vault に置き、実行時に読む(環境変数・コードに置かない)。公開鍵は、秘密鍵から計算する。
- `sub`(連絡先)には、メールアドレスではなく、Web の画面の URL を使う(個人の情報を通知サービスに送らない)。

ログには、件数と結果の種類だけを出す(エンドポイント・user_id は出さない)。
"""

from __future__ import annotations

import base64
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

VAPID_SECRET_NAME = "vapid-private-key"  # Key Vault の秘密の名前(値は、秘密鍵の生の32バイトの base64url)
PUSH_TTL_SECONDS = 12 * 3600  # 届けられないとき、通知サービスが保持する時間

# ブラウザの通知サービスのホスト(これ以外のエンドポイントは、購読として受け付けない)
_ALLOWED_HOSTS = ("fcm.googleapis.com", "updates.push.services.mozilla.com", "web.push.apple.com")
_ALLOWED_SUFFIXES = (".notify.windows.com", ".push.apple.com", ".push.services.mozilla.com")


def allowed_endpoint(url: str) -> bool:
    """ブラウザの通知サービスの https のエンドポイントか。"""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host or parsed.username or parsed.password or parsed.port not in (None, 443):
        return False
    return host in _ALLOWED_HOSTS or host.endswith(_ALLOWED_SUFFIXES)


def _b64url_bytes(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass(frozen=True)
class Subscription:
    endpoint: str
    p256dh: str
    auth: str

    def validate(self) -> None:
        if not allowed_endpoint(self.endpoint) or len(self.endpoint) > 1000:
            raise ValueError("通知の送り先が正しくありません。")
        try:
            key, secret = _b64url_bytes(self.p256dh), _b64url_bytes(self.auth)
        except (ValueError, TypeError) as exc:
            raise ValueError("通知の鍵が正しくありません。") from exc
        if len(key) != 65 or key[0] != 4 or len(secret) != 16:  # P-256 の非圧縮の公開鍵と、16バイトの認証の秘密
            raise ValueError("通知の鍵が正しくありません。")

    def info(self) -> dict[str, Any]:
        return {"endpoint": self.endpoint, "keys": {"p256dh": self.p256dh, "auth": self.auth}}


class PushStore(Protocol):
    async def add_subscription(self, user_id: uuid.UUID, subscription: Subscription, now: datetime) -> None: ...

    async def remove_subscription(self, user_id: uuid.UUID, endpoint: str) -> bool: ...

    async def subscriptions(self, user_id: uuid.UUID) -> list[Subscription]: ...

    async def subscription_count(self, user_id: uuid.UUID) -> int: ...


class InMemoryPushStore:
    def __init__(self) -> None:
        self.items: dict[uuid.UUID, dict[str, Subscription]] = {}

    async def add_subscription(self, user_id: uuid.UUID, subscription: Subscription, now: datetime) -> None:
        # 同じエンドポイントを、別のユーザーが持つことはない(端末を譲った・アカウントを替えた: 新しいユーザーに移す)
        for items in self.items.values():
            items.pop(subscription.endpoint, None)
        self.items.setdefault(user_id, {})[subscription.endpoint] = subscription

    async def remove_subscription(self, user_id: uuid.UUID, endpoint: str) -> bool:
        return self.items.get(user_id, {}).pop(endpoint, None) is not None

    async def subscriptions(self, user_id: uuid.UUID) -> list[Subscription]:
        return list(self.items.get(user_id, {}).values())

    async def subscription_count(self, user_id: uuid.UUID) -> int:
        return len(self.items.get(user_id, {}))


@dataclass(frozen=True)
class VapidKeys:
    private_raw: str  # 秘密鍵の生の32バイト(base64url)
    public_key: str  # 公開鍵(非圧縮の65バイト、base64url。ブラウザの applicationServerKey)

    @classmethod
    def from_private(cls, private_raw: str) -> VapidKeys:
        from cryptography.hazmat.primitives import serialization
        from py_vapid import Vapid02

        vapid = Vapid02.from_raw(private_raw.encode())
        public = vapid.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        return cls(private_raw, base64.urlsafe_b64encode(public).decode().rstrip("="))

    @staticmethod
    def generate() -> VapidKeys:
        """新しい鍵(鍵を作るスクリプトだけが使う)。"""
        from cryptography.hazmat.primitives.asymmetric import ec

        key = ec.generate_private_key(ec.SECP256R1())
        raw = key.private_numbers().private_value.to_bytes(32, "big")
        return VapidKeys.from_private(base64.urlsafe_b64encode(raw).decode().rstrip("="))


# 送信の関数(テストで差し替える)。(購読, 本文, 鍵, 連絡先) → HTTP の状態
Sender = Callable[[Subscription, str, VapidKeys, str], Awaitable[int]]


async def webpush_sender(subscription: Subscription, data: str, keys: VapidKeys, subject: str) -> int:
    from py_vapid import (
        Vapid02,  # RFC 8292 の形(Authorization: vapid t=…, k=…)。Apple の通知サービスは、この形だけ
    )
    from pywebpush import WebPushException, webpush_async

    try:
        response = await webpush_async(
            subscription.info(), data=data, vapid_private_key=Vapid02.from_raw(keys.private_raw.encode()),
            vapid_claims={"sub": subject}, ttl=PUSH_TTL_SECONDS, timeout=10,
        )  # fmt: skip
    except WebPushException as exc:
        return int(getattr(exc.response, "status", 0) or getattr(exc.response, "status_code", 0) or 0)
    return int(getattr(response, "status", 0) or getattr(response, "status_code", 0) or 201)


@dataclass
class PushService:
    """購読の登録・取り消しと、通知の送信。鍵がない(設定していない)ときは、通知を送らない。"""

    store: PushStore
    load_keys: Callable[[], Awaitable[VapidKeys | None]]
    subject: str  # VAPID の連絡先(Web の画面の URL)
    sender: Sender = webpush_sender
    _keys: VapidKeys | None = field(default=None, init=False)
    _loaded: bool = field(default=False, init=False)

    async def keys(self) -> VapidKeys | None:
        if not self._loaded:
            try:
                self._keys = await self.load_keys()
            except Exception as exc:  # noqa: BLE001 - 鍵を読めなければ、通知を使わない(会話は止めない)
                logger.warning("通知の鍵を読めませんでした: %s", type(exc).__name__)
                self._keys = None
            self._loaded = True
        return self._keys

    async def config(self) -> dict[str, Any]:
        keys = await self.keys()
        return {"enabled": keys is not None, "public_key": keys.public_key if keys else None}

    async def subscribe(self, user_id: uuid.UUID, subscription: Subscription, now: datetime) -> None:
        subscription.validate()
        await self.store.add_subscription(user_id, subscription, now)

    async def unsubscribe(self, user_id: uuid.UUID, endpoint: str) -> bool:
        return await self.store.remove_subscription(user_id, endpoint)

    async def notify(self, user_id: uuid.UUID, title: str, body: str, url: str = "/") -> dict[str, int]:
        """ユーザーのすべての購読に送る。結果の件数(sent / gone / failed)を返す。"""
        counts = {"sent": 0, "gone": 0, "failed": 0}
        keys = await self.keys()
        if keys is None or not self.subject:
            return counts
        data = json.dumps({"title": title, "body": body, "url": url}, ensure_ascii=False)
        for subscription in await self.store.subscriptions(user_id):
            if not allowed_endpoint(subscription.endpoint):
                continue  # 保存のあとで許可の一覧が変わったとき
            try:
                status = await self.sender(subscription, data, keys, self.subject)
            except Exception as exc:  # noqa: BLE001 - 1件の失敗で、ほかの端末への送信を止めない
                logger.warning("通知の送信に失敗しました: %s", type(exc).__name__)
                counts["failed"] += 1
                continue
            if status in (404, 410):
                await self.store.remove_subscription(user_id, subscription.endpoint)  # 購読が取り消された
                counts["gone"] += 1
            elif 200 <= status < 300:
                counts["sent"] += 1
            else:
                counts["failed"] += 1
        return counts


def push_subject(web_origins: tuple[str, ...] | list[str]) -> str:
    """VAPID の連絡先: Web の画面の https の URL(メールアドレスは使わない)。なければ空(通知を送らない)。"""
    return next((origin for origin in web_origins if origin.startswith("https://")), "")


def keyvault_loader(vault_uri: str) -> Callable[[], Awaitable[VapidKeys | None]]:
    """Key Vault から VAPID の秘密鍵を読む関数(マネージド ID。秘密がなければ None)。"""

    async def load() -> VapidKeys | None:
        from azure.core.exceptions import ResourceNotFoundError
        from azure.identity.aio import DefaultAzureCredential
        from azure.keyvault.secrets.aio import SecretClient

        async with DefaultAzureCredential() as credential, SecretClient(vault_uri, credential) as client:
            try:
                secret = await client.get_secret(VAPID_SECRET_NAME)
            except ResourceNotFoundError:
                return None
        return VapidKeys.from_private(secret.value) if secret.value else None

    return load
