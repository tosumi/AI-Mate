"""トピックの文書(OKF: Open Knowledge Format v0.2)と、MyCompanion のプロファイル(本体仕様書 §9.13)。

1つのトピック = 1つの OKF コンセプト文書(YAML の frontmatter + Markdown の本文)。
`(user_id, character_id)` ごとに、1つの仮想のバンドルを持つ(保存先は PostgreSQL の行。ファイルには置かない)。

- **文書を書くのは、コードだけ**。LLM は操作(項目の追加など)を JSON で返し、ここで文書に組み立てる。
  既存の項目を LLM に書き直させない(要約の要約による劣化と、命令の混入を防ぐ)。
- OKF の語彙を、記憶の意味に使う: `sources` = 根拠の記憶、`generated` = 最後に内容を変えた処理、
  `verified` = ユーザーの確認、`status` = トピックの確立(draft = 候補 / stable = 確立 / deprecated = 現行でない)。
- 会話のたびに変わる運用の値(mute、隔離、埋め込みなど)は、frontmatter に入れない(行の列に持つ)。
"""

from __future__ import annotations

import re
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import yaml

from mycompanion.memory.timeparse import Occurred
from mycompanion.safety import screening

OKF_VERSION = "0.2"
PROFILE_VERSION = "1.1"  # 1.0 = 初版、1.1 = トピック同士の関係(mc_relations、# 関係)

TYPE_ENTITY = "Topic Entity"  # 繰り返し話に出る、名前のある対象(人物、ペット、場所、組織、作品)
TYPE_THREAD = "Topic Thread"  # 始まりと経過がある出来事・取り組み・懸念
TYPE_ARCHIVE = "Topic Archive"  # 削除(アーカイブ)されたトピックの概要
TYPES = {"entity": TYPE_ENTITY, "thread": TYPE_THREAD}
STATUSES = ("draft", "stable", "deprecated")
ENTRY_KINDS = ("event", "state", "plan", "outcome", "correction")

# トピック同士の関係(保存するのは「正の向き」だけ。逆向きは、相手の文書から引く)
RELATION_KINDS = ("part_of", "member_of", "involves", "follows", "parallel_with")
RELATION_ROLES = ("participant", "place", "subject")  # involves だけ
# 両端の型の組み合わせ(元, 相手)
RELATION_ENDS = {
    "part_of": (("thread", "thread"), ("entity", "entity")),  # 子 → 親
    "member_of": (("entity", "entity"),),  # 構成員 → 集団
    "involves": (("thread", "entity"),),  # 出来事 → 人・場所・主題
    "follows": (("thread", "thread"),),  # 後 → 前
    "parallel_with": (("thread", "thread"),),  # 対称(片側にだけ保存する)
}
RELATION_LABELS = {
    "part_of": "全体", "member_of": "所属", "follows": "前の出来事", "parallel_with": "同じ頃",
    "participant": "参加者", "place": "場", "subject": "主題",
}  # fmt: skip
RELATIONS_MAX = 8
INVOLVES_MAX = 5

TITLE_MAX = 30
DESCRIPTION_MAX = 80
ENTRY_MAX = 120
ALIASES_MAX = 5
TAGS_MAX = 5
ARCHIVE_SUMMARY_MAX = 200

_ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
# ユーザーの性格・傾向の判断(トピックにしない。§13.1)
_PERSONALITY = re.compile(r"(?:ユーザー|相手|この人)は.{0,12}(?:性格|気質|タイプ|な人)|飽きっぽい|心配性|怠け者|神経質な人")
_EXTERNAL_LINK = re.compile(r"\]\((?!/)|https?://", re.IGNORECASE)


class OkfError(ValueError):
    """文書が、OKF またはプロファイルに適合しない。保存しない。"""


@dataclass
class Source:
    """根拠の記憶(OKF の `sources` の1件)。"""

    key: str  # m-<memory_id の先頭8文字>。本文の脚注のラベル
    memory_id: uuid.UUID | None
    told_on: date | None = None  # 話した日(脚注に、日付だけを書く)
    last_modified: datetime | None = None
    expired: bool = False  # 元の記憶が、期限切れで消えた(確立したトピックでは、項目を残す)


@dataclass
class Relation:
    """トピック同士の関係(OKF の拡張の項目 `mc_relations` の1件)。ユーザーが明言したものだけ。"""

    key: str  # r1, r2, …
    to: str  # 相手の文書のパス(バンドルの中だけ)
    kind: str
    to_title: str = ""  # 相手の名前(本文のリンクの文字)
    role: str | None = None
    basis: str = "stated"
    source_keys: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return RELATION_LABELS[self.role or self.kind]


