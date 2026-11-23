-- =====================================================================
-- 0011_topic_memory: トピック別の時系列記憶(本体仕様書 §9.13)
--
-- - topic_doc: 1行 = 1つの OKF 文書(トピック)。frontmatter(jsonb)と本文(text)が正。
--   バンドルは (user_id, character_id) ごと。キャラクタをまたいで、共有しない。
-- - topic_doc_source / topic_entry: 派生の索引(文書から作り直せる)。記憶の削除の伝播と、並べ替えに使う。
--   複合外部キーで、別のキャラクタの記憶を根拠にできないようにする(アプリの誤りを、データベースが拒否する)。
-- - topic_doc_revision: 文書の以前の版(保持は短い。削除の伝播の対象)。
-- - topic_archive: 削除(アーカイブ)されたトピックの概要。**アプリのロールに SELECT を与えない**。
--   生成の経路(検索、プロンプト、話題選択)からは読めない。利用者への表示・書き出し・削除の伝播は、専用の関数を通す。
-- - memory.recall_excluded: アーカイブしたトピックにだけ属していた記憶を、想起の対象から外す。
-- =====================================================================

ALTER TABLE memory ADD CONSTRAINT memory_owner_unique UNIQUE (user_id, character_id, memory_id);
ALTER TABLE memory ADD COLUMN recall_excluded text CHECK (recall_excluded IN ('topic_archived'));

CREATE TABLE topic_doc (
    doc_id               uuid PRIMARY KEY,
    user_id              uuid NOT NULL REFERENCES users ON DELETE CASCADE,
    character_id         text NOT NULL,
    path                 text NOT NULL,                 -- 'threads/t-3m8w1c.md'(コンセプトID + .md)
    okf_type             text NOT NULL CHECK (okf_type IN ('Topic Entity', 'Topic Thread')),
    frontmatter          jsonb NOT NULL,                -- YAML の frontmatter(書き出しで YAML にする)
    body                 text NOT NULL,                 -- Markdown の本文(コードだけが組み立てる)
    okf_status           text GENERATED ALWAYS AS (frontmatter->>'status') STORED,
    title                text GENERATED ALWAYS AS (frontmatter->>'title') STORED,
    -- 運用の値(frontmatter に入れない)
    lifecycle            text NOT NULL CHECK (lifecycle IN ('candidate', 'active', 'dormant', 'merged')),
    established_at       timestamptz,
    stale_after          timestamptz,
    muted                boolean NOT NULL DEFAULT false,
    quarantined          boolean NOT NULL DEFAULT false,
    embedding            vector(1536),                  -- title + aliases + description の埋め込み
    embedding_model      text,
    embedding_dimensions integer,
    mc_profile           text NOT NULL,
    next_entry_no        integer NOT NULL DEFAULT 1,    -- 項目の ID は単調に増やし、再利用しない
    version              integer NOT NULL DEFAULT 1,
    created_day          date,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, character_id, path),
    UNIQUE (user_id, character_id, doc_id)
);

CREATE TABLE topic_doc_source (
    user_id       uuid NOT NULL,
    character_id  text NOT NULL,
    doc_id        uuid NOT NULL,
    source_key    text NOT NULL,                        -- sources[].id(例: m-7c1e2a90)
    memory_id     uuid,                                 -- 期限切れで消えたら NULL(項目は残す)
    told_on       date,
    last_modified timestamptz,
    expired       boolean NOT NULL DEFAULT false,
    PRIMARY KEY (doc_id, source_key),
    FOREIGN KEY (user_id, character_id, doc_id)
        REFERENCES topic_doc (user_id, character_id, doc_id) ON DELETE CASCADE,
    -- 記憶は論理削除(行は残る)。物理削除はアカウントの削除のときだけなので、連鎖で消す
    FOREIGN KEY (user_id, character_id, memory_id)
        REFERENCES memory (user_id, character_id, memory_id) ON DELETE CASCADE
);
CREATE INDEX topic_doc_source_memory_idx ON topic_doc_source (memory_id) WHERE memory_id IS NOT NULL;

