-- 0263 顧客獲得型で用件・最終目的（アポ）を断られたお客様に、同じ業務の次の実施回で、前の電話から日数をあけてかける（2026-11-20。書いた人：CL-2。
--      DESIGN_15-2 の915。CL-1 の連絡帳 4-207 の (1) の (f)・4-208 の (5)・CL-2 の連絡帳 4-445）
--
-- ✅ 2026-11-20 ユーザー（計画書 9-9）「伺い83：アポをはっきり断られたあと、同じ業務の次の回でかけるか。切られたり起こっていなかったらウ（間をあけてかける。30日）」。
-- 切られた・怒っていたら、次の実施回でもかけない＝発信禁止の印（伺い80。会話の側が電話の終わりに付ける）・苦情（依頼者のどの業務からもかけない＝0216）が受け持つ。
-- ⛔ それまで：断られたお客様は、電話の型によらず業務の設定 business.call_declined_again だけで決め、顧客獲得型でも次の実施回で一度もかけなかった。
-- ⇒ 断られた電話を数える1か所（salescall.declining_calls＝0225・0232・0236）を、断られた電話そのもの（その電話の業務の版・終わった時刻）を返す
--    salescall.declining_attempts にし、declining_calls はその有無だけを読む（決め方は1か所のまま）。同じ業務の断りは、電話の型ごとの扱い
--    （core/business_settings.DECLINED_NEXT）で Python が分ける：確認の電話＝業務の設定 business.call_declined_again／顧客獲得型＝門が、前の電話から
--    業務の設定 business.declined_wait_days の日数あけてかける（core/target_policy.declined_wait_until・gate の間隔の1か所）。

CREATE FUNCTION salescall.declining_attempts(p_client uuid, p_business uuid, p_not_business uuid, p_e164 text, p_client_ref text, p_task uuid,
                                             p_declined boolean, p_complaint boolean)
RETURNS TABLE (attempt_id uuid, version_id uuid, ended_at timestamptz) LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT a.attempt_id, a.version_id, coalesce(a.ended_at, a.claimed_at)
  FROM salescall.tasks t
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
                  OR (p_task IS NOT NULL AND salescall.same_named_person(t.task_id, a.person_since, p_task)))))
$$;
REVOKE ALL ON FUNCTION salescall.declining_attempts(uuid, uuid, uuid, text, text, uuid, boolean, boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.declining_attempts(uuid, uuid, uuid, text, text, uuid, boolean, boolean) TO salescall_app;

CREATE OR REPLACE FUNCTION salescall.declining_calls(p_client uuid, p_business uuid, p_not_business uuid, p_e164 text, p_client_ref text, p_task uuid,
                                                     p_declined boolean, p_complaint boolean)
RETURNS boolean LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT EXISTS (SELECT 1 FROM salescall.declining_attempts(p_client, p_business, p_not_business, p_e164, p_client_ref, p_task,
                                                            p_declined, p_complaint))
$$;

-- 同じ業務で断られた電話（電話の型ごとに分けるのは Python＝core/target_policy の1か所）。前の真偽の形 declined_in_business は外す（読み手は target_policy だけ）
DROP FUNCTION salescall.declined_in_business(uuid, text, text, uuid);
CREATE FUNCTION salescall.declined_in_business(p_business uuid, p_e164 text, p_client_ref text, p_task uuid)
RETURNS TABLE (version_id uuid, ended_at timestamptz) LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT d.version_id, d.ended_at
  FROM salescall.businesses b
  CROSS JOIN LATERAL salescall.declining_attempts(b.client_id, p_business, NULL, p_e164, p_client_ref, p_task, true, false) d
  WHERE b.business_id = p_business
$$;
REVOKE ALL ON FUNCTION salescall.declined_in_business(uuid, text, text, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.declined_in_business(uuid, text, text, uuid) TO salescall_app;

INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, finest_scope) VALUES
('business.declined_wait_days', 'override', 'integer',
 '顧客獲得型（こちらからおかけする営業の電話）の業務で、アポ（最終目的）を断られたお客様に、同じ業務を後日もう一度実施する（新しい実施回）とき、'
 '前の電話から何日あけてかけるかです（初め30日）。その実施回では、もうかけません。お客様に切られた・怒っていたときは発信禁止・苦情になり、'
 '次の実施回でもかけません。確認の電話の業務には使いません（business.call_declined_again で決めます）',
 '計画書 9-9', 'business');
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('business.declined_wait_days', 'system', '30', 'decided',
 '2026-11-20 ユーザー「伺い83：アポをはっきり断られたあと、同じ業務の次の回でかけるか。切られたり起こっていなかったらウ（間をあけてかける。30日）」',
 '業務ごとに契約者が選ぶとき', 'ユーザー');
