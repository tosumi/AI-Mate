"""運営者のロールと許可(本体仕様書 §15.1 の M9 の表)。

ロールは、運営者のテナントのアプリのロール(トークンの roles)。v1.5 では固定で割り当て、権限の昇格(PIM)は
入れない(2026-11-24 のユーザーの決定)。どのロールにも、会話の本文を読む許可はない。
"""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    DESIGNER = "designer"  # ペルソナ設計者
    APPROVER = "approver"  # 承認者
    SUPPORT = "support"  # サポート
    ADMIN = "admin"  # 管理者
    AUDITOR = "auditor"  # 監査


class Permission(StrEnum):
    VIEW_SELF = "view_self"  # 自分の情報とロール
    VIEW_REPORTS = "view_reports"  # 報告の一覧・詳細(本文なし)
    HANDLE_REPORTS = "handle_reports"  # 報告の対応状況・対応メモの更新
    VIEW_RATINGS = "view_ratings"  # 評価の集計
    VIEW_AUDIT = "view_audit"  # 監査ログの閲覧
    VIEW_SYSTEM = "view_system"  # システムの状況(Azure のメトリックのダッシュボード)


# ロール → 許可(仕様書の表と同じ。変えるときは、仕様書とテストも変える)
GRANTS: dict[Role, frozenset[Permission]] = {
    Role.DESIGNER: frozenset({Permission.VIEW_SELF, Permission.VIEW_RATINGS}),
    Role.APPROVER: frozenset({Permission.VIEW_SELF, Permission.VIEW_RATINGS}),
    Role.SUPPORT: frozenset(
        {Permission.VIEW_SELF, Permission.VIEW_REPORTS, Permission.HANDLE_REPORTS, Permission.VIEW_RATINGS}
    ),
    Role.ADMIN: frozenset(
        {
            Permission.VIEW_SELF,
            Permission.VIEW_REPORTS,
            Permission.HANDLE_REPORTS,
            Permission.VIEW_RATINGS,
            Permission.VIEW_AUDIT,
            Permission.VIEW_SYSTEM,
        }
    ),
    Role.AUDITOR: frozenset({Permission.VIEW_SELF, Permission.VIEW_AUDIT}),
}


def known_roles(names: frozenset[str] | set[str]) -> list[Role]:
    """トークンのロールのうち、知っているものだけ(知らない名前は無視する)。"""
    return sorted((Role(n) for n in names if n in Role._value2member_map_), key=list(Role).index)


def permissions_for(names: frozenset[str] | set[str]) -> frozenset[Permission]:
    """ロールの組み合わせで使える許可。ロールがなければ、何も許さない。"""
    allowed: set[Permission] = set()
    for role in known_roles(names):
        allowed |= GRANTS[role]
    return frozenset(allowed)
