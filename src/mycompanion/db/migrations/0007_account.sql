-- =====================================================================
-- 0007_account: アカウントの書き出しと削除(本体仕様書 §11.5.5、§13.4.5)
--
-- アプリのロールは、行レベルセキュリティの下で、(user_id, character_id) の組ごとにしかデータを読めない。
-- 自分のデータの書き出しと削除は、キャラクタをまたぐので、専用の関数で行う。
-- どちらの関数も、**現在のリクエストのユーザー本人**(app_user_id())に対してだけ動く。
-- =====================================================================

-- 自分がデータを持っているキャラクタの一覧(書き出しで、キャラクタごとに読むために使う)
CREATE OR REPLACE FUNCTION list_my_characters()
RETURNS TABLE (character_id text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT character_id FROM sessions WHERE user_id = app_user_id()
    UNION SELECT character_id FROM memory WHERE user_id = app_user_id()
    UNION SELECT character_id FROM relationships WHERE user_id = app_user_id()
$$;

-- 自分のアカウントの全データを削除する。
-- users を消すと、会話の状態・記憶・設定・同意・連携は、外部キーの CASCADE で消える。
-- 監査ログ(audit_logs)は残す(本文や感情の値は含まない。保持期間は法務の確認で決める)。
CREATE OR REPLACE FUNCTION delete_my_account() RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_user_id uuid := app_user_id();
BEGIN
    IF v_user_id IS NULL THEN
        RAISE EXCEPTION 'ユーザーが設定されていません';
    END IF;
    INSERT INTO deletion_requests (user_id, scope, status, completed_at)
    VALUES (v_user_id, 'all', 'completed', now());
    INSERT INTO audit_logs (actor_type, actor_id, action, target_user_id)
    VALUES ('user', v_user_id::text, 'account_deleted', v_user_id);
    DELETE FROM users WHERE user_id = v_user_id;
    RETURN FOUND;
END
$$;

REVOKE ALL ON FUNCTION list_my_characters() FROM PUBLIC;
REVOKE ALL ON FUNCTION delete_my_account() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION list_my_characters() TO mycompanion_app;
GRANT EXECUTE ON FUNCTION delete_my_account() TO mycompanion_app;
