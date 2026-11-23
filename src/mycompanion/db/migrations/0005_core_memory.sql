-- =====================================================================
-- 0005_core_memory: 「大切なこと」の記憶(時系列の整理とは別に残す)
--
-- ユーザーの呼び名のように、いつ話したかに関係なく、ずっと覚えておくべき事実。
-- - 長期に直接置き、日次整理・月次整理・忘却の対象にしない(消せるのは、ユーザーだけ)。
-- - core_key は種類。user_name(呼び名)は1件だけで、言い直されたら置き換える。
-- =====================================================================

ALTER TABLE memory ADD COLUMN core_key text
    CHECK (core_key IN ('user_name', 'profile', 'relation', 'request', 'other'));

-- 呼び名は、(user, character) ごとに1件だけ
CREATE UNIQUE INDEX memory_core_single ON memory (user_id, character_id, core_key)
    WHERE core_key = 'user_name' AND deleted_at IS NULL;
CREATE INDEX memory_core_idx ON memory (user_id, character_id) WHERE core_key IS NOT NULL AND deleted_at IS NULL;
