-- 0234 目的のお客様だと確かめられなかった通話を1か所で決め、その通話で伺った連絡先も使わない・連絡先のいまの値を担当の方ごとに1つにする
--      （2026-11-17 の5回目の見直し。書いた人：CL-2。DESIGN.md 7-1e・3-4-1・15-2 の845）
--
-- ⛔ 0108〜0233：(a) 確かめられなかった通話（判定なしで繋ぎ、確かめ直しで別の方・確かめられなかった）かを、答えの1か所（conversation_io._in_force）・
--    判定（target_recheck.unconfirmed）が別々に書いていた。その通話で伺った連絡先（ほかのご希望のメールなど）は、報告を受け取ったときに行へ足し、
--    確かめ直しのあとも、会話へ渡す連絡先・画面のいまの連絡先に使っていた（別の方の連絡先へ資料を送りうる）。
--    (b) 連絡先のいまの値が2か所：会話へ渡す salescall.task_person_contacts（いまの担当の方の連絡先＝0226）と、画面の見方 task_contacts_now（持ち主を
--    見ずに、いちばん新しい値）。0230 で前の担当の方のものとして残したメールが、画面ではいまのメールに見えた。
-- ⇒ (1) 確かめられなかった通話か＝salescall.call_unconfirmed の1か所（target_rechecks の結果が target でない）。
--    (2) いまの連絡先＝見方 task_contacts_now の1か所：いまの担当の方の期の連絡先（task_contact_period）で、確かめられなかった通話で伺ったものを除いた、
--        種類ごとのいちばん新しい値。会話へ渡す task_person_contacts は、これを読む。

CREATE FUNCTION salescall.call_unconfirmed(p_attempt uuid) RETURNS boolean LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT EXISTS (SELECT 1 FROM salescall.target_rechecks x WHERE x.attempt_id = p_attempt AND x.outcome <> 'target')
$$;
REVOKE ALL ON FUNCTION salescall.call_unconfirmed(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.call_unconfirmed(uuid) TO salescall_app;

CREATE OR REPLACE VIEW salescall.task_contacts_now WITH (security_invoker = true) AS
  SELECT DISTINCT ON (c.task_id, c.kind, coalesce(c.service, '')) c.tenant_id, c.client_id, c.task_id, c.kind, c.value, c.service, c.origin,
         c.import_id, c.source_row, c.attempt_id, c.turn, c.recorded_by, c.recorded_at
  FROM salescall.task_contacts c
  WHERE salescall.task_contact_period(c.seq) IS NOT DISTINCT FROM salescall.task_person_since(c.task_id, now())
    AND NOT (c.attempt_id IS NOT NULL AND salescall.call_unconfirmed(c.attempt_id))
  ORDER BY c.task_id, c.kind, coalesce(c.service, ''), c.seq DESC;

CREATE OR REPLACE FUNCTION salescall.task_person_contacts(p_task uuid) RETURNS TABLE (kind text, value text, service text) LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT n.kind, n.value, n.service FROM salescall.task_contacts_now n WHERE n.task_id = p_task
$$;
