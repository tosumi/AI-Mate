-- =====================================================================
-- 0006_core_preference: 「大切なこと」に、ユーザーの好きなもの・嫌いなもの(preference)を加える
-- =====================================================================

ALTER TABLE memory DROP CONSTRAINT memory_core_key_check;
ALTER TABLE memory ADD CONSTRAINT memory_core_key_check
    CHECK (core_key IN ('user_name', 'profile', 'preference', 'relation', 'request', 'other'));
