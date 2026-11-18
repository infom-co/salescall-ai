-- 0091 時刻を指定された約束の前に空けるのは、約束の数だけの回線・空ける時間は業務の平均通話時間（DESIGN.md 5-2-2・15-2 の198。
-- 2026-10-19 ユーザー決定。書いた人：CL-2）
--
-- ユーザー（2026-10-19）：「初期値5分はOK、その後は、業務ごとに平均通話時間を測って、それを利用するといいでしょう。30分かかる
-- 業務と1分で終わる業務では、同じにできないからです」「1本は通常業務に使っていいでしょ！無駄なことするな。該当の回線を空けて
-- おくだけでいいじゃない」。
-- (1) 空ける時間：これから始めるふつうの電話の業務の、平均通話時間（お客様が出た通話の、予約から終わりまで。直近30日）。数が
--     少ないうち（設定 gate.exact_promise_guard_samples 本より少ない）は、設定 gate.exact_promise_guard（初め5分）。
--     ⇒ 測るのは salescall.business_call_seconds の1か所（門が読む）。
-- (2) 空ける回線：その約束をかけられる回線のうち、約束の数だけ（ほかに空いている回線が、近い約束の数より少ないときだけ、この回線を
--     空ける）。前（0090）は、どの回線も空けていた（無駄）。⇒ 決めるのは salescall.line_kept_for_promise の1か所。
--     2本の回線が同時に「ほかが空いている」と見てふつうの電話を始めても、門の取引は直列化の水準なので、片方がやり直して数え直す。

INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, finest_scope) VALUES
('gate.exact_promise_guard_samples', 'override', 'integer',
 '業務の平均通話時間を、時刻を指定した約束の前の空けに使い始める本数（お客様が出た通話の数。これより少ないうちは gate.exact_promise_guard）',
 'DESIGN.md 5-2-2', 'client');
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('gate.exact_promise_guard_samples', 'system', '10', 'provisional',
 '平均がぶれない程度の本数（仮）。業務の通話の数が増えたら見直す', '通話の長さのばらつきを見て', 'CL-2');

-- 業務の平均通話時間（秒）と、数えた本数：お客様が出た通話の、予約から終わりまで（回線がふさがる長さ）。直近 p_days 日。
CREATE FUNCTION salescall.business_call_seconds(p_business uuid, p_days integer)
RETURNS TABLE (seconds integer, calls integer)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT coalesce(round(avg(extract(epoch FROM a.ended_at - a.claimed_at)))::integer, 0), count(*)::integer
  FROM salescall.attempts a
  JOIN salescall.business_versions v ON v.version_id = a.version_id
  WHERE v.business_id = p_business AND a.tenant_id = salescall.cur_tenant()
    AND a.ended_at IS NOT NULL AND a.claimed_at > now() - make_interval(days => p_days)
    AND salescall.reached_answer(a.attempt_id)
$$;
REVOKE ALL ON FUNCTION salescall.business_call_seconds(uuid, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.business_call_seconds(uuid, integer) TO salescall_app;

-- この回線を、時刻を指定された約束のために空けておくか：p_until までに来る、この回線でかけられる約束の数が、ほかに空いている
-- （使わなくなっていない・借りられている・通話していない）回線のうち、その約束をかけられる回線の数より多いとき。
CREATE FUNCTION salescall.line_kept_for_promise(p_line uuid, p_until timestamptz, p_except uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  WITH soon AS (
    SELECT t.client_id
    FROM salescall.lines l
    JOIN salescall.tasks t ON t.tenant_id = l.tenant_id AND (l.client_id IS NULL OR t.client_id = l.client_id)
    JOIN LATERAL (SELECT n.kind, n.promised, n.callee_at FROM salescall.task_next_actions n WHERE n.task_id = t.task_id
                  ORDER BY n.seq DESC LIMIT 1) n ON true
    WHERE l.line_id = p_line AND l.tenant_id = salescall.cur_tenant()
      AND t.task_id <> p_except
      AND n.kind = 'call' AND n.promised AND n.callee_at IS NOT NULL AND n.callee_at > now() AND n.callee_at <= p_until
      AND salescall.client_active(t.client_id) AND NOT salescall.task_excluded_now(t.task_id)),
  free AS (
    SELECT o.line_id FROM salescall.lines o
    WHERE o.tenant_id = salescall.cur_tenant() AND o.line_id <> p_line AND o.retired_at IS NULL
      AND o.lease_holder IS NOT NULL AND o.lease_expires_at > now()
      AND EXISTS (SELECT 1 FROM soon s WHERE o.client_id IS NULL OR o.client_id = s.client_id)
      AND NOT EXISTS (SELECT 1 FROM salescall.attempts a WHERE a.line_id = o.line_id
                      AND salescall.is_live(a.state, a.claim_expires_at)))
  SELECT (SELECT count(*) FROM soon) > (SELECT count(*) FROM free)
$$;
REVOKE ALL ON FUNCTION salescall.line_kept_for_promise(uuid, timestamptz, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.line_kept_for_promise(uuid, timestamptz, uuid) TO salescall_app;

-- 0090 の関数は使わない（どの回線も空けていた）。読む所は無くなったので消す
DROP FUNCTION salescall.line_exact_promise_soon(uuid, timestamptz, uuid);
