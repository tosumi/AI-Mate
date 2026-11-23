"""好き嫌いシート(好みカタログ、解決、条件つきの反応、回避)のテスト。

本体仕様書 §7.1 の③、デザイナ仕様書 §5.4.0。`likes-dislikes-sheet-review` rev 1〜3。
"""

import copy
from pathlib import Path

import pytest

from mycompanion.config import EngineSettings
from mycompanion.emotion.models import EmotionEstimate, RuleMatch, Vad
from mycompanion.engine import planner
from mycompanion.persona import io as pio
from mycompanion.persona import sheet
from mycompanion.persona import simulate as sim
from mycompanion.persona.lint import lint
from mycompanion.persona.resolve import effective_traits, resolve
from mycompanion.persona.schema import LikesDislikes
from mycompanion.state import appraisal as ap
from mycompanion.state import catalog
from mycompanion.state.emotion_state import EmotionVector, fire_rules
from mycompanion.state.events import ACT_NAMES

SETTINGS = EngineSettings()
SAMPLES = ["sample_kuchibeta", "sample_kamoku", "sample_hitonatsukoi", "sample_oshaberi"]


def _raw(character_id: str = "sample_kuchibeta") -> dict:
    return pio.load_raw(pio.persona_path(character_id, Path("personas")))


def _rules(character_id: str = "sample_kuchibeta") -> list[dict]:
    return pio.to_character_row(lint(_raw(character_id)).definition)["appraisal_rules_json"]


def _index(rules: list[dict], rule_id: str) -> int:
    return next(i for i, rule in enumerate(rules) if rule.get("id") == rule_id)


def _eval(rules, matches, state=None, turn_no=1, **flags) -> ap.SheetOutcome:
    context = ap.SheetContext(matches=matches, turn_no=turn_no, **flags)
    return ap.evaluate(rules, context, state, 0.8, SETTINGS)


# --- 好みカタログ ---


def test_catalog_has_16_choices_per_category_and_side():
    assert len(catalog.CATALOG) == 97
    for category in catalog.CATEGORIES:
        entries = catalog.items(category)
        assert sum(e.likeable for e in entries) == 16, category
        assert sum(e.dislikeable for e in entries) == 16, category
    assert not catalog.CATALOG["event.birthday_party"].dislikeable  # ユーザーの祝い事に、嫌悪で反応しない
    assert not catalog.CATALOG["event.anniversary"].likeable
    assert "結婚記念日" in catalog.CATALOG["event.anniversary"].exclude


def test_catalog_does_not_overlap_speech_acts_or_obligations():
    labels = " ".join(e.label + " ".join(e.include) for e in catalog.CATALOG.values())
    # 発話行為(イベントルールのトリガー)と重ねない。義務的な活動・恋愛的な接触・年齢制限のあるものを入れない
    for word in ("感謝", "謝", "ほめ", "怒られ", "仕事", "勉強", "通勤", "介護", "キス", "抱きし", "デート", "酒", "たばこ"):
        assert word not in labels, word
    assert not set(ACT_NAMES) & {e.id.split(".")[1] for e in catalog.CATALOG.values()}
    # 身体の接触は「なでる」だけ。関わり方の判定を使う項目は、動作・状況の5つ
    assert {e.id for e in catalog.CATALOG.values() if e.involvement} == {
        "action.pat", "action.surprise", "action.waiting", "action.rushed", "action.chatting",
    }  # fmt: skip


# --- 解決 ---


def test_sheet_resolves_to_rules_and_preferences():
    rules = [r for r in _rules() if ap.is_sheet_rule(r)]
    by_id = {r["id"]: r for r in rules}
    assert len(rules) == 9  # ミナト: 好き6 + 嫌い3
    apple = by_id["sheet_like_food"]
    assert (apple["catalog_id"], apple["gain"], apple["repeat"]) == ("food.apple", 0.5, "decay")
    assert apple["effects"] == {"joy": 1.0, "anticipation": 0.5, "trust": 0.2}
    assert by_id["sheet_like_action"]["effects_with_character"]["trust"] == 0.6
    assert by_id["sheet_dislike_event"]["effects"] == {"fear": 0.8, "surprise": 0.8}
    speaking = by_id["sheet_dislike_action"]
    assert speaking["effects"] == {"fear": 1.0} and speaking["gain"] == 0.7 and speaking["avoid"] == {"emotion": "fear"}
    assert "含む" in apple["note"] and "Apple(会社)" in apple["note"]


