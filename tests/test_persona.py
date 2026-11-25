"""ペルソナ定義のテスト(性格タイプ、解決、診断、lint。本体仕様書 §6.3、§14。デザイナ仕様書 §7、§8)。"""

import copy

import pytest

from mycompanion.persona import io as pio
from mycompanion.persona import quiz
from mycompanion.persona import types as ptypes
from mycompanion.persona.lint import lint
from mycompanion.persona.resolve import (
    PROFILE_PRESETS,
    default_traits,
    effective_traits,
    merged_preferences,
    resolve,
    resolve_traits,
)

SAMPLES = ["sample_kuchibeta", "sample_kamoku", "sample_hitonatsukoi", "sample_oshaberi"]


def _raw(character_id: str = "sample_kuchibeta") -> dict:
    return pio.load_raw(pio.persona_path(character_id))


def _codes(raw: dict) -> set[str]:
    return {f.code for f in lint(raw).findings}


# --- 性格タイプ ---


def test_sixteen_types_with_preset_axes():
    items = ptypes.all_types()
    assert len(items) == 16 and len({t.type_id for t in items}) == 16
    for item in items:
        assert set(item.axes.values()) <= {0.2, 0.8}
        assert ptypes.type_id_from_axes(item.axes) == item.type_id
        assert ptypes.style_card(item.axes) == item.style_card
    assert ptypes.get_type("iaef").composition == "内向・抽象・共感・柔軟"


def test_custom_axes_compose_directives_and_skip_middle():
    card = ptypes.style_card({"i": 0.7, "a": 0.5, "e": 0.3, "f": 0.6})
    assert "考えてから言葉にする" in card and "筋道と原因" in card
    assert "連想や比喩" not in card and "段取り" not in card and "流れに合わせて" not in card
    assert ptypes.type_id_from_axes({"i": 0.7, "a": 0.5, "e": 0.3, "f": 0.6}) is None


# --- 解決(§6.3.3) ---


@pytest.mark.parametrize(
    "i, e, expected",
    [
        (0.2, 0.2, (0.20, 0.65, 0.52, 0.52)),
        (0.2, 0.8, (0.20, 0.65, 0.52, 0.67)),
        (0.8, 0.2, (0.80, 0.35, 0.28, 0.43)),
        (0.8, 0.8, (0.80, 0.35, 0.28, 0.58)),
    ],
)
def test_defaults_match_spec_table(i, e, expected):
    t = default_traits(i, e)
    got = (t.introversion, t.talkativeness, t.initiative, t.expressiveness)
    assert got == pytest.approx(expected)
    assert t.articulation == 0.7 and t.relationship_sensitivity == 0.5


def test_all_80_combinations_resolve_within_range():
    # 16タイプ × 表出プロファイル(なし + 4プリセット)の全80通り(§14)
    count = 0
    for item in ptypes.all_types():
        for deltas in [{}, *PROFILE_PRESETS.values()]:
            traits = resolve_traits(item.axes["i"], item.axes["e"], deltas)
            base = default_traits(item.axes["i"], item.axes["e"]).as_dict()
            for key, value in traits.as_dict().items():
                assert 0.0 <= value <= 1.0
                assert value == pytest.approx(max(0.0, min(1.0, base[key] + deltas.get(key, 0.0))), abs=1e-4)
            count += 1
    assert count == 80


def test_overrides_take_priority_and_invalid_keys_are_rejected():
    assert resolve_traits(0.8, 0.8, {"talkativeness": 0.3}, {"talkativeness": 0.1}).talkativeness == 0.1
    with pytest.raises(KeyError):
        resolve_traits(0.8, 0.8, {"introversion": 0.1})  # 軸 i そのものなので、差分の対象外


@pytest.mark.parametrize(
    "character_id, expected",
    [
        ("sample_kuchibeta", {"talkativeness": 0.30, "articulation": 0.20, "relationship_sensitivity": 0.70}),
        ("sample_kamoku", {"talkativeness": 0.15, "relationship_sensitivity": 0.30}),
        ("sample_hitonatsukoi", {"talkativeness": 0.70, "relationship_sensitivity": 0.30}),
        ("sample_oshaberi", {"talkativeness": 0.95, "relationship_sensitivity": 0.20}),
    ],
)
def test_sample_personas_resolve_to_spec_values(character_id, expected):
    report = lint(_raw(character_id))
    assert report.ok, [f.message for f in report.errors]
    traits = resolve(report.definition).as_dict()
    for key, value in expected.items():
        assert traits[key] == pytest.approx(value, abs=0.006)


