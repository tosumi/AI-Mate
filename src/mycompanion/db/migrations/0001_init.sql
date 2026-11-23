-- =====================================================================
-- 0001_init: PostgreSQL の初期スキーマ(本体仕様書 §12、§11.5.3、§13.4.6)
--
-- - 記憶・状態・関係値などは (user_id, character_id) の組ごとに分離する。
--   行レベルセキュリティ(RLS)で、データベース側で強制する。
-- - アプリは、トランザクションごとに次の2つを設定してからアクセスする。
--     SELECT set_config('app.user_id', '<uuid>', true);
--     SELECT set_config('app.character_id', '<id>', true);
-- - アプリのロール(mycompanion_app)には、RLS を迂回する権限を与えない。
-- - このマイグレーションは、テーブルの所有者(管理者ロール)で実行する。
--   所有者は RLS の対象外(FORCE は付けない)。アプリは所有者のロールで接続しないこと。
-- =====================================================================

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- アプリ用のグループロール。マネージド ID や開発者の Entra ロールに付与して使う
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mycompanion_app') THEN
        CREATE ROLE mycompanion_app NOLOGIN NOBYPASSRLS;
    END IF;
END
$$;

-- マイグレーションを実行した管理者も、アプリとして接続するときは SET ROLE でこのロールに切り替える
GRANT mycompanion_app TO CURRENT_USER;

-- 現在のリクエストの user_id / character_id(未設定なら NULL。NULL はどの行にも一致しない)
CREATE OR REPLACE FUNCTION app_user_id() RETURNS uuid
LANGUAGE sql STABLE AS $$
    SELECT nullif(current_setting('app.user_id', true), '')::uuid
$$;

CREATE OR REPLACE FUNCTION app_character_id() RETURNS text
LANGUAGE sql STABLE AS $$
    SELECT nullif(current_setting('app.character_id', true), '')
$$;

-- ---------------------------------------------------------------------
-- アカウント(§13.4)
-- ---------------------------------------------------------------------

CREATE TABLE users (
    user_id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),  -- 内部のID。会話・記憶・感情はこのIDだけで紐づける
    status          text NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'suspended', 'deleting', 'deleted')),
    created_at      timestamptz NOT NULL DEFAULT now(),
    suspended_until timestamptz,
    deleted_at      timestamptz
);

CREATE TABLE identity_links (
    idp           text NOT NULL,          -- entra_external_id
    idp_subject   text NOT NULL,          -- Entra のサブジェクト。メールアドレスは保持しない
    user_id       uuid NOT NULL REFERENCES users ON DELETE CASCADE,
    linked_at     timestamptz NOT NULL DEFAULT now(),
    last_login_at timestamptz,
    PRIMARY KEY (idp, idp_subject)
);
CREATE INDEX identity_links_user_idx ON identity_links (user_id);

CREATE TABLE consents (
    consent_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    uuid NOT NULL REFERENCES users ON DELETE CASCADE,
    type       text NOT NULL
               CHECK (type IN ('terms', 'privacy', 'emotion_data', 'memory', 'apac_processing')),
    version    text NOT NULL,
    granted_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz
);
CREATE INDEX consents_user_idx ON consents (user_id, type);

