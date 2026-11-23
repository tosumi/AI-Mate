"""Web Push の VAPID の鍵を作り、秘密鍵を Key Vault に保存する(本体仕様書 §15.1 の M8。D5)。

- 秘密鍵は、ファイル・コマンドライン・画面に出さず、そのまま Key Vault に書く(開発者の Entra ID の認証)。
- すでにあれば、作り直さない(作り直すと、ブラウザの購読がすべて使えなくなるため。`--rotate` のときだけ)。
- 表示するのは、公開鍵だけ(秘密ではない。画面は API から受け取るので、どこかに設定する必要はない)。

    . .\\scripts\\load-env.ps1
    uv run python scripts/create_vapid_key.py            # なければ作る
    uv run python scripts/create_vapid_key.py --rotate   # 作り直す(購読は、登録し直しが必要)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from mycompanion.push.service import VAPID_SECRET_NAME, VapidKeys


async def main() -> int:
    parser = argparse.ArgumentParser(description="VAPID の鍵を作り、Key Vault に保存する")
    parser.add_argument("--rotate", action="store_true", help="すでにあっても作り直す")
    args = parser.parse_args()
    vault_uri = (os.environ.get("KEY_VAULT_URI") or "").strip()
    if not vault_uri:
        print("エラー: KEY_VAULT_URI がありません(.env を読み込んでください)", file=sys.stderr)
        return 2

    from azure.core.exceptions import ResourceNotFoundError
    from azure.identity.aio import DefaultAzureCredential
    from azure.keyvault.secrets.aio import SecretClient

    async with DefaultAzureCredential() as credential, SecretClient(vault_uri, credential) as client:
        try:
            existing = await client.get_secret(VAPID_SECRET_NAME)
        except ResourceNotFoundError:
            existing = None
        if existing is not None and existing.value and not args.rotate:
            print("すでにあります(作り直すときは --rotate)。公開鍵:", VapidKeys.from_private(existing.value).public_key)
            return 0
        keys = VapidKeys.generate()
        await client.set_secret(VAPID_SECRET_NAME, keys.private_raw, content_type="vapid-private-key-raw-b64url")
        print("Key Vault に保存しました。公開鍵:", keys.public_key)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
