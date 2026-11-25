-- =====================================================================
-- 0022_persona_publish: 運営者の画面からのキャラクタの提出・公開(本体仕様書 §15.1 の M10 の手順4)
--
-- - 提出(設計者): 定義(YAML を読んだもの)と、提出のときの検査の結果を persona_submissions に残す。
--   一般ユーザーの下書きは、ここには入らない(一般ユーザーは YAML ファイルだけ。2026-11-25 の決定)。
-- - 公開(承認者): 提出した本人は承認できない(関数の中でも確かめる)。公開の版は、これまでの公開版より大きいこと。
--   characters に新しい行を足し、character_reviews に承認の記録を残す(CLI の persona publish と同じ形)。
-- - ロールバック・非推奨も、関数で行う。アプリのロールは、characters を直接は変えられない(SELECT のまま)。
-- - 関数は PUBLIC から外し、アプリのロールだけに許す。
-- =====================================================================

CREATE TABLE persona_submissions (
    submission_id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    character_id     text NOT NULL CHECK (character_id ~ '^[A-Za-z0-9_]+$'),
    version_label    text NOT NULL CHECK (version_label ~ '^[0-9]+\.[0-9]+\.[0-9]+$'),
    definition_json  jsonb NOT NULL,           -- 提出された定義(YAML を読んだもの)
    character_row    jsonb NOT NULL,           -- 公開するときの characters の行(解決済みの値を含む)
    lint_json        jsonb NOT NULL DEFAULT '[]'::jsonb,
    simulation_summary text NOT NULL DEFAULT '',
    submitted_by     text NOT NULL,            -- 運営者のオブジェクト ID
    submitted_at     timestamptz NOT NULL,
    status           text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    decided_by       text,
    decided_at       timestamptz,
    notes            text CHECK (notes IS NULL OR char_length(notes) <= 1000),
    published_version integer
);
CREATE INDEX persona_submissions_status_idx ON persona_submissions (status, submitted_at);
ALTER TABLE persona_submissions ENABLE ROW LEVEL SECURITY;  -- アプリのロールには、直接の権限を与えない

-- 提出
CREATE OR REPLACE FUNCTION ops_submit_persona(
    p_character_id text, p_version_label text, p_definition jsonb, p_row jsonb, p_lint jsonb,
    p_simulation text, p_operator text, p_now timestamptz
) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_id uuid;
BEGIN
    IF coalesce(p_operator, '') = '' THEN
        RAISE EXCEPTION '運営者が指定されていません';
    END IF;
    INSERT INTO persona_submissions (character_id, version_label, definition_json, character_row, lint_json,
                                     simulation_summary, submitted_by, submitted_at)
    VALUES (p_character_id, p_version_label, p_definition, p_row, coalesce(p_lint, '[]'::jsonb),
            coalesce(p_simulation, ''), p_operator, p_now)
    RETURNING submission_id INTO v_id;
    RETURN v_id;
END
$$;

-- 提出の一覧(新しい順。定義の本体は返さない)
CREATE OR REPLACE FUNCTION ops_list_persona_submissions(p_status text, p_limit integer)
RETURNS TABLE (
    submission_id uuid, character_id text, version_label text, name text, submitted_by text, submitted_at timestamptz,
    status text, decided_by text, decided_at timestamptz, published_version integer
)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT s.submission_id, s.character_id, s.version_label, s.definition_json ->> 'name', s.submitted_by,
           s.submitted_at, s.status, s.decided_by, s.decided_at, s.published_version
      FROM persona_submissions s
     WHERE p_status IS NULL OR s.status = p_status
     ORDER BY s.submitted_at DESC
     LIMIT least(greatest(coalesce(p_limit, 50), 1), 200)
$$;

-- 提出の詳細
CREATE OR REPLACE FUNCTION ops_get_persona_submission(p_submission_id uuid)
RETURNS TABLE (
    submission_id uuid, character_id text, version_label text, definition_json jsonb, lint_json jsonb,
    simulation_summary text, submitted_by text, submitted_at timestamptz, status text, decided_by text,
    decided_at timestamptz, notes text, published_version integer
)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT s.submission_id, s.character_id, s.version_label, s.definition_json, s.lint_json, s.simulation_summary,
           s.submitted_by, s.submitted_at, s.status, s.decided_by, s.decided_at, s.notes, s.published_version
      FROM persona_submissions s
     WHERE s.submission_id = p_submission_id
