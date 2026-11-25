"""運営者の画面からのキャラクタの提出・公開と、実際のモデルでの試し(本体仕様書 §15.1 の M10 の手順4)。

- 提出(設計者)と公開(承認者)を、別の許可にする。提出した本人は承認できない(保存先の関数でも確かめる)。
- 検査は CLI と同じ(lint、標準シナリオと専用シナリオの模擬)。公開の行は CLI と同じ形(`persona.io.to_character_row`)。
- 実際のモデルでの試しは、状態と記憶をメモリ上に置き、DB に書かない。回数に上限を設ける。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from mycompanion.channel.types import TurnInput
from mycompanion.config import EngineSettings
from mycompanion.engine.engine import Engine
from mycompanion.engine.stores import InMemoryStores
from mycompanion.llm.client import LLMClient
from mycompanion.persona import io as pio
from mycompanion.persona import publish as pub
from mycompanion.persona import simulate as sim
from mycompanion.persona.lint import LintReport, lint
from mycompanion.ratelimit import SlidingWindowLimiter

NOTES_MAX = 1000


class PersonaError(Exception):
    """運営者に、そのまま伝えてよい失敗。`kind` は HTTP の状態に対応させる。"""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind  # invalid / not_found / forbidden / conflict / limited
        self.message = message


class PersonaStore(Protocol):
    async def submit(
        self, character_id: str, label: str, definition: dict[str, Any], row: dict[str, Any], lint_json: list[dict[str, Any]],
        simulation: str, operator: str, now: datetime,
    ) -> uuid.UUID: ...
    async def list_submissions(self, status: str | None, limit: int) -> list[dict[str, Any]]: ...
    async def get_submission(self, submission_id: uuid.UUID) -> dict[str, Any] | None: ...
    async def publish(self, submission_id: uuid.UUID, operator: str, notes: str | None, now: datetime) -> int: ...
    async def reject(self, submission_id: uuid.UUID, operator: str, notes: str | None, now: datetime) -> bool: ...
    async def list_versions(self, character_id: str | None) -> list[dict[str, Any]]: ...
    async def get_version(self, character_id: str, version: int) -> dict[str, Any] | None: ...
    async def rollback(self, character_id: str, label: str, operator: str, now: datetime) -> int: ...
    async def deprecate(self, character_id: str, operator: str, now: datetime) -> int: ...


# ---------------------------------------------------------------------------
# 検査(提出の前と、承認の前)
# ---------------------------------------------------------------------------


def _findings(report: LintReport) -> list[dict[str, str]]:
    return [{"code": f.code, "level": f.level, "message": f.message, "path": f.path} for f in report.findings]


async def inspect(raw: dict[str, Any], personas_root: Path | None = None) -> dict[str, Any]:
    """lint と、標準シナリオ・専用シナリオの模擬。提出できるか(止める理由)も返す。"""
    report = lint(raw)
    result: dict[str, Any] = {"ok": False, "findings": _findings(report), "scenarios": None, "blocking": []}
    if report.definition is None:
        result["blocking"] = ["定義の形が正しくありません(lint のエラーを確認してください)"]
        return result
    persona = sim.persona_from_definition(report.definition)
    scenarios = sim.scenarios_for(report.definition.character_id, personas_root)
    reports = await asyncio.to_thread(lambda: asyncio.run(sim.run_scenarios(persona, scenarios)))
    result["scenarios"] = {
        "passed": sum(1 for r in reports if r.ok), "total": len(reports),
        "failed": [{"scenario": r.scenario_id, "title": r.title, "problems": [*r.violations, *r.failures]} for r in reports if not r.ok],
        "modes": sim.mode_distribution(reports),
    }  # fmt: skip
    result["blocking"] = pub.blocking_reasons("draft", report, reports, for_publish=False)
    result["ok"] = not result["blocking"]
    return result


def _summary(inspection: dict[str, Any]) -> str:
    s = inspection["scenarios"] or {}
    return f"mock: {s.get('passed', 0)}/{s.get('total', 0)} 合格"


# ---------------------------------------------------------------------------
# 提出・公開
# ---------------------------------------------------------------------------


class PersonaPublisher:
    def __init__(self, store: PersonaStore, personas_root: Path | None = None) -> None:
        self._store = store
        self._root = personas_root

    async def submit(self, raw: dict[str, Any], operator: str, now: datetime) -> dict[str, Any]:
        inspection = await inspect(raw, self._root)
        if not inspection["ok"]:
            raise PersonaError("invalid", "提出できません: " + " / ".join(inspection["blocking"]))
        definition = lint(raw).definition
        assert definition is not None
        try:
            pub.parse_version(definition.version)
        except pub.PublishError as exc:
            raise PersonaError("invalid", str(exc)) from None
        stored = {**raw, "status": "review"}
        row = pio.to_character_row(definition)
        submission_id = await self._store.submit(
            definition.character_id, definition.version, stored, json.loads(json.dumps(row, default=str)),
            inspection["findings"], _summary(inspection), operator, now,
        )  # fmt: skip
        return {"submission_id": str(submission_id), "inspection": inspection}

    async def detail(self, submission_id: uuid.UUID) -> dict[str, Any]:
        row = await self._store.get_submission(submission_id)
        if row is None:
            raise PersonaError("not_found", "提出が見つかりません")
        definition = row["definition_json"]
        # 公開中の版との違い(初めての公開なら、なし)
        versions = await self._store.list_versions(row["character_id"])
        current = next((v for v in versions if v["status"] == "published"), None)
        changes: list[dict[str, Any]] = []
        if current is not None:
            published = await self._store.get_version(row["character_id"], current["version"])
            definition_obj = lint(definition).definition
            if published is not None and definition_obj is not None:
                proposed = pio.to_character_row(definition_obj)
                columns = [c for c in proposed if c not in ("character_id", "version_label", "status")]
                changes = [
                    {"path": path, "before": before, "after": after}
                    for path, before, after in pub.diff_rows(
                        {c: published.get(c) for c in columns}, {c: json.loads(json.dumps(proposed[c], default=str)) for c in columns}
                    )
                ]
        return {
            **{k: _jsonable(v) for k, v in row.items() if k != "definition_json"},
            "definition": definition,
            "current_version": _jsonable(current) if current else None,
            "changes": [{k: _jsonable(v) for k, v in c.items()} for c in changes],
        }

    async def approve(self, submission_id: uuid.UUID, operator: str, notes: str | None, now: datetime) -> dict[str, Any]:
        row = await self._store.get_submission(submission_id)
        if row is None:
            raise PersonaError("not_found", "提出が見つかりません")
        if row["status"] != "pending":
            raise PersonaError("conflict", "この提出は、すでに処理されています")
        if row["submitted_by"] == operator:
            # 設計者と承認者を分ける(画面・API・保存先の関数の3か所で確かめる)
            raise PersonaError("forbidden", "提出した本人は、承認できません")
        warnings = [f for f in row["lint_json"] if f.get("level") == "warning"]
        if warnings and not (notes or "").strip():
            codes = ", ".join(sorted({f["code"] for f in warnings}))
            raise PersonaError("invalid", f"警告({codes})があるので、確認した理由のメモが必要です")
        number = await self._store.publish(submission_id, operator, (notes or "").strip() or None, now)
        return {"published_version": number, "character_id": row["character_id"], "version_label": row["version_label"]}

    async def reject(self, submission_id: uuid.UUID, operator: str, notes: str | None, now: datetime) -> None:
        if not (notes or "").strip():
            raise PersonaError("invalid", "差し戻す理由を書いてください")
        if not await self._store.reject(submission_id, operator, notes, now):
            raise PersonaError("conflict", "提出が見つからないか、すでに処理されています")

    async def versions(self) -> list[dict[str, Any]]:
        """キャラクタごとの版の一覧(新しい順)。"""
        grouped: dict[str, dict[str, Any]] = {}
        for row in await self._store.list_versions(None):
            item = grouped.setdefault(row["character_id"], {"character_id": row["character_id"], "name": row["name"], "versions": []})
            item["versions"].append({k: _jsonable(row[k]) for k in ("version", "version_label", "status", "created_at")})
        return list(grouped.values())

    async def diff(self, character_id: str, first: int, second: int) -> list[dict[str, Any]]:
        rows = [await self._store.get_version(character_id, v) for v in (first, second)]
        if rows[0] is None or rows[1] is None:
            raise PersonaError("not_found", "版が見つかりません")
        columns = [c for c in rows[0] if c not in ("character_id", "version", "status", "created_at", "version_label")]
        return [
            {"path": p, "before": _jsonable(b), "after": _jsonable(a)}
            for p, b, a in pub.diff_rows({c: rows[0][c] for c in columns}, {c: rows[1][c] for c in columns})
        ]

    async def rollback(self, character_id: str, label: str, operator: str, now: datetime) -> int:
        try:
            pub.parse_version(label)
        except pub.PublishError as exc:
            raise PersonaError("invalid", str(exc)) from None
        return await self._store.rollback(character_id, label, operator, now)

    async def deprecate(self, character_id: str, operator: str, now: datetime) -> int:
        return await self._store.deprecate(character_id, operator, now)


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# 実際のモデルでの試し(運営者だけ。DB に書かない)
# ---------------------------------------------------------------------------

PREVIEW_TTL_SECONDS = 30 * 60
PREVIEW_MAX_SESSIONS = 20
CHAT_LIMIT_PER_HOUR = 60
LIVE_SIMULATIONS_PER_HOUR = 5
LIVE_SCENARIOS_MAX = 3


@dataclass
class _PreviewSession:
    owner: str
    digest: str
    engine: Engine
    character_id: str
    user_id: uuid.UUID
    last_used: float


class LivePreview:
    def __init__(self, llm: LLMClient, settings: EngineSettings | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        self._llm = llm
        self._settings = settings or EngineSettings()
        self._clock = clock
        self._sessions: OrderedDict[str, _PreviewSession] = OrderedDict()
        self._chat_limit = SlidingWindowLimiter(CHAT_LIMIT_PER_HOUR, 3600, clock)
        self._sim_limit = SlidingWindowLimiter(LIVE_SIMULATIONS_PER_HOUR, 3600, clock)

    @staticmethod
    def _key(operator: str) -> uuid.UUID:
        return uuid.uuid5(uuid.NAMESPACE_URL, f"mycompanion-ops:{operator}")

    def _expire(self) -> None:
        now = self._clock()
        for key in [k for k, s in self._sessions.items() if now - s.last_used > PREVIEW_TTL_SECONDS]:
            del self._sessions[key]
        while len(self._sessions) > PREVIEW_MAX_SESSIONS:
            self._sessions.popitem(last=False)

    async def chat(self, raw: dict[str, Any], operator: str, session_id: str | None, text: str) -> dict[str, Any]:
        report = lint(raw)
        if report.definition is None or not report.ok:
            raise PersonaError("invalid", "定義にエラーがあるため、試せません")
        if not self._chat_limit.allow(self._key(operator)):
            raise PersonaError("limited", "試しの回数が多すぎます。しばらく待ってから、もう一度お試しください")
        self._expire()
        digest = hashlib.sha256(json.dumps(raw, sort_keys=True, default=str).encode()).hexdigest()
        session = self._sessions.get(session_id or "")
        if session is None or session.owner != operator or session.digest != digest:
            # 定義を変えたら、新しい会話にする(前の定義の感情・関係を持ち越さない)
            persona = sim.persona_from_definition(report.definition)
            stores = InMemoryStores()
            stores.add_character(persona)
            session_id = str(uuid.uuid4())
            session = _PreviewSession(operator, digest, Engine(self._llm, stores, self._settings), persona.character_id, uuid.uuid4(), 0)
            self._sessions[session_id] = session
        session.last_used = self._clock()
        self._sessions.move_to_end(session_id or "")
        result = await session.engine.handle_turn(
            session.user_id, TurnInput(channel="web", character_id=session.character_id, text=text), use_memory=False
        )
        return {"session_id": session_id, "actions": [a.model_dump() for a in result.output.actions]}

    async def simulate(self, raw: dict[str, Any], operator: str, scenario_ids: list[str], personas_root: Path | None) -> dict[str, Any]:
        report = lint(raw)
        if report.definition is None or not report.ok:
            raise PersonaError("invalid", "定義にエラーがあるため、試せません")
        if not 1 <= len(scenario_ids) <= LIVE_SCENARIOS_MAX:
            raise PersonaError("invalid", f"シナリオは1〜{LIVE_SCENARIOS_MAX}件を選んでください")
        scenarios = [s for s in sim.scenarios_for(report.definition.character_id, personas_root) if s.id in scenario_ids]
        if len(scenarios) != len(set(scenario_ids)):
            raise PersonaError("invalid", "シナリオが見つかりません")
        if not self._sim_limit.allow(self._key(operator)):
            raise PersonaError("limited", "実際のモデルでのシミュレーションの回数が多すぎます。しばらく待ってから、もう一度お試しください")
        persona = sim.persona_from_definition(report.definition)
        reports = await sim.run_scenarios(persona, scenarios, live=self._llm)
        return {"scenarios": [r.as_dict() for r in reports], "modes": sim.mode_distribution(reports), "at": datetime.now(UTC).isoformat()}


def scenario_list(character_id: str | None = None, personas_root: Path | None = None) -> list[dict[str, str]]:
    scenarios = sim.scenarios_for(character_id, personas_root) if character_id else sim.load_scenarios()
    return [{"id": s.id, "title": s.title} for s in scenarios]
