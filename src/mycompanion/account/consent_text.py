"""同意画面の文面(`legal/consent.md` のドラフト)を読み、画面ごとに分ける。

- 文面の正は `legal/consent.md`。API のコンテナに含めるため、同じ内容を、このパッケージに `consent.md` として置く
  (2つが一致していることは、自動テストで確かめる)。
- 同意を記録するときは、表示した画面の文面のハッシュ(SHA-256)も残す(何に同意したかを、あとで示せるように)。
- ドラフトには、未記入の箇所(【 】)がある。文面は、弁護士等の確認を経て確定する。確定するまで、版に `-draft` を付ける。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources

# 画面と、その画面のチェックボックス(上から順)に対応する同意の種類
SCREEN_ITEMS: dict[str, tuple[str, ...]] = {
    "1": ("terms", "privacy"),
    "2": ("emotion_data", "memory"),
    "3": ("third_party_ai", "apac_processing"),
    "4": (),  # 年齢の質問(同意ではない)
    "4'": ("guardian_confirmed",),
    "5": ("sensitive_memory", "romantic_expression"),
}
# CLI・Web では表示しない画面(ストアアプリ用)
SKIPPED_SCREENS = ("4''",)

_HEADING = re.compile(r"^## 画面(\S+)\s*(.*)$")
_CHECKBOX = re.compile(r"^- \[ \] (.+)$")
_BUTTONS = re.compile(r"^(\[[^\]]+\]\s*)+$")
_VERSION = re.compile(r"^版:\s*([0-9.]+)(.*)$", re.MULTILINE)


@dataclass(frozen=True)
class Screen:
    id: str  # 1 / 2 / 3 / 4 / 4' / 5
    title: str
    text: str  # 画面に表示する文面(チェックボックスと、ボタンの行は除く)
    labels: tuple[str, ...]  # チェックボックスの文面(上から順)
    types: tuple[str, ...]  # 対応する同意の種類
    text_hash: str  # 文面(チェックボックスを含む)の SHA-256


@dataclass(frozen=True)
class ConsentDocument:
    version: str
    screens: dict[str, Screen]

    def screen_of(self, kind: str) -> Screen | None:
        return next((s for s in self.screens.values() if kind in s.types), None)


def parse(markdown: str) -> ConsentDocument:
    match = _VERSION.search(markdown)
    version = match.group(1) if match else "0"
    if match is None or "【" in match.group(2):
        version += "-draft"  # 制定日が未記入のあいだは、ドラフト

    screens: dict[str, Screen] = {}
    current: tuple[str, str] | None = None
    quoted: list[str] = []

    def close() -> None:
        if current is None or current[0] in SKIPPED_SCREENS:
            return
        body, labels = [], []
        for line in quoted:
            if _BUTTONS.match(line):
                continue
            box = _CHECKBOX.match(line)
            if box:
                labels.append(box.group(1))
            else:
                body.append(line)
        types = SCREEN_ITEMS.get(current[0], ())
        if len(labels) != len(types):
            raise ValueError(f"同意書の画面{current[0]}のチェックボックスの数が、想定と違います")
        full = "\n".join(quoted).strip()
        screens[current[0]] = Screen(
            id=current[0],
            title=current[1],
            text="\n".join(body).strip(),
            labels=tuple(labels),
            types=types,
            text_hash=hashlib.sha256(full.encode("utf-8")).hexdigest(),
        )

    for raw in markdown.splitlines():
        heading = _HEADING.match(raw)
        if heading:
            close()
            current, quoted = (heading.group(1), heading.group(2).strip()), []
        elif raw.startswith("## "):
            close()
            current, quoted = None, []
        elif current is not None and raw.startswith(">"):
            quoted.append(raw[1:].strip())
    close()
    missing = [key for key in SCREEN_ITEMS if key not in screens]
    if missing:
        raise ValueError(f"同意書に、画面が見つかりません: {', '.join(missing)}")
    return ConsentDocument(version=version, screens=screens)


@lru_cache(maxsize=1)
def load() -> ConsentDocument:
    text = (resources.files("mycompanion.account") / "consent.md").read_text(encoding="utf-8")
    return parse(text)
