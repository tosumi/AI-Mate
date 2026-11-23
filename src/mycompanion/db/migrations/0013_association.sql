-- =====================================================================
-- 0013_association: 関係をたどる連想(本体仕様書 §9.13 の第3段階)
--
-- - user_settings.association_enabled: キャラクタが、連想で話題を移してよいか(既定は on。`/settings association`)。
--   off でも、ユーザーが話題を移したときに、キャラクタがつながりに気づくことは、できる。
-- - トピック同士の関係は、文書(topic_doc.frontmatter の mc_relations)に持つ。新しい表は、作らない。
-- - 話題の焦点・訪問済み・連鎖の数は、セッションの状態(companion_state.last_triggers_json の topic_state)に持つ。
-- =====================================================================

ALTER TABLE user_settings ADD COLUMN association_enabled boolean NOT NULL DEFAULT true;
