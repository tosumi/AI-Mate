-- =====================================================================
-- 0019_proactive_daily: 自発発話の1日の上限を、キャラクタごとにする(本体仕様書 §15.1 の M8。2026-11-21 の決定)
--
-- - 0018 では、すべてのキャラクタを合わせた回数を proactive_quota(ユーザー単位)に持っていた。
--   上限をキャラクタごと(性格で1〜3回)にしたので、回数を proactive_state((ユーザー × キャラクタ))に移し、
--   proactive_quota は削除する(キャラクタをまたぐ情報を持たない)。
-- =====================================================================

ALTER TABLE proactive_state
    ADD COLUMN local_day  date,                                            -- ユーザーのローカル日
    ADD COLUMN sent_today integer NOT NULL DEFAULT 0 CHECK (sent_today >= 0);

DROP TABLE proactive_quota;
