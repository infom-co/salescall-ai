-- 0241 PC のログインの待ちを、データベースに置く（2026-11-18。書いた人：CL-2。DESIGN.md 5-5・15-2 の867。CL-3 のお願い 26 への答え）
--
-- ⛔ 前は、PC のログイン（RFC 8628 の機器のログイン）の待っている間の記録（確かめのコード・契約者の許可）を、画面のサーバーの手元の記憶にだけ
--    置いていた（CL-3 の web/pc_login.py）。画面のサーバーを2台以上で動かすと、PC が始めたサーバーと、契約者が許可したページのサーバー・
--    PC が受け取りを聞くサーバーが違えば、許可が見えず、PC はログインできない（期限切れでやり直し続ける）。
--    ✅ 2026-11-18 ユーザー：「私だけでも、メインパソコンとサブパソコンがあるし、契約者が増えたら、2台以上は確実なんじゃないの？」→ 表を作る案に「OK」
-- ⇒ 待っている間の記録を、この表の1か所に置く。どのサーバーからも同じ記録が見え、時刻はデータベースの時計の1つで数える。
--    - 始める（pc_login_start）：PC が始める＝ログイン不要。サーバーの全体の取引（システムの処理）だけが呼ぶ。期限の切れた待ちはここで消す。
--      PC の待ちの印（device_code）は指紋だけを置く（通行証と同じ。値は保存しない）。
--    - ページに出す（pc_login_waiting）・許可する／断る（pc_login_decide）：ログインした管理者か契約者だけ（その契約者の取引）。許可した契約者が、
--      その PC の契約者になる。前の PC（PC が名乗った入れ替える PC）は、その契約者の PC のときだけ名前を見せる。
--    - 受け取りを聞く（pc_login_poll）：サーバーの全体の取引だけ。待ち・遅く（聞く間隔を5秒延ばす＝RFC 8628 3.5）・断られた・期限切れ・許可済み。
--    - 受け取る（pc_login_take）：許可した契約者の取引で、システムの処理だけ。待ちを消すのと通行証を払い出すのが同じ取引＝払い出しは1回だけ
--      （2台のサーバーが同時に聞いても、消せたほうだけが払い出す）。
--    表は、どの役も直接は読み書きしない（関数だけ）。

CREATE TABLE salescall.pc_logins (
  device_sha  text PRIMARY KEY CHECK (device_sha ~ '^[0-9a-f]{64}$'),        -- PC の待ちの印の指紋だけ
  user_code   text NOT NULL UNIQUE CHECK (user_code ~ '^[BCDFGHJKLMNPQRSTVWXZ]{4}-[BCDFGHJKLMNPQRSTVWXZ]{4}$'),   -- 確かめのコード
  pc_label    text NOT NULL CHECK (btrim(pc_label) <> '' AND length(pc_label) <= 60),
  replaces    uuid,                                                          -- この PC が前に持っていた PC の番号（PC が名乗る）
  started_at  timestamptz NOT NULL DEFAULT now(),
  expires_at  timestamptz NOT NULL,
  poll_every  integer NOT NULL CHECK (poll_every > 0),                       -- 受け取りを聞いてよい間隔（秒）
  polled_at   timestamptz,
  decision    text CHECK (decision IN ('approved', 'denied')),
  tenant_id   uuid REFERENCES salescall.tenants,                             -- 許可した（断った）契約者
  decided_by  text CHECK (decided_by IS NULL OR btrim(decided_by) <> ''),
  decided_at  timestamptz,
  CHECK (expires_at > started_at),
  CHECK ((decision IS NULL) = (tenant_id IS NULL) AND (decision IS NULL) = (decided_by IS NULL)
         AND (decision IS NULL) = (decided_at IS NULL))
);
ALTER TABLE salescall.pc_logins ENABLE ROW LEVEL SECURITY;   -- 決まりを書かない＝関数のほかからは見えない
REVOKE ALL ON salescall.pc_logins FROM PUBLIC;

-- 始める（PC の名前と入れ替える PC は PC が名乗る。確かめのコードが重なったら false＝呼んだ側が別のコードで呼び直す）
CREATE FUNCTION salescall.pc_login_start(p_device_sha text, p_user_code text, p_label text, p_replaces uuid,
                                         p_expires integer, p_every integer) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
DECLARE n integer;
BEGIN
  PERFORM salescall.hub_only();
  DELETE FROM salescall.pc_logins WHERE expires_at <= now();
  INSERT INTO salescall.pc_logins (device_sha, user_code, pc_label, replaces, expires_at, poll_every)
  VALUES (p_device_sha, p_user_code, p_label, p_replaces, now() + make_interval(secs => p_expires), p_every)
  ON CONFLICT DO NOTHING;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n = 1;
