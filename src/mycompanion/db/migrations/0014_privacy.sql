-- =====================================================================
-- 0014_privacy: 個人情報・ストア要件の残り(本体仕様書 §13.3、§13.4.5、§13.5.8)
--
-- - response_reports: ユーザーによる、AI の応答の報告(不適切な応答など)。フィルタの改善に使う。
--   会話の本文は持たない(どのターンか、理由の種類、本人が書いたメモだけ)。
-- - 監査ログ(audit_logs): アカウントを削除するとき、その人の user_id を、元に戻せない別の値に置き換える。
--   保持期間を過ぎた行を消す関数を置く(期間は、設定値。法務の確認で変わりうる)。
-- =====================================================================

CREATE TABLE response_reports (
    report_id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      uuid NOT NULL REFERENCES users ON DELETE CASCADE,
    character_id text NOT NULL,
    session_id   uuid,
    turn_no      integer,
    reason       text NOT NULL CHECK (reason IN ('inappropriate', 'harmful', 'incorrect', 'other')),
    note         text CHECK (note IS NULL OR char_length(note) <= 500),
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX response_reports_created_idx ON response_reports (created_at);

ALTER TABLE response_reports ENABLE ROW LEVEL SECURITY;
CREATE POLICY response_reports_isolation ON response_reports FOR ALL TO mycompanion_app
    USING (user_id = app_user_id() AND character_id = app_character_id())
    WITH CHECK (user_id = app_user_id() AND character_id = app_character_id());
GRANT SELECT, INSERT ON response_reports TO mycompanion_app;

-- 自分のアカウントの全データを削除する(0007 の置き換え)。
-- 監査ログと削除の記録は残すが、user_id は、元に戻せない別の値(その場で作る乱数の ID)に置き換える。
CREATE OR REPLACE FUNCTION delete_my_account() RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_user_id uuid := app_user_id();
    v_alias   uuid := gen_random_uuid();
BEGIN
    IF v_user_id IS NULL THEN
        RAISE EXCEPTION 'ユーザーが設定されていません';
    END IF;
    INSERT INTO deletion_requests (user_id, scope, status, completed_at)
    VALUES (v_user_id, 'all', 'completed', now());
    INSERT INTO audit_logs (actor_type, actor_id, action, target_user_id)
    VALUES ('user', v_user_id::text, 'account_deleted', v_user_id);
    DELETE FROM users WHERE user_id = v_user_id;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    UPDATE audit_logs
       SET target_user_id = CASE WHEN target_user_id = v_user_id THEN v_alias ELSE target_user_id END,
           actor_id = CASE WHEN actor_id = v_user_id::text THEN v_alias::text ELSE actor_id END
     WHERE target_user_id = v_user_id OR actor_id = v_user_id::text;
    UPDATE deletion_requests SET user_id = v_alias WHERE user_id = v_user_id;
    RETURN true;
END
$$;

-- 保持期間を過ぎた監査ログを消す(ワーカーが、定期的に呼ぶ)。消した行数を返す
CREATE OR REPLACE FUNCTION purge_audit_logs(p_before timestamptz) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_count integer;
BEGIN
    DELETE FROM audit_logs WHERE ts < p_before;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count;
END
$$;

REVOKE ALL ON FUNCTION purge_audit_logs(timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION purge_audit_logs(timestamptz) TO mycompanion_app;
