-- 0077 契約者ごとのデータベースの合言葉（DESIGN.md 11章・15-2 の174。2026-10-18 ユーザー決定。書いた人：CL-2）
--
-- 見直しで見つかった穴：どの契約者か・誰か（管理者か、システムの処理か）は、取引ごとに読み書きの層が渡す値で決まる。この値は、
-- つないだ側が自由に入れられる。一方で、契約者の PC には、すべての契約者で共通のシステムの役の合言葉を置く形だった。⇒ その合言葉を
-- 持つ契約者は、値を書き換えて、ほかの契約者の行を読み書きでき、管理者としても動けた。
-- ユーザー（2026-10-18）：「SalesCall AIのアカウントが払い出され、（恐らく）ストライプで入金が完了するか、アカウントを作成するかしたら、
-- 合言葉も払い出されるのがいいんじゃないでしょうか？」
--
-- (1) 契約者ごとの合言葉（データベースのログインの役）：名前は「salescall_t_<契約者の番号>」。システムの役の権限を受け継ぐ。
--     作る・合言葉を替える・使えなくするのは、払い出し専用の役（salescall_provisioner。setup_roles.sql）だけ（core/logins.py）。
-- (2) **データベースが、つないだ合言葉から契約者を決める**（`salescall.login_tenant`）：契約者の合言葉でつないだら、取引で渡された
--     契約者がその契約者でなければ、どの行にも触れる前に断る（ほかの契約者の取引・全体の取引は開けない）。管理者としても動けない。
--     判断は `cur_tenant`・`cur_actor_kind` の1か所（行ごとの制限・守りの仕掛けは、どれもこれを読む）。管理者の PC・画面の機械の
--     システムの役の合言葉は、前と同じ。
-- (3) 行ごとの制限の無い表・持ち主の権限で動く関数で、契約者をまたげた所を塞ぐ：祝日の暦（書けるのは全体の取引だけ。契約者が
--     書けると、すべての契約者の電話の時間帯が変わる）・仕事の借用（契約者の合言葉は、その契約者の仕事だけ）・時給の数え方
--     （依頼者だけでなく契約者も見る）。
-- (4) 合言葉の払い出しの記録（tenant_login_events。合言葉そのものは持たない）。

-- ═════════ (2) つないだ合言葉から契約者を決める ═════════
CREATE FUNCTION salescall.login_tenant() RETURNS uuid LANGUAGE sql STABLE AS $$
  SELECT CASE WHEN session_user::text LIKE 'salescall\_t\_%' THEN substr(session_user::text, 13)::uuid END
$$;
CREATE FUNCTION salescall.refuse_scope(p_why text) RETURNS text LANGUAGE plpgsql STABLE AS $$
BEGIN
  RAISE EXCEPTION '%', p_why USING ERRCODE = 'insufficient_privilege', CONSTRAINT = 'login_scope';
END $$;

CREATE OR REPLACE FUNCTION salescall.cur_tenant() RETURNS uuid LANGUAGE sql STABLE AS $$
  SELECT CASE WHEN salescall.login_tenant() IS NULL
                   OR salescall.login_tenant() = nullif(current_setting('salescall.tenant_id', true), '')::uuid
              THEN nullif(current_setting('salescall.tenant_id', true), '')::uuid
              ELSE salescall.refuse_scope('この接続は、1人の契約者の分だけを扱います（ほかの契約者・全体の取引は開けません）')::uuid END
$$;
CREATE OR REPLACE FUNCTION salescall.cur_actor_kind() RETURNS text LANGUAGE sql STABLE AS $$
  SELECT CASE WHEN salescall.login_tenant() IS NOT NULL AND current_setting('salescall.actor_kind', true) = 'admin'
              THEN salescall.refuse_scope('契約者の接続では、管理者として動けません')
              ELSE nullif(current_setting('salescall.actor_kind', true), '') END
$$;

