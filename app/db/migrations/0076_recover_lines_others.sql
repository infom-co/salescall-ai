-- 0076 落ちた試行の後始末を直す（DESIGN.md 5-4・15-2 の172。2026-10-18 の見直し。書いた人：CL-2）
--
-- 見直しで見つかった2つの穴（0063 の recover_lines）：
-- (1) データベースに60秒を超えてつながらないと、自分の回線の借用の期限が切れる。つながり直したとき、決まった間隔の後始末
--     （借用の期限が切れた回線）が延長より先に走ると、自分の回線ごとの処理がまだ話している通話を結果不明にした（締めが、話している
--     最中のお客様へのお詫びを予定し、番号ごとの「進行中は1本」の守りも外れた）。
--     ⇒ 決まった間隔の後始末は、呼んだ見守り役が自分で借りている回線を除く（自分の分は、その回線の処理を立ち上げる前の後始末
--        だけがする＝その回線の処理が無いと確かめたあと）。
-- (2) 後始末は契約者ごとの仕事なので、その契約者の PC が落ちたまま戻らないと、話している途中だった試行が残り続け、その番号には
--     すべての契約者がかけられなかった。
--     ⇒ 管理者の PC が、すべての契約者に共通の仕事として、期限が長く切れたままの回線（その契約者のほかの PC も後始末していない）を
--        後始末する。どの契約者に、そういう回線があるかは `salescall.tenants_with_stale_lines` が返す（全体の取引だけ。
--        返すのは契約者の番号だけ）。

DROP FUNCTION salescall.recover_lines(text, uuid);
-- p_holder を渡したら「その見守り役が借りている回線」（起動したとき＝前に落ちた自分の分。p_line を渡せば、その回線だけ＝回線の処理が
-- 落ちたとき）、NULL なら「借用の期限が p_expired_for より前に切れた回線」（決まった間隔で＝ほかの PC が落ちた分。p_except＝呼んだ
-- 見守り役の名前。自分が借りている回線は除く）
CREATE FUNCTION salescall.recover_lines(p_holder text, p_line uuid DEFAULT NULL, p_except text DEFAULT NULL,
                                        p_expired_for interval DEFAULT '0 seconds')
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
                       THEN x.lease_expires_at < now() - p_expired_for AND x.lease_holder IS DISTINCT FROM p_except
                       ELSE x.lease_holder = p_holder END)
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

CREATE FUNCTION salescall.tenants_with_stale_lines(p_expired_for interval) RETURNS SETOF uuid
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
BEGIN
  IF salescall.cur_tenant() IS NOT NULL OR salescall.cur_actor_kind() IS DISTINCT FROM 'system' THEN
    RAISE EXCEPTION 'すべての契約者に共通の仕事（全体の取引のシステムの処理）だけが呼びます' USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN QUERY SELECT DISTINCT x.tenant_id FROM salescall.lines x
               WHERE x.lease_holder IS NOT NULL AND x.lease_expires_at < now() - p_expired_for ORDER BY 1;
END $$;

REVOKE ALL ON FUNCTION salescall.recover_lines(text, uuid, text, interval), salescall.tenants_with_stale_lines(interval)
  FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.recover_lines(text, uuid, text, interval), salescall.tenants_with_stale_lines(interval)
  TO salescall_app;