def path_kind(path: str) -> str:
    return "entity" if path.startswith("entities/") else "thread"


@dataclass
class Entry:
    """タイムラインの項目(1行)。"""

    key: str  # t1, t2, …(文書の中で単調に増やし、再利用しない)
    kind: str  # event / state / plan / outcome / correction
    text: str
    occurred: Occurred
    told_on: date
    source_keys: list[str] = field(default_factory=list)
    told_at: datetime | None = None
    expression: str | None = None  # 元の時期の表現(「昨日から」)
    superseded_by: str | None = None  # 訂正された(この項目は「訂正済み」に移る)
    resolves: str | None = None  # 予定の結果(どの plan の項目か)
    valence: float | None = None  # 元の記憶の感情タグ
    sensitivity: str | None = None
    quarantined: bool = False

    @property
    def active(self) -> bool:
        return self.superseded_by is None and not self.quarantined


@dataclass
class TopicDoc:
    doc_id: uuid.UUID
    path: str  # entities/e-xxxxxx.md / threads/t-xxxxxx.md
    okf_type: str
    title: str
    description: str = ""
    tags: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    status: str = "draft"
    generated_by: str = ""
    generated_at: datetime | None = None
    verified: list[dict[str, str]] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)
    entries: list[Entry] = field(default_factory=list)
    next_entry_no: int = 1
    relations: list[Relation] = field(default_factory=list)
    # --- 運用の値(frontmatter に入れない) ---
    lifecycle: str = "candidate"  # candidate / active / dormant / merged
    muted: bool = False
    quarantined: bool = False
    established_at: datetime | None = None
    stale_after: datetime | None = None
    embedding: list[float] | None = None
    preference: float = 0.0  # 嗜好度(キャラクタの好みとの照合。-1〜+1)
    activation: float = 0.0  # 活性度のキャッシュ(トピックの想起から計算)
    last_mentioned_by_character_at: datetime | None = None
    last_followup_at: datetime | None = None
    followup_keys: list[str] = field(default_factory=list)  # 「その後」を聞いた予定の項目(同じ予定は、1回だけ)
    version: int = 1
    created_day: date | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def kind(self) -> str:
        return "entity" if self.okf_type == TYPE_ENTITY else "thread"

    @property
    def established(self) -> bool:
        return self.status == "stable"

    @property
    def short_id(self) -> str:
        """利用者に見せる ID(パスの識別子。例: t-3m8w1c)。"""
        return self.path.rsplit("/", 1)[-1].removesuffix(".md")

    def source(self, key: str) -> Source | None:
        return next((s for s in self.sources if s.key == key), None)

    def sorted_entries(self, include_superseded: bool = False) -> list[Entry]:
        """表示の順: 時期の昇順 → 精度の細かい順 → 話した日 → 項目の番号。時期が不明なら、話した日の位置。"""
        order = {"day": 0, "week": 1, "month": 2, "season": 3, "year": 4, "unknown": 5}
        items = [e for e in self.entries if include_superseded or e.superseded_by is None]
        return sorted(
            items,
            key=lambda e: (e.occurred.start or e.told_on, order.get(e.occurred.precision, 5), e.told_on, int(e.key[1:])),
        )

    def frontmatter(self) -> dict[str, Any]:
        active = [e for e in self.entries if e.superseded_by is None]
        dates = sorted(e.occurred.start or e.told_on for e in active)
        data: dict[str, Any] = {
            "type": self.okf_type,
            "title": self.title,
            "description": self.description,
            "tags": list(self.tags),
            "aliases": list(self.aliases),
            "status": self.status,
            "generated": {"by": self.generated_by, "at": _iso(self.generated_at)},
            "sources": [
                {
                    "id": s.key,
                    "resource": f"memory:{s.memory_id}" if s.memory_id else "memory:expired",
                    **({"last_modified": _iso(s.last_modified)} if s.last_modified else {}),
                    **({"expired": True} if s.expired else {}),
                }
                for s in self.sources
            ],
            "mc_profile": PROFILE_VERSION,
        }
        if self.relations:
            data["mc_relations"] = [
                {
                    "id": r.key, "to": f"/{r.to}", "title": r.to_title, "kind": r.kind,
                    **({"role": r.role} if r.role else {}), "basis": r.basis, "sources": list(r.source_keys),
                }
                for r in self.relations
            ]  # fmt: skip
        if self.stale_after is not None:
            data["stale_after"] = _iso(self.stale_after)
        if self.verified:
            data["verified"] = list(self.verified)
        if dates:
            data["first_occurred"], data["last_occurred"] = dates[0].isoformat(), dates[-1].isoformat()
        return data

    def body(self) -> str:
        """本文を組み立てる。節は固定(タイムライン、訂正済み、脚注)。"""
        lines: list[str] = []
        if self.relations:
            # 関係の種類は、OKF にないので、周りの文章(ラベル)で表す。機械が読む正は、frontmatter の mc_relations
            lines += ["# 関係", ""]
            lines += [
                f"- {r.label}: [{r.to_title}](/{r.to}){''.join(f'[^{k}]' for k in r.source_keys)}"
                for r in self.relations
            ]
            lines.append("")
        lines += ["# タイムライン", "", "| ID | 時期 | 話した日 | 種類 | 内容 |", "|---|---|---|---|---|"]
        lines += [_row(e) for e in self.sorted_entries()]
        corrected = [e for e in self.entries if e.superseded_by is not None]
        if corrected:
            lines += ["", "# 訂正済み", "", "| ID | 時期 | 話した日 | 種類 | 内容 |", "|---|---|---|---|---|"]
            lines += [_row(e, struck=True) for e in sorted(corrected, key=lambda e: int(e.key[1:]))]
        used = {key for e in self.entries for key in e.source_keys}
        used |= {key for r in self.relations for key in r.source_keys}
        notes = [s for s in self.sources if s.key in used]
        if notes:
            lines.append("")
            # 脚注には、日付だけを書く(会話の本文は書かない)
            lines += [f"[^{s.key}]: {s.told_on.isoformat() if s.told_on else '日付不明'} の会話" for s in notes]
        return "\n".join(lines) + "\n"

    def markdown(self) -> str:
        """OKF の文書(書き出し・表示用)。"""
        front = yaml.safe_dump(self.frontmatter(), allow_unicode=True, sort_keys=False, width=1000)
        return f"---\n{front}---\n\n{self.body()}"


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat().replace("+00:00", "Z") if moment else None


