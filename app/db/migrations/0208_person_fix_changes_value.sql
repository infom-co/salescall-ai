-- 0208 値が変わらない直しは、お呼びする方の替わりにしない（2026-11-08。書いた人：CL-2。DESIGN.md 3-4-1 の【0208】・2章 Q140〜・
--      15-2 の670〜）
--
-- 計画書 7-1 (19)「お呼びする方の名前・役職・部署を直したときは違う相手」は、名前などの中身が変わったときのこと。⛔ 0203〜0206 は、お呼びする方が
-- 替わる欄（person_fix_field）の直しの記録が1つ増えるだけで担当の期が替わった：同じ名前のまま読みだけを直しても・同じ名前を入れ直しても、
-- 担当の期が替わり、前の担当の方との約束の選びを求め、直す前の通話は前の担当の方への通話になり、予定は新しい担当の方への形になった。
-- ⇒ (1) その直しの直前の、その欄の値を1か所（task_field_before）で決める（その欄の、その直しより前のいちばん新しい直し。無ければ取り込んだ値）。
--    (2) その直しで、お呼びする方が替わるか（person_fix_changes）を1か所にする：お呼びする方が替わる欄（person_fix_field）で、値が直前の値と違う。
--        担当の期（task_person_since）・直しの守り（task_fix_guard）・Python の入口（core/row_fixes.fix。データベースに問い合わせる）が読む。

-- ═════════ (1) その直しの直前の、その欄の値（1か所） ═════════
-- p_seq＝その直しの記録の番号（seq）。NULL＝これから足す直し（いまの値）。
CREATE FUNCTION salescall.task_field_before(p_task uuid, p_field text, p_seq bigint) RETURNS text LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT CASE WHEN p.seq IS NOT NULL THEN p.value
              WHEN p_field = 'source_name' THEN t.source_name
              WHEN p_field = 'target_name' THEN t.target_name
              WHEN p_field = 'target_full_name' THEN t.target_full_name
              WHEN p_field = 'target_role' THEN t.target_role
              WHEN p_field = 'target_department' THEN t.target_department END
  FROM salescall.tasks t
  LEFT JOIN LATERAL (SELECT x.seq, x.value FROM salescall.task_fixes x
                     WHERE x.task_id = t.task_id AND x.field = p_field AND (p_seq IS NULL OR x.seq < p_seq)
                     ORDER BY x.seq DESC LIMIT 1) p ON true
  WHERE t.task_id = p_task
$$;
REVOKE ALL ON FUNCTION salescall.task_field_before(uuid, text, bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.task_field_before(uuid, text, bigint) TO salescall_app;

-- ═════════ (2) その直しで、お呼びする方が替わるか（1か所） ═════════
-- 替わる欄（person_fix_field）で、値（value。読みは見ない）が直前の値と違うときだけ。読みだけの直し・同じ値の入れ直しは替わらない。
CREATE FUNCTION salescall.person_fix_changes(p_task uuid, p_field text, p_value text, p_seq bigint) RETURNS boolean LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT salescall.person_fix_field(p_field) AND p_value IS DISTINCT FROM salescall.task_field_before(p_task, p_field, p_seq)
$$;
REVOKE ALL ON FUNCTION salescall.person_fix_changes(uuid, text, text, bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.person_fix_changes(uuid, text, text, bigint) TO salescall_app;

-- 担当の期（0206 の形から、名前などの直しは、お呼びする方が替わる直しだけにした）
CREATE OR REPLACE FUNCTION salescall.task_person_since(p_task uuid, p_at timestamptz) RETURNS timestamptz LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT max(x.at) FROM (
    SELECT d.recorded_at AS at FROM salescall.task_dial_fixes d                                   -- 「担当の方は違う」を選んだ番号の直し
    WHERE d.task_id = p_task AND NOT d.same_person AND d.recorded_at <= p_at
    UNION ALL
    SELECT f.recorded_at FROM salescall.task_fixes f                                              -- お呼びする方の名前・役職・部署の中身の直し
    WHERE f.task_id = p_task AND salescall.person_fix_changes(f.task_id, f.field, f.value, f.seq) AND f.recorded_at <= p_at) x
$$;

-- salescall.task_fix_guard（0206 の形から、選びが要るのを、お呼びする方が替わる直しだけにした）
CREATE OR REPLACE FUNCTION salescall.task_fix_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT salescall.cur_is_person() THEN
    RAISE EXCEPTION 'お客様リストの行を直せるのは、管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'task_fix_by_person';
  END IF;
  IF NEW.recorded_by IS DISTINCT FROM salescall.cur_actor() THEN
    RAISE EXCEPTION '記録した方は、いま操作している方です' USING ERRCODE = 'check_violation', CONSTRAINT = 'task_fix_recorded_by';
  END IF;
  IF (NEW.keep_old_promise IS NOT NULL) IS DISTINCT FROM
     (salescall.person_fix_changes(NEW.task_id, NEW.field, NEW.value, NULL) AND salescall.task_old_promise_choice_needed(
        NEW.task_id, (SELECT c.phone_e164 FROM salescall.contacts c WHERE c.contact_id = salescall.task_dial_contact(NEW.task_id)))) THEN
    RAISE EXCEPTION '前の担当の方との約束を守るか（keep_old_promise）は、お呼びする方の名前・フルネーム・役職・部署の中身を直し、約束が残っていて、その約束の番号がいまかける番号でないときだけ、必ず選びます'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'task_fix_old_promise_choice';
  END IF;
  RETURN NEW;
END $$;
