-- 0111 ほかの依頼者がお客様と約束している番号へ、約束の前にかけるとき（2026-10-22 ユーザー決定）。書いた人：CL-2
--
-- ユーザー（2026-10-22）：「基本は声を変え、契約者ごとに設定で選べるといいですね」（2026-10-17 の決定「依頼者が違うと、期間に
-- 関係なく、声と発信番号は変えた方がいい」の続き）。
-- - 場面：依頼者Aがお客様と「15時にかけ直す」と約束している番号へ、依頼者Bの一覧の電話が、その前にかかりうる。
-- - 基本（初めの値 call_other_voice）：かける。声は、ほかの依頼者がその番号で聞かせた声と替える（前からの決まり＝gate.choose_voice）。
-- - 契約者が選べる（wait）：ほかの依頼者の約束の時刻が過ぎるまで、その番号へは、約束でない電話をかけない（約束の電話のあとは、
--   依頼者をまたいだ間隔＝gate.cross_client_interval・gate.other_contractor_interval が、そのまま効く）。決めるのは門の間隔の1か所
--   （core/gate.py の _pacing）。
-- - 契約者が選べる（never）：ほかの依頼者が約束している番号へは、かけない（2026-10-22 ユーザー：「『約束のあとまで待つ』に加え、
--   『同じ番号には送らない』も選べた方がいいんじゃない」）。データで決まる「かけない理由」の1か所（core/target_policy.py）が答える
--   （ほかの契約者がかけた番号に「かけない」と同じ形。その行は終える）。
-- - ほかの依頼者・契約者の約束は、時刻だけを返す関数で読む（中身は出さない）。

INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, finest_scope, choices) VALUES
('gate.before_other_promise', 'override', 'choice',
 'ほかの依頼者がお客様と約束している番号へ、約束の前にかけるか（call_other_voice＝声を替えてかける・既定／wait＝約束の時刻が過ぎるまで待つ／never＝その番号へはかけない。契約者ごと）',
 'DESIGN.md 5-2 の5', 'tenant', ARRAY['call_other_voice', 'wait', 'never']);
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('gate.before_other_promise', 'system', '"call_other_voice"', 'decided',
 '2026-10-22 ユーザー決定：「基本は声を変え、契約者ごとに設定で選べるといいですね」「『約束のあとまで待つ』に加え、『同じ番号には送らない』も選べた方がいいんじゃない」',
 '契約者が変えたいとき', 'ユーザー');

-- その番号へ、ほかの依頼者（ほかの契約者を含む）が約束している、これからの電話の最も早い時刻（無ければ空。時刻のほかは返さない）
CREATE FUNCTION salescall.number_next_promise_elsewhere(p_phone text) RETURNS timestamptz
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT min(coalesce(n.callee_at, n.call_at))
  FROM salescall.current_next_actions n
  JOIN salescall.task_numbers k ON k.task_id = n.task_id AND k.contact_id = n.contact_id
  WHERE k.phone_e164 = p_phone AND n.client_id IS DISTINCT FROM salescall.cur_client()
    AND n.kind = 'call' AND n.promised AND coalesce(n.callee_at, n.call_at) > now()
$$;
REVOKE ALL ON FUNCTION salescall.number_next_promise_elsewhere(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.number_next_promise_elsewhere(text) TO salescall_app;
