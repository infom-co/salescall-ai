-- 0079 PC のデータベースの鍵を、PC ごとにする（DESIGN.md 11章・15-2 の176。2026-10-18 ユーザー決定。書いた人：CL-2）
--
-- ユーザー（2026-10-18）：「合言葉というより、SalesCall AIのログインとパスワードにより、データベースを判断できないの？」
-- 「グーグルIDでログインしたり、パスキーを設定したりもできますか？」⇒ 契約者は、PC からブラウザで SalesCall AI にログインし
-- （ID とパスワード・Google・パスキー）、サーバーがその PC の鍵を渡す（core/logins.py・supervisor/pc_login.py）。
-- 0077 は契約者ごとに鍵1つだったので、2台目の PC でログインすると、1台目の鍵が使えなくなった。
-- ⇒ 鍵（データベースのログインの役）は PC ごと：名前は「salescall_t_<契約者の番号>_<PC の番号>」。データベースは、名前の頭の
--    契約者の番号から契約者を決める（login_tenant。決まりは前と同じ1か所）。PC ごとにログインを外せ、契約が終わったら、その
--    契約者のすべての PC の鍵を止める。払い出しの記録に、どの PC か（番号と、契約者に見せる PC の名前）を持つ。

CREATE OR REPLACE FUNCTION salescall.login_tenant() RETURNS uuid LANGUAGE sql STABLE AS $$
  SELECT CASE WHEN session_user::text LIKE 'salescall\_t\_%' THEN substr(session_user::text, 13, 36)::uuid END
$$;

ALTER TABLE salescall.tenant_login_events
  ADD COLUMN pc text CHECK (pc ~ '^[0-9a-f]{8}$'),                         -- どの PC の鍵か（NULL＝その契約者のすべての PC）
  ADD COLUMN pc_label text CHECK (pc_label IS NULL OR btrim(pc_label) <> ''),   -- 契約者に見せる PC の名前（ログインしたときの名前）
  ADD CONSTRAINT tenant_login_events_issue_has_pc CHECK (kind <> 'issued' OR pc IS NOT NULL);