CREATE TABLE topic_entry (
    user_id            uuid NOT NULL,
    character_id       text NOT NULL,
    doc_id             uuid NOT NULL,
    entry_key          text NOT NULL,                   -- 本文の ID 列(t1, t2, …)
    entry_kind         text NOT NULL CHECK (entry_kind IN ('event', 'state', 'plan', 'outcome', 'correction')),
    occurred_from      date,
    occurred_to        date,
    occurred_precision text NOT NULL
                       CHECK (occurred_precision IN ('day', 'week', 'month', 'season', 'year', 'unknown')),
    time_expression    text,                            -- 元の表現(「昨日から」)
    told_at            timestamptz,
    told_on            date NOT NULL,
    superseded_by      text,
    resolves           text,
    valence            real,
    sensitivity        text,
    quarantined        boolean NOT NULL DEFAULT false,
    source_keys        text[] NOT NULL,
    PRIMARY KEY (doc_id, entry_key),
    FOREIGN KEY (user_id, character_id, doc_id)
        REFERENCES topic_doc (user_id, character_id, doc_id) ON DELETE CASCADE
);
CREATE INDEX topic_entry_order_idx ON topic_entry (user_id, character_id, doc_id, occurred_from, told_on);

CREATE TABLE topic_doc_revision (
    user_id      uuid NOT NULL,
    character_id text NOT NULL,
    doc_id       uuid NOT NULL,
    version      integer NOT NULL,
    frontmatter  jsonb NOT NULL,
    body         text NOT NULL,
    change_kind  text NOT NULL,     -- creation / update / correction / deletion_propagation / expiry / user_edit / dormant
    target_day   date,
    changed_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (doc_id, version),
    FOREIGN KEY (user_id, character_id, doc_id)
        REFERENCES topic_doc (user_id, character_id, doc_id) ON DELETE CASCADE
);

CREATE TABLE topic_archive (
    archive_id      uuid PRIMARY KEY,
    user_id         uuid NOT NULL REFERENCES users ON DELETE CASCADE,
    character_id    text NOT NULL,
    original_doc_id uuid NOT NULL,                      -- 元の文書の ID(元の行は、もうない)
    path            text NOT NULL,                      -- 'archive/a-5r2x0q.md'
    frontmatter     jsonb NOT NULL,                     -- type: Topic Archive、status: deprecated ほか
    body            text NOT NULL,                      -- '# 概要' の節
    summary_state   text NOT NULL CHECK (summary_state IN ('ok', 'pending', 'withdrawn')),
    pending_json    jsonb NOT NULL DEFAULT '[]'::jsonb, -- 概要の再試行に使う項目(1回だけ。成功・失敗にかかわらず消す)
    memory_ids      uuid[] NOT NULL DEFAULT '{}',       -- 根拠の記憶(削除の伝播で、取り下げる概要を探す)
    first_occurred  date,
    last_occurred   date,
    archived_at     timestamptz NOT NULL,
    UNIQUE (user_id, character_id, path)
);

-- 行レベルセキュリティ(memory と同じ方針。§13.4.6)
DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['topic_doc', 'topic_doc_source', 'topic_entry', 'topic_doc_revision', 'topic_archive']
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    END LOOP;
END
$$;

CREATE POLICY topic_doc_isolation ON topic_doc FOR ALL TO mycompanion_app
    USING (user_id = app_user_id() AND character_id = app_character_id())
    WITH CHECK (user_id = app_user_id() AND character_id = app_character_id());
CREATE POLICY topic_doc_source_isolation ON topic_doc_source FOR ALL TO mycompanion_app
    USING (user_id = app_user_id() AND character_id = app_character_id())
    WITH CHECK (user_id = app_user_id() AND character_id = app_character_id());