-- ═════════ (3) 契約者をまたげた所を塞ぐ ═════════
CREATE FUNCTION salescall.calendar_by_system() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF salescall.cur_tenant() IS NOT NULL THEN
    RAISE EXCEPTION '祝日の暦を書けるのは、全体の取引だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'calendar_by_system';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER calendar_loads_by_system BEFORE INSERT ON salescall.calendar_loads
  FOR EACH ROW EXECUTE FUNCTION salescall.calendar_by_system();
CREATE TRIGGER calendar_holidays_by_system BEFORE INSERT ON salescall.calendar_holidays
  FOR EACH ROW EXECUTE FUNCTION salescall.calendar_by_system();

CREATE OR REPLACE FUNCTION salescall.take_job(p_key text, p_holder text, p_seconds integer) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
DECLARE got text;
BEGIN
  IF salescall.cur_actor_kind() IS DISTINCT FROM 'system' THEN
    RAISE EXCEPTION '見守り役（システムの処理）だけが呼べます' USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF salescall.login_tenant() IS NOT NULL AND p_key NOT LIKE '%:' || salescall.login_tenant()::text THEN
    RAISE EXCEPTION '契約者の接続で借りられるのは、その契約者の仕事だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'login_scope';
  END IF;
  INSERT INTO salescall.job_leases AS j (job_key, holder, expires_at)
  VALUES (p_key, p_holder, now() + make_interval(secs => p_seconds))
  ON CONFLICT (job_key) DO UPDATE SET holder = EXCLUDED.holder, expires_at = EXCLUDED.expires_at
    WHERE j.expires_at < now() OR j.holder = EXCLUDED.holder
  RETURNING holder INTO got;
  RETURN got IS NOT NULL;
END $$;

CREATE OR REPLACE FUNCTION salescall.work_seconds(p_from timestamptz, p_to timestamptz)
RETURNS TABLE (at timestamptz, seconds numeric)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT a.claimed_at,
         extract(epoch FROM a.ended_at - greatest(a.claimed_at, p_from))
         + CASE WHEN a.purpose = 'resume' THEN 0
                ELSE coalesce((SELECT t.hourly_gap_seconds FROM salescall.reward_terms t
                               WHERE t.client_id = a.client_id AND t.valid_from <= a.claimed_at
                               ORDER BY t.valid_from DESC LIMIT 1), 0) END
  FROM salescall.attempts a
  WHERE a.client_id = salescall.cur_client() AND salescall.cur_client() IS NOT NULL
    AND a.tenant_id = salescall.cur_tenant()
    AND a.state NOT IN ('claimed', 'released') AND a.ended_at IS NOT NULL AND a.purpose <> 'apology'
    AND a.ended_at > p_from AND a.claimed_at <= p_to
$$;

-- ═════════ (4) 払い出しの記録 ═════════
CREATE TABLE salescall.tenant_login_events (
  event_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES salescall.tenants,
  kind      text NOT NULL CHECK (kind IN ('issued', 'revoked')),   -- 払い出した（初めて・替えた）・使えなくした
  done_by   text NOT NULL CHECK (btrim(done_by) <> ''),
  done_at   timestamptz NOT NULL DEFAULT now()
);
CREATE FUNCTION salescall.tenant_login_event_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF salescall.cur_tenant() IS NOT NULL OR coalesce(salescall.cur_actor_kind(), '') NOT IN ('admin', 'system') THEN
    RAISE EXCEPTION '合言葉を払い出すのは、全体の取引の管理者かシステムの処理だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'tenant_login_by_system';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER tenant_login_event_guard BEFORE INSERT ON salescall.tenant_login_events
  FOR EACH ROW EXECUTE FUNCTION salescall.tenant_login_event_guard();
CREATE TRIGGER tenant_login_events_frozen BEFORE UPDATE OR DELETE ON salescall.tenant_login_events
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
ALTER TABLE salescall.tenant_login_events ENABLE ROW LEVEL SECURITY;
CREATE POLICY system_only ON salescall.tenant_login_events
  USING (salescall.cur_tenant() IS NULL) WITH CHECK (salescall.cur_tenant() IS NULL);
GRANT SELECT, INSERT ON salescall.tenant_login_events TO salescall_app;
