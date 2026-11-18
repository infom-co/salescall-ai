-- 0225 同じ業務・同じ依頼者で断られた・苦情があったお客様かを、通話の結果から決める（2026-11-16。書いた人：CL-2。DESIGN.md 5-2-1・15-2 の830）
--
-- ⛔ 0168・0216 の salescall.declined_in_business・declined_in_client は、断られた行の「いちばん新しい予定」が「断られた」「苦情」で終わっているかで
--    決めていた（原則14・伺い8・計画書 7-3 (1) の止め）。そのため、次のとき止めが外れた・効かなかった（2026-11-16 の3回目の見直し）：
--    (a) 断られた通話で AI が言ってはいけないことも言った：行は「言ってはいけないことを言った」で終わり、「断られた」で終わらない＝一度も止めない。
--    (b) そのあとお詫びの電話・番号の直し・「担当の方は違う」の直しで、その行に新しい予定が足された：止めが外れ、同じお客様のほかの行（次の実施回・
--        同じ依頼者のほかの業務）からかかった（取り消せない害＝断ったお客様へのかけ直し）。
--    (c) 断られた行の番号すべてで止めた：前の担当の方の断りで、新しい担当の方の番号のほかの行まで止めた（計画書 7-1 (22)(24)「鈴木様へはかける」）。
-- ⇒ (1) 通話の「いまの結果」の1か所 salescall.call_result：その通話のいちばん新しい判定の版（判定し直しを含む）と、そのあと次の通話の判定までに
--        契約者が直した版（人の版＝approval.correct_result。直せるのは行のいちばん新しいシステムの版だけ）のうち、いちばん新しいもの。
--    (2) 断った・苦情のお客様＝いまの結果が「断られた」（declined）・苦情（complaint）の通話の、その通話でお話しした方の番号（その通話の担当の期の
--        方にお呼びできる番号＝salescall.task_number_reaches の1か所）と、前の担当の方への通話でなければ管理番号（依頼者が管理番号をお客様を表す変わらない
--        番号としたとき＝呼ぶ側が渡す）。行の予定は見ない（お詫びの電話・行の戻しでは消えない。判定し直し・契約者の直しで「断りでない」となれば外れる）。
--    (3) 断られた行そのもの（p_task）は数えない：その行の扱い（終えるか・新しい担当の方へ引き継ぐか）は、行の決まりの1か所（next_action.answer_ends_row・
--        answer_still_ends）が決める。止めるのは、同じお客様のほかの行（次の実施回・同じ依頼者のほかの業務）。

CREATE FUNCTION salescall.call_result(p_attempt uuid) RETURNS SETOF salescall.task_results LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT r.* FROM salescall.call_judgements j
  JOIN salescall.task_results r ON r.task_id = j.task_id AND r.version_no >= j.version_no
  WHERE j.attempt_id = p_attempt
    AND (r.attempt_id = p_attempt
         OR (r.kind = 'human' AND NOT EXISTS (   -- 契約者の直しは、その通話の判定のあと、ほかの通話の判定より前のもの
               SELECT 1 FROM salescall.task_results x
               WHERE x.task_id = j.task_id AND x.kind IN ('judged', 'rejudged') AND x.attempt_id IS DISTINCT FROM p_attempt
                 AND x.version_no > j.version_no AND x.version_no < r.version_no)))
  ORDER BY r.version_no DESC LIMIT 1
$$;
REVOKE ALL ON FUNCTION salescall.call_result(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.call_result(uuid) TO salescall_app;

-- お客様の断り・苦情が、その番号・管理番号に当たる通話（1か所。p_kinds＝declined・complaint のどれを数えるか・p_business＝その業務の行だけ
-- （NULL＝依頼者のすべての業務）・p_not_business＝その業務の行を除く）
CREATE FUNCTION salescall.declining_calls(p_client uuid, p_business uuid, p_not_business uuid, p_e164 text, p_client_ref text, p_task uuid,
                                          p_declined boolean, p_complaint boolean)
RETURNS boolean LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT EXISTS (
    SELECT 1 FROM salescall.tasks t
    JOIN salescall.runs r ON r.client_id = t.client_id AND r.run_id = t.run_id
    JOIN salescall.attempts a ON a.task_id = t.task_id
    CROSS JOIN LATERAL salescall.call_result(a.attempt_id) e
    WHERE t.client_id = p_client AND t.task_id IS DISTINCT FROM p_task
      AND (p_business IS NULL OR r.business_id = p_business)
      AND (p_not_business IS NULL OR r.business_id IS DISTINCT FROM p_not_business)
      AND (EXISTS (SELECT 1 FROM salescall.task_numbers k WHERE k.task_id = t.task_id AND k.phone_e164 = p_e164)
           OR (p_client_ref IS NOT NULL AND t.client_ref = p_client_ref))
      AND ((p_declined AND e.business_state = 'declined') OR (p_complaint AND e.complaint))
      AND (EXISTS (SELECT 1 FROM salescall.task_numbers k
                   WHERE k.task_id = t.task_id AND k.phone_e164 = p_e164
                     AND salescall.task_number_reaches(t.task_id, k.contact_id, a.person_since))
           OR (p_client_ref IS NOT NULL AND t.client_ref = p_client_ref AND NOT salescall.former_person_call(a.attempt_id))))
$$;
REVOKE ALL ON FUNCTION salescall.declining_calls(uuid, uuid, uuid, text, text, uuid, boolean, boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.declining_calls(uuid, uuid, uuid, text, text, uuid, boolean, boolean) TO salescall_app;

DROP FUNCTION salescall.declined_in_business(uuid, text, text);
CREATE FUNCTION salescall.declined_in_business(p_business uuid, p_e164 text, p_client_ref text, p_task uuid) RETURNS boolean
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT salescall.declining_calls(b.client_id, p_business, NULL, p_e164, p_client_ref, p_task, true, false)
  FROM salescall.businesses b WHERE b.business_id = p_business
$$;
REVOKE ALL ON FUNCTION salescall.declined_in_business(uuid, text, text, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.declined_in_business(uuid, text, text, uuid) TO salescall_app;

-- 同じ依頼者のほかの業務で断られた・どの業務でも苦情があった（同じ業務の断りは declined_in_business＝業務の設定が受け持つ）
DROP FUNCTION salescall.declined_in_client(uuid, uuid, text, text);
CREATE FUNCTION salescall.declined_in_client(p_client uuid, p_business uuid, p_e164 text, p_client_ref text, p_task uuid) RETURNS boolean
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT salescall.declining_calls(p_client, NULL, p_business, p_e164, p_client_ref, p_task, true, false)
      OR salescall.declining_calls(p_client, NULL, NULL, p_e164, p_client_ref, p_task, false, true)
$$;
REVOKE ALL ON FUNCTION salescall.declined_in_client(uuid, uuid, text, text, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.declined_in_client(uuid, uuid, text, text, uuid) TO salescall_app;