def clean_text(text: str, limit: int) -> str:
    """表の1セルに入る形にする(改行と縦棒を除く)。"""
    return re.sub(r"\s+", " ", text.replace("|", "/")).strip()[:limit]


def _row(entry: Entry, struck: bool = False) -> str:
    notes = "".join(f"[^{key}]" for key in entry.source_keys)
    text = f"~~{entry.text}~~" if struck else entry.text
    return f"| {entry.key} | {entry.occurred.label()} | {entry.told_on.isoformat()} | {entry.kind} | {text}{notes} |"


_ROW = re.compile(r"^\| (t\d+) \| (.*?) \| (\d{4}-\d{2}-\d{2}) \| (\w+) \| (.*) \|$")
_NOTE = re.compile(r"\[\^(m-[0-9a-f]{8})\]")


def parse_body(body: str) -> dict[str, dict[str, Any]]:
    """本文のタイムライン(と訂正済み)の表を読む。項目の ID → 時期の表記、話した日、種類、内容、脚注、訂正済みか。

    本文が、項目の内容の正(索引の表は、本文と元の記憶から作り直せる派生)。
    """
    rows: dict[str, dict[str, Any]] = {}
    struck = False
    for line in body.splitlines():
        if line.startswith("# "):
            struck = line.strip() == "# 訂正済み"
            continue
        match = _ROW.match(line)
        if not match:
            continue
        content = match[5]
        keys = _NOTE.findall(content)
        text = _NOTE.sub("", content).strip()
        if struck and text.startswith("~~") and text.endswith("~~"):
            text = text[2:-2]
        rows[match[1]] = {
            "label": match[2], "told_on": date.fromisoformat(match[3]), "kind": match[4],
            "text": text, "source_keys": keys, "struck": struck,
        }  # fmt: skip
    return rows


def parse_markdown(text: str) -> tuple[dict[str, Any], str]:
    """OKF の文書を、frontmatter と本文に分ける。"""
    if not text.startswith("---\n"):
        raise OkfError("frontmatter がありません")
    try:
        front, body = text[4:].split("\n---\n", 1)
        data = yaml.safe_load(front)
    except (ValueError, yaml.YAMLError) as exc:
        raise OkfError("frontmatter を解析できません") from exc
    if not isinstance(data, dict):
        raise OkfError("frontmatter が、項目の集まりではありません")
    return data, body.lstrip("\n")


