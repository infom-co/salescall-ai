-- 0019 止まった理由を確かめて、人の判断で再開できるようにする（2026-10-15 ユーザー：「何で止まったのかは確認できるようにしてあるの？
-- 人の判断で再開できる仕組みは？」）。書いた人：CL-2
-- - 全体の範囲の停止を、人が止める・解くのは、システムの管理者だけ（原因を問わない。0018 は人の緊急停止だけだった）。
--   システムの処理（自動で止める・確かめで解く）は、いままでどおり。契約者の範囲は、その契約者の運営者。
-- - 何回続いたか（1か所）は、同じ原因・同じ範囲の停止が解かれた時刻からも数え直す。
--   ⚠️ いままでは、解いたあとも前のエラーを数えていたので、人が再開しても（確かめで解いても）、次のエラー1回でまた止まった。
DROP TRIGGER system_manual_stop_by_admin ON salescall.stop_events;
DROP FUNCTION salescall.system_manual_stop_by_admin();
CREATE FUNCTION salescall.system_scope_by_admin() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.scope = 'system' AND current_user <> 'salescall_owner'
     AND salescall.cur_actor_kind() IS DISTINCT FROM 'admin' AND salescall.cur_actor_kind() IS DISTINCT FROM 'system' THEN
    RAISE EXCEPTION '全体の範囲の停止を、人が止める・解くのは、システムの管理者だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'system_scope_by_admin';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER system_scope_by_admin BEFORE INSERT ON salescall.stop_events
  FOR EACH ROW EXECUTE FUNCTION salescall.system_scope_by_admin();

DROP FUNCTION salescall.consecutive_errors(text[], uuid);
-- 数だけを返す。p_line を渡すと、その回線のエラーを数える（その回線で相手を呼び出せた発信・その回線の同じ原因の停止が解かれた時刻
-- より後）。渡さなければ、全体のエラーを数える（どこかの通話が会話に入った時刻・全体の同じ原因の停止が解かれた時刻より後）。
CREATE FUNCTION salescall.consecutive_errors(p_kinds text[], p_cause text, p_line uuid DEFAULT NULL) RETURNS integer
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  WITH since AS (
    SELECT greatest(
      (SELECT coalesce(max(h.at), '-infinity'::timestamptz)
       FROM salescall.attempt_state_history h JOIN salescall.attempts a ON a.attempt_id = h.attempt_id
       WHERE CASE WHEN p_line IS NULL THEN h.to_state = 'in_conversation'
                  ELSE a.line_id = p_line AND h.to_state = 'ringing' END),
      (SELECT coalesce(max(r.at), '-infinity'::timestamptz)
       FROM salescall.stop_events r JOIN salescall.stop_events s ON s.event_id = r.resolves_event_id
       WHERE s.cause::text = p_cause
         AND CASE WHEN p_line IS NULL THEN s.scope = 'system' ELSE s.scope = 'line' AND s.line_id = p_line END)) AS t
  )
  SELECT count(*)::integer FROM salescall.error_events e, since
  WHERE e.kind = ANY(p_kinds) AND e.occurred_at > since.t AND (p_line IS NULL OR e.line_id = p_line)
$$;
REVOKE ALL ON FUNCTION salescall.consecutive_errors(text[], text, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.consecutive_errors(text[], text, uuid) TO salescall_app;

-- 止まった理由の説明（core/stops.py の explain）に出す、その停止に数えたエラー（止めた時刻までの、新しい順）。
-- 依頼主の範囲の外のエラーも数えて止めた（会話AI・回線はみんなで使う）ので、表の持ち主の権限で読み、エラーの文面と番号だけを返す
-- （相手の電話番号・発話は、もともとエラーの記録に入れない）。読めるのは、その停止が見える取引だけ：全体の停止＝全体の取引、
-- 回線の停止＝その回線の契約者の取引。
CREATE FUNCTION salescall.errors_for_stop(p_stop bigint, p_kinds text[], p_limit integer)
RETURNS TABLE (error_id bigint, source text, kind text, code integer, message text, occurred_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT e.error_id, e.source, e.kind, e.code, e.message, e.occurred_at
  FROM salescall.stop_events s JOIN salescall.error_events e
    ON e.kind = ANY(p_kinds) AND e.occurred_at <= s.at AND (s.scope <> 'line' OR e.line_id = s.line_id)
  WHERE s.event_id = p_stop AND s.action = 'stop'
    AND ((s.scope = 'system' AND salescall.cur_tenant() IS NULL)
         OR (s.scope = 'line' AND s.tenant_id = salescall.cur_tenant()))
  ORDER BY e.occurred_at DESC, e.error_id DESC LIMIT p_limit
$$;
REVOKE ALL ON FUNCTION salescall.errors_for_stop(bigint, text[], integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.errors_for_stop(bigint, text[], integer) TO salescall_app;

-- その通話で、言ってはいけないことを言ったか（通話後の検証で、2つの耳で確かめた数が1以上。判断は1か所）。
-- 2026-10-15 ユーザー：「その行はひとまず終える。依頼主でなく、管理者へ報告」⇒ 次の動き（終える）と、結果を自動で送らない、が読む。
CREATE FUNCTION salescall.said_forbidden(p_attempt uuid) RETURNS boolean
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT EXISTS (SELECT 1 FROM salescall.verification_summaries v WHERE v.attempt_id = p_attempt AND v.forbidden_speech > 0)
$$;
GRANT EXECUTE ON FUNCTION salescall.said_forbidden(uuid) TO salescall_app;
