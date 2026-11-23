-- =====================================================================
-- 0010_likes_dislikes: 好き嫌いシート(ペルソナデザイナ仕様書 §5.4.0)
--
-- - characters.likes_dislikes_json: 設計用の元データ(好みカタログから選んだ項目。カタログの版を含む)。
--   エンジンが読むのは、公開時に解決した値(appraisal_rules_json の origin: sheet、preferences_json)。
--   ユーザーのデータを持たない。
-- - シート由来のルールの、セッション内の回数・累計・回避の状態は、companion_state.last_triggers_json に持つ
--   (既存の列。(user_id, character_id) で分離済み)。新しいテーブルは作らない。
-- =====================================================================

ALTER TABLE characters ADD COLUMN likes_dislikes_json jsonb;
