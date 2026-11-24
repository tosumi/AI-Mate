-- =====================================================================
-- 0021_ops_reports: 運営者による報告の確認・監査ログの閲覧(本体仕様書 §15.1 の M9 の手順2)
--
-- - response_reports に、対応状況(未対応・対応中・対応済み)と、運営者の対応メモ(本文を書かない運用。500字まで)、
--   更新者(運営者のオブジェクト ID)・更新日時を足す。ユーザーのロールからは、これまでどおり読み書きできない列は
--   変えない(アプリのロールの権限は SELECT, INSERT のまま。更新は下の関数だけ)。
-- - 運営者の API は、下の関数(SECURITY DEFINER)だけを使う。関数は、会話の本文の列を持つテーブルを読まない
--   (会話の本文は Cosmos DB にあり、ここにはない)。一般ユーザーの行レベルセキュリティは緩めない。
-- - user_id は、API の中でだけ使い(ガードレールの記録を引くため)、応答では仮の ID に置き換える。
-- =====================================================================

ALTER TABLE response_reports
    ADD COLUMN status         text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'in_progress', 'resolved')),
    ADD COLUMN ops_memo       text CHECK (ops_memo IS NULL OR char_length(ops_memo) <= 500),
    ADD COLUMN ops_updated_by text,
    ADD COLUMN ops_updated_at timestamptz;
CREATE INDEX response_reports_status_idx ON response_reports (status, created_at);

-- 監査ログを、時刻の順に読む(閲覧用)
CREATE INDEX audit_logs_ts_idx ON audit_logs (ts);

-- 報告の一覧(新しい順)。ユーザーのメモは返さない(あるかどうかだけ)
CREATE OR REPLACE FUNCTION ops_list_reports(p_status text, p_before timestamptz, p_limit integer)
RETURNS TABLE (
    report_id uuid, user_id uuid, character_id text, session_id uuid, turn_no integer, reason text,
    has_note boolean, status text, created_at timestamptz, ops_updated_at timestamptz
)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT r.report_id, r.user_id, r.character_id, r.session_id, r.turn_no, r.reason,
           r.note IS NOT NULL AND r.note <> '', r.status, r.created_at, r.ops_updated_at
      FROM response_reports r
     WHERE (p_status IS NULL OR r.status = p_status)
       AND (p_before IS NULL OR r.created_at < p_before)
     ORDER BY r.created_at DESC
     LIMIT least(greatest(coalesce(p_limit, 50), 1), 200)
$$;

-- 報告の詳細(本人のメモと、運営者の対応メモを含む)
CREATE OR REPLACE FUNCTION ops_get_report(p_report_id uuid)
RETURNS TABLE (
    report_id uuid, user_id uuid, character_id text, session_id uuid, turn_no integer, reason text, note text,
    status text, ops_memo text, ops_updated_by text, ops_updated_at timestamptz, created_at timestamptz
)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT r.report_id, r.user_id, r.character_id, r.session_id, r.turn_no, r.reason, r.note,
           r.status, r.ops_memo, r.ops_updated_by, r.ops_updated_at, r.created_at
      FROM response_reports r
     WHERE r.report_id = p_report_id
$$;

-- 対応状況・対応メモの更新。null の項目は変えない。メモを空文字にすると消す。更新できたら true
CREATE OR REPLACE FUNCTION ops_update_report(
    p_report_id uuid, p_status text, p_memo text, p_operator text, p_now timestamptz
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    IF p_operator IS NULL OR p_operator = '' THEN
        RAISE EXCEPTION '運営者が指定されていません';
    END IF;
    UPDATE response_reports
       SET status = coalesce(p_status, status),
           ops_memo = CASE WHEN p_memo IS NULL THEN ops_memo ELSE nullif(p_memo, '') END,
           ops_updated_by = p_operator,
           ops_updated_at = p_now
     WHERE report_id = p_report_id;
    RETURN FOUND;
END
$$;

-- 監査ログの閲覧(新しい順)。追記のみの表を、読むだけ
CREATE OR REPLACE FUNCTION ops_list_audit_logs(
    p_since timestamptz, p_until timestamptz, p_actor_type text, p_before timestamptz, p_limit integer
)
RETURNS TABLE (
    log_id uuid, ts timestamptz, actor_type text, actor_id text, action text, target_user_id uuid, details_json jsonb
)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT a.log_id, a.ts, a.actor_type, a.actor_id, a.action, a.target_user_id, a.details_json
      FROM audit_logs a
     WHERE (p_since IS NULL OR a.ts >= p_since)
       AND (p_until IS NULL OR a.ts < p_until)
       AND (p_actor_type IS NULL OR a.actor_type = p_actor_type)
       AND (p_before IS NULL OR a.ts < p_before)
     ORDER BY a.ts DESC
     LIMIT least(greatest(coalesce(p_limit, 100), 1), 500)
$$;

REVOKE ALL ON FUNCTION ops_list_reports(text, timestamptz, integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION ops_get_report(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION ops_update_report(uuid, text, text, text, timestamptz) FROM PUBLIC;
REVOKE ALL ON FUNCTION ops_list_audit_logs(timestamptz, timestamptz, text, timestamptz, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ops_list_reports(text, timestamptz, integer) TO mycompanion_app;
GRANT EXECUTE ON FUNCTION ops_get_report(uuid) TO mycompanion_app;
GRANT EXECUTE ON FUNCTION ops_update_report(uuid, text, text, text, timestamptz) TO mycompanion_app;
GRANT EXECUTE ON FUNCTION ops_list_audit_logs(timestamptz, timestamptz, text, timestamptz, integer) TO mycompanion_app;
