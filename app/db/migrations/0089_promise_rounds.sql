-- 0089 約束したかけ直しの次の回・約束の日にかけられなかったお詫び・約束の電話の取り置き（DESIGN.md 7-1d・7-2・5-2・15-2 の193〜195。
-- 2026-10-19 ユーザー決定。書いた人：CL-2）
--
-- ユーザー（2026-10-19）：「お客様が、13日を選ばれた理由が何かが必要でしょう。例えば、金曜だったからや、土日祝だったから、
-- 平日だったからなどです。理由のあるものはどの規則に従うのがいいでしょう。理由がどうしても分からないものは「時刻の決まっていない
-- 約束」の方針の方がいいかもですね」「月を選んだ場合、たまたま希望日が月末・月の初め・上旬にあたった場合です。しかし、月ぎめの
-- 業務の都合や振り込みなどの入金の間隔などの話の場合は、月単位で考えた方がいい」「（回数は時刻の決まっていない約束の方針の回数）OK」
-- 「「その日に一度もかけられなかった」場合は、次につながった際には、お約束の日にかけることができなかったことを深くお詫びする必要」
-- 「その日の約束の電話には目印が付き、それを処理してから、通常の順序の電話を始めた方がいい」「（回線の1日の上限の取り置き）OK」
--
-- (1) 次の予定の幅に、いまの回の本当の始まり（window_from）・話せなかったときの次の回の決め方（window_repeat＝day／week／
--     month:...。読むのは core/spoken_time、次の回を探すのは core/next_action の1か所）・何回目の回か（window_round。時刻の
--     決まっていない約束の方針の回数まで）を持たせる。
-- (2) 約束の日（回）に一度もかけられなかった ⇒ 次にお客様につながったときに、お詫びから始める（apology_owed。次の予定が
--     代わっても引き継ぎ、目的のお客様と話せた通話の締めで外す。会話の処理は予約証の印を読んで話す）。
-- (3) 回線の1日の上限で、今日の約束の電話の分を取り置く（ふつうの電話は、その分を使わない）。数えるのは、この関数の1か所。

ALTER TABLE salescall.task_next_actions
  ADD COLUMN window_from timestamptz,
  ADD COLUMN window_repeat text,
  ADD COLUMN window_round integer NOT NULL DEFAULT 0,
  ADD COLUMN apology_owed boolean NOT NULL DEFAULT false,
  ADD CONSTRAINT task_next_actions_window_round CHECK (window_round >= 0),
  ADD CONSTRAINT task_next_actions_window_repeat CHECK (
    window_repeat IS NULL
    OR (window_until IS NOT NULL
        AND window_repeat ~ '^(day|week|month:(all|part:[^:]+|day:([1-9]|[12][0-9]|3[01])))$')),
  ADD CONSTRAINT task_next_actions_window_from CHECK (
    window_from IS NULL OR (window_until IS NOT NULL AND window_from < window_until));

-- 今日、その回線でかける約束の電話の数（予定の時刻が今日までの、約束したかけ直し）。その回線でかけてよい依頼者（専用の回線なら
-- その依頼者、共有の回線なら契約者のすべての依頼者）の行を数える。p_except＝いま門に来ている行（自分の分は取り置かない）。
-- ⚠️ 回線が2本以上あると、どの回線も同じ約束の分を取り置く（多めに残す側＝約束の電話を落とさない側に倒す）。
CREATE FUNCTION salescall.line_promised_due(p_line uuid, p_day date, p_except uuid) RETURNS integer
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT count(*)::integer
  FROM salescall.lines l
  JOIN salescall.tasks t ON t.tenant_id = l.tenant_id AND (l.client_id IS NULL OR t.client_id = l.client_id)
  JOIN LATERAL (SELECT n.kind, n.promised, n.call_at FROM salescall.task_next_actions n WHERE n.task_id = t.task_id
                ORDER BY n.seq DESC LIMIT 1) n ON true
  WHERE l.line_id = p_line AND l.tenant_id = salescall.cur_tenant()
    AND t.task_id <> p_except
    AND n.kind = 'call' AND n.promised AND salescall.jst(n.call_at)::date <= p_day
    AND salescall.client_active(t.client_id) AND NOT salescall.task_excluded_now(t.task_id)   -- かけない約束は取り置かない
$$;
REVOKE ALL ON FUNCTION salescall.line_promised_due(uuid, date, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.line_promised_due(uuid, date, uuid) TO salescall_app;
