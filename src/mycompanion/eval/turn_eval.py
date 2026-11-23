"""1ターンの遅延と、感情フィルタの働きの測定(本体仕様書 §15.1 の M6-6、付録F.4)。

下書き・感情フィルタのデプロイの組み合わせごとに、同じ入力を、サンプルのキャラクタ(付録C)でエンジンに通し、
段ごとの所要時間(感情推定、下書き、フィルタ、合計)と、フィルタが文面を変えた割合・作り直した回数を数える。
最終の文面は、モデル名を伏せた比較の資料に並べる(人が、キャラクタらしさと自然さで順位をつける)。

- 記憶は使わない(メモリ上の保存先。LLM の遅延だけを測る)。入力ごとに、別のユーザーとして1ターンだけ実行する。
- 入力は合成の文(実際のユーザーの発話ではない)。
"""

from __future__ import annotations

import random
import statistics
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mycompanion.channel.types import TurnInput
from mycompanion.config import EngineSettings
from mycompanion.engine.engine import Engine
from mycompanion.engine.stores import InMemoryStores
from mycompanion.llm.client import LLMClient, Persona

# 場面の違う入力(下書きの比較と同じものに、短い・相談・感謝などを足す)
TURN_INPUTS = [
    "ただいま。今日は一日バタバタだったよ。",
    "明日から旅行なんだ。すごく楽しみ!",
    "飼ってたハムスターが昨日死んじゃって、ずっと泣いてる。",
    "同僚に手柄を横取りされた。本当に腹が立つ。",
    "転職するか今の会社に残るか迷ってて。どう考えたらいいと思う?",
    "まあ、大丈夫です。慣れてるんで。",
    "最近ねこを飼い始めたんだ。",
    "なんか眠れなくてさ。",
    "この前話を聞いてくれてありがとう。少し楽になったよ。",
    "週末、何して過ごすのがいいかな?",
]


@dataclass
class TurnSample:
    persona: str
    input_index: int
    mode: str
    emotion_sec: float
    draft_sec: float
    filter_sec: float
    total_sec: float
    draft_text: str | None
    final_text: str | None
    changed: bool
    regenerated: bool
    flags: list[str] = field(default_factory=list)
    plan: dict[str, Any] = field(default_factory=dict)  # Planner の方針(フィルタを省ける場面の分析に使う)
    edit_summary: str | None = None


def load_personas(root: Path = Path("personas")) -> list[Persona]:
    from mycompanion.persona import io as pio
    from mycompanion.persona.simulate import persona_from_definition

    return [persona_from_definition(pio.load(pio.persona_path(cid, root))) for cid in pio.list_ids(root)]


async def run_combo(
    llm: LLMClient, personas: list[Persona], runs: int, inputs: list[str] | None = None, single: bool = False
) -> list[TurnSample]:
    """1つの組み合わせで、キャラクタ × 入力 × 回数 だけ実行する。`single` なら、1回で応答を作る動作。"""
    inputs = inputs or TURN_INPUTS
    stores = InMemoryStores()
    for persona in personas:
        stores.add_character(persona)
    engine = Engine(llm, stores, EngineSettings(compose_mode="single" if single else "two_stage"), None)
    samples: list[TurnSample] = []
    for _ in range(runs):
        for persona in personas:
            for index, text in enumerate(inputs):
                user = uuid.uuid4()  # 毎回、別のユーザー(会話の続きにしない)
                turn = TurnInput(channel="cli", character_id=persona.character_id, text=text)
                result = await engine.handle_turn(user, turn, datetime.now(UTC), filter_mode="both", use_memory=False)
                (record,) = stores.turns[(user, persona.character_id)]
                latency = record.latency_json
                final = record.filtered_text or record.draft_text
                samples.append(
                    TurnSample(
                        persona=persona.name, input_index=index, mode=record.response_mode,
                        emotion_sec=latency.get("emotion_sec", 0.0), draft_sec=latency.get("draft_sec", 0.0),
                        filter_sec=latency.get("filter_sec", 0.0), total_sec=latency.get("total_sec", 0.0),
                        draft_text=record.draft_text, final_text=final,
                        changed=bool(record.filtered_text) and record.filtered_text != record.draft_text,
                        regenerated="filter_regenerated" in record.safety_flags or "draft_regenerated" in record.safety_flags,
                        flags=list(record.safety_flags), plan=dict(record.plan_json or {}),
                        edit_summary=record.edit_summary,
                    )
                )  # fmt: skip
                del result
    return samples


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = min(len(ordered) - 1, round(q * (len(ordered) - 1)))
    return round(ordered[position], 2)


