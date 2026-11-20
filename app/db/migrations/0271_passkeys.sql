-- 0271 ログインの2段階の確かめ＝パスキー（2026-11-20。書いた人：CL-2。DESIGN_15-2 の928。計画書 14-1・CL-3 の連絡帳 4-1 の 1・CL-2 の連絡帳 4-456）
--
-- ✅ 2026-10-18 ユーザー（計画書 14-1）「2段階認証は任意で…確かめにパスキーも使える」。✅ 2026-11-20 ユーザー（「絶対に安全なのは何？」への答えのあと）「ア パスキーだけ」：
-- 2段階の確かめはパスキーだけ（偽のサイトで使えない・サーバーは公開の鍵だけを持つ）。端末を失くしたときのために、予備のパスキーを別の端末に登録でき、一度だけ使える
-- 予備の合言葉を用意する。
-- ⇒ パスキーを登録した方は、パスワード（か Google）のあと、パスキーか予備の合言葉で確かめるまで、ログインの印を使えない（判断は core/people.by_session の1か所）。
--    WebAuthn の確かめは core/passkeys の1か所（外の道具 webauthn を使ってよいのは、そこだけ＝機械が見張る）。どの表も書き換えない・消さない（いまの形は記録から読む）。
--    読み書きできるのは、ログインの確かめ（全体の取引のシステムの処理）だけ（0251 の person_sessions と同じ）。
CREATE TABLE salescall.person_passkeys (
  passkey_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  person_id      uuid NOT NULL REFERENCES salescall.people,
  credential_id  bytea NOT NULL UNIQUE CHECK (length(credential_id) BETWEEN 16 AND 1023),
  public_key     bytea NOT NULL CHECK (length(public_key) > 0),
  label          text NOT NULL CHECK (btrim(label) <> ''),          -- 管理者・契約者が付けた名前（「仕事の PC」など）
  added_at       timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE salescall.person_passkey_uses (                          -- 使った記録（署名の回数。前より小さければ写しの疑い＝断る）
  seq            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  passkey_id     uuid NOT NULL REFERENCES salescall.person_passkeys,
  sign_count     bigint NOT NULL CHECK (sign_count >= 0),
  used_at        timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE salescall.person_passkey_removals (
  passkey_id     uuid PRIMARY KEY REFERENCES salescall.person_passkeys,
  removed_at     timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE salescall.person_passkey_challenges (                    -- 1回だけ使える問い（登録・確かめ。短い期限）
  challenge      bytea PRIMARY KEY CHECK (length(challenge) >= 32),
  purpose        text NOT NULL CHECK (purpose IN ('register', 'login')),
  person_id      uuid NOT NULL REFERENCES salescall.people,
  token_sha      text REFERENCES salescall.person_sessions,           -- 確かめ（login）は、その2段目を待つログインの印
  expires_at     timestamptz NOT NULL,
  CHECK ((purpose = 'login') = (token_sha IS NOT NULL))
);
CREATE TABLE salescall.person_passkey_challenge_uses (
  challenge      bytea PRIMARY KEY REFERENCES salescall.person_passkey_challenges,
  used_at        timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE salescall.person_recovery_codes (                        -- 予備の合言葉（写しだけ。作り直すと前の組は使えない＝いちばん新しい組だけ）
  seq            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  person_id      uuid NOT NULL REFERENCES salescall.people,
  batch          uuid NOT NULL,
  code_sha       text NOT NULL UNIQUE CHECK (code_sha ~ '^[0-9a-f]{64}$'),
  created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE salescall.person_recovery_code_uses (
  seq            bigint PRIMARY KEY REFERENCES salescall.person_recovery_codes,
  used_at        timestamptz NOT NULL DEFAULT now()
);
-- ログインの印：作ったときに2段目が要ったか（パスキーを登録していたか）と、2段目を通った記録
ALTER TABLE salescall.person_sessions ADD COLUMN needs_second boolean NOT NULL DEFAULT false;
CREATE TABLE salescall.person_session_seconds (
  token_sha      text PRIMARY KEY REFERENCES salescall.person_sessions,
  how            text NOT NULL CHECK (how IN ('passkey', 'recovery_code')),
  at             timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE salescall.person_login_events DROP CONSTRAINT person_login_events_why_check;
ALTER TABLE salescall.person_login_events ADD CONSTRAINT person_login_events_why_check
  CHECK (why IN ('ok', 'no_such_login', 'wrong_password', 'no_password', 'inactive', 'locked',
                 'passkey_ok', 'passkey_failed', 'recovery_code_ok', 'recovery_code_failed'));
ALTER TABLE salescall.person_login_events DROP CONSTRAINT person_login_events_check;
ALTER TABLE salescall.person_login_events ADD CONSTRAINT person_login_events_ok CHECK (ok = (why IN ('ok', 'passkey_ok', 'recovery_code_ok')));

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['person_passkeys', 'person_passkey_uses', 'person_passkey_removals', 'person_passkey_challenges',
                           'person_passkey_challenge_uses', 'person_recovery_codes', 'person_recovery_code_uses', 'person_session_seconds'] LOOP
    EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON salescall.%I FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change()', t || '_frozen', t);
    EXECUTE format('ALTER TABLE salescall.%I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY scope ON salescall.%I USING (salescall.cur_tenant() IS NULL AND salescall.cur_actor_kind() = ''system'')'
                   ' WITH CHECK (salescall.cur_tenant() IS NULL AND salescall.cur_actor_kind() = ''system'')', t);
    EXECUTE format('GRANT SELECT, INSERT ON salescall.%I TO salescall_app', t);
  END LOOP;
END $$;
