-- 0096 見直し4（2026-10-19 深夜。ユーザー：「他にも…食い違いや、不備、修正漏れ、負の遺産の消し忘れ、重複や一本化もれなどは
-- 起こっていないですか？」）で見つけた不備を直す（DESIGN.md 15-2 の204。書いた人：CL-2）
--
-- (1) 行のいまの予定の見方（0094）を DISTINCT ON で作っていたので、候補を出す処理が行ごとに読む形（LATERAL）では条件が中まで
--     届かず、依頼者の予定の記録を毎回すべて並べ替えていた ⇒ 「その行の最大の番号の記録」の形にする（行ごとに索引で読める）。
-- (2) 回線の1日の上限の取り置き（0094）は、約束を回線の数で割って回線ごとに切り上げたので、約束1つを3本の回線が1本ずつ取り置いた
--     （合計3本）。回線の一覧も約束ごとに作り直していた ⇒ 約束を、それをかけられる回線に1つずつ順に割り当て（約束の番号順に
--     回線を回す）、この回線に割り当たった数を取り置く。合計はちょうど約束の数。
-- (3) 約束の前の空け（0092）も、専用の回線がその依頼者をかけられるかを line_serves の1か所で見る。
-- (4) 設定の意味：callback.exact_slot（0093）が、外した回数の設定（0095）のことを書いていた。gate.exact_promise_guard（0090）の
--     初め5分はユーザーが「OK」と決めたのに、仮のままだった。

CREATE OR REPLACE VIEW salescall.current_next_actions WITH (security_invoker = true) AS
  SELECT n.* FROM salescall.task_next_actions n
  WHERE n.seq = (SELECT max(m.seq) FROM salescall.task_next_actions m WHERE m.task_id = n.task_id);

CREATE OR REPLACE FUNCTION salescall.line_promised_due(p_line uuid, p_day date, p_except uuid) RETURNS integer
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  WITH me AS (SELECT l.client_id FROM salescall.lines l WHERE l.line_id = p_line AND l.tenant_id = salescall.cur_tenant()),
  serving AS (SELECT s.line_id, s.client_id FROM salescall.lines_serving() s
              UNION SELECT p_line, me.client_id FROM me),
  due AS (SELECT w.task_id, w.client_id, dense_rank() OVER (ORDER BY w.task_id) - 1 AS r
          FROM salescall.promises_waiting() w
          WHERE w.task_id <> p_except AND salescall.jst(w.call_at)::date <= p_day),
  cand AS (SELECT d.task_id, d.r, s.line_id,
                  row_number() OVER (PARTITION BY d.task_id ORDER BY s.line_id) - 1 AS k,
                  count(*) OVER (PARTITION BY d.task_id) AS n
           FROM due d JOIN serving s ON salescall.line_serves(s.client_id, d.client_id))
  SELECT count(*)::integer FROM cand WHERE line_id = p_line AND k = r % n
$$;

CREATE OR REPLACE FUNCTION salescall.line_kept_for_promise(p_line uuid, p_until timestamptz, p_except uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  WITH me AS (SELECT l.client_id FROM salescall.lines l WHERE l.line_id = p_line AND l.tenant_id = salescall.cur_tenant()),
  soon AS (SELECT w.client_id, count(*)::integer AS p FROM salescall.promises_waiting() w
           WHERE w.task_id <> p_except AND w.callee_at > now() AND w.callee_at <= p_until GROUP BY w.client_id),
  idle AS (SELECT s.client_id FROM salescall.lines_serving() s WHERE s.idle AND s.line_id <> p_line),
  per AS (SELECT soon.p,
                 (SELECT count(*) FROM idle WHERE idle.client_id IS NOT NULL
                                                AND salescall.line_serves(idle.client_id, soon.client_id))::integer AS d,
                 (me.client_id IS NOT NULL AND salescall.line_serves(me.client_id, soon.client_id))::integer AS mine
          FROM soon, me),
  shared AS (SELECT count(*)::integer AS s FROM idle WHERE idle.client_id IS NULL)
  SELECT coalesce((SELECT sum(least(p, d + mine)) + least(sum(greatest(p - d - mine, 0)),
                                                          (SELECT s FROM shared) + (SELECT (client_id IS NULL)::integer FROM me))
                   FROM per), 0)
       > coalesce((SELECT sum(least(p, d)) + least(sum(greatest(p - d, 0)), (SELECT s FROM shared)) FROM per), 0)
$$;

ALTER TABLE salescall.setting_definitions DISABLE TRIGGER setting_definitions_frozen;
UPDATE salescall.setting_definitions
SET meaning = 'お客様が時刻ちょうどで約束したかけ直し（「明日の15時」）に出なかったとき、その時刻からかけ直す時間の長さ（この中で、'
              'callback.exact_retry_gaps の間をあけてかけ直す）'
WHERE key = 'callback.exact_slot';
ALTER TABLE salescall.setting_definitions ENABLE TRIGGER setting_definitions_frozen;
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('gate.exact_promise_guard', 'system', '300', 'decided',
 '2026-10-19 ユーザー：「初期値5分はOK、その後は、業務ごとに平均通話時間を測って、それを利用する」（測れたら平均を使う＝0091）',
 '運用で見直すとき', 'CL-2');
