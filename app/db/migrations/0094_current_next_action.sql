-- 0094 「行のいまの予定」と「その回線でかけてよい依頼者か」を、データベースの1か所にする（DESIGN.md 7-2・5-2-2・15-2 の202。
-- 2026-10-19 の見直し。書いた人：CL-2）
--
-- 見直しで見つけた一本化もれ：
-- (1) 行のいまの予定（次の動きの記録のいちばん新しいもの）を選ぶ書き方が、門・候補を出す処理・次の動き・締め・業務設定・
--     データベースの関数に、およそ10か所あった（ORDER BY seq DESC LIMIT 1 ／ max(seq) ／ 新しいものが無い）。
--     ⇒ 見方 current_next_actions の1か所（結果の版の latest_task_results と同じ形。読む人の権限で読む）。
-- (2) 「その回線でかけてよい依頼者か」（専用の回線はその依頼者だけ、共有の回線は契約者のすべての依頼者）が、門・候補を出す
--     処理・約束の取り置きに別々に書かれていた。⇒ salescall.line_serves の1か所。

CREATE VIEW salescall.current_next_actions WITH (security_invoker = true) AS
  SELECT DISTINCT ON (n.task_id) n.* FROM salescall.task_next_actions n ORDER BY n.task_id, n.seq DESC;
GRANT SELECT ON salescall.current_next_actions TO salescall_app;

-- 回線（割り当てた依頼者。共有の回線は空）で、その依頼者の行をかけてよいか
CREATE FUNCTION salescall.line_serves(p_line_client uuid, p_client uuid) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$ SELECT p_line_client IS NULL OR p_line_client = p_client $$;
GRANT EXECUTE ON FUNCTION salescall.line_serves(uuid, uuid) TO salescall_app;

CREATE OR REPLACE FUNCTION salescall.task_excluded_now(p_task uuid) RETURNS boolean LANGUAGE sql STABLE AS $$
  SELECT CASE salescall.task_exclusion_level(p_task)
           WHEN 'force' THEN true                                   -- 強制かけない：例外なし
           WHEN 'exclude' THEN coalesce((SELECT salescall.is_new_call(n.purpose, n.promised) FROM salescall.current_next_actions n
                                         WHERE n.task_id = p_task), true)
           ELSE false END
$$;

-- これからかける約束（0092 と同じ。いまの予定かは current_next_actions で見る。約束の予定だけの部分の索引から読む）
CREATE OR REPLACE FUNCTION salescall.promises_waiting()
RETURNS TABLE (task_id uuid, client_id uuid, call_at timestamptz, callee_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT n.task_id, n.client_id, n.call_at, n.callee_at
  FROM salescall.task_next_actions n
  JOIN salescall.tasks t ON t.task_id = n.task_id
  JOIN salescall.runs r ON r.run_id = t.run_id
  WHERE n.tenant_id = salescall.cur_tenant() AND n.promised AND n.kind = 'call'
    AND n.seq = (SELECT c.seq FROM salescall.current_next_actions c WHERE c.task_id = n.task_id)
    AND salescall.client_active(n.client_id) AND NOT salescall.task_excluded_now(n.task_id)
    AND NOT EXISTS (SELECT 1 FROM salescall.open_stops(n.tenant_id, n.client_id, r.business_id))
    AND NOT EXISTS (SELECT 1 FROM salescall.attempts a
                    WHERE a.task_id = n.task_id AND salescall.is_live(a.state, a.claim_expires_at))
$$;

CREATE OR REPLACE FUNCTION salescall.line_promised_due(p_line uuid, p_day date, p_except uuid) RETURNS integer
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  WITH me AS (SELECT l.client_id FROM salescall.lines l WHERE l.line_id = p_line AND l.tenant_id = salescall.cur_tenant()),
  serving AS (SELECT * FROM salescall.lines_serving() UNION SELECT p_line, (SELECT client_id FROM me), true WHERE EXISTS (SELECT 1 FROM me))
  SELECT coalesce(ceil(sum(1.0 / (SELECT count(DISTINCT s.line_id) FROM serving s
                                  WHERE salescall.line_serves(s.client_id, w.client_id)))), 0)::integer
  FROM salescall.promises_waiting() w, me
  WHERE w.task_id <> p_except AND salescall.jst(w.call_at)::date <= p_day AND salescall.line_serves(me.client_id, w.client_id)
$$;
