"""送信前の、連絡先と住所の番地のマスク(本体仕様書 §11.3、§13.3)。

ユーザーの発話に含まれる**連絡先**(電話番号、メールアドレス、郵便番号)と、**住所の番地**を、LLM に送る前に置き換える。
置き換えたあとの文面を、推定・下書き・会話の記録・記憶のすべてで使う(記憶にも残らない)。

- 氏名は、マスクしない(名前を隠すと、呼び名や、友人・家族の話が成り立たなくなるため。2026-11-17 の決定)。
- 住所は、**番地まで書かれたもの**だけを対象にし、番地以降(建物名・部屋番号を含む)を「[番地]」に置き換える。
  町名までは残す(「東京都渋谷区桜丘町[番地]」)。番地のない住所・住所の一部(「渋谷区」「新宿3丁目」)は、
  個人を特定しないので、マスクしない(2026-11-19 の決定)。
- 番地の手がかりは、**郵便番号データの町名**(`data/towns_ja.txt.gz`。`scripts/build_town_dictionary.py` で作る)。
  町名の直後に、形のはっきりした番地(「1-2-3」「9番1号」「123番地」「6の12の1」など)が続くときだけ置き換える。
  町名のない数字(「2-3駅」「3番窓口」)には反応しない。番地の形は厳しめにして、取りこぼしを受け入れる
  (取りこぼす書き方は、個人の特定に至らないと判断した。2026-11-19 の決定)。
- 漢数字は、算用数字と同じに扱う。そのほかの表記ゆれ(ヶ / ケ、旧字など)は、扱わない(§11.3 の考慮事項)。
- 日付・金額・時刻などの数字を、誤って消さないように、形のはっきりしたものだけを対象にする。
- 何を置き換えたかは、種類と件数だけを返す(元の値は、どこにも残さない)。
"""

from __future__ import annotations

import functools
import gzip
import re
import unicodedata
from pathlib import Path

# 全角の数字・記号でも書かれるので、判定は、互換の正規化(NFKC)をした写しで行う
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
# 日本の電話番号: 0 で始まる10〜11桁(区切りは、ハイフン・空白・括弧)。国番号つき(+81)も
_PHONE = re.compile(
    r"(?<![\d-])(?:\+81[-\s]?\d{1,4}|0\d{1,4})[-\s(]?\d{1,4}[-\s)]?\d{3,4}(?![\d-])"
)
# 郵便番号: 〒 つき、または 3桁-4桁
_POSTAL = re.compile(r"〒\s?\d{3}-?\d{4}|(?<![\d-])\d{3}-\d{4}(?![\d-])")

MASKS = (("email", _EMAIL, "[メールアドレス]"), ("postal_code", _POSTAL, "[郵便番号]"), ("phone", _PHONE, "[電話番号]"))

# --- 住所の番地 ------------------------------------------------------------