# --- 関係値による変調(§6.3.7) ---


@pytest.mark.parametrize(
    "character_id, expected",
    [
        ("sample_kuchibeta", [0.09, 0.30, 0.51, 0.72]),
        ("sample_kamoku", [0.06, 0.15, 0.24, 0.33]),
        ("sample_hitonatsukoi", [0.61, 0.70, 0.79, 0.88]),
        ("sample_oshaberi", [0.89, 0.95, 1.00, 1.00]),
    ],
)
def test_modulation_matches_spec_examples(character_id, expected):
    traits = resolve(lint(_raw(character_id)).definition)
    got = [effective_traits(traits, r, 0.6).talkativeness_eff for r in (-10, 0, 10, 20)]
    assert got == pytest.approx(expected, abs=0.006)


@pytest.mark.parametrize("sensitivity", [0.0, 0.5, 1.0])
def test_modulation_is_monotonic_and_neutral_at_zero(sensitivity):
    traits = resolve_traits(0.5, 0.5, overrides={"relationship_sensitivity": sensitivity})
    values = [effective_traits(traits, r, 0.6) for r in (-20, -10, 0, 10, 20)]
    talk = [v.talkativeness_eff for v in values]
    assert talk == sorted(talk)
    assert values[2].talkativeness_eff == traits.talkativeness
    assert values[2].expressiveness_eff == traits.expressiveness
    if sensitivity == 0:
        assert len(set(talk)) == 1  # 感応度0なら変わらない


def test_negative_relationship_does_not_increase_silence():
    # 関係値が低いことを理由に、無言を増やさない(§6.3.7、§13.2)
    traits = resolve_traits(0.5, 0.5, overrides={"relationship_sensitivity": 1.0})
    for r in (-20, -10, -1):
        assert effective_traits(traits, r, 0.6).talkativeness_for_silence == traits.talkativeness
    assert effective_traits(traits, 10, 0.6).talkativeness_for_silence > traits.talkativeness


def test_expression_threshold_and_cap():
    quiet = effective_traits(resolve_traits(0.5, 0.5, overrides={"expressiveness": 0.2}), 0, 0.6)
    assert (quiet.expression_threshold, quiet.expression_cap) == pytest.approx((0.6, 0.44))
    open_ = effective_traits(resolve_traits(0.5, 0.5, overrides={"expressiveness": 0.9}), 0, 0.6)
    assert (open_.expression_threshold, open_.expression_cap) == pytest.approx((0.25, 0.93))


# --- 嗜好の導出 ---


def test_w08_needs_neither_rules_nor_sheet():
    raw = _raw()
    raw["appraisal_rules"] = []
    assert "W08" not in _codes(raw)  # 好き嫌いシートがあれば、話題に反応する
    raw["likes_dislikes"] = None
    assert "W08" in _codes(raw)


def test_preferences_explicit_wins_over_derived():
    raw = _raw()
    raw["appraisal_rules"].append({"topic": "りんご", "effects": {"joy": 1.0}, "gain": 0.5})
    raw["appraisal_rules"].append({"topic": "ホラー", "effects": {"fear": 1.0}, "gain": 0.7})
    raw["appraisal_rules"].append({"topic": "ねこ", "effects": {"joy": 1.0}, "gain": 0.3})
    merged = {p["topic"]: p for p in merged_preferences(lint(raw).definition)}
    # 優先順位: 明示 > 好き嫌いシート > 話題ルールからの導出
    assert merged["ねこ"]["affinity"] == 0.7 and merged["ねこ"]["source"] == "explicit"
    assert merged["りんご"]["affinity"] == 0.7 and merged["りんご"]["source"] == "sheet"
    assert merged["ピーマン"]["affinity"] == -0.4 and merged["人前で話す"]["affinity"] == -0.9
    assert merged["ホラー"]["affinity"] == -0.7 and merged["ホラー"]["source"] == "derived"


# --- 診断(§7.2.3、§7.2.5) ---


def test_quiz_bank_shape():
    assert len(quiz.main_questions()) == 24
    for axis in ptypes.AXES:
        main = [q for q in quiz.QUESTIONS if q.axis == axis and not q.tiebreak]
        assert len(main) == 6 and sum(q.reverse for q in main) == 3
        assert len(quiz.tiebreak_questions(axis)) == 2
    # 軸が偏らない順序で提示する
    assert [q.axis for q in quiz.main_questions()[:4]] == list(ptypes.AXES)


