-- 0107 サーバーと PC の分け方の (D)：PC がデータベースにつながなくなったので、PC のデータベースの鍵の仕組みを外す（DESIGN.md 5-5・11章）。
-- 書いた人：CL-2
--
-- 2026-10-21 ユーザー：「最近はREAなどでプログラムを読み取るのが簡単になっているようなので、読み取られないように対応する必要が
-- あります。その上での最適解を考えてください」⇒ 承認：契約者の PC には電話をつなぐ部分（PC の部品）だけを置き、それ以外はサーバーで
-- 動かす。PC はサーバーにだけつなぎ、PC ごとの通行証（0105 pc_agents）で、どの契約者のどの PC かを決める。
-- ⇒ 0077・0079・0084 の「PC ごとのデータベースのログインの役（salescall_t_…）と、データベースがその役から契約者を決める仕組み」は、
--    使う所が無くなった。残すと、使われない道（鍵を作る関数・払い出し専用の役）が残るので外す。
--    0077・0084 で一緒に塞いだ、契約者をまたげた所（祝日の暦は全体の取引だけ・時給の数え方は契約者も見る・管理者あての知らせは全体の
--    取引だけ・停止による予約の取り消しは見える停止だけ・控えのフォルダ名の重なりは契約者の中だけ）は、そのまま残す（どの接続にも効く守り）。
--
-- (1) 契約者の決め方を、取引で渡された値だけに戻す（判断は cur_tenant・cur_actor_kind の1か所のまま）。そのうえで、**データベースは
--     サーバーの役（salescall_app）と表の持ち主だけを受け付ける**（`salescall.server_session` の1か所。2026-10-21 ユーザー：「人に頼る
--     システムは極力作るな」）：前の PC の鍵の役は、作った役（払い出し専用の役）の権限でしか消せず、表の作りの役では消せない
--     （試して確かめた：消す・ログインを止める・システムの役の権限を外す、のどれも断られる）。消せずに残っても、この守りで、
--     その役は何も読めず書けない（取引の値を読む関数が断る・行ごとの制限の無い表にも、同じ守りの決まりを付ける・持ち主の権限で
--     動く関数も、取引の値を読むか、この守りを呼ぶ）。⇒ 人が手で消す手順は要らない。
-- (2) 仕事の借用の確かめ（job_key_allowed）は「システムの処理だけ」に戻す。
-- (3) PC の鍵を作る・止める関数と、払い出しの記録の表を外す。PC の鍵の役が残っていれば、消せるものは消す（消せないものは (1) の守りで
--     何もできない。表の作りは止めない）。
-- (4) 落ちた PC の回線の後始末の契約者の探し方（tenants_with_stale_lines）と、その長さの引数を外す（回線を借りるのは中央の見守り役
--     だけ＝前の自分・前の見守り役の回線は、起動のときと決まった間隔の後始末で足りる）。
-- 払い出し専用の役（salescall_provisioner）と鍵を作る役（salescall_login_maker）は、前の表の作りを流すために setup_roles.sql が
-- 作り続ける（どちらも、つなげない役。この表の作りのあとは、何の権限も持たない）。

-- ═════════ (1) 契約者の決め方と、受け付ける役 ═════════
-- つないだ役（session_user＝持ち主の権限で動く関数の中でも変わらない）が、サーバーの役か表の持ち主（か、データベースの管理者）か。
CREATE FUNCTION salescall.server_session() RETURNS boolean LANGUAGE sql STABLE AS $$
  SELECT CASE WHEN session_user::text IN ('salescall_app', 'salescall_owner') THEN true
              ELSE coalesce((SELECT r.rolsuper FROM pg_roles r WHERE r.rolname = session_user), false) END
$$;
CREATE FUNCTION salescall.refuse_outsider() RETURNS text LANGUAGE plpgsql STABLE AS $$
BEGIN
  RAISE EXCEPTION 'このデータベースを使えるのは、SalesCall AI のサーバーだけです' USING ERRCODE = 'insufficient_privilege',
    CONSTRAINT = 'server_only';
