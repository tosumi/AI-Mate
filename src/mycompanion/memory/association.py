"""関係をたどる連想(本体仕様書 §9.13 の第3段階。`topic-timeline-memory.md` §18)。

会話の**焦点**(いま話しているトピック)から、トピック同士の関係をたどって、近いトピックほど強く「思い浮かぶ」
ようにする(活性化の拡散。2段まで)。話題が出尽くしかけたとき、キャラクタが「〜といえば」と1つ橋渡しして、
次の話題へ移れる。

- **連想のしやすさ**(どれだけ遠く・広く思い浮かぶか)と、**話題の移りやすさ**(どれだけ早く・頻繁に移るか)は、
  キャラクタの性格タイプの軸から決まる。一律にしない。抽象・柔軟・外向の側ほど、広く速く移る。
  共感・論理の軸は、量ではなく、**どの関係をたどるか**(人へ / 順序へ)を変える。
- ユーザー(会話の相手)の性格は、使わない。量と向きは、キャラクタの性格タイプ・嗜好度だけで決める。
- たどるのは、ユーザーが明言した関係と、そこから決まる「同じ親の子どうし」だけ。似ているだけの話題へは、移らない。
- 除外するトピックは、終点にも、**経由点にも**しない(橋渡しの言葉で、経由したものが表に出るため)。
- 追加の LLM の呼び出しはない(関係のたどり方とスコアは、コードで決める)。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from mycompanion.memory.okf import Relation, TopicDoc

# 関係の種類ごとの、連想の強さ(辺の重み)。両方向にたどる
EDGE_WEIGHTS = {"involves": 0.8, "follows": 0.7, "part_of": 0.7, "parallel_with": 0.6, "member_of": 0.5, "sibling": 0.5}
# 軸による、たどる関係の好み。(軸, 係数)。pref = 1 + 0.25 × Σ 係数 × (2 × 軸の値 − 1)
EDGE_PREFERENCES: dict[str, tuple[tuple[str, int], ...]] = {
    "participant": (("a", -1), ("e", +1)),  # 共感・具体の側: 「そこにいた人」へ
    "place": (("a", -1),),  # 具体の側: 「その場所・もの」へ
    "subject": (("a", -1),),
    "follows": (("e", -1), ("f", -1)),  # 論理・計画の側: 「その次・その前」へ
    "part_of": (("a", +1), ("f", -1)),  # 抽象・計画の側: 「全体の中の位置」へ
    "parallel_with": (("a", +1), ("f", +1)),  # 抽象・柔軟の側: 「同じ頃の別の話」へ
    "sibling": (("a", +1), ("f", +1)),
    "member_of": (("a", -1),),  # 具体の側: 「その人の所属」へ
}
SIBLING_LABEL = "同じ取り組みの別の話"
NEUTRAL_AXES = {"i": 0.5, "a": 0.5, "e": 0.5, "f": 0.5}


def _clip01(value: float) -> float:
    return max(0.0, min(1.0, value))


def default_ease(axes: dict[str, float]) -> float:
    """連想のしやすさの既定値。抽象・柔軟・外向の側ほど、離れた話題どうしのつながりに気づきやすい。"""
    return _clip01(0.20 + 0.35 * axes["a"] + 0.30 * axes["f"] + 0.15 * (1 - axes["i"]))


def default_shift(axes: dict[str, float]) -> float:
    """話題の移りやすさの既定値。柔軟・外向の側ほど、思いついた話題へ流れやすい。"""
    return _clip01(0.15 + 0.45 * axes["f"] + 0.25 * (1 - axes["i"]) + 0.10 * axes["a"])


@dataclass(frozen=True)
class AssocParams:
    """キャラクタごとの、連想の制御の値。"""

    ease: float  # 連想のしやすさ(0〜1)
    shift: float  # 話題の移りやすさ(0〜1)
    hops: int  # たどる段数(1 か 2)
    delta: float  # 2段目の減衰
    floor: float  # 候補にする、連想の強さの下限
    level: str  # 移りやすさの段階(low / mid / high)
    chain_max: int  # ユーザーが乗ってこないまま、続けて移る回数の上限
    session_max: int  # 1セッションの、連想による移動の回数
    dwell_turns: int  # 移ったあと、同じ話題に留まるターン数

    def probability(self, initiative: float) -> float:
        """話題が出尽くしかけたとき、連想で移る確率。"""
        return _clip01(initiative * (0.3 + 0.7 * self.shift))


def params(axes: dict[str, float] | None, traits: dict[str, Any] | None = None) -> AssocParams:
    """性格タイプの軸(と、公開時に解決した値)から、連想の制御の値を決める。上限は、差分を付けても超えない。"""
    axes = {**NEUTRAL_AXES, **(axes or {})}
    traits = traits or {}
    ease = _clip01(float(traits.get("association_ease", default_ease(axes))))
    shift = _clip01(float(traits.get("topic_shift", default_shift(axes))))
    level = "low" if shift < 0.4 else "mid" if shift < 0.65 else "high"
    chain, session, dwell = {"low": (1, 2, 3), "mid": (2, 4, 2), "high": (3, 6, 1)}[level]
    return AssocParams(
        ease=round(ease, 4), shift=round(shift, 4), hops=1 if ease < 0.5 else 2, delta=0.3 + 0.4 * ease,
        floor=max(0.2, 0.45 - 0.25 * ease), level=level, chain_max=chain, session_max=session, dwell_turns=dwell,
    )  # fmt: skip


def edge_preference(kind: str, axes: dict[str, float] | None) -> float:
    axes = {**NEUTRAL_AXES, **(axes or {})}
    return 1.0 + 0.25 * sum(sign * (2 * axes[axis] - 1) for axis, sign in EDGE_PREFERENCES.get(kind, ()))


@dataclass(frozen=True)
class Edge:
    target: TopicDoc
    kind: str  # 関係の種類(involves は、役割で分ける: participant / place / subject)
    label: str  # 橋渡しの説明に使う、日本語のラベル


def _edge_kind(relation: Relation) -> str:
    return relation.role or relation.kind if relation.kind == "involves" else relation.kind


def edges(doc: TopicDoc, docs: list[TopicDoc]) -> list[Edge]:
    """そのトピックから、関係でたどれる相手(両方向)。同じ親の子どうしも、つながりとみなす。"""
    by_path = {d.path: d for d in docs}
    found = [Edge(by_path[r.to], _edge_kind(r), r.label) for r in doc.relations if r.to in by_path]
    found += [Edge(other, _edge_kind(r), r.label) for other in docs for r in other.relations if r.to == doc.path]
    parents = {r.to for r in doc.relations if r.kind == "part_of"}
    found += [
        Edge(other, "sibling", SIBLING_LABEL)
        for other in docs
        if other.doc_id != doc.doc_id and any(r.kind == "part_of" and r.to in parents for r in other.relations)
    ]
    return found


@dataclass(frozen=True)
class Association:
    doc: TopicDoc
    strength: float
    path: str  # つながりの説明(例: 「忘年会の幹事 —場→ 駅前 —場→ 駅前のカフェ」)
    hops: int


def spread(
    focus: TopicDoc,
    docs: list[TopicDoc],
    blocked: set[uuid.UUID],
    axes: dict[str, float] | None,
    values: AssocParams,
) -> list[Association]:
    """焦点から、関係をたどって近いトピックの、連想の強さを計算する(2段まで)。

    `blocked` のトピックは、終点にも、経由点にもしない。同じトピックに複数の経路があれば、強いほうを採る。
    """

    def weight(edge: Edge) -> float:
        base = EDGE_WEIGHTS["involves" if edge.kind in ("participant", "place", "subject") else edge.kind]
        return base * edge_preference(edge.kind, axes)

    best: dict[uuid.UUID, Association] = {}

    def offer(candidate: Association) -> None:
        current = best.get(candidate.doc.doc_id)
        if current is None or candidate.strength > current.strength:
            best[candidate.doc.doc_id] = candidate

    for first in edges(focus, docs):
        if first.target.doc_id in blocked or first.target.doc_id == focus.doc_id:
            continue
        strength = weight(first)
        offer(Association(first.target, strength, f"{focus.title} —{first.label}→ {first.target.title}", 1))
        if values.hops < 2:
            continue
        for second in edges(first.target, docs):
            if second.target.doc_id in blocked or second.target.doc_id in (focus.doc_id, first.target.doc_id):
                continue
            path = f"{focus.title} —{first.label}→ {first.target.title} —{second.label}→ {second.target.title}"
            offer(Association(second.target, strength * weight(second) * values.delta, path, 2))
    found = [a for a in best.values() if a.strength >= values.floor]
    return sorted(found, key=lambda a: a.strength, reverse=True)


def bridge_hint(axes: dict[str, float] | None) -> str:
    """橋渡しの言い方(性格タイプの軸に合わせる)。下書きに渡す。感情の情報は含めない。"""
    axes = {**NEUTRAL_AXES, **(axes or {})}
    parts = []
    parts.append("「〜って、…と似てるね」のように、共通点から移る" if axes["a"] >= 0.5 else "「〜といえば、あの」と、具体的なものから移る")
    parts.append("人のことなら「〜さんは元気?」と気にかける" if axes["e"] >= 0.5 else "「それで、そのあと〜は」と、順を追う")
    parts.append("前置きなしに、軽く移る" if axes["f"] >= 0.5 else "「話は変わるけど」と、区切りを言ってから移る")
    return "。".join(parts) + "。"
