-- 0092 約束の電話のための回線の取り置きと空けを、契約者の回線全体で正しく数える（DESIGN.md 5-2-2・15-2 の199。
-- 2026-10-19 の見直し。書いた人：CL-2）
--
-- 0089・0091 の数え方の不足：
-- (1) 回線の1日の上限の取り置き（line_promised_due）は、どの回線も同じ約束の分をまるごと取り置いていた（回線が3本なら3倍）。
--     止まっている依頼者・業務の約束、いま通話中の約束も数えていた。
-- (2) 約束の前の空け（line_kept_for_promise）は、専用の回線と共有の回線が混ざると足りなかった（例：依頼者Aの専用の回線と
--     共有の回線が1本ずつで、AとBの約束が1つずつ。専用の回線から見ると「共有の回線が空いている」ので空けず、Bの約束をかける
--     回線が無くなった）。
-- (3) 平均通話時間に、途中の誤りで終わった通話（technical_error。復旧で終わりにすると何時間にもなる）を入れていた。
--
-- ⇒ (a) これからかける約束（いまの予定が約束したかけ直しで、かけない・止まっている・通話中でないもの）を読むのは
--       salescall.promises_waiting の1か所。取り置きと空けの両方が読む（約束の予定だけの部分の索引で読む）。
--   (b) 取り置き：約束1つを、それをかけられる回線（使わなくなっていない・借りられている・止まっていない）で等しく分け、この回線の
--       分を足して切り上げる（専用の回線はその依頼者、共有の回線は契約者のすべての依頼者をかけられる）。
--   (c) 空け：近い約束と空いている回線の割り当ての数（専用の回線は自分の依頼者の約束、共有の回線は残りの約束）を、この回線を
--       入れたときと入れないときで比べ、入れると増えるときだけ、この回線を空ける。
--   (d) 平均通話時間は、ふつうに終えた通話（ended）とお客様が切った通話（dropped）だけ。

CREATE INDEX task_next_actions_promised ON salescall.task_next_actions (tenant_id) WHERE promised AND kind = 'call';