END $$;
CREATE FUNCTION salescall.server_only() RETURNS void LANGUAGE plpgsql STABLE AS $$
BEGIN
  IF NOT salescall.server_session() THEN
    PERFORM salescall.refuse_outsider();
  END IF;
END $$;
-- 取引の値を読む4つ（行ごとの制限・守りの仕掛け・持ち主の権限で動く関数は、どれもこれを読む）は、ほかの役には答えずに断る
CREATE OR REPLACE FUNCTION salescall.cur_tenant() RETURNS uuid LANGUAGE sql STABLE AS $$
  SELECT CASE WHEN salescall.server_session() THEN nullif(current_setting('salescall.tenant_id', true), '')::uuid
              ELSE salescall.refuse_outsider()::uuid END
$$;
CREATE OR REPLACE FUNCTION salescall.cur_client() RETURNS uuid LANGUAGE sql STABLE AS $$
  SELECT CASE WHEN salescall.server_session() THEN nullif(current_setting('salescall.client_id', true), '')::uuid
              ELSE salescall.refuse_outsider()::uuid END
$$;
CREATE OR REPLACE FUNCTION salescall.cur_actor() RETURNS text LANGUAGE sql STABLE AS $$
  SELECT CASE WHEN salescall.server_session() THEN nullif(current_setting('salescall.actor', true), '')
              ELSE salescall.refuse_outsider() END
$$;
CREATE OR REPLACE FUNCTION salescall.cur_actor_kind() RETURNS text LANGUAGE sql STABLE AS $$
  SELECT CASE WHEN salescall.server_session() THEN nullif(current_setting('salescall.actor_kind', true), '')
              ELSE salescall.refuse_outsider() END
$$;
-- 行ごとの制限の無い表（すべての契約者に共通の決まり・暦・声 など）にも、同じ守りの決まりを付ける（表の持ち主は、決まりを通らない）
DO $$
DECLARE t text;
BEGIN
  FOR t IN SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname = 'salescall' AND c.relkind = 'r' AND NOT c.relrowsecurity LOOP
    EXECUTE format('ALTER TABLE salescall.%I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY server_only ON salescall.%I USING (salescall.server_session())'
                   ' WITH CHECK (salescall.server_session())', t);
  END LOOP;
END $$;
-- 持ち主の権限で動く関数のうち、取引の値を読まない2つにも、守りを入れる（ほかは、取引の値を読む＝断る）
CREATE OR REPLACE FUNCTION salescall.next_customer_promise_at(p_except uuid) RETURNS timestamptz
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
BEGIN
  PERFORM salescall.server_only();
  RETURN (SELECT min(w.callee_at) FROM salescall.promises_waiting() w WHERE w.callee_at > now() AND w.task_id <> p_except);
END $$;
CREATE OR REPLACE FUNCTION salescall.release_expired_claims(p_task uuid, p_contact uuid, p_phone text, p_line uuid)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
DECLARE n integer;
BEGIN
  PERFORM salescall.server_only();
  UPDATE salescall.attempts SET state = 'released'
  WHERE state = 'claimed' AND claim_expires_at <= now()
    AND (task_id = p_task OR contact_id = p_contact OR phone_e164 = p_phone OR line_id = p_line);
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$;

-- ═════════ (2) 仕事の借用 ═════════
CREATE OR REPLACE FUNCTION salescall.job_key_allowed(p_key text) RETURNS void LANGUAGE plpgsql STABLE AS $$
BEGIN
  IF salescall.cur_actor_kind() IS DISTINCT FROM 'system' THEN
    RAISE EXCEPTION '見守り役（システムの処理）だけが呼べます' USING ERRCODE = 'insufficient_privilege';
  END IF;
END $$;

DROP FUNCTION salescall.login_tenant();
DROP FUNCTION salescall.refuse_scope(text);

