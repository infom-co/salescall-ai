-- 0075 番号ごとの応答率の数え方を直す（DESIGN.md 5-2-3・15-2 の171。2026-10-18 の見直し。書いた人：CL-2）
--
-- 見直しで見つかった2つの穴（0068 の数え方）：
-- (1) かけた本数に数える条件が「呼び出し中・話し中・応答なしに入った」だけだった。回線によっては、呼び出し中を知らせずに、
--     発信中からすぐ「出た」へ移る（試行の状態の決まりで許している）。その通話は、かけた本数にも出てもらえた本数にも入らず、
--     割合が低く出た（回線の部品が呼び出し中を知らせるかで、間違えて知らせるか、低下を見逃す）。
-- (2) いま呼び出し中の通話を「出てもらえなかった」に数えていた（まだ結果が出ていない）。
-- ⇒ かけた本数＝お客様の電話を鳴らしたか、出てもらえたか、話し中・応答なしで終わった、終わった通話（進行中の通話は数えない）。
--    出てもらえた本数＝そのうち、電話に出た（応答に入った）通話。ほかは 0068 と同じ。

CREATE OR REPLACE FUNCTION salescall.line_answer_counts(p_line uuid, p_from date, p_to date)
RETURNS TABLE (day date, hour integer, dials integer, answers integer)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT salescall.jst(a.claimed_at)::date, extract(hour FROM salescall.jst(a.claimed_at))::integer,
         count(*)::integer,
         count(*) FILTER (WHERE EXISTS (SELECT 1 FROM salescall.attempt_state_history h
                                        WHERE h.attempt_id = a.attempt_id AND h.to_state = 'answered'))::integer
  FROM salescall.attempts a
  WHERE a.line_id = p_line AND a.tenant_id = salescall.cur_tenant() AND salescall.cur_client() IS NULL
    AND a.purpose NOT IN ('apology', 'resume')
    AND NOT salescall.is_active(a.state)
    AND salescall.jst(a.claimed_at)::date BETWEEN p_from AND p_to
    AND EXISTS (SELECT 1 FROM salescall.attempt_state_history h
                WHERE h.attempt_id = a.attempt_id AND h.to_state IN ('ringing', 'answered', 'busy', 'no_answer'))
  GROUP BY 1, 2
$$;