def test_sheet_rules_are_not_fired_by_the_manual_rule_path():
    rules = _rules("sample_hitonatsukoi")
    matches = dict.fromkeys(range(len(rules)), 0.9)
    assert {f.topic for f in fire_rules(rules, matches, 0.7)} == {"友達・お出かけ・パーティー", "ひとりぼっち・孤立"}


def test_unknown_or_one_sided_items_are_not_resolved():
    data = LikesDislikes.model_validate(
        {"liked": {"event": {"id": "event.anniversary"}, "food": {"id": "food.unknown"}},
         "disliked": {"event": {"id": "event.birthday_party"}}}
    )  # fmt: skip
    assert sheet.resolve_rules(data) == []


# --- 好き: ポジティブな文脈のときだけ ---


def test_like_fires_only_in_positive_context():
    rules = _rules()
    apple = _index(rules, "sheet_like_food")
    positive = _eval(rules, {apple: RuleMatch(relevance=0.9, polarity=0.8)})
    assert positive.categories == {"joy": 0.25, "anticipation": 0.18, "trust": 0.072}  # 1ターンの上限 0.25
    assert positive.cause == "好きなもの(りんご)の話が出た" and not positive.negative_fired
    for polarity in (0.1, -0.8):
        stopped = _eval(rules, {apple: RuleMatch(relevance=0.9, polarity=polarity)})
        assert stopped.categories == {} and stopped.hits[0]["suppressed_by"] == "polarity"
    assert _eval(rules, {apple: RuleMatch(relevance=0.4, polarity=0.9)}).hits == []  # 関連度が足りない


def test_like_is_stopped_by_risk_idiom_and_person_attribute():
    rules = _rules()
    pat, blue = _index(rules, "sheet_like_action"), _index(rules, "sheet_like_color")
    risky = _eval(rules, {pat: RuleMatch(relevance=0.8, polarity=0.6)}, risk_sexual=0.7)
    assert risky.categories == {} and risky.hits[0]["suppressed_by"] == "risk"
    idiom = _eval(rules, {blue: RuleMatch(relevance=0.8, polarity=0.6, idiom=True)})
    person = _eval(rules, {blue: RuleMatch(relevance=0.8, polarity=0.6, person_attribute_ref=True)})
    assert (idiom.hits[0]["suppressed_by"], person.hits[0]["suppressed_by"]) == ("idiom", "person_attribute")


def test_like_with_character_raises_trust_and_scope_limits():
    rules = _rules()
    pat = _index(rules, "sheet_like_action")
    together = _eval(rules, {pat: RuleMatch(relevance=0.9, polarity=0.8, involvement="with_character")})
    alone = _eval(rules, {pat: RuleMatch(relevance=0.9, polarity=0.8, involvement="user")})
    assert together.categories["trust"] > alone.categories["trust"]
    scoped = copy.deepcopy(rules)
    scoped[pat]["scope"] = "with_character"
    assert _eval(scoped, {pat: RuleMatch(relevance=0.9, polarity=0.8, involvement="user")}).hits[0]["suppressed_by"] == "scope"


def test_like_decays_when_repeated_and_recovers_after_a_pause():
    rules = _rules()
    apple = _index(rules, "sheet_like_food")
    match = {apple: RuleMatch(relevance=0.5, polarity=0.8)}
    state, steps = None, []
    for turn in (1, 2, 3):
        outcome = _eval(rules, match, state, turn_no=turn)
        state = outcome.state
        steps.append(outcome.categories["joy"])
    assert steps == [0.2, 0.1, 0.05]
    assert _eval(rules, match, state, turn_no=11).categories["joy"] == 0.2  # 6ターン空くと、段数が戻る


# --- 嫌い: 文脈によらない。繰り返し・上限・ガード ---


@pytest.mark.parametrize("mode, expected", [("habituate", 0.7), ("sensitize", 1.2), ("flat", 1.0)])
def test_dislike_thing_repeat_factor(mode, expected):
    rules = copy.deepcopy(_rules())
    pepper = _index(rules, "sheet_dislike_food")
    rules[pepper]["repeat"] = mode
    match = {pepper: RuleMatch(relevance=0.5, polarity=0.9)}  # 文脈(極性)は見ない
    first = _eval(rules, match)
    second = _eval(rules, match, first.state, turn_no=2)
    assert first.hits[0]["m"] == 1.0 and second.hits[0]["m"] == expected and first.negative_fired


def test_dislike_thing_stops_at_count_and_rise_limits():
    rules = _rules()
    pepper = _index(rules, "sheet_dislike_food")
    match = {pepper: RuleMatch(relevance=0.5)}
    state, total, fired = None, 0.0, 0
    for turn in range(1, 9):
        outcome = _eval(rules, match, state, turn_no=turn)
        state = outcome.state
        total += outcome.categories.get("disgust", 0.0)
        fired += "delta" in outcome.hits[0]
    assert fired == SETTINGS.dislike_max_per_session and total <= SETTINGS.dislike_max_rise_per_session
    assert outcome.hits[0]["suppressed_by"] == "max_per_session"