-- ═════════ (3) PC の鍵を作る・止める関数と、払い出しの記録 ═════════
SET ROLE salescall_login_maker;   -- 2つの関数の持ち主（0084）。残っている PC の鍵の役も、この役が作った
DO $$
DECLARE r record;
BEGIN
  FOR r IN SELECT rolname FROM pg_roles WHERE rolname LIKE 'salescall\_t\_%' LOOP
    BEGIN   -- 0084 からの役（この役が作った）は消せる。それより前の役（払い出し専用の役が作った）は消せないが、(1) の守りで何もできない
      EXECUTE format('ALTER ROLE %I NOLOGIN', r.rolname);
      PERFORM pg_terminate_backend(a.pid) FROM pg_stat_activity a WHERE a.usename = r.rolname;
      EXECUTE format('DROP ROLE %I', r.rolname);
    EXCEPTION WHEN insufficient_privilege THEN
      RAISE NOTICE '前の PC の鍵の役 % は消せません（つないでも、データベースが断ります）', r.rolname;
    END;
  END LOOP;
END $$;
DROP FUNCTION salescall.make_pc_login(text, text);
DROP FUNCTION salescall.stop_pc_logins(text);
RESET ROLE;
REVOKE USAGE ON SCHEMA salescall FROM salescall_provisioner, salescall_login_maker;

DROP TABLE salescall.tenant_login_events;
DROP FUNCTION salescall.tenant_login_event_guard();

-- ═════════ (4) 落ちた PC の回線の後始末 ═════════
DROP FUNCTION salescall.tenants_with_stale_lines(interval);
DROP FUNCTION salescall.recover_lines(text, uuid, text, interval);
-- p_holder を渡したら「その見守り役が借りている回線」（起動したとき＝前に落ちた自分の分。p_line を渡せば、その回線だけ＝回線の流れが
-- 止まったとき）、NULL なら「借用の期限が切れた回線」（決まった間隔で＝前に動いていた見守り役の分。p_except＝呼んだ見守り役の名前。
-- 自分が借りている回線は除く）
CREATE FUNCTION salescall.recover_lines(p_holder text, p_line uuid DEFAULT NULL, p_except text DEFAULT NULL)
RETURNS TABLE (line_id uuid, released integer, ended integer)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
DECLARE l record; r integer; e integer;
BEGIN
  IF salescall.cur_tenant() IS NULL THEN
    RAISE EXCEPTION '契約者の取引で呼びます' USING ERRCODE = 'insufficient_privilege';
  END IF;
  FOR l IN SELECT x.line_id FROM salescall.lines x
           WHERE x.tenant_id = salescall.cur_tenant() AND x.lease_holder IS NOT NULL
             AND (CASE WHEN p_holder IS NULL
                       THEN x.lease_expires_at < now() AND x.lease_holder IS DISTINCT FROM p_except
                       ELSE x.lease_holder = p_holder END)
             AND (p_line IS NULL OR x.line_id = p_line)
           FOR UPDATE LOOP
    UPDATE salescall.attempts SET state = 'released' WHERE attempts.line_id = l.line_id AND state = 'claimed';
    GET DIAGNOSTICS r = ROW_COUNT;
    UPDATE salescall.attempts SET state = 'technical_error' WHERE attempts.line_id = l.line_id AND salescall.is_active(state);
    GET DIAGNOSTICS e = ROW_COUNT;
    IF p_line IS NULL THEN   -- 回線の流れだけが止まったとき（p_line）は、借用を続けたまま立ち上げ直す
      UPDATE salescall.lines SET lease_holder = NULL, lease_expires_at = NULL WHERE lines.line_id = l.line_id;
    END IF;
    line_id := l.line_id; released := r; ended := e;
    RETURN NEXT;
  END LOOP;
END $$;
REVOKE ALL ON FUNCTION salescall.recover_lines(text, uuid, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.recover_lines(text, uuid, text) TO salescall_app;
