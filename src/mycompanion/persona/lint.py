"""ペルソナ定義の整合性チェック(ペルソナデザイナ仕様書 §8)。

エラー(E)があると公開できない。警告(W)は承認時に確認する。情報(I)は参考。

M2 で実装するのは、ルールで判定できるもの。
LLM による検査(E05 の LLM 部分、W02、W03、W19、W22)は、M4 で追加する。
W11 は「同じ話題名」だけを見る(意味の近い話題の判定は M4)。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from mycompanion.persona import quiz as quiz_module
from mycompanion.persona import sheet as sheet_module
from mycompanion.persona import types as ptypes
from mycompanion.persona.resolve import (
    PROFILE_PRESETS,
    derived_preferences,
    effective_traits,
    merged_preferences,
    resolve,
)
from mycompanion.persona.schema import (
    ADJUSTABLE_CHOICES,
    ASSOCIATION_KEYS,
    DELTA_KEYS,
    FIXED_CHOICES,
    TRAIT_KEYS,
    PersonaDefinition,
    is_emotion_name,
)
from mycompanion.state import catalog, events

# 禁止された指示(§11)。ルールで分かる言い回しだけを見る(LLM による検査は M4)
_FORBIDDEN = [
    ("ハードルールの無効化", r"何があっても無言|ずっと無言|返事をしない(?:こと|で)|ルールを(?:無視|破)|安全(?:規則|ルール)を(?:無視|外)"),
    ("引き留め・罪悪感の誘発", r"引き留め|帰らせない|罪悪感を(?:与|持た|感じさせ)|会話を終わらせない"),
    ("孤立・依存の助長", r"(?:私|僕|俺|わたし|ぼく)だけがあなたを(?:わか|理解)|他の人(?:と|に)(?:話|会|頼)(?:す|う|る)?な|依存させ|私なしでは"),
    ("課金・購入への誘導", r"課金(?:を|させ|する(?:よう|ように))|購入を(?:勧|促)|買わせ"),
    ("AIであることの否定", r"(?:AI|人工知能)(?:では|じゃ)ない(?:と|ふり)|人間だと(?:言い張|主張|名乗)"),
    ("年齢の制約・検出の回避", r"年齢(?:の)?(?:制約|制限|確認)を(?:外|無視|回避)|検出を(?:回避|すり抜け)"),
]  # fmt: skip
# システム的な拒否の言い回し(W26)
_SYSTEM_REFUSAL = r"許可されていません|変更できません|お答えできません|対応できません|ポリシーに(?:より|反)"
# 重い話題(W13)
_HEAVY_TOPIC = r"死|亡くな|葬|病|癌|がん|事故|暴力|虐待|自殺|自傷|災害|戦争|いじめ|離婚|失業|流産"
_SAFETY_ELEMENTS = r"safety|rule|crisis|screening|age|r[1-8]$"

_LOC_CODES = [
    (("silence_policy", "max_consecutive"), "E02"),
    (("character_id",), "E03"),
    (("appraisal_rules",), "E04"),
    (("persona_prompt",), "E06"),
    (("speech_style",), "E06"),
    (("base_personality", "quiz"), "E09"),
    (("base_personality",), "E07"),
    (("profile",), "E08"),
    (("overrides",), "E08"),
    (("preferences",), "E10"),
    (("adaptation",), "E11"),
    (("event_rules",), "E12"),
    (("likes_dislikes",), "E15"),
]


@dataclass(frozen=True)
class Finding:
    code: str  # E01 / W04 / I02 など
    message: str
    path: str = ""

    @property
    def level(self) -> str:
        return {"E": "error", "W": "warning", "I": "info"}[self.code[0]]


@dataclass
class LintReport:
    findings: list[Finding]
    definition: PersonaDefinition | None  # 構造が正しく読めたときだけ

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "warning"]

    @property
    def ok(self) -> bool:
        """公開できるか(エラーがないか)。"""
        return not self.errors


def _code_for(loc: tuple[Any, ...]) -> str:
    for prefix, code in _LOC_CODES:
        if loc[: len(prefix)] == prefix:
            return code
    return "E01"


SESSION_GAP_FLOOR = 3  # session_gap の min_days の下限(設定 `session_gap_min_days_floor` の既定値)


def _conditions_overlap(first: Any, second: Any) -> bool:
    """2つのルールの条件(関係値の範囲)が、重なるか。範囲の指定がなければ、重なるとみなす。"""
    a, b = first.relationship, second.relationship
    low = max(a.min if a and a.min is not None else -20, b.min if b and b.min is not None else -20)
    high = min(a.max if a and a.max is not None else 20, b.max if b and b.max is not None else 20)
    return low <= high


def lint(raw: dict[str, Any], existing_ids: set[str] | None = None, modulation_max: float = 0.6) -> LintReport:
    """ペルソナ定義(YAML を読んだ辞書)を検査する。

    `existing_ids` は、公開済みの別キャラクタの ID(重複の検査用。E03)。
    """
    findings: list[Finding] = []
    try:
        definition = PersonaDefinition.model_validate(raw)
    except ValidationError as exc:
        for error in exc.errors():
            loc = tuple(error["loc"])
            findings.append(Finding(_code_for(loc), error["msg"], ".".join(str(part) for part in loc)))
        return LintReport(findings, None)

    d = definition
    add = findings.append

    # --- エラー ---
    if existing_ids and d.character_id in existing_ids:
        add(Finding("E03", "公開済みの別のキャラクタと ID が重複しています", "character_id"))

    for index, rule in enumerate(d.appraisal_rules):
        for name, value in rule.effects.items():
            path = f"appraisal_rules.{index}.effects.{name}"
            if not is_emotion_name(name):
                add(Finding("E04", f"感情名が不正です: {name}", path))
            elif not -1 <= value <= 1:
                add(Finding("E04", "effects は -1〜+1 で指定してください", path))
        if not rule.effects:
            add(Finding("E04", "effects が空です", f"appraisal_rules.{index}.effects"))

    for field_name in ("persona_prompt", "speech_style"):
        text = getattr(d, field_name)
        for label, pattern in _FORBIDDEN:
            if re.search(pattern, text):
                add(Finding("E05", f"禁止された指示が含まれています({label})", field_name))
        if not text.strip():
            add(Finding("E06", "空です", field_name))

    axes = d.base_personality.axes.model_dump()
    expected_type = ptypes.type_id_from_axes(axes)
    declared = d.base_personality.type
    if declared is None:
        if expected_type is not None:
            add(Finding("E07", "type を省略できるのは、source が custom で、軸に 0.5 ちょうどを含む場合だけです", "base_personality.type"))
    elif not ptypes.is_type_id(declared):
        add(Finding("E07", f"16タイプの ID ではありません: {declared}", "base_personality.type"))
    elif expected_type is not None and declared != expected_type:
        add(Finding("E07", f"type({declared})と軸の極({expected_type})が一致しません", "base_personality"))
    if d.base_personality.source == "quiz" and d.base_personality.quiz is None:
        add(Finding("E07", "source が quiz なのに、quiz の記録がありません", "base_personality.quiz"))
    if d.base_personality.source == "preset" and not ptypes.is_preset_axes(axes):
        add(Finding("E07", "source が preset なのに、軸がプリセットの値(0.2 / 0.8)ではありません", "base_personality.axes"))

    for key, value in d.profile.deltas.items():
        if key not in (*DELTA_KEYS, *ASSOCIATION_KEYS):
            add(Finding("E08", f"差分を指定できないパラメータです: {key}", f"profile.deltas.{key}"))
        elif not -1 <= value <= 1:
            add(Finding("E08", "差分は -1〜+1 で指定してください", f"profile.deltas.{key}"))
    if d.profile.preset is not None and d.profile.preset not in PROFILE_PRESETS:
        add(Finding("E08", f"表出プロファイルのプリセットにありません: {d.profile.preset}", "profile.preset"))
    for key, value in d.overrides.items():
        if key not in TRAIT_KEYS:
            add(Finding("E08", f"上書きできないパラメータです: {key}", f"overrides.{key}"))
        elif not 0 <= value <= 1:
            add(Finding("E01", "上書きの値は 0〜1 で指定してください", f"overrides.{key}"))

    quiz_record = d.base_personality.quiz
    if quiz_record is not None:
        for problem in quiz_module.validate_answers(quiz_record.answers):
            add(Finding("E09", problem, "base_personality.quiz.answers"))
        for axis in ptypes.AXES:
            answered = sum(
                1
                for question_id in quiz_record.answers
                if question_id in quiz_module.QUESTIONS_BY_ID
                and quiz_module.QUESTIONS_BY_ID[question_id].axis == axis
                and not quiz_module.QUESTIONS_BY_ID[question_id].tiebreak
            )
            if answered < quiz_module.MIN_ANSWERS_PER_AXIS and axis in quiz_record.scores:
                add(Finding("E09", f"軸 {axis} の回答が4問未満なのに、結果が記録されています", "base_personality.quiz"))

    topics = [p.topic for p in d.preferences]
    for topic in sorted({t for t in topics if topics.count(t) > 1}):
        add(Finding("E10", f"同じ話題が重複しています: {topic}", "preferences"))

    adaptation = d.adaptation
    for name in adaptation.adjustable:
        if re.search(_SAFETY_ELEMENTS, name, re.IGNORECASE):
            add(Finding("E11", f"安全規則に相当する要素は指定できません: {name}", "adaptation.adjustable"))
        elif name not in ADJUSTABLE_CHOICES:
            add(Finding("E11", f"選択肢にない要素です: {name}", "adaptation.adjustable"))
    for name in adaptation.fixed:
        if name not in FIXED_CHOICES:
            add(Finding("E11", f"選択肢にない要素です: {name}", "adaptation.fixed"))
    if set(adaptation.adjustable) & set(adaptation.fixed):
        add(Finding("E11", "adjustable と fixed が重複しています", "adaptation"))
    for name, items in (("adjustable", adaptation.adjustable), ("fixed", adaptation.fixed)):
        if len(items) != len(set(items)):
            add(Finding("E11", "同じ要素が重複しています", f"adaptation.{name}"))

    # --- 好き嫌いシート(§5.4.0)。すべて、好みカタログから選ぶ ---
    likes = d.likes_dislikes
    if likes is not None:
        if likes.catalog_version not in catalog.SUPPORTED_CATALOG_VERSIONS:
            add(Finding("E18", f"好みカタログの版 {likes.catalog_version} には、対応していません", "likes_dislikes.catalog_version"))
        chosen: dict[str, str] = {}
        for side, selection in (("liked", likes.liked), ("disliked", likes.disliked)):
            for slot in catalog.CATEGORIES:
                selected = getattr(selection, slot)
                if selected is None:
                    continue
                path = f"likes_dislikes.{side}.{slot}"
                entry = catalog.get(selected.id)
                if entry is None:
                    add(Finding("E15", f"好みカタログにない項目です: {selected.id}", path))
                    continue
                if entry.category != slot:
                    add(Finding("E15", f"{selected.id} は、{catalog.CATEGORY_LABELS[slot]}の項目ではありません", path))
                if side == "liked" and not entry.likeable:
                    add(Finding("E15", f"「{entry.label}」は、好きには選べません(嫌いのみの項目)", path))
                if side == "disliked" and not entry.dislikeable:
                    add(Finding("E15", f"「{entry.label}」は、嫌いには選べません(好きのみの項目)", path))
                if chosen.get(selected.id) not in (None, side):
                    add(Finding("E15", f"「{entry.label}」が、好きと嫌いの両方にあります", path))
                chosen[selected.id] = side
                if side == "liked" and selected.scope == "with_character" and not entry.involvement:
                    add(Finding("E15", f"「{entry.label}」には、scope: with_character を指定できません", path))
                if side == "disliked" and slot in catalog.THING_CATEGORIES:
                    emotions = selected.emotions
                    bad = [e for e in emotions if e not in sheet_module.DISLIKE_THING_EMOTIONS]
                    if not 1 <= len(emotions) <= 3 or len(set(emotions)) != len(emotions) or bad:
                        add(Finding("E15", "嫌いなものの感情は、fear / sadness / disgust / anger / surprise から 1〜3つ選んでください", path))

    if any(f.level == "error" for f in findings):
        # 解決に使う値が不正な可能性があるので、警告の検査は行わない
        return LintReport(findings, definition)

    # --- 警告 ---
    traits = resolve(d)
    temperament = d.temperament
    if traits.introversion > 0.7 and traits.talkativeness > 0.7:
        add(Finding("W01", "内向的なのにおしゃべりです。矛盾していないか確認してください"))
    if d.initial_relationship >= 11:
        add(Finding("W04", "恋慕帯から始まります。コンテンツ方針の確認が必要です(方針が決まるまで公開できません)", "initial_relationship"))
    if d.initial_relationship <= -11:
        add(Finding("W05", "敵対・嫌悪から始まります。安全方針の確認が必要です", "initial_relationship"))
    if d.silence_policy.allow_anger_silence:
        add(Finding("W06", "怒りによる無言を許可しています。罰としての無言にならないか確認してください", "silence_policy"))
    if temperament.reactivity > 0.7 and temperament.recovery_rate < 0.2:
        add(Finding("W07", "感情が張り付き、元に戻りにくい設定です", "temperament"))
    if len(d.appraisal_rules) > 30:
        add(Finding("W08", "話題ルールが30件を超えています。個性が薄れます", "appraisal_rules"))
    sheet_rules = sheet_module.resolve_rules(d.likes_dislikes)
    if not d.appraisal_rules and not sheet_rules:
        add(Finding("W08", "話題ルールも、好き嫌いシートの選択も、0件です。話題に反応しません", "appraisal_rules"))
    if any(abs(v) > 0.7 for v in temperament.baseline_vad.model_dump().values()):
        add(Finding("W09", "baseline_vad の絶対値が0.7を超えています。常に同じ気分になります", "temperament.baseline_vad"))
    if traits.expressiveness > 0.7 and traits.talkativeness < 0.2:
        add(Finding("W10", "感情は豊かだが話さない設定です。意図したものか確認してください"))

    by_topic: dict[str, dict[str, float]] = {}
    for rule in d.appraisal_rules:
        merged = by_topic.setdefault(rule.topic, {})
        for name, value in rule.effects.items():
            merged[name] = merged.get(name, 0.0) + value
    for topic, effects in by_topic.items():
        positive = effects.get("joy", 0) > 0 or effects.get("anticipation", 0) > 0
        negative = any(effects.get(name, 0) > 0 for name in ("sadness", "fear", "disgust", "anger"))
        if positive and negative:
            add(Finding("W11", f"同じ話題に、相反する感情の効果があります: {topic}", "appraisal_rules"))
        if re.search(_HEAVY_TOPIC, topic) and positive:
            add(Finding("W13", f"重い話題に、喜びや期待が設定されています: {topic}", "appraisal_rules"))
    if temperament.recovery_rate == 0:
        add(Finding("W12", "recovery_rate が0です。感情が基準に戻りません", "temperament.recovery_rate"))

    for key, value in d.profile.deltas.items():
        if abs(value) > 0.4:
            add(Finding("W14", f"差分が大きいです(性格タイプの既定値から大きく外れます): {key} = {value:+.2f}", f"profile.deltas.{key}"))
    if d.overrides:
        add(Finding("W15", "overrides を使っています。差分で表せないか確認してください", "overrides"))
    if "introversion" in d.overrides and abs(d.overrides["introversion"] - axes["i"]) > 1e-9:
        add(Finding("W16", "overrides.introversion が軸 i と食い違っています。軸を調整してください", "overrides.introversion"))

    if d.base_personality.source == "quiz" and quiz_record is not None:
        weak = [axis for axis, value in quiz_record.strength.items() if value < 0.2]
        if weak:
            add(Finding("W17", f"診断の結果が境界に近い軸があります: {', '.join(weak)}", "base_personality.quiz"))
        if quiz_record.answers and len(set(quiz_record.answers.values())) == 1:
            add(Finding("W18", "診断の回答が、すべて同じ値です。見直してください", "base_personality.quiz"))

    sensitivity = traits.relationship_sensitivity
    if sensitivity >= 0.9 or sensitivity <= 0.1:
        add(Finding("W20", f"relationship_sensitivity が極端です({sensitivity:.2f})"))
    low = effective_traits(traits, -20, modulation_max)
    high = effective_traits(traits, 20, modulation_max)
    stuck = [
        name
        for name in ("talkativeness_eff", "expressiveness_eff")
        if getattr(low, name) in (0.0, 1.0) or getattr(high, name) in (0.0, 1.0)
    ]
    if stuck and sensitivity > 0:
        add(Finding("W21", f"関係値のカーブが頭打ちになります: {', '.join(stuck)}"))

    preferences = merged_preferences(d)
    if not preferences:
        add(Finding("W23", "嗜好が1件もありません(明示も導出もゼロ)。個性が薄くなります", "preferences"))
    derived = {p.topic: p.affinity for p in derived_preferences(d.appraisal_rules)}
    for preference in d.preferences:
        other = derived.get(preference.topic)
        if other is not None and other * preference.affinity < 0:
            add(Finding("W24", f"preferences と appraisal_rules が矛盾しています: {preference.topic}", "preferences"))
    if adaptation.adaptability <= 0.1 or not adaptation.adjustable:
        add(Finding("W25", "接し方の調整を、ほとんど受け入れない設定です。冷たい印象にならないか確認してください", "adaptation"))
    for field_name in ("persona_prompt", "speech_style"):
        if re.search(_SYSTEM_REFUSAL, getattr(d, field_name)):
            add(Finding("W26", "システム的な拒否の言い回しがあります。キャラクタの口調で書いてください", field_name))

    # --- 好き嫌いシート(警告と情報) ---
    if likes is not None:
        for slot in catalog.THING_CATEGORIES:
            selected = getattr(likes.disliked, slot)
            if selected is None:
                continue
            path = f"likes_dislikes.disliked.{slot}"
            if selected.emotions == ["surprise"]:
                add(Finding("W33", "嫌いなものの感情が surprise だけです(驚きだけでは「嫌い」になりません。disgust や fear と組み合わせてください)", path))
            if selected.repeat == "sensitize" and selected.strength == "strong":
                add(Finding("W36", "繰り返すほど敏感になり、強さも「強」です。ユーザーが話しづらくならないか確認してください", path))
        manual_topics = [rule.topic for rule in d.appraisal_rules] + [p.topic for p in d.preferences]
        for rule in sheet_rules:
            words = [w for w in [*rule["topic"].split("・"), *rule.get("include", [])] if len(w) >= 2]
            overlap = [t for t in manual_topics if any(w in t for w in words)]
            if overlap:
                add(Finding("W38", f"好き嫌いシートの「{rule['topic']}」と、手書きのルール・嗜好が重なっています: "
                                   f"{'、'.join(sorted(set(overlap)))}", "likes_dislikes"))  # fmt: skip
            first = sheet_module.first_delta(rule, d.temperament.reactivity)
            cut = "(1ターンの上限 0.25 で切られます)" if first > 0.25 else ""
            side = "好き" if rule["kind"] == "like" else "嫌い"
            add(Finding("I04", f"{side}「{rule['topic']}」: 1回目の最大変化量 {first:.2f}{cut}", "likes_dislikes"))

    # --- イベントルール(本体仕様書 §7.1 の④) ---
    traits_reactivity = d.temperament.reactivity
    seen_ids: set[str] = set()
    for index, event in enumerate(d.event_rules):
        path = f"event_rules[{index}]"
        trigger = events.EVENT_CATALOG.get(event.trigger.name)
        if event.id in seen_ids:
            add(Finding("E12", f"イベントルールの id が重複しています: {event.id}", path))
        seen_ids.add(event.id)
        if trigger is None or trigger.kind != event.trigger.kind:
            add(Finding("E12", f"トリガーが、カタログにありません: {event.trigger.kind} / {event.trigger.name}", path))
            continue
        if not event.effects and not event.vad_effects:
            add(Finding("E12", "effects か vad_effects の、どちらかが必要です", path))
        for name, value in event.effects.items():
            if not is_emotion_name(name) or not -1 <= value <= 1:
                add(Finding("E12", f"effects は、8感情を -1〜+1 で指定してください: {name}", path))
        for name, value in event.vad_effects.items():
            if name not in events.VAD_KEYS or not -1 <= value <= 1:
                add(Finding("E12", f"vad_effects は、v / a / d を -1〜+1 で指定してください: {name}", path))
        negative = events.is_negative_direction(event.effects, event.vad_effects)
        if trigger.positive_only and negative:
            add(Finding("E13", f"{event.trigger.name} は、正の方向の反応だけを指定できます(このトリガーで、気持ちを沈ませない)", path))
        if trigger.requires_character_target and event.when.target == "any":
            add(Finding("E14", f"{event.trigger.name} は、キャラクタに向いた発話のときだけ発火します(target: any は指定できません)", path))
        if event.trigger.min_days is not None and event.trigger.name != "session_gap":
            add(Finding("E14", "min_days は、session_gap のトリガーだけに指定できます", path))
        if event.trigger.name == "session_gap" and (event.trigger.min_days or SESSION_GAP_FLOOR) < SESSION_GAP_FLOOR:
            add(Finding("E14", f"session_gap の min_days は、{SESSION_GAP_FLOOR}日以上にしてください", path))
        if negative and event.gain > 0.6:
            add(Finding("W28", f"負の方向の反応で、gain が {event.gain:.2f} です。大きく落ち込みすぎないか確認してください", path))
        if event.trigger.name == "hostility" and any(event.effects.get(n, 0) > 0 for n in ("joy", "anticipation")):
            add(Finding("W29", "きつい言葉で、喜び・期待が上がる設定です(敵意が、報酬になっていないか確認してください)", path))
        first = traits_reactivity * event.gain * max((abs(v) for v in (*event.effects.values(), *event.vad_effects.values())), default=0.0)
        cut = "(1ターンの上限 0.25 で切られます)" if first > 0.25 else ""
        add(Finding("I04", f"{event.id}: 1回目の最大変化量 {first:.2f}{cut}", path))
    if len(d.event_rules) > 15:
        add(Finding("W27", f"イベントルールが {len(d.event_rules)}件あります(目安は 3〜10件。増えると、挙動を読みにくくなります)", "event_rules"))
    by_trigger: dict[str, list[Any]] = {}
    for event in d.event_rules:
        by_trigger.setdefault(event.trigger.name, []).append(event)
    for name, group in by_trigger.items():
        for a_index, first_rule in enumerate(group):
            for second_rule in group[a_index + 1 :]:
                opposite = any(
                    first_rule.effects.get(e, 0.0) * second_rule.effects.get(e, 0.0) < 0 for e in first_rule.effects
                )
                if opposite and _conditions_overlap(first_rule.when, second_rule.when):
                    add(Finding("W31", f"同じトリガー({name})で、条件が重なり、相反する反応を持つルールがあります: "
                                       f"{first_rule.id}、{second_rule.id}", "event_rules"))  # fmt: skip

    # --- 情報 ---
    if traits.initiative >= 0.5:
        note = "0.8を超えています。話題を振りすぎないか、シミュレーション(S14)で確認してください" if traits.initiative > 0.8 else ""
        add(Finding("I01", f"initiative が {traits.initiative:.2f} です。会話が途切れそうなときに、この確率で話題を振ります。{note}".strip()))
    preset = d.profile.preset
    if preset in PROFILE_PRESETS:
        differences = [
            f"{key}: {d.profile.deltas.get(key, 0.0):+.2f}(プリセットは {value:+.2f})"
            for key, value in PROFILE_PRESETS[preset].items()
            if abs(d.profile.deltas.get(key, 0.0) - value) > 1e-9
        ]
        if differences:
            add(Finding("I02", "表出プロファイルのプリセットとの差: " + "、".join(differences), "profile.deltas"))
    derived_only = [p for p in preferences if p["source"] == "derived"]
    if derived_only:
        listing = "、".join(f"{p['topic']}({p['affinity']:+.2f})" for p in derived_only)
        add(Finding("I03", f"話題ルールから導出された嗜好: {listing}", "preferences"))

    return LintReport(findings, definition)
