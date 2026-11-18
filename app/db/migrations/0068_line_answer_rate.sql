-- 0068 番号ごとの応答率の見張り（Intent.md 8-6「番号ごとの1日の上限を設け、番号ごとの応答率の急な低下を監視する」。
-- DESIGN.md 5-2-3・15-2 の165。2026-10-18。書いた人：CL-2）
--
-- 同じ番号から短時間に大量にかけると、お客様の端末やアプリの迷惑電話の判定に載り、出てもらえなくなる（Intent.md 8-6）。
-- 前は、1日の上限（line.max_dials_per_day）はあったが、出てもらえる割合が落ちたことに気づく仕組みが無かった。
-- ⇒ 回線（番号）ごとに、今日の「出てもらえた割合」を、その回線の前の日々の同じ時間帯の割合と比べ、はっきり落ちていたら契約者へ
--    知らせる（core/answer_rate.py。止めない＝お客様に影響が出るものではない。原則6）。
--
-- (1) 回線の、日・時間帯ごとの「かけた本数」と「出てもらえた本数」（数えるのはこの関数の1か所。契約者の取引で、その契約者の回線だけ）。
--     かけた本数＝お客様の電話を鳴らした（呼び出し中に入った）か、話し中・応答なしで終わった通話（回線や仕組みの誤りで鳴らせなかった
--     通話は数えない＝番号の評判と関係ない）。出てもらえた本数＝そのうち、電話に出た（応答に入った）通話。
--     お詫びの電話・途中で切れたあとのかけ直しは数えない（もう話したお客様への電話で、評判の材料にならない）。
-- (2) 設定（契約者ごと）：どこまで落ちたら知らせるか（いつもの割合に対する比）と、知らせるのに要る今日の本数。

CREATE FUNCTION salescall.line_answer_counts(p_line uuid, p_from date, p_to date)
RETURNS TABLE (day date, hour integer, dials integer, answers integer)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT salescall.jst(a.claimed_at)::date, extract(hour FROM salescall.jst(a.claimed_at))::integer,
         count(*)::integer,
         count(*) FILTER (WHERE EXISTS (SELECT 1 FROM salescall.attempt_state_history h
                                        WHERE h.attempt_id = a.attempt_id AND h.to_state = 'answered'))::integer
  FROM salescall.attempts a
  WHERE a.line_id = p_line AND a.tenant_id = salescall.cur_tenant() AND salescall.cur_client() IS NULL
    AND a.purpose NOT IN ('apology', 'resume')
    AND salescall.jst(a.claimed_at)::date BETWEEN p_from AND p_to
    AND EXISTS (SELECT 1 FROM salescall.attempt_state_history h
                WHERE h.attempt_id = a.attempt_id AND h.to_state IN ('ringing', 'busy', 'no_answer'))
  GROUP BY 1, 2
$$;
REVOKE ALL ON FUNCTION salescall.line_answer_counts(uuid, date, date) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.line_answer_counts(uuid, date, date) TO salescall_app;

INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, finest_scope) VALUES
('line.answer_drop_ratio', 'override', 'ratio',
 '回線（番号）の今日の「出てもらえた割合」が、その回線のいつもの同じ時間帯の割合の、この比より下がったら、契約者へ知らせる（迷惑電話として扱われているおそれ。発信は止めない）',
 'DESIGN.md 5-2-3', 'tenant'),
('line.answer_drop_min_dials', 'override', 'integer',
 '回線の応答率の低下を知らせるのに要る、その日にかけた本数（少ない本数の偶然のぶれで知らせない）',
 'DESIGN.md 5-2-3', 'tenant');
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('line.answer_drop_ratio', 'system', '0.5', 'provisional',
 '「急な低下」＝いつもの半分より下。時間帯の違い（昼休み・夕方）は、同じ時間帯どうしで比べて除く。さらに、偶然では100回に1回も起きない下がり方のときだけ知らせる（answer_rate.SIGNIFICANCE）',
 '最初の本番の回線の実測を見て', 'CL-2'),
('line.answer_drop_min_dials', 'system', '30', 'provisional',
 'いつもの応答率が3割なら、30本で約9本出てもらえる見込み。それより少ない本数では、半分に下がっても偶然と見分けにくい',
 '最初の本番の回線の実測を見て', 'CL-2');
