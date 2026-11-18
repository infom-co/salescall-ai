-- 0111 ほかの依頼者がお客様と約束している番号へ、約束の前にかけるとき（2026-10-22 ユーザー決定）。書いた人：CL-2
--
-- ユーザー（2026-10-22）：「基本は声を変え、契約者ごとに設定で選べるといいですね」（2026-10-17 の決定「依頼者が違うと、期間に
-- 関係なく、声と発信番号は変えた方がいい」の続き）。
-- - 場面：依頼者Aがお客様と「15時にかけ直す」と約束している番号へ、依頼者Bの一覧の電話が、その前にかかりうる。
-- - 基本（初めの値 call_other_voice）：かける。声は、ほかの依頼者がその番号で聞かせた声と替える（前からの決まり＝gate.choose_voice）。
-- - 契約者が選べる（wait）：ほかの依頼者の約束がまだ済んでいない（その行のいまの予定が約束の電話のまま）あいだは、その番号へ、
--   約束でない電話をかけない。約束の時刻を過ぎても、まだかけていなければ待つ（約束の電話のあとは、依頼者をまたいだ間隔＝
--   gate.cross_client_interval・gate.other_contractor_interval が、そのまま効く）。
-- - 契約者が選べる（never）：ほかの依頼者の約束がまだ済んでいない番号へは、約束でない電話をかけない。その行は終える（2026-10-22
--   ユーザー：「『約束のあとまで待つ』に加え、『同じ番号には送らない』も選べた方がいいんじゃない」）。
-- - どちらも決めるのは門の間隔の1か所（core/gate.py の _pacing。約束でない電話＝promised でないときだけ）。お客様と約束したかけ直し・
--   お詫び・途中で切れたあとの続き・番号の聞き直しは止めない。取込（プレビュー・登録）では判断しない（約束は時間で済むので、
--   取り込むときの事実で行を止めない。⛔ 2026-10-22 の見直し13：前は「かけない」をデータで決まる理由に置き、約束の電話・お詫び
--   まで止めて行を終え、取込でも止めていた）。
-- - ほかの依頼者・契約者の約束は、関係（同じ契約者か）と、同じ契約者のときだけ時刻を返す関数で読む（ほかの契約者の約束は、ある
--   ことだけ。時刻も中身も出さない＝依頼者をまたいだ間隔の「契約者の外は中身を出さない」と同じ。⛔ 見直し13：前は時刻を返した）。

INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, finest_scope, choices) VALUES
('gate.before_other_promise', 'override', 'choice',
 'ほかの依頼者がお客様と約束している番号へ、約束の前にかけるか（call_other_voice＝声を替えてかける・既定／wait＝約束の時刻が過ぎるまで待つ／never＝その番号へはかけない。契約者ごと）',
 'DESIGN.md 5-2 の5', 'tenant', ARRAY['call_other_voice', 'wait', 'never']);
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('gate.before_other_promise', 'system', '"call_other_voice"', 'decided',
 '2026-10-22 ユーザー決定：「基本は声を変え、契約者ごとに設定で選べるといいですね」「『約束のあとまで待つ』に加え、『同じ番号には送らない』も選べた方がいいんじゃない」',
 '契約者が変えたいとき', 'ユーザー');

-- その番号へ、ほかの依頼者（ほかの契約者を含む）の、まだ済んでいない約束の電話：関係（same_tenant／other_tenant）ごとに1行。
-- wait_until＝待つ時刻（幅の約束は幅の終わり、無ければ約束の時刻とかける時刻の遅いほう）。ほかの契約者の分は空（時刻を出さない）。
CREATE FUNCTION salescall.number_next_promise_elsewhere(p_phone text) RETURNS TABLE (relation text, wait_until timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT x.relation, CASE WHEN x.relation = 'same_tenant' THEN max(x.until) END
  FROM (SELECT CASE WHEN n.tenant_id = salescall.cur_tenant() THEN 'same_tenant' ELSE 'other_tenant' END AS relation,
               coalesce(n.window_until, greatest(n.callee_at, n.call_at)) AS until
        FROM salescall.current_next_actions n
        JOIN salescall.task_numbers k ON k.task_id = n.task_id AND k.contact_id = n.contact_id
        WHERE k.phone_e164 = p_phone AND n.client_id IS DISTINCT FROM salescall.cur_client()
          AND n.kind = 'call' AND n.promised) x
  GROUP BY x.relation
$$;
REVOKE ALL ON FUNCTION salescall.number_next_promise_elsewhere(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.number_next_promise_elsewhere(text) TO salescall_app;
