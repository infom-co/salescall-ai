-- 0226 会話の側へ渡す「分かっている連絡先」を、いまお呼びする方のものだけにする（2026-11-16。書いた人：CL-2。DESIGN.md 3-5・15-2 の832）
--
-- ⛔ それまで：customer_contacts.by_role（会話の側が重ねて伺わない連絡先＝conversation_io.known_contacts）が、行の連絡先（メール・FAX・住所・LINE の ID）の
--    いちばん新しいものを、担当の期を見ずに渡していた。「担当の方は違う」で直した行で、前の担当の方のメール・LINE の ID を、新しい担当の方の分かっている
--    連絡先として渡し（伺わずに前の担当の方のメールへ資料を送る）、前の担当の方に戻すと逆になった（2026-11-16 の3回目の見直し。計画書 7-1 (22)(30)）。
-- ⇒ 連絡先の持ち主の期（1か所）：通話で伺った連絡先は、その通話の担当の期（通話の person_since）・ほかは記録した時刻の担当の期（番号の持ち主の期
--    salescall.task_number_periods・task_former_number と同じ決まり）。いまの方の連絡先＝その期が、いまの担当の期の連絡先の、種類（とサービス名）ごとの
--    いちばん新しいもの。

CREATE FUNCTION salescall.task_contact_period(p_seq bigint) RETURNS timestamptz LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT CASE WHEN c.attempt_id IS NOT NULL THEN (SELECT a.person_since FROM salescall.attempts a WHERE a.attempt_id = c.attempt_id)
              ELSE salescall.task_person_since(c.task_id, c.recorded_at) END
  FROM salescall.task_contacts c WHERE c.seq = p_seq
$$;
REVOKE ALL ON FUNCTION salescall.task_contact_period(bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.task_contact_period(bigint) TO salescall_app;

CREATE FUNCTION salescall.task_person_contacts(p_task uuid) RETURNS TABLE (kind text, value text, service text) LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT DISTINCT ON (c.kind, coalesce(c.service, '')) c.kind, c.value, c.service
  FROM salescall.task_contacts c
  WHERE c.task_id = p_task
    AND salescall.task_contact_period(c.seq) IS NOT DISTINCT FROM salescall.task_person_since(p_task, now())
  ORDER BY c.kind, coalesce(c.service, ''), c.seq DESC
$$;
REVOKE ALL ON FUNCTION salescall.task_person_contacts(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.task_person_contacts(uuid) TO salescall_app;
