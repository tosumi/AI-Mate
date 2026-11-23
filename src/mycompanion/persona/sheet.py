"""好き嫌いシートの解決(ペルソナデザイナ仕様書 §5.4.0、`likes-dislikes-sheet-review-rev3.md`)。

シート(`likes_dislikes`)は設計用の元データ。公開時に、選んだ項目ごとに
「条件つきの話題ルール 1件」と「嗜好 1件」へ解決して、スナップショットとして保存する。
エンジンが読むのは、解決後の値だけ(`appraisal_rules` の `origin: sheet`、`preferences`)。

しきい値・上限・繰り返しの倍率は、全キャラクタ共通の設定(`EngineSettings`)で持つ。
ここで決めるのは、強さ → `gain`・`affinity`、種類 → 効果の配分、だけ。
"""

from __future__ import annotations

from typing import Any

from mycompanion.persona.schema import LikesDislikes
from mycompanion.state import catalog

# 強さの対応表
GAIN = {"weak": 0.3, "medium": 0.5, "strong": 0.7}
AFFINITY = {"weak": 0.4, "medium": 0.7, "strong": 0.9}

# 好き: 喜びを主にする。trust は相手への信頼の感情なので、話題が出ただけでは小さくする
LIKE_EFFECTS = {"joy": 1.0, "anticipation": 0.5, "trust": 0.2}
# ユーザーとキャラクタの間の行為(「イイコイイコしてあげる」)では、trust を大きくする
LIKE_EFFECTS_WITH_CHARACTER = {"joy": 1.0, "trust": 0.6, "anticipation": 0.4}
# 嫌いなもの: 選んだ感情の数に応じて配分する(合計が大きくなりすぎないように)
DISLIKE_SHARE = {1: 1.0, 2: 0.8, 3: 0.6}

DISLIKE_THING_EMOTIONS = ("fear", "sadness", "disgust", "anger", "surprise")


def _base(slot: str, kind: str, item: catalog.CatalogItem, strength: str) -> dict[str, Any]:
    return {
        "id": f"sheet_{kind}_{slot}",
        "origin": "sheet",
        "kind": kind,  # like / dislike
        "catalog_id": item.id,
        "category": item.category,
        "unit": "thing" if item.category in catalog.THING_CATEGORIES else "activity",
        "topic": item.label,
        "note": catalog.note_text(item),
        "include": list(item.include),
        "involvement": item.involvement,
        "gain": GAIN[strength],
    }


def resolve_rules(sheet: LikesDislikes | None) -> list[dict[str, Any]]:
    """シートを、解決済みの話題ルール(`origin: sheet`)にする。カタログにない ID は、無視する(lint の E15)。"""
    if sheet is None:
        return []
    rules: list[dict[str, Any]] = []
    for slot in catalog.CATEGORIES:
        liked = getattr(sheet.liked, slot)
        item = catalog.get(liked.id) if liked else None
        if liked and item and item.likeable and item.category == slot:
            rule = _base(slot, "like", item, liked.strength)
            rule["effects"] = dict(LIKE_EFFECTS)
            rule["scope"] = liked.scope if item.involvement else "any"
            if item.involvement:
                rule["effects_with_character"] = dict(LIKE_EFFECTS_WITH_CHARACTER)
            rule["repeat"] = "decay"
            rules.append(rule)

        disliked = getattr(sheet.disliked, slot)
        item = catalog.get(disliked.id) if disliked else None
        if not (disliked and item and item.dislikeable and item.category == slot):
            continue
        rule = _base(slot, "dislike", item, disliked.strength)
        if slot in catalog.THING_CATEGORIES:
            emotions = [e for e in dict.fromkeys(disliked.emotions) if e in DISLIKE_THING_EMOTIONS][:3]
            if not emotions:
                continue
            rule["effects"] = dict.fromkeys(emotions, DISLIKE_SHARE[len(emotions)])
            rule["repeat"] = disliked.repeat
        else:
            rule["effects"] = {disliked.emotion: 1.0}
            rule["repeat"] = "flat"  # 出るたびに毎回(逓減しない)。1ターンと累計の上限は、かける
            rule["avoid"] = {"emotion": disliked.emotion} if disliked.avoid else None
            # 「おしゃべりが嫌い」でも、ユーザーとの会話そのものには、嫌悪を起こさない
            rule["block_with_character"] = item.id in catalog.NO_DISLIKE_WITH_CHARACTER
            rule["affection"] = item.id in catalog.AFFECTION_ITEMS
        rules.append(rule)
    return rules


def sheet_preferences(sheet: LikesDislikes | None) -> list[dict[str, Any]]:
    """シートから作る嗜好(想起のしやすさ。本体仕様書 §9.10)。"""
    if sheet is None:
        return []
    result = []
    for rule in resolve_rules(sheet):
        slot = rule["category"]
        selected = getattr(sheet.liked if rule["kind"] == "like" else sheet.disliked, slot)
        sign = 1 if rule["kind"] == "like" else -1
        result.append({"topic": rule["topic"], "affinity": sign * AFFINITY[selected.strength], "source": "sheet"})
    return result


def first_delta(rule: dict[str, Any], reactivity: float) -> float:
    """1回目の最大変化量の目安(関連度 1.0。1ターンの上限を適用する前)。"""
    effects = [*rule["effects"].values(), *rule.get("effects_with_character", {}).values()]
    return reactivity * rule["gain"] * max(effects, default=0.0)
