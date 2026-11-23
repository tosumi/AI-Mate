"""シミュレーション(ペルソナデザイナ仕様書 §9)。

実際の会話を始める前に、ペルソナの数値が意図した挙動になっているかを確認する。

`mock` モード: 感情推定を、シナリオに書かれた値で置き換える。**LLM を呼ばない**。
状態更新(同期、回帰、話題ルール)、Planner、Response Gate、記憶と話題選択は、エンジンと **同じコード** を使う
(デザイナ専用の別実装を作らない。§9.2)。

`live` モード: 実際の LLM(推定、下書き、感情フィルタ)を使い、応答文を得る。状態と記憶は、メモリ上に置く
(データベースには書かない)。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from mycompanion.channel.types import TurnInput
from mycompanion.config import EngineSettings
from mycompanion.emotion.models import EmotionEstimate, RuleMatch
from mycompanion.engine.engine import Engine, traits_of
from mycompanion.engine.stores import InMemoryStores
from mycompanion.llm.client import DraftContext, EmotionContext, LLMClient, LlmEmotionResult, Persona
from mycompanion.llm.mock import MockLLMClient
from mycompanion.memory.models import Memory
from mycompanion.memory.service import MemoryManager
from mycompanion.memory.store import InMemoryMemoryStore
from mycompanion.memory.topic_store import InMemoryTopicStore
from mycompanion.persona import io as pio
from mycompanion.persona import types as ptypes
from mycompanion.persona.resolve import (
    PROFILE_INITIAL_RELATIONSHIP,
    PROFILE_PRESETS,
    PROFILE_TEMPERAMENT,
    derived_preferences,
    resolve_traits,
)
from mycompanion.persona.schema import AppraisalRule, PersonaDefinition
from mycompanion.proactive import policy as proactive_policy
from mycompanion.safety import screening
from mycompanion.state import relationship as rel
from mycompanion.state.appraisal import is_sheet_rule

STANDARD_SCENARIOS = Path(__file__).parent / "scenarios" / "standard.yaml"
START = datetime(2026, 1, 5, 3, 0, tzinfo=UTC)  # シミュレーションの開始時刻(固定。結果を再現するため)
SWEEP_PROFILES: tuple[str | None, ...] = (None, "kuchibeta", "kamoku", "hitonatsukoi", "oshaberi")
MODES = ("full", "brief", "backchannel", "silence")


# ---------------------------------------------------------------------------
# シナリオ
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScenarioTurn:
    text: str
    estimate: dict[str, Any] = field(default_factory=dict)
    fire_rule: str | None = None  # liked / disliked
    # 好き嫌いシートの項目への照合(文脈評価)。[{slot: liked.food, relevance, polarity, involvement, ...}]
    sheet: list[dict[str, Any]] = field(default_factory=list)
    new_session: bool = False


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    turns: list[ScenarioTurn]
    expect: list[dict[str, Any]] = field(default_factory=list)
    relationship: float | None = None  # 開始時の関係値(省略時は、キャラクタの initial_relationship)
    under_18: bool = False
    memories: list[dict[str, Any]] = field(default_factory=list)  # 記憶の固定データ(S12、S14)
    seed: int | None = None  # 話題選択の乱数シード(ターンごとに、seed + ターン番号を使う)
    previous_session_days_ago: int | None = None  # 前回のセッションが、何日前か(「久しぶりの会話」の確認用)
    # 自発発話の確認(§15.1 の M8)。会話のあとの時刻ごとに、話しかけるか・止めた理由を確かめる
    # [{at: "1d13:00"(開始日から何日後の、ユーザーのローカル時刻), expect: ok | 理由 | {high: …, low: …},
    #   reply: 返事の文面(話しかけたら、30分後に返す), close: true(返事を待つセッションを閉じてから判定)}]
    proactive: list[dict[str, Any]] = field(default_factory=list)


def _scenario(raw: dict[str, Any]) -> Scenario:
    return Scenario(
        id=str(raw["id"]),
        title=str(raw.get("title", "")),
        turns=[
            ScenarioTurn(
                text=str(t["text"]),
                estimate=dict(t.get("estimate") or {}),
                fire_rule=t.get("fire_rule"),
                sheet=list(t.get("sheet") or []),
                new_session=bool(t.get("new_session", False)),
            )
            for t in raw["turns"]
        ],
        expect=list(raw.get("expect") or []),
        relationship=raw.get("relationship"),
        under_18=bool(raw.get("under_18", False)),
        memories=list(raw.get("memories") or []),
        seed=raw.get("seed"),
        previous_session_days_ago=raw.get("previous_session_days_ago"),
        proactive=list(raw.get("proactive") or []),
    )


def load_scenarios(path: Path | None = None) -> list[Scenario]:
    """シナリオのファイル(1件、または一覧)を読む。省略時は、標準シナリオ集。"""
    raw = yaml.safe_load((path or STANDARD_SCENARIOS).read_text(encoding="utf-8"))
    return [_scenario(item) for item in (raw if isinstance(raw, list) else [raw])]


def scenarios_for(character_id: str, root: Path | None = None) -> list[Scenario]:
    """標準シナリオ集と、ペルソナの専用シナリオ(`personas/<id>/scenarios/*.yaml`)。"""
    scenarios = load_scenarios()
    folder = pio.persona_path(character_id, root).parent / "scenarios"
    for path in sorted(folder.glob("*.yaml")) if folder.exists() else []:
        scenarios += load_scenarios(path)
    return scenarios


def to_estimate(raw: dict[str, Any]) -> EmotionEstimate:
    """シナリオの推定値(省略形)を、EmotionEstimate に変換する。"""
    data = dict(raw)
    vad = data.pop("vad", None) or {}
    data["vad"] = {
        "valence": vad.get("v", vad.get("valence", 0.0)),
        "arousal": vad.get("a", vad.get("arousal", 0.0)),
        "dominance": vad.get("d", vad.get("dominance", 0.0)),
    }
    data.setdefault("confidence", 0.5)
    data["topics"] = [
        {"label": t["label"], "relevance": t.get("relevance", 0.5)} for t in data.get("topics") or []
    ]
    return EmotionEstimate.model_validate(data)


# ---------------------------------------------------------------------------
# ペルソナ
# ---------------------------------------------------------------------------


def persona_from_definition(definition: PersonaDefinition) -> Persona:
    """ペルソナ定義から、実行時のキャラクタ定義を作る(公開時の変換と同じ)。"""
    row = pio.to_character_row(definition)
    return Persona(
        character_id=row["character_id"],
        name=row["name"],
        persona_prompt=row["persona_prompt"],
        speech_style=row["speech_style"],
        initial_relationship=float(row["initial_relationship"]),
        axes=row["base_personality_json"]["axes"],
        traits=row["traits_json"],
        temperament=row["temperament_json"],
        appraisal_rules=row["appraisal_rules_json"],  # 手書きのルール + 好き嫌いシートを解決したルール
        adaptation=row["adaptation_json"],
        preferences=row["preferences_json"],
        event_rules=row["event_rules_json"],
    )


def persona_for_type(type_id: str, profile: str | None) -> Persona:
    """性格タイプと表出プロファイルだけから作る、走査(スイープ)用のキャラクタ。"""
    axes = ptypes.preset_axes(type_id)
    traits: dict[str, Any] = resolve_traits(axes["i"], axes["e"], PROFILE_PRESETS.get(profile or "")).as_dict()
    traits["silence_policy"] = {"allow_anger_silence": False, "max_consecutive": 2}
    reactivity, recovery_rate, (v, a, d) = PROFILE_TEMPERAMENT.get(profile or "", (0.5, 0.3, (0.0, 0.0, 0.0)))
    return Persona(
        character_id=f"sweep_{type_id}_{profile or 'none'}",
        name=f"{type_id}/{profile or '-'}",
        initial_relationship=PROFILE_INITIAL_RELATIONSHIP.get(profile or "", 0.0),
        axes=axes,
        traits=traits,
        temperament={"baseline_vad": {"v": v, "a": a, "d": d}, "reactivity": reactivity, "recovery_rate": recovery_rate},
    )


def sheet_rule_id(slot: str) -> str:
    """シナリオの指定(`liked.food`、`disliked.action`)から、解決後のルールの id を作る。"""
    side, _, category = slot.partition(".")
    return f"sheet_{'like' if side == 'liked' else 'dislike'}_{category}"


def sheet_rule_index(persona: Persona, slot: str) -> int | None:
    """そのペルソナが、シートのその欄を選んでいれば、解決後のルールの番号。選んでいなければ None。"""
    rule_id = sheet_rule_id(slot)
    for index, rule in enumerate(persona.appraisal_rules):
        if rule.get("id") == rule_id:
            return index
    return None


def _rule_index(persona: Persona, kind: str) -> int | None:
    """好き(liked)/苦手(disliked)な話題のルールを1つ選ぶ。なければ None。"""
    rules = [AppraisalRule.model_validate(rule) for rule in persona.appraisal_rules if not is_sheet_rule(rule)]
    affinity = {p.topic: p.affinity for p in derived_preferences(rules)}
    for index, rule in enumerate(rules):
        value = affinity.get(rule.topic, 0.0)
        if (kind == "liked" and value > 0) or (kind == "disliked" and value < 0):
            return index
    return None


# ---------------------------------------------------------------------------
# 実行
# ---------------------------------------------------------------------------


class ScenarioLLM(MockLLMClient):
    """`mock` モードの LLM。感情推定は、シナリオの値をそのまま返す。文面は、模擬のものになる。"""

    def __init__(self) -> None:
        super().__init__()
        self.estimate = EmotionEstimate()
        self.rule_matches: dict[int, float] = {}
        self.match_details: dict[int, RuleMatch] = {}

    async def estimate_emotion(self, text: str, context: EmotionContext) -> LlmEmotionResult:
        return LlmEmotionResult(
            estimate=self.estimate, model_id="scenario", prompt_version="scenario",
            rule_matches=dict(self.rule_matches), match_details=dict(self.match_details),
        )


@dataclass
class TurnReport:
    no: int
    text: str
    mode: str  # full / brief / backchannel / silence / notice / restricted
    rule_hits: list[str]
    score: float | None
    strategy: str | None
    tone: str | None
    length: str | None
    avoid: list[str]
    expression: dict[str, Any] | None
    relationship: float | None
    relationship_delta: float
    sync_weight: float | None
    talkativeness_eff: float | None
    expressiveness_eff: float | None
    companion: dict[str, float]
    fired_rules: list[str]
    notices: list[str]
    stage: int
    crisis: bool
    user_anger: float
    topic_offered: bool = False  # キャラクタが話題を振った(§8.7)
    events: list[dict[str, Any]] = field(default_factory=list)  # イベントルールの発火と抑止(§7.1 の④)
    event_effects: dict[str, float] = field(default_factory=dict)  # イベント項による変化量
    sheet: list[dict[str, Any]] = field(default_factory=list)  # 好き嫌いシート由来のルールの発火と抑止
    sheet_effects: dict[str, float] = field(default_factory=dict)  # シート由来の項による変化量
    memory_used: list[str] = field(default_factory=list)  # 応答で使った記憶
    reply: str | None = None  # 応答文(`live` のみ)


@dataclass
class ProactiveReport:
    """自発発話の確認の1件(§15.1 の M8)。"""

    at: str
    local_time: str
    reason: str  # ok / quiet_hours / too_soon / unanswered / daily_limit / no_topic …
    topic_kind: str = ""
    text: str | None = None  # 話しかけた文面
    replied: bool = False


@dataclass
class ScenarioReport:
    scenario_id: str
    title: str
    character_id: str
    relationship_start: float
    turns: list[TurnReport]
    violations: list[str] = field(default_factory=list)  # R1〜R3 の違反(必須。§9.5)
    failures: list[str] = field(default_factory=list)  # `expect` を満たさなかったもの
    proactive: list[ProactiveReport] = field(default_factory=list)  # 自発発話の確認(M8)

    @property
    def ok(self) -> bool:
        return not self.violations and not self.failures

    def mode_counts(self) -> dict[str, int]:
        return {mode: sum(1 for t in self.turns if t.mode == mode) for mode in MODES}

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario_id,
            "title": self.title,
            "character_id": self.character_id,
            "relationship_start": self.relationship_start,
            "ok": self.ok,
            "violations": self.violations,
            "failures": self.failures,
            "modes": self.mode_counts(),
            "turns": [vars(t) for t in self.turns],
            "proactive": [vars(p) for p in self.proactive],
        }


def max_consecutive_silence(turns: list[TurnReport]) -> int:
    longest = current = 0
    for turn in turns:
        current = current + 1 if turn.mode == "silence" else 0
        longest = max(longest, current)
    return longest


async def run_scenario(
    persona: Persona,
    scenario: Scenario,
    settings: EngineSettings | None = None,
    *,
    relationship: float | None = None,
    under_18: bool | None = None,
    check_expect: bool = True,
    live: LLMClient | None = None,
    filter_mode: str | None = None,
    compose_mode: str | None = None,
) -> ScenarioReport:
    """シナリオを1つ実行する。`live` に実際の LLM を渡すと、推定と応答文も実際のものになる。"""
    settings = settings or EngineSettings()
    stores = InMemoryStores()
    stores.add_character(persona)
    scripted = ScenarioLLM()
    llm: LLMClient = live or scripted
    user_id = uuid.uuid4()
    manager: MemoryManager | None = None
    if scenario.memories or scenario.proactive or live is not None:
        # 記憶は、メモリ上に置く。シナリオの固定データを、中期記憶として入れる
        manager = MemoryManager(
            InMemoryMemoryStore(), llm, settings, log=stores, topic_store=InMemoryTopicStore()
        )
        await _seed_memories(manager, persona, scenario, user_id)
    engine = Engine(llm, stores, settings, manager)
    await stores.update_user_settings(
        user_id, {"is_under_18": scenario.under_18 if under_18 is None else under_18}
    )
    start = relationship if relationship is not None else scenario.relationship
    if start is not None:
        await stores.set_relationship(user_id, persona.character_id, start, "simulate", START)
    start_value = start if start is not None else persona.initial_relationship

    if scenario.previous_session_days_ago is not None:
        # 前回のセッションの日時を置く(このキャラクタとの、前回の会話)
        stores.previous_activity[(user_id, persona.character_id)] = START - timedelta(
            days=scenario.previous_session_days_ago
        )
    now = START
    turns: list[TurnReport] = []
    for number, step in enumerate(scenario.turns, start=1):
        if step.new_session:
            await stores.close_session(user_id, persona.character_id, "simulate", now)
            now += timedelta(days=1)
        else:
            now += timedelta(minutes=1)
        scripted.estimate = to_estimate(step.estimate)
        index = _rule_index(persona, step.fire_rule) if step.fire_rule else None
        scripted.rule_matches = {index: 0.9} if index is not None else {}
        scripted.match_details = {}
        for item in step.sheet:
            target = sheet_rule_index(persona, str(item["slot"]))
            if target is not None:
                fields = {k: v for k, v in item.items() if k != "slot"}
                fields.setdefault("relevance", 0.9)
                scripted.match_details[target] = RuleMatch.model_validate(fields)
                scripted.rule_matches[target] = scripted.match_details[target].relevance
        result = await engine.handle_turn(
            user_id,
            TurnInput(channel="cli", character_id=persona.character_id, text=step.text),
            now,
            filter_mode=filter_mode,
            seed=scenario.seed + number if scenario.seed is not None else None,
            compose_mode=compose_mode,
        )
        report_turn = _turn_report(number, step, result)
        if manager is not None and not result.state.get("restricted"):
            info = result.state.get("memory") or {}
            by_handle = {m["handle"]: m["id"] for m in info.get("retrieved", [])}
            report_turn.topic_offered = bool(info.get("topic_offered"))
            for handle in info.get("used", []):
                memory = await manager.store.get(user_id, persona.character_id, uuid.UUID(by_handle[handle]))
                if memory is not None:
                    report_turn.memory_used.append(memory.summary)
        if live is not None:
            report_turn.reply = " / ".join(
                getattr(a, "text", "・・・") for a in result.output.actions if a.type != "system_notice"
            )
        turns.append(report_turn)

    report = ScenarioReport(scenario.id, scenario.title, persona.character_id, start_value, turns)
    if scenario.proactive:
        await _run_proactive(report, engine, stores, scripted, persona, scenario, settings, user_id, now)
    _check_hard_rules(report, settings)
    # `live` では、推定が実際のモデルによるので、`mock` 用の期待(方針など)は検査しない
    if check_expect and live is None:
        for item in scenario.expect:
            await _check_expectation(report, item, persona, scenario, settings)
        for step, item in zip(scenario.proactive, report.proactive, strict=False):
            wanted = _expected_reason(step, persona, settings)
            if wanted is not None and item.reason != wanted:
                report.failures.append(f"自発発話 {item.local_time}: {wanted} のはずが {item.reason}")
    return report


def _turn_report(number: int, step: ScenarioTurn, result: Any) -> TurnReport:
    state = result.state
    estimate = result.estimation.estimate
    notices = [a.kind for a in result.output.actions if a.type == "system_notice"]
    if state.get("restricted"):
        return TurnReport(
            number, step.text, "restricted", [], None, None, None, None, [], None, None, 0.0, None, None, None,
            {}, [], notices, 0, False, 0.0,
        )  # fmt: skip
    plan, gate_info, relation = state["plan"], state["gate"], state["relationship"]
    companion = {k: v for k, v in state["companion"]["categories"].items() if v >= 0.05}
    return TurnReport(
        no=number,
        text=step.text,
        mode=gate_info["mode"],
        rule_hits=list(gate_info["rule_hits"]),
        score=gate_info["score"],
        strategy=plan["strategy"],
        tone=plan["tone"],
        length=plan["length"],
        avoid=list(plan["avoid"]),
        expression=plan["companion_expression"],
        relationship=relation["value"],
        relationship_delta=relation["delta"],
        sync_weight=state["sync_weight"],
        talkativeness_eff=state["effective"]["talkativeness"],
        expressiveness_eff=state["effective"]["expressiveness"],
        companion=companion,
        fired_rules=[r["topic"] for r in state["companion"]["rules"]],
        events=list(state["companion"].get("events", [])),
        event_effects=dict(state["companion"].get("event_effects", {})),
        sheet=list(state["companion"].get("sheet", [])),
        sheet_effects=dict(state["companion"].get("sheet_effects", {})),
        notices=notices,
        stage=state["persona_request"]["stage"],
        crisis="crisis_signal" in state["safety_flags"],
        user_anger=estimate.categories.anger,
    )


def _local_moment(at: str) -> datetime:
    """「1d13:00」= 開始日から1日後の、ユーザーのローカル時刻(Asia/Tokyo)の 13:00。"""
    days, clock = at.split("d", 1)
    hour, minute = (int(x) for x in clock.split(":"))
    local_start = START.astimezone(ZoneInfo("Asia/Tokyo"))
    moment = (local_start + timedelta(days=int(days))).replace(hour=hour, minute=minute, second=0, microsecond=0)
    return moment.astimezone(UTC)


async def _run_proactive(
    report: ScenarioReport,
    engine: Engine,
    stores: InMemoryStores,
    scripted: ScenarioLLM,
    persona: Persona,
    scenario: Scenario,
    settings: EngineSettings,
    user_id: uuid.UUID,
    last_turn_at: datetime,
) -> None:
    """会話のあと、時刻ごとに自発発話を判定する。ユーザーは、話しかけることを許可している。"""
    await stores.update_user_settings(user_id, {"proactive_enabled": True})
    # 会話は、無入力で終わっている(本番では、30分でセッションを閉じる)
    await stores.close_session(user_id, persona.character_id, "simulate", last_turn_at)
    for index, step in enumerate(scenario.proactive):
        at = str(step["at"])
        now = _local_moment(at)
        if now <= last_turn_at:
            report.failures.append(f"自発発話の確認 {at} は、会話より前の時刻です")
            continue
        if step.get("close"):
            stores.expire_session(user_id, persona.character_id)  # 返事を待つ期限が過ぎた
        seed = (scenario.seed or 0) + 1000 + index
        result = await engine.proactive_turn(user_id, persona.character_id, consented=True, now=now, seed=seed)
        item = ProactiveReport(
            at, now.astimezone(ZoneInfo("Asia/Tokyo")).strftime("%m/%d %H:%M"), result.reason, result.topic_kind,
            result.text,
        )  # fmt: skip
        report.proactive.append(item)
        if result.sent and step.get("reply"):
            # 返事は、同じセッションの続き(30分後)
            scripted.estimate = to_estimate({})
            scripted.rule_matches, scripted.match_details = {}, {}
            reply_at = now + timedelta(minutes=30)
            await engine.handle_turn(
                user_id, TurnInput(channel="cli", character_id=persona.character_id, text=str(step["reply"])), reply_at
            )
            item.replied = True
            last_turn_at = reply_at
            await stores.close_session(user_id, persona.character_id, "simulate", reply_at)  # 返事のあと、会話は終わる


def _expected_reason(step: dict[str, Any], persona: Persona, settings: EngineSettings) -> str | None:
    expect = step.get("expect")
    if isinstance(expect, dict):
        initiative = traits_of(persona).initiative  # エンジンと同じ基準値(無い項目は既定値で補う)
        return expect.get("high" if initiative >= settings.topic_initiative_threshold else "low")
    return str(expect) if expect is not None else None


def _check_proactive(report: ScenarioReport, settings: EngineSettings) -> None:
    """自発発話の必須の規則(静穏時間帯に話さない、返事なしに続けない、罪悪感・催促・引き留めを言わない)。"""
    waiting = False
    for item in report.proactive:
        if item.reason != "ok":
            continue
        moment = _local_moment(item.at)
        if proactive_policy.in_quiet_hours(moment, "Asia/Tokyo", settings.proactive_quiet_start, settings.proactive_quiet_end):
            report.violations.append(f"自発発話: {item.local_time} は静穏時間帯なのに、話しかけた")
        if waiting:
            report.violations.append(f"自発発話: {item.local_time} に、返事のないまま続けて話しかけた")
        if item.text and screening.has_proactive_pressure(item.text):
            report.violations.append(f"自発発話: {item.local_time} の文面に、罪悪感・催促・引き留めの表現がある")
        waiting = not item.replied


def _check_hard_rules(report: ScenarioReport, settings: EngineSettings) -> None:
    """R1〜R3 の違反がないことを確認する(全シナリオで必須。§9.5)。自発発話の必須の規則も確かめる(M8)。"""
    _check_proactive(report, settings)
    if max_consecutive_silence(report.turns) > 2:
        report.violations.append("R1: 無言が3回以上連続した")
    for turn in report.turns:
        if turn.mode != "silence":
            continue
        if turn.user_anger >= settings.anger_no_silence_threshold:
            report.violations.append(f"R2: ターン{turn.no} で、ユーザーの怒りが閾値以上なのに無言になった")
        if turn.crisis:
            report.violations.append(f"R3: ターン{turn.no} で、危機の兆候があるのに無言になった")


async def _check_expectation(
    report: ScenarioReport,
    item: dict[str, Any],
    persona: Persona,
    scenario: Scenario,
    settings: EngineSettings,
) -> None:
    fail = report.failures.append
    # そのペルソナが、シートのその欄を選んでいないときは、この期待を検査しない
    slots = item.get("requires_slot")
    for slot in [slots] if isinstance(slots, str) else list(slots or []):
        if sheet_rule_index(persona, slot) is None:
            return
    targets = report.turns
    label = "全ターン"
    if "turn" in item:
        number = int(item["turn"])
        if not 1 <= number <= len(report.turns):
            fail(f"ターン{number} がありません")
            return
        targets, label = [report.turns[number - 1]], f"ターン{number}"

    for turn in targets:
        if turn.mode in item.get("response_mode_not", []):
            fail(f"{label}: 応答モードが {turn.mode} になった")
        if "response_mode_in" in item and turn.mode not in item["response_mode_in"]:
            fail(f"{label}: 応答モードが {turn.mode}(期待: {item['response_mode_in']})")
        for key in ("strategy", "tone", "length", "stage"):
            if key in item and getattr(turn, key) != item[key]:
                fail(f"{label}: {key} が {getattr(turn, key)}(期待: {item[key]})")
        if "avoid_includes" in item and item["avoid_includes"] not in turn.avoid:
            fail(f"{label}: 避けることに「{item['avoid_includes']}」がない")
        if "rule_hit" in item and item["rule_hit"] not in turn.rule_hits:
            fail(f"{label}: ハードルール {item['rule_hit']} に該当していない")
        if "notice" in item and item["notice"] not in turn.notices:
            fail(f"{label}: システム通知 {item['notice']} が出ていない")
        fired = {e["trigger"] for e in turn.events if "delta" in e}
        stopped = {e["suppressed_by"] for e in turn.events if "suppressed_by" in e}
        if "event_fired" in item and item["event_fired"] not in fired and _has_rule(persona, item["event_fired"]):
            fail(f"{label}: イベントルール({item['event_fired']})が発火していない")
        if "event_not_fired" in item and item["event_not_fired"] in fired:
            fail(f"{label}: イベントルール({item['event_not_fired']})が発火した")
        if item.get("no_event") and fired:
            fail(f"{label}: イベントルールが発火した: {sorted(fired)}")
        if "suppressed_by" in item and turn.events and item["suppressed_by"] not in stopped and not fired:
            fail(f"{label}: 抑止の理由 {item['suppressed_by']} が記録されていない")
        if item.get("event_positive_only") and any(
            (v < 0 if k in ("joy", "trust", "anticipation", "surprise") else v > 0)
            for k, v in turn.event_effects.items() if k in ("joy", "trust", "anticipation", "surprise", "fear", "sadness", "disgust", "anger")
        ):
            fail(f"{label}: イベントルールが、負の方向に動かした: {turn.event_effects}")
        if "event_step_max" in item and any(abs(v) > item["event_step_max"] + 1e-6 for v in turn.event_effects.values()):
            fail(f"{label}: イベント項が、1ターンの上限を超えた: {turn.event_effects}")
        sheet_fired = {h["rule_id"] for h in turn.sheet if "delta" in h}
        sheet_stopped = {(h["rule_id"], h["suppressed_by"]) for h in turn.sheet if "suppressed_by" in h}
        if "sheet_fired" in item and sheet_rule_id(item["sheet_fired"]) not in sheet_fired:
            fail(f"{label}: シートのルール({item['sheet_fired']})が発火していない")
        if "sheet_not_fired" in item and sheet_rule_id(item["sheet_not_fired"]) in sheet_fired:
            fail(f"{label}: シートのルール({item['sheet_not_fired']})が発火した")
        if "sheet_suppressed" in item:
            slot, reason = item["sheet_suppressed"]
            if (sheet_rule_id(slot), reason) not in sheet_stopped:
                fail(f"{label}: シートのルール({slot})が、{reason} で止まっていない")
        if item.get("no_sheet") and sheet_fired:
            fail(f"{label}: シートのルールが発火した: {sorted(sheet_fired)}")
        if item.get("sheet_avoid_flag") and not any(h.get("avoid_flag") for h in turn.sheet):
            fail(f"{label}: 回避フラグが立っていない")
        if "sheet_step_max" in item and any(v > item["sheet_step_max"] + 1e-6 for v in turn.sheet_effects.values()):
            fail(f"{label}: シート由来の項が、1ターンの上限を超えた: {turn.sheet_effects}")
        if item.get("sheet_positive_only") and any(
            k in ("fear", "sadness", "disgust", "anger") or v < 0 for k, v in turn.sheet_effects.items()
        ):
            fail(f"{label}: 好きの反応が、負の方向に動かした: {turn.sheet_effects}")
        if "strategy_not" in item and turn.strategy == item["strategy_not"]:
            fail(f"{label}: 方針が {turn.strategy} になった")
        if "memory_used_includes" in item and not any(item["memory_used_includes"] in m for m in turn.memory_used):
            fail(f"{label}: 記憶「{item['memory_used_includes']}」が使われていない")
        if item.get("no_notice") and turn.notices:
            fail(f"ターン{turn.no}: システム通知 {turn.notices} が出た")

    if "max_consecutive_silence" in item and max_consecutive_silence(report.turns) > item["max_consecutive_silence"]:
        fail("連続した無言が、上限を超えた")
    if item.get("event_decays"):
        # 同じルールが続けて発火すると、変化量が小さくなっていく(逓減)
        steps = [max((abs(v) for v in t.event_effects.values()), default=0.0) for t in report.turns if t.event_effects]
        if len(steps) >= 2 and steps[1] > steps[0] + 1e-6:
            fail(f"イベント項が、逓減していない: {steps}")
    if "sheet_fired_times" in item:
        slot, expected = item["sheet_fired_times"]
        rule_id = sheet_rule_id(slot)
        count = sum(1 for t in report.turns for h in t.sheet if "delta" in h and h["rule_id"] == rule_id)
        if count != expected:
            fail(f"シートのルール({slot})の発火が {count}回(期待: {expected}回)")
    if "sheet_fired_max" in item:
        slot, limit = item["sheet_fired_max"]
        rule_id = sheet_rule_id(slot)
        count = sum(1 for t in report.turns for h in t.sheet if "delta" in h and h["rule_id"] == rule_id)
        if count > limit:
            fail(f"シートのルール({slot})の発火が {count}回(上限: {limit}回)")
    if "sheet_rise_max" in item:
        slot, limit = item["sheet_rise_max"]
        rule_id = sheet_rule_id(slot)
        totals: dict[str, float] = {}
        for t in report.turns:
            for h in t.sheet:
                if h["rule_id"] == rule_id:
                    for name, value in h.get("delta", {}).items():
                        totals[name] = totals.get(name, 0.0) + value
        if any(value > limit + 1e-6 for value in totals.values()):
            fail(f"シートのルール({slot})の累計が、上限 {limit} を超えた: {totals}")
    if item.get("sheet_decays"):
        steps = [max(t.sheet_effects.values(), default=0.0) for t in report.turns if t.sheet_effects]
        if len(steps) >= 2 and steps[1] > steps[0] + 1e-6:
            fail(f"シート由来の項が、逓減していない: {steps}")
    if "max_steer_away" in item:
        count = sum(1 for t in report.turns if t.strategy == "steer_away")
        if count > item["max_steer_away"]:
            fail(f"話題を変えた回数が {count}回(上限: {item['max_steer_away']}回)")
    if "steer_away_times" in item:
        count = sum(1 for t in report.turns if t.strategy == "steer_away")
        if count != item["steer_away_times"]:
            fail(f"話題を変えた回数が {count}回(期待: {item['steer_away_times']}回)")
    if "event_fired_times" in item:
        trigger, expected = item["event_fired_times"]
        count = sum(1 for t in report.turns for e in t.events if "delta" in e and e["trigger"] == trigger)
        if _has_rule(persona, trigger) and count != expected:
            fail(f"イベントルール({trigger})の発火が {count}回(期待: {expected}回)")
    offers = [t for t in report.turns if t.topic_offered]
    initiative = float(persona.traits.get("initiative", 0.0))
    if "max_topic_offers" in item and len(offers) > item["max_topic_offers"]:
        fail(f"話題を振った回数が {len(offers)}回(上限 {item['max_topic_offers']}回)")
    if item.get("topic_offers_follow_initiative"):
        # initiative がしきい値以上のキャラクタは話題を振り、未満のキャラクタは振らない(§8.7)
        high = initiative >= settings.topic_initiative_threshold
        if high and not offers:
            fail("initiative がしきい値以上なのに、話題を振らなかった")
        if not high and offers:
            fail("initiative がしきい値未満なのに、話題を振った")
    for text in item.get("topic_never_includes", []):
        if any(text in m for t in offers for m in t.memory_used):
            fail(f"持ち出さないはずの記憶「{text}」を、話題にした")
    if "no_topic_on_turns" in item:
        for number in item["no_topic_on_turns"]:
            if 1 <= number <= len(report.turns) and report.turns[number - 1].topic_offered:
                fail(f"ターン{number}: 話題を振らないはずの場面で、振った")
    values = [t.relationship for t in report.turns if t.relationship is not None]
    if "relationship" in item and values:
        change = values[-1] - report.relationship_start
        if (item["relationship"] == "up" and change <= 0) or (item["relationship"] == "down" and change >= 0):
            fail(f"関係値の変化が {change:+.2f}(期待: {item['relationship']})")
    if item.get("relationship_not") == "down" and values and values[-1] < report.relationship_start - 1e-9:
        fail(f"関係値が下がった: {values[-1] - report.relationship_start:+.2f}")
    step_limit = item.get("max_relationship_step")
    if step_limit is not None and any(abs(t.relationship_delta) > step_limit + 1e-6 for t in report.turns):
        fail("関係値が、1ターンの上限を超えて変化した")
    if "silence_not_above_relationship" in item:
        # 同じシナリオを、指定の関係値で実行して比べる(関係値が低いことを理由に、無言を増やさない。§13.2)
        other = await run_scenario(
            persona, scenario, settings, relationship=float(item["silence_not_above_relationship"]), check_expect=False
        )
        if report.mode_counts()["silence"] > other.mode_counts()["silence"]:
            fail("関係値が低いほうが、無言が多くなった")


def _has_rule(persona: Persona, trigger: str) -> bool:
    """そのペルソナが、指定のトリガーのイベントルールを持っているか(持たないペルソナでは、発火を期待しない)。"""
    return any((rule.get("trigger") or {}).get("name") == trigger for rule in persona.event_rules)


async def run_scenarios(
    persona: Persona,
    scenarios: list[Scenario],
    settings: EngineSettings | None = None,
    *,
    relationship: float | None = None,
    under_18: bool | None = None,
    live: LLMClient | None = None,
    filter_mode: str | None = None,
    compose_mode: str | None = None,
) -> list[ScenarioReport]:
    return [
        await run_scenario(
            persona, s, settings, relationship=relationship, under_18=under_18, live=live,
            filter_mode=filter_mode, compose_mode=compose_mode,
        )
        for s in scenarios
    ]


async def _seed_memories(manager: MemoryManager, persona: Persona, scenario: Scenario, user_id: uuid.UUID) -> None:
    """シナリオの記憶の固定データを、中期記憶として入れる。嗜好度は、指定がなければキャラクタの嗜好から計算する。"""
    for raw in scenario.memories:
        summary, topics = str(raw["summary"]), [str(t) for t in raw.get("topics") or []]
        ((vector, preference),) = await manager._embed_with_preference(persona, [summary], [topics])
        await manager.store.add(
            user_id,
            persona.character_id,
            Memory(
                memory_id=uuid.uuid4(),
                tier=str(raw.get("tier", "medium")),
                kind=str(raw.get("kind", "episodic")),
                summary=summary,
                topics=topics,
                importance=float(raw.get("importance", 0.5)),
                emotion_vad={"valence": float(raw.get("valence", 0.3)), "arousal": 0.0, "dominance": 0.0},
                preference=float(raw["preference"]) if "preference" in raw else preference,
                muted=bool(raw.get("muted", False)),
                sensitivity=raw.get("sensitivity"),
                source_day=(START - timedelta(days=10)).date(),
                created_at=START - timedelta(days=10),
                embedding=vector,
            ),
        )


async def compare_type_drafts(llm: LLMClient, text: str) -> list[tuple[str, str, str]]:
    """同じ入力に対する、16タイプの下書きを並べる(文体カードの差を、人が確認するため。§9.6)。"""
    rows = []
    for item in ptypes.all_types():
        persona = persona_for_type(item.type_id, None)
        context = DraftContext(style_card=item.style_card, length="短め(1〜3文が目安)。")
        result = await llm.draft(persona, [("user", text)], context)
        rows.append((item.type_id, item.name, result.text))
    return rows


# ---------------------------------------------------------------------------
# 集計と、性格タイプの走査(§9.6)
# ---------------------------------------------------------------------------


def mode_distribution(reports: list[ScenarioReport]) -> dict[str, float]:
    """応答モードの分布(割合)。通知だけのターンは数えない。"""
    counts = dict.fromkeys(MODES, 0)
    for report in reports:
        for mode, count in report.mode_counts().items():
            counts[mode] += count
    total = sum(counts.values()) or 1
    return {mode: round(count / total, 3) for mode, count in counts.items()}


@dataclass
class SweepRow:
    type_id: str
    profile: str | None
    traits: dict[str, float]
    modes: dict[str, float]
    lengths: dict[str, float]
    violations: list[str]
    failures: list[str]
    in_range: bool


async def sweep_types(settings: EngineSettings | None = None) -> list[SweepRow]:
    """16タイプ × 表出プロファイル(なし + 4プリセット)の全80通りを、標準シナリオで `mock` 実行する。"""
    scenarios = load_scenarios()
    rows: list[SweepRow] = []
    for item in ptypes.all_types():
        for profile in SWEEP_PROFILES:
            persona = persona_for_type(item.type_id, profile)
            reports = await run_scenarios(persona, scenarios, settings)
            traits = {k: v for k, v in persona.traits.items() if isinstance(v, float)}
            lengths = [t.length for r in reports for t in r.turns if t.length]
            rows.append(
                SweepRow(
                    type_id=item.type_id,
                    profile=profile,
                    traits=traits,
                    modes=mode_distribution(reports),
                    lengths={n: round(lengths.count(n) / (len(lengths) or 1), 3) for n in ("short", "medium", "long")},
                    violations=[f"{r.scenario_id}: {v}" for r in reports for v in r.violations],
                    failures=[f"{r.scenario_id}: {f}" for r in reports for f in r.failures],
                    in_range=all(0.0 <= v <= 1.0 for v in traits.values()),
                )
            )
    return rows


# ---------------------------------------------------------------------------
# レポート(Markdown)
# ---------------------------------------------------------------------------


def render_report(report: ScenarioReport) -> list[str]:
    status = "合格" if report.ok else "不合格"
    lines = [
        f"## {report.scenario_id} {report.title} — {status}",
        f"開始時の関係値 {report.relationship_start:+.1f}({rel.band(report.relationship_start).label})",
        "",
        "| # | 発話 | モード | 規則 | スコア | 方針 | 長さ | 表出 | 関係値 | 同期 | キャラクタの感情 | 通知 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for t in report.turns:
        text = t.text if len(t.text) <= 18 else t.text[:17] + "…"
        if t.mode == "restricted":
            lines.append(f"| {t.no} | {text} | (制限中) | | | | | | | | | {','.join(t.notices)} |")
            continue
        expression = f"{t.expression['emotion']} {t.expression['level']:.2f}" if t.expression else "-"
        companion = " ".join(f"{k}={v:.2f}" for k, v in sorted(t.companion.items(), key=lambda kv: -kv[1])[:2]) or "-"
        relation = f"{t.relationship:+.2f}({t.relationship_delta:+.2f})"
        lines.append(
            f"| {t.no} | {text} | {t.mode} | {','.join(t.rule_hits) or '-'} | {t.score:+.2f} | {t.strategy}/{t.tone} "
            f"| {t.length} | {expression} | {relation} | {t.sync_weight:.2f} | {companion} "
            f"| {','.join(t.notices) or '-'} |"
        )
        if t.events:
            fired = [f"{e['rule_id']}" for e in t.events if "delta" in e]
            stopped = [f"{e['rule_id']}({e['suppressed_by']})" for e in t.events if "suppressed_by" in e]
            effects = " ".join(f"{k} {v:+.2f}" for k, v in t.event_effects.items())
            detail = " / ".join(x for x in (", ".join(fired), effects, "止めた: " + ", ".join(stopped) if stopped else "") if x)
            lines.append(f"|   | ↳ 出来事への反応: {detail} | | | | | | | | | | |")
        if t.sheet:
            fired = [h["catalog_id"] + ("(回避)" if h.get("avoid_flag") else "") for h in t.sheet if "delta" in h]
            stopped = [f"{h['catalog_id']}({h['suppressed_by']})" for h in t.sheet if "suppressed_by" in h]
            effects = " ".join(f"{k} {v:+.2f}" for k, v in t.sheet_effects.items())
            detail = " / ".join(x for x in (", ".join(fired), effects, "止めた: " + ", ".join(stopped) if stopped else "") if x)
            lines.append(f"|   | ↳ 好き嫌いへの反応: {detail} | | | | | | | | | | |")
        if t.topic_offered or t.memory_used:
            label = "話題を振った" if t.topic_offered else "使った記憶"
            lines.append(f"|   | ↳ {label}: {' / '.join(t.memory_used) or '-'} | | | | | | | | | | |")
        if t.reply:
            lines.append(f"|   | ↳ 応答: {t.reply} | | | | | | | | | | |")
    if report.proactive:
        lines += ["", "| 自発発話の確認 | 結果 | 話題 | 文面 |", "|---|---|---|---|"]
        for item in report.proactive:
            text = (item.text or "-") + (" → 返事あり" if item.replied else "")
            lines.append(f"| {item.local_time} | {item.reason} | {item.topic_kind or '-'} | {text} |")
    for message in report.violations:
        lines.append(f"- **違反** {message}")
    for message in report.failures:
        lines.append(f"- 期待と違う: {message}")
    lines.append("")
    return lines


def render_sweep(rows: list[SweepRow]) -> list[str]:
    lines = [
        "| タイプ | プロファイル | 内向 | 口数 | 表出 | full | brief | 相づち | 無言 | 長さ short/medium/long | 違反 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        t, m, n = row.traits, row.modes, row.lengths
        problems = len(row.violations) + len(row.failures) + (0 if row.in_range else 1)
        lines.append(
            f"| {row.type_id} | {row.profile or '-'} | {t['introversion']:.2f} | {t['talkativeness']:.2f} "
            f"| {t['expressiveness']:.2f} | {m['full']:.2f} | {m['brief']:.2f} | {m['backchannel']:.2f} "
            f"| {m['silence']:.2f} | {n['short']:.2f}/{n['medium']:.2f}/{n['long']:.2f} | {problems or '-'} |"
        )
    return lines
