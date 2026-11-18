-- 0227 「お客様がその声を聞いたか」を1か所にする（2026-11-16。書いた人：CL-2。DESIGN.md 2章 Q11・15-2 の833）
--
-- ⛔ それまで：同じお客様には同じ声で話す決まり（門の声の選び＝core/gate.choose_voice）が、同じ行では「発信した通話」の声（鳴らなかった・出なかった
--    通話も入る）を、ほかの行では「お客様が出た通話」の声（0216 の salescall.number_last_voice_elsewhere）を、お客様が聞いた声としていた。同じ行で、
--    出た通話の声 A のあと、A が使えないあいだに声 B でかけて出なかったら、A が戻っても B で話し、お客様が聞いたことの無い声になった（2026-11-16 の
--    3回目の見直し）。
-- ⇒ お客様がその通話の声を聞いたか（1か所）＝発信まで進み（is_dialed）、相手が出た（reached_answer）。同じ行の声も、ほかの行の声も、これで読む。

CREATE FUNCTION salescall.voice_heard(p_attempt uuid) RETURNS boolean LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT coalesce((SELECT salescall.is_dialed(a.state) AND salescall.reached_answer(a.attempt_id)
                   FROM salescall.attempts a WHERE a.attempt_id = p_attempt), false)
$$;
REVOKE ALL ON FUNCTION salescall.voice_heard(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.voice_heard(uuid) TO salescall_app;

CREATE OR REPLACE FUNCTION salescall.number_last_voice_elsewhere(p_phone text, p_task uuid) RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT v.voice_name FROM salescall.attempts a JOIN salescall.voice_types v ON v.code = a.voice_type
  WHERE a.phone_e164 = p_phone AND a.task_id IS DISTINCT FROM p_task
    AND EXISTS (SELECT 1 FROM salescall.tasks t WHERE t.task_id = p_task AND t.client_id = salescall.cur_client())   -- 取引の依頼者の行だけ
    AND salescall.voice_heard(a.attempt_id)
  ORDER BY a.claimed_at DESC LIMIT 1
$$;
