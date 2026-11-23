"""出来事の時期の正規化(本体仕様書 §9.13。`topic-timeline-memory.md` §7)。

「昨日」「先週」などの相対表現を、**話した日**(ユーザーのローカルの日付)を基準に、日付の範囲と精度に直す。
よく出る表現は、ここ(コード)で決定的に変換する。LLM の解釈と食い違ったときは、コードの値を使う。

- 精度は、ユーザーが話した精度のままにする(「先週」を、特定の日に細かくしない)。
- 変換できない表現(「この前」「少し前」)は、None を返す(呼び出し側が、LLM の解釈を検査して使うか、不明にする)。
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta

PRECISIONS = ("day", "week", "month", "season", "year", "unknown")
SEASONS = {"春": (3, 5), "夏": (6, 8), "秋": (9, 11), "冬": (12, 2)}
_WEEKDAYS = "月火水木金土日"
# 全角の数字(０〜９)。ソースに直接書かず、文字コードから作る
_FULLWIDTH_DIGITS = "".join(chr(0xFF10 + n) for n in range(10))
_FULLWIDTH = str.maketrans(_FULLWIDTH_DIGITS, "0123456789")
_KANJI_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


@dataclass(frozen=True)
class Occurred:
    """出来事の時期。`start`〜`end`(両端を含む)と、精度。"""

    start: date | None
    end: date | None
    precision: str = "unknown"

    def label(self) -> str:
        """タイムラインの「時期」の列に書く形。"""
        if self.start is None or self.precision == "unknown":
            return "不明"
        if self.precision == "day":
            return self.start.isoformat()
        if self.precision == "week":
            return f"{self.start.isoformat()} の週"
        if self.precision == "month":
            return f"{self.start.year:04d}-{self.start.month:02d}"
        if self.precision == "season":
            name = next((n for n, (first, _) in SEASONS.items() if first == self.start.month), "")
            return f"{self.start.year:04d} {name}".strip()
        return f"{self.start.year:04d}"


UNKNOWN = Occurred(None, None, "unknown")


def _day(value: date) -> Occurred:
    return Occurred(value, value, "day")


def _week(day_in_week: date) -> Occurred:
    """その日を含む週(月曜〜日曜)。"""
    monday = day_in_week - timedelta(days=day_in_week.weekday())
    return Occurred(monday, monday + timedelta(days=6), "week")


def _month(year: int, month: int) -> Occurred:
    year += (month - 1) // 12
    month = (month - 1) % 12 + 1
    return Occurred(date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1]), "month")


def _year(year: int) -> Occurred:
    return Occurred(date(year, 1, 1), date(year, 12, 31), "year")


def _season(year: int, name: str) -> Occurred:
    first, last = SEASONS[name]
    end_year = year + 1 if last < first else year
    return Occurred(date(year, first, 1), date(end_year, last, calendar.monthrange(end_year, last)[1]), "season")


def _number(text: str) -> int | None:
    if text.isdigit():
        return int(text)
    text = text.translate(_FULLWIDTH)
    if text.isdigit():
        return int(text)
    if text in _KANJI_DIGITS:
        return _KANJI_DIGITS[text]
    if len(text) == 2 and text[0] == "十" and text[1] in _KANJI_DIGITS:
        return 10 + _KANJI_DIGITS[text[1]]
    return None


_N = "([0-9" + _FULLWIDTH_DIGITS + "]+|[一二三四五六七八九十]{1,2})"


def normalize(expression: str | None, told_on: date) -> Occurred | None:
    """時期の表現を、話した日を基準に正規化する。決められなければ None。"""
    text = (expression or "").strip()
    if not text:
        return None

    # --- 絶対の日付 ---
    if match := re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})", text):
        try:
            return _day(date(int(match[1]), int(match[2]), int(match[3])))
        except ValueError:
            return None
    if match := re.search(r"(\d{4})年(\d{1,2})月", text):
        return _month(int(match[1]), int(match[2])) if 1 <= int(match[2]) <= 12 else None
    if match := re.search(_N + r"月" + _N + r"日", text):
        month, day = _number(match[1]), _number(match[2])
        try:
            return _day(date(told_on.year, month or 0, day or 0))
        except ValueError:
            return None

    # --- 曜日(「先週の金曜日」「来週の月曜」「金曜日」) ---
    if match := re.search(rf"(先週|今週|来週)?の?([{_WEEKDAYS}])曜", text):
        offset = {"先週": -7, "今週": 0, "来週": 7, None: 0}[match[1]]
        monday = told_on - timedelta(days=told_on.weekday()) + timedelta(days=offset)
        return _day(monday + timedelta(days=_WEEKDAYS.index(match[2])))

    # --- 相対の日・週・月・年 ---
    fixed_days = (
        (r"おととい|一昨日", -2), (r"昨日|きのう|昨夜|昨晩|ゆうべ", -1), (r"今日|きょう|本日|今朝|今夜|さっき", 0),
        (r"あさって|明後日", 2), (r"明日|あした|あす", 1),
    )  # fmt: skip
    for pattern, days in fixed_days:
        if re.search(pattern, text):
            return _day(told_on + timedelta(days=days))
    if match := re.search(_N + r"日(前|後)", text):
        days = _number(match[1]) or 0
        return _day(told_on + timedelta(days=days if match[2] == "後" else -days))
    if match := re.search(_N + r"週間?(前|後)", text):
        weeks = _number(match[1]) or 0
        return _week(told_on + timedelta(weeks=weeks if match[2] == "後" else -weeks))
    if match := re.search(_N + r"[かヶケ]月(前|後)", text):
        months = _number(match[1]) or 0
        return _month(told_on.year, told_on.month + (months if match[2] == "後" else -months))
    for pattern, weeks in ((r"先週", -1), (r"今週", 0), (r"来週", 1)):
        if re.search(pattern, text):
            return _week(told_on + timedelta(weeks=weeks))
    for pattern, months in ((r"先月", -1), (r"今月", 0), (r"来月", 1)):
        if re.search(pattern, text):
            return _month(told_on.year, told_on.month + months)
    year_words = ((r"おととし|一昨年", -2), (r"去年|昨年", -1), (r"今年", 0), (r"再来年", 2), (r"来年", 1))
    for pattern, years in year_words:
        if re.search(pattern, text):
            year = told_on.year + years
            # 「去年の秋」「今年の10月」のように、季節や月が続くとき
            if season := re.search(r"([春夏秋冬])", text):
                return _season(year, season[1])
            if month := re.search(_N + r"月", text):
                number = _number(month[1])
                return _month(year, number) if number and 1 <= number <= 12 else _year(year)
            return _year(year)
    if match := re.fullmatch(_N + r"月(?:ごろ|頃|上旬|中旬|下旬)?", text):
        number = _number(match[1])
        return _month(told_on.year, number) if number and 1 <= number <= 12 else None
    return None


def check_range(start: date | None, end: date | None, precision: str, told_on: date, future: bool) -> Occurred:
    """LLM が出した時期の解釈を検査する。おかしければ、不明にする(推測で細かくしない)。

    `future` は、予定(これから起きること)か。過去の出来事なのに未来の日付、範囲の前後が逆、
    100年以上前、などは採らない。
    """
    if start is None or precision not in PRECISIONS or precision == "unknown":
        return UNKNOWN
    end = end or start
    if end < start or abs((told_on - start).days) > 366 * 100:
        return UNKNOWN
    if not future and start > told_on:
        return UNKNOWN
    return Occurred(start, end, precision)
