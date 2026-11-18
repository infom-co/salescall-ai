-- 0236 管理番号で止める断り・苦情を、同じお名前の方の行にも当てる（2026-11-17 の5回目の見直し。書いた人：CL-2。DESIGN.md 5-2-1・15-2 の847。
--      ✅ 2026-11-17 ユーザー：伺い「田中様が断ったあと『担当の方は違う』で鈴木様に直した行と同じ管理番号の、次の回の一覧の田中様あての行にかけるか」に
--      「名前が同じなら止める（お勧め）」）
--
-- ⛔ 0232：管理番号で止めるのは、断った方がいまもその行の担当の方のときだけだった。そのため、断った行を鈴木様に直すと、次の実施回・ほかの業務の、
--    同じ管理番号でまだ田中様あての行（一覧が古い）にかけた（会社の番号だけなら、番号でも止まらない）。
-- ⇒ 管理番号の道：断った通話の担当の期が、断った行のいまの期であるか、調べている行（p_task）のお呼びする方が、断った方と同じお名前（その通話の期の
--    お名前＝salescall.task_person_values。比べ方は表記をそろえた形 salescall.compare_form。フルネームが両方にあればフルネームで、無ければお名前で）。
--    同じお名前かを決めるのは salescall.same_named_person の1か所。

CREATE FUNCTION salescall.same_named_person(p_task uuid, p_since timestamptz, p_other uuid) RETURNS boolean LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT coalesce((
    SELECT CASE WHEN v.target_full_name IS NOT NULL AND n.target_full_name IS NOT NULL
                THEN salescall.compare_form(v.target_full_name) = salescall.compare_form(n.target_full_name)
                ELSE v.target_name IS NOT NULL AND n.target_name IS NOT NULL
                     AND salescall.compare_form(v.target_name) = salescall.compare_form(n.target_name) END
    FROM salescall.task_person_values(p_task, p_since) v, salescall.tasks_now n WHERE n.task_id = p_other), false)
$$;
REVOKE ALL ON FUNCTION salescall.same_named_person(uuid, timestamptz, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.same_named_person(uuid, timestamptz, uuid) TO salescall_app;

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
           OR (p_client_ref IS NOT NULL AND t.client_ref = p_client_ref
               AND (salescall.attempt_in_period(a.attempt_id, now())
                    OR (p_task IS NOT NULL AND salescall.same_named_person(t.task_id, a.person_since, p_task))))))
$$;