$$;

-- 版の番号(X.Y.Z)を、比べられる配列にする
CREATE OR REPLACE FUNCTION persona_version_key(p_label text) RETURNS integer[]
LANGUAGE sql IMMUTABLE SET search_path = public, pg_temp AS $$
    SELECT string_to_array(p_label, '.')::integer[]
$$;

-- 承認して公開する。公開した内部の版の番号を返す
CREATE OR REPLACE FUNCTION ops_publish_persona_submission(
    p_submission_id uuid, p_operator text, p_notes text, p_now timestamptz
) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    s persona_submissions%ROWTYPE;
    v_newest integer[];
    v_number integer;
BEGIN
    SELECT * INTO s FROM persona_submissions WHERE submission_id = p_submission_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION '提出が見つかりません' USING ERRCODE = 'no_data_found';
    END IF;
    IF s.status <> 'pending' THEN
        RAISE EXCEPTION 'この提出は、すでに処理されています' USING ERRCODE = 'check_violation';
    END IF;
    IF coalesce(p_operator, '') = '' OR p_operator = s.submitted_by THEN
        -- 設計者と承認者を分ける(提出した本人は、承認できない)
        RAISE EXCEPTION '提出した本人は、承認できません' USING ERRCODE = 'insufficient_privilege';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtext('characters:' || s.character_id));
    SELECT max(persona_version_key(version_label)) INTO v_newest
      FROM characters
     WHERE character_id = s.character_id AND status IN ('published', 'deprecated') AND version_label IS NOT NULL;
    IF v_newest IS NOT NULL AND persona_version_key(s.version_label) <= v_newest THEN
        RAISE EXCEPTION 'バージョンは、これまでの公開版(%)より大きくしてください', array_to_string(v_newest, '.')
            USING ERRCODE = 'check_violation';
    END IF;
    SELECT coalesce(max(version), 0) + 1 INTO v_number FROM characters WHERE character_id = s.character_id;
    INSERT INTO characters
    SELECT * FROM jsonb_populate_record(
        NULL::characters,
        s.character_row || jsonb_build_object(
            'character_id', s.character_id, 'version', v_number, 'version_label', s.version_label,
            'status', 'published', 'created_at', p_now
        )
    );
    INSERT INTO character_reviews (character_id, version, reviewer, reviewed_at, lint_report_json,
                                   simulation_report_ref, notes, decision)
    VALUES (s.character_id, v_number, p_operator, p_now, s.lint_json, s.simulation_summary, p_notes, 'approved');
    UPDATE persona_submissions
       SET status = 'approved', decided_by = p_operator, decided_at = p_now, notes = p_notes, published_version = v_number
     WHERE submission_id = p_submission_id;
    RETURN v_number;
END
$$;

