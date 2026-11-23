-- =====================================================================
-- 0020_push_subscriptions: Web Push の購読(本体仕様書 §15.1 の M8 の「通知」。D5)
--
-- - ブラウザごとの購読(送り先のエンドポイントと、暗号化の鍵)。ユーザー単位で分離する。
-- - 同じエンドポイント(同じブラウザ)は、1人のユーザーにだけ属する。別のアカウントで登録し直すと、そちらへ移す
--   (RLS の下では、ほかのユーザーの行を消せないので、登録は関数で行う。関数が触れるのは、同じエンドポイントの行だけ)。
-- - アカウントの削除で消える(users の外部キー)。送り先から「もうない」と返ったら、ワーカーが消す。
-- =====================================================================

CREATE TABLE push_subscriptions (
    subscription_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         uuid NOT NULL REFERENCES users ON DELETE CASCADE,
    endpoint        text NOT NULL UNIQUE CHECK (endpoint LIKE 'https://%' AND char_length(endpoint) <= 1000),
    p256dh          text NOT NULL CHECK (char_length(p256dh) <= 200),
    auth            text NOT NULL CHECK (char_length(auth) <= 100),
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX push_subscriptions_user_idx ON push_subscriptions (user_id);

ALTER TABLE push_subscriptions ENABLE ROW LEVEL SECURITY;
CREATE POLICY push_subscriptions_isolation ON push_subscriptions FOR ALL TO mycompanion_app
    USING (user_id = app_user_id()) WITH CHECK (user_id = app_user_id());
GRANT SELECT, DELETE ON push_subscriptions TO mycompanion_app;

-- 自分の購読として登録する(同じエンドポイントがほかのユーザーにあれば、こちらへ移す)
CREATE OR REPLACE FUNCTION register_my_push_subscription(p_endpoint text, p_p256dh text, p_auth text, p_now timestamptz)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_user_id uuid := app_user_id();
BEGIN
    IF v_user_id IS NULL THEN
        RAISE EXCEPTION 'ユーザーが設定されていません';
    END IF;
    INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth, created_at, updated_at)
    VALUES (v_user_id, p_endpoint, p_p256dh, p_auth, p_now, p_now)
    ON CONFLICT (endpoint) DO UPDATE SET user_id = EXCLUDED.user_id, p256dh = EXCLUDED.p256dh,
        auth = EXCLUDED.auth, updated_at = EXCLUDED.updated_at;
END
$$;

REVOKE ALL ON FUNCTION register_my_push_subscription(text, text, text, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION register_my_push_subscription(text, text, text, timestamptz) TO mycompanion_app;
