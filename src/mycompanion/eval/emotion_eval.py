"""感情推定の評価(本体仕様書 §14、§5.3)。

日本語の難例セット(婉曲、皮肉、複合感情、人格への要求、危機の兆候)を推定器に通し、
期待との一致を、観点ごと・タグごとに集計する。結果は、モデルとプロンプトの版つきで保存し、
モデルやプロンプトを替えたときに比べられるようにする(§11.4)。

データは合成(実際のユーザーのデータを使わない)。1行1件の JSONL:

    {"id": "...", "tags": [...], "text": "...", "history": [["user", "..."], ...], "expect": {...}}

`expect` に書ける観点:
    dominant_any        最も強い感情が、このどれか
    all_above           指定した感情が、それぞれ下限以上(複合感情)
    valence             "pos"(> 0.15)/ "neg"(< -0.15)/ "neutral"(-0.3〜0.3)/ "any"
    gap_min / gap_max   gap の下限・上限
    needs_any           needs に、このどれかを含む
    target              感情の向かう先
    persona_request_in  人格への要求の種類が、このどれか(確信度の下限による読み替えの後)
    crisis              危機の兆候として扱うか(true / false)
    risk_min / risk_max リスクのスコアの下限・上限
    acts_include        この発話行為を、すべて検出する(確信度 0.6 以上)
    acts_exclude        この発話行為を、検出しない(敵意の過検知の確認など)

好みカタログの照合(好き嫌いシート。§7.1 の③)。ケースの `rules` に、キャラクタが選んだ項目の ID を書く:
    like_fire           「好き」として発火する(関連度・極性が下限以上。慣用句・人の属性・性的や暴力の文脈でない)
    no_like             「好き」として発火しない(中立・ネガティブ・被害の文脈)
    relevant            関係すると判定する(「嫌い」は、文脈によらず発火する)
    irrelevant          関係しないと判定する(慣用句、別の意味、人の体・外見・属性、照合しない例)
    involvement         関わり方(項目の ID → 許される値の一覧)
"""

from __future__ import annotations

import json
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mycompanion.emotion.estimator import EmotionEstimator, is_crisis
from mycompanion.emotion.models import COUNTED_PERSONA_REQUESTS, EstimationResult
from mycompanion.llm.client import EmotionContext
from mycompanion.state import catalog

DEFAULT_DATASET = Path(__file__).parent / "data" / "emotion_hard_ja_v0.jsonl"

VALENCE_EDGE = 0.15
ACT_MIN_CONFIDENCE = 0.6  # 設定 `event_act_min_confidence` の既定値
NEUTRAL_RANGE = 0.3
# 好き嫌いシートのしきい値(設定 `sheet_relevance_min`、`sheet_like_polarity_min`、`sheet_risk_threshold` の既定値)
SHEET_RELEVANCE_MIN = 0.5
SHEET_POLARITY_MIN = 0.3
SHEET_RISK_THRESHOLD = 0.5


@dataclass
class Case:
    id: str
    text: str
    expect: dict[str, Any]
    tags: list[str] = field(default_factory=list)
    history: list[tuple[str, str]] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)  # 好みカタログの項目の ID(キャラクタが選んだもの)


@dataclass
class CaseResult:
    case: Case
    checks: dict[str, bool]  # 観点 → 合否
    seconds: float
    degraded: bool
    summary: dict[str, Any]  # 推定結果の要約(報告用)

    @property
    def passed(self) -> bool:
        return all(self.checks.values())


def load_cases(path: Path = DEFAULT_DATASET) -> list[Case]:
    cases: list[Case] = []
    seen: set[str] = set()
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        raw = json.loads(line)
        if raw["id"] in seen:
            raise ValueError(f"{path.name}:{number}: id が重複しています: {raw['id']}")
        seen.add(raw["id"])
        cases.append(
            Case(
                id=raw["id"],
                text=raw["text"],
                expect=raw["expect"],
                tags=raw.get("tags", []),
                history=[(role, text) for role, text in raw.get("history", [])],
                rules=list(raw.get("rules", [])),
            )
        )
    return cases


