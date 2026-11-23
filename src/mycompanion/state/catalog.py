"""好みカタログ(好き嫌いシートの選択肢。`likes-dislikes-sheet-review-rev3.md` §2)。

キャラクタの「すきなもの・こと」「嫌いなもの・こと」は、ここから選ぶ(自由記述にしない)。
選択肢を閉じることで、人の属性・集団、宗教行事、年齢制限のあるもの、恋愛的なもの、重い出来事、
義務的な活動(仕事・勉強など)への好悪を、**選べないこと自体で防ぐ**。

- 区分は6つ: 色、食べ物、イベント(=「もの」)、生活、趣味・活動、動作・状況(=「こと」)。
- 区分ごとに、好き・嫌いの選択肢は 16 ずつ(イベントは全17項目。誕生日会は好きのみ、周年祝いは嫌いのみ)。
- 「照合のヒント」「照合しない例」は、文脈評価のプロンプトと、評価セットの両方に使う。
- 項目を足す・表示名を変えるときは、`CATALOG_VERSION` を上げる。削除はせず、非推奨にする
  (公開済みのキャラクタは、公開時に解決したルールのまま動く)。
"""

from __future__ import annotations

from dataclasses import dataclass

CATALOG_VERSION = 2
SUPPORTED_CATALOG_VERSIONS = (2,)

CATEGORIES = ("color", "food", "event", "life", "hobby", "action")
THING_CATEGORIES = ("color", "food", "event")  # もの
ACTIVITY_CATEGORIES = ("life", "hobby", "action")  # こと
CATEGORY_LABELS = {
    "color": "色", "food": "食べ物", "event": "イベント",
    "life": "生活", "hobby": "趣味・活動", "action": "動作・状況",
}  # fmt: skip


@dataclass(frozen=True)
class CatalogItem:
    id: str
    label: str
    include: tuple[str, ...] = ()  # 照合のヒント(含む)
    exclude: tuple[str, ...] = ()  # 照合しない例
    likeable: bool = True
    dislikeable: bool = True
    involvement: bool = False  # 関わり方の判定(ユーザーとキャラクタの間の行為か)を使う

    @property
    def category(self) -> str:
        return self.id.split(".", 1)[0]


def _item(item_id: str, label: str, include: str = "", exclude: str = "", **flags: bool) -> CatalogItem:
    def split(text: str) -> tuple[str, ...]:
        return tuple(part for part in text.split("、") if part)

    return CatalogItem(item_id, label, split(include), split(exclude), **flags)