CREATE TABLE user_settings (
    user_id             uuid PRIMARY KEY REFERENCES users ON DELETE CASCADE,
    silence_enabled     boolean NOT NULL DEFAULT true,
    debug               boolean NOT NULL DEFAULT false,
    silence_render      text NOT NULL DEFAULT 'dots' CHECK (silence_render IN ('dots', 'none')),
    filter_mode         text NOT NULL DEFAULT 'on' CHECK (filter_mode IN ('on', 'off', 'both')),
    timezone            text NOT NULL DEFAULT 'Asia/Tokyo',
    locale              text NOT NULL DEFAULT 'ja-JP',
    is_under_18         boolean NOT NULL DEFAULT true,   -- 18歳未満フラグ。既定は安全側(§13.4.4)
    age_flag_source     text,                            -- self_declared など
    age_flag_updated_at timestamptz,
    updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE channel_identities (
    channel         text NOT NULL,
    channel_user_id text NOT NULL,
    user_id         uuid NOT NULL REFERENCES users ON DELETE CASCADE,
    linked_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (channel, channel_user_id)   -- 1つの外部アカウントは、1つの user_id にだけ紐づく
);
CREATE INDEX channel_identities_user_idx ON channel_identities (user_id);

CREATE TABLE link_codes (
    code_hash  text PRIMARY KEY,           -- ワンタイムコードはハッシュで保存する
    user_id    uuid NOT NULL REFERENCES users ON DELETE CASCADE,
    channel    text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    used_at    timestamptz,
    attempts   integer NOT NULL DEFAULT 0
);
CREATE INDEX link_codes_user_idx ON link_codes (user_id, created_at);

-- 追記のみ。アカウント削除後も保持する(user_id は仮名化。保持期間は法務確認。§11.5.5)
CREATE TABLE audit_logs (
    log_id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ts             timestamptz NOT NULL DEFAULT now(),
    actor_type     text NOT NULL CHECK (actor_type IN ('user', 'system', 'operator')),
    actor_id       text,
    action         text NOT NULL,
    target_user_id uuid,                   -- 外部キーにしない(アカウント削除後も行を残すため)
    details_json   jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX audit_logs_target_idx ON audit_logs (target_user_id, ts);

CREATE TABLE deletion_requests (
    request_id   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      uuid NOT NULL,            -- 外部キーにしない(完了の記録を残すため)
    scope        text NOT NULL,            -- memory:<id> / all
    requested_at timestamptz NOT NULL DEFAULT now(),
    status       text NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'running', 'completed', 'failed')),
    completed_at timestamptz,
    details_json jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX deletion_requests_user_idx ON deletion_requests (user_id, requested_at);

-- ---------------------------------------------------------------------
-- キャラクタ(全ユーザー共通の定義。アプリは読み取りのみ)
-- ---------------------------------------------------------------------

CREATE TABLE characters (
    character_id          text NOT NULL,
    version               integer NOT NULL DEFAULT 1,
    name                  text NOT NULL,
    persona_prompt        text NOT NULL DEFAULT '',
    speech_style          text NOT NULL DEFAULT '',
    initial_relationship  real NOT NULL DEFAULT 0 CHECK (initial_relationship BETWEEN -20 AND 20),
    base_personality_json jsonb NOT NULL DEFAULT '{}'::jsonb,   -- type, axes, source
    profile_json          jsonb NOT NULL DEFAULT '{}'::jsonb,   -- 差分(設計用の元データ)
    overrides_json        jsonb NOT NULL DEFAULT '{}'::jsonb,   -- 上書き(設計用の元データ)
    traits_json           jsonb NOT NULL DEFAULT '{}'::jsonb,   -- 解決済みの値(実行時はこれを使う)
    preferences_json      jsonb NOT NULL DEFAULT '{}'::jsonb,   -- 話題ごとの嗜好
    adaptation_json       jsonb NOT NULL DEFAULT '{}'::jsonb,   -- adaptability / adjustable / fixed
    temperament_json      jsonb NOT NULL DEFAULT '{}'::jsonb,
    appraisal_rules_json  jsonb NOT NULL DEFAULT '[]'::jsonb,
    concept_json          jsonb NOT NULL DEFAULT '{}'::jsonb,
    status                text NOT NULL DEFAULT 'draft'
                          CHECK (status IN ('draft', 'review', 'published', 'deprecated')),
    created_at            timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (character_id, version)
);

-- ---------------------------------------------------------------------
-- 会話の状態((user_id, character_id) ごとに分離)
-- ---------------------------------------------------------------------

CREATE TABLE sessions (
    session_id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id          uuid NOT NULL REFERENCES users ON DELETE CASCADE,
    character_id     text NOT NULL,        -- セッションは1つのキャラクタとの会話
    channel          text NOT NULL,
    started_at       timestamptz NOT NULL DEFAULT now(),
    ended_at         timestamptz,
    last_activity_at timestamptz NOT NULL DEFAULT now(),   -- 無入力の検知に使う
    status           text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'closed')),
    closed_reason    text
);
CREATE INDEX sessions_user_char_idx ON sessions (user_id, character_id, started_at);
CREATE INDEX sessions_idle_idx ON sessions (last_activity_at) WHERE status = 'active';

CREATE TABLE relationships (
    user_id      uuid NOT NULL REFERENCES users ON DELETE CASCADE,
    character_id text NOT NULL,
    value        real NOT NULL CHECK (value BETWEEN -20 AND 20),   -- 関係値
    updated_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, character_id)
);

