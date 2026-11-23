-- =====================================================================
-- 0017_rating_snapshots: 人間評価の集計の保存(本体仕様書 §14.1、§15.1 の M6-7)
--
-- 評価(response_ratings)は、評価者がアカウントを削除すると消える(本人のデータのため)。
-- そのままでは、過去の版の集計の数字が変わり、版が上がったときの比較ができなくなる。
-- そこで、評価の期間の終わりに、集計の結果(版・作り方・モデルごとの件数と平均だけ)を保存する。
-- 保存する内容には、user_id・本文・メモを含めない(個人に結びつかない統計)。評価者の削除の影響を受けない。
-- 件数が少ない組(min_n 未満)は、平均を保存しない(少数の評価から、個人の点が推測されないように)。
-- =====================================================================

CREATE TABLE rating_snapshots (
    snapshot_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    label       text NOT NULL CHECK (char_length(label) BETWEEN 1 AND 100),  -- 例: 「2026-11 v1.0 前」
    since       timestamptz NOT NULL,   -- 集計の対象(この時刻以降に付けた評価)
    until       timestamptz NOT NULL,   -- 保存した時刻
    min_n       integer NOT NULL CHECK (min_n >= 1),
    rows_json   jsonb NOT NULL          -- rating_summary の行(件数が min_n 未満の組は、平均を null にする)
);
CREATE INDEX rating_snapshots_until_idx ON rating_snapshots (until);

-- ユーザーのデータを持たない。アプリのロールからは、下の関数だけで読み書きする
ALTER TABLE rating_snapshots ENABLE ROW LEVEL SECURITY;

CREATE OR REPLACE FUNCTION save_rating_snapshot(p_label text, p_since timestamptz, p_min_n integer)
RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_id uuid;
BEGIN
    INSERT INTO rating_snapshots (label, since, until, min_n, rows_json)
    SELECT p_label, p_since, now(), p_min_n, coalesce(jsonb_agg(
               jsonb_build_object(
                   'character_id', s.character_id, 'character_version', s.character_version,
                   'compose_mode', s.compose_mode, 'reply_model', s.reply_model,
                   'reply_prompt_version', s.reply_prompt_version, 'response_mode', s.response_mode,
                   'raters', s.raters, 'n', s.n,
                   'overall', CASE WHEN s.n >= p_min_n THEN s.overall END,
                   'persona_fit', CASE WHEN s.n >= p_min_n THEN s.persona_fit END,
                   'naturalness', CASE WHEN s.n >= p_min_n THEN s.naturalness END,
                   'empathy', CASE WHEN s.n >= p_min_n THEN s.empathy END,
                   'low', CASE WHEN s.n >= p_min_n THEN s.low END
               )), '[]'::jsonb)
      FROM rating_summary(p_since) s
    RETURNING snapshot_id INTO v_id;
    RETURN v_id;
END
$$;

CREATE OR REPLACE FUNCTION list_rating_snapshots()
RETURNS TABLE (snapshot_id uuid, label text, since timestamptz, until timestamptz, min_n integer, rows_json jsonb)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT snapshot_id, label, since, until, min_n, rows_json FROM rating_snapshots ORDER BY until
$$;

REVOKE ALL ON FUNCTION save_rating_snapshot(text, timestamptz, integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION list_rating_snapshots() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION save_rating_snapshot(text, timestamptz, integer) TO mycompanion_app;
GRANT EXECUTE ON FUNCTION list_rating_snapshots() TO mycompanion_app;
