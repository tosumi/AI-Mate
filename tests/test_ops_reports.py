"""運営者の API: 報告の確認・評価の集計・監査ログの閲覧(本体仕様書 §15.1 の M9 の手順2)。

いちばん大事なのは、会話の本文・ユーザーの ID が、運営者の API の応答に入らないこと。
"""

import json
import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from mycompanion.api.app import AppDeps, create_app
from mycompanion.api.auth import AuthError, OperatorPrincipal
from mycompanion.db.migrate import list_migrations
from mycompanion.ops.audit import InMemoryOperatorAuditLog
from mycompanion.ops.roles import Role
from mycompanion.ops.store import InMemoryOperatorStore
from mycompanion.ops.views import pseudonym

NOW = datetime.now(UTC)
USER = uuid.uuid4()
SESSION = uuid.uuid4()
REPORT = uuid.uuid4()
BARE_REPORT = uuid.uuid4()  # ターンを指定しない報告
# 応答に出てはいけない文字列(会話の本文・下書き・編集の内容・検出の根拠・ユーザーの ID)
SECRETS = ["秘密の入力ABC", "応答の本文XYZ", "下書きの本文QRS", "編集の内容LMN", "検出の根拠DEF", "計画の本文GHI", str(USER)]

OPERATORS = {f"tok-{r.value}": OperatorPrincipal(f"oid-{r.value}", "t", frozenset({r.value})) for r in Role}


class FakeOperatorVerifier:
    async def verify(self, token: str) -> OperatorPrincipal:
        if token not in OPERATORS:
            raise AuthError("bad")
        return OPERATORS[token]


class NoUsers:
    async def verify(self, token: str):
        raise AuthError("bad")

    async def resolve_user(self, idp: str, subject: str) -> uuid.UUID:
        raise AssertionError


def _store() -> InMemoryOperatorStore:
    store = InMemoryOperatorStore()
    store.reports = [
        {
            "report_id": REPORT, "user_id": USER, "character_id": "haru", "session_id": SESSION, "turn_no": 3,
            "reason": "inappropriate", "note": "言い方が気になりました", "status": "open", "ops_memo": None,
            "ops_updated_by": None, "ops_updated_at": None, "created_at": NOW - timedelta(hours=1),
        },
        {
            "report_id": BARE_REPORT, "user_id": USER, "character_id": "koko", "session_id": None, "turn_no": None,
            "reason": "other", "note": None, "status": "resolved", "ops_memo": None, "ops_updated_by": None,
            "ops_updated_at": None, "created_at": NOW - timedelta(hours=2),
        },
    ]  # fmt: skip
    store.turns = [
        {
            "user_id": USER, "character_id": "haru", "session_id": SESSION, "turn_no": 3, "turn_id": "t3",
            "id": "t3", "ts": NOW - timedelta(hours=1, minutes=5),
            "user_input": "秘密の入力ABC", "reply_text": "応答の本文XYZ", "draft_text": "下書きの本文QRS",
            "filtered_text": "応答の本文XYZ", "edit_summary": "編集の内容LMN", "plan_json": {"intent": "計画の本文GHI"},
            "response_mode": "brief", "filter_mode": "on", "safety_flags": ["output_softened"],
            "silence_reason": None, "gate_rule_hits_json": ["R2"], "system_notices": [],
            "character_version": 4, "compose_mode": "single", "reply_model": "gpt-x", "reply_prompt_version": "p7",
        },
    ]  # fmt: skip
    store.findings = [
        {"turn_id": "t3", "layer": "output", "category": "violence", "severity": "medium", "detector": "guardrail",
         "score": 0.7, "action": "annotate", "details": {"matched": "検出の根拠DEF"}},
    ]  # fmt: skip
    return store


@pytest.fixture
def env():
    audit = InMemoryOperatorAuditLog()
    store = _store()
    deps = AppDeps(
        verifier=NoUsers(), users=NoUsers(), ops_verifier=FakeOperatorVerifier(), ops_audit=audit, ops_store=store
    )
    return TestClient(create_app(deps)), audit, store


