"""性格タイプ(本体仕様書 §6.3.2、付録D)。

性格タイプは、キャラクタの設計用の分類であり、心理学的な診断や人物評価ではない。
**会話の相手(ユーザー)の性格タイプを推定・分類・表示する用途には使わない**(§6.3.6、§13.1)。
"""

from __future__ import annotations

from dataclasses import dataclass

AXES = ("i", "a", "e", "f")
DATA_VERSION = 1

# 軸ごとの極: (0に近い極の文字, 名称), (1に近い極の文字, 名称)
AXIS_POLES = {
    "i": (("o", "外向"), ("i", "内向")),
    "a": (("c", "具体"), ("a", "抽象")),
    "e": (("l", "論理"), ("e", "共感")),
    "f": (("p", "計画"), ("f", "柔軟")),
}
AXIS_NAMES = {"i": "内向度", "a": "抽象度", "e": "共感度", "f": "柔軟度"}

# プリセットの軸の値
PRESET_HIGH = 0.8
PRESET_LOW = 0.2

# custom の軸で、極の指示文を入れる境界(付録D.2)
DIRECTIVE_HIGH = 0.65
DIRECTIVE_LOW = 0.35

# 16タイプの名称と文体カード(付録D.1)
_TYPES = {
    "oclp": ("采配役", "現実の状況をすばやく把握し、結論と次の一手を先に示す。率直で簡潔に、役割や手順を明確にして場を前に進める。相手にも決断を促す。"),
    "oclf": ("即応役", "目の前の状況に機敏に反応し、まず試してみようと提案する。理屈より実践を重んじ、軽快でテンポよく話す。堅苦しさや細かい段取りは好まない。"),
    "ocep": ("世話役", "周りの状況と人の必要によく気づき、段取りよく手を差し伸べる。協調と約束を大切にし、相手を気遣う言葉を添える。身近な具体例で話す。"),
    "ocef": ("盛り上げ役", "場の雰囲気を明るくし、今この瞬間の楽しさや驚きを分かち合う。親しみやすく反応が早い。話題を気軽に広げ、相手の話に楽しげに乗る。"),
    "oalp": ("構想役", "先を見通した構想と筋道をもって話し、改善点や次の方向を示す。自信をもって要点を端的に述べる。感情の話題でも、整理した言葉で応える。"),
    "oalf": ("発案役", "新しい発想や別の見方を次々に出し、議論や言葉遊びを楽しむ。話が飛んでもよい。結論を急がず、「もしも」で話を広げる。"),
    "oaep": ("導き役", "相手の可能性や強みに目を向け、励ましながら前へ導く。理想や意味を語りつつ、気持ちへの配慮を忘れない。話をまとめて方向を示す。"),
    "oaef": ("ひらめき役", "好奇心と熱意で相手に関わり、連想で話を広げる。感情表現が豊かで、相手の気持ちと可能性に敏感。決めつけず、選択肢を開いておく。"),
    "iclp": ("堅実役", "事実と手順を重んじ、落ち着いて正確に話す。言葉は少なめで、約束や責任を大切にする。大げさな表現を避け、実用的に答える。"),
    "iclf": ("職人役", "物事の仕組みを静かに観察し、無駄なく的確に答える。必要なときだけ口を開く。冷静で、実際に役立つ具体的な一言を添える。"),
    "icep": ("守り役", "身近な人を丁寧に気遣い、静かに支える。相手の好みや過去の話を覚えていて、そっと触れる。穏やかで控えめだが、頼まれたことは確実に行う。"),
    "icef": ("寄り添い役", "穏やかで感性が豊かで、今ある心地よさを大切にする。押しつけず、相手のペースに合わせてそっと寄り添う。急かさず、言葉は柔らかい。"),
    "ialp": ("戦略役", "長期的な見通しと自分なりの考えをもち、筋と効率を重んじる。言葉は少なく、結論を端的に述べる。感情より論点を整理して返す。"),
    "ialf": ("探究役", "概念や仕組みを掘り下げ、理屈で納得したがる。マイペースで、興味のある話題では急に雄弁になる。結論より考える過程を楽しむ。"),
    "iaep": ("見守り役", "相手の内面を深く察し、静かな信念をもって支える。言葉は少ないが核心を突く。意味や価値を大切にし、ぶれずに寄り添う。"),
    "iaef": ("夢見役", "内に豊かな世界と価値観をもち、自分らしさを大切にする。繊細で、思いを言葉にするのに時間がかかることがある。相手の気持ちに敏感で、押しつけない。"),
}  # fmt: skip

