-- 0063 見守り役（PC で常に動き、システムの処理をまとめて動かす役。DESIGN.md 5-4・15-2 の160。書いた人：CL-2）
--
-- Intent.md 14-1：「本番のPCで、ログインのときに起動する見守り役が、システムの処理をまとめて動かす」。
-- (1) 回線の借用：見守り役が回線を借りて（期限つき）、動いている間、決まった間隔で延ばす。門は、借りている見守り役の予約だけを
--     通す（5-2 の8。前からある）。借りる・延ばす・返すのは、この関数だけ（ほかの見守り役が生きて借りている回線は借りられない）。
-- (2) 落ちた試行の後始末（DESIGN.md 4-2「落ちた試行の見つけ方」）：借用の期限が切れた回線（PCが落ちた・ネットが切れた）と、
--     起動した見守り役が前に借りていた回線の、進行中の試行を終わらせる。予約のまま＝取り消し（released）、発信してから＝結果不明
--     （technical_error）。ほかの見守り役が生きて借りている回線の通話は、終わらせない。
-- (3) 仕事の借用：決まった間隔の仕事（締め・控え など）を、同じ仕事を2つの見守り役が同時にしないよう、回線と同じ「期限つきの借用」
--     で1つに決める（PC が2台あっても1つだけが動く。落ちれば期限で次が引き継ぐ）。
-- (4) エラーの出どころに「見守り役」（supervisor）を足す（見守り役の処理の誤り＝根源的。止めずに CL-2 へ修正依頼）。
-- (5) 「前の発信の締めが済んでいない行」の決まりを1か所にする（門が中に書いていたものを、候補を出す処理も同じものを読む）。

-- ═════════ (1) 回線の借用 ═════════
CREATE FUNCTION salescall.lease_line(p_line uuid, p_holder text, p_seconds integer) RETURNS timestamptz
LANGUAGE plpgsql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
DECLARE until timestamptz;
BEGIN
  IF p_holder IS NULL OR btrim(p_holder) = '' OR p_seconds IS NULL OR p_seconds < 1 THEN
    RAISE EXCEPTION '借りる見守り役の名前と、期限（秒）が要ります' USING ERRCODE = 'check_violation';
  END IF;
  UPDATE salescall.lines SET lease_holder = p_holder, lease_expires_at = now() + make_interval(secs => p_seconds)
  WHERE line_id = p_line AND tenant_id = salescall.cur_tenant() AND retired_at IS NULL
    AND (lease_holder IS NULL OR lease_holder = p_holder OR lease_expires_at < now())
  RETURNING lease_expires_at INTO until;
  RETURN until;   -- 借りられなければ NULL（ほかの見守り役が生きて借りている・使わなくなった回線・ほかの契約者の回線）
END $$;
CREATE FUNCTION salescall.return_line(p_line uuid, p_holder text) RETURNS boolean
LANGUAGE sql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  UPDATE salescall.lines SET lease_holder = NULL, lease_expires_at = NULL
  WHERE line_id = p_line AND tenant_id = salescall.cur_tenant() AND lease_holder = p_holder
  RETURNING true
$$;