def _as(role: Role) -> dict[str, str]:
    return {"Authorization": f"Bearer tok-{role.value}"}


def _assert_no_secrets(response) -> None:
    text = response.text
    for secret in SECRETS:
        assert secret not in text, secret


# --- 報告 ------------------------------------------------------------------------------------------


def test_report_list_has_no_note_text_and_uses_a_pseudonym(env):
    client, _, _ = env
    response = client.get("/ops/v1/reports", headers=_as(Role.SUPPORT))
    assert response.status_code == 200
    reports = response.json()["reports"]
    assert [r["report_id"] for r in reports] == [str(REPORT), str(BARE_REPORT)]  # 新しい順
    assert reports[0]["user"] == pseudonym(USER) and reports[0]["has_note"] is True
    assert "言い方が気になりました" not in response.text  # 一覧には、本人のメモも出さない
    _assert_no_secrets(response)


def test_report_list_filters_by_status(env):
    client, _, _ = env
    body = client.get("/ops/v1/reports", params={"status": "resolved"}, headers=_as(Role.ADMIN)).json()
    assert [r["report_id"] for r in body["reports"]] == [str(BARE_REPORT)]
    assert client.get("/ops/v1/reports", params={"status": "x"}, headers=_as(Role.ADMIN)).status_code == 422


def test_report_detail_shows_guardrails_but_no_conversation_text(env):
    client, audit, _ = env
    response = client.get(f"/ops/v1/reports/{REPORT}", headers=_as(Role.SUPPORT))
    assert response.status_code == 200
    body = response.json()
    assert body["note"] == "言い方が気になりました"  # 本人が報告に書いたメモは出す(仕様書 §15.1 の M9)
    guard = body["guardrails"]
    assert guard["response_mode"] == "brief" and guard["safety_flags"] == ["output_softened"]
    assert guard["gate_rule_hits_json"] == ["R2"] and guard["reply_model"] == "gpt-x"
    assert guard["screening"] == [
        {"layer": "output", "category": "violence", "severity": "medium", "detector": "guardrail", "score": 0.7,
         "action": "annotate"}
    ]  # fmt: skip
    _assert_no_secrets(response)
    # 監査ログに、対象の報告の ID が残る
    assert audit.entries[-1].details["target"] == {"report_id": str(REPORT)}


def test_report_without_a_turn_has_no_guardrails(env):
    client, _, _ = env
    body = client.get(f"/ops/v1/reports/{BARE_REPORT}", headers=_as(Role.ADMIN)).json()
    assert body["guardrails"] is None and body["turn_no"] is None


def test_unknown_report_is_404(env):
    client, _, _ = env
    assert client.get(f"/ops/v1/reports/{uuid.uuid4()}", headers=_as(Role.ADMIN)).status_code == 404