END $$;

-- ページに出す（まだ決めていない・期限の内の待ちだけ。前の PC の名前は、その契約者の止めていない PC のときだけ）
CREATE FUNCTION salescall.pc_login_waiting(p_user_code text)
RETURNS TABLE (pc_label text, replaces_label text, expires_at timestamptz)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
BEGIN
  IF salescall.cur_tenant() IS NULL OR NOT salescall.cur_is_person() THEN
    RAISE EXCEPTION 'PC のログインを見られるのは、ログインした管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN QUERY
    SELECT w.pc_label, a.label, w.expires_at FROM salescall.pc_logins w
    LEFT JOIN salescall.pc_agents a ON a.pc_id = w.replaces AND a.tenant_id = salescall.cur_tenant() AND a.revoked_at IS NULL
    WHERE w.user_code = p_user_code AND w.decision IS NULL AND w.expires_at > now();
END $$;

-- 許可する／断る（まだ決めていない・期限の内の待ちだけ。決めた PC の名前を返す。無ければ NULL）
CREATE FUNCTION salescall.pc_login_decide(p_user_code text, p_approve boolean) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
DECLARE got text;
BEGIN
  IF salescall.cur_tenant() IS NULL OR NOT salescall.cur_is_person() THEN
    RAISE EXCEPTION 'PC のログインを許可する・断るのは、ログインした管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege';
  END IF;
  UPDATE salescall.pc_logins SET decision = CASE WHEN p_approve THEN 'approved' ELSE 'denied' END,
         tenant_id = salescall.cur_tenant(), decided_by = salescall.cur_actor(), decided_at = now()
   WHERE user_code = p_user_code AND decision IS NULL AND expires_at > now()
  RETURNING pc_label INTO got;
  RETURN got;
END $$;

-- 受け取りを聞く（state：authorization_pending・slow_down・access_denied・expired_token・approved。approved なら許可した契約者と方）
CREATE FUNCTION salescall.pc_login_poll(p_device_sha text) RETURNS TABLE (state text, tenant_id uuid, decided_by text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
DECLARE w salescall.pc_logins;
BEGIN
  PERFORM salescall.hub_only();
  SELECT * INTO w FROM salescall.pc_logins x WHERE x.device_sha = p_device_sha FOR UPDATE;
  IF NOT FOUND OR w.expires_at <= now() THEN
    DELETE FROM salescall.pc_logins x WHERE x.device_sha = p_device_sha;
    state := 'expired_token';
  ELSIF w.decision = 'denied' THEN
    DELETE FROM salescall.pc_logins x WHERE x.device_sha = p_device_sha;
    state := 'access_denied';
  ELSIF w.decision = 'approved' THEN   -- 消すのは受け取り（pc_login_take）＝払い出しと同じ取引
    state := 'approved'; tenant_id := w.tenant_id; decided_by := w.decided_by;
  ELSIF w.polled_at IS NOT NULL AND now() < w.polled_at + make_interval(secs => w.poll_every) THEN
    UPDATE salescall.pc_logins x SET polled_at = now(), poll_every = x.poll_every + 5 WHERE x.device_sha = p_device_sha;
    state := 'slow_down';
  ELSE
    UPDATE salescall.pc_logins x SET polled_at = now() WHERE x.device_sha = p_device_sha;
    state := 'authorization_pending';
  END IF;
  RETURN NEXT;
END $$;

-- 受け取る（許可した契約者の取引で、システムの処理だけ。許可済み・期限の内の待ちを消して、PC の名前と入れ替える PC を返す。無ければ行なし）
CREATE FUNCTION salescall.pc_login_take(p_device_sha text) RETURNS TABLE (pc_label text, replaces uuid)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
BEGIN
  IF salescall.cur_tenant() IS NULL OR salescall.cur_actor_kind() IS DISTINCT FROM 'system' THEN
    RAISE EXCEPTION 'PC のログインの受け取りは、許可した契約者の取引のシステムの処理だけです' USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN QUERY
    DELETE FROM salescall.pc_logins x
     WHERE x.device_sha = p_device_sha AND x.decision = 'approved' AND x.tenant_id = salescall.cur_tenant() AND x.expires_at > now()
    RETURNING x.pc_label, x.replaces;
END $$;

REVOKE ALL ON FUNCTION salescall.pc_login_start(text, text, text, uuid, integer, integer), salescall.pc_login_waiting(text),
  salescall.pc_login_decide(text, boolean), salescall.pc_login_poll(text), salescall.pc_login_take(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.pc_login_start(text, text, text, uuid, integer, integer), salescall.pc_login_waiting(text),
  salescall.pc_login_decide(text, boolean), salescall.pc_login_poll(text), salescall.pc_login_take(text) TO salescall_app;
