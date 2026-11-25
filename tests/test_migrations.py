"""マイグレーションの静的な検査。

実際の PostgreSQL での RLS の検証は、Azure の dev 用 DB に接続するテスト(azure マーカー)で行う。
ここでは、分離の対象から漏れたテーブルがないことを、SQL の文面から確認する。
"""

import re

from mycompanion.db.migrate import checksum, list_migrations

# (user_id, character_id) で分離しないことを意図しているテーブル
CHARACTER_SCOPE_EXEMPT = {
    "characters",  # キャラクタ定義(全ユーザー共通、読み取りのみ)
    "character_reviews",  # 公開前のレビューの記録(設計用。アプリのロールには読ませない)
    "persona_submissions",  # 0022: 運営者の提出(設計用。関数からだけ読み書きする)
}
# ユーザーのデータを持たず、個別の方針で保護しているテーブル
REFERENCE_TABLES = {
    "characters", "audit_logs", "personality_types", "quiz_bank", "character_reviews",
    "rating_snapshots",  # 0017: 人間評価の集計の保存(件数と平均だけ。関数からだけ読み書きする)
    "persona_submissions",  # 0022: 運営者のキャラクタの提出(関数からだけ読み書きする。ユーザーのデータなし)
}  # fmt: skip

SCOPED_MARKER = "-- (user_id, character_id) で分離するテーブル"
USER_MARKER = "-- ユーザー単位で分離するテーブル"
# 0003 以降で追加した、ユーザー単位で分離するテーブル(個別の文で方針を付けている)
LATER_USER_TABLES = {"user_throttle", "rate_limits", "proactive_quota", "push_subscriptions"}  # 0018・0020
# 0009 以降で追加した、(user_id, character_id) で分離するテーブル
LATER_SCOPED_TABLES = {
    "companion_rule_counters",
    # 0011: トピック別の時系列記憶
    "topic_doc", "topic_doc_source", "topic_entry", "topic_doc_revision", "topic_archive",
    "topic_recall_events",  # 0012
    "response_reports",  # 0014
    "response_ratings",  # 0016
    "proactive_state",  # 0018
}  # fmt: skip


def _sql() -> str:
    return "\n".join(body.replace("\r\n", "\n") for _, body in list_migrations())


def _tables(sql: str) -> dict[str, str]:
    return {m.group(1): m.group(2) for m in re.finditer(r"CREATE TABLE (\w+) \((.*?)\n\);", sql, re.DOTALL)}


def _policy_list(sql: str, marker: str) -> set[str]:
    block = sql[sql.index(marker) :]
    array = re.search(r"ARRAY ARRAY\[(.*?)\]", block, re.DOTALL).group(1)
    return set(re.findall(r"'(\w+)'", array))


def test_migrations_are_ordered_and_named():
    names = [name for name, _ in list_migrations()]
    assert names and names == sorted(names)
    assert all(re.fullmatch(r"\d{4}_\w+\.sql", name) for name in names)


def test_checksum_ignores_line_endings():
    assert checksum("a\r\nb") == checksum("a\nb")


def test_every_character_scoped_table_has_isolation_policy():
    sql = _sql()
    tables = _tables(sql)
    with_character = {name for name, cols in tables.items() if re.search(r"^\s+character_id\s", cols, re.MULTILINE)}
    assert with_character - CHARACTER_SCOPE_EXEMPT == _policy_list(sql, SCOPED_MARKER) | LATER_SCOPED_TABLES
    for table in LATER_SCOPED_TABLES:
        policy = f"CREATE POLICY {table}_isolation ON {table} FOR ALL TO mycompanion_app"
        block = sql[sql.index(policy) :]
        assert "character_id = app_character_id()" in block[:300]


def test_every_table_has_rls():
    sql = _sql()
    protected = (
        _policy_list(sql, SCOPED_MARKER) | _policy_list(sql, USER_MARKER) | REFERENCE_TABLES | LATER_USER_TABLES
        | LATER_SCOPED_TABLES
    )
    for table in LATER_USER_TABLES:
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in sql
        assert f"CREATE POLICY {table}_isolation ON {table} FOR ALL TO mycompanion_app" in sql
    assert set(_tables(sql)) == protected
    # どのテーブルも、行レベルセキュリティを有効にしている
    for table in REFERENCE_TABLES:
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in sql


def test_app_role_cannot_bypass_rls():
    sql = _sql()
    assert "CREATE ROLE mycompanion_app NOLOGIN NOBYPASSRLS" in sql


def test_safety_state_is_minimal():
    # user_safety_state は最小限の列だけを持つ(§13.4.6)。列を増やす変更は、ここで止める
    columns = re.findall(r"^\s+(\w+)\s", _tables(_sql())["user_safety_state"], re.MULTILINE)
    assert columns == ["user_id", "last_crisis_signal_at", "exempt_until", "updated_at"]