def test_report_is_updated_and_the_memo_text_is_not_audited(env):
    client, audit, store = env
    memo = "手順どおり確認済み(メモの本文)"
    response = client.patch(
        f"/ops/v1/reports/{REPORT}", json={"status": "in_progress", "memo": memo}, headers=_as(Role.SUPPORT)
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "in_progress" and body["ops_memo"] == memo and body["ops_updated_by"] == "oid-support"
    assert store.reports[0]["status"] == "in_progress"
    _assert_no_secrets(response)
    change = [e for e in audit.entries if e.action == "report_updated"]
    assert len(change) == 1 and change[0].details == {
        "report_id": str(REPORT), "status": "in_progress", "memo_changed": True
    }  # fmt: skip
    assert all(memo not in json.dumps(e.details, ensure_ascii=False) for e in audit.entries)


def test_memo_can_be_cleared(env):
    client, _, store = env
    store.reports[0]["ops_memo"] = "前のメモ"
    body = client.patch(f"/ops/v1/reports/{REPORT}", json={"memo": ""}, headers=_as(Role.ADMIN)).json()
    assert body["ops_memo"] is None and body["status"] == "open"


@pytest.mark.parametrize(
    "payload", [{}, {"status": "closed"}, {"memo": "あ" * 501}, {"status": "open", "extra": 1}]
)
def test_invalid_updates_are_rejected(env, payload):
    client, _, store = env
    assert client.patch(f"/ops/v1/reports/{REPORT}", json=payload, headers=_as(Role.SUPPORT)).status_code == 422
    assert store.reports[0]["ops_updated_by"] is None


def test_update_of_unknown_report_is_404(env):
    client, _, _ = env
    response = client.patch(f"/ops/v1/reports/{uuid.uuid4()}", json={"status": "resolved"}, headers=_as(Role.ADMIN))
    assert response.status_code == 404


def test_update_is_not_done_when_the_change_cannot_be_audited(env):
    client, audit, store = env

    original = audit.record

    async def fail_on_change(entry):
        if entry.action == "report_updated":
            raise RuntimeError("書けません")
        await original(entry)

    audit.record = fail_on_change
    response = client.patch(f"/ops/v1/reports/{REPORT}", json={"status": "resolved"}, headers=_as(Role.SUPPORT))
    assert response.status_code == 503 and store.reports[0]["status"] == "open"


# --- ロールごとの許可(仕様書の表) -------------------------------------------------------------------

ENDPOINTS = {
    "reports": ("GET", "/ops/v1/reports", None),
    "report": ("GET", f"/ops/v1/reports/{REPORT}", None),
    "handle": ("PATCH", f"/ops/v1/reports/{REPORT}", {"status": "in_progress"}),
    "ratings": ("GET", "/ops/v1/ratings", None),
    "audit": ("GET", "/ops/v1/audit-logs", None),
}
ALLOWED_FOR = {
    Role.DESIGNER: {"ratings"},
    Role.APPROVER: {"ratings"},
    Role.SUPPORT: {"reports", "report", "handle", "ratings"},
    Role.ADMIN: {"reports", "report", "handle", "ratings", "audit"},
    Role.AUDITOR: {"audit"},
}


@pytest.mark.parametrize("role", list(Role))
def test_each_role_reaches_only_its_endpoints(env, role):
    client, audit, store = env
    for name, (method, path, payload) in ENDPOINTS.items():
        response = client.request(method, path, json=payload, headers=_as(role))
        expected = 200 if name in ALLOWED_FOR[role] else 403
        assert response.status_code == expected, (role, name)
        assert audit.entries[-1].decision == ("allowed" if expected == 200 else "denied")
    if "handle" not in ALLOWED_FOR[role]:
        assert store.reports[0]["status"] == "open"


# --- 評価の集計 -------------------------------------------------------------------------------------


def _rating(user: str, overall: int, mode: str) -> dict:
    return {
        "user_id": user, "character_id": "haru", "character_version": 4, "compose_mode": mode,
        "reply_model": "gpt-x", "reply_prompt_version": "p7", "response_mode": "full",
        "overall": overall, "persona_fit": None, "naturalness": None, "empathy": None, "ts": NOW,
    }  # fmt: skip


def test_ratings_hide_averages_of_small_groups(env):
    client, _, store = env
    store.ratings = [_rating(f"u{i}", 4, "single") for i in range(5)] + [_rating("lone", 1, "two_stage")]
    body = client.get("/ops/v1/ratings", params={"min_n": 3}, headers=_as(Role.DESIGNER)).json()
    rows = {r["compose_mode"]: r for r in body["rows"]}
    assert rows["single"]["n"] == 5 and rows["single"]["overall"] == 4.0
    assert rows["two_stage"]["n"] == 1 and rows["two_stage"]["overall"] is None and rows["two_stage"]["low"] is None
    assert "lone" not in json.dumps(body) and "user_id" not in json.dumps(body)


def test_ratings_min_n_has_a_floor(env):
    client, _, _ = env
    assert client.get("/ops/v1/ratings", params={"min_n": 1}, headers=_as(Role.ADMIN)).status_code == 422


# --- 監査ログの閲覧 ---------------------------------------------------------------------------------


def test_audit_logs_use_pseudonyms_and_only_known_details(env):
    client, _, store = env
    store.audit_logs = [
        {"log_id": uuid.uuid4(), "ts": NOW - timedelta(minutes=2), "actor_type": "user", "actor_id": str(USER),
         "action": "account_deleted", "target_user_id": USER, "details_json": {}},
        {"log_id": uuid.uuid4(), "ts": NOW - timedelta(minutes=1), "actor_type": "operator", "actor_id": "oid-x",
         "action": "GET /ops/v1/me", "target_user_id": None,
         "details_json": {"decision": "allowed", "roles": ["admin"], "permission": "view_self", "idp_subject": "s-1"}},
    ]  # fmt: skip
    response = client.get("/ops/v1/audit-logs", headers=_as(Role.AUDITOR))
    entries = response.json()["entries"]
    assert [e["actor_type"] for e in entries] == ["operator", "user"]  # 新しい順
    assert entries[0]["details"] == {"decision": "allowed", "roles": ["admin"], "permission": "view_self"}
    assert entries[1]["actor_id"] == pseudonym(USER) and entries[1]["target_user"] == pseudonym(USER)
    assert "s-1" not in response.text
    _assert_no_secrets(response)
    only_ops = client.get("/ops/v1/audit-logs", params={"actor_type": "operator"}, headers=_as(Role.ADMIN)).json()
    assert [e["actor_type"] for e in only_ops["entries"]] == ["operator"]


def test_pseudonym_is_stable_and_does_not_contain_the_id():
    assert pseudonym(USER) == pseudonym(str(USER)) != pseudonym(uuid.uuid4())
    assert re.fullmatch(r"u-[0-9a-f]{16}", pseudonym(USER)) and str(USER) not in pseudonym(USER)


def test_ops_data_is_unavailable_without_a_store():
    deps = AppDeps(
        verifier=NoUsers(), users=NoUsers(), ops_verifier=FakeOperatorVerifier(), ops_audit=InMemoryOperatorAuditLog()
    )
    client = TestClient(create_app(deps))
    assert client.get("/ops/v1/reports", headers=_as(Role.ADMIN)).status_code == 503


def test_web_origin_may_send_report_updates():
    # 運営者の画面(同じ配信元の /ops)から、報告の更新(PATCH)を送れる(CORS の事前確認)
    origin = "https://web.example.test"
    deps = AppDeps(
        verifier=NoUsers(), users=NoUsers(), ops_verifier=FakeOperatorVerifier(), ops_audit=InMemoryOperatorAuditLog(),
        ops_store=_store(), web_origins=(origin,),
    )  # fmt: skip
    response = TestClient(create_app(deps)).options(
        f"/ops/v1/reports/{REPORT}",
        headers={"Origin": origin, "Access-Control-Request-Method": "PATCH", "Access-Control-Request-Headers": "authorization,content-type"},
    )  # fmt: skip
    assert response.status_code == 200 and "PATCH" in response.headers["access-control-allow-methods"]


# --- マイグレーション(運営者用の関数) -----------------------------------------------------------------


def test_ops_functions_are_definer_functions_granted_only_to_the_app_role():
    sql = dict(list_migrations())["0021_ops_reports.sql"]
    names = re.findall(r"CREATE OR REPLACE FUNCTION (ops_\w+)", sql)
    assert set(names) == {"ops_list_reports", "ops_get_report", "ops_update_report", "ops_list_audit_logs"}
    assert sql.count("SECURITY DEFINER SET search_path = public, pg_temp") == len(names)
    for name in names:
        assert re.search(rf"REVOKE ALL ON FUNCTION {name}\(.*\) FROM PUBLIC;", sql)
        assert re.search(rf"GRANT EXECUTE ON FUNCTION {name}\(.*\) TO mycompanion_app;", sql)
    # アプリのロールの、報告の表への権限は広げない(更新は関数だけ)
    assert "GRANT UPDATE" not in sql and "GRANT SELECT" not in sql