CREATE TABLE relationship_events (
    event_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         uuid NOT NULL REFERENCES users ON DELETE CASCADE,
    character_id    text NOT NULL,
    session_id      uuid,
    turn_no         integer,
    ts              timestamptz NOT NULL DEFAULT now(),
    delta           real NOT NULL,
    value_after     real NOT NULL,
    reason_code     text NOT NULL,
    source_event_id text                   -- Cosmos 側の emotion_events のID
);
CREATE INDEX relationship_events_idx ON relationship_events (user_id, character_id, ts);

-- ユーザーの感情状態。キャラクタごとに分離する(§4.2)
CREATE TABLE user_state (
    user_id         uuid NOT NULL REFERENCES users ON DELETE CASCADE,
    character_id    text NOT NULL,
    layer           text NOT NULL CHECK (layer IN ('instant', 'session_mood', 'baseline')),
    session_id      uuid,                  -- session_mood のときのみ
    vad_json        jsonb NOT NULL DEFAULT '{}'::jsonb,
    categories_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    sample_sessions integer NOT NULL DEFAULT 0,   -- baseline の根拠の量
    sample_days     integer NOT NULL DEFAULT 0,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, character_id, layer)
);

-- キャラクタをまたいで共有する唯一の状態(§4.2、§13.4.6)。
-- 感情の値・会話の内容・キャラクタの情報は持たない。列を増やさないこと
CREATE TABLE user_safety_state (
    user_id               uuid PRIMARY KEY REFERENCES users ON DELETE CASCADE,
    last_crisis_signal_at timestamptz,
    exempt_until          timestamptz,
    updated_at            timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE companion_state (
    user_id            uuid NOT NULL REFERENCES users ON DELETE CASCADE,
    character_id       text NOT NULL,
    layer              text NOT NULL CHECK (layer IN ('instant', 'session_mood', 'baseline')),
    session_id         uuid,
    vad_json           jsonb NOT NULL DEFAULT '{}'::jsonb,
    categories_json    jsonb NOT NULL DEFAULT '{}'::jsonb,
    last_triggers_json jsonb NOT NULL DEFAULT '[]'::jsonb,
    updated_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, character_id, layer)
);

-- 人格の改変・規則の解除の繰り返しの状態(§8.8.4)
CREATE TABLE persona_pressure (
    user_id            uuid NOT NULL REFERENCES users ON DELETE CASCADE,
    character_id       text NOT NULL,
    window_started_at  timestamptz,
    count              integer NOT NULL DEFAULT 0,
    stage              smallint NOT NULL DEFAULT 1 CHECK (stage BETWEEN 1 AND 4),
    notice_issued_at   timestamptz,
    count_since_notice integer NOT NULL DEFAULT 0,
    cooldown_until     timestamptz,
    updated_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, character_id)
);

-- ---------------------------------------------------------------------
-- 記憶(§9)
-- ---------------------------------------------------------------------

CREATE TABLE memory (
    memory_id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                        uuid NOT NULL REFERENCES users ON DELETE CASCADE,
    character_id                   text NOT NULL,
    tier                           text NOT NULL CHECK (tier IN ('short', 'medium', 'long')),
    kind                           text NOT NULL
                                   CHECK (kind IN ('episodic', 'semantic', 'relationship',
                                                   'monthly_summary', 'interaction_preference')),
    summary                        text NOT NULL,
    topics_json                    jsonb NOT NULL DEFAULT '[]'::jsonb,
    emotion_vad_json               jsonb,
    emotion_categories_json        jsonb,
    importance                     real NOT NULL DEFAULT 0 CHECK (importance BETWEEN 0 AND 1),
    sensitivity                    text,                       -- センシティブ項目の区分
    pinned                         boolean NOT NULL DEFAULT false,
    muted                          boolean NOT NULL DEFAULT false,   -- キャラクタ起点の話題選択から除外
    quarantined                    boolean NOT NULL DEFAULT false,   -- 隔離(検索対象外、昇格不可。§13.5.6)
    quarantine_reason              text,
    trust_level                    text NOT NULL DEFAULT 'normal' CHECK (trust_level IN ('normal', 'low')),
    preference                     real NOT NULL DEFAULT 0 CHECK (preference BETWEEN -1 AND 1),   -- 嗜好度
    preference_version             integer,
    activation                     real NOT NULL DEFAULT 0,          -- 活性度のキャッシュ(§9.9.2)
    activation_updated_at          timestamptz,
    origin_memory_id               uuid REFERENCES memory ON DELETE CASCADE,   -- 再活性化コピーの原本
    last_mentioned_by_character_at timestamptz,
    source_day                     date,                       -- 短期・中期: 元になった日
    period                         text,                       -- monthly_summary: 対象月(YYYY-MM)
    expires_at                     timestamptz,                -- 中期: 保持期限(再固定化でのみ延長)
    recall_days                    integer NOT NULL DEFAULT 0, -- 想起日数(recall_events の集計キャッシュ)
    user_recall_days               integer NOT NULL DEFAULT 0,
    last_recalled_at               timestamptz,
    promoted_from                  uuid,
    promoted_at                    timestamptz,
    consolidated_at                timestamptz,
    source_event_ids               jsonb NOT NULL DEFAULT '[]'::jsonb,
    source_memory_ids              jsonb NOT NULL DEFAULT '[]'::jsonb,
    stale                          boolean NOT NULL DEFAULT false,
    embedding                      vector(1536),               -- 埋め込み(§11.6)
    embedding_model                text,
    embedding_dimensions           integer,
    version                        integer NOT NULL DEFAULT 1,
    created_at                     timestamptz NOT NULL DEFAULT now(),
    deleted_at                     timestamptz
);
CREATE INDEX memory_scope_idx ON memory (user_id, character_id, tier) WHERE deleted_at IS NULL;
CREATE INDEX memory_expires_idx ON memory (expires_at) WHERE tier = 'medium' AND deleted_at IS NULL;
-- 月次の概要は (user_id, character_id, period) で一意
CREATE UNIQUE INDEX memory_monthly_unique ON memory (user_id, character_id, period)
    WHERE kind = 'monthly_summary' AND deleted_at IS NULL;