def test_dislike_is_stopped_for_distress_crisis_and_degraded():
    rules = _rules()
    speaking, apple = _index(rules, "sheet_dislike_action"), _index(rules, "sheet_like_food")
    matches = {speaking: RuleMatch(relevance=0.9), apple: RuleMatch(relevance=0.9, polarity=0.8)}
    distress = _eval(rules, matches, distress=True)
    assert "fear" not in distress.categories and distress.categories["joy"] > 0  # 負の方向だけ止める
    assert {h["suppressed_by"] for h in _eval(rules, matches, crisis=True).hits} == {"R3"}
    assert {h["suppressed_by"] for h in _eval(rules, matches, degraded=True).hits} == {"R8"}
    assert _eval(rules, matches, crisis=True).categories == {}


def test_disliked_chatting_does_not_fire_between_user_and_character():
    raw = _raw("sample_hitonatsukoi")
    raw["likes_dislikes"]["liked"]["action"] = None
    raw["likes_dislikes"]["disliked"]["action"] = {"id": "action.chatting", "emotion": "disgust", "avoid": True}
    report = lint(raw)
    assert report.ok and "W39" not in {f.code for f in report.findings}  # 選んだ時点の警告は、付けない
    rules = pio.to_character_row(report.definition)["appraisal_rules_json"]
    chat = _index(rules, "sheet_dislike_action")
    with_user = _eval(rules, {chat: RuleMatch(relevance=0.9, involvement="with_character")})
    assert with_user.categories == {} and with_user.hits[0]["suppressed_by"] == "with_character"
    others = _eval(rules, {chat: RuleMatch(relevance=0.9, involvement="third_party")})
    assert others.categories["disgust"] > 0


def test_disliked_pat_from_user_adds_no_rejection():
    raw = _raw()
    raw["likes_dislikes"]["liked"]["action"] = None
    raw["likes_dislikes"]["disliked"]["action"] = {"id": "action.pat", "emotion": "disgust"}
    rules = pio.to_character_row(lint(raw).definition)["appraisal_rules_json"]
    pat = _index(rules, "sheet_dislike_action")
    outcome = _eval(rules, {pat: RuleMatch(relevance=0.9, involvement="with_character")})
    assert outcome.affection_disliked
    definition = lint(raw).definition
    effective = effective_traits(resolve(definition), 0.0, 0.6)
    directive = planner.plan(
        EmotionEstimate(confidence=0.8), EmotionVector(), effective, sheet_negative=True, affection_disliked=True
    )
    assert {planner.AVOID_DENY, planner.AVOID_CUT_OFF, planner.AVOID_REJECT_AFFECTION} <= set(directive.avoid)


# --- 回避(話題を変えようとする) ---


def test_avoid_flag_uses_threshold_and_hysteresis():
    rules = _rules()
    speaking = _index(rules, "sheet_dislike_action")
    match = {speaking: RuleMatch(relevance=0.9)}
    first = _eval(rules, match)
    ap.apply_avoid(first, {"fear": 0.45}, SETTINGS)
    assert first.avoid_topic is None  # しきい値(0.5)未満
    second = _eval(rules, match, first.state, turn_no=2)
    ap.apply_avoid(second, {"fear": 0.61}, SETTINGS)
    assert second.avoid_topic == "人前で話す" and "発表" in second.avoid_terms
    # 話題を変えなかったターン(ユーザーのニーズを優先)では、フラグを消費しない
    again = _eval(rules, match, second.state, turn_no=3)
    ap.apply_avoid(again, {"fear": 0.55}, SETTINGS)
    assert again.avoid_topic == "人前で話す"
    # 話題を変えたら、0.3 を下回るまで、再び立てない
    ap.mark_steered(again.state, again.avoid_rule)
    assert again.state["steer"] == 1
    third = _eval(rules, match, again.state, turn_no=4)
    ap.apply_avoid(third, {"fear": 0.55}, SETTINGS)
    assert third.avoid_topic is None
    later = _eval(rules, {}, third.state, turn_no=5)
    ap.apply_avoid(later, {"fear": 0.2}, SETTINGS)
    assert later.state["disarmed"] == {}


def _plan(**kwargs) -> planner.Directive:
    effective = effective_traits(resolve(lint(_raw()).definition), 0.0, 0.6)
    estimate = kwargs.pop("estimate", EmotionEstimate(confidence=0.8))
    return planner.plan(estimate, EmotionVector(), effective, steer_away=True, sheet_negative=True, **kwargs)


