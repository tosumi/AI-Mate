-- =====================================================================
-- 0004_memory: 記憶の実装に必要な追加(本体仕様書 §9、§11.5.4)
--
-- - memory.last_reactivated_at: 再活性化のクールダウンの判定(§9.9.4)。
-- - スケジューラ用の関数: 整理の対象と、無入力のセッションを見つける。
--   スケジューラは、どのユーザーとして動くわけでもないので、行レベルセキュリティの下では対象を探せない。
--   この関数だけが、ユーザーをまたいで ID と日付を返す。**記憶の内容・会話の内容は返さない。**
--   実際の整理は、返ってきた (user_id, character_id) のスコープを設定して、RLS の下で行う。
-- =====================================================================

ALTER TABLE memory ADD COLUMN last_reactivated_at timestamptz;

CREATE INDEX memory_short_idx ON memory (source_day) WHERE tier = 'short' AND deleted_at IS NULL;
CREATE INDEX memory_origin_idx ON memory (origin_memory_id) WHERE origin_memory_id IS NOT NULL;

-- 短期記憶を持つ (user, character) と、その最も古い日
CREATE OR REPLACE FUNCTION list_consolidation_targets()
RETURNS TABLE (user_id uuid, character_id text, timezone text, oldest_day date)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT m.user_id, m.character_id, coalesce(s.timezone, 'Asia/Tokyo'), min(m.source_day)
      FROM memory m
      LEFT JOIN user_settings s ON s.user_id = m.user_id
     WHERE m.tier = 'short' AND m.deleted_at IS NULL AND m.source_day IS NOT NULL
     GROUP BY m.user_id, m.character_id, s.timezone
$$;

-- 記憶を持つ (user, character) の一覧(月次整理と、期限切れの削除の対象)
CREATE OR REPLACE FUNCTION list_memory_scopes()
RETURNS TABLE (user_id uuid, character_id text, timezone text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT DISTINCT m.user_id, m.character_id, coalesce(s.timezone, 'Asia/Tokyo')
      FROM memory m
      LEFT JOIN user_settings s ON s.user_id = m.user_id
     WHERE m.deleted_at IS NULL
$$;

-- 無入力が続いている、進行中のセッション
CREATE OR REPLACE FUNCTION list_idle_sessions(p_cutoff timestamptz)
RETURNS TABLE (user_id uuid, character_id text, session_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT s.user_id, s.character_id, s.session_id
      FROM sessions s
     WHERE s.status = 'active' AND s.last_activity_at < p_cutoff
$$;

REVOKE ALL ON FUNCTION list_consolidation_targets() FROM PUBLIC;
REVOKE ALL ON FUNCTION list_memory_scopes() FROM PUBLIC;
REVOKE ALL ON FUNCTION list_idle_sessions(timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION list_consolidation_targets() TO mycompanion_app;
GRANT EXECUTE ON FUNCTION list_memory_scopes() TO mycompanion_app;
GRANT EXECUTE ON FUNCTION list_idle_sessions(timestamptz) TO mycompanion_app;

-- user_state の baseline を、アプリが更新できるようにする(0001 で付与済みだが、明示する)
GRANT SELECT, INSERT, UPDATE ON user_state TO mycompanion_app;
