-- =====================================================================
-- 0003_gate_and_screening: Response Gate とスクリーニングの状態(本体仕様書 §8.4、§13.5)
--
-- - sessions: セッション内のターン数と、連続した無言の回数(R1・R4 の判定に使う)。
-- - user_throttle: 重大な検出の回数と、一時制限の期限(ユーザー単位。§13.5.5)。
--   アカウントの停止(users.suspended_until)とは区別する。本文や検出の内容は持たない。
-- - rate_limits: ユーザーあたりのターン数の上限(§13.5.3 の L0)。
-- =====================================================================

ALTER TABLE sessions
    ADD COLUMN turn_count          integer NOT NULL DEFAULT 0,
    ADD COLUMN consecutive_silence integer NOT NULL DEFAULT 0 CHECK (consecutive_silence BETWEEN 0 AND 2);

-- 一時制限(キャラクタをまたぐ。重大な検出は、キャラクタを替えても同じ扱いにするため)
CREATE TABLE user_throttle (
    user_id             uuid PRIMARY KEY REFERENCES users ON DELETE CASCADE,
    serious_event_times jsonb NOT NULL DEFAULT '[]'::jsonb,   -- 直近24時間の、重大な検出の時刻だけ
    throttled_until     timestamptz,                          -- 解除の通知を出すまで残す
    updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE rate_limits (
    user_id      uuid NOT NULL REFERENCES users ON DELETE CASCADE,
    bucket       text NOT NULL CHECK (bucket IN ('minute', 'day')),
    window_start timestamptz NOT NULL,
    count        integer NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, bucket)
);

-- 行レベルセキュリティ(ユーザー単位)
ALTER TABLE user_throttle ENABLE ROW LEVEL SECURITY;
CREATE POLICY user_throttle_isolation ON user_throttle FOR ALL TO mycompanion_app
    USING (user_id = app_user_id()) WITH CHECK (user_id = app_user_id());
GRANT SELECT, INSERT, UPDATE ON user_throttle TO mycompanion_app;

ALTER TABLE rate_limits ENABLE ROW LEVEL SECURITY;
CREATE POLICY rate_limits_isolation ON rate_limits FOR ALL TO mycompanion_app
    USING (user_id = app_user_id()) WITH CHECK (user_id = app_user_id());
GRANT SELECT, INSERT, UPDATE ON rate_limits TO mycompanion_app;