TOWNS_FILE = Path(__file__).parent / "data" / "towns_ja.txt.gz"
BANCHI_LABEL = "[番地]"
_KANJI_DIGITS = {"〇": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_KANJI_UNITS = {"十": 10, "百": 100, "千": 1000}
# 数字: 算用数字と漢数字を、同じに扱う
D = "[0-9〇一二三四五六七八九十百千]"
_NUMBER_RUN = re.compile(rf"{D}+")
_DASH = "[-‐‑–−ー]"
# 番地として扱う形(厳しめ。町名の直後にあるときだけ使う)
_BANCHI = re.compile(
    rf"(?:{D}+番地(?:の?{D}+号?)?"  # 123番地、1番地1、7番地5、1番地の5
    rf"|{D}+番{D}+号"  # 9番1号
    rf"|{D}+(?:{_DASH}{D}+){{1,3}}号?"  # 1-2-3、2-8、1-2-3-405
    rf"|{D}+の{D}+の{D}+"  # 6の12の1(「の」区切りは、3つ続くときだけ)
    rf"|[0-9]{{2,}}(?=$|[\s、。,.!?！？にでがはをへだ]))"  # 427、888(算用数字の2桁以上だけ。直後が文の区切りか助詞)
    # 直後が数字・単位なら、番地ではない(2-3日、12-13万円 など)
    rf"(?![0-9〇一二三四五六七八九十百千日時分人円万月年位点%])"
)
# 番地のあとに続く、建物名と部屋番号・階(空白をはさんでもよい)
_BUILDING = re.compile(rf"[\s　]?[一-龥ァ-ヶーA-Za-z]{{1,20}}{D}+(?:号室|号|階|F)?")
# 町名と番地のあいだの「〇丁目」(町名の一部として残す)
_CHOME = re.compile(rf"{D}+丁目$")
# 町名の直前にあれば、住所らしさが強いとみなす文字(都道府県・市区町村・字)
_ADMIN_SUFFIX = "都道府県市区町村郡字"
# 確かな番地の形(〇番〇号、〇番地、数字が3つ以上続く、「の」区切りが3つ)
_STRONG = re.compile(rf"番地|番{D}+号|{D}+{_DASH}{D}+{_DASH}{D}+|の{D}+の")
_MAX_TOWN_CHARS = 24


def _kanji_number(text: str) -> int:
    """漢数字の並びを、整数にする(「二十三」→ 23、「一二三」→ 123)。"""
    if not any(ch in _KANJI_UNITS for ch in text):
        return int("".join(str(_KANJI_DIGITS[ch]) for ch in text))
    total, current = 0, 0
    for ch in text:
        if ch in _KANJI_DIGITS:
            current = current * 10 + _KANJI_DIGITS[ch]
        else:
            total += (current or 1) * _KANJI_UNITS[ch]
            current = 0
    return total + current


def _canonical(text: str) -> str:
    """照合用の形: NFKC にし、数字の並び(算用数字・漢数字)を、算用数字にそろえる。"""

    def number(match: re.Match[str]) -> str:
        run = match.group()
        if run.isascii():
            return str(int(run))
        if any(ch.isascii() for ch in run):
            return run  # 算用数字と漢数字が混ざったものは、そのまま
        return str(_kanji_number(run))

    return _NUMBER_RUN.sub(number, unicodedata.normalize("NFKC", text))


@functools.cache
def _towns() -> frozenset[str]:
    """町名の辞書(照合用の形)。ファイルがなければ、空(番地のマスクは働かない)。"""
    if not TOWNS_FILE.exists():
        return frozenset()
    with gzip.open(TOWNS_FILE, "rt", encoding="utf-8") as handle:
        return frozenset(_canonical(line.strip()) for line in handle if line.strip())


def _town_before(text: str, end: int, strong: bool) -> bool:
    """`end` の直前(「〇丁目」を除く)が、辞書にある町名で終わっているか。

    短い町名(2文字以下。「中央」「大手」「上」など)は、ふつうの言葉にも出るので、厳しく見る:
    1文字の町名は、直前に市区町村・字があるときだけ。2文字の町名は、直前に市区町村などがあるか、
    番地の形が確か(`strong`)なときだけ。
    """
    prefix = text[:end]
    chome = _CHOME.search(prefix)
    if chome:
        prefix = prefix[: chome.start()]
    towns = _towns()
    for length in range(min(_MAX_TOWN_CHARS, len(prefix)), 0, -1):  # 長い町名から
        if _canonical(prefix[-length:]) not in towns:
            continue
        admin = len(prefix) > length and prefix[-length - 1] in _ADMIN_SUFFIX
        if length >= 3 or admin or (length == 2 and strong):
            return True
    return False


def mask_banchi(text: str) -> tuple[str, int]:
    """番地まで書かれた住所の、番地以降を「[番地]」に置き換える。置き換えた件数も返す。"""
    if not _towns():
        return text, 0
    pieces: list[str] = []
    position = count = 0
    for match in _BANCHI.finditer(text):
        strong = bool(_STRONG.search(match.group()))
        if match.start() < position or not _town_before(text, match.start(), strong):
            continue
        end = match.end()
        building = _BUILDING.match(text, end)
        if building:
            end = building.end()
        pieces += [text[position : match.start()], BANCHI_LABEL]
        position = end
        count += 1
    pieces.append(text[position:])
    return "".join(pieces), count


def _digits(text: str) -> int:
    return sum(ch.isdigit() for ch in text)


def mask_contacts(text: str, banchi: bool = True) -> tuple[str, dict[str, int]]:
    """連絡先(と、`banchi` なら住所の番地)を置き換えた文面と、種類ごとの件数を返す。

    何も置き換えなければ、もとの文面と空の辞書。
    """
    normalized = unicodedata.normalize("NFKC", text)
    counts: dict[str, int] = {}
    result = normalized
    for name, pattern, label in MASKS:

        def replace(match: re.Match[str], name: str = name, label: str = label) -> str:
            if name == "phone" and not 10 <= _digits(match.group()) <= 12:
                return match.group()  # 桁数が合わないものは、電話番号とみなさない
            counts[name] = counts.get(name, 0) + 1
            return label

        result = pattern.sub(replace, result)
    if banchi:
        result, found = mask_banchi(result)
        if found:
            counts["address_banchi"] = found
    # 何も置き換えなかったら、もとの文面をそのまま返す(全角の文字を、勝手に半角にしない)
    return (result, counts) if counts else (text, {})
