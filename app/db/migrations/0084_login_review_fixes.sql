-- 0084 契約者の鍵の見直しで見つかった穴を塞ぐ（DESIGN.md 11章・15-2 の180。2026-10-18 の見直し。書いた人：CL-2）
--
-- 見直し（使い捨てのデータベースで、契約者の鍵の役を作って実際に試した）で見つかった穴：
-- (1) 管理者あての知らせ（契約者の欄が空・管理者用の印つき）は、誰の種類が「system」でも読めた ⇒ 契約者の取引を種類「system」で
--     開くと、ほかの契約者の管理者あての知らせ（控えの失敗・エラーの中身）が読めた。⇒ 管理者あての知らせは、全体の取引だけ。
-- (2) 払い出し専用の役が、役を作る権限とシステムの役の管理権を直接持っていた ⇒ システムの役の合言葉を替える・名前を自由に付けた
--     システムの役を作る（契約者で絞られない）・自分をシステムの役にする、ができた。⇒ 権限は、ログインできない役
--     （salescall_login_maker。setup_roles.sql）だけが持ち、次の2つの関数の中でだけ使う。払い出し専用の役は、関数を呼べるだけ。
--     - make_pc_login：名前が「salescall_t_<契約者の番号>_<PC の番号>」の形、合言葉が確かめの値（SCRAM-SHA-256）の形のときだけ、
--       作る・替える（合言葉の平文は受け取らない）。
--     - stop_pc_logins：その形の名前（または、その契約者のすべての PC＝名前の頭）の鍵を止め、**つないだままの接続も切る**（前は
--       止めても、開いたままの接続で読み書きを続けられた）。止めた（ある）鍵の数を返す（もう止まっていても数える＝やり直せる）。
-- (3) 控えのフォルダ名が使われているかを、すべての契約者の中で探していた ⇒ ほかの契約者に同じ名前の依頼者がいるかが分かった
--     （フォルダ名もずれた）。⇒ フォルダは PC（契約者）ごとのハードディスクなので、重なりは契約者の中だけで見る。
-- (4) 仕事の借用を返す関数（return_job）に確かめが無く、全体の仕事・ほかの契約者の仕事の借用も返せた ⇒ 借りるときと同じ決まり
--     （salescall.job_key_allowed の1か所）を、借りる・返すの両方で読む。
-- (5) 停止による予約の取り消し（release_claims_for_stop）が、ほかの契約者の停止の番号でも動いた ⇒ その取引から見える範囲の
--     停止だけ（全体の取引＝どれでも・契約者の取引＝その契約者の停止と全体の停止）。

-- ═════════ (1) 管理者あての知らせ ═════════
DROP POLICY notice_scope ON salescall.notification_events;
CREATE POLICY notice_scope ON salescall.notification_events
  USING ((tenant_id IS NULL AND (NOT for_admin
                                 OR (salescall.cur_tenant() IS NULL AND salescall.cur_actor_kind() IN ('admin', 'system'))))
         OR (tenant_id = salescall.cur_tenant()
             AND (client_id IS NULL OR salescall.cur_client() IS NULL OR client_id = salescall.cur_client())))
  WITH CHECK ((tenant_id IS NULL AND salescall.cur_tenant() IS NULL AND NOT for_admin)
              OR (tenant_id = salescall.cur_tenant()
                  AND client_id IS NOT DISTINCT FROM salescall.cur_client()));

-- ═════════ (2) PC の鍵を作る・止める ═════════
CREATE FUNCTION salescall.make_pc_login(p_role text, p_verifier text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
BEGIN
  IF p_role IS NULL OR p_role !~ '^salescall_t_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}_[0-9a-f]{8}$' THEN
    RAISE EXCEPTION 'PC の鍵の名前の形が違います' USING ERRCODE = 'insufficient_privilege', CONSTRAINT = 'pc_login_name';
  END IF;
  IF p_verifier IS NULL OR p_verifier !~ '^SCRAM-SHA-256\$[0-9]+:[A-Za-z0-9+/=]+\$[A-Za-z0-9+/=]+:[A-Za-z0-9+/=]+$' THEN
    RAISE EXCEPTION 'PC の鍵の合言葉は、確かめの値（SCRAM-SHA-256）で渡します' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'pc_login_verifier';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = p_role) THEN
    EXECUTE format('ALTER ROLE %I LOGIN PASSWORD %L', p_role, p_verifier);
  ELSE
    EXECUTE format('CREATE ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE IN ROLE salescall_app',
                   p_role, p_verifier);
  END IF;
END $$;

CREATE FUNCTION salescall.stop_pc_logins(p_name text) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
DECLARE r record; n integer := 0;
BEGIN
  IF p_name IS NULL OR p_name !~ '^salescall_t_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}_([0-9a-f]{8})?$' THEN
    RAISE EXCEPTION 'PC の鍵の名前（または、その契約者のすべての PC＝名前の頭）の形が違います' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'pc_login_name';
  END IF;
  FOR r IN SELECT rolname FROM pg_roles
           WHERE CASE WHEN p_name LIKE '%\_' THEN starts_with(rolname, p_name) ELSE rolname = p_name END LOOP
    EXECUTE format('ALTER ROLE %I NOLOGIN', r.rolname);
    PERFORM pg_terminate_backend(a.pid) FROM pg_stat_activity a WHERE a.usename = r.rolname;   -- 開いたままの接続も切る
    n := n + 1;
  END LOOP;
  RETURN n;