CREATE TABLE recall_events (
    event_id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    memory_id        uuid NOT NULL REFERENCES memory ON DELETE CASCADE,
    user_id          uuid NOT NULL REFERENCES users ON DELETE CASCADE,
    character_id     text NOT NULL,
    session_id       uuid,
    turn_id          text,
    ts               timestamptz NOT NULL DEFAULT now(),
    day              date NOT NULL,          -- 想起した日(ユーザーのローカル日付)
    trigger          text NOT NULL CHECK (trigger IN ('user', 'character')),
    source           text NOT NULL CHECK (source IN ('search', 'topic_selection', 'working_set')),
    weight           real NOT NULL,          -- 起点の重み(user 1.0、character 0.5)
    used_in_response boolean NOT NULL DEFAULT false,
    status           text NOT NULL DEFAULT 'confirmed'
                     CHECK (status IN ('pending', 'confirmed', 'discarded')),
    confirmed_at     timestamptz,
    UNIQUE (memory_id, day, trigger)
);
CREATE INDEX recall_events_scope_idx ON recall_events (user_id, character_id, day);

-- セッション内ワーキングセット(§9.9.3)。セッション終了で削除
CREATE TABLE working_set (
    user_id            uuid NOT NULL REFERENCES users ON DELETE CASCADE,
    character_id       text NOT NULL,
    session_id         uuid NOT NULL REFERENCES sessions ON DELETE CASCADE,
    memory_id          uuid NOT NULL REFERENCES memory ON DELETE CASCADE,
    added_at           timestamptz NOT NULL DEFAULT now(),
    last_recalled_turn integer NOT NULL DEFAULT 0,
    ttl_turns          integer NOT NULL,
    PRIMARY KEY (session_id, memory_id)
);

CREATE TABLE memory_consolidation_runs (
    run_id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id        uuid NOT NULL REFERENCES users ON DELETE CASCADE,
    character_id   text NOT NULL,
    run_type       text NOT NULL CHECK (run_type IN ('daily', 'monthly', 'promotion')),
    target         text NOT NULL,            -- 対象日(YYYY-MM-DD)または対象月(YYYY-MM)
    started_at     timestamptz NOT NULL DEFAULT now(),
    finished_at    timestamptz,
    status         text NOT NULL DEFAULT 'running'
                   CHECK (status IN ('running', 'success', 'failed', 'skipped')),
    counts_json    jsonb NOT NULL DEFAULT '{}'::jsonb,
    model_id       text,
    prompt_version text,
    error          text
);
CREATE INDEX consolidation_runs_idx
    ON memory_consolidation_runs (user_id, character_id, run_type, target);

-- ---------------------------------------------------------------------
-- 行レベルセキュリティ(§13.4.6)
-- ---------------------------------------------------------------------

