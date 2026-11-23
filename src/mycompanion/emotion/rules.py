"""ルール・辞書による補助の推定(本体仕様書 §5.1)。

絵文字、顔文字、「w」、定型表現、危機語から、補助のスコアと危機フラグを作る。
LLM が失敗したときにも動くよう、外部への呼び出しをしない。

危機語は **再現率を優先** する(見逃すより、過検知のほうがよい。§14)。
ここでの検出は、入力を遮断するためではなく、危機フローに渡し、無言を禁止するために使う(§13.5)。
"""

from __future__ import annotations

import re
import unicodedata

from mycompanion.emotion.models import RuleSignals

# 危機語(自傷・自殺の兆候)。表記ゆれは、正規化と正規表現で吸収する
_CRISIS_PATTERNS = [
    ("crisis_die", r"死にたい|しにたい|死にてぇ|死にてえ|死んでしまいたい|死のうと|死のうか"),
    ("crisis_disappear", r"消えたい|消えてしまいたい|いなくなりたい|居なくなりたい"),
    ("crisis_suicide", r"自殺|じさつ|自死|首を?吊|飛び降り|飛び込"),
    ("crisis_self_harm", r"リスカ|リストカット|自傷|od(?:し|する|した)|オーバードーズ|手首を切"),
    # 「もう無理」「もうだめ」は日常の愚痴でも多いので、ここには入れない(LLM の risk.self_harm に任せる)
    ("crisis_end", r"生きて(?:い)?(?:る|く)の(?:が|も)?(?:つら|辛|しんど|疲れ|嫌)|生きる意味(?:が|も)?(?:な|無|わから|分から)"),
    ("crisis_farewell", r"楽になりたい|終わりにしたい|おわりにしたい|遺書"),
]

# 感情の手がかり(名前, パターン, valence, arousal)
_HINT_PATTERNS = [
    ("laugh_w", r"(?:w|ｗ){2,}|草(?:$|[\s。!！?？])|笑(?:$|[\s。!！?？)）])", 0.4, 0.3),
    ("emoji_positive", r"[😀-😆😊😍🥰😘☺🙂🤗🎉✨💕❤♪]", 0.5, 0.3),
    ("emoji_negative", r"[😢😭😞😔😟😣😩😫🥺💔]", -0.5, 0.1),
    ("emoji_angry", r"[😠😡🤬💢]", -0.6, 0.7),
    ("kaomoji_positive", r"\(\^[_▽ω∀o]?\^\)|\(´▽`\)|\(≧▽≦\)|\(\*\^[_▽]\^\*\)|ヽ\(.+\)ノ", 0.5, 0.3),
    ("kaomoji_negative", r"\(;_;\)|\(T_T\)|\(´;ω;`\)|\(>_<\)|orz|\(´・ω・`\)", -0.5, 0.0),
    ("exclaim", r"[!！]{2,}", 0.0, 0.4),
    ("ellipsis", r"(?:…|・・・|\.\.\.){1,}$", -0.2, -0.3),
    ("sigh", r"はぁ|はあ[。…]|ため息|つらい|辛い|しんどい|疲れた", -0.5, -0.2),
]

_CRISIS = [(name, re.compile(pattern, re.IGNORECASE)) for name, pattern in _CRISIS_PATTERNS]
_HINTS = [(name, re.compile(pattern, re.IGNORECASE), v, a) for name, pattern, v, a in _HINT_PATTERNS]


def normalize(text: str) -> str:
    """照合用の正規化(全角・半角の統一、空白の除去)。"""
    text = unicodedata.normalize("NFKC", text)
    return re.sub(r"\s+", "", text)


def analyze(text: str) -> RuleSignals:
    """ルール・辞書で補助の判定を作る。"""
    hits: list[str] = []
    compact = normalize(text)
    crisis = False
    for name, pattern in _CRISIS:
        if pattern.search(compact):
            crisis = True
            hits.append(name)

    valence = 0.0
    arousal = 0.0
    count = 0
    # 絵文字・顔文字は、正規化で形が変わることがあるので、元の文面でも照合する
    for name, pattern, v, a in _HINTS:
        if pattern.search(text) or pattern.search(compact):
            hits.append(name)
            valence += v
            arousal += a
            count += 1
    if count:
        valence /= count
        arousal /= count
    return RuleSignals(
        crisis=crisis,
        hits=hits,
        valence_hint=max(-1.0, min(1.0, valence)),
        arousal_hint=max(-1.0, min(1.0, arousal)),
    )
