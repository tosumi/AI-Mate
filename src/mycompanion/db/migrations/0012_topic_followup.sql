-- =====================================================================
-- 0012_topic_followup: トピックの想起と、予定の「その後」(本体仕様書 §9.13 の第2段階)
--
-- - topic_doc: 嗜好度、活性度、キャラクタが最後に持ち出した時刻、最後に「その後」を聞いた時刻、聞いた予定の項目。
--   どれも運用の値で、OKF の文書(frontmatter)には入れない。
-- - topic_recall_events: トピックの想起の記録。トピックごと・日ごと・起点ごとに1回。
--   記憶の想起(recall_events)とは別に数える(タイムラインが使われても、根拠の記憶の期限は延びない)。
-- =====================================================================

ALTER TABLE topic_doc
    ADD COLUMN preference real NOT NULL DEFAULT 0 CHECK (preference BETWEEN -1 AND 1),
    ADD COLUMN activation real NOT NULL DEFAULT 0,
    ADD COLUMN last_mentioned_by_character_at timestamptz,
    ADD COLUMN last_followup_at timestamptz,
    ADD COLUMN followup_keys text[] NOT NULL DEFAULT '{}';   -- 「その後」を聞いた予定の項目(同じ予定は、1回だけ)

CREATE TABLE topic_recall_events (
    event_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      uuid NOT NULL,
    character_id text NOT NULL,
    doc_id       uuid NOT NULL,
    session_id   uuid,
    turn_id      text,
    ts           timestamptz NOT NULL DEFAULT now(),
    day          date NOT NULL,
    trigger      text NOT NULL CHECK (trigger IN ('user', 'character')),
    source       text NOT NULL CHECK (source IN ('search', 'topic_selection', 'followup')),
    weight       real NOT NULL,
    status       text NOT NULL DEFAULT 'confirmed' CHECK (status IN ('pending', 'confirmed', 'discarded')),
    confirmed_at timestamptz,
    UNIQUE (doc_id, day, trigger),
    FOREIGN KEY (user_id, character_id, doc_id)
        REFERENCES topic_doc (user_id, character_id, doc_id) ON DELETE CASCADE
);
CREATE INDEX topic_recall_events_session_idx ON topic_recall_events (session_id) WHERE session_id IS NOT NULL;

ALTER TABLE topic_recall_events ENABLE ROW LEVEL SECURITY;
CREATE POLICY topic_recall_events_isolation ON topic_recall_events FOR ALL TO mycompanion_app
    USING (user_id = app_user_id() AND character_id = app_character_id())
    WITH CHECK (user_id = app_user_id() AND character_id = app_character_id());
GRANT SELECT, INSERT, UPDATE, DELETE ON topic_recall_events TO mycompanion_app;
