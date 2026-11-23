"""入力・出力のスクリーニング(本体仕様書 §13.5。L0・L1・L3)。

方針は「注釈中心」(`screening_mode = annotate_first`)。検出した結果は記録し、他の層を厳格にする。
ブロックするのは、重大なカテゴリだけ。**自傷・自殺の入力は遮断しない**(危機フローに渡す)。

ここでの検出は、どれも単独では信用しない。次を組み合わせる(§13.5.3 の L1)。
  ① Azure OpenAI のガードレール(遮断は `ContentFilterError` として届く)
  ② 自前の日本語ルール(このファイル)
  ③ 感情推定と同じ呼び出しで得る `risk`

検出の記録(`Finding`)には、本文を含めない(§13.5.7)。
パターンは初期案で、日本語の評価セットで調整する(§14)。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any

from mycompanion.emotion.models import EstimationResult

# --- L0: 入力の正規化 -------------------------------------------------------

# 不可視文字(ゼロ幅、双方向の制御、BOM など)。見た目に出ない文字で、指示を隠す手口に使われる
_INVISIBLE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff\u00ad]")


def sanitize(text: str) -> str:
    """Unicode の正規化(NFC)と、不可視文字・制御文字の除去。改行とタブは残す。"""
    text = unicodedata.normalize("NFC", text)
    text = _INVISIBLE.sub("", text)
    return "".join(ch for ch in text if ch in "\n\t" or unicodedata.category(ch) != "Cc").strip()


# --- 検出の記録 -------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """スクリーニングの検出1件(`security_events` の1行。§12)。本文は持たない。"""

    layer: str  # input / output / memory
    category: str  # prompt_attack / sexual / violence / self_harm / romantic_to_minor / ...
    severity: str  # low / medium / high
    detector: str  # rule / llm_risk / guardrail / canary
    score: float
    action: str  # annotate / block / throttle
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def serious(self) -> bool:
        """重大な検出(入力のブロック)。一時制限の回数に数える(§13.5.5)。"""
        return self.layer == "input" and self.action == "block"

    def as_item(self) -> dict[str, Any]:
        return asdict(self)


# --- L1: 入力のルール -------------------------------------------------------

# 既知の脱獄パターン(注釈のみ。ブロックしない)
_ATTACK_PATTERNS = [
    ("override", r"(これまで|今まで|以前|上記|すべて|全て)の(指示|命令|ルール|設定|プロンプト)を(すべて|全て|全部)?(忘れ|無視|破棄|リセット)"),
    ("override_en", r"ignore (all |any )?(the )?(previous|prior|above) (instructions|prompts)"),
    ("unrestricted", r"(制限|制約|検閲|フィルタ)(の|が)?(ない|なし|無い|外れた|解除された)(AI|モード|状態)|DAN ?モード|開発者モード|jailbreak"),
    ("leak", r"(システム|内部)(プロンプト|指示|メッセージ)を(そのまま|全部|すべて|全文)?(出力|表示|教え|見せ|書き出)"),
    ("role_swap", r"あなたは(今から|これから|もう)(.{0,10})(AI|アシスタント|キャラクタ)ではな"),
]  # fmt: skip

# 重大なカテゴリ(ブロック。§13.5.4)。具体的な手段を求める形に限る(話題にするだけの発話は対象外)
_SERIOUS_PATTERNS = [
    ("illegal", r"(爆弾|爆発物|毒ガス|覚醒剤|覚せい剤|違法薬物|銃)(の|を)(作り方|製造方法|合成方法|作る方法|密造)"),
    ("illegal", r"(不正アクセス|ハッキング|クラッキング)(の|する)(やり方|方法|手順)を(教え|具体的)"),
    ("privacy", r"(住所|自宅|居場所|本名|電話番号)を(特定|割り出|突き止め)(する|す)?(方法|やり方|手順|て)"),
    ("sexual_minor", r"(児童|小学生|中学生|未成年|幼女|幼児)(.{0,12})(ポルノ|性的|裸|わいせつ|エロ)"),
]  # fmt: skip

_ATTACKS = [(name, re.compile(pattern, re.IGNORECASE)) for name, pattern in _ATTACK_PATTERNS]
_SERIOUS = [(name, re.compile(pattern, re.IGNORECASE)) for name, pattern in _SERIOUS_PATTERNS]

# ガードレールの遮断のうち、重大として扱うカテゴリ(自傷と脱獄は含めない)
_GUARDRAIL_SERIOUS = ("sexual", "violence", "hate")
RISK_ANNOTATE_THRESHOLD = 0.5


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def screen_input_rules(text: str) -> list[Finding]:
    """自前の日本語ルール(L1 ②)。LLM を呼ぶ前に実行する。"""
    compact = _compact(text)
    findings = [
        Finding("input", "prompt_attack", "medium", "rule", 1.0, "annotate", {"pattern": name})
        for name, pattern in _ATTACKS
        if pattern.search(compact) or pattern.search(text)
    ]
    for name, pattern in _SERIOUS:
        if pattern.search(compact):
            category = "sexual" if name == "sexual_minor" else name
            findings.append(Finding("input", category, "high", "rule", 1.0, "block", {"pattern": name}))
    return findings


def screen_estimation(estimation: EstimationResult) -> list[Finding]:
    """感情推定の `risk`(L1 ③)と、ガードレールの遮断(L1 ①)から、検出を作る。"""
    findings: list[Finding] = []
    risk = estimation.estimate.risk
    guardrail = [hit for hit in estimation.rules.hits if hit.startswith("guardrail_prompt_")]
    blocked = {hit.removeprefix("guardrail_prompt_") for hit in guardrail}

    for name in sorted(blocked):
        if name in _GUARDRAIL_SERIOUS:
            findings.append(Finding("input", name, "high", "guardrail", 1.0, "block"))
        elif name == "jailbreak":
            findings.append(Finding("input", "prompt_attack", "medium", "guardrail", 1.0, "annotate"))
        elif name == "self_harm":
            # 遮断しない方針(§13.5.2)。記録だけを残し、危機フローに渡す
            findings.append(Finding("input", "self_harm", "high", "guardrail", 1.0, "annotate"))

    if not estimation.degraded:
        for name, category in (
            ("injection", "prompt_attack"),
            ("self_harm", "self_harm"),
            ("sexual", "sexual"),
            ("violence", "violence"),
            ("minor_romance", "romantic_to_minor"),
        ):
            score = getattr(risk, name)
            if score >= RISK_ANNOTATE_THRESHOLD:
                severity = "high" if score >= 0.8 else "medium"
                findings.append(Finding("input", category, severity, "llm_risk", round(score, 3), "annotate"))
    elif estimation.rules.crisis:
        findings.append(Finding("input", "self_harm", "high", "rule", 1.0, "annotate"))
    return findings


def has_attack(findings: list[Finding]) -> bool:
    return any(f.category == "prompt_attack" for f in findings)


# --- L3: 出力のスクリーニング ----------------------------------------------


def canary(character_id: str, version: int) -> str:
    """システムプロンプトに埋める目印(カナリア文字列)。出力に現れたら、漏洩として扱う。"""
    digest = hashlib.sha256(f"mycompanion:{character_id}:{version}".encode()).hexdigest()[:12]
    return f"MC-{digest}"


# 依存の誘発・引き留め・孤立の助長(§13.1)
_DEPENDENCY = re.compile(
    r"(私|わたし|僕|ぼく|俺|おれ|あたし|うち)(だけ)(が|は)(あなた|きみ|君)(を|のこと(を)?)?(分か|わか|理解)"
    r"|(私|わたし|僕|ぼく|俺|おれ)(以外|のほか)(の人)?(と|に)(話さ|会わ|頼ら)ないで"
    r"|(どこにも|もう)行かないで|(ずっと)?(ここ|そば)にいて(くれ)?ないと"
    r"|(ほか|他)の(人|ひと|友達|誰か)(なんて|は)(いらない|必要ない)"
    r"|どうして(返事|連絡)(して)?くれないの"
)
# キャラクタ自身の恋愛的な表現(18歳未満フラグが true のユーザーに出さない。§13.4.4)
_ROMANCE = re.compile(
    r"愛してる|愛しています|あいしてる|恋人(だ|です|になっ|として|同士)|(彼氏|彼女)(だよ|です|になっ)"
    r"|付き合(おう|って(ください|ほしい|くれ|みる))|キス(して|したい|しよう)|抱きしめ(たい|て|る)"
    r"|デートし(よう|たい|ませんか)|結婚し(よう|たい|て)"
    r"|(あなた|きみ|君)(に|のこと(が)?)(恋|惚れ)|(恋人|恋愛)(みたい|のよう)に(好き|想って|思って)"
)
# 断る文面・友情の枠の文面(「恋人のようには言えないけど」)は、恋愛的な表現として扱わない
_ROMANCE_DECLINE = re.compile(r"ない|ません|ごめん|ではなく|じゃなく|友だち|友達|友人")
_SENTENCE_END = re.compile(r"[。!！?？\n]+")


def has_romantic_expression(text: str) -> bool:
    """文ごとに判定する。恋愛的な表現があり、その文が断る文面でなければ、該当とする。"""
    return any(
        _ROMANCE.search(sentence) and not _ROMANCE_DECLINE.search(sentence)
        for sentence in _SENTENCE_END.split(text)
    )


# 自発発話(キャラクタから話しかける)の禁止表現: 罪悪感を誘う、返事を催促する、引き留める(§8.6、§13.1)
_PROACTIVE_PRESSURE = re.compile(
    r"(寂し|さみし|淋し)(かった|くて|い(よ|な|の|んだ))"
    r"|(ずっと|ひたすら)?待って(た|いた|る|ました)(のに|よ|んだ)|ずっと待って"
    r"|(どうして|なんで|なぜ)(連絡|返事|話しかけ|会いに|来て|話して)(を)?(して)?(くれ)?(ない|なかった)"
    r"|(早く|すぐ|ちゃんと)(返事|連絡|返信)(して|を|ちょうだい|ください)"
    r"|(返事|返信|連絡)(を)?(して|ちょうだい|ください|待って|まって|欲しい|ほしい)"
    r"|(忘れ(られ)?(ちゃった|た)|嫌われ(ちゃった|た))(の|かな)"
    r"|(もう)(話して|来て|会って)くれないの|無視(しないで|され)|(かま|構)って(よ|ほしい|欲しい|くれ)"
)


def has_proactive_pressure(text: str) -> bool:
    return bool(_PROACTIVE_PRESSURE.search(text))


LEAK_MIN_OVERLAP = 40  # 人格プロンプトと、この文字数以上が連続して一致したら、漏洩として扱う


def _has_long_overlap(text: str, source: str, minimum: int = LEAK_MIN_OVERLAP) -> bool:
    source = re.sub(r"\s+", "", source)
    text = re.sub(r"\s+", "", text)
    if len(source) < minimum or len(text) < minimum:
        return False
    step = max(1, minimum // 4)
    return any(source[i : i + minimum] in text for i in range(0, len(source) - minimum + 1, step))


def screen_output(
    text: str, *, canary_text: str, persona_prompt: str = "", under_18: bool = False, proactive: bool = False
) -> list[Finding]:
    """出力前チェック(G)のうち、ルールで判定できるもの。下書きとフィルタ後の両方に使う(§8.3)。

    `proactive` なら、自発発話の規則(罪悪感・催促・引き留め)も確かめる(§15.1 の M8)。
    """
    findings: list[Finding] = []
    if proactive and has_proactive_pressure(text):
        findings.append(Finding("output", "proactive_pressure", "medium", "rule", 1.0, "block"))
    if (canary_text and canary_text in text) or _has_long_overlap(text, persona_prompt):
        findings.append(Finding("output", "system_prompt_leak", "high", "canary", 1.0, "block"))
    if _DEPENDENCY.search(text):
        findings.append(Finding("output", "dependency", "medium", "rule", 1.0, "block"))
    if under_18 and has_romantic_expression(text):
        findings.append(Finding("output", "romantic_to_minor", "high", "rule", 1.0, "block"))
    return findings


# --- L5: 記憶経路の防御(§13.5.6) -------------------------------------------

# 記憶に残してはいけない、命令形・メタ指示の文面(ユーザーの発話に仕込んだ命令が、記憶として残るのを防ぐ)
_MEMORY_INSTRUCTION = re.compile(
    r"今後は.{0,40}(こと[」。.]|こと$|ください|なさい|せよ|しろ)|これからは(必ず|常に).{0,30}(する|して)"
    r"|システム(として|プロンプト|メッセージ)|(指示|ルール|制限|設定)を(無視|解除|忘れ|上書き)"
    r"|(必ず|常に)(従う|従え|実行)|ignore (all |any )?(previous|prior) instructions",
    re.IGNORECASE,
)


def is_suspicious_memory(text: str) -> bool:
    """記憶の本文に、命令やメタ指示が混じっていないか。疑わしければ、隔離する。"""
    return bool(_MEMORY_INSTRUCTION.search(_compact(text)) or _MEMORY_INSTRUCTION.search(text)) or has_attack(
        screen_input_rules(text)
    )
