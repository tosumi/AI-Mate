-- =====================================================================
-- 0018_proactive: 無入力時の自発的な発話(本体仕様書 §8.6、§15.1 の M8)
--
-- - user_settings.proactive_enabled: キャラクタから話しかけてよいか(既定オフ。ユーザーが明示的に許可したときだけ)。
-- - sessions.awaiting_reply_until: 自発発話で始めたセッションは、ユーザーの返事を待つあいだ、無入力でも閉じない
--   (返事が、同じセッションの続きになるように)。ユーザーが話すと消す。
-- - proactive_state((user, character) で分離): 返事のない自発発話の数、最後の自発発話。本文は持たない。
-- - proactive_quota(ユーザー単位): ユーザーのローカル日ごとの、すべてのキャラクタを合わせた回数だけ。
--   どのキャラクタが話しかけたかは持たない(キャラクタをまたぐ情報を増やさない。§13.4.6)。
-- - list_proactive_candidates: スケジューラ用。許可したユーザーの、しばらく会話のない組の ID とタイムゾーンだけを返す。
-- - アカウントの削除で消える(users の外部キー)。
-- =====================================================================

ALTER TABLE user_settings ADD COLUMN proactive_enabled boolean NOT NULL DEFAULT false;

ALTER TABLE sessions ADD COLUMN awaiting_reply_until timestamptz;

CREATE TABLE proactive_state (
    user_id         uuid NOT NULL REFERENCES users ON DELETE CASCADE,
    character_id    text NOT NULL,
    unanswered      integer NOT NULL DEFAULT 0 CHECK (unanswered >= 0),   -- 返事のない自発発話の数
    last_sent_at    timestamptz,
    last_session_id uuid,               -- 最後の自発発話のセッション(画面に表示するため)
    last_turn_no    integer,
    seen_at         timestamptz,        -- 画面で表示した時刻(まだなら NULL)
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, character_id)
);
ALTER TABLE proactive_state ENABLE ROW LEVEL SECURITY;
CREATE POLICY proactive_state_isolation ON proactive_state FOR ALL TO mycompanion_app
    USING (user_id = app_user_id() AND character_id = app_character_id())
    WITH CHECK (user_id = app_user_id() AND character_id = app_character_id());
GRANT SELECT, INSERT, UPDATE, DELETE ON proactive_state TO mycompanion_app;

CREATE TABLE proactive_quota (
    user_id    uuid PRIMARY KEY REFERENCES users ON DELETE CASCADE,
    local_day  date NOT NULL,           -- ユーザーのローカル日
    sent       integer NOT NULL DEFAULT 0 CHECK (sent >= 0),
    updated_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE proactive_quota ENABLE ROW LEVEL SECURITY;
CREATE POLICY proactive_quota_isolation ON proactive_quota FOR ALL TO mycompanion_app
    USING (user_id = app_user_id()) WITH CHECK (user_id = app_user_id());
GRANT SELECT, INSERT, UPDATE ON proactive_quota TO mycompanion_app;

-- 無入力が続いている、進行中のセッション(0004 の置き換え。返事を待っている自発発話のセッションは除く)
CREATE OR REPLACE FUNCTION list_idle_sessions(p_cutoff timestamptz)
RETURNS TABLE (user_id uuid, character_id text, session_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT s.user_id, s.character_id, s.session_id
      FROM sessions s
     WHERE s.status = 'active' AND s.last_activity_at < p_cutoff
       AND (s.awaiting_reply_until IS NULL OR s.awaiting_reply_until < now())
$$;

-- 自発発話の候補: 許可したユーザーで、最後の会話が p_before より前で、進行中のセッションがない組
CREATE OR REPLACE FUNCTION list_proactive_candidates(p_before timestamptz)
RETURNS TABLE (user_id uuid, character_id text, timezone text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT s.user_id, s.character_id, coalesce(u.timezone, 'Asia/Tokyo')
      FROM sessions s
      JOIN user_settings u ON u.user_id = s.user_id AND u.proactive_enabled
     GROUP BY s.user_id, s.character_id, u.timezone
    HAVING max(s.last_activity_at) < p_before AND bool_and(s.status = 'closed')
$$;

REVOKE ALL ON FUNCTION list_proactive_candidates(timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION list_proactive_candidates(timestamptz) TO mycompanion_app;
