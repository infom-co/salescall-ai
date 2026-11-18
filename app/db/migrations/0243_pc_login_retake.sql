-- 0243 PC が通行証を受け取った答えが届かなかったときは、同じ待ちで払い出し直す（2026-11-18 の8回目の見直し。書いた人：CL-2。DESIGN.md 5-5・15-2 の872）
--
-- ⛔ 0241：受け取り（pc_login_take）で待ちを消したので、払い出した答えが PC に届かない（通信の切れ・時間切れ）と、PC は「時間切れ」を受けて新しいログインを
--    始め、払い出した PC の記録は、だれも持たないまま止めずに残った（動いている合図が一度も来ないので、契約者と管理者に見覚えの無い「PC が止まった」が届き、
--    PC の一覧にも残った）。
-- ⇒ 受け取っても待ちは期限まで残し、払い出した PC を待ちに書く（issued_pc）。同じ待ちの印でまた聞かれたら（答えが届かなかった）、前に払い出した PC を止めてから
--    払い出し直す（生きている通行証は、いつも1つ）。期限の切れた待ちは、今までどおり始めるときに消す・聞かれたら「時間切れ」。

ALTER TABLE salescall.pc_logins ADD COLUMN issued_pc uuid;

-- 受け取る（許可した契約者の取引で、システムの処理だけ。許可済み・期限の内の待ちを、払い出し直しまで押さえ、PC の名前・入れ替える PC・前に払い出した PC を返す）
DROP FUNCTION salescall.pc_login_take(text);
CREATE FUNCTION salescall.pc_login_take(p_device_sha text) RETURNS TABLE (pc_label text, replaces uuid, issued_pc uuid)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
BEGIN
  IF salescall.cur_tenant() IS NULL OR salescall.cur_actor_kind() IS DISTINCT FROM 'system' THEN
    RAISE EXCEPTION 'PC のログインの受け取りは、許可した契約者の取引のシステムの処理だけです' USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN QUERY
    SELECT x.pc_label, x.replaces, x.issued_pc FROM salescall.pc_logins x
     WHERE x.device_sha = p_device_sha AND x.decision = 'approved' AND x.tenant_id = salescall.cur_tenant() AND x.expires_at > now()
    FOR UPDATE;
END $$;

-- 払い出した PC を待ちに書く（受け取りと同じ取引で、システムの処理だけ）
CREATE FUNCTION salescall.pc_login_issued(p_device_sha text, p_pc uuid) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
BEGIN
  IF salescall.cur_tenant() IS NULL OR salescall.cur_actor_kind() IS DISTINCT FROM 'system' THEN
    RAISE EXCEPTION 'PC のログインの受け取りは、許可した契約者の取引のシステムの処理だけです' USING ERRCODE = 'insufficient_privilege';
  END IF;
  UPDATE salescall.pc_logins x SET issued_pc = p_pc
   WHERE x.device_sha = p_device_sha AND x.decision = 'approved' AND x.tenant_id = salescall.cur_tenant();
END $$;

REVOKE ALL ON FUNCTION salescall.pc_login_take(text), salescall.pc_login_issued(text, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.pc_login_take(text), salescall.pc_login_issued(text, uuid) TO salescall_app;
