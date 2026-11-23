-- =====================================================================
-- 0009_event_rules: イベントルール(本体仕様書 §7.1 の④)
--
-- - characters.event_rules_json: キャラクタごとの、出来事への反応の定義(ユーザーのデータを持たない)。
-- - companion_rule_counters: ルールごとの発火のカウンタ。逓減と回数の上限のためだけに使う。
--   ユーザーの傾向として、集計・保存しない。(user_id, character_id) ごとに分離する。
-- =====================================================================

ALTER TABLE characters ADD COLUMN event_rules_json jsonb NOT NULL DEFAULT '[]'::jsonb;

CREATE TABLE companion_rule_counters (
    user_id             uuid NOT NULL REFERENCES users ON DELETE CASCADE,
    character_id        text NOT NULL,
    rule_id             text NOT NULL,
    session_id          uuid,                  -- セッション単位の値(回数、段数、最後のターン)が、どのセッションのものか
    fired_count_session integer NOT NULL DEFAULT 0,
    saturation_n        integer NOT NULL DEFAULT 0,   -- 逓減の段数
    last_fired_turn     integer,
    last_fired_at       timestamptz,
    fired_day           date,                  -- ユーザーのローカル日付
    fired_count_day     integer NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, character_id, rule_id)
);

ALTER TABLE companion_rule_counters ENABLE ROW LEVEL SECURITY;
CREATE POLICY companion_rule_counters_isolation ON companion_rule_counters FOR ALL TO mycompanion_app
    USING (user_id = app_user_id() AND character_id = app_character_id())
    WITH CHECK (user_id = app_user_id() AND character_id = app_character_id());
GRANT SELECT, INSERT, UPDATE, DELETE ON companion_rule_counters TO mycompanion_app;
