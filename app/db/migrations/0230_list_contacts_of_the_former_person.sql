-- 0230 取り込み直しで、一覧の方が前の担当の方のとき、一覧で増えた番号・連絡先を前の担当の方のものとして残す（2026-11-16。書いた人：CL-2。
--      DESIGN.md 3-4-1・15-2 の837）
--
-- ✅ 2026-11-16 ユーザー（伺い「取り込み直しで、『別の方に替わった』で直した行（一覧は山田部長のまま・いまは佐藤様）に、一覧で増えた携帯・メールを
--    どなたの連絡先とするか」へ）：「前の方の連絡先として残す（お勧め）」。⛔ それまで：足した時刻の担当の期＝いまの担当の方のものになり、佐藤様あての
--    LINE のご招待の SMS が山田様の携帯へ届きえた。
-- ⇒ 番号と連絡先に、持ち主の期を書ける欄 owner_period（NULL＝今までどおり足した時刻・通話で決める／'-infinity'＝最初の担当の方の期／ほか＝その期の始まり。
--    戻す先の期と同じ値＝salescall.back_period）。書くのは取込の登録済みの行の1か所（core/importer）。読むのは持ち主の期の決め方の各1か所
--    （番号＝task_number_periods・task_former_number、連絡先＝task_contact_period）。

ALTER TABLE salescall.task_numbers ADD COLUMN owner_period timestamptz,
  ADD CONSTRAINT task_numbers_owner_period CHECK (owner_period IS NULL OR record_origin = 'list');   -- 書けるのは一覧から足した番号だけ
ALTER TABLE salescall.task_contacts ADD COLUMN owner_period timestamptz,
  ADD CONSTRAINT task_contacts_owner_period CHECK (owner_period IS NULL OR origin = 'list');

CREATE OR REPLACE FUNCTION salescall.task_number_periods(p_task uuid, p_contact uuid, p_at timestamptz) RETURNS SETOF timestamptz LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT salescall.task_person_since(p_task, f.recorded_at) FROM salescall.task_dial_fixes f
  WHERE f.task_id = p_task AND f.contact_id = p_contact AND f.recorded_at <= p_at
  UNION ALL
  SELECT a.person_since FROM salescall.task_designations d JOIN salescall.attempts a ON a.attempt_id = d.attempt_id
  WHERE d.task_id = p_task AND d.contact_id = p_contact AND d.recorded_at <= p_at
  UNION ALL
  SELECT salescall.back_period(n.owner_period) FROM salescall.task_numbers n   -- 持ち主の期を書いた番号（0230）
  WHERE n.task_id = p_task AND n.contact_id = p_contact AND n.owner_period IS NOT NULL AND n.added_at <= p_at
$$;

CREATE OR REPLACE FUNCTION salescall.task_former_number(p_task uuid, p_contact uuid, p_at timestamptz) RETURNS boolean
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT coalesce((
    SELECT NOT salescall.task_current_person_number(p_task, p_contact, p_at)
       AND EXISTS (SELECT 1 FROM salescall.task_numbers n WHERE n.task_id = p_task AND n.contact_id = p_contact
                     AND ((CASE WHEN n.owner_period IS NOT NULL THEN salescall.back_period(n.owner_period)
                                ELSE salescall.task_person_since(p_task, n.added_at) END) IS DISTINCT FROM s.since AND n.added_at <= p_at
                          OR EXISTS (SELECT 1 FROM salescall.attempts a WHERE a.attempt_id = n.source_attempt_id
                                       AND a.person_since IS DISTINCT FROM s.since)))
    FROM (SELECT salescall.task_person_since(p_task, p_at) AS since) s), false)
$$;

CREATE OR REPLACE FUNCTION salescall.task_contact_period(p_seq bigint) RETURNS timestamptz LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT CASE WHEN c.owner_period IS NOT NULL THEN salescall.back_period(c.owner_period)
              WHEN c.attempt_id IS NOT NULL THEN (SELECT a.person_since FROM salescall.attempts a WHERE a.attempt_id = c.attempt_id)
              ELSE salescall.task_person_since(c.task_id, c.recorded_at) END
  FROM salescall.task_contacts c WHERE c.seq = p_seq
$$;