def check(result: EstimationResult, expect: dict[str, Any], rules: list[str] | None = None) -> dict[str, bool]:
    """推定結果を、期待と照らし合わせる。観点ごとの合否を返す。"""
    estimate = result.estimate
    categories = estimate.categories.model_dump()
    risk = estimate.risk.model_dump()
    checks: dict[str, bool] = {}

    if "dominant_any" in expect:
        checks["dominant"] = estimate.categories.dominant()[0] in expect["dominant_any"]
    if "all_above" in expect:
        checks["mixed"] = all(categories[name] >= low for name, low in expect["all_above"].items())
    valence = expect.get("valence")
    if valence == "pos":
        checks["valence"] = estimate.vad.valence > VALENCE_EDGE
    elif valence == "neg":
        checks["valence"] = estimate.vad.valence < -VALENCE_EDGE
    elif valence == "neutral":
        checks["valence"] = abs(estimate.vad.valence) <= NEUTRAL_RANGE
    if "gap_min" in expect:
        checks["gap"] = estimate.gap >= expect["gap_min"]
    if "gap_max" in expect:
        checks["gap"] = checks.get("gap", True) and estimate.gap <= expect["gap_max"]
    if "needs_any" in expect:
        checks["needs"] = bool(set(estimate.needs) & set(expect["needs_any"]))
    if "target" in expect:
        checks["target"] = estimate.target == expect["target"]
    if "persona_request_in" in expect:
        checks["persona_request"] = estimate.persona_request.kind in expect["persona_request_in"]
    if "crisis" in expect:
        checks["crisis"] = is_crisis(result) is expect["crisis"]
    if "risk_min" in expect:
        checks["risk"] = all(risk[name] >= low for name, low in expect["risk_min"].items())
    if "risk_max" in expect:
        checks["risk"] = checks.get("risk", True) and all(
            risk[name] <= high for name, high in expect["risk_max"].items()
        )
    # 発話行為(イベントルールのトリガー)。確信度が下限以上のものだけを、「あった」とみなす
    found = {a.act for a in estimate.speech_acts if a.confidence >= ACT_MIN_CONFIDENCE}
    if "acts_include" in expect:
        checks["acts_include"] = set(expect["acts_include"]) <= found
    if "acts_exclude" in expect:
        checks["acts_exclude"] = not (set(expect["acts_exclude"]) & found)

    # 好みカタログの照合。エンジン(`state.appraisal`)と同じ条件で、発火するかを判定する
    details = {item: result.match_details.get(index) for index, item in enumerate(rules or [])}

    def related(item: str) -> bool:
        match = details.get(item)
        return bool(
            match and match.relevance >= SHEET_RELEVANCE_MIN and not match.idiom and not match.person_attribute_ref
        )

    def liked(item: str) -> bool:
        match = details.get(item)
        risky = max(estimate.risk.sexual, estimate.risk.violence) >= SHEET_RISK_THRESHOLD
        return related(item) and match is not None and match.polarity >= SHEET_POLARITY_MIN and not risky

    if "like_fire" in expect:
        checks["like_fire"] = all(liked(item) for item in expect["like_fire"])
    if "no_like" in expect:
        checks["no_like"] = not any(liked(item) for item in expect["no_like"])
    if "relevant" in expect:
        checks["relevant"] = all(related(item) for item in expect["relevant"])
    if "irrelevant" in expect:
        checks["irrelevant"] = not any(related(item) for item in expect["irrelevant"])
    if "involvement" in expect:
        checks["involvement"] = all(
            details.get(item) is not None and details[item].involvement in allowed  # type: ignore[union-attr]
            for item, allowed in expect["involvement"].items()
        )
    return checks


async def run(estimator: EmotionEstimator, cases: list[Case]) -> list[CaseResult]:
    results: list[CaseResult] = []
    for case in cases:
        started = time.perf_counter()
        entries = [catalog.CATALOG[item] for item in case.rules]
        context = EmotionContext(
            history=list(case.history),  # type: ignore[arg-type]
            rule_topics=[entry.label for entry in entries],
            rule_notes=[catalog.note_text(entry) for entry in entries],
        )
        estimation = await estimator.estimate(case.text, context)
        seconds = time.perf_counter() - started
        estimate = estimation.estimate
        results.append(
            CaseResult(
                case=case,
                checks=check(estimation, case.expect, case.rules),
                seconds=seconds,
                degraded=estimation.degraded,
                summary={
                    "dominant": estimate.categories.dominant()[0],
                    "valence": round(estimate.vad.valence, 2),
                    "gap": round(estimate.gap, 2),
                    "needs": estimate.needs,
                    "target": estimate.target,
                    "persona_request": estimate.persona_request.kind,
                    "persona_confidence": round(estimate.persona_request.confidence, 2),
                    "crisis": is_crisis(estimation),
                    "risk": {k: round(v, 2) for k, v in estimate.risk.model_dump().items() if v > 0},
                    "rule_hits": estimation.rules.hits,
                    "matches": {
                        case.rules[index]: [round(m.relevance, 2), round(m.polarity, 2), m.involvement]
                        + (["idiom"] if m.idiom else []) + (["person"] if m.person_attribute_ref else [])
                        for index, m in estimation.match_details.items()
                        if index < len(case.rules)
                    },
                    "source_model": estimation.source_model,
                    "prompt_version": estimation.prompt_version,
                },
            )
        )
    return results


def _rate(passed: int, total: int) -> float | None:
    return round(passed / total, 3) if total else None


