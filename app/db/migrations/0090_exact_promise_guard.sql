-- 0090 時刻を指定された約束の前は、その回線でふつうの電話を始めない（DESIGN.md 5-2-2・15-2 の197。2026-10-19 ユーザー決定。
-- 書いた人：CL-2）
--
-- ユーザー（2026-10-19）：「約束の時刻に回線がほかの電話でふさがっていると、その電話が終わってから最優先でかけます。OK」
-- 「時刻ちょうどと約束した場合や、幅のない指定の場合は、約束の数分前からふつうの電話を始めない形がいいでしょう」。
-- ⇒ お客様が時刻を指定した約束（次の予定の callee_at。「明日の午後3時」「3時頃」「30分後」など、幅の無い指定）が、決めた時間の
--   うちに来るなら、その約束をかけられる回線は、ふつうの電話を始めない（門の順8。理由 line_promise_soon＝回線の理由なので、
--   回線が少し休み、行の予定は書き直さない）。約束の電話そのものは止めない。
--   幅のある約束（「夕方」「金曜なら」）は、幅の中のどこでかけても約束どおりなので、ここには入れない（優先枠で先にかける＝0089）。

INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, finest_scope) VALUES
('gate.exact_promise_guard', 'override', 'seconds',
 'お客様が時刻を指定した約束の、この時間前からは、その約束をかけられる回線でふつうの電話を始めない（約束の時刻に回線をふさがない）',
 'DESIGN.md 5-2-2', 'client');
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('gate.exact_promise_guard', 'system', '300', 'provisional',
 '2026-10-19 ユーザー「約束の数分前から」：5分（仮）。ふつうの電話の長さの実測で決め直す', '通話の長さを測ってから', 'CL-2');

-- その回線でかけてよい依頼者の行に、p_until までに来る、お客様が時刻を指定した約束があるか（いま門に来ている行を除く。
-- 数えるのはこの関数の1か所。「かけない」で止まる約束・無効の依頼者の約束は入れない）。
CREATE FUNCTION salescall.line_exact_promise_soon(p_line uuid, p_until timestamptz, p_except uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT EXISTS (
    SELECT 1
    FROM salescall.lines l
    JOIN salescall.tasks t ON t.tenant_id = l.tenant_id AND (l.client_id IS NULL OR t.client_id = l.client_id)
    JOIN LATERAL (SELECT n.kind, n.promised, n.callee_at FROM salescall.task_next_actions n WHERE n.task_id = t.task_id
                  ORDER BY n.seq DESC LIMIT 1) n ON true
    WHERE l.line_id = p_line AND l.tenant_id = salescall.cur_tenant()
      AND t.task_id <> p_except
      AND n.kind = 'call' AND n.promised AND n.callee_at IS NOT NULL AND n.callee_at > now() AND n.callee_at <= p_until
      AND salescall.client_active(t.client_id) AND NOT salescall.task_excluded_now(t.task_id))
$$;
REVOKE ALL ON FUNCTION salescall.line_exact_promise_soon(uuid, timestamptz, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.line_exact_promise_soon(uuid, timestamptz, uuid) TO salescall_app;