-- (user_id, character_id) で分離するテーブル
DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'sessions', 'relationships', 'relationship_events', 'user_state', 'companion_state',
        'persona_pressure', 'memory', 'recall_events', 'working_set', 'memory_consolidation_runs'
    ]
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format(
            'CREATE POLICY %I ON %I FOR ALL TO mycompanion_app '
            'USING (user_id = app_user_id() AND character_id = app_character_id()) '
            'WITH CHECK (user_id = app_user_id() AND character_id = app_character_id())',
            t || '_isolation', t
        );
        EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON %I TO mycompanion_app', t);
    END LOOP;
END
$$;

-- ユーザー単位で分離するテーブル(user_safety_state は、キャラクタをまたぐ唯一の例外)
DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'users', 'identity_links', 'consents', 'user_settings', 'channel_identities',
        'link_codes', 'user_safety_state', 'deletion_requests'
    ]
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format(
            'CREATE POLICY %I ON %I FOR ALL TO mycompanion_app '
            'USING (user_id = app_user_id()) WITH CHECK (user_id = app_user_id())',
            t || '_isolation', t
        );
    END LOOP;
END
$$;

-- users: 参照と状態の更新だけ(作成は resolve_user、削除は削除のオーケストレーションで行う)
GRANT SELECT, UPDATE ON users TO mycompanion_app;
GRANT SELECT, UPDATE, DELETE ON identity_links TO mycompanion_app;
GRANT SELECT, INSERT, UPDATE ON consents TO mycompanion_app;
GRANT SELECT, INSERT, UPDATE ON user_settings TO mycompanion_app;
GRANT SELECT, INSERT, DELETE ON channel_identities TO mycompanion_app;
GRANT SELECT, INSERT, UPDATE ON link_codes TO mycompanion_app;
GRANT SELECT, INSERT, UPDATE ON user_safety_state TO mycompanion_app;
GRANT SELECT, INSERT, UPDATE ON deletion_requests TO mycompanion_app;

-- キャラクタ定義: 公開済みのものだけを読める
ALTER TABLE characters ENABLE ROW LEVEL SECURITY;
CREATE POLICY characters_published ON characters FOR SELECT TO mycompanion_app
    USING (status = 'published');
GRANT SELECT ON characters TO mycompanion_app;

-- 監査ログ: 追記のみ(読み取り・更新・削除はできない)
ALTER TABLE audit_logs ENABLE ROW LEVEL SECURITY;
CREATE POLICY audit_logs_append ON audit_logs FOR INSERT TO mycompanion_app WITH CHECK (true);
GRANT INSERT ON audit_logs TO mycompanion_app;

-- ---------------------------------------------------------------------
-- 認証直後の user_id の解決(§13.4.2)
--
-- トークンのサブジェクトから内部の user_id を引く。初回は users と user_settings を作る。
-- user_id が分かる前の処理なので、RLS の外(所有者の権限)で、この関数だけに限って行う。
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION resolve_user(p_idp text, p_subject text) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_user_id uuid;
BEGIN
    IF p_idp IS NULL OR p_subject IS NULL OR p_idp = '' OR p_subject = '' THEN
        RAISE EXCEPTION 'idp と subject は必須です';
    END IF;

    UPDATE identity_links SET last_login_at = now()
     WHERE idp = p_idp AND idp_subject = p_subject
    RETURNING user_id INTO v_user_id;
    IF FOUND THEN
        RETURN v_user_id;
    END IF;

    INSERT INTO users DEFAULT VALUES RETURNING user_id INTO v_user_id;
    BEGIN
        INSERT INTO identity_links (idp, idp_subject, user_id, last_login_at)
        VALUES (p_idp, p_subject, v_user_id, now());
    EXCEPTION WHEN unique_violation THEN
        -- 同時に初回ログインした場合: 先に作られたほうを使う
        DELETE FROM users WHERE user_id = v_user_id;
        SELECT user_id INTO v_user_id FROM identity_links
         WHERE idp = p_idp AND idp_subject = p_subject;
        RETURN v_user_id;
    END;

    INSERT INTO user_settings (user_id) VALUES (v_user_id);
    INSERT INTO audit_logs (actor_type, actor_id, action, target_user_id)
    VALUES ('system', 'resolve_user', 'user_created', v_user_id);
    RETURN v_user_id;
END
$$;

REVOKE ALL ON FUNCTION resolve_user(text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION resolve_user(text, text) TO mycompanion_app;