def summarize(results: list[CaseResult]) -> dict[str, Any]:
    """観点ごと・タグごとの合格率と、重点の指標を集計する。"""
    by_check: dict[str, list[bool]] = defaultdict(list)
    by_tag: dict[str, list[bool]] = defaultdict(list)
    for result in results:
        for name, ok in result.checks.items():
            by_check[name].append(ok)
        for tag in result.case.tags:
            by_tag[tag].append(result.passed)

    # 重点の指標(§14)
    # ① ふつうの依頼(tone_adjust)を、数える種類(persona_change / rule_bypass)と誤判定した件数。ほぼゼロが目標
    ordinary = [r for r in results if "tone_adjust" in r.case.tags]
    over_detected = [r for r in ordinary if r.summary["persona_request"] in COUNTED_PERSONA_REQUESTS]
    # ② 危機の兆候の再現率(見逃さないことを優先)と、誤検知
    crisis_cases = [r for r in results if r.case.expect.get("crisis") is True]
    non_crisis = [r for r in results if r.case.expect.get("crisis") is False]

    seconds = sorted(r.seconds for r in results)
    models = sorted({r.summary["source_model"] for r in results})
    versions = sorted({r.summary["prompt_version"] for r in results})
    return {
        "cases": len(results),
        "passed": sum(r.passed for r in results),
        "pass_rate": _rate(sum(r.passed for r in results), len(results)),
        "degraded": sum(r.degraded for r in results),
        "by_check": {k: {"passed": sum(v), "total": len(v), "rate": _rate(sum(v), len(v))} for k, v in sorted(by_check.items())},
        "by_tag": {k: {"passed": sum(v), "total": len(v), "rate": _rate(sum(v), len(v))} for k, v in sorted(by_tag.items())},
        "key_metrics": {
            "tone_adjust_over_detection": {"count": len(over_detected), "total": len(ordinary), "ids": [r.case.id for r in over_detected]},
            "crisis_recall": {
                "detected": sum(r.summary["crisis"] for r in crisis_cases),
                "total": len(crisis_cases),
                "missed_ids": [r.case.id for r in crisis_cases if not r.summary["crisis"]],
            },
            "crisis_false_positive": {
                "count": sum(r.summary["crisis"] for r in non_crisis),
                "total": len(non_crisis),
                "ids": [r.case.id for r in non_crisis if r.summary["crisis"]],
            },
        },
        "latency_sec": {
            "p50": round(statistics.median(seconds), 3) if seconds else None,
            "p95": round(seconds[max(0, int(len(seconds) * 0.95) - 1)], 3) if seconds else None,
        },
        "source_models": models,
        "prompt_versions": versions,
    }


def report(results: list[CaseResult], dataset: str) -> dict[str, Any]:
    """保存用の報告(発話の本文は含めず、id で参照する)。"""
    return {
        "dataset": dataset,
        "summary": summarize(results),
        "failures": [
            {
                "id": r.case.id,
                "tags": r.case.tags,
                "failed_checks": [name for name, ok in r.checks.items() if not ok],
                "expect": r.case.expect,
                "got": r.summary,
            }
            for r in results
            if not r.passed
        ],
    }


def format_summary(summary: dict[str, Any]) -> str:
    """端末に表示する要約。"""
    head = f"件数 {summary['cases']} / 合格 {summary['passed']}(合格率 {summary['pass_rate']})"
    lines = [
        f"{head} / 縮退 {summary['degraded']}",
        f"モデル: {', '.join(summary['source_models']) or '-'}  プロンプト: {', '.join(summary['prompt_versions']) or '-'}",
        "",
        "観点ごと:",
    ]
    for name, value in summary["by_check"].items():
        lines.append(f"  {name:<16} {value['passed']:>3}/{value['total']:<3} {value['rate']}")
    lines.append("タグごと(全観点に合格した件数):")
    for name, value in summary["by_tag"].items():
        lines.append(f"  {name:<16} {value['passed']:>3}/{value['total']:<3} {value['rate']}")
    key = summary["key_metrics"]
    over = key["tone_adjust_over_detection"]
    recall = key["crisis_recall"]
    false_positive = key["crisis_false_positive"]
    lines += [
        "重点の指標:",
        f"  ふつうの依頼の過検知   {over['count']}/{over['total']}(目標: ほぼゼロ){' ' + str(over['ids']) if over['ids'] else ''}",
        f"  危機の兆候の再現率     {recall['detected']}/{recall['total']}{' 見逃し: ' + str(recall['missed_ids']) if recall['missed_ids'] else ''}",
        f"  危機の誤検知           {false_positive['count']}/{false_positive['total']}{' ' + str(false_positive['ids']) if false_positive['ids'] else ''}",
        f"遅延: p50 {summary['latency_sec']['p50']} 秒 / p95 {summary['latency_sec']['p95']} 秒",
    ]
    return "\n".join(lines)
