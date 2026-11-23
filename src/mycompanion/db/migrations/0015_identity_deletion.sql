-- =====================================================================
-- 0015_identity_deletion: 外部テナント(Entra External ID)のアカウントの削除(本体仕様書 §15.1 の M6-3)
--
-- - アカウントの全データ削除のとき、サインインの情報(外部テナントのユーザー)の削除を、
--   deletion_requests に「保留中」(scope = identity)として記録する。
--   外部テナントのユーザーのオブジェクト ID は、削除が終わるまでだけ details_json に持ち、完了したら消す。
-- - スケジューラが保留中の行を読み、ワーカーが Microsoft Graph で削除して、完了にする。
--   失敗したら、次の回に計画し直される(キューの再配信には頼らない)。
-- =====================================================================

-- 自分のアカウントの全データを削除する(0014 の置き換え。外部テナントのアカウントの削除の記録を足した)
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
    -- サインインの情報の削除は、ワーカーが行う(保留中として記録する)
    INSERT INTO deletion_requests (user_id, scope, status, details_json)
    SELECT v_user_id, 'identity', 'pending', jsonb_build_object('idp', idp, 'idp_subject', idp_subject)
      FROM identity_links
     WHERE user_id = v_user_id AND idp = 'entra_external_id';
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

-- 保留中の、外部テナントのアカウントの削除(ワーカーが読む)
CREATE OR REPLACE FUNCTION pending_identity_deletions() RETURNS TABLE (idp_subject text)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT DISTINCT details_json->>'idp_subject'
      FROM deletion_requests
     WHERE scope = 'identity' AND status = 'pending' AND details_json ? 'idp_subject'
$$;

-- 外部テナントのアカウントの削除を、完了にする。オブジェクト ID は、記録から消す
CREATE OR REPLACE FUNCTION complete_identity_deletion(p_subject text) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_count integer;
BEGIN
    UPDATE deletion_requests
       SET status = 'completed', completed_at = now(), details_json = details_json - 'idp_subject'
     WHERE scope = 'identity' AND status = 'pending' AND details_json->>'idp_subject' = p_subject;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count;
END
$$;

REVOKE ALL ON FUNCTION pending_identity_deletions() FROM PUBLIC;
REVOKE ALL ON FUNCTION complete_identity_deletion(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION pending_identity_deletions() TO mycompanion_app;
GRANT EXECUTE ON FUNCTION complete_identity_deletion(text) TO mycompanion_app;
