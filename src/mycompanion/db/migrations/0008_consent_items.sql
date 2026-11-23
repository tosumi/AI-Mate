-- =====================================================================
-- 0008_consent_items: 同意の項目の追加と、同意の内容の記録(legal/consent.md。本体仕様書 §13.4.4)
--
-- - 追加する種類: third_party_ai(AI サービスへの送信)、guardian_confirmed(保護者の同意の確認)、
--   sensitive_memory(センシティブな内容の記憶。任意)、romantic_expression(恋愛的な表現。任意)。
-- - 表示した文面のハッシュと、取得したチャンネルを記録する(何に同意したかを、あとで示せるように)。
-- =====================================================================

ALTER TABLE consents DROP CONSTRAINT consents_type_check;
ALTER TABLE consents ADD CONSTRAINT consents_type_check CHECK (type IN (
    'terms', 'privacy', 'emotion_data', 'memory', 'apac_processing',
    'third_party_ai', 'guardian_confirmed', 'sensitive_memory', 'romantic_expression'
));

ALTER TABLE consents
    ADD COLUMN text_hash text,    -- 表示した画面の文面の SHA-256
    ADD COLUMN channel   text CHECK (channel IN ('cli', 'web', 'ios', 'android', 'line'));
