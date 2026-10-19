-- 0097 見直し5（2026-10-19。ユーザー：「他にも…食い違いや、不備、修正漏れ、負の遺産の消し忘れ、重複や一本化もれなどは
-- 起こっていないですか？」）で見つけた一本化もれを直す（DESIGN.md 15-2 の205。書いた人：CL-2）
--
-- (1) 「その通話のいちばん新しい判定」（判定し直しがあれば、その版）を選ぶ書き方が、次の動き（再発信の方針の鍵の回数・約束を
--     した通話）・締め（判定し直し）・誤判定率（正解を付ける相手）に、別々にあった ⇒ 見方 call_judgements の1か所。
-- (2) 行のいちばん新しい結果の見方 latest_task_results（0029）が、0094 の current_next_actions と同じ DISTINCT ON の形で、
--     ほかの表と結ぶと条件が中まで届かず、すべての結果を並べ替えていた ⇒ 0096 と同じ「その行の最大の番号」の形にする。

CREATE INDEX task_results_judgements ON salescall.task_results (attempt_id, version_no) WHERE kind IN ('judged', 'rejudged');

CREATE VIEW salescall.call_judgements WITH (security_invoker = true) AS
  SELECT r.* FROM salescall.task_results r
  WHERE r.kind IN ('judged', 'rejudged')
    AND r.version_no = (SELECT max(m.version_no) FROM salescall.task_results m
                        WHERE m.attempt_id = r.attempt_id AND m.kind IN ('judged', 'rejudged'));
GRANT SELECT ON salescall.call_judgements TO salescall_app;

CREATE OR REPLACE VIEW salescall.latest_task_results WITH (security_invoker = true) AS
  SELECT r.* FROM salescall.task_results r
  WHERE r.version_no = (SELECT max(m.version_no) FROM salescall.task_results m WHERE m.task_id = r.task_id);

-- (3) 「発信まで進んだ試行か」（予約のまま・かけずに取り消した予約は入れない）の条件が、Python（attempts.dialed）とデータベースの
--     関数7つに、別々に書いてあった ⇒ salescall.is_dialed の1か所（進行中の salescall.is_active と並べる）。Python も、これを呼ぶ。
CREATE FUNCTION salescall.is_dialed(s salescall.attempt_state) RETURNS boolean LANGUAGE sql IMMUTABLE AS
$$ SELECT s NOT IN ('claimed', 'released') $$;
GRANT EXECUTE ON FUNCTION salescall.is_dialed(salescall.attempt_state) TO salescall_app;

CREATE OR REPLACE FUNCTION salescall.task_awaiting_decision(p_task uuid)
 RETURNS boolean
 LANGUAGE sql
 STABLE
AS $function$
  SELECT EXISTS (SELECT 1 FROM salescall.attempts a WHERE a.task_id = p_task AND salescall.is_dialed(a.state)
                 AND NOT salescall.is_settled(a.attempt_id))
$function$;
CREATE OR REPLACE FUNCTION salescall.line_dials_on(p_line uuid, p_day date)
 RETURNS integer
 LANGUAGE sql
 STABLE SECURITY DEFINER
 SET search_path TO 'salescall', 'pg_temp'
AS $function$
  SELECT count(*)::integer FROM salescall.attempts a
  WHERE a.line_id = p_line AND a.tenant_id = salescall.cur_tenant()
    AND salescall.is_dialed(a.state)
    AND salescall.jst(a.claimed_at)::date = p_day
$function$;
CREATE OR REPLACE FUNCTION salescall.number_last_dial_elsewhere(p_phone text)
 RETURNS TABLE(relation text, last_at timestamp with time zone)
 LANGUAGE sql
 STABLE SECURITY DEFINER
 SET search_path TO 'salescall', 'pg_temp'
AS $function$
  SELECT CASE WHEN a.tenant_id = salescall.cur_tenant() THEN 'same_tenant' ELSE 'other_tenant' END, max(a.claimed_at)
  FROM salescall.attempts a
  WHERE a.phone_e164 = p_phone AND a.client_id IS DISTINCT FROM salescall.cur_client()
    AND salescall.is_dialed(a.state)
  GROUP BY 1
$function$;
CREATE OR REPLACE FUNCTION salescall.number_dialed_by_other_tenant(p_phone text)
 RETURNS boolean
 LANGUAGE sql
 STABLE SECURITY DEFINER
 SET search_path TO 'salescall', 'pg_temp'
AS $function$
  SELECT EXISTS (SELECT 1 FROM salescall.attempts a WHERE a.phone_e164 = p_phone AND a.tenant_id <> salescall.cur_tenant()
                 AND salescall.is_dialed(a.state))
