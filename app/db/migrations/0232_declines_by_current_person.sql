-- 0232 管理番号で止める断り・苦情も、いまの担当の方の断りかで決める（2026-11-16 の4回目の見直し。書いた人：CL-2。DESIGN.md 5-2-1・15-2 の840）
--
-- ⛔ 0225 の salescall.declining_calls は、断り・苦情の通話のいまの結果（call_result）から止める範囲を決めるが、2つの道で時点が違った：
--    番号の道＝その番号で、その通話の担当の期の方にいまお呼びできるか（task_number_reaches＝いまの番号の持ち主）・管理番号の道＝その通話が、締めたときに
--    前の担当の方への通話だったか（former_person_call＝締めたときの期）。そのため、同じ田中様の「うちは要らない」でも、「担当の方は違う」で鈴木様に直す
--    前に断っていれば、次の実施回・同じ依頼者のほかの業務の、同じ管理番号の鈴木様の行まで止め、直したあとに断っていれば止めなかった（計画書 7-1 (22)(24)(26)
--    「鈴木様へはかける」と、直す前と後の答えが食い違った）。
-- ⇒ 管理番号の道も、いまの期で決める：断った通話の担当の期が、その行のいまの担当の期のときだけ（attempt_in_period(通話, now())）。前の担当の方に戻せば
--    また止まる（7-1 (30)）。断り・苦情の止めは、いまの結果から決まる形（判定し直し・契約者の直しで外れる＝0225）なので、時点も「いま」にそろえる。
--    発信禁止（取り消せない決定。締めたときに範囲を決める＝dnc.refusal_reach）は変えない。

CREATE OR REPLACE FUNCTION salescall.declining_calls(p_client uuid, p_business uuid, p_not_business uuid, p_e164 text, p_client_ref text, p_task uuid,
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
           OR (p_client_ref IS NOT NULL AND t.client_ref = p_client_ref AND salescall.attempt_in_period(a.attempt_id, now()))))
$$;
