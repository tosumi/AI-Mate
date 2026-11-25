"""ウィザード: 質問に答えるだけで、ペルソナ定義ができあがる(本体仕様書 §15.1 の M10-2)。

- **状態を持たない**。回答(質問 ID → 回答)を受け取り、次の質問か、組み立てた定義を返す。
- 基本は静的な質問で進め、回答を定義に組み立てたときに警告になる組み合わせ(矛盾・衝突)は、
  確認の質問・追加の質問を挟む(半動的)。
- 性格タイプは、診断の質問(付録D)を各軸4問に絞って使う。境界に近い軸だけ、予備の質問を足す。
- 人格の説明・話し方は、性格タイプの文体カードと回答から、決まった文の型で組み立てる(LLM は使わない)。
- 想定するキャラクタについて答えてもらう。答える人自身の性格は扱わない(付録D.6)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from mycompanion.persona import io as pio
from mycompanion.persona import quiz
from mycompanion.persona import types as ptypes
from mycompanion.persona.lint import LintReport, lint
from mycompanion.persona.resolve import default_traits
from mycompanion.state import catalog

WIZARD_VERSION = 1
MAIN_PER_AXIS = 4  # 性格タイプの質問は、各軸4問(診断の最小の問数)
DEFAULT_TYPE = "iaef"  # 性格タイプの質問に1問も答えずに組み立てたとき(CLI の `persona new` の既定と同じ)
DELTA_LIMIT = 0.4  # 差分の大きさの上限(W14 の境界を超えない)

Kind = Literal["scale", "choice", "likes", "dislikes"]


class WizardError(ValueError):
    """回答の不備(利用者に、そのまま伝えてよい)。"""


@dataclass(frozen=True)
class Option:
    value: str
    label: str


@dataclass(frozen=True)
class Question:
    id: str
    section: str
    kind: Kind
    text: str
    options: tuple[Option, ...] = ()
    default: Any = None
    note: str = ""  # 確認の質問で、なぜ聞くのかの説明

    def view(self, name: str) -> dict[str, Any]:
        return {
            "id": self.id, "section": self.section, "kind": self.kind, "text": self.text.replace("{name}", name),
            "options": [{"value": o.value, "label": o.label.replace("{name}", name)} for o in self.options],
            "default": self.default, "note": self.note,
        }  # fmt: skip


SECTIONS = (
    ("concept", "役どころ"),
    ("type", "性格"),
    ("expression", "口数と表情"),
    ("temperament", "気持ちの動き"),
    ("likes", "好き嫌い"),
    ("events", "出来事への反応"),
    ("speech", "話し方"),
)
SCALE_DEFAULT = 3


def _scale(id_: str, section: str, text: str) -> Question:
    return Question(id_, section, "scale", text, default=SCALE_DEFAULT)


def _choice(id_: str, section: str, text: str, options: list[tuple[str, str]], default: str, note: str = "") -> Question:
    return Question(id_, section, "choice", text, tuple(Option(v, label) for v, label in options), default, note)


# --- 役どころ ---
ROLE_LABELS = {
    "friend": "気の合う友だち",
    "listener": "話を聞いてくれる聞き役",
    "advisor": "相談に乗ってくれる相手",
    "cheerer": "元気をくれる存在",
}
ROLE = _choice("co_role", "concept", "{name}は、話し相手にとって、どんな存在ですか。", list(ROLE_LABELS.items()), "friend")

# --- 性格タイプ(診断の質問を各軸4問) ---
TYPE_MAIN = tuple(
    _scale(q.id, "type", "{name}は、" + q.text)
    for q in quiz.main_questions()[: MAIN_PER_AXIS * len(ptypes.AXES)]  # 軸を順に混ぜた並びの、先頭から各軸4問
)
# 境界に近い軸だけ、予備の質問を1問足す(それでも決まらなければ、近いほうの極を選んでもらう)
TYPE_TIEBREAK = {
    axis: tuple(_scale(q.id, "type", "{name}は、" + q.text) for q in quiz.tiebreak_questions(axis)[:1]) for axis in ptypes.AXES
}


def _pole_question(axis: str) -> Question:
    (low_letter, low_name), (high_letter, high_name) = ptypes.AXIS_POLES[axis]
    return _choice(
        f"pole_{axis}", "type", f"{{name}}は、「{low_name}」と「{high_name}」の、どちらに近いですか。",
        [(low_letter, low_name), (high_letter, high_name)], high_letter,
        note="回答が、ちょうど真ん中になりました。近いほうを選んでください。",
    )  # fmt: skip


POLE = {axis: _pole_question(axis) for axis in ptypes.AXES}

# --- 口数と表情(表出パラメータの差分) ---
# 回答 → 目標の値。3(どちらともいえない)は、性格タイプの既定値のまま
EXPRESSION_TARGETS = {1: 0.15, 2: 0.3, 4: 0.65, 5: 0.85}
SENSITIVITY_TARGETS = {1: 0.2, 2: 0.35, 4: 0.65, 5: 0.8}  # W20(0.1 以下・0.9 以上)にしない
EXPRESSION = (
    ("ex_talk", "talkativeness", "{name}は、よくしゃべるほうだ。"),
    ("ex_articulate", "articulation", "{name}は、思ったことを言葉にするのが上手だ。"),
    ("ex_express", "expressiveness", "{name}は、うれしい・悲しいが、言葉や態度にすぐ出る。"),
    ("ex_initiative", "initiative", "{name}は、自分から話題を振ったり、話しかけたりする。"),
    ("ex_sensitivity", "relationship_sensitivity", "{name}は、親しくなるほど、口数や表情が大きく変わる。"),
)
EXPRESSION_QUESTIONS = tuple(_scale(id_, "expression", text) for id_, _, text in EXPRESSION)
DISTANCE = {"stranger": 0.0, "friendly": 3.0, "close": 6.0}  # 恋慕帯(11 以上。W04)にはしない
DISTANCE_QUESTION = _choice(
    "ex_distance", "expression", "はじめて話す相手との距離感は。",
    [("stranger", "初対面らしく、少し距離がある"), ("friendly", "気さくで、すぐに打ち解ける"),
     ("close", "最初から、親しい友だちのよう")], "stranger",
)  # fmt: skip

# --- 気持ちの動き(気質) ---
REACTIVITY = {1: 0.2, 2: 0.35, 3: 0.5, 4: 0.7, 5: 0.85}
RECOVERY = {1: 0.15, 2: 0.22, 3: 0.3, 4: 0.45, 5: 0.6}
MOODS = {  # ふだんの気分 → baseline_vad(W09 の 0.7 を超えない)
    "neutral": ("とくに偏りはない", (0.0, 0.0, 0.0)),
    "bright": ("明るく元気", (0.4, 0.3, 0.2)),
    "calm": ("穏やかで落ち着いている", (0.2, -0.2, 0.1)),
    "quiet": ("静かで控えめ", (0.0, -0.3, 0.0)),
    "wistful": ("少し物思いにふけりがち", (-0.2, -0.1, -0.1)),
}
TEMPERAMENT_QUESTIONS = (
    _scale("tm_reactivity", "temperament", "{name}は、ちょっとしたことでも、気持ちが大きく動く。"),
    _scale("tm_recovery", "temperament", "{name}は、気持ちが動いても、すぐに元に戻る。"),
    _choice("tm_mood", "temperament", "{name}の、ふだんの気分に近いものは。",
            [(k, label) for k, (label, _) in MOODS.items()], "neutral"),
)  # fmt: skip

# --- 好き嫌い(好みカタログ。区分ごとに1つまで) ---
LIKED = Question("lk_liked", "likes", "likes", "{name}の好きなものを、区分ごとに1つまで選んでください(選ばない区分があってもかまいません)。", default={})
DISLIKED = Question("lk_disliked", "likes", "dislikes", "{name}の苦手なものを、区分ごとに1つまで選んでください(選ばない区分があってもかまいません)。", default={})
DISLIKE_REACTION = _choice(
    "lk_dislike_reaction", "likes", "苦手なものに出会ったとき、{name}は。",
    [("disgust", "顔をしかめて、嫌がる"), ("fear", "こわがったり、不安になったりする")], "disgust",
)  # fmt: skip

# --- 出来事への反応(イベントルール) ---
EVENT_QUESTIONS = (
    _choice("ev_thanks", "events", "感謝されたり、ほめられたりしたとき、{name}は。",
            [("glad", "素直に喜ぶ"), ("shy", "照れながら喜ぶ"), ("modest", "控えめに受け止める")], "glad"),
    _choice("ev_hostility", "events", "きつい言葉を向けられたとき、{name}は。",
            [("hurt", "傷ついて、悲しむ"), ("wary", "少し距離を置く"), ("calm", "静かに受け流す")], "wary"),
    _choice("ev_reunion", "events", "しばらくぶりに話しかけられたとき、{name}は。",
            [("big", "とても喜ぶ"), ("little", "少しうれしそうにする"), ("none", "とくに変わらない")], "little"),
)  # fmt: skip
ADAPT = _scale("ev_adapt", "events", "{name}は、相手から「もっと優しく話して」などと頼まれたら、合わせて変える。")
ADAPTABILITY = {1: 0.2, 2: 0.35, 3: 0.5, 4: 0.65, 5: 0.8}  # W25(0.1 以下)にしない

# --- 話し方 ---
FIRST_PERSON = {"watashi": "わたし", "watakushi": "わたくし", "boku": "ぼく", "ore": "おれ", "uchi": "うち", "name": "{name}"}
SPEECH_QUESTIONS = (
    _choice("sp_first_person", "speech", "{name}の一人称は。", [(k, v) for k, v in FIRST_PERSON.items()], "watashi"),
    _choice("sp_call", "speech", "{name}は、話し相手をどう呼びますか。",
            [("name_san", "名前に「さん」をつけて"), ("name", "名前で(呼び捨て)"), ("anata", "「あなた」"), ("kimi", "「きみ」")],
            "name_san"),
    _choice("sp_tone", "speech", "{name}の口調は。",
            [("polite", "です・ます調の丁寧な話し方"), ("soft", "やわらかい丁寧語"), ("casual", "くだけた口調(タメ口)"),
             ("blunt", "ぶっきらぼう")], "soft"),
    _choice("sp_ending", "speech", "{name}の話し方のくせは。",
            [("none", "とくにない"), ("ne", "「〜ね」「〜よ」をよく使う"), ("kana", "「〜かな」「〜かも」と控えめに言う"),
             ("energetic", "「〜!」と元気よく話す")], "none"),
)  # fmt: skip

# --- 確認の質問(組み立てたときに警告になる組み合わせ) ---
FOLLOW_UPS = {
    "W01": _choice(
        "fu_w01", "expression", "{name}は、内向的なのに、とてもよくしゃべる設定になっています。",
        [("keep", "このままにする(意図どおり)"), ("reduce", "口数を少し控えめにする")], "keep",
        note="性格タイプの回答と、口数の回答が食い違っています。",
    ),
    "W10": _choice(
        "fu_w10", "expression", "{name}は、感情は豊かなのに、ほとんど話さない設定になっています。",
        [("keep", "このままにする(意図どおり)"), ("express_less", "感情の出し方を控えめにする"),
         ("talk_more", "口数を少し増やす")], "keep",
        note="感情が表に出ても、言葉が少ないため、伝わりにくくなります。",
    ),
    "W07": _choice(
        "fu_w07", "temperament", "{name}は、気持ちが大きく動くのに、なかなか元に戻らない設定になっています。",
        [("keep", "このままにする"), ("recover", "気持ちが戻りやすくする")], "keep",
        note="一度動いた気持ちが、会話のあいだ、ずっと続きやすくなります。",
    ),
    "W08": Question(
        "fu_w08", "likes", "likes", "好きなものも苦手なものもないと、{name}は話題に反応しません。好きなものを選んでください(選ばずに進むこともできます)。",
        default={}, note="話題への反応は、好き嫌いから作ります。",
    ),
}  # fmt: skip
EXPRESSION_FOLLOW_UPS = ("W01", "W10")  # W21 は、回答の矛盾ではない(ほとんどの性格タイプで出る)ので聞かない

ALL_QUESTIONS: dict[str, Question] = {
    q.id: q
    for q in (
        ROLE, *TYPE_MAIN, *(q for qs in TYPE_TIEBREAK.values() for q in qs), *POLE.values(),
        *EXPRESSION_QUESTIONS, DISTANCE_QUESTION, *TEMPERAMENT_QUESTIONS, LIKED, DISLIKED, DISLIKE_REACTION,
        *EVENT_QUESTIONS, ADAPT, *SPEECH_QUESTIONS, *FOLLOW_UPS.values(),
    )
}  # fmt: skip


# ---------------------------------------------------------------------------
# 回答の検査
# ---------------------------------------------------------------------------


def _check_sheet(question: Question, value: Any) -> None:
    if not isinstance(value, dict):
        raise WizardError(f"回答の形が正しくありません: {question.id}")
    for category, item_id in value.items():
        if category not in catalog.CATEGORIES:
            raise WizardError(f"好みカタログの区分ではありません: {category}")
        if item_id is None:
            continue
        item = catalog.get(item_id) if isinstance(item_id, str) else None
        if item is None or item.category != category:
            raise WizardError(f"好みカタログにない項目です: {item_id}")
        if question.kind == "dislikes" and not item.dislikeable:
            raise WizardError(f"「{item.label}」は、苦手には選べません")
        if question.kind == "likes" and not item.likeable:
            raise WizardError(f"「{item.label}」は、好きには選べません")


def validate(answers: dict[str, Any]) -> None:
    _validate_each(answers)
    liked = {v for v in dict(answers.get(LIKED.id) or {}).values() if v}
    liked |= {v for v in dict(answers.get("fu_w08") or {}).values() if v}
    both = liked & {v for v in dict(answers.get(DISLIKED.id) or {}).values() if v}
    if both:
        item = catalog.get(min(both))
        raise WizardError(f"「{item.label if item else ''}」が、好きと苦手の両方にあります")


def _validate_each(answers: dict[str, Any]) -> None:
    for question_id, value in answers.items():
        question = ALL_QUESTIONS.get(question_id)
        if question is None:
            raise WizardError(f"ウィザードにない質問です: {question_id}")
        if question.kind == "scale":
            if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 5:
                raise WizardError(f"回答は 1〜5 で指定してください: {question_id}")
        elif question.kind == "choice":
            if value not in {o.value for o in question.options}:
                raise WizardError(f"選択肢にない回答です: {question_id}")
        else:
            _check_sheet(question, value)


# ---------------------------------------------------------------------------
# 組み立て
# ---------------------------------------------------------------------------


@dataclass
class Built:
    definition: dict[str, Any]
    type_answered: bool  # 性格タイプの質問に1問でも答えたか
    quiz_result: quiz.QuizResult | None = None
    notes: list[str] = field(default_factory=list)


def _get(answers: dict[str, Any], question: Question) -> Any:
    return answers.get(question.id, question.default)


def _type_answers(answers: dict[str, Any]) -> dict[str, int]:
    return {qid: v for qid, v in answers.items() if qid in quiz.QUESTIONS_BY_ID}


def _base_personality(answers: dict[str, Any]) -> tuple[dict[str, Any] | None, quiz.QuizResult | None]:
    """性格タイプ。答えていない本問は「どちらともいえない」、決まらない軸は、選んだ極(なければ既定のタイプの極)。"""
    given = _type_answers(answers)
    if not given:
        return None, None
    filled = {q.id: SCALE_DEFAULT for q in TYPE_MAIN} | given
    choice = {axis: answers.get(POLE[axis].id, DEFAULT_TYPE[ptypes.AXES.index(axis)]) for axis in ptypes.AXES}
    result = quiz.score(filled, choice)
    return quiz.base_personality(result, filled, {a: c for a, c in choice.items() if result.axes[a].borderline}, "scores"), result


def _delta(target_map: dict[int, float], answer: int, default: float) -> float:
    """回答の向きにだけ動かす(「やや当てはまる」で、既定値より下がらないように)。"""
    target = target_map.get(answer)
    if target is None:
        return 0.0
    delta = max(0.0, target - default) if answer > SCALE_DEFAULT else min(0.0, target - default)
    return round(max(-DELTA_LIMIT, min(DELTA_LIMIT, delta)), 2)


def _expression_deltas(answers: dict[str, Any], axes: dict[str, float]) -> dict[str, float]:
    defaults = default_traits(axes["i"], axes["e"]).as_dict()
    deltas: dict[str, float] = {}
    for (_, key, _), question in zip(EXPRESSION, EXPRESSION_QUESTIONS, strict=True):
        targets = SENSITIVITY_TARGETS if key == "relationship_sensitivity" else EXPRESSION_TARGETS
        value = _delta(targets, _get(answers, question), defaults[key])
        if value:
            deltas[key] = value

    def set_value(key: str, value: float) -> None:
        deltas[key] = round(max(-DELTA_LIMIT, min(DELTA_LIMIT, value - defaults[key])), 2)

    def current(key: str) -> float:
        return defaults[key] + deltas.get(key, 0.0)

    # 確認の質問で「変える」を選んだもの
    if answers.get("fu_w01") == "reduce" and current("talkativeness") > 0.7:
        set_value("talkativeness", 0.65)
    if answers.get("fu_w10") == "express_less" and current("expressiveness") > 0.7:
        set_value("expressiveness", 0.65)
    if answers.get("fu_w10") == "talk_more" and current("talkativeness") < 0.2:
        set_value("talkativeness", 0.3)
    return {k: v for k, v in deltas.items() if v}


def _sheet(answers: dict[str, Any]) -> dict[str, Any]:
    liked_answer = dict(_get(answers, LIKED)) | {k: v for k, v in (answers.get("fu_w08") or {}).items() if v}
    liked: dict[str, Any] = {c: None for c in catalog.CATEGORIES}
    for category, item_id in liked_answer.items():
        if item_id:
            liked[category] = {"id": item_id, "strength": "medium"}
            if category == "action":
                liked[category]["scope"] = "any"
    emotion = _get(answers, DISLIKE_REACTION)
    disliked: dict[str, Any] = {c: None for c in catalog.CATEGORIES}
    for category, item_id in dict(_get(answers, DISLIKED)).items():
        if not item_id:
            continue
        if category in ("color", "food", "event"):  # 嫌いなもの: 慣れる(敏感にならない)
            disliked[category] = {"id": item_id, "emotions": [emotion], "strength": "medium", "repeat": "habituate"}
        else:  # 嫌いなこと: 一定量で話題を変えようとする
            disliked[category] = {"id": item_id, "emotion": emotion, "strength": "medium", "avoid": True}
    return {"catalog_version": catalog.CATALOG_VERSION, "liked": liked, "disliked": disliked}


def _rule(id_: str, name: str, effects: dict[str, float], gain: float, **extra: Any) -> dict[str, Any]:
    kind = "state" if name == "session_gap" else "act"
    trigger: dict[str, Any] = {"kind": kind, "name": name}
    if name == "session_gap":
        trigger["min_days"] = 7
    return {"id": id_, "trigger": trigger, "effects": effects, "gain": gain, **extra}


def _event_rules(answers: dict[str, Any]) -> list[dict[str, Any]]:
    thanks, hostility, reunion = (_get(answers, q) for q in EVENT_QUESTIONS)
    # 自分のことを聞かれた・打ち明けてもらえたときは、質問の数を抑えるため、聞かずに、静かに受け止める反応にする
    interest = "quiet"
    rules = {
        "glad": [_rule("thanked", "gratitude", {"joy": 0.8, "trust": 0.4}, 0.5),
                 _rule("praised", "praise", {"joy": 0.7}, 0.5)],
        "shy": [_rule("thanked", "gratitude", {"joy": 0.5, "surprise": 0.3}, 0.4),
                _rule("praised", "praise", {"joy": 0.4, "surprise": 0.4}, 0.4)],
        "modest": [_rule("thanked", "gratitude", {"joy": 0.4, "trust": 0.3}, 0.2)],
    }[thanks]
    rules += {
        "hurt": [_rule("hurt_by_hostility", "hostility", {"sadness": 0.8, "trust": -0.3}, 0.5, cooldown_turns=1),
                 _rule("forgiven", "apology", {"sadness": -0.6, "trust": 0.4, "joy": 0.2}, 0.5)],
        "wary": [_rule("wary_after_hostility", "hostility", {"trust": -0.6}, 0.3, cooldown_turns=1),
                 _rule("forgiven", "apology", {"trust": 0.4}, 0.3)],
        "calm": [_rule("unmoved_by_hostility", "hostility", {"sadness": 0.3}, 0.2, cooldown_turns=1)],
    }[hostility]
    rules += {
        "big": [_rule("long_time_no_see", "session_gap", {"joy": 0.8, "surprise": 0.4}, 0.5)],
        "little": [_rule("long_time_no_see", "session_gap", {"joy": 0.5}, 0.3)],
        "none": [],
    }[reunion]
    rules += {
        "happy": [_rule("asked_about_self", "question_about_character", {"joy": 0.6, "anticipation": 0.5}, 0.4),
                  _rule("confided_in", "self_disclosure", {"trust": 0.8}, 0.4)],
        "quiet": [_rule("asked_about_self", "question_about_character", {"anticipation": 0.4}, 0.2),
                  _rule("confided_in", "self_disclosure", {"trust": 0.5}, 0.3)],
        "none": [],
    }[interest]
    return rules  # fmt: skip


# 人格の説明の文の型(回答が 1・2 → 低い側、4・5 → 高い側。3 は書かない)
TRAIT_SENTENCES = {
    "ex_talk": ("口数は少なめで、短く話す。", "おしゃべりで、話がよく弾む。"),
    "ex_articulate": ("思いをうまく言葉にできず、言いよどむことがある。", "考えを分かりやすく言葉にするのが得意。"),
    "ex_express": ("感情をあまり表に出さない。", "うれしさや悲しさが、言葉や態度に素直に出る。"),
    "ex_initiative": ("自分から話題を振ることは少なく、相手の話を待つ。", "自分から話題を振ったり、問いかけたりする。"),
    "ex_sensitivity": ("相手との距離に関わらず、態度はあまり変わらない。", "親しくなるにつれて、口数も表情も豊かになっていく。"),
    "tm_reactivity": ("ちょっとしたことでは動じない。", "気持ちが動きやすい。"),
    "tm_recovery": ("気持ちを引きずりやすい。", "気持ちの切り替えが早い。"),
}
ROLE_SENTENCES = {
    "friend": "{name}は、相手と気軽に話せる、気の合う友だちのような存在。",
    "listener": "{name}は、相手の話をじっくり聞く聞き役。自分の話より、相手の話を大切にする。",
    "advisor": "{name}は、相手の相談に乗る存在。気持ちを受け止めたうえで、一緒に考える。",
    "cheerer": "{name}は、相手に元気を届ける存在。明るい話題や励ましで、場を温める。",
}
MOOD_SENTENCES = {
    "neutral": "",
    "bright": "ふだんは明るく元気。",
    "calm": "ふだんは穏やかで落ち着いている。",
    "quiet": "ふだんは静かで控えめ。",
    "wistful": "ふだんは、少し物思いにふけりがち。",
}
CALL_SENTENCES = {
    "name_san": "相手を、名前に「さん」をつけて呼ぶ(名前がわからないときは「あなた」)。",
    "name": "相手を名前で呼ぶ(名前がわからないときは「あなた」)。",
    "anata": "相手を「あなた」と呼ぶ。",
    "kimi": "相手を「きみ」と呼ぶ。",
}
TONE_SENTENCES = {
    "polite": "です・ます調の、丁寧な話し方。",
    "soft": "やわらかい丁寧語で、親しみをこめて話す。",
    "casual": "くだけた口調(タメ口)で話す。",
    "blunt": "ぶっきらぼうで、そっけない話し方。ただし、相手を傷つける言い方はしない。",
}
ENDING_SENTENCES = {
    "none": "",
    "ne": "語尾に「〜ね」「〜よ」をよく使う。",
    "kana": "「〜かな」「〜かも」と、控えめに言うことが多い。",
    "energetic": "「〜!」と、元気よく話す。",
}


def _persona_prompt(answers: dict[str, Any], name: str, type_id: str) -> str:
    # 文体カードは、決まった性格タイプの手書きのもの(軸の値から合成すると、真ん中寄りの軸では文が出ない)
    parts = [ROLE_SENTENCES[_get(answers, ROLE)].format(name=name), ptypes.get_type(type_id).style_card]
    for question in (*EXPRESSION_QUESTIONS, *TEMPERAMENT_QUESTIONS[:2]):
        answer = _get(answers, question)
        if answer != SCALE_DEFAULT:
            parts.append(TRAIT_SENTENCES[question.id][0 if answer < SCALE_DEFAULT else 1])
    parts.append(MOOD_SENTENCES[_get(answers, TEMPERAMENT_QUESTIONS[2])])
    return "".join(p for p in parts if p)


def _speech_style(answers: dict[str, Any], name: str) -> str:
    first_person, call, tone, ending = (_get(answers, q) for q in SPEECH_QUESTIONS)
    first = f"自分のことを「{name}」と呼ぶ。" if first_person == "name" else f"一人称は「{FIRST_PERSON[first_person]}」。"
    return first + CALL_SENTENCES[call] + TONE_SENTENCES[tone] + ENDING_SENTENCES[ending]


def build(character_id: str, name: str, answers: dict[str, Any]) -> Built:
    """回答から定義を組み立てる。答えていない質問は、既定の回答として扱う。"""
    validate(answers)
    base, result = _base_personality(answers)
    type_id = base["type"] if base else DEFAULT_TYPE
    try:
        definition = pio.new_definition(character_id, name, type_id, None)
    except ValueError as exc:
        raise WizardError(str(exc)) from None
    if base:
        definition["base_personality"] = base
    axes = definition["base_personality"]["axes"]
    role = _get(answers, ROLE)
    definition["concept"].update({"one_line": ROLE_LABELS[role], "role": role, "core_trait": ptypes.get_type(type_id).name})
    definition["persona_prompt"] = _persona_prompt(answers, name, type_id)
    definition["speech_style"] = _speech_style(answers, name)
    definition["profile"] = {"preset": None, "deltas": _expression_deltas(answers, axes)}
    definition["initial_relationship"] = DISTANCE[_get(answers, DISTANCE_QUESTION)]
    definition["adaptation"]["adaptability"] = ADAPTABILITY[_get(answers, ADAPT)]
    reactivity = REACTIVITY[_get(answers, TEMPERAMENT_QUESTIONS[0])]
    recovery = RECOVERY[_get(answers, TEMPERAMENT_QUESTIONS[1])]
    if answers.get("fu_w07") == "recover" and recovery < 0.2:
        recovery = 0.3
    v, a, d = MOODS[_get(answers, TEMPERAMENT_QUESTIONS[2])][1]
    definition["temperament"] = {"baseline_vad": {"v": v, "a": a, "d": d}, "reactivity": reactivity, "recovery_rate": recovery}
    definition["likes_dislikes"] = _sheet(answers)
    definition["event_rules"] = _event_rules(answers)
    return Built(definition, type_answered=base is not None, quiz_result=result)


# ---------------------------------------------------------------------------
# 次の質問(半動的)
# ---------------------------------------------------------------------------


def _answered(answers: dict[str, Any], questions: tuple[Question, ...] | list[Question]) -> bool:
    return all(q.id in answers for q in questions)


def _codes(report: LintReport) -> set[str]:
    return {f.code for f in report.findings}


def flow(character_id: str, name: str, answers: dict[str, Any]) -> list[Question]:
    """いまの回答で、聞く質問の並び(答えたものを含む)。前の区切りの回答で、確認の質問・追加の質問が入る。"""
    validate(answers)
    questions: list[Question] = [ROLE, *TYPE_MAIN]
    if _answered(answers, TYPE_MAIN):
        filled = _type_answers(answers)
        main_only = {k: v for k, v in filled.items() if not quiz.QUESTIONS_BY_ID[k].tiebreak}
        first = quiz.score(main_only)
        for axis in ptypes.AXES:
            tiebreak = TYPE_TIEBREAK[axis]
            if first.axes[axis].borderline or any(q.id in answers for q in tiebreak):
                questions += tiebreak
                if _answered(answers, tiebreak) and (quiz.score(filled).axes[axis].pole is None or POLE[axis].id in answers):
                    questions.append(POLE[axis])
    questions += [*EXPRESSION_QUESTIONS, DISTANCE_QUESTION]
    sections_done = _answered(answers, questions)
    report = lint(build(character_id, name, answers).definition) if sections_done else None
    if report is not None:
        questions += [FOLLOW_UPS[c] for c in EXPRESSION_FOLLOW_UPS if c in _codes(report) or FOLLOW_UPS[c].id in answers]
    questions += TEMPERAMENT_QUESTIONS
    if report is not None and _answered(answers, questions):
        report = lint(build(character_id, name, answers).definition)
        if "W07" in _codes(report) or FOLLOW_UPS["W07"].id in answers:
            questions.append(FOLLOW_UPS["W07"])
    questions += [LIKED, DISLIKED]
    if any(dict(answers.get(DISLIKED.id) or {}).values()) or DISLIKE_REACTION.id in answers:
        questions.append(DISLIKE_REACTION)
    if _answered(answers, (LIKED, DISLIKED)):
        empty = not any(dict(answers[LIKED.id]).values()) and not any(dict(answers[DISLIKED.id]).values())
        if empty or FOLLOW_UPS["W08"].id in answers:
            questions.append(FOLLOW_UPS["W08"])
    questions += [*EVENT_QUESTIONS, ADAPT, *SPEECH_QUESTIONS]
    return questions


# 確認の質問で「このまま」を選んだ(または、追加の質問をした)ので、確認済みとして扱う警告
def acknowledged(answers: dict[str, Any]) -> list[str]:
    codes = [code for code, q in FOLLOW_UPS.items() if q.id in answers]
    if FOLLOW_UPS["W08"].id in answers:
        codes.append("W23")  # 好き嫌いがないと、嗜好もない(W08 の確認で、選ばずに進んだ)
    if any(q.id in answers for qs in TYPE_TIEBREAK.values() for q in qs):
        codes.append("W17")  # 境界に近い軸は、予備の質問(と、極の選択)で確かめた
    return codes


def step(character_id: str, name: str, answers: dict[str, Any], finish: bool = False) -> dict[str, Any]:
    """次の質問を返す。すべて答えたか、`finish`(残りを既定の回答にする)なら、組み立てた定義を返す。"""
    questions = flow(character_id, name, answers)
    remaining = [q for q in questions if q.id not in answers]
    section_labels = dict(SECTIONS)
    out: dict[str, Any] = {
        "version": WIZARD_VERSION,
        "sections": [{"id": s, "label": label} for s, label in SECTIONS],
        "progress": {"answered": len(questions) - len(remaining), "total": len(questions)},
        "question": None,
        "done": False,
    }
    if remaining and not finish:
        question = remaining[0]
        out["question"] = question.view(name) | {"section_label": section_labels[question.section]}
        return out
    built = build(character_id, name, answers)
    out["done"] = True
    out["definition"] = built.definition
    out["acknowledged"] = acknowledged(answers)
    out["defaulted"] = len(remaining)  # 既定の回答にした質問の数
    return out