def new_path(kind: str) -> str:
    """文書のパス。名前の変更や統合で変わらないよう、日本語の名前は使わず、短い固定の識別子にする。"""
    token = "".join(secrets.choice(_ID_ALPHABET) for _ in range(6))
    return f"{'entities' if kind == 'entity' else 'threads'}/{kind[0]}-{token}.md"


def source_key(memory_id: uuid.UUID) -> str:
    return f"m-{str(memory_id)[:8]}"


def validate(doc: TopicDoc) -> None:
    """保存の前の検査(§9.13 の適合性)。違反したら `OkfError`。

    1. OKF の適合性(frontmatter が解析でき、`type` が空でない)。
    2. プロファイル(必須の項目、値、長さ、本文の節、リンクがバンドル内だけ、脚注のラベルがすべて出典にある)。
    3. スクリーニング(名前・別名・説明・項目の内容に、命令文・メタ指示がない。ユーザーの性格の判断がない)。
    """
    front, body = parse_markdown(doc.markdown())
    if not front.get("type"):
        raise OkfError("type がありません")
    if doc.okf_type not in (TYPE_ENTITY, TYPE_THREAD):
        raise OkfError(f"type が不正です: {doc.okf_type}")
    if doc.status not in STATUSES:
        raise OkfError(f"status が不正です: {doc.status}")
    if not doc.title.strip() or len(doc.title) > TITLE_MAX:
        raise OkfError("title が空か、長すぎます")
    if len(doc.description) > DESCRIPTION_MAX:
        raise OkfError("description が長すぎます")
    if len(doc.aliases) > ALIASES_MAX or len(doc.tags) > TAGS_MAX:
        raise OkfError("aliases か tags が多すぎます")
    if not re.fullmatch(r"(entities/e|threads/t)-[a-z0-9]{6}\.md", doc.path):
        raise OkfError(f"パスが不正です: {doc.path}")
    if not doc.generated_by or doc.generated_at is None:
        raise OkfError("generated がありません")

    keys = {s.key for s in doc.sources}
    seen: set[str] = set()
    for entry in doc.entries:
        if entry.key in seen or not re.fullmatch(r"t\d+", entry.key):
            raise OkfError(f"項目の ID が不正です: {entry.key}")
        seen.add(entry.key)
        if entry.kind not in ENTRY_KINDS:
            raise OkfError(f"項目の種類が不正です: {entry.kind}")
        if not entry.text.strip() or len(entry.text) > ENTRY_MAX or "|" in entry.text or "\n" in entry.text:
            raise OkfError(f"項目の内容が不正です: {entry.key}")
        if not entry.source_keys or not set(entry.source_keys) <= keys:
            raise OkfError(f"項目の出典が、sources にありません: {entry.key}")
        for ref in (entry.superseded_by, entry.resolves):
            if ref is not None and ref not in {e.key for e in doc.entries}:
                raise OkfError(f"項目の参照先がありません: {entry.key} → {ref}")
    if len(doc.relations) > RELATIONS_MAX or sum(r.kind == "involves" for r in doc.relations) > INVOLVES_MAX:
        raise OkfError("関係が多すぎます")
    pairs: set[tuple[str, str]] = set()
    for relation in doc.relations:
        if relation.kind not in RELATION_KINDS or (relation.kind == "involves") != (relation.role in RELATION_ROLES):
            raise OkfError(f"関係の種類が不正です: {relation.key}")
        if not re.fullmatch(r"(entities/e|threads/t)-[a-z0-9]{6}\.md", relation.to) or relation.to == doc.path:
            raise OkfError(f"関係の相手が、バンドルの中の別の文書ではありません: {relation.key}")
        if (doc.kind, path_kind(relation.to)) not in RELATION_ENDS[relation.kind]:
            raise OkfError(f"関係の両端の型の組み合わせが不正です: {relation.key}")
        if relation.basis != "stated" or not relation.source_keys or not set(relation.source_keys) <= keys:
            raise OkfError(f"関係の根拠がありません: {relation.key}")
        if (relation.to, relation.kind) in pairs or not relation.to_title.strip():
            raise OkfError(f"関係が重複しているか、相手の名前がありません: {relation.key}")
        pairs.add((relation.to, relation.kind))
    if set(_NOTE.findall(body)) - keys:
        raise OkfError("脚注のラベルが、sources にありません")
    if set(parse_body(body)) != seen:
        raise OkfError("本文の表と、項目が一致しません")

    texts = (doc.title, doc.description, *doc.aliases, *(e.text for e in doc.entries), *(r.to_title for r in doc.relations))
    for text in texts:
        if _EXTERNAL_LINK.search(text):
            raise OkfError("バンドルの外を指すリンクは、書けません")
        if screening.is_suspicious_memory(text):
            raise OkfError("命令文・メタ指示のような内容は、文書に書けません")
        if _PERSONALITY.search(text):
            raise OkfError("ユーザーの性格・傾向の判断は、トピックにしません")


