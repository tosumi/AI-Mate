"""質問による診断(ペルソナデザイナ仕様書 §7.2.3、付録D)。

設計者が、**想定するキャラクタについて** 5段階で答え、性格タイプの候補を決める。
設計者本人や、会話の相手の性格を測る用途には使わない(付録D.6)。
結果は「回答から見た傾向」であり、診断ではない。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mycompanion.persona.types import AXES, AXIS_POLES

BANK_VERSION = 1
SCALE_LABELS = (
    "まったく当てはまらない",
    "あまり当てはまらない",
    "どちらともいえない",
    "やや当てはまる",
    "とても当てはまる",
)
PREAMBLE = "あなたが想定しているキャラクタについて答えてください。"
DISCLAIMER = "回答から見た傾向であり、診断ではありません。"

MIN_ANSWERS_PER_AXIS = 4  # これ未満の軸は、結果を出さない
BORDERLINE = 0.08  # |s − 0.5| がこれ未満なら境界
SCORE_CLIP = (0.1, 0.9)  # 採用方法 scores のときの軸の値の範囲


@dataclass(frozen=True)
class Question:
    id: str
    axis: str
    reverse: bool  # True: 回答が高いほど「0に近い極」(外向/具体/論理/計画)
    tiebreak: bool  # True: 予備(境界のときだけ使う)
    text: str


def _q(id_: str, reverse: bool, text: str) -> Question:
    return Question(id=id_, axis=id_[0], reverse=reverse, tiebreak=id_[1] == "t", text=text)


# 質問バンク(付録D.1〜D.4)。質問文は「このキャラクタは…」に続く
QUESTIONS: tuple[Question, ...] = (
    _q("i1", False, "大人数の場より、少人数や一人の時間で元気を取り戻す。"),
    _q("i2", True, "初対面の人にも、自分から気軽に話しかける。"),
    _q("i3", False, "考えを口に出す前に、頭の中でよく整理する。"),
    _q("i4", True, "人と話すことで、考えがまとまったり、元気が出たりする。"),
    _q("i5", False, "自分のことは、親しくならないとあまり話さない。"),
    _q("i6", True, "沈黙が続くと、場をつなごうとして自分から話し始める。"),
    _q("it1", False, "一日中人と過ごすと、疲れてひとりになりたくなる。"),
    _q("it2", True, "集まりでは、自然と輪の中心近くにいる。"),
    _q("a1", False, "目の前の事実や経験より、そこから連想する意味や可能性に関心が向く。"),
    _q("a2", True, "具体的な例や数字がないと、話をつかみにくい。"),
    _q("a3", False, "新しいアイデアや「もしも」の話が好きだ。"),
    _q("a4", True, "実際に確かめられる、現実的な話を好む。"),
    _q("a5", False, "話が、たとえ話や抽象的な表現に飛びやすい。"),
    _q("a6", True, "手順や前例どおりに進めるほうが安心する。"),
    _q("at1", False, "「なぜそうなるのか」という背景や仕組みを考えるのが好きだ。"),
    _q("at2", True, "説明は、まず具体例から入るほうが好きだ。"),
    _q("e1", False, "判断するとき、筋が通っているかより、人の気持ちや調和を重くみる。"),
    _q("e2", True, "相談されると、まず解決策や改善点を考える。"),
    _q("e3", False, "相手が落ち込んでいると、自分も心が動いてしまう。"),
    _q("e4", True, "厳しいことでも、正しいと思えば率直に伝える。"),
    _q("e5", False, "議論で相手を言い負かすことより、気まずくならないことを優先する。"),
    _q("e6", True, "感情的になっている相手に、冷静な分析を返しがちだ。"),
    _q("et1", False, "人を評価するとき、結果の正しさより、気持ちへの配慮を重視する。"),
    _q("et2", True, "誰かが失敗したとき、まず原因を分析してしまう。"),
    _q("f1", False, "予定を決めすぎず、その場の流れで動くのが好きだ。"),
    _q("f2", True, "物事は早めに決めて、段取りよく進めたい。"),
    _q("f3", False, "選択肢を最後まで開けておきたい。"),
    _q("f4", True, "約束や締め切りは、きちんと守らないと落ち着かない。"),
    _q("f5", False, "話があちこちに飛んでも、気にならない。"),
    _q("f6", True, "会話でも、結論や落としどころを意識する。"),
    _q("ft1", False, "状況が変わったら、計画を変えるのは苦にならない。"),
    _q("ft2", True, "旅行は、行き先と日程を事前にしっかり決めておきたい。"),
)
QUESTIONS_BY_ID = {q.id: q for q in QUESTIONS}


def main_questions() -> list[Question]:
    """本問(24問)。軸が偏らないよう、軸を順に混ぜて並べる。"""
    per_axis = {axis: [q for q in QUESTIONS if q.axis == axis and not q.tiebreak] for axis in AXES}
    return [per_axis[axis][n] for n in range(6) for axis in AXES]


def tiebreak_questions(axis: str) -> list[Question]:
    return [q for q in QUESTIONS if q.axis == axis and q.tiebreak]


@dataclass
class AxisResult:
    axis: str
    answered: int
    score: float | None = None  # 0〜1。1に近い極(内向/抽象/共感/柔軟)ほど大きい
    strength: float | None = None  # 0〜1
    pole: str | None = None  # 極の文字。境界で決まらない場合と、回答不足の場合は None
    borderline: bool = False
    used_tiebreak: bool = False


@dataclass
class QuizResult:
    axes: dict[str, AxisResult]
    type_id: str | None  # 4軸すべての極が決まったときだけ
    warnings: list[str] = field(default_factory=list)

    @property
    def undecided_axes(self) -> list[str]:
        return [axis for axis, result in self.axes.items() if result.pole is None]


def _normalized(question: Question, answer: int) -> float:
    x = (answer - 1) / 4
    return 1 - x if question.reverse else x


def validate_answers(answers: dict[str, int]) -> list[str]:
    """回答の不備(質問IDが質問バンクにない、回答が 1〜5 でない)を返す。"""
    problems = []
    for question_id, answer in answers.items():
        if question_id not in QUESTIONS_BY_ID:
            problems.append(f"質問バンクにない質問IDです: {question_id}")
        elif not isinstance(answer, int) or isinstance(answer, bool) or not 1 <= answer <= 5:
            problems.append(f"回答は 1〜5 の整数で指定してください: {question_id}")
    return problems


def score(answers: dict[str, int], tiebreak_choice: dict[str, str] | None = None) -> QuizResult:
    """採点して、軸ごとの極とタイプIDを判定する。

    - 本問だけで採点し、境界の軸は、予備の回答があれば 8問で再採点する。
    - それでも境界なら、自動では決めない。`tiebreak_choice`(軸 → 極の文字)があれば、それを使う。
    """
    problems = validate_answers(answers)
    if problems:
        raise ValueError("; ".join(problems))
    tiebreak_choice = tiebreak_choice or {}

    results: dict[str, AxisResult] = {}
    for axis in AXES:
        main = [(q, answers[q.id]) for q in QUESTIONS if q.axis == axis and not q.tiebreak and q.id in answers]
        extra = [(q, answers[q.id]) for q in tiebreak_questions(axis) if q.id in answers]
        result = AxisResult(axis=axis, answered=len(main))
        results[axis] = result
        if len(main) < MIN_ANSWERS_PER_AXIS:
            continue

        values = [_normalized(q, a) for q, a in main]
        s = sum(values) / len(values)
        if abs(s - 0.5) < BORDERLINE and extra:
            values += [_normalized(q, a) for q, a in extra]
            s = sum(values) / len(values)
            result.used_tiebreak = True
            result.answered = len(values)
        result.score = round(s, 4)
        result.strength = round(abs(s - 0.5) * 2, 4)
        low, high = AXIS_POLES[axis]
        if abs(s - 0.5) < BORDERLINE:
            result.borderline = True
            choice = tiebreak_choice.get(axis)
            result.pole = choice if choice in (low[0], high[0]) else None
        else:
            result.pole = high[0] if s > 0.5 else low[0]

    decided = all(results[axis].pole for axis in AXES)
    warnings = []
    if answers and len(set(answers.values())) == 1:
        warnings.append("すべての質問に同じ値で答えています。回答を見直してください。")
    return QuizResult(
        axes=results,
        type_id="".join(results[axis].pole for axis in AXES) if decided else None,  # type: ignore[misc]
        warnings=warnings,
    )


def axes_from_scores(result: QuizResult) -> dict[str, float]:
    """採用方法 scores: 回答のスコアを、そのまま軸の値にする(0.1〜0.9 にクリップ)。

    設計者が極を選んだ軸は、選んだ極の側に必ず入るようにする。
    """
    low_clip, high_clip = SCORE_CLIP
    axes = {}
    for axis, r in result.axes.items():
        if r.score is None or r.pole is None:
            raise ValueError(f"軸 {axis} の結果が決まっていません")
        value = max(low_clip, min(high_clip, r.score))
        is_high = r.pole == AXIS_POLES[axis][1][0]
        if is_high and value <= 0.5:
            value = 0.51
        elif not is_high and value >= 0.5:
            value = 0.49
        axes[axis] = round(value, 2)
    return axes


def answers_for_type(type_id: str, include_tiebreak: bool = False) -> dict[str, int]:
    """そのタイプの極を示す回答(5 または 1)を機械的に作る(往復テスト用。§7.2.5)。"""
    answers = {}
    for question in QUESTIONS:
        if question.tiebreak and not include_tiebreak:
            continue
        letter = type_id[AXES.index(question.axis)]
        is_high = letter == AXIS_POLES[question.axis][1][0]
        answers[question.id] = 5 if is_high != question.reverse else 1
    return answers