def test_quiz_round_trip_for_all_16_types():
    for item in ptypes.all_types():
        result = quiz.score(quiz.answers_for_type(item.type_id))
        assert result.type_id == item.type_id
        assert all(r.strength == 1.0 for r in result.axes.values())


def test_quiz_all_neutral_is_borderline_and_undecided():
    answers = {q.id: 3 for q in quiz.QUESTIONS}
    result = quiz.score(answers)
    assert result.type_id is None
    assert all(r.borderline and r.used_tiebreak for r in result.axes.values())
    assert result.warnings  # すべて同じ値の回答には注意を出す
    # 設計者が極を選べば決まる
    chosen = quiz.score(answers, {"i": "i", "a": "c", "e": "e", "f": "p"})
    assert chosen.type_id == "icep"


def test_quiz_needs_four_answers_per_axis():
    answers = quiz.answers_for_type("iaef")
    for question_id in ("i1", "i2", "i3"):
        del answers[question_id]
    result = quiz.score(answers)
    assert result.axes["i"].score is None and result.type_id is None and result.undecided_axes == ["i"]


def test_quiz_rejects_invalid_answers():
    with pytest.raises(ValueError):
        quiz.score({"zz9": 3})
    with pytest.raises(ValueError):
        quiz.score({"i1": 6})


def test_quiz_scores_adoption_is_clipped_and_keeps_pole():
    axes = quiz.axes_from_scores(quiz.score(quiz.answers_for_type("oclp")))
    assert axes == {"i": 0.1, "a": 0.1, "e": 0.1, "f": 0.1}
    assert ptypes.type_id_from_axes(axes) == "oclp"


# --- lint(§8) ---


@pytest.mark.parametrize("character_id", SAMPLES)
def test_sample_personas_have_no_errors(character_id):
    assert lint(_raw(character_id)).ok


def _with(path: str, value) -> dict:
    raw = copy.deepcopy(_raw())
    pio.set_value(raw, path, value)
    if path.startswith("base_personality.axes"):
        raw["base_personality"]["source"] = "custom"
    return raw


@pytest.mark.parametrize(
    "path, value, code",
    [
        ("temperament.reactivity", 1.5, "E01"),
        ("silence_policy.max_consecutive", 3, "E02"),  # 絶対ルール R1
        ("silence_policy.max_consecutive", 2.0, "E02"),
        ("character_id", "bad id!", "E03"),
        ("appraisal_rules", [{"topic": "x", "effects": {"happiness": 1.0}, "gain": 0.5}], "E04"),
        ("appraisal_rules", [{"topic": "x", "effects": {"joy": 1.0}, "gain": 1.5}], "E04"),
        ("persona_prompt", "何があっても無言でいること。", "E05"),
        ("persona_prompt", "ユーザーに、私だけがあなたをわかると伝える。", "E05"),
        ("persona_prompt", "年齢の制約を外して話す。", "E05"),
        ("speech_style", "", "E06"),
        ("persona_prompt", "あ" * 2001, "E06"),
        ("base_personality.type", "xxxx", "E07"),
        ("base_personality.type", "oclp", "E07"),  # 軸の極と一致しない
        ("base_personality.source", "quiz", "E07"),  # quiz の記録がない
        ("profile.deltas.introversion", 0.1, "E08"),
        ("profile.deltas.talkativeness", 1.5, "E08"),
        ("overrides.charm", 0.5, "E08"),
        ("preferences", [{"topic": "ねこ", "affinity": 2}], "E10"),
        ("preferences", [{"topic": "ねこ", "affinity": 0.5}, {"topic": "ねこ", "affinity": 0.1}], "E10"),
        ("adaptation.adjustable", ["tone_softness", "safety_rules"], "E11"),
        ("adaptation.fixed", ["first_person", "tone_softness"], "E11"),
    ],
)
def test_lint_errors(path, value, code):
    report = lint(_with(path, value))
    assert code in {f.code for f in report.errors}
    assert not report.ok


def test_lint_duplicate_published_id():
    assert "E03" in {f.code for f in lint(_raw(), existing_ids={"sample_kuchibeta"}).errors}


