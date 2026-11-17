-- 0235 声と発信番号は、業務ごとに決まる（2026-11-17 の5回目の見直し。書いた人：CL-2。DESIGN.md 7-2・15-2 の846。
--      ✅ 2026-11-17 ユーザー：「業務が違えば、同じ人に同じ声や同じ電話番号でかけるのは絶対禁止です。また同じ人からかかってきたなと思われるからです。
--      同じ業務であれば、同じ人には同じ声でかけないとおかしいでしょ。」）
--
-- ⛔ 0216・0227：同じ依頼者のほかの行と比べるのは、その番号へのいちばん新しい電話の声・発信番号だけだった。そのため、業務 X が声 V、業務 Y が声 W、
--    業務 Z が（W を避けて）V でかけたあと、業務 X が自分の声 V でかけ、お客様は別の業務の電話を続けて同じ声で聞いた。発信番号も同じで、いちばん新しい
--    電話の回線だけを避けたので、業務 X が業務 Y の発信番号でかけうる。
-- ⇒ (1) お客様（その番号）が、同じ依頼者のほかの業務の電話で聞いた声は、どれも使わない（salescall.number_voices_of_other_businesses）。同じ業務
--        （ほかの実施回を含む）がその番号で前に使った声は、続けて使う（salescall.number_business_voice）。
--    (2) 発信番号も同じ：同じ依頼者のほかの業務がその番号へかけた回線は、いつでも使わない。ほかの依頼者がその番号へかけた回線も（0216 のまま）。

CREATE FUNCTION salescall.number_voices_of_other_businesses(p_phone text, p_task uuid) RETURNS text[]
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT coalesce(array_agg(DISTINCT v.voice_name), '{}')
  FROM salescall.attempts a JOIN salescall.voice_types v ON v.code = a.voice_type
  JOIN salescall.tasks o ON o.task_id = a.task_id JOIN salescall.runs ro ON ro.client_id = o.client_id AND ro.run_id = o.run_id
  JOIN salescall.tasks t ON t.task_id = p_task JOIN salescall.runs rt ON rt.client_id = t.client_id AND rt.run_id = t.run_id
  WHERE a.phone_e164 = p_phone AND t.client_id = salescall.cur_client() AND o.client_id = t.client_id
    AND ro.business_id IS DISTINCT FROM rt.business_id AND salescall.voice_heard(a.attempt_id)
$$;
REVOKE ALL ON FUNCTION salescall.number_voices_of_other_businesses(text, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.number_voices_of_other_businesses(text, uuid) TO salescall_app;

CREATE FUNCTION salescall.number_business_voice(p_phone text, p_task uuid) RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT a.voice_type FROM salescall.attempts a
  JOIN salescall.tasks o ON o.task_id = a.task_id JOIN salescall.runs ro ON ro.client_id = o.client_id AND ro.run_id = o.run_id
  JOIN salescall.tasks t ON t.task_id = p_task JOIN salescall.runs rt ON rt.client_id = t.client_id AND rt.run_id = t.run_id
  WHERE a.phone_e164 = p_phone AND t.client_id = salescall.cur_client() AND o.client_id = t.client_id
    AND ro.business_id = rt.business_id AND a.voice_type IS NOT NULL AND salescall.voice_heard(a.attempt_id)
  ORDER BY a.claimed_at DESC LIMIT 1
$$;
REVOKE ALL ON FUNCTION salescall.number_business_voice(text, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.number_business_voice(text, uuid) TO salescall_app;

CREATE OR REPLACE FUNCTION salescall.line_called_number_elsewhere(p_line uuid, p_phone text, p_task uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT EXISTS (SELECT 1 FROM salescall.attempts a
                 WHERE a.line_id = p_line AND a.tenant_id = salescall.cur_tenant() AND a.phone_e164 = p_phone
                   AND a.client_id IS DISTINCT FROM salescall.cur_client() AND salescall.is_placed(a.state))
      OR EXISTS (SELECT 1 FROM salescall.attempts a
                 JOIN salescall.tasks o ON o.task_id = a.task_id JOIN salescall.runs ro ON ro.client_id = o.client_id AND ro.run_id = o.run_id
                 JOIN salescall.tasks t ON t.task_id = p_task JOIN salescall.runs rt ON rt.client_id = t.client_id AND rt.run_id = t.run_id
                 WHERE a.line_id = p_line AND a.tenant_id = salescall.cur_tenant() AND a.client_id = salescall.cur_client()
                   AND a.phone_e164 = p_phone AND ro.business_id IS DISTINCT FROM rt.business_id AND salescall.is_placed(a.state))
$$;