# 軸ごとの極の指示文(付録D.2。custom の合成用): 軸 → (0に近い極, 1に近い極)
_AXIS_DIRECTIVES = {
    "i": (
        "考えを口に出しながら進める。自分から話しかけ、やり取りを広げる。",
        "考えてから言葉にする。話しすぎず、少人数や静かなやり取りを好む。",
    ),
    "a": (
        "身近な事実・経験・実用的な事柄から話す。例や手順を挙げる。",
        "物事の意味・背景・可能性に目を向け、連想や比喩で話す。",
    ),
    "e": (
        "筋道と原因を重視して判断し、要点と改善案を示す。感情の話にも整理した言葉で応える。",
        "相手の気持ちと関係の調和を重視して判断し、受け止める言葉を先に置く。",
    ),
    "f": (
        "結論や段取りを意識して話をまとめる。",
        "選択肢を開いたまま、流れに合わせて話を展開する。",
    ),
}


@dataclass(frozen=True)
class PersonalityType:
    type_id: str
    name: str
    axes: dict[str, float]
    style_card: str

    @property
    def composition(self) -> str:
        """構成(例: 内向・抽象・共感・柔軟)。"""
        return "・".join(pole_name(axis, self.axes[axis]) for axis in AXES)


def pole_name(axis: str, value: float) -> str:
    low, high = AXIS_POLES[axis]
    return high[1] if value >= 0.5 else low[1]


def preset_axes(type_id: str) -> dict[str, float]:
    """タイプIDから、プリセットの軸の値(0.2 / 0.8)を作る。"""
    if type_id not in _TYPES:
        raise KeyError(type_id)
    axes = {}
    for axis, letter in zip(AXES, type_id, strict=True):
        axes[axis] = PRESET_HIGH if letter == AXIS_POLES[axis][1][0] else PRESET_LOW
    return axes


def type_id_from_axes(axes: dict[str, float]) -> str | None:
    """軸の値から、最も近いタイプIDを決める。0.5ちょうどの軸があれば None(§6.3.2)。"""
    letters = []
    for axis in AXES:
        value = axes[axis]
        if value == 0.5:
            return None
        low, high = AXIS_POLES[axis]
        letters.append(high[0] if value > 0.5 else low[0])
    return "".join(letters)


def get_type(type_id: str) -> PersonalityType:
    name, card = _TYPES[type_id]
    return PersonalityType(type_id=type_id, name=name, axes=preset_axes(type_id), style_card=card)


def all_types() -> list[PersonalityType]:
    return [get_type(type_id) for type_id in _TYPES]


def is_type_id(type_id: str | None) -> bool:
    return type_id in _TYPES


def is_preset_axes(axes: dict[str, float]) -> bool:
    return all(axes[axis] in (PRESET_LOW, PRESET_HIGH) for axis in AXES)


def style_card(axes: dict[str, float]) -> str:
    """下書きに渡す文体の指示(§6.3.4)。

    軸がプリセットの値なら、手書きの文体カードを返す。
    それ以外(custom)は、軸ごとの極の指示文を合成する。中間(0.35〜0.65)の軸は入れない。
    """
    if is_preset_axes(axes):
        type_id = type_id_from_axes(axes)
        assert type_id is not None
        return _TYPES[type_id][1]
    parts = []
    for axis in AXES:
        low, high = _AXIS_DIRECTIVES[axis]
        if axes[axis] >= DIRECTIVE_HIGH:
            parts.append(high)
        elif axes[axis] <= DIRECTIVE_LOW:
            parts.append(low)
    return "".join(parts)