_ITEMS = (
    # --- 色 ---
    _item("color.red", "赤", "真っ赤、赤いもの", "赤字、赤の他人、真っ赤な嘘"),
    _item("color.blue", "青", "青いもの、ブルー", "青春、青二才"),
    _item("color.yellow", "黄色", "黄色いもの、イエロー", "黄色い声"),
    _item("color.green", "緑", "緑色、グリーン(色として)", "緑化(植物の意味)"),
    _item("color.white", "白", "白いもの、真っ白", "白状、白黒つける、白紙に戻す"),
    _item("color.black", "黒", "黒いもの、真っ黒", "黒字、黒幕、白黒つける"),
    _item("color.pink", "ピンク", "桃色"),
    _item("color.purple", "紫", "紫色、パープル"),
    _item("color.orange", "オレンジ色", "橙色", "オレンジ(果物)"),
    _item("color.light_blue", "水色", "空色"),
    _item("color.brown", "茶色", "ブラウン", "茶番、お茶(飲み物)"),
    _item("color.gray", "灰色", "グレー", "グレーゾーン"),
    _item("color.gold", "金色", "ゴールド", "金(お金)、金曜日"),
    _item("color.silver", "銀色", "シルバー", "シルバー世代"),
    _item("color.navy", "紺色", "ネイビー"),
    _item("color.yellow_green", "黄緑", "ライムグリーン"),
    # --- 食べ物 ---
    _item("food.apple", "りんご", "アップルパイ、りんごジュース", "Apple(会社)、りんご色のほっぺ(比喩)"),
    _item("food.mandarin", "みかん", "柑橘"),
    _item("food.strawberry", "いちご", "いちご大福、いちごミルク"),
    _item("food.banana", "バナナ"),
    _item("food.chocolate", "チョコレート", "チョコ菓子"),
    _item("food.cake", "ケーキ", "ショートケーキ"),
    _item("food.ice_cream", "アイスクリーム", "ソフトクリーム"),
    _item("food.bread", "パン", "食パン、パン屋"),
    _item("food.onigiri", "おにぎり", "おむすび"),
    _item("food.ramen", "ラーメン"),
    _item("food.curry", "カレーライス", "カレー"),
    _item("food.sushi", "寿司", "回転寿司"),
    _item("food.fish", "魚料理", "焼き魚、煮魚、魚介", "魚(生きている魚、ペット)"),
    _item("food.natto", "納豆"),
    _item("food.green_pepper", "ピーマン"),
    _item("food.tomato", "トマト", "ミニトマト"),
    # --- イベント(全17項目。好き16・嫌い16) ---
    _item("event.hanami", "お花見", "桜を見に行く"),
    _item("event.fireworks", "花火大会", "花火"),
    _item("event.summer_festival", "夏祭り", "縁日、屋台"),
    _item("event.beach", "海水浴", "海で泳ぐ"),
    _item("event.camping", "キャンプ", "バーベキュー"),
    _item("event.autumn_leaves", "紅葉狩り", "紅葉を見に行く"),
    _item("event.snow_play", "雪遊び", "雪合戦、雪だるま"),
    _item("event.moon_viewing", "お月見", "十五夜"),
    # ユーザー自身の祝い事になりやすい。嫌いにすると、ユーザーの誕生日の話に嫌悪で反応する
    _item("event.birthday_party", "誕生日会", "誕生日のお祝い", dislikeable=False),
    # 会社・店・団体の周年行事だけ。個人の記念日・命日は、照合しない
    _item("event.anniversary", "周年祝い", "創立記念、開店記念、〇周年のイベント", "結婚記念日、交際記念日、誕生日、命日", likeable=False),
    _item("event.amusement_park", "遊園地", "テーマパーク"),
    _item("event.zoo_aquarium", "動物園・水族館"),
    _item("event.picnic", "ピクニック"),
    _item("event.travel", "旅行", "旅、遠出", "出張(仕事)"),
    _item("event.concert", "ライブ・コンサート", "フェス"),
    _item("event.sports_watching", "スポーツ観戦", "試合を見に行く"),
    _item("event.cleaning", "大掃除", "", "掃除当番(日常)"),
    # --- 生活 ---
    _item("life.cooking", "料理", "ご飯を作る、お菓子作り", "料理番組を見る"),
    _item("life.cleaning", "掃除", "部屋を掃除する", "大掃除"),
    _item("life.laundry", "洗濯", "洗濯物を干す"),
    _item("life.tidying", "片付け", "整理整頓"),
    _item("life.shopping", "買い物", "ショッピング"),
    _item("life.early_rising", "早起き", "朝早く起きる"),
    _item("life.staying_up", "夜更かし", "遅くまで起きている", "夜勤(仕事)"),
    _item("life.napping", "昼寝", "うたた寝"),
    _item("life.bathing", "お風呂", "湯船につかる、温泉"),
    _item("life.walking", "散歩", "ぶらぶら歩く"),
    _item("life.plants", "植物の世話", "水やり、園芸"),
    _item("life.pet_care", "ペットの世話", "犬の散歩、えさやり"),
    _item("life.redecorating", "模様替え", "部屋の配置を変える"),
    _item("life.diary", "日記を書く", "手帳をつける"),
    _item("life.house_sitting", "留守番", "家で待つ"),
    _item("life.lazing", "のんびり過ごす", "ごろごろする、何もしない日"),
    # --- 趣味・活動 ---
    _item("hobby.reading", "読書", "本を読む、漫画", "教科書・資料を読む(勉強・仕事)"),
    _item("hobby.movies", "映画を観る", "映画館、配信で映画"),
    _item("hobby.music_listening", "音楽を聴く", "", "ライブ"),
    _item("hobby.singing", "歌う", "カラオケ、鼻歌"),
    _item("hobby.instrument", "楽器を弾く", "ピアノ、ギター"),
    _item("hobby.drawing", "絵を描く", "イラスト、スケッチ"),
    _item("hobby.photography", "写真を撮る", "カメラ"),
    _item("hobby.video_games", "テレビゲーム", "ゲーム機、スマホゲーム"),
    _item("hobby.board_games", "ボードゲーム", "将棋、トランプ"),
    _item("hobby.sports", "スポーツをする", "運動、ジョギング", "スポーツ観戦"),
    _item("hobby.dancing", "ダンス", "踊る"),
    _item("hobby.crafts", "手芸・工作", "編み物、プラモデル"),
    _item("hobby.fishing", "釣り"),
    _item("hobby.hiking", "山歩き", "登山、ハイキング"),
    _item("hobby.stargazing", "星を見る", "天体観測"),
    _item("hobby.puzzles", "パズル", "クロスワード、ジグソーパズル"),
    # --- 動作・状況。身体の接触は「なでる」だけ(恋愛的・性的な方向へ広げない) ---
    _item("action.pat", "なでる・なでられる", "イイコイイコ、よしよし、頭をなでる", "性的な文脈", involvement=True),
    _item("action.surprise", "驚かされる", "サプライズ、どっきり", involvement=True),
    _item("action.public_speaking", "人前で話す", "発表、スピーチ、プレゼン"),
    _item("action.nervous", "緊張する", "あがる、ドキドキする(不安で)", "ドキドキ(期待・恋愛の意味)"),
    _item("action.waiting", "待つ・待たされる", "行列に並ぶ、待ち合わせで待つ", involvement=True),
    _item("action.rushed", "急かされる", "せかされる、締め切りに追われる", involvement=True),
    _item("action.crowds", "人混み", "満員電車、混雑"),
    _item("action.loud_noise", "大きな音", "雷、工事の音"),
    _item("action.quiet_place", "静かな場所で過ごす", "図書館、静かなカフェ"),
    _item("action.rainy_day", "雨の日", "雨音、梅雨"),
    _item("action.hot_day", "暑い日", "猛暑、夏の日差し"),
    _item("action.cold_day", "寒い日", "冬の朝、雪の日"),
    _item("action.heights", "高い所", "展望台、観覧車"),
    _item("action.dark", "暗い所", "夜道、停電"),
    _item("action.vehicles", "乗り物で移動する", "電車、バス、ドライブ", "通勤(仕事)"),
    _item("action.chatting", "おしゃべりする", "雑談、世間話", involvement=True),
)

CATALOG: dict[str, CatalogItem] = {item.id: item for item in _ITEMS}

# 嫌いに選んでも、ユーザーとキャラクタの間の行為(with_character)では発火させない項目。
# 「おしゃべりが嫌い」なキャラクタが、ユーザーとの会話そのものに嫌悪を起こさないようにする(エンジンの固定の規則)
NO_DISLIKE_WITH_CHARACTER = ("action.chatting",)
# 嫌いに選んだとき、表出でユーザーの好意を拒絶しないよう、Planner の avoid に加える項目
AFFECTION_ITEMS = ("action.pat",)


def items(category: str | None = None) -> list[CatalogItem]:
    return [item for item in _ITEMS if category is None or item.category == category]


def get(item_id: str) -> CatalogItem | None:
    return CATALOG.get(item_id)


def note_text(item: CatalogItem) -> str:
    """文脈評価のプロンプトに渡す、1項目の補足(照合のヒント + 照合しない例)。なければ空文字。"""
    parts = []
    if item.include:
        parts.append(f"含む: {'、'.join(item.include)}")
    if item.exclude:
        parts.append(f"含まない: {'、'.join(item.exclude)}")
    return " / ".join(parts)
