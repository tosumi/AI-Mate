"""キャラクタの提出・公開の保存先(テスト用のメモリ上の実装)。本番は `db.ops_store.PgPersonaStore`。

保存先の関数(0022)と同じ規則を持つ: 提出した本人は承認できない、公開の版はこれまでより大きいこと、
ロールバックは指定の版より新しい公開版を非推奨にする。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from mycompanion.ops.personas import PersonaError


def _key(label: str) -> tuple[int, ...]:
    return tuple(int(n) for n in label.split("."))


class InMemoryPersonaStore:
    def __init__(self) -> None:
        self.submissions: dict[uuid.UUID, dict[str, Any]] = {}
        self.characters: list[dict[str, Any]] = []
        self.reviews: list[dict[str, Any]] = []

    async def submit(
        self, character_id: str, label: str, definition: dict[str, Any], row: dict[str, Any],
        lint_json: list[dict[str, Any]], simulation: str, operator: str, now: datetime,
    ) -> uuid.UUID:  # fmt: skip
        submission_id = uuid.uuid4()
        self.submissions[submission_id] = {
            "submission_id": submission_id, "character_id": character_id, "version_label": label,
            "definition_json": definition, "character_row": row, "lint_json": lint_json,
            "simulation_summary": simulation, "submitted_by": operator, "submitted_at": now, "status": "pending",
            "decided_by": None, "decided_at": None, "notes": None, "published_version": None,
        }  # fmt: skip
        return submission_id

    async def list_submissions(self, status: str | None, limit: int) -> list[dict[str, Any]]:
        rows = [
            {**{k: v for k, v in s.items() if k not in ("definition_json", "character_row", "lint_json", "simulation_summary", "notes")},
             "name": s["definition_json"].get("name")}
            for s in self.submissions.values() if status is None or s["status"] == status
        ]  # fmt: skip
        return sorted(rows, key=lambda r: r["submitted_at"], reverse=True)[:limit]

    async def get_submission(self, submission_id: uuid.UUID) -> dict[str, Any] | None:
        s = self.submissions.get(submission_id)
        return {k: v for k, v in s.items() if k != "character_row"} if s else None

    async def publish(self, submission_id: uuid.UUID, operator: str, notes: str | None, now: datetime) -> int:
        s = self.submissions.get(submission_id)
        if s is None:
            raise PersonaError("not_found", "提出が見つかりません")
        if s["status"] != "pending":
            raise PersonaError("conflict", "この提出は、すでに処理されています")
        if not operator or operator == s["submitted_by"]:
            raise PersonaError("forbidden", "提出した本人は、承認できません")
        mine = [c for c in self.characters if c["character_id"] == s["character_id"]]
        published = [_key(c["version_label"]) for c in mine if c["status"] in ("published", "deprecated")]
        if published and _key(s["version_label"]) <= max(published):
            raise PersonaError("conflict", "バージョンは、これまでの公開版より大きくしてください")
        number = max((c["version"] for c in mine), default=0) + 1
        self.characters.append({
            **s["character_row"], "character_id": s["character_id"], "version": number,
            "version_label": s["version_label"], "status": "published", "created_at": now,
        })  # fmt: skip
        self.reviews.append({"character_id": s["character_id"], "version": number, "reviewer": operator, "decision": "approved", "notes": notes})
        s.update(status="approved", decided_by=operator, decided_at=now, notes=notes, published_version=number)
        return number

    async def reject(self, submission_id: uuid.UUID, operator: str, notes: str | None, now: datetime) -> bool:
        s = self.submissions.get(submission_id)
        if s is None or s["status"] != "pending":
            return False
        s.update(status="rejected", decided_by=operator, decided_at=now, notes=notes)
        return True

    async def list_versions(self, character_id: str | None) -> list[dict[str, Any]]:
        rows = [c for c in self.characters if character_id is None or c["character_id"] == character_id]
        return [
            {k: c.get(k) for k in ("character_id", "version", "version_label", "name", "status", "created_at")}
            for c in sorted(rows, key=lambda c: (c["character_id"], -c["version"]))
        ]

    async def get_version(self, character_id: str, version: int) -> dict[str, Any] | None:
        return next((dict(c) for c in self.characters if c["character_id"] == character_id and c["version"] == version), None)

    async def rollback(self, character_id: str, label: str, operator: str, now: datetime) -> int:
        targets = [c for c in self.characters if c["character_id"] == character_id and c["version_label"] == label
                   and c["status"] in ("published", "deprecated")]  # fmt: skip
        if not targets:
            raise PersonaError("not_found", "公開されたことのある版が見つかりません")
        target = max(c["version"] for c in targets)
        for c in self.characters:
            if c["character_id"] == character_id and c["version"] > target and c["status"] == "published":
                c["status"] = "deprecated"
            if c["character_id"] == character_id and c["version"] == target:
                c["status"] = "published"
        self.reviews.append({"character_id": character_id, "version": target, "reviewer": operator, "decision": "rollback"})
        return target

    async def deprecate(self, character_id: str, operator: str, now: datetime) -> int:
        count = 0
        for c in self.characters:
            if c["character_id"] == character_id and c["status"] == "published":
                c["status"] = "deprecated"
                count += 1
        return count
