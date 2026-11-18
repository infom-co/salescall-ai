-- 0206 お呼びする方の名前などの直しでも、前の担当の方との約束を守るかを選ぶ（2026-11-08。書いた人：CL-2。DESIGN.md 3-4-1 の【0206】・
--      2章 Q120〜・15-2 の630〜）
--
-- 計画書 7-1 (19)「お呼びする方の名前・役職・部署を直したときは違う相手」：名前・フルネーム・役職・部署の直し（task_fixes）でも担当の期が
-- 替わる（0203 の task_person_since）。⛔ 0205 までは、番号の「違う」の直しと違い、前の担当の方との約束が残っていても「守るか」の選びが無く、
-- 直す前に決めた約束の予定が前の担当の方の期のまま残り、その電話は前の担当の方への通話になった（契約者の選び無しに「守る」になっていた）。
-- ⇒ 番号の直しの「違う」と同じ決まりにそろえる（7-1 (21)(25)・0202 の本体の決定「約束の番号が新しい担当の方の番号なら『守る』は選べない」）：
-- (1) お呼びする方が替わる欄か（person_fix_field）を1か所にする。担当の期（task_person_since）と、直しの守り（task_fix_guard）と、
--     Python の入口（core/row_fixes.fix）が読む。⛔ 前は task_person_since が欄の組を書き写していた。
-- (2) 直しの記録（task_fixes）に、契約者の選び keep_old_promise（番号の直しの task_dial_fixes.keep_old_promise と同じ意味）を持つ。
--     選びが要るかは、番号の直しと同じ1か所 task_old_promise_choice_needed（0203）：名前などの直しでは番号は変わらないので、新しい担当の方へ
--     かける番号は、いまかける番号（task_dial_contact）。約束の番号がいまかける番号なら、前の担当の方へかける道が無いので選べない（約束をやめ、
--     引き継ぎを確かめる電話＝core/next_action の1か所）。別の番号（前の担当の方の携帯など）なら、必ず選ぶ。
-- (3) 選びの見方（old_promise_choices）に、名前などの直しの選びも入れる（いまの約束に対する選び task_promise_choice が読む）。

-- ═════════ (1) お呼びする方が替わる欄（1か所） ═════════
CREATE FUNCTION salescall.person_fix_field(p_field text) RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
  SELECT coalesce(p_field IN ('target_name', 'target_full_name', 'target_role', 'target_department'), false)
$$;
REVOKE ALL ON FUNCTION salescall.person_fix_field(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.person_fix_field(text) TO salescall_app;

-- 担当の期（0203 の形から、欄の組を1か所へ移した）
CREATE OR REPLACE FUNCTION salescall.task_person_since(p_task uuid, p_at timestamptz) RETURNS timestamptz LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT max(x.at) FROM (
    SELECT d.recorded_at AS at FROM salescall.task_dial_fixes d                                   -- 「担当の方は違う」を選んだ番号の直し
    WHERE d.task_id = p_task AND NOT d.same_person AND d.recorded_at <= p_at
    UNION ALL
    SELECT f.recorded_at FROM salescall.task_fixes f                                              -- お呼びする方の名前・役職・部署の直し
    WHERE f.task_id = p_task AND salescall.person_fix_field(f.field) AND f.recorded_at <= p_at) x
$$;

-- ═════════ (2) 名前などの直しの選び ═════════
ALTER TABLE salescall.task_fixes ADD COLUMN keep_old_promise boolean;   -- 前に直した行は、選ぶ形が無かったので無い（前の動きのまま）

-- salescall.task_fix_guard（0201 の形に、前の担当の方との約束の選びを足した。決め方は番号の直しの守り task_dial_fix_guard と同じ1か所）
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
     (salescall.person_fix_field(NEW.field) AND salescall.task_old_promise_choice_needed(
        NEW.task_id, (SELECT c.phone_e164 FROM salescall.contacts c WHERE c.contact_id = salescall.task_dial_contact(NEW.task_id)))) THEN
    RAISE EXCEPTION '前の担当の方との約束を守るか（keep_old_promise）は、お呼びする方の名前・フルネーム・役職・部署を直し、約束が残っていて、その約束の番号がいまかける番号でないときだけ、必ず選びます'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'task_fix_old_promise_choice';
  END IF;
  RETURN NEW;
END $$;

-- ═════════ (3) 選びを1つに読む ═════════
-- 0202 の形に、名前などの直しの選びを足した（列は同じ）
CREATE OR REPLACE VIEW salescall.old_promise_choices WITH (security_invoker = true) AS
  SELECT f.task_id, f.keep_old_promise, f.recorded_at FROM salescall.task_dial_fixes f WHERE f.keep_old_promise IS NOT NULL
  UNION ALL
  SELECT c.task_id, c.keep_old_promise, c.recorded_at FROM salescall.task_old_promise_choices c
  UNION ALL
  SELECT x.task_id, x.keep_old_promise, x.recorded_at FROM salescall.task_fixes x WHERE x.keep_old_promise IS NOT NULL;