-- ═════════ (2) 落ちた試行の後始末 ═════════
-- p_holder を渡したら「その見守り役が借りている回線」（起動したとき＝前に落ちた自分の分。p_line を渡せば、その回線だけ＝回線の処理が
-- 落ちたとき）、NULL なら「借用の期限が切れた回線」（決まった間隔で＝ほかの PC が落ちた分）
CREATE FUNCTION salescall.recover_lines(p_holder text, p_line uuid DEFAULT NULL)
RETURNS TABLE (line_id uuid, released integer, ended integer)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
DECLARE l record; r integer; e integer;
BEGIN
  IF salescall.cur_tenant() IS NULL THEN
    RAISE EXCEPTION '契約者の取引で呼びます' USING ERRCODE = 'insufficient_privilege';
  END IF;
  FOR l IN SELECT x.line_id FROM salescall.lines x
           WHERE x.tenant_id = salescall.cur_tenant() AND x.lease_holder IS NOT NULL
             AND (CASE WHEN p_holder IS NULL THEN x.lease_expires_at < now() ELSE x.lease_holder = p_holder END)
             AND (p_line IS NULL OR x.line_id = p_line)
           FOR UPDATE LOOP
    UPDATE salescall.attempts SET state = 'released' WHERE attempts.line_id = l.line_id AND state = 'claimed';
    GET DIAGNOSTICS r = ROW_COUNT;
    UPDATE salescall.attempts SET state = 'technical_error' WHERE attempts.line_id = l.line_id AND salescall.is_active(state);
    GET DIAGNOSTICS e = ROW_COUNT;
    IF p_line IS NULL THEN   -- 回線の処理だけが落ちたとき（p_line）は、借用を続けたまま立ち上げ直す
      UPDATE salescall.lines SET lease_holder = NULL, lease_expires_at = NULL WHERE lines.line_id = l.line_id;
    END IF;
    line_id := l.line_id; released := r; ended := e;
    RETURN NEXT;
  END LOOP;
END $$;

-- ═════════ (3) 仕事の借用 ═════════
CREATE TABLE salescall.job_leases (
  job_key    text PRIMARY KEY CHECK (btrim(job_key) <> ''),   -- 仕事の名前（契約者ごとの仕事は「名前:契約者」）
  holder     text NOT NULL CHECK (btrim(holder) <> ''),
  expires_at timestamptz NOT NULL
);
REVOKE ALL ON salescall.job_leases FROM salescall_app;
CREATE FUNCTION salescall.take_job(p_key text, p_holder text, p_seconds integer) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
DECLARE got text;
BEGIN
  IF salescall.cur_actor_kind() IS DISTINCT FROM 'system' THEN
    RAISE EXCEPTION '見守り役（システムの処理）だけが呼べます' USING ERRCODE = 'insufficient_privilege';
  END IF;
  INSERT INTO salescall.job_leases AS j (job_key, holder, expires_at)
  VALUES (p_key, p_holder, now() + make_interval(secs => p_seconds))
  ON CONFLICT (job_key) DO UPDATE SET holder = EXCLUDED.holder, expires_at = EXCLUDED.expires_at
    WHERE j.expires_at < now() OR j.holder = EXCLUDED.holder
  RETURNING holder INTO got;
  RETURN got IS NOT NULL;
END $$;
CREATE FUNCTION salescall.return_job(p_key text, p_holder text) RETURNS boolean
LANGUAGE sql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  UPDATE salescall.job_leases SET expires_at = now() WHERE job_key = p_key AND holder = p_holder RETURNING true
$$;

REVOKE ALL ON FUNCTION salescall.lease_line(uuid, text, integer), salescall.return_line(uuid, text),
  salescall.recover_lines(text, uuid), salescall.take_job(text, text, integer), salescall.return_job(text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.lease_line(uuid, text, integer), salescall.return_line(uuid, text),
  salescall.recover_lines(text, uuid), salescall.take_job(text, text, integer), salescall.return_job(text, text) TO salescall_app;

-- ═════════ (5) 前の発信の締めが済んでいない行（門と、候補を出す処理が同じ決まりを読む＝1か所） ═════════
CREATE FUNCTION salescall.task_awaiting_decision(p_task uuid) RETURNS boolean LANGUAGE sql STABLE AS $$
  SELECT EXISTS (SELECT 1 FROM salescall.attempts a WHERE a.task_id = p_task AND a.state NOT IN ('claimed', 'released')
                 AND NOT salescall.is_settled(a.attempt_id))
$$;
GRANT EXECUTE ON FUNCTION salescall.task_awaiting_decision(uuid) TO salescall_app;

-- ═════════ (4) エラーの出どころ ═════════
ALTER TABLE salescall.error_events DROP CONSTRAINT error_events_source_check;
ALTER TABLE salescall.error_events ADD CONSTRAINT error_events_source_check
  CHECK (source IN ('ai', 'ear', 'line', 'storage', 'delivery', 'recording', 'supervisor'));