def summarize(samples: list[TurnSample]) -> dict[str, Any]:
    full = [s for s in samples if s.mode in ("full", "brief")]
    summary: dict[str, Any] = {
        "turns": len(samples),
        "modes": {m: sum(s.mode == m for s in samples) for m in sorted({s.mode for s in samples})},
        "with_reply": len(full),
    }
    for name in ("emotion_sec", "draft_sec", "filter_sec", "total_sec"):
        values = [getattr(s, name) for s in full]
        summary[name] = {"p50": _pct(values, 0.5), "p95": _pct(values, 0.95), "mean": round(statistics.fmean(values), 2) if values else 0.0}
    summary["filter_changed_rate"] = round(sum(s.changed for s in full) / len(full), 3) if full else 0.0
    summary["regenerated"] = sum(s.regenerated for s in full)
    summary["fallback"] = sum("fallback_response" in s.flags or "output_blocked" in s.flags for s in full)
    return summary


def comparison_sheet(results: dict[str, list[TurnSample]], seed: int = 0) -> tuple[str, str]:
    """最終の文面を、組み合わせの名前を伏せて並べた資料と、対応表。1回目の実行だけを使う。"""
    rng = random.Random(seed)
    combos = list(results)
    first: dict[str, dict[tuple[str, int], TurnSample]] = {}
    for combo, samples in results.items():
        first[combo] = {}
        for s in samples:
            first[combo].setdefault((s.persona, s.input_index), s)
    keys = sorted({k for table in first.values() for k in table})
    lines = ["# 応答の比較(組み合わせの名前は伏せてある)", "",
             "各入力について、キャラクタらしさと自然さで順位をつけてください(無言・相づちは「—」)。", ""]  # fmt: skip
    answers = ["# 対応表(評価が終わるまで見ない)", ""]
    for number, (persona, index) in enumerate(keys, start=1):
        order = combos[:]
        rng.shuffle(order)
        lines += [f"## {number}. {persona}", "", f"> {TURN_INPUTS[index]}", ""]
        for label, combo in zip("ABCDEFGH", order, strict=False):
            sample = first[combo].get((persona, index))
            text = (sample.final_text if sample else None) or "—"
            lines.append(f"- **{label}**: {text}")
            answers.append(f"- {number} {label}: {combo}")
        lines.append("")
    return "\n".join(lines), "\n".join(answers)


def render(summaries: dict[str, dict[str, Any]]) -> list[str]:
    lines = ["| 組み合わせ(下書き / フィルタ) | 応答あり | 推定 p50 | 下書き p50 / p95 | フィルタ p50 / p95 | 合計 p50 / p95 | フィルタが変えた割合 | 作り直し | 定型 |",
             "|---|---|---|---|---|---|---|---|---|"]  # fmt: skip
    for combo, s in summaries.items():
        lines.append(
            f"| {combo} | {s['with_reply']}/{s['turns']} | {s['emotion_sec']['p50']} | "
            f"{s['draft_sec']['p50']} / {s['draft_sec']['p95']} | {s['filter_sec']['p50']} / {s['filter_sec']['p95']} | "
            f"{s['total_sec']['p50']} / {s['total_sec']['p95']} | {s['filter_changed_rate']:.0%} | {s['regenerated']} | {s['fallback']} |"
        )
    return lines
