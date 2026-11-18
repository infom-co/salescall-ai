-- 0213 前の担当の方に戻す選び・約束を守る選びの決まりをそろえる（2026-11-08。書いた人：CL-2。DESIGN.md 3-4-1 の【0213】・2章 Q180〜・
--      15-2 の710〜）
--
-- ✅ 2026-11-08 ユーザー＝計画書 7-1 (30)（伺い「前の方へ戻す」）：担当を鈴木様に直したあと、「やはり田中様だった」と前の担当の方に戻すとき：
--    「前の方へ戻す選び」（直す画面に「前の担当の方に戻す」を出し、戻すと前の担当の方のそのときの答え＝用件の断りなども戻る。断っていれば
--    行は終わり、田中様へはかけない）。
-- ⇒ (1) 直しの記録（名前などの直し task_fixes・番号の直し task_dial_fixes）に、戻す先の期 back_to を持つ（「担当の方は違う」を選んだ直しだけ。
--        NULL＝新しい期・'-infinity'＝最初の担当の方の期・ほか＝その期の始まりの時刻）。戻すと、新しい期を作らず、その期がまた「いまの期」になる。
--    (2) お呼びする方が替わった時刻の1か所（task_person_changes）は、替わった時刻と、替わった先の期の組を返す（戻す直しは、戻す先の期）。
--        担当の期（task_person_since）は、その時刻までのいちばん新しい替わりの、替わった先の期。その期の終わり（task_person_until）は、その期が
--        いちばん新しく始まった（戻った）時刻のあとの、いちばん早い替わり。
--    (3) 戻せる期（task_back_periods）＝その行の前の期だけ（いまの期・まだ無い期には戻せない。データベースが守る＝task_person_back_guard）。
--    (4) 期が「行ったり来たり」するので、番号の持ち主の期を時刻の大小でなく、その番号を足した・直した・指定した時刻の期で読む
--        （task_number_periods の1か所。いまの担当の方の番号 task_current_person_number・前の担当の方の番号 task_former_number・
--        その期の担当の方にお呼びできる番号 task_number_reaches が読む）。
--
-- 本体の決定（2026-11-08。約束を守る選びの決まりをそろえる）：「守る」を選べるかは1か所で決め、その決め方は「その約束の番号で、前の担当の方に
-- お呼びできるか」（task_promise_keepable）＝約束の番号が、ほかの期の担当の方だけの番号（その期のうちに直した番号・その期の通話で指定された番号）
-- でなく、その直しで新しい担当の方だけの番号になる番号（番号の「違う」の直しの番号）でもない。名前などの直しでは、直す前からの会社の番号も
-- 前の担当の方の番号（会話は、その通話の期のお名前でお呼びする＝0210）。直すときの選び（task_old_promise_choice_needed）と、通話中の直しのあとの
-- 前の担当の方の新しい約束の選び（task_old_promise_choices の「守る」＝task_old_promise_keep_guard）が、同じ1か所を読む。
-- ⛔ 0202・0206 の task_old_promise_choice_needed は「約束の番号が、いまかける番号（名前などの直し）・直す番号（番号の直し）なら『守る』は選べない」
--    で、0210 の通話中の名前の直しのあとの選びは、同じ番号でも「守る」を選べた（2つの道で答えが違った）。

-- ═════════ (1) 戻す先の期 ═════════
ALTER TABLE salescall.task_fixes ADD COLUMN back_to timestamptz;        -- 「担当の方は違う」で、前の担当の方に戻す直しだけ（'-infinity'＝最初の期）
ALTER TABLE salescall.task_dial_fixes ADD COLUMN back_to timestamptz;   -- 同じ意味

-- 戻す先の期の値（1か所）：'-infinity'＝最初の担当の方の期（期の始まりは NULL）
CREATE FUNCTION salescall.back_period(p_back_to timestamptz) RETURNS timestamptz LANGUAGE sql IMMUTABLE AS $$
  SELECT nullif(p_back_to, '-infinity'::timestamptz)
