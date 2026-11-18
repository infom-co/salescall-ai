-- 0193 番号の直しの見直し（2026-11-06 の第三者の見直しの直し。書いた人：CL-2。DESIGN.md 3-4-1・15-2 の 505）
--
-- (1) 直しの時刻はデータベースが決める（0188 の task_designations・task_sub_numbers と同じ stamp_now）。⛔ 前は契約者の取引が
--     task_dial_fixes（0179）・task_fixes（0178）の recorded_at を好きに書けた：かける番号の直しの時刻を先の時刻にすると、行の回数を
--     数え始める時刻（task_count_from）が先になり、行の回数が0のまま・サブも効かなかった。お呼びする方の直しの時刻を前にすると、
--     約束のあとに相手が替わったか（next_action._target_changed_since）を取り違えた。
-- (2) 「いちばん新しいかける番号の直し」は1か所（task_latest_dial_fix＝順番の番号 seq のいちばん大きいもの）。いまかける番号
--     （task_dial_contact）と、行の回数を数え始める時刻（task_count_from）の両方がこれを読む。⛔ 前は、いまかける番号は seq、
--     数え始めは時刻の最大で決めていて、2つの直しの時刻と順が食い違うと、別の直しを指した。
-- (3) お客様と約束した電話かは1か所（promise_call＝目的 normal で約束の印）。サブを作る通話（call_makes_sub）と、Python の次の動き
--     （next_action の約束の始まり・番号を直したときの向け直し・終えた行の戻し）が、どれもこれを読む。続きの電話（resume。約束の印は
--     付く）は約束の電話ではない（✅ 2026-11-06 ユーザー：サブは「約束の電話だけ」と伺ったとき、続きの電話は約束の外として伺った）。
--     ⛔ 前は Python が「約束の印で目的 normal か resume」、0190 が「目的 normal で約束の印」と、2つの決め方があった。
-- (4) その行がかけうる番号は1か所（task_dialable）：取り込んだ番号・いまかける番号・サブ・お客様が指定した番号・いまの予定の番号。
--     同じ実施回のほかの行への直しの守り（task_dial_fix_guard）と、追加の取込（core/importer.py）の両方がこれを読む。⛔ 前は守りが
--     「いまかける番号とサブ」、取込が「取り込んだ番号といまかける番号」で、行B を X から Y に直したあと（行B のまだかけていない約束の
--     電話は X のまま）、行A を X に直せた。お客様が指定した番号も見ていなかった。
--     同時に2つの直し（または直しと取込）が同じ番号へ向かっても、書く取引は直列化できる形（SERIALIZABLE。core/access）で動くので、
--     あとから確定するほうが、やり直しで先の直しを見て断られる（試験 tests.test_import.DialFixes で確かめる）。

-- ═════════ (1) ═════════
CREATE TRIGGER stamp_now BEFORE INSERT ON salescall.task_dial_fixes FOR EACH ROW EXECUTE FUNCTION salescall.stamp_now('recorded_at');
CREATE TRIGGER stamp_now BEFORE INSERT ON salescall.task_fixes FOR EACH ROW EXECUTE FUNCTION salescall.stamp_now('recorded_at');

-- ═════════ (2) ═════════
CREATE FUNCTION salescall.task_latest_dial_fix(p_task uuid) RETURNS salescall.task_dial_fixes LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT f.* FROM salescall.task_dial_fixes f WHERE f.task_id = p_task ORDER BY f.seq DESC LIMIT 1
$$;
REVOKE ALL ON FUNCTION salescall.task_latest_dial_fix(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.task_latest_dial_fix(uuid) TO salescall_app;

CREATE OR REPLACE FUNCTION salescall.task_dial_contact(p_task uuid) RETURNS uuid LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT coalesce((salescall.task_latest_dial_fix(p_task)).contact_id,
                  (SELECT t.contact_id FROM salescall.tasks t WHERE t.task_id = p_task))
$$;
CREATE OR REPLACE FUNCTION salescall.task_count_from(p_task uuid) RETURNS timestamptz LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT coalesce((salescall.task_latest_dial_fix(p_task)).recorded_at, '-infinity'::timestamptz)
$$;

-- ═════════ (3) ═════════
CREATE FUNCTION salescall.promise_call(p_purpose text, p_promised boolean) RETURNS boolean LANGUAGE sql IMMUTABLE AS
$$ SELECT coalesce(p_purpose = 'normal' AND p_promised, false) $$;
GRANT EXECUTE ON FUNCTION salescall.promise_call(text, boolean) TO salescall_app;

CREATE OR REPLACE FUNCTION salescall.call_makes_sub(p_attempt uuid) RETURNS boolean LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT coalesce((SELECT salescall.promise_call(a.purpose, a.promised)                         -- お客様と約束したかけ直しの電話
                          AND a.claimed_at >= salescall.task_count_from(a.task_id)              -- いちばん新しい直しのあとの通話
                          AND salescall.task_number_replaced_at(a.task_id, a.contact_id, a.claimed_at)   -- かけたとき前の番号
                   FROM salescall.attempts a WHERE a.attempt_id = p_attempt), false)
$$;

-- ═════════ (4) ═════════
CREATE FUNCTION salescall.task_dialable(p_task uuid) RETURNS SETOF uuid LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT c FROM unnest(ARRAY[
      (SELECT t.contact_id FROM salescall.tasks t WHERE t.task_id = p_task),                    -- 取り込んだ番号
      salescall.task_dial_contact(p_task),                                                       -- いまかける番号
      salescall.task_sub_contact(p_task),                                                        -- サブ
      salescall.task_designated_contact_at(p_task, now()),                                       -- お客様が指定した番号
      (SELECT n.contact_id FROM salescall.current_next_actions n WHERE n.task_id = p_task AND n.kind = 'call')   -- いまの予定の番号
    ]) AS c
  WHERE c IS NOT NULL
$$;
REVOKE ALL ON FUNCTION salescall.task_dialable(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.task_dialable(uuid) TO salescall_app;

CREATE OR REPLACE FUNCTION salescall.task_dial_fix_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF salescall.cur_actor_kind() IS DISTINCT FROM 'operator' AND salescall.cur_actor_kind() IS DISTINCT FROM 'admin' THEN
    RAISE EXCEPTION 'かける番号を直せるのは、管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'task_dial_fix_by_person';
  END IF;
  IF NEW.recorded_by IS DISTINCT FROM salescall.cur_actor() THEN
    RAISE EXCEPTION '記録した方は、いま操作している方です' USING ERRCODE = 'check_violation', CONSTRAINT = 'task_dial_fix_recorded_by';
  END IF;
  IF EXISTS (SELECT 1 FROM salescall.tasks me JOIN salescall.tasks o ON o.run_id = me.run_id AND o.task_id <> me.task_id
             WHERE me.task_id = NEW.task_id AND NEW.contact_id IN (SELECT salescall.task_dialable(o.task_id))) THEN
    RAISE EXCEPTION 'その番号は、同じ実施回のほかの行がかけうる番号です' USING ERRCODE = 'unique_violation',
      CONSTRAINT = 'task_dial_fix_one_row_per_number';
  END IF;
  RETURN NEW;
END $$;