# ---------------------------------------------------------------------------
# アーカイブ(トピックの削除。概要だけを残す)
# ---------------------------------------------------------------------------


@dataclass
class TopicArchive:
    archive_id: uuid.UUID
    original_doc_id: uuid.UUID
    path: str  # archive/a-xxxxxx.md
    title: str
    summary: str  # 概要(最大200文字)。取り下げたら空
    summary_state: str  # ok / pending(作成に失敗。次の日次整理で1回だけ再試行)/ withdrawn(取り下げ)
    first_occurred: date | None
    last_occurred: date | None
    archived_at: datetime
    memory_ids: list[uuid.UUID] = field(default_factory=list)  # 根拠の記憶(削除の伝播のため)
    pending_lines: list[str] = field(default_factory=list)  # 概要の再試行に使う項目(成功したら、消す)

    @property
    def short_id(self) -> str:
        return self.path.rsplit("/", 1)[-1].removesuffix(".md")

    def frontmatter(self) -> dict[str, Any]:
        first_sentence = re.split(r"(?<=。)", self.summary, maxsplit=1)[0][:DESCRIPTION_MAX]
        data: dict[str, Any] = {
            "type": TYPE_ARCHIVE,
            "title": self.title,
            "description": first_sentence,
            "status": "deprecated",
            "mc_archived_at": _iso(self.archived_at),
            "sources": [{"id": source_key(m), "resource": f"memory:{m}"} for m in self.memory_ids],
            "mc_profile": PROFILE_VERSION,
        }
        if self.first_occurred and self.last_occurred:
            data["first_occurred"] = self.first_occurred.isoformat()
            data["last_occurred"] = self.last_occurred.isoformat()
        return data

    def body(self) -> str:
        return f"# 概要\n\n{self.summary}\n" if self.summary else "# 概要\n\n(概要はありません)\n"

    def markdown(self) -> str:
        front = yaml.safe_dump(self.frontmatter(), allow_unicode=True, sort_keys=False, width=1000)
        return f"---\n{front}---\n\n{self.body()}"

    def view(self) -> dict[str, Any]:
        return {
            "id": self.short_id,
            "title": self.title,
            "summary": self.summary,
            "summary_state": self.summary_state,
            "first_occurred": self.first_occurred.isoformat() if self.first_occurred else None,
            "last_occurred": self.last_occurred.isoformat() if self.last_occurred else None,
            "archived_at": self.archived_at.isoformat(),
        }


def new_archive_path() -> str:
    return "archive/a-" + "".join(secrets.choice(_ID_ALPHABET) for _ in range(6)) + ".md"


def index_markdown(docs: list[TopicDoc]) -> str:
    """バンドルの `index.md`(トピックの一覧)。保存せず、必要なときに合成する。"""
    lines = ["---", f'okf_version: "{OKF_VERSION}"', "---", "", "# トピック", ""]
    for doc in sorted(docs, key=lambda d: (d.okf_type, d.title)):
        if doc.established:
            lines.append(f"- [{doc.title}](/{doc.path}): {doc.description}")
    return "\n".join(lines) + "\n"


def relations_from_frontmatter(front: dict[str, Any]) -> list[Relation]:
    """frontmatter の `mc_relations` を読む(保存した文書を、読み直すとき)。"""
    return [
        Relation(
            key=str(item["id"]), to=str(item["to"]).lstrip("/"), kind=str(item["kind"]),
            to_title=str(item.get("title", "")), role=item.get("role"), basis=str(item.get("basis", "stated")),
            source_keys=[str(k) for k in item.get("sources") or []],
        )
        for item in front.get("mc_relations") or []
    ]  # fmt: skip