$$;
REVOKE ALL ON FUNCTION salescall.back_period(timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.back_period(timestamptz) TO salescall_app;

-- ═════════ (2) お呼びする方が替わった時刻と、替わった先の期（1か所） ═════════
-- 0210 の形（時刻だけ）から、替わった先の期の組にした（戻す直しは、戻す先の期。ほかの替わりは、その時刻が新しい期の始まり）
DROP FUNCTION salescall.task_person_changes(uuid);
CREATE FUNCTION salescall.task_person_changes(p_task uuid) RETURNS TABLE (at timestamptz, since timestamptz) LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT d.recorded_at, CASE WHEN d.back_to IS NULL THEN d.recorded_at ELSE salescall.back_period(d.back_to) END
  FROM salescall.task_dial_fixes d                                                -- 「担当の方は違う」を選んだ番号の直し
  WHERE d.task_id = p_task AND NOT d.same_person
  UNION ALL
  SELECT f.recorded_at, CASE WHEN f.back_to IS NULL THEN f.recorded_at ELSE salescall.back_period(f.back_to) END
  FROM salescall.task_fixes f                                                     -- 「担当の方は違う」を選んだ名前・役職・部署の中身の直し
  WHERE f.task_id = p_task AND salescall.person_fix_changes(f.task_id, f.field, f.value, f.seq, f.same_person)
$$;
REVOKE ALL ON FUNCTION salescall.task_person_changes(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.task_person_changes(uuid) TO salescall_app;

-- 担当の期（その時刻までのいちばん新しい替わりの、替わった先の期。最初の担当の方の期は NULL）
CREATE OR REPLACE FUNCTION salescall.task_person_since(p_task uuid, p_at timestamptz) RETURNS timestamptz LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT c.since FROM salescall.task_person_changes(p_task) c WHERE c.at <= p_at ORDER BY c.at DESC, c.since DESC NULLS LAST LIMIT 1
$$;

-- その期の終わり＝その期がいちばん新しく始まった（戻った）時刻のあとの、いちばん早い替わり（いまの期なら NULL）
CREATE OR REPLACE FUNCTION salescall.task_person_until(p_task uuid, p_since timestamptz) RETURNS timestamptz LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT min(c.at) FROM salescall.task_person_changes(p_task) c
  WHERE c.at > coalesce((SELECT max(b.at) FROM salescall.task_person_changes(p_task) b WHERE b.since IS NOT DISTINCT FROM p_since), '-infinity')
$$;

-- 戻せる期（1か所）：その行にあった期（最初の担当の方の期と、替わった先の期）のうち、いまの期でないもの。p_since＝その期の始まり
-- （最初の担当の方の期は NULL）・last_until＝その期がいちばん新しく終わった時刻（新しい順に並べる）
CREATE FUNCTION salescall.task_back_periods(p_task uuid) RETURNS TABLE (since timestamptz, last_until timestamptz) LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT p.since, salescall.task_person_until(p_task, p.since)
  FROM (SELECT NULL::timestamptz AS since UNION SELECT c.since FROM salescall.task_person_changes(p_task) c) p
  WHERE p.since IS DISTINCT FROM salescall.task_person_since(p_task, now())
$$;
REVOKE ALL ON FUNCTION salescall.task_back_periods(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.task_back_periods(uuid) TO salescall_app;

-- その期へ戻せるか（1か所。直す入口 core/row_fixes と守り task_person_back_guard が読む）
CREATE FUNCTION salescall.task_back_ok(p_task uuid, p_back_to timestamptz) RETURNS boolean LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT EXISTS (SELECT 1 FROM salescall.task_back_periods(p_task) b WHERE b.since IS NOT DISTINCT FROM salescall.back_period(p_back_to))
$$;
REVOKE ALL ON FUNCTION salescall.task_back_ok(uuid, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.task_back_ok(uuid, timestamptz) TO salescall_app;

-- 戻す直しの守り（名前などの直し・番号の直しの両方）：戻せるのは「担当の方は違う」を選んだ直しで、その行の前の期だけ
CREATE FUNCTION salescall.task_person_back_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.back_to IS NOT NULL AND (NEW.same_person IS NOT FALSE OR NOT salescall.task_back_ok(NEW.task_id, NEW.back_to)) THEN
    RAISE EXCEPTION '前の担当の方に戻せるのは、「担当の方は違う」を選んだ直しで、その行の前の担当の方の期だけです（いまの期・まだ無い期には戻せません）'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'task_person_back_to_former';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER task_person_back_guard BEFORE INSERT ON salescall.task_fixes
  FOR EACH ROW EXECUTE FUNCTION salescall.task_person_back_guard();
CREATE TRIGGER task_person_back_guard BEFORE INSERT ON salescall.task_dial_fixes
  FOR EACH ROW EXECUTE FUNCTION salescall.task_person_back_guard();

-- ═════════ (4) 番号の持ち主の期（1か所） ═════════
-- その番号を持つ担当の期：その番号へ直した直しの期（「同じ」の直しも）・その番号を指定した通話の期（p_at まで）
CREATE FUNCTION salescall.task_number_periods(p_task uuid, p_contact uuid, p_at timestamptz) RETURNS SETOF timestamptz LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT salescall.task_person_since(p_task, f.recorded_at) FROM salescall.task_dial_fixes f
  WHERE f.task_id = p_task AND f.contact_id = p_contact AND f.recorded_at <= p_at
  UNION ALL
  SELECT a.person_since FROM salescall.task_designations d JOIN salescall.attempts a ON a.attempt_id = d.attempt_id
  WHERE d.task_id = p_task AND d.contact_id = p_contact AND d.recorded_at <= p_at
$$;
REVOKE ALL ON FUNCTION salescall.task_number_periods(uuid, uuid, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.task_number_periods(uuid, uuid, timestamptz) TO salescall_app;

-- いまの担当の方の番号（0211 の形から、期の始まりのあとの時刻でなく、その番号を持つ期で読む形にした）：いまかける番号・いまの期のうちに直した番号・
-- いまの期の通話で指定された番号
CREATE OR REPLACE FUNCTION salescall.task_current_person_number(p_task uuid, p_contact uuid, p_at timestamptz) RETURNS boolean
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT coalesce((SELECT p_contact = salescall.task_dial_contact_at(p_task, p_at)), false)
      OR EXISTS (SELECT 1 FROM salescall.task_number_periods(p_task, p_contact, p_at) o(since)
                 WHERE o.since IS NOT DISTINCT FROM salescall.task_person_since(p_task, p_at))
$$;

-- 前の担当の方の番号（0203 の形から、期の始まりより前の時刻でなく、ほかの期のうちに足した番号で読む形にした。最初の担当の方の期へ戻したときも
-- 読める＝`WHERE s.since IS NOT NULL` は外した。替わっていなければ、どの番号も、いまの期のもの）：いまの担当の方の番号でなく、ほかの期のうちに
-- 行に足した番号（取り込んだ番号は最初の担当の方の期）か、ほかの期の通話で言われた番号
CREATE OR REPLACE FUNCTION salescall.task_former_number(p_task uuid, p_contact uuid, p_at timestamptz) RETURNS boolean
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT coalesce((
    SELECT NOT salescall.task_current_person_number(p_task, p_contact, p_at)
       AND EXISTS (SELECT 1 FROM salescall.task_numbers n WHERE n.task_id = p_task AND n.contact_id = p_contact
                     AND (salescall.task_person_since(p_task, n.added_at) IS DISTINCT FROM s.since AND n.added_at <= p_at
                          OR EXISTS (SELECT 1 FROM salescall.attempts a WHERE a.attempt_id = n.source_attempt_id
                                       AND a.person_since IS DISTINCT FROM s.since)))
    FROM (SELECT salescall.task_person_since(p_task, p_at) AS since) s), false)
$$;

-- その番号で、その期（p_since）の担当の方にお呼びできるか（1か所）：ほかの期の担当の方の番号（ほかの期のうちに直した番号・ほかの期の通話で
-- 指定された番号）でない
CREATE FUNCTION salescall.task_number_reaches(p_task uuid, p_contact uuid, p_since timestamptz) RETURNS boolean LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT NOT EXISTS (SELECT 1 FROM salescall.task_number_periods(p_task, p_contact, now()) o(since) WHERE o.since IS DISTINCT FROM p_since)
$$;
REVOKE ALL ON FUNCTION salescall.task_number_reaches(uuid, uuid, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.task_number_reaches(uuid, uuid, timestamptz) TO salescall_app;

-- ═════════ 約束を守る選び（本体の決定。1か所） ═════════
-- いまの約束（いまの予定）の番号で、その約束の担当の方（予定の期の方＝前の担当の方）にお呼びできるか：p_phone＝その直しで新しい担当の方だけの
-- 番号になる番号（番号の「違う」の直しの番号。名前などの直し・通話中の直しのあとの選びは NULL＝番号は変わらない）でなく、ほかの期の担当の方の
-- 番号でもない（task_number_reaches）
CREATE FUNCTION salescall.task_promise_keepable(p_task uuid, p_phone text) RETURNS boolean LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT coalesce((SELECT k.phone_e164 IS DISTINCT FROM p_phone AND salescall.task_number_reaches(p_task, n.contact_id, n.person_since)
                   FROM salescall.current_next_actions n JOIN salescall.task_numbers k ON k.task_id = n.task_id AND k.contact_id = n.contact_id
                   WHERE n.task_id = p_task), false)
$$;
REVOKE ALL ON FUNCTION salescall.task_promise_keepable(uuid, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.task_promise_keepable(uuid, text) TO salescall_app;

-- 直すときの選び（前の担当の方との約束も守るか）が要るか（0203 の形から、「守る」を選べるかを task_promise_keepable の1か所で読み、戻す直しを
-- 足した）：約束が残っていて、守れて（その約束の番号で前の担当の方にお呼びできる）、その約束が、直したあとのいまの期の約束でない（戻す直しで、
-- 戻す先の方の約束なら、その方の約束としてまた生きる＝選ばない）。p_back_to＝戻す先の期（NULL＝新しい期）
DROP FUNCTION salescall.task_old_promise_choice_needed(uuid, text);
CREATE FUNCTION salescall.task_old_promise_choice_needed(p_task uuid, p_phone text, p_back_to timestamptz) RETURNS boolean LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT salescall.task_promise_alive(p_task)
     AND salescall.task_promise_keepable(p_task, p_phone)
     AND (p_back_to IS NULL OR (SELECT n.person_since FROM salescall.current_next_actions n WHERE n.task_id = p_task)
                                 IS DISTINCT FROM salescall.back_period(p_back_to))
$$;
REVOKE ALL ON FUNCTION salescall.task_old_promise_choice_needed(uuid, text, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.task_old_promise_choice_needed(uuid, text, timestamptz) TO salescall_app;

-- 番号の直しの守り（0202 の形に、戻す先の期を渡すことを足した）
CREATE OR REPLACE FUNCTION salescall.task_dial_fix_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT salescall.cur_is_person() THEN
    RAISE EXCEPTION 'かける番号を直せるのは、管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'task_dial_fix_by_person';
  END IF;
  IF NEW.recorded_by IS DISTINCT FROM salescall.cur_actor() THEN
    RAISE EXCEPTION '記録した方は、いま操作している方です' USING ERRCODE = 'check_violation', CONSTRAINT = 'task_dial_fix_recorded_by';
  END IF;
  IF (NEW.keep_old_promise IS NOT NULL) IS DISTINCT FROM
     (NOT NEW.same_person AND salescall.task_old_promise_choice_needed(
        NEW.task_id, (SELECT c.phone_e164 FROM salescall.contacts c WHERE c.contact_id = NEW.contact_id), NEW.back_to)) THEN
    RAISE EXCEPTION '前の担当の方との約束を守るか（keep_old_promise）は、「担当の方は違う」を選び、約束が残っていて、その約束の番号で前の担当の方にお呼びできる（直す番号でない）ときだけ、必ず選びます'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'task_dial_fix_old_promise_choice';
  END IF;
  IF EXISTS (SELECT 1 FROM salescall.tasks me JOIN salescall.tasks o ON o.run_id = me.run_id AND o.task_id <> me.task_id
             WHERE me.task_id = NEW.task_id AND NEW.contact_id IN (SELECT salescall.task_dialable(o.task_id))) THEN
    RAISE EXCEPTION 'その番号は、同じ実施回のほかの行がかけうる番号です' USING ERRCODE = 'unique_violation',
      CONSTRAINT = 'task_dial_fix_one_row_per_number';
  END IF;
  RETURN NEW;
END $$;

-- 名前などの直しの守り（0210 の形から、名前などの直しでは新しい担当の方だけの番号が無い＝NULL を渡し、戻す先の期を渡すことを足した）
CREATE OR REPLACE FUNCTION salescall.task_fix_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT salescall.cur_is_person() THEN
    RAISE EXCEPTION 'お客様リストの行を直せるのは、管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'task_fix_by_person';
  END IF;
  IF NEW.recorded_by IS DISTINCT FROM salescall.cur_actor() THEN
    RAISE EXCEPTION '記録した方は、いま操作している方です' USING ERRCODE = 'check_violation', CONSTRAINT = 'task_fix_recorded_by';
  END IF;
  IF (NEW.same_person IS NOT NULL) IS DISTINCT FROM salescall.person_fix_differs(NEW.task_id, NEW.field, NEW.value, NULL) THEN
    RAISE EXCEPTION '担当の方は同じか・違うか（same_person）は、お呼びする方の名前・フルネーム・役職・部署の値が本当に変わる直し（空欄を埋める・消すも入る）だけ、必ず選びます'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'task_fix_same_person_choice';
  END IF;
  IF (NEW.keep_old_promise IS NOT NULL) IS DISTINCT FROM
     (salescall.person_fix_changes(NEW.task_id, NEW.field, NEW.value, NULL, NEW.same_person) AND salescall.task_old_promise_choice_needed(
        NEW.task_id, NULL, NEW.back_to)) THEN
    RAISE EXCEPTION '前の担当の方との約束を守るか（keep_old_promise）は、お呼びする方の名前・フルネーム・役職・部署を「担当の方は違う」で直し、約束が残っていて、その約束の番号で前の担当の方にお呼びできるときだけ、必ず選びます'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'task_fix_old_promise_choice';
  END IF;
  RETURN NEW;
END $$;

-- 前の担当の方の新しい約束の選びで「守る」を選べるのは、その約束の番号で前の担当の方にお呼びできるときだけ（直すときの選びと同じ1か所）
CREATE FUNCTION salescall.task_old_promise_keep_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.keep_old_promise AND NOT salescall.task_promise_keepable(NEW.task_id, NULL) THEN
    RAISE EXCEPTION '前の担当の方との約束を守るのは、その約束の番号で前の担当の方にお呼びできるときだけです（新しい担当の方の番号への約束は守れません）'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'task_old_promise_choice_keepable';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER task_old_promise_keep_guard BEFORE INSERT ON salescall.task_old_promise_choices
  FOR EACH ROW EXECUTE FUNCTION salescall.task_old_promise_keep_guard();
