"""アカウントの操作: 同意の記録、年齢フラグ、データの書き出し、アカウントの削除
(本体仕様書 §11.5.5、§13.4.4、§13.4.5。同意の項目と流れは `legal/consent.md`)。

同意の項目:

| 種類 | 必須 / 任意 | 対象 |
|---|---|---|
| terms / privacy / emotion_data / third_party_ai / apac_processing | 必須 | 全員 |
| memory | 最初は必須。あとから撤回できる(撤回後は、記憶を使わない会話になる) | 全員 |
| guardian_confirmed | 18歳未満フラグが true のときは必須 | 18歳未満 |
| sensitive_memory / romantic_expression | 任意(既定は、同意しない・無効) | 18歳以上のみ |

- 必須の同意がそろうまで、会話はできない。
- 年齢は「18歳以上ですか」への答えだけを持つ(生年月日は持たない)。答えなければ、18歳未満の扱い(安全側)。
- 18歳未満の扱いに変わったら、sensitive_memory と romantic_expression を撤回し、保存済みのセンシティブな記憶を削除する。
- 同意を記録するときは、表示した文面のハッシュと、取得したチャンネルも残す。
- 書き出しと削除は、キャラクタをまたぐ。**本人のデータに対してだけ**行う。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from mycompanion.account import consent_text

# 全員に必須の同意(撤回すると、会話はできなくなる)
REQUIRED = ("terms", "privacy", "emotion_data", "third_party_ai", "apac_processing")
MEMORY = "memory"  # 最初は必須。撤回はできる
GUARDIAN = "guardian_confirmed"  # 18歳未満フラグが true のときに必須
ADULT_OPTIONAL = ("sensitive_memory", "romantic_expression")  # 18歳以上のみ。任意
KNOWN = (*REQUIRED, MEMORY, GUARDIAN, *ADULT_OPTIONAL)
CHANNELS = ("cli", "web", "ios", "android", "line")


class AccountError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# AI の応答の報告の理由(不適切 / 有害・危険 / 事実と違う / そのほか)
REPORT_REASONS = ("inappropriate", "harmful", "incorrect", "other")
# 応答への評価の観点(人間評価。§14)。overall は必須、ほかは任意。どれも 1〜5
RATING_AXES = ("overall", "persona_fit", "naturalness", "empathy")
RATING_CONTEXT = ("character_version", "response_mode", "compose_mode", "reply_model", "reply_prompt_version")


class AccountStore(Protocol):
    async def consents(self, user_id: uuid.UUID) -> list[dict[str, Any]]:
        """同意の履歴(撤回されたものを含む)。各項目は type / version / granted_at / revoked_at。"""
        ...

    async def grant(
        self, user_id: uuid.UUID, items: list[tuple[str, str]], version: str, channel: str, now: datetime
    ) -> None:
        """同意を記録する。`items` は(種類, 表示した文面のハッシュ)。"""
        ...

    async def revoke(self, user_id: uuid.UUID, kind: str, now: datetime) -> int: ...

    async def is_under_18(self, user_id: uuid.UUID) -> bool: ...

    async def set_age_flag(self, user_id: uuid.UUID, under_18: bool, now: datetime) -> None: ...

    async def delete_sensitive_memories(self, user_id: uuid.UUID, now: datetime) -> int:
        """保存済みのセンシティブな記憶を、全キャラクタ分、削除する。"""
        ...

    async def export_data(self, user_id: uuid.UUID) -> dict[str, Any]:
        """本人の全データ(設定、同意、関係値、記憶、会話、感情の推定)。"""
        ...

    async def delete_all(self, user_id: uuid.UUID, now: datetime) -> dict[str, int]:
        """本人の全データを削除する。冪等。"""
        ...

    async def add_report(self, user_id: uuid.UUID, character_id: str, report: dict[str, Any], now: datetime) -> str:
        """AI の応答の報告を記録する。報告の ID を返す。"""
        ...

    async def purge_audit_logs(self, before: datetime) -> int:
        """保持期間を過ぎた監査ログを消す。消した件数を返す。"""
        ...

    async def pending_identity_deletions(self) -> list[str]:
        """保留中の、外部テナントのアカウントの削除(オブジェクト ID)。"""
        ...

    async def upsert_rating(
        self, user_id: uuid.UUID, character_id: str, rating: dict[str, Any], now: datetime
    ) -> str: ...

    async def rating_summary(self, since: datetime) -> list[dict[str, Any]]:
        """評価の集計(件数と平均だけ。user_id・本文・メモは返さない)。"""
        ...

    async def save_rating_snapshot(self, label: str, since: datetime, min_n: int, now: datetime) -> str: ...

    async def list_rating_snapshots(self) -> list[dict[str, Any]]: ...

    async def complete_identity_deletion(self, subject: str, now: datetime) -> int: ...


class AccountOps:
    def __init__(self, store: AccountStore) -> None:
        self._store = store
        self._document = consent_text.load()

    @property
    def version(self) -> str:
        return self._document.version

    async def status(self, user_id: uuid.UUID) -> dict[str, Any]:
        """同意の状況と、同意画面(表示する文面)。"""
        version = self.version
        history = [c for c in await self._store.consents(user_id) if c["version"] == version]
        granted = {c["type"] for c in history if c["revoked_at"] is None}
        ever = {c["type"] for c in history}
        under_18 = await self._store.is_under_18(user_id)

        missing = [kind for kind in REQUIRED if kind not in granted]
        if MEMORY not in ever:
            missing.append(MEMORY)  # 最初は必須。いちど同意して撤回した場合は、求めない
        if under_18 and GUARDIAN not in granted:
            missing.append(GUARDIAN)

        screens = []
        for screen in self._document.screens.values():
            if screen.id == "4'" and not under_18:
                continue
            if screen.id == "5" and under_18:
                continue  # 任意の設定は、18歳以上のみ
            screens.append(
                {
                    "id": screen.id,
                    "title": screen.title,
                    "text": screen.text,
                    "items": [
                        {
                            "type": kind,
                            "label": label,
                            "required": kind not in ADULT_OPTIONAL,
                            "granted": kind in granted,
                        }
                        for kind, label in zip(screen.types, screen.labels, strict=True)
                    ],
                }
            )
        return {
            "version": version,
            "is_under_18": under_18,
            "screens": screens,
            "missing": missing,
            "memory": MEMORY in granted,
            "sensitive_memory": not under_18 and "sensitive_memory" in granted,
            "romantic_expression": not under_18 and "romantic_expression" in granted,
        }

    async def missing(self, user_id: uuid.UUID) -> list[str]:
        """まだ得られていない、必須の同意。空なら、会話できる。"""
        return (await self.status(user_id))["missing"]

    async def sensitive_allowed(self, user_id: uuid.UUID) -> bool:
        """センシティブな内容を、記憶に残してよいか(18歳以上で、同意がある)。"""
        return (await self.status(user_id))["sensitive_memory"]

    async def grant(self, user_id: uuid.UUID, kinds: list[str], channel: str = "cli") -> dict[str, Any]:
        unknown = [kind for kind in kinds if kind not in KNOWN]
        if unknown or not kinds:
            raise AccountError("invalid_value", f"同意の種類が正しくありません: {', '.join(unknown) or '(なし)'}")
        if channel not in CHANNELS:
            raise AccountError("invalid_value", f"チャンネルが正しくありません: {channel}")
        under_18 = await self._store.is_under_18(user_id)
        if under_18 and any(kind in ADULT_OPTIONAL for kind in kinds):
            raise AccountError("not_allowed", "この設定は、18歳以上の方だけが選べます。")
        items = []
        for kind in dict.fromkeys(kinds):
            screen = self._document.screen_of(kind)
            items.append((kind, screen.text_hash if screen else ""))
        await self._store.grant(user_id, items, self.version, channel, datetime.now(UTC))
        return await self.status(user_id)

    async def revoke(self, user_id: uuid.UUID, kind: str) -> dict[str, Any]:
        if kind not in KNOWN:
            raise AccountError("invalid_value", f"同意の種類が正しくありません: {kind}")
        now = datetime.now(UTC)
        await self._store.revoke(user_id, kind, now)
        state = await self.status(user_id)
        if kind == "sensitive_memory":
            # 同意を撤回したら、保存済みのセンシティブな記憶も削除する
            state["sensitive_memories_deleted"] = await self._store.delete_sensitive_memories(user_id, now)
        return state

    async def declare_age(self, user_id: uuid.UUID, adult: bool | None) -> dict[str, Any]:
        """「18歳以上ですか」への答えを記録する。「いいえ」「答えない」は、18歳未満の扱い(§13.4.4)。

        18歳未満の扱いに変わったら、18歳以上だけの設定を撤回し、保存済みのセンシティブな記憶を削除する。
        """
        now = datetime.now(UTC)
        under_18 = adult is not True
        was_under_18 = await self._store.is_under_18(user_id)
        await self._store.set_age_flag(user_id, under_18, now)
        result: dict[str, Any] = {"is_under_18": under_18}
        if under_18:
            revoked = [kind for kind in ADULT_OPTIONAL if await self._store.revoke(user_id, kind, now)]
            deleted = await self._store.delete_sensitive_memories(user_id, now)
            if revoked or deleted or not was_under_18:
                result |= {"revoked": revoked, "sensitive_memories_deleted": deleted}
        return result

    async def export(self, user_id: uuid.UUID) -> dict[str, Any]:
        data = await self._store.export_data(user_id)
        return {"exported_at": datetime.now(UTC).isoformat(), "user_id": str(user_id), **data}

    async def delete(self, user_id: uuid.UUID) -> dict[str, Any]:
        now = datetime.now(UTC)
        counts = await self._store.delete_all(user_id, now)
        # 削除は、この呼び出しの中で完了する(完了の時刻を返す。§13.4.5)
        return {"deleted": True, "completed_at": now.isoformat(), "counts": counts}

    async def report(
        self,
        user_id: uuid.UUID,
        character_id: str,
        reason: str,
        note: str = "",
        session_id: uuid.UUID | None = None,
        turn_no: int | None = None,
    ) -> dict[str, Any]:
        """AI の応答の報告(不適切な応答など。§13.5.8)。会話の本文は、持たない。"""
        if reason not in REPORT_REASONS:
            raise AccountError("invalid_value", f"reason は {' / '.join(REPORT_REASONS)} のいずれかで指定してください。")
        item = {
            "reason": reason, "note": note.strip()[:500] or None,
            "session_id": session_id, "turn_no": turn_no,
        }  # fmt: skip
        report_id = await self._store.add_report(user_id, character_id, item, datetime.now(UTC))
        return {"reported": True, "id": report_id}

    async def rate(
        self,
        user_id: uuid.UUID,
        character_id: str,
        session_id: uuid.UUID,
        turn_no: int,
        scores: dict[str, int | None],
        note: str = "",
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """応答への評価(人間評価。§14、§15.1 の M6-7)。会話の本文は持たない。同じターンは、上書きする。

        `context` は、評価の対象のターンの作り方の文脈(エンジンが、本人のターンの記録から引く)。
        """
        if context is None:
            raise AccountError("turn_not_found", "評価するターンが見つかりません。")
        clean: dict[str, Any] = {}
        for axis in RATING_AXES:
            value = scores.get(axis)
            if value is None and axis != "overall":
                continue
            if not isinstance(value, int) or not 1 <= value <= 5:
                raise AccountError("invalid_value", f"{axis} は 1〜5 の整数で指定してください。")
            clean[axis] = value
        item = {
            **{axis: clean.get(axis) for axis in RATING_AXES},
            **{key: context.get(key) for key in RATING_CONTEXT},
            "note": note.strip()[:500] or None, "session_id": session_id, "turn_no": turn_no,
        }  # fmt: skip
        rating_id = await self._store.upsert_rating(user_id, character_id, item, datetime.now(UTC))
        return {"rated": True, "id": rating_id}

    async def rating_summary(self, since: datetime) -> list[dict[str, Any]]:
        """評価の集計(運営者・開発者が見る。件数と平均だけ)。"""
        return await self._store.rating_summary(since)

    async def save_rating_snapshot(self, label: str, since: datetime, min_n: int = 5) -> dict[str, Any]:
        """集計の結果を保存する(§14.1)。評価者がアカウントを削除しても、保存した集計は変わらない。

        保存するのは、版・作り方・モデルごとの件数と平均だけ。件数が `min_n` 未満の組は、平均を保存しない。
        """
        label = label.strip()
        if not 1 <= len(label) <= 100:
            raise AccountError("invalid_value", "名前(label)は、1〜100文字で指定してください。")
        if min_n < 1:
            raise AccountError("invalid_value", "min_n は 1 以上で指定してください。")
        snapshot_id = await self._store.save_rating_snapshot(label, since, min_n, datetime.now(UTC))
        return {"saved": True, "id": snapshot_id}

    async def list_rating_snapshots(self) -> list[dict[str, Any]]:
        """保存した集計(古い順)。版が上がったときの比較に使う。"""
        return await self._store.list_rating_snapshots()

    async def purge_audit_logs(self, now: datetime, retention_days: int) -> int:
        """保持期間を過ぎた監査ログを消す(ワーカーが、定期的に呼ぶ)。"""
        return await self._store.purge_audit_logs(now - timedelta(days=retention_days))

    async def pending_identity_deletions(self) -> list[str]:
        """アカウントの削除のあと、まだ消していない外部テナントのアカウント(§15.1 の M6-3)。"""
        return await self._store.pending_identity_deletions()

    async def complete_identity_deletion(self, subject: str, now: datetime) -> int:
        return await self._store.complete_identity_deletion(subject, now)
