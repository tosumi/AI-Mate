-- =====================================================================
-- 0002_persona_design: ペルソナの設計用のデータ(ペルソナデザイナ仕様書 §14)
--
-- 性格タイプは、キャラクタの設計用の分類。ユーザーごとのデータではない。
-- アプリのロールは、読み取りだけができる(登録は、管理者ロールで行う)。
-- =====================================================================

-- 公開時のバージョン番号(X.Y.Z)。characters.version は、内部の通し番号として使う
ALTER TABLE characters ADD COLUMN version_label text;

-- 16タイプの定義(データファイルから取り込む)
CREATE TABLE personality_types (
    type_id      text PRIMARY KEY,
    name         text NOT NULL,
    axes_json    jsonb NOT NULL,
    style_card   text NOT NULL,
    summary      text NOT NULL DEFAULT '',
    data_version integer NOT NULL
);

-- 診断の質問バンク
CREATE TABLE quiz_bank (
    bank_version integer NOT NULL,
    question_id  text NOT NULL,
    axis         text NOT NULL CHECK (axis IN ('i', 'a', 'e', 'f')),
    keyed_pole   text NOT NULL,
    reverse      boolean NOT NULL,
    text         text NOT NULL,
    is_tiebreak  boolean NOT NULL DEFAULT false,
    PRIMARY KEY (bank_version, question_id)
);

-- 公開前のレビューの記録
CREATE TABLE character_reviews (
    review_id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    character_id          text NOT NULL,
    version               integer NOT NULL,
    reviewer              text,
    reviewed_at           timestamptz,
    lint_report_json      jsonb NOT NULL DEFAULT '{}'::jsonb,
    simulation_report_ref text,
    notes                 text,
    decision              text
);
CREATE INDEX character_reviews_idx ON character_reviews (character_id, version);

-- 設計用のテーブル: アプリは読み取りのみ(レビューの記録は読ませない)
ALTER TABLE personality_types ENABLE ROW LEVEL SECURITY;
CREATE POLICY personality_types_read ON personality_types FOR SELECT TO mycompanion_app USING (true);
GRANT SELECT ON personality_types TO mycompanion_app;

ALTER TABLE quiz_bank ENABLE ROW LEVEL SECURITY;
CREATE POLICY quiz_bank_read ON quiz_bank FOR SELECT TO mycompanion_app USING (true);
GRANT SELECT ON quiz_bank TO mycompanion_app;

ALTER TABLE character_reviews ENABLE ROW LEVEL SECURITY;
