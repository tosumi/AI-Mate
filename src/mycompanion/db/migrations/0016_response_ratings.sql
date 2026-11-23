-- =====================================================================
-- 0016_response_ratings: 応答への評価(人間評価。本体仕様書 §14、§15.1 の M6-7)
--
-- - 評価者(開発者・試験の協力者)が、自分の会話の応答に、点(1〜5)と短いメモを付ける。
--   会話の本文は持たない。評価の時点の「作り方の文脈」(キャラクタの版、応答モード、作り方、生成のモデル、
--   プロンプトの版)を、ターンの記録から写して持つ(集計で、作り方・モデルごとに比べるため)。
-- - 同じターンへの評価は、付け直すと上書きする。
-- - アカウントの削除で消える(users の外部キー)。書き出しの対象。
-- - 集計の関数は、件数と平均だけを返す(user_id、本文、メモは返さない)。
-- =====================================================================

CREATE TABLE response_ratings (
    rating_id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id              uuid NOT NULL REFERENCES users ON DELETE CASCADE,
    character_id         text NOT NULL,
    session_id           uuid NOT NULL,
    turn_no              integer NOT NULL CHECK (turn_no >= 1),
    overall              smallint NOT NULL CHECK (overall BETWEEN 1 AND 5),   -- 総合
    persona_fit          smallint CHECK (persona_fit BETWEEN 1 AND 5),        -- キャラクタらしさ
    naturalness          smallint CHECK (naturalness BETWEEN 1 AND 5),        -- 自然さ
    empathy              smallint CHECK (empathy BETWEEN 1 AND 5),            -- 気持ちへの合い方
    note                 text CHECK (note IS NULL OR char_length(note) <= 500),
    character_version    integer,
    response_mode        text,
    compose_mode         text,
    reply_model          text,
    reply_prompt_version text,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, character_id, session_id, turn_no)
);
CREATE INDEX response_ratings_created_idx ON response_ratings (created_at);

ALTER TABLE response_ratings ENABLE ROW LEVEL SECURITY;
CREATE POLICY response_ratings_isolation ON response_ratings FOR ALL TO mycompanion_app
    USING (user_id = app_user_id() AND character_id = app_character_id())
    WITH CHECK (user_id = app_user_id() AND character_id = app_character_id());
GRANT SELECT, INSERT, UPDATE ON response_ratings TO mycompanion_app;

-- 評価の集計(作り方・モデル・プロンプトの版・キャラクタの版・応答モードごと)。件数と平均だけを返す
CREATE OR REPLACE FUNCTION rating_summary(p_since timestamptz)
RETURNS TABLE (
    character_id text, character_version integer, compose_mode text, reply_model text,
    reply_prompt_version text, response_mode text, raters bigint, n bigint,
    overall numeric, persona_fit numeric, naturalness numeric, empathy numeric, low bigint
)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT r.character_id, r.character_version, r.compose_mode, r.reply_model, r.reply_prompt_version,
           r.response_mode, count(DISTINCT r.user_id), count(*),
           round(avg(r.overall), 2), round(avg(r.persona_fit), 2), round(avg(r.naturalness), 2),
           round(avg(r.empathy), 2), count(*) FILTER (WHERE r.overall <= 2)
      FROM response_ratings r
     WHERE r.updated_at >= p_since
     GROUP BY 1, 2, 3, 4, 5, 6
     ORDER BY 1, 2, 3, 4, 5, 6
$$;

REVOKE ALL ON FUNCTION rating_summary(timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION rating_summary(timestamptz) TO mycompanion_app;