END $$;

-- 持ち主を作る役にする（持ち主を移すには、移す先がその場所に作る権限を持つ決まり。移したら外す）
GRANT USAGE, CREATE ON SCHEMA salescall TO salescall_login_maker;
ALTER FUNCTION salescall.make_pc_login(text, text) OWNER TO salescall_login_maker;
ALTER FUNCTION salescall.stop_pc_logins(text) OWNER TO salescall_login_maker;
REVOKE CREATE ON SCHEMA salescall FROM salescall_login_maker;
REVOKE ALL ON FUNCTION salescall.make_pc_login(text, text), salescall.stop_pc_logins(text) FROM PUBLIC;
GRANT USAGE ON SCHEMA salescall TO salescall_provisioner;
GRANT EXECUTE ON FUNCTION salescall.make_pc_login(text, text), salescall.stop_pc_logins(text) TO salescall_provisioner;

-- ═════════ (3) 控えのフォルダ名の重なりは、契約者の中だけ ═════════
DROP INDEX salescall.client_backup_folders_one_name;
DO $$
DECLARE c text;
BEGIN   -- 列に付けた「1つだけ」の約束（0054。すべての契約者の中で1つ）を外す
  SELECT conname INTO STRICT c FROM pg_constraint
  WHERE conrelid = 'salescall.client_backup_folders'::regclass AND contype = 'u';
  EXECUTE format('ALTER TABLE salescall.client_backup_folders DROP CONSTRAINT %I', c);
END $$;
CREATE UNIQUE INDEX client_backup_folders_one_name ON salescall.client_backup_folders (tenant_id, lower(folder_name));
CREATE OR REPLACE FUNCTION salescall.backup_folder_taken(p_name text) RETURNS boolean
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
BEGIN
  PERFORM salescall.only_backup_system();
  RETURN EXISTS (SELECT 1 FROM salescall.client_backup_folders
                 WHERE tenant_id = salescall.cur_tenant() AND lower(folder_name) = lower(p_name));
END $$;

-- ═════════ (4) 仕事の借用：借りる・返すは同じ決まり ═════════
CREATE FUNCTION salescall.job_key_allowed(p_key text) RETURNS void LANGUAGE plpgsql STABLE AS $$
BEGIN
  IF salescall.cur_actor_kind() IS DISTINCT FROM 'system' THEN
    RAISE EXCEPTION '見守り役（システムの処理）だけが呼べます' USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF salescall.login_tenant() IS NOT NULL AND p_key NOT LIKE '%:' || salescall.login_tenant()::text THEN
    RAISE EXCEPTION '契約者の接続で借りる・返すのは、その契約者の仕事だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'login_scope';
  END IF;
END $$;
CREATE OR REPLACE FUNCTION salescall.take_job(p_key text, p_holder text, p_seconds integer) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
DECLARE got text;
BEGIN
  PERFORM salescall.job_key_allowed(p_key);
  INSERT INTO salescall.job_leases AS j (job_key, holder, expires_at)
  VALUES (p_key, p_holder, now() + make_interval(secs => p_seconds))
  ON CONFLICT (job_key) DO UPDATE SET holder = EXCLUDED.holder, expires_at = EXCLUDED.expires_at
    WHERE j.expires_at < now() OR j.holder = EXCLUDED.holder
  RETURNING holder INTO got;
  RETURN got IS NOT NULL;
END $$;
CREATE OR REPLACE FUNCTION salescall.return_job(p_key text, p_holder text) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
DECLARE got boolean;
BEGIN
  PERFORM salescall.job_key_allowed(p_key);
  UPDATE salescall.job_leases SET expires_at = now() WHERE job_key = p_key AND holder = p_holder RETURNING true INTO got;
  RETURN got;
END $$;

-- ═════════ (5) 停止による予約の取り消しは、その取引から見える停止だけ ═════════
CREATE OR REPLACE FUNCTION salescall.release_claims_for_stop(p_event_id bigint) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
DECLARE s salescall.stop_events; n integer;
BEGIN
  SELECT * INTO s FROM salescall.open_stop_events e WHERE e.event_id = p_event_id
    AND (salescall.cur_tenant() IS NULL OR e.tenant_id IS NULL OR e.tenant_id = salescall.cur_tenant());
  IF NOT FOUND THEN
    RAISE EXCEPTION 'まだ解かれていない、その停止はありません' USING ERRCODE = 'check_violation';
  END IF;
  UPDATE salescall.attempts a SET state = 'released'
  WHERE a.state = 'claimed'
    AND (s.scope = 'system'
         OR (s.scope = 'tenant' AND a.tenant_id = s.tenant_id)
         OR (s.scope = 'client' AND a.client_id = s.client_id)
         OR (s.scope = 'business' AND a.client_id = s.client_id
             AND a.version_id IN (SELECT v.version_id FROM salescall.business_versions v WHERE v.business_id = s.business_id))
         OR (s.scope = 'line' AND a.line_id = s.line_id));
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$;