-- 差し戻す
CREATE OR REPLACE FUNCTION ops_reject_persona_submission(
    p_submission_id uuid, p_operator text, p_notes text, p_now timestamptz
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    IF coalesce(p_operator, '') = '' THEN
        RAISE EXCEPTION '運営者が指定されていません';
    END IF;
    UPDATE persona_submissions
       SET status = 'rejected', decided_by = p_operator, decided_at = p_now, notes = p_notes
     WHERE submission_id = p_submission_id AND status = 'pending';
    RETURN FOUND;
END
$$;

-- 版の一覧(全キャラクタ。設計用の元データは返さない)
CREATE OR REPLACE FUNCTION ops_list_persona_versions(p_character_id text)
RETURNS TABLE (character_id text, version integer, version_label text, name text, status text, created_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT c.character_id, c.version, c.version_label, c.name, c.status, c.created_at
      FROM characters c
     WHERE p_character_id IS NULL OR c.character_id = p_character_id
     ORDER BY c.character_id, c.version DESC
$$;

-- 1つの版の内容(差分の表示用。CLI の persona diff と同じ列)
CREATE OR REPLACE FUNCTION ops_get_persona_version(p_character_id text, p_version integer)
RETURNS SETOF characters
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT * FROM characters WHERE character_id = p_character_id AND version = p_version
$$;

-- ロールバック: 指定の版に戻し、それより新しい公開版を非推奨にする
CREATE OR REPLACE FUNCTION ops_rollback_persona(
    p_character_id text, p_version_label text, p_operator text, p_now timestamptz
) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_target integer;
BEGIN
    IF coalesce(p_operator, '') = '' THEN
        RAISE EXCEPTION '運営者が指定されていません';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtext('characters:' || p_character_id));
    SELECT max(version) INTO v_target FROM characters
     WHERE character_id = p_character_id AND version_label = p_version_label AND status IN ('published', 'deprecated');
    IF v_target IS NULL THEN
        RAISE EXCEPTION '公開されたことのある版が見つかりません' USING ERRCODE = 'no_data_found';
    END IF;
    UPDATE characters SET status = 'deprecated'
     WHERE character_id = p_character_id AND version > v_target AND status = 'published';
    UPDATE characters SET status = 'published' WHERE character_id = p_character_id AND version = v_target;
    INSERT INTO character_reviews (character_id, version, reviewer, reviewed_at, decision)
    VALUES (p_character_id, v_target, p_operator, p_now, 'rollback');
    RETURN v_target;
END
$$;

-- 非推奨: 公開中の版をすべて非推奨にする(会話では使えなくなる)。変えた行の数を返す
CREATE OR REPLACE FUNCTION ops_deprecate_persona(p_character_id text, p_operator text, p_now timestamptz)
RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_count integer;
BEGIN
    IF coalesce(p_operator, '') = '' THEN
        RAISE EXCEPTION '運営者が指定されていません';
    END IF;
    UPDATE characters SET status = 'deprecated' WHERE character_id = p_character_id AND status = 'published';
    GET DIAGNOSTICS v_count = ROW_COUNT;
    IF v_count > 0 THEN
        INSERT INTO character_reviews (character_id, version, reviewer, reviewed_at, decision)
        SELECT p_character_id, max(version), p_operator, p_now, 'deprecate' FROM characters WHERE character_id = p_character_id;
    END IF;
    RETURN v_count;
END
$$;

REVOKE ALL ON FUNCTION ops_submit_persona(text, text, jsonb, jsonb, jsonb, text, text, timestamptz) FROM PUBLIC;
REVOKE ALL ON FUNCTION ops_list_persona_submissions(text, integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION ops_get_persona_submission(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION ops_publish_persona_submission(uuid, text, text, timestamptz) FROM PUBLIC;
REVOKE ALL ON FUNCTION ops_reject_persona_submission(uuid, text, text, timestamptz) FROM PUBLIC;
REVOKE ALL ON FUNCTION ops_list_persona_versions(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION ops_get_persona_version(text, integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION ops_rollback_persona(text, text, text, timestamptz) FROM PUBLIC;
REVOKE ALL ON FUNCTION ops_deprecate_persona(text, text, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ops_submit_persona(text, text, jsonb, jsonb, jsonb, text, text, timestamptz) TO mycompanion_app;
GRANT EXECUTE ON FUNCTION ops_list_persona_submissions(text, integer) TO mycompanion_app;
GRANT EXECUTE ON FUNCTION ops_get_persona_submission(uuid) TO mycompanion_app;
GRANT EXECUTE ON FUNCTION ops_publish_persona_submission(uuid, text, text, timestamptz) TO mycompanion_app;
GRANT EXECUTE ON FUNCTION ops_reject_persona_submission(uuid, text, text, timestamptz) TO mycompanion_app;
GRANT EXECUTE ON FUNCTION ops_list_persona_versions(text) TO mycompanion_app;
GRANT EXECUTE ON FUNCTION ops_get_persona_version(text, integer) TO mycompanion_app;
GRANT EXECUTE ON FUNCTION ops_rollback_persona(text, text, text, timestamptz) TO mycompanion_app;
GRANT EXECUTE ON FUNCTION ops_deprecate_persona(text, text, timestamptz) TO mycompanion_app;
