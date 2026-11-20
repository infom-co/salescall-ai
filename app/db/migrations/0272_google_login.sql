-- 0272 Google のアカウントでのログイン（2026-11-20。書いた人：CL-2。DESIGN_15-2 の929。計画書 14-1・CL-3 の連絡帳 4-1 の 1・CL-2 の連絡帳 4-457）
--
-- ✅ 2026-10-18 ユーザー（計画書 14-1）「ログインは ID とパスワードか Google のアカウント」。
-- ⇒ ログインした方が、自分の Google のアカウントを結び付ける（Google の印の sub＝変わらない番号で結ぶ。メールは見せるためだけ）。1つの Google のアカウントは
--    1人の方にだけ結び付く（データベースが守る）。結び付けた方は、Google でログインできる（パスキーを登録した方は2段目が要る＝0271 のまま）。
--    どの記録も書き換えない（いまの結び付きは、その sub のいちばん新しい記録）。読み書きは、ログインの確かめ（全体の取引のシステムの処理）だけ。
CREATE TABLE salescall.person_google_links (
  seq          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  person_id    uuid NOT NULL REFERENCES salescall.people,
  sub          text NOT NULL CHECK (sub ~ '^[0-9A-Za-z_-]{1,255}$'),
  email        text NOT NULL CHECK (position('@' in email) > 1),
  linked       boolean NOT NULL,                                     -- 真＝結び付けた／偽＝外した
  at           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX person_google_links_sub ON salescall.person_google_links (sub, seq DESC);
CREATE FUNCTION salescall.person_google_link_guard() RETURNS trigger LANGUAGE plpgsql SET search_path = salescall, pg_temp AS $$
DECLARE cur salescall.person_google_links;
BEGIN
  SELECT * INTO cur FROM salescall.person_google_links l WHERE l.sub = NEW.sub ORDER BY l.seq DESC LIMIT 1;
  IF NEW.linked AND FOUND AND cur.linked AND cur.person_id <> NEW.person_id THEN
    RAISE EXCEPTION 'この Google のアカウントは、ほかのログインに結び付いています' USING ERRCODE = 'unique_violation', CONSTRAINT = 'person_google_one_person';
  END IF;
  IF NOT NEW.linked AND NOT (FOUND AND cur.linked AND cur.person_id = NEW.person_id) THEN
    RAISE EXCEPTION '結び付いていない Google のアカウントは外せません' USING ERRCODE = 'check_violation', CONSTRAINT = 'person_google_unlink_own';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER person_google_link_guard BEFORE INSERT ON salescall.person_google_links FOR EACH ROW EXECUTE FUNCTION salescall.person_google_link_guard();
CREATE TRIGGER person_google_links_frozen BEFORE UPDATE OR DELETE ON salescall.person_google_links FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
ALTER TABLE salescall.person_google_links ENABLE ROW LEVEL SECURITY;
CREATE POLICY scope ON salescall.person_google_links
  USING (salescall.cur_tenant() IS NULL AND salescall.cur_actor_kind() = 'system')
  WITH CHECK (salescall.cur_tenant() IS NULL AND salescall.cur_actor_kind() = 'system');
GRANT SELECT, INSERT ON salescall.person_google_links TO salescall_app;

ALTER TABLE salescall.person_login_events DROP CONSTRAINT person_login_events_why_check;
ALTER TABLE salescall.person_login_events ADD CONSTRAINT person_login_events_why_check
  CHECK (why IN ('ok', 'no_such_login', 'wrong_password', 'no_password', 'inactive', 'locked',
                 'passkey_ok', 'passkey_failed', 'recovery_code_ok', 'recovery_code_failed', 'google_ok', 'google_unlinked'));
ALTER TABLE salescall.person_login_events DROP CONSTRAINT person_login_events_ok;
ALTER TABLE salescall.person_login_events ADD CONSTRAINT person_login_events_ok CHECK (ok = (why IN ('ok', 'passkey_ok', 'recovery_code_ok', 'google_ok')));