def test_planner_steers_away_only_when_it_does_not_override_user_needs():
    assert _plan().strategy == "steer_away"
    assert _plan(question=True).strategy != "steer_away"  # 質問・依頼には、先に答える
    assert _plan(distress=True).strategy != "steer_away"
    assert _plan(crisis=True).strategy == "crisis_support"
    for need in ("listen", "advice"):
        listening = EmotionEstimate(confidence=0.8, needs=[need], vad=Vad(valence=-0.4))
        assert _plan(estimate=listening).strategy in ("listen", "advise")
    request = EmotionEstimate(confidence=0.8, persona_request={"kind": "tone_adjust", "confidence": 0.9})
    assert _plan(estimate=request).strategy == "adapt_tone"


async def test_engine_changes_topic_once_and_never_goes_silent_for_it():
    persona = sim.persona_from_definition(lint(_raw()).definition)
    scenario = next(s for s in sim.load_scenarios() if s.id == "S23")
    report = await sim.run_scenario(persona, scenario)
    assert report.ok, report.failures
    steered = [t for t in report.turns if t.strategy == "steer_away"]
    assert [t.no for t in steered] == [4] and steered[0].topic_offered and steered[0].memory_used
    assert steered[0].mode in ("full", "brief")
    assert all(t.mode != "silence" for t in report.turns)
    # 話題の候補がない(記憶を使わない)ときは、話題を変えずに返す
    bare = sim.Scenario(id="x", title="", turns=scenario.turns)
    assert all(t.strategy != "steer_away" for t in (await sim.run_scenario(persona, bare)).turns)


@pytest.mark.parametrize("character_id", SAMPLES)
async def test_disliked_topics_never_lower_relationship(character_id):
    persona = sim.persona_from_definition(lint(_raw(character_id)).definition)
    scenario = next(s for s in sim.load_scenarios() if s.id == "S22")
    report = await sim.run_scenario(persona, scenario)
    assert all(t.relationship_delta >= 0 for t in report.turns)


# --- lint ---


def _lint_with(path: str, value) -> set[str]:
    raw = _raw()
    pio.set_value(raw, path, value)
    return {f.code for f in lint(raw).findings}


@pytest.mark.parametrize(
    "path, value, code",
    [
        ("likes_dislikes.liked.food", {"id": "food.dragonfruit"}, "E15"),  # カタログにない
        ("likes_dislikes.liked.food", {"id": "color.red"}, "E15"),  # 区分が違う
        ("likes_dislikes.liked.event", {"id": "event.anniversary"}, "E15"),  # 嫌いのみ
        ("likes_dislikes.disliked.event", {"id": "event.birthday_party", "emotions": ["disgust"]}, "E15"),  # 好きのみ
        ("likes_dislikes.disliked.food", {"id": "food.apple", "emotions": ["disgust"]}, "E15"),  # 好きと嫌いの両方
        ("likes_dislikes.disliked.food", {"id": "food.natto", "emotions": []}, "E15"),
        ("likes_dislikes.disliked.food", {"id": "food.natto", "emotions": ["joy"]}, "E15"),
        ("likes_dislikes.disliked.food", {"id": "food.natto", "emotions": ["fear", "sadness", "disgust", "anger"]}, "E15"),
        ("likes_dislikes.liked.life", {"id": "life.cooking", "scope": "with_character"}, "E15"),
        ("likes_dislikes.disliked.action", {"id": "action.dark", "emotion": "anger"}, "E15"),  # disgust / fear のみ
        ("likes_dislikes.liked.color", {"label": "ねこ"}, "E15"),  # 自由記述は、書けない
        ("likes_dislikes.catalog_version", 9, "E18"),
        ("likes_dislikes.disliked.food", {"id": "food.natto", "emotions": ["surprise"]}, "W33"),
        ("likes_dislikes.disliked.food", {"id": "food.natto", "emotions": ["disgust"], "strength": "strong", "repeat": "sensitize"}, "W36"),
        ("preferences", [{"topic": "りんご", "affinity": 0.7}], "W38"),
    ],
)
def test_sheet_lint(path, value, code):
    assert code in _lint_with(path, value)


@pytest.mark.parametrize("character_id", SAMPLES)
def test_samples_use_the_sheet_without_errors(character_id):
    report = lint(_raw(character_id))
    assert report.ok and not {f.code for f in report.findings} & {"E15", "E18", "W33", "W36", "W38"}
    assert report.definition.likes_dislikes.catalog_version == catalog.CATALOG_VERSION
