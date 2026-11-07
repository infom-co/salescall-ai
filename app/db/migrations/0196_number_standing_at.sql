-- 0196 番号の立場を、すべて「その時刻」で読む（2026-11-06 の見直し。書いた人：CL-2。DESIGN.md 3-4-1・15-2 の510）
--
-- ⛔ 0188 の task_number_replaced_at（その時刻の番号の立場）は、いまかける番号だけを「いま」で読み（task_dial_contact）、サブ・お客様が
--    指定した番号の数え始めも「いま」の直しから読んでいた。通話の途中・判定待ちのあいだに契約者が番号を直すと、その通話でかけた番号が
--    「かけたとき前の番号だった」とされ、約束の電話がいきなり新しい番号へ向いた（計画書 7-1 (4)「まだ約束の電話をかけていない場合は、
--    旧番号でかからなければ、新番号でかけます」と食い違った）。
-- ⇒ その時刻のいまかける番号は salescall.task_dial_contact_at の1か所（その時刻までのいちばん新しい直し＝task_latest_dial_fix_at・0194）。
--    いまの番号（task_dial_contact）も、これを読む。サブ・お客様が指定した番号の数え始めは task_count_from_at（0194）。前に直した番号かも、
--    その時刻までの直しで見る。

CREATE FUNCTION salescall.task_dial_contact_at(p_task uuid, p_at timestamptz) RETURNS uuid LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT coalesce((salescall.task_latest_dial_fix_at(p_task, p_at)).contact_id,
                  (SELECT t.contact_id FROM salescall.tasks t WHERE t.task_id = p_task))
$$;
REVOKE ALL ON FUNCTION salescall.task_dial_contact_at(uuid, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.task_dial_contact_at(uuid, timestamptz) TO salescall_app;

CREATE OR REPLACE FUNCTION salescall.task_dial_contact(p_task uuid) RETURNS uuid LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$ SELECT salescall.task_dial_contact_at(p_task, 'infinity'::timestamptz) $$;

CREATE OR REPLACE FUNCTION salescall.task_sub_contact_at(p_task uuid, p_at timestamptz) RETURNS uuid LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT s.contact_id FROM salescall.task_sub_numbers s
  WHERE s.task_id = p_task AND s.recorded_at >= salescall.task_count_from_at(p_task, p_at) AND s.recorded_at <= p_at
    AND s.contact_id IS DISTINCT FROM salescall.task_dial_contact_at(p_task, p_at)
  ORDER BY s.seq DESC LIMIT 1
$$;

CREATE OR REPLACE FUNCTION salescall.task_designated_contact_at(p_task uuid, p_at timestamptz) RETURNS uuid LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT d.contact_id FROM salescall.task_designations d
  WHERE d.task_id = p_task AND d.recorded_at >= salescall.task_count_from_at(p_task, p_at) AND d.recorded_at <= p_at
  ORDER BY d.seq DESC LIMIT 1
$$;

CREATE OR REPLACE FUNCTION salescall.task_number_replaced_at(p_task uuid, p_contact uuid, p_at timestamptz) RETURNS boolean
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT p_contact IS DISTINCT FROM salescall.task_dial_contact_at(p_task, p_at)
     AND p_contact IS DISTINCT FROM salescall.task_sub_contact_at(p_task, p_at)
     AND p_contact IS DISTINCT FROM salescall.task_designated_contact_at(p_task, p_at)
     AND (p_contact = (SELECT t.contact_id FROM salescall.tasks t WHERE t.task_id = p_task)
          OR EXISTS (SELECT 1 FROM salescall.task_dial_fixes f WHERE f.task_id = p_task AND f.contact_id = p_contact
                       AND f.recorded_at <= p_at))
$$;
