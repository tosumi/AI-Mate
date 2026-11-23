"""郵便番号データから、住所の番地のマスクに使う「町名の辞書」を作る(本体仕様書 §11.3)。

入力: 日本郵便の「住所の郵便番号(1レコード1行、UTF-8形式)」の全国一括の zip(utf_ken_all.zip)。
出力: src/mycompanion/safety/data/towns_ja.txt.gz(町名を1行に1つ)と、towns_ja.meta.json(出典と件数)。

    uv run python -I scripts/build_town_dictionary.py <utf_ken_all.zip のパス>

ダウンロードしたファイルは、信頼できないデータとして扱う: 展開せずに zip から直接読み、中身は文字列としてだけ使う。
郵便番号データは、日本郵便が著作権を主張しておらず、自由に配布してよいとされている。

整理の規則:
- 「以下に掲載がない場合」は、町名ではないので除く。
- 「〇〇市の次に番地がくる場合」「〇〇村一円」は、町名がない市区町村なので、その市区町村名を町名として入れる
  (「島原市1234番地」のような住所の手がかりにする)。
- 超高層ビルの階ごとの行(「桜丘町〇〇タワー(7階)」)は除く。同じ町の、ビルを除いた行が別にある。
- 括弧の中(「(次のビルを除く)」「(1〜19丁目)」、京都の通り名など)は除く。
- 地割の範囲(「湯之沢31地割〜湯之沢35地割」)は、最初の数字の前までを町名にする。
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import re
import sys
import unicodedata
import zipfile
from datetime import UTC, datetime
from pathlib import Path

OUTPUT = Path(__file__).resolve().parent.parent / "src" / "mycompanion" / "safety" / "data"
_PAREN = re.compile(r"（[^）]*）|\([^)]*\)")
_BUILDING_FLOOR = re.compile(r"（(?:[0-9０-９]+階|地階・階層不明)）")
_SPECIAL = re.compile(r"^(?P<name>.+?)(?:の次に番地がくる場合|一円)$")
_DIGIT = re.compile(r"[0-9０-９]")


def clean(town: str, city: str) -> list[str]:
    """1行の町域名から、辞書に入れる町名(0〜1件)を作る。"""
    if town == "以下に掲載がない場合" or _BUILDING_FLOOR.search(town):
        return []
    special = _SPECIAL.match(town)
    if special:
        # 町名がない市区町村: 市区町村名(郡は除く)を、町名として使う
        return [special["name"]]
    name = _PAREN.sub("", town)
    if "地割" in name or "〜" in name or "、" in name:
        found = _DIGIT.search(name)
        name = name[: found.start()] if found else name.split("、")[0]
    name = unicodedata.normalize("NFKC", name).strip()
    return [name] if name else []


def main() -> int:
    if len(sys.argv) != 2:
        print("使い方: build_town_dictionary.py <utf_ken_all.zip>", file=sys.stderr)
        return 2
    source = Path(sys.argv[1])
    with zipfile.ZipFile(source) as archive:
        members = [n for n in archive.namelist() if n.lower().endswith(".csv")]
        if len(members) != 1:
            print(f"エラー: zip の中の CSV が1つではありません: {members}", file=sys.stderr)
            return 1
        info = archive.getinfo(members[0])
        if info.file_size > 100_000_000:
            print("エラー: CSV が大きすぎます", file=sys.stderr)
            return 1
        text = archive.read(info).decode("utf-8")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows or any(len(r) != 15 for r in rows):
        print("エラー: 列の数が、想定(15列)と違います", file=sys.stderr)
        return 1
    towns: set[str] = set()
    for row in rows:
        towns.update(clean(row[8], row[7]))
    OUTPUT.mkdir(parents=True, exist_ok=True)
    body = "\n".join(sorted(towns)) + "\n"
    # 作成の日時を入れない(同じ入力からは、同じファイルを作る)
    with gzip.GzipFile(OUTPUT / "towns_ja.txt.gz", "wb", mtime=0) as handle:
        handle.write(body.encode("utf-8"))
    meta = {
        "source": "日本郵便 住所の郵便番号(1レコード1行、UTF-8形式)utf_ken_all.zip",
        "source_csv_modified": datetime(*info.date_time, tzinfo=UTC).date().isoformat(),
        "records": len(rows),
        "towns": len(towns),
        "license": "郵便番号データは、日本郵便株式会社が著作権を主張しておらず、自由に配布してよいとされている",
    }
    (OUTPUT / "towns_ja.meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"町名 {len(towns)}件(元の行 {len(rows)}件)を書き出しました")
    return 0


if __name__ == "__main__":
    sys.exit(main())