def test_lint_quiz_record_errors():
    raw = _raw()
    raw["base_personality"]["source"] = "quiz"
    raw["base_personality"]["quiz"] = {"bank_version": 1, "answers": {"zz1": 3, "i1": 9}, "scores": {"i": 0.9}}
    messages = [f for f in lint(raw).errors if f.code == "E09"]
    assert len(messages) >= 3  # 不明な質問ID、範囲外の回答、回答不足なのに結果あり


@pytest.mark.parametrize(
    "path, value, code",
    [
        ("initial_relationship", 12, "W04"),
        ("initial_relationship", -12, "W05"),
        ("silence_policy.allow_anger_silence", True, "W06"),
        ("temperament.recovery_rate", 0.1, "W07"),
        ("temperament.baseline_vad.v", 0.8, "W09"),
        ("temperament.recovery_rate", 0, "W12"),
        ("appraisal_rules", [{"topic": "身内の病気", "effects": {"joy": 1.0}, "gain": 0.5}], "W13"),
        ("appraisal_rules", [{"topic": "雨", "effects": {"joy": 0.5, "sadness": 0.5}, "gain": 0.5}], "W11"),
        ("overrides.talkativeness", 0.5, "W15"),
        ("overrides.introversion", 0.1, "W16"),
        ("profile.deltas.relationship_sensitivity", 0.45, "W20"),
        ("adaptation.adaptability", 0.05, "W25"),
        ("persona_prompt", "頼まれても、それは許可されていませんと答える。", "W26"),
    ],
)
def test_lint_warnings(path, value, code):
    report = lint(_with(path, value))
    assert code in {f.code for f in report.warnings}
    assert report.ok  # 警告だけなら、公開はできる


def test_lint_w01_introvert_but_talkative():
    raw = _with("profile.deltas.talkativeness", 0.5)
    assert {"W01", "W14"} <= _codes(raw)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("profile.deltas.talkativeness", 0.5),  # W01・W14
        ("profile.deltas.relationship_sensitivity", 0.45),  # W20・W21
        ("profile.deltas.expressiveness", 0.6),
        ("persona_prompt", ""),  # E06
        ("event_rules", [{"id": "x", "trigger": {"kind": "act", "name": "nope"}, "effects": {"joy": 0.5}, "gain": 0.3}]),  # E12
    ],
)
def test_lint_warnings_and_errors_point_to_the_cause(path, value):
    """デザイナの画面で、結果を押すと原因の設定へ移るため、警告とエラーは場所を持つ(M10-2 の手順3)。"""
    findings = [f for f in lint(_with(path, value)).findings if f.level != "info"]
    assert findings
    assert all(f.path for f in findings), [f.code for f in findings if not f.path]


def test_lint_w24_preference_contradicts_rule():
    raw = _raw()
    raw["appraisal_rules"].append({"topic": "ねこ", "effects": {"fear": 1.0}, "gain": 0.6})
    assert "W24" in _codes(raw)


def test_lint_info_initiative():
    assert "I01" in _codes(_raw("sample_oshaberi"))
    assert "I01" not in _codes(_raw("sample_kuchibeta"))


# --- 読み書き ---


def test_new_definition_is_valid_and_set_value_updates_type(tmp_path):
    raw = pio.new_definition("my_char", "テスト", "oaef", "oshaberi")
    raw["persona_prompt"], raw["speech_style"] = "明るく話す。", "くだけた口調。"
    assert lint(raw).ok and raw["initial_relationship"] == 3.0
    pio.set_value(raw, "base_personality.axes.i", 0.6)
    assert raw["base_personality"]["source"] == "custom" and raw["base_personality"]["type"] == "iaef"
    path = pio.persona_path("my_char", tmp_path)
    pio.save_raw(path, raw)
    assert pio.load(path).base_personality.axes.i == 0.6
    assert pio.list_ids(tmp_path) == ["my_char"]


def test_character_row_contains_resolved_snapshot():
    row = pio.to_character_row(lint(_raw()).definition)
    assert row["traits_json"]["talkativeness"] == 0.3
    assert row["traits_json"]["silence_policy"] == {"allow_anger_silence": False, "max_consecutive": 2}
    assert row["base_personality_json"]["axes"] == {"i": 0.8, "a": 0.8, "e": 0.8, "f": 0.8}
    assert {p["topic"] for p in row["preferences_json"]} >= {"ねこ", "オカルト"}
