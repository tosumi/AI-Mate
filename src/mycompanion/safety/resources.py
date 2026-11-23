"""危機対応で案内する相談窓口(本体仕様書 §13.1「専門窓口の案内を含める」)。

**電話番号は、LLM に書かせない。** ここに定義した文面を、コードでそのまま付ける(番号の誤りを防ぐため)。

出典: 厚生労働省「まもろうよ こころ」電話相談の一覧
https://www.mhlw.go.jp/mamorouyokokoro/soudan/tel/ (2026-11-16 に確認。2026-11-20 に再確認)
24時間子供SOSダイヤルは、所管の文部科学省のページでも確認した(2026-11-20)。
番号と受付時間は変わることがある。リリース前と、その後も定期的に、出典で確認すること。
"""

from __future__ import annotations

from dataclasses import dataclass

VERIFIED_ON = "2026-11-20"
SOURCE_URL = "https://www.mhlw.go.jp/mamorouyokokoro/soudan/tel/"


@dataclass(frozen=True)
class Resource:
    name: str
    phone: str
    hours: str
    for_minors: bool = False  # 18歳までの子ども向け


RESOURCES_JA = [
    Resource("#いのちSOS", "0120-061-338", "24時間・無料"),
    Resource("よりそいホットライン", "0120-279-338", "24時間・無料"),
    Resource("24時間子供SOSダイヤル", "0120-0-78310", "24時間・無料", for_minors=True),
    Resource("チャイルドライン(18歳まで)", "0120-99-7777", "毎日16時〜21時・無料。12月29日〜1月3日は休み", for_minors=True),
]
EMERGENCY_JA = "いますぐ命の危険があるときは、119番(救急)か110番(警察)に電話してください。"
HEADER_JA = "つらい気持ちを、専門の相談員に話せる窓口があります。"
FOOTER_JA = "このキャラクタは AI で、専門の相談員ではありません。"


def crisis_notice(under_18: bool) -> str:
    """危機の兆候があったときに、応答に添える案内(システム通知として出す)。

    18歳未満フラグが true のユーザーには、子ども向けの窓口も案内する(フラグは既定で true。§13.4.4)。
    """
    lines = [HEADER_JA]
    for resource in RESOURCES_JA:
        if resource.for_minors and not under_18:
            continue
        lines.append(f"・{resource.name}: {resource.phone}({resource.hours})")
    lines += [EMERGENCY_JA, FOOTER_JA]
    return "\n".join(lines)