$function$;
CREATE OR REPLACE FUNCTION salescall.number_last_voice_elsewhere(p_phone text)
 RETURNS text
 LANGUAGE sql
 STABLE SECURITY DEFINER
 SET search_path TO 'salescall', 'pg_temp'
AS $function$
  SELECT v.voice_name FROM salescall.attempts a JOIN salescall.voice_types v ON v.code = a.voice_type
  WHERE a.phone_e164 = p_phone AND a.client_id IS DISTINCT FROM salescall.cur_client()
    AND salescall.is_dialed(a.state) AND salescall.reached_answer(a.attempt_id)
  ORDER BY a.claimed_at DESC LIMIT 1
$function$;
CREATE OR REPLACE FUNCTION salescall.line_called_number_elsewhere(p_line uuid, p_phone text)
 RETURNS boolean
 LANGUAGE sql
 STABLE SECURITY DEFINER
 SET search_path TO 'salescall', 'pg_temp'
AS $function$
  SELECT EXISTS (SELECT 1 FROM salescall.attempts a
                 WHERE a.line_id = p_line AND a.tenant_id = salescall.cur_tenant() AND a.phone_e164 = p_phone
                   AND a.client_id IS DISTINCT FROM salescall.cur_client() AND salescall.is_dialed(a.state)
                   AND EXISTS (SELECT 1 FROM salescall.attempt_state_history h
                               WHERE h.attempt_id = a.attempt_id AND h.to_state = 'ringing'))
$function$;
CREATE OR REPLACE FUNCTION salescall.work_seconds(p_from timestamp with time zone, p_to timestamp with time zone)
 RETURNS TABLE(at timestamp with time zone, seconds numeric)
 LANGUAGE sql
 STABLE SECURITY DEFINER
 SET search_path TO 'salescall', 'pg_temp'
AS $function$
  SELECT a.claimed_at,
         extract(epoch FROM a.ended_at - greatest(a.claimed_at, p_from))
         + CASE WHEN a.purpose = 'resume' THEN 0
                ELSE coalesce((SELECT t.hourly_gap_seconds FROM salescall.reward_terms t
                               WHERE t.client_id = a.client_id AND t.valid_from <= a.claimed_at
                               ORDER BY t.valid_from DESC LIMIT 1), 0) END
  FROM salescall.attempts a
  WHERE a.client_id = salescall.cur_client() AND salescall.cur_client() IS NOT NULL
    AND a.tenant_id = salescall.cur_tenant()
    AND salescall.is_dialed(a.state) AND a.ended_at IS NOT NULL AND a.purpose <> 'apology'
    AND a.ended_at > p_from AND a.claimed_at <= p_to
$function$;

-- (4) 回線の1日の上限の取り置き（0096）は、約束を、それをかけられる回線（専用と共有）に区別なく順に割り当てたので、依頼者Aの
--     約束が共有の回線に寄ると、共有の回線がほかの依頼者のふつうの電話を控え、Aの専用の回線は上限まで使えた
--     （2026-10-19 ユーザー：「1本は通常業務に使っていいでしょ！無駄なことするな」）⇒ 約束は、その依頼者の専用の回線に先に
--     割り当てる（専用の回線どうしは順に）。専用の回線が無い依頼者の約束だけを、共有の回線に順に割り当てる。合計は約束の数ちょうど。
CREATE OR REPLACE FUNCTION salescall.line_promised_due(p_line uuid, p_day date, p_except uuid) RETURNS integer
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  WITH me AS (SELECT l.client_id FROM salescall.lines l WHERE l.line_id = p_line AND l.tenant_id = salescall.cur_tenant()),
  serving AS (SELECT s.line_id, s.client_id FROM salescall.lines_serving() s
              UNION SELECT p_line, me.client_id FROM me),
  due AS (SELECT w.task_id, w.client_id, dense_rank() OVER (ORDER BY w.task_id) - 1 AS r
          FROM salescall.promises_waiting() w
          WHERE w.task_id <> p_except AND salescall.jst(w.call_at)::date <= p_day),
  cand AS (SELECT d.task_id, d.r, s.line_id, s.client_id IS NOT NULL AS own,
                  count(*) FILTER (WHERE s.client_id IS NOT NULL) OVER (PARTITION BY d.task_id) AS owns
           FROM due d JOIN serving s ON salescall.line_serves(s.client_id, d.client_id)),
  pool AS (SELECT c.task_id, c.r, c.line_id,
                  row_number() OVER (PARTITION BY c.task_id ORDER BY c.line_id) - 1 AS k,
                  count(*) OVER (PARTITION BY c.task_id) AS n
           FROM cand c WHERE c.own OR c.owns = 0)
  SELECT count(*)::integer FROM pool WHERE line_id = p_line AND k = r % n
$$;