CREATE POLICY topic_entry_isolation ON topic_entry FOR ALL TO mycompanion_app
    USING (user_id = app_user_id() AND character_id = app_character_id())
    WITH CHECK (user_id = app_user_id() AND character_id = app_character_id());
CREATE POLICY topic_doc_revision_isolation ON topic_doc_revision FOR ALL TO mycompanion_app
    USING (user_id = app_user_id() AND character_id = app_character_id())
    WITH CHECK (user_id = app_user_id() AND character_id = app_character_id());
CREATE POLICY topic_archive_isolation ON topic_archive FOR ALL TO mycompanion_app
    USING (user_id = app_user_id() AND character_id = app_character_id())
    WITH CHECK (user_id = app_user_id() AND character_id = app_character_id());

GRANT SELECT, INSERT, UPDATE, DELETE ON topic_doc, topic_doc_source, topic_entry, topic_doc_revision
    TO mycompanion_app;
-- アーカイブは、書き込み(INSERT)だけ。読む・直す・消すは、下の関数を通す(現在のスコープの行だけ)
GRANT INSERT ON topic_archive TO mycompanion_app;

CREATE OR REPLACE FUNCTION list_my_topic_archives()
RETURNS TABLE (
    archive_id uuid, original_doc_id uuid, path text, frontmatter jsonb, body text, summary_state text,
    pending_json jsonb, memory_ids uuid[], first_occurred date, last_occurred date, archived_at timestamptz
)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT a.archive_id, a.original_doc_id, a.path, a.frontmatter, a.body, a.summary_state,
           a.pending_json, a.memory_ids, a.first_occurred, a.last_occurred, a.archived_at
      FROM topic_archive a
     WHERE a.user_id = app_user_id() AND a.character_id = app_character_id()
     ORDER BY a.archived_at
$$;

CREATE OR REPLACE FUNCTION update_my_topic_archive(
    p_archive_id uuid, p_frontmatter jsonb, p_body text, p_state text, p_pending jsonb, p_memory_ids uuid[]
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    UPDATE topic_archive
       SET frontmatter = p_frontmatter, body = p_body, summary_state = p_state,
           pending_json = p_pending, memory_ids = p_memory_ids
     WHERE archive_id = p_archive_id AND user_id = app_user_id() AND character_id = app_character_id();
    RETURN FOUND;
END
$$;

CREATE OR REPLACE FUNCTION purge_my_topic_archive(p_archive_id uuid) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    DELETE FROM topic_archive
     WHERE archive_id = p_archive_id AND user_id = app_user_id() AND character_id = app_character_id();
    RETURN FOUND;
END
$$;

REVOKE ALL ON FUNCTION list_my_topic_archives() FROM PUBLIC;
REVOKE ALL ON FUNCTION update_my_topic_archive(uuid, jsonb, text, text, jsonb, uuid[]) FROM PUBLIC;
REVOKE ALL ON FUNCTION purge_my_topic_archive(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION list_my_topic_archives() TO mycompanion_app;
GRANT EXECUTE ON FUNCTION update_my_topic_archive(uuid, jsonb, text, text, jsonb, uuid[]) TO mycompanion_app;
GRANT EXECUTE ON FUNCTION purge_my_topic_archive(uuid) TO mycompanion_app;

-- 自分がデータを持っているキャラクタの一覧に、トピックとアーカイブを加える(書き出し用)
CREATE OR REPLACE FUNCTION list_my_characters()
RETURNS TABLE (character_id text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT character_id FROM sessions WHERE user_id = app_user_id()
    UNION SELECT character_id FROM memory WHERE user_id = app_user_id()
    UNION SELECT character_id FROM relationships WHERE user_id = app_user_id()
    UNION SELECT character_id FROM topic_doc WHERE user_id = app_user_id()
    UNION SELECT character_id FROM topic_archive WHERE user_id = app_user_id()
$$;