-- これからかける約束（契約者の分。いまの予定が約束したかけ直しで、依頼者が有効・「かけない」で止まらない・依頼者と業務が止まって
-- いない・いま通話中でない）。call_at＝かけてよくなる時刻、callee_at＝お客様が指定した時刻（幅の約束は空）。
CREATE FUNCTION salescall.promises_waiting()
RETURNS TABLE (task_id uuid, client_id uuid, call_at timestamptz, callee_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT n.task_id, n.client_id, n.call_at, n.callee_at
  FROM salescall.task_next_actions n
  JOIN salescall.tasks t ON t.task_id = n.task_id
  JOIN salescall.runs r ON r.run_id = t.run_id
  WHERE n.tenant_id = salescall.cur_tenant() AND n.promised AND n.kind = 'call'
    AND NOT EXISTS (SELECT 1 FROM salescall.task_next_actions m WHERE m.task_id = n.task_id AND m.seq > n.seq)
    AND salescall.client_active(n.client_id) AND NOT salescall.task_excluded_now(n.task_id)
    AND NOT EXISTS (SELECT 1 FROM salescall.open_stops(n.tenant_id, n.client_id, r.business_id))
    AND NOT EXISTS (SELECT 1 FROM salescall.attempts a
                    WHERE a.task_id = n.task_id AND salescall.is_live(a.state, a.claim_expires_at))
$$;
REVOKE ALL ON FUNCTION salescall.promises_waiting() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.promises_waiting() TO salescall_app;

-- かけられる回線（使わなくなっていない・借りられている・回線が止まっていない。契約者の分）。client_id＝専用の依頼者（共有は空）、
-- idle＝いま通話していない。
CREATE FUNCTION salescall.lines_serving()
RETURNS TABLE (line_id uuid, client_id uuid, idle boolean)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT o.line_id, o.client_id,
         NOT EXISTS (SELECT 1 FROM salescall.attempts a WHERE a.line_id = o.line_id AND salescall.is_live(a.state, a.claim_expires_at))
  FROM salescall.lines o
  WHERE o.tenant_id = salescall.cur_tenant() AND o.retired_at IS NULL AND o.lease_expires_at > now()
    AND NOT EXISTS (SELECT 1 FROM salescall.open_stop_events s WHERE s.scope = 'line' AND s.line_id = o.line_id)
$$;
REVOKE ALL ON FUNCTION salescall.lines_serving() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.lines_serving() TO salescall_app;

-- 今日、この回線が取り置く約束の電話の数（予定の時刻が今日までの約束を、それをかけられる回線で等しく分けた、この回線の分。
-- 切り上げ）。p_except＝いま門に来ている行（自分の分は取り置かない）。
CREATE OR REPLACE FUNCTION salescall.line_promised_due(p_line uuid, p_day date, p_except uuid) RETURNS integer
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  WITH me AS (SELECT l.client_id FROM salescall.lines l WHERE l.line_id = p_line AND l.tenant_id = salescall.cur_tenant()),
  serving AS (SELECT * FROM salescall.lines_serving() UNION SELECT p_line, (SELECT client_id FROM me), true WHERE EXISTS (SELECT 1 FROM me))
  SELECT coalesce(ceil(sum(1.0 / (SELECT count(DISTINCT s.line_id) FROM serving s
                                  WHERE s.client_id IS NULL OR s.client_id = w.client_id))), 0)::integer
  FROM salescall.promises_waiting() w, me
  WHERE w.task_id <> p_except AND salescall.jst(w.call_at)::date <= p_day
    AND (me.client_id IS NULL OR me.client_id = w.client_id)
$$;

-- この回線を、時刻を指定された約束のために空けておくか：p_until までに来る約束と、空いている回線（専用の回線は自分の依頼者の
-- 約束、共有の回線は残りの約束）の割り当ての数が、この回線を入れると増えるとき。
-- 割り当ての数＝依頼者ごとの min(約束, 専用の回線) の和 ＋ min(専用で足りない約束の和, 共有の回線)。
CREATE OR REPLACE FUNCTION salescall.line_kept_for_promise(p_line uuid, p_until timestamptz, p_except uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  WITH me AS (SELECT l.client_id FROM salescall.lines l WHERE l.line_id = p_line AND l.tenant_id = salescall.cur_tenant()),
  soon AS (SELECT w.client_id, count(*)::integer AS p FROM salescall.promises_waiting() w
           WHERE w.task_id <> p_except AND w.callee_at > now() AND w.callee_at <= p_until GROUP BY w.client_id),
  idle AS (SELECT s.client_id FROM salescall.lines_serving() s WHERE s.idle AND s.line_id <> p_line),
  per AS (SELECT soon.p, (SELECT count(*) FROM idle WHERE idle.client_id = soon.client_id)::integer AS d,
                 (me.client_id IS NOT DISTINCT FROM soon.client_id)::integer AS mine
          FROM soon, me),
  shared AS (SELECT count(*)::integer AS s FROM idle WHERE idle.client_id IS NULL)
  SELECT coalesce((SELECT sum(least(p, d + mine)) + least(sum(greatest(p - d - mine, 0)),
                                                          (SELECT s FROM shared) + (SELECT (client_id IS NULL)::integer FROM me))
                   FROM per), 0)
       > coalesce((SELECT sum(least(p, d)) + least(sum(greatest(p - d, 0)), (SELECT s FROM shared)) FROM per), 0)
$$;

-- 業務の平均通話時間（秒）と、数えた本数：お客様が出て、ふつうに終えた・お客様が切った通話の、予約から終わりまで。直近 p_days 日。
CREATE OR REPLACE FUNCTION salescall.business_call_seconds(p_business uuid, p_days integer)
RETURNS TABLE (seconds integer, calls integer)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT coalesce(round(avg(extract(epoch FROM a.ended_at - a.claimed_at)))::integer, 0), count(*)::integer
  FROM salescall.attempts a
  JOIN salescall.business_versions v ON v.version_id = a.version_id
  WHERE v.business_id = p_business AND a.tenant_id = salescall.cur_tenant()
    AND a.state IN ('ended', 'dropped') AND a.ended_at IS NOT NULL
    AND a.claimed_at > now() - make_interval(days => p_days)
    AND salescall.reached_answer(a.attempt_id)
$$;

ALTER TABLE salescall.setting_definitions DISABLE TRIGGER setting_definitions_frozen;
UPDATE salescall.setting_definitions
SET meaning = 'お客様が時刻を指定した約束の前に、回線を空けておく長さの初めの値（その業務の通話の数が gate.exact_promise_guard_samples に'
              '届くまで。届いたら業務の平均通話時間）。この長さのうちに約束が来るなら、約束の数だけの回線でほかの電話を始めない'
WHERE key = 'gate.exact_promise_guard';
ALTER TABLE salescall.setting_definitions ENABLE TRIGGER setting_definitions_frozen;
