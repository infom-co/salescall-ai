-- 0201 「担当の方は違う」と前の担当の方との約束・見直し (1)(2) の残りの守りと一本化（2026-11-07。書いた人：CL-2。DESIGN.md 3-4-1 の【0201】・
--      3-6・3-8-3・2章 Q60〜・15-2 の570〜）
--
-- (1) ✅ 2026-11-07 ユーザー（計画書 7-1 (21)。伺い「違う方の約束」）：番号を直すとき「担当の方は違う」を選び、前の担当の方とのかけ直しの約束が
--     まだ残っていたら「前の担当者とのかけ直しの約束があったが、次の担当にだけかけるのかを契約者に問うて、選択してもらう。」
--     ⇒ かける番号の直し（task_dial_fixes）に、契約者の選び keep_old_promise（真＝前の担当の方との約束も守る・偽＝次の担当の方にだけ
--     かける）を持つ。選びが要るのは「違う」で、まだ守っていない約束が残っているとき（task_promise_open の1か所）だけ。要るときは必ず
--     持ち、要らないときは持たない（データベースが守る＝task_dial_fix_guard）。
--     約束が残っているかの判断（約束の始まり・約束の電話をもうかけたか）は、ここの関数の1か所にする（core/next_action の _promise_since・
--     _promise_tried も、これを読む）。
-- (2) 「違う」を選んだ直しのあと、前の番号で前の担当の方と話せても、前の番号をサブにしない（サブは「新しい番号がつながらないときに元の
--     番号へもかける」＝同じ方のための番号。違う方の番号へは、新しい担当の方の代わりにかけない）。決めるのは call_makes_sub の1か所。
-- (3) 通話の途中の申告の印 in_call（0198）をデータベースが守る：書き換えられない（dnc_before_change）・付けられるのは、その通話でかけた
--     番号で、お客様が出た通話の登録だけ（dnc_in_call_guard）。⛔ 前は、締めが入れた登録の印をあとから真にでき、契約者の取引が別の番号に
--     印つきの登録を書けた（第三者の見直し (2) の4）。
-- (4) 「いま操作している方は管理者か契約者か」を1か所（cur_is_person）にし、その決まりを持つ関数をすべて、これを読む形に作り直す
--     （下の (8)。どれも、その関数のいちばん新しい形を元にした）。
-- (5) その解決策の業務か（client_solutions → 行 → 実施回 → 業務）を1か所（solution_business）にし、4つの守りが読む。
-- (6) 営業の電話の目的（normal・resume＝引き継ぎを確かめる印 handover を持てる・番号を直したらいまかける番号へ替える）を1か所
--     （sales_purpose）にし、次の予定の CHECK が読む（Python の next_action.SALES_PURPOSES と同じ組かは試験が見張る）。
-- (7) 写しの元のいちばん元の版（version_copy_root）を、版の元をたどる1か所（version_origins）から読む（⛔ 前は version_copies だけを
--     たどる再帰が別にあった）。
-- (8) 実施回の版の手の割り当て（run_versions の auto でない行）は、つながりの先の版だけ（core/business_settings.assign は、途中の版を
--     選ぶと写しを作って割り当てる。⛔ 前は契約者の取引が run_versions へ直接、途中の版を割り当てられた＝写しを作らず、手で外した印も
--     付かなかった。第三者の見直し (2) の5）。手で外した印は、割り当てと同じ取引で、データベースが付ける（下の (9)）。

-- ═════════ (4) いま操作している方は管理者か契約者か（1か所） ═════════
CREATE FUNCTION salescall.cur_is_person() RETURNS boolean LANGUAGE sql STABLE AS $$
  SELECT coalesce(salescall.cur_actor_kind() IN ('operator', 'admin'), false)
$$;
GRANT EXECUTE ON FUNCTION salescall.cur_is_person() TO salescall_app;

-- ═════════ (1) 約束が残っているか（1か所） ═════════
-- いまの約束の始まり（お客様と約束したかけ直しの予定が続いている間の、最初の約束の予定の時刻。無ければ NULL）。約束の予定かは promise_call
-- （0193。続きの電話の予定は約束の始まりにしない）
CREATE FUNCTION salescall.task_promise_since(p_task uuid) RETURNS timestamptz LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT min(n.decided_at) FROM salescall.task_next_actions n
  WHERE n.task_id = p_task AND salescall.promise_call(n.purpose, n.promised)
    AND n.decided_at > coalesce((SELECT max(m.decided_at) FROM salescall.task_next_actions m
                                 WHERE m.task_id = n.task_id AND m.kind = 'call' AND NOT m.promised), '-infinity'::timestamptz)
$$;
REVOKE ALL ON FUNCTION salescall.task_promise_since(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.task_promise_since(uuid) TO salescall_app;

-- いまの約束の電話を、もうかけたか（約束の始まりのあとに、お客様のところで鳴った電話がある＝rang の1か所。0124）
CREATE FUNCTION salescall.task_promise_tried(p_task uuid) RETURNS boolean LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT coalesce((SELECT EXISTS (SELECT 1 FROM salescall.attempts a WHERE a.task_id = p_task AND a.claimed_at >= s.since
                                    AND salescall.rang(a.state, a.cut_off, a.attempt_id))
                   FROM (SELECT salescall.task_promise_since(p_task) AS since) s WHERE s.since IS NOT NULL), false)
$$;
REVOKE ALL ON FUNCTION salescall.task_promise_tried(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.task_promise_tried(uuid) TO salescall_app;

-- まだ守っていない約束が残っているか：いまの予定が、お客様と約束したかけ直しの電話で、その約束の電話をまだかけていない
CREATE FUNCTION salescall.task_promise_open(p_task uuid) RETURNS boolean LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT coalesce((SELECT salescall.promise_call(n.purpose, n.promised) FROM salescall.current_next_actions n
                   WHERE n.task_id = p_task AND n.kind = 'call'), false)
     AND NOT salescall.task_promise_tried(p_task)
$$;
REVOKE ALL ON FUNCTION salescall.task_promise_open(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.task_promise_open(uuid) TO salescall_app;

ALTER TABLE salescall.task_dial_fixes ADD COLUMN keep_old_promise boolean;   -- 前に直した行は、選ぶ形が無かったので無い（前の動きのまま）

-- 0193 の形に、(4) の1か所と、前の担当の方との約束の選びを足した
CREATE OR REPLACE FUNCTION salescall.task_dial_fix_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT salescall.cur_is_person() THEN
    RAISE EXCEPTION 'かける番号を直せるのは、管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'task_dial_fix_by_person';
  END IF;
  IF NEW.recorded_by IS DISTINCT FROM salescall.cur_actor() THEN
    RAISE EXCEPTION '記録した方は、いま操作している方です' USING ERRCODE = 'check_violation', CONSTRAINT = 'task_dial_fix_recorded_by';
  END IF;
  IF (NEW.keep_old_promise IS NOT NULL) IS DISTINCT FROM (NOT NEW.same_person AND salescall.task_promise_open(NEW.task_id)) THEN
    RAISE EXCEPTION '前の担当の方との約束を守るか（keep_old_promise）は、「担当の方は違う」を選び、まだ守っていない約束が残っているときだけ、必ず選びます'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'task_dial_fix_old_promise_choice';
  END IF;
  IF EXISTS (SELECT 1 FROM salescall.tasks me JOIN salescall.tasks o ON o.run_id = me.run_id AND o.task_id <> me.task_id
             WHERE me.task_id = NEW.task_id AND NEW.contact_id IN (SELECT salescall.task_dialable(o.task_id))) THEN
    RAISE EXCEPTION 'その番号は、同じ実施回のほかの行がかけうる番号です' USING ERRCODE = 'unique_violation',
      CONSTRAINT = 'task_dial_fix_one_row_per_number';
  END IF;
  RETURN NEW;
END $$;

-- ═════════ (2) サブにするのは、担当の方が同じ直しのあとだけ ═════════
CREATE OR REPLACE FUNCTION salescall.call_makes_sub(p_attempt uuid) RETURNS boolean LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT coalesce((SELECT salescall.promise_call(a.purpose, a.promised)                         -- お客様と約束したかけ直しの電話
                          AND a.claimed_at >= salescall.task_count_from(a.task_id)              -- いちばん新しい直しのあとの通話
                          AND salescall.task_number_replaced_at(a.task_id, a.contact_id, a.claimed_at)   -- かけたとき前の番号
                          AND (salescall.task_latest_dial_fix_at(a.task_id, a.claimed_at)).same_person   -- その直しで、担当の方は同じ
                   FROM salescall.attempts a WHERE a.attempt_id = p_attempt), false)
$$;

-- ═════════ (3) 通話の途中の申告の印 ═════════
-- 0065 の形に、印 in_call（変えられない欄）と (4) の1か所を足した
CREATE OR REPLACE FUNCTION salescall.dnc_before_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION '発信禁止の登録は消せません（取り消しは記録つきで）' USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  IF OLD.revoked_at IS NOT NULL
     OR (NEW.tenant_id, NEW.client_id, NEW.entry_id, NEW.phone_e164, NEW.client_ref, NEW.reason, NEW.source_attempt_id,
         NEW.registered_at, NEW.registered_by, NEW.import_id, NEW.in_call)
        IS DISTINCT FROM
        (OLD.tenant_id, OLD.client_id, OLD.entry_id, OLD.phone_e164, OLD.client_ref, OLD.reason, OLD.source_attempt_id,
         OLD.registered_at, OLD.registered_by, OLD.import_id, OLD.in_call) THEN
    RAISE EXCEPTION '発信禁止の登録は、取り消すこと（1回）しかできません' USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  IF NEW.revoked_at IS NOT NULL AND NOT salescall.cur_is_person() THEN
    RAISE EXCEPTION '発信禁止の取り消しは、管理者か契約者だけができます（システムの処理はできません）' USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN NEW;
END $$;

CREATE FUNCTION salescall.dnc_in_call_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  -- 理由・出どころの通話・番号の形は CHECK dnc_entries_in_call_declared（0198）が守る。ここは、その通話との合い方だけ
  IF NEW.in_call AND NEW.reason = 'refused_in_call' AND NEW.source_attempt_id IS NOT NULL AND NEW.phone_e164 IS NOT NULL
     AND NOT EXISTS (SELECT 1 FROM salescall.attempts a WHERE a.attempt_id = NEW.source_attempt_id
                       AND a.phone_e164 = NEW.phone_e164 AND salescall.reached_answer(a.attempt_id)) THEN
    RAISE EXCEPTION '通話の途中の申告の印は、その通話でかけた番号の、お客様が出た通話からの登録にだけ付けます'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'dnc_entries_in_call_of_call';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER dnc_in_call_guard BEFORE INSERT ON salescall.dnc_entries
  FOR EACH ROW EXECUTE FUNCTION salescall.dnc_in_call_guard();

-- ═════════ (5) その解決策の業務（1か所） ═════════
CREATE FUNCTION salescall.solution_business(p_solution uuid) RETURNS uuid LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT r.business_id FROM salescall.client_solutions c JOIN salescall.tasks t ON t.task_id = c.task_id
    JOIN salescall.runs r ON r.run_id = t.run_id
  WHERE c.solution_id = p_solution
$$;
REVOKE ALL ON FUNCTION salescall.solution_business(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.solution_business(uuid) TO salescall_app;

-- 0197 の形に、(4)(5) の1か所を足した
CREATE OR REPLACE FUNCTION salescall.client_solution_line_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.chosen_by IS NOT NULL THEN
    -- 契約者が「流れに入っていない前の解決策」の一覧から選んで足した（core/client_solutions.add_earlier）：選んだのは、いま操作している
    -- 管理者か契約者
    IF NEW.chosen_by IS DISTINCT FROM salescall.cur_actor() OR NOT salescall.cur_is_person() THEN
      RAISE EXCEPTION '前の解決策を選んで足せるのは、いま操作している管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege',
        CONSTRAINT = 'client_solution_line_chosen_by_person';
    END IF;
  ELSIF NOT EXISTS (SELECT 1 FROM salescall.client_solutions c WHERE c.solution_id = NEW.solution_id
                    AND c.recorded_by = salescall.cur_actor()) THEN
    RAISE EXCEPTION '流れごとの結果を書けるのは、その解決策を足した方だけです' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'client_solution_line_by_recorder';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM salescall.business_versions b WHERE b.version_id = NEW.base_version
                   AND b.business_id = salescall.solution_business(NEW.solution_id))
     OR (NEW.version_id IS NOT NULL AND NEW.version_id <> NEW.base_version
         AND NOT EXISTS (SELECT 1 FROM salescall.version_line(NEW.base_version) l WHERE l.version_id = NEW.version_id)) THEN
    RAISE EXCEPTION '流れの版は、その解決策の業務の版で、足した版は足す元の版か、そのつながりの先の版だけです' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'client_solution_line_versions';
  END IF;
  RETURN NEW;
END $$;

-- 0197 の形に、(5) の1か所を足した
CREATE OR REPLACE FUNCTION salescall.client_solution_replacement_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.recorded_at := now();
  IF NEW.recorded_by IS DISTINCT FROM salescall.cur_actor() THEN
    RAISE EXCEPTION '記録した方は、いま操作している方です' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'client_solution_replacement_recorded_by';
  END IF;
  IF NOT salescall.version_made_now(NEW.version_id)
     OR NOT EXISTS (SELECT 1 FROM salescall.business_versions b WHERE b.version_id = NEW.version_id
                      AND b.business_id = salescall.solution_business(NEW.solution_id)) THEN
    RAISE EXCEPTION '解決策を置き換えた版は、その解決策の業務の、その取引で取り込み直した版だけです' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'client_solution_replacement_version';
  END IF;
  RETURN NEW;
END $$;

-- 0200 の形に、(5) の1か所を足した
CREATE OR REPLACE FUNCTION salescall.client_solution_removal_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.recorded_at := now();
  IF NEW.recorded_by IS DISTINCT FROM salescall.cur_actor() THEN
    RAISE EXCEPTION '記録した方は、いま操作している方です' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'client_solution_removal_recorded_by';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM salescall.run_versions v WHERE v.run_id = NEW.run_id AND v.version_id = NEW.version_id AND NOT v.auto
                   AND v.assigned_at = now() AND v.assigned_by = salescall.cur_actor())
     OR NOT EXISTS (SELECT 1 FROM salescall.business_versions b WHERE b.version_id = NEW.version_id
                      AND b.business_id = salescall.solution_business(NEW.solution_id)) THEN
    RAISE EXCEPTION '手で外した解決策を残せるのは、その取引でその実施回に、その業務の版を手で割り当てたときだけです'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'client_solution_removal_by_assignment';
  END IF;
  RETURN NEW;
END $$;

-- 0200 の形に、(5) の1か所を足した
CREATE OR REPLACE FUNCTION salescall.client_solution_line_run_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.chosen_run IS NOT NULL AND NOT EXISTS (
       SELECT 1 FROM salescall.runs o WHERE o.run_id = NEW.chosen_run AND o.business_id = salescall.solution_business(NEW.solution_id)) THEN
    RAISE EXCEPTION '「入れる」を選んだ実施回は、その解決策の業務の実施回だけです' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'client_solution_line_chosen_run';
  END IF;
  RETURN NEW;
END $$;

-- ═════════ (6) 営業の電話の目的（1か所） ═════════
CREATE FUNCTION salescall.sales_purpose(p_purpose text) RETURNS boolean LANGUAGE sql IMMUTABLE AS
$$ SELECT coalesce(p_purpose IN ('normal', 'resume'), false) $$;
GRANT EXECUTE ON FUNCTION salescall.sales_purpose(text) TO salescall_app;
ALTER TABLE salescall.task_next_actions DROP CONSTRAINT task_next_actions_handover_calls;
ALTER TABLE salescall.task_next_actions
  ADD CONSTRAINT task_next_actions_handover_calls CHECK (NOT handover OR (kind = 'call' AND salescall.sales_purpose(purpose)));

-- ═════════ (7) 写しの元のいちばん元の版（版の元をたどる1か所から） ═════════
-- 版の元を近い順にたどり、その版から写しだけが続くところまで（写しでない元が1つでも挟まれば、そこで止まる）のいちばん遠い版
CREATE OR REPLACE FUNCTION salescall.version_copy_root(p_version uuid) RETURNS uuid LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT coalesce((SELECT o.version_id FROM (SELECT x.version_id, x.depth, bool_and(x.copied) OVER (ORDER BY x.depth) AS all_copied
                                             FROM salescall.version_origins(p_version) x) o
                   WHERE o.all_copied ORDER BY o.depth DESC LIMIT 1), p_version)
$$;

-- ═════════ (8)(9) 実施回の版の割り当て・手で外した印 ═════════
-- その版の、その問いの名前の答え（業務の答え、無ければ「使わない」にしていない標準の答え。業務設定の重ね方＝core/business_settings._with_standard
-- と同じ）。依頼者の解決策が、その版の流れに入っているか（solution_outside）の1か所が読む
CREATE FUNCTION salescall.version_answer(p_version uuid, p_name text) RETURNS text LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT coalesce(
    (SELECT a ->> 'text' FROM salescall.business_contents c
       CROSS JOIN LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(c.content -> 'answers') = 'array' THEN c.content -> 'answers'
                                                    ELSE '[]'::jsonb END) a
     WHERE c.version_id = p_version AND a ->> 'name' = p_name LIMIT 1),
    (SELECT a ->> 'text' FROM salescall.business_versions v JOIN salescall.standard_versions s ON s.standard_id = v.standard_id
       LEFT JOIN salescall.business_contents c ON c.version_id = v.version_id
       CROSS JOIN LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(s.content -> 'answers') = 'array' THEN s.content -> 'answers'
                                                    ELSE '[]'::jsonb END) a
     WHERE v.version_id = p_version AND a ->> 'name' = p_name
       AND NOT (CASE WHEN jsonb_typeof(c.content -> 'answers_off') = 'array' THEN c.content -> 'answers_off' ELSE '[]'::jsonb END) ? p_name
     LIMIT 1))
$$;
REVOKE ALL ON FUNCTION salescall.version_answer(uuid, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.version_answer(uuid, text) TO salescall_app;

-- その答え（問いの名前・文）が、その版の流れに入っていないか（流れのいちばん後の版＝version_tip の答えが、その文と違う・無い）。入っているかの
-- 1か所（core/client_solutions._outside も、これを読む）
CREATE FUNCTION salescall.solution_outside(p_version uuid, p_name text, p_text text) RETURNS boolean LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT salescall.version_answer(salescall.version_tip(p_version), p_name) IS DISTINCT FROM p_text
$$;
REVOKE ALL ON FUNCTION salescall.solution_outside(uuid, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.solution_outside(uuid, text, text) TO salescall_app;

-- その業務に、依頼者の解決策で足せた問答（問いの名前ごとに、いちばん新しく足せたもの。取り込み直した中身で置き換えられたものは入れない）。
-- core/client_solutions._latest の1か所（前は Python の問い合わせ。手で外した印を付ける所が同じものを読むため、ここへ移した）
CREATE FUNCTION salescall.business_solutions(p_business uuid)
RETURNS TABLE (solution_id uuid, attempt_id uuid, answer_name text, solution_text text, point_text text, recorded_at timestamptz)
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT l.solution_id, l.attempt_id, l.answer_name, l.solution_text, l.point_text, l.recorded_at FROM (
    SELECT DISTINCT ON (c.answer_name) c.solution_id, c.attempt_id, c.answer_name, c.solution_text, c.point_text, c.recorded_at
    FROM salescall.client_solutions c JOIN salescall.tasks t ON t.task_id = c.task_id JOIN salescall.runs r ON r.run_id = t.run_id
    WHERE r.business_id = p_business AND EXISTS (SELECT 1 FROM salescall.client_solution_lines l
                                                 WHERE l.solution_id = c.solution_id AND l.version_id IS NOT NULL)
    ORDER BY c.answer_name, c.seq DESC) l   -- 足した順（データベースが振る＝0192）
  WHERE NOT EXISTS (SELECT 1 FROM salescall.client_solution_replacements x WHERE x.solution_id = l.solution_id)
  ORDER BY l.answer_name
$$;
REVOKE ALL ON FUNCTION salescall.business_solutions(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.business_solutions(uuid) TO salescall_app;

-- 実施回に版を手で割り当てたとき「手で外した」とする解決策（1か所。✅ 2026-11-07 ユーザー＝計画書 6-1 (6)）：その業務のいまの解決策のうち、
-- 割り当てた版（p_after）の流れに入っていないもの。ただし前の版（p_before）の流れにも入っていなかったものは、新しく外したことにしない。
-- 前と同じ版なら無い。実施回を作るとき（前の版が無い）は、入っていないものすべて
CREATE FUNCTION salescall.hand_removals(p_before uuid, p_after uuid) RETURNS SETOF uuid LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT s.solution_id
  FROM salescall.business_solutions((SELECT b.business_id FROM salescall.business_versions b WHERE b.version_id = p_after)) s
  WHERE p_before IS DISTINCT FROM p_after
    AND salescall.solution_outside(p_after, s.answer_name, s.solution_text)
    AND (p_before IS NULL OR NOT salescall.solution_outside(p_before, s.answer_name, s.solution_text))
$$;
REVOKE ALL ON FUNCTION salescall.hand_removals(uuid, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.hand_removals(uuid, uuid) TO salescall_app;

-- 手の割り当ては、つながりの先の版だけ（途中の版は、core/business_settings.assign が写しを作って割り当てる）。0189 の形に足した
CREATE OR REPLACE FUNCTION salescall.run_version_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.assigned_at := now();
  IF NEW.auto AND salescall.cur_actor_kind() IS DISTINCT FROM 'system' THEN
    RAISE EXCEPTION '自動の割り当ては、システムの処理だけです' USING ERRCODE = 'insufficient_privilege', CONSTRAINT = 'run_version_auto_by_system';
  END IF;
  IF NOT NEW.auto AND salescall.version_tip(NEW.version_id) IS DISTINCT FROM NEW.version_id THEN
    RAISE EXCEPTION '手で割り当てられるのは、つながりの先の版だけです（途中の版は、写しを作って割り当てます）' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'run_version_manual_tip';
  END IF;
  RETURN NEW;
END $$;

-- 手で外した印は、割り当てと同じ取引で、データベースが付ける（どの道で割り当てても。⛔ 前は core/client_solutions.removed_by_hand が付け、
-- 契約者の取引が run_versions へ直接書くと付かなかった）。前の版＝その実施回の、この割り当ての前の割り当て（無ければ実施回を作るとき）
CREATE FUNCTION salescall.run_version_removals() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO salescall.client_solution_removals (tenant_id, client_id, solution_id, version_id, run_id, recorded_by)
  SELECT NEW.tenant_id, NEW.client_id, h, NEW.version_id, NEW.run_id, salescall.cur_actor()
  FROM salescall.hand_removals((SELECT v.version_id FROM salescall.run_versions v WHERE v.run_id = NEW.run_id AND v.seq < NEW.seq
                                ORDER BY v.seq DESC LIMIT 1), NEW.version_id) h;
  RETURN NULL;
END $$;
CREATE TRIGGER run_version_removals AFTER INSERT ON salescall.run_versions
  FOR EACH ROW WHEN (NOT NEW.auto) EXECUTE FUNCTION salescall.run_version_removals();

-- ═════════ (4) の続き：「管理者か契約者か」の決まりを持つ関数を、cur_is_person を読む形に作り直す ═════════
-- どれも、その関数のいちばん新しい形（0200 までの表の作りの、最後に作った形）を元にし、決まりの所だけを置き換えた。持ち主の権限で動く形
-- （あとで ALTER FUNCTION で足したもの）は、作り直すと外れるので、同じ形に戻す。ここより上で作り直したもの：task_dial_fix_guard・
-- dnc_before_change・client_solution_line_guard。⚠️ stop_emergency_pass_guard の元の形（NOT IN）は、操作している方の種類の無い取引を
-- 断らなかった（NULL）。cur_is_person は断る側（より守る側）

-- salescall.apology_approval_by_person（0043_plan_cross_check_2.sql の形から）
CREATE OR REPLACE FUNCTION salescall.apology_approval_by_person() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT salescall.cur_is_person()
     OR NEW.approved_by IS DISTINCT FROM salescall.cur_actor() THEN
    RAISE EXCEPTION 'お詫びの電話を承認するのは、管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'apology_approval_by_person';
  END IF;
  RETURN NEW;
END $$;

-- salescall.client_solution_guard（0184_client_solutions.sql の形から）
CREATE OR REPLACE FUNCTION salescall.client_solution_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT salescall.cur_is_person() THEN
    RAISE EXCEPTION '依頼者の解決策を足せるのは、管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'client_solution_by_person';
  END IF;
  IF NEW.recorded_by IS DISTINCT FROM salescall.cur_actor() THEN
    RAISE EXCEPTION '記録した方は、いま操作している方です' USING ERRCODE = 'check_violation', CONSTRAINT = 'client_solution_recorded_by';
  END IF;
  RETURN NEW;
END $$;

-- salescall.client_solution_leave_out_guard（0200_solution_leave_out.sql の形から）
CREATE OR REPLACE FUNCTION salescall.client_solution_leave_out_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.recorded_at := now();
  IF NEW.recorded_by IS DISTINCT FROM salescall.cur_actor()
     OR (NOT salescall.cur_is_person()) THEN
    RAISE EXCEPTION '手で外した解決策を「入れない」と確かめられるのは、いま操作している管理者か契約者だけです'
      USING ERRCODE = 'insufficient_privilege', CONSTRAINT = 'client_solution_leave_out_by_person';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM salescall.client_solution_removals d WHERE d.solution_id = NEW.solution_id AND d.run_id = NEW.run_id) THEN
    RAISE EXCEPTION '「入れない」と確かめられるのは、その実施回で手で外した解決策だけです' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'client_solution_leave_out_removed';
  END IF;
  RETURN NEW;
END $$;

-- salescall.client_state_by_person（0051_registry.sql の形から）
CREATE OR REPLACE FUNCTION salescall.client_state_by_person() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT salescall.cur_is_person()
     OR NEW.set_by IS DISTINCT FROM salescall.cur_actor() THEN
    RAISE EXCEPTION '依頼者を有効・無効にできるのは、管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN NEW;
END $$;

-- salescall.contractor_dnc_guard（0065_dnc_list_import.sql の形から）
CREATE OR REPLACE FUNCTION salescall.contractor_dnc_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION '発信禁止の登録は消せません（取り消しは記録つきで）' USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  IF TG_OP = 'UPDATE' AND (OLD.revoked_at IS NOT NULL
     OR (NEW.tenant_id, NEW.entry_id, NEW.phone_e164, NEW.reason, NEW.registered_at, NEW.registered_by, NEW.import_id)
        IS DISTINCT FROM (OLD.tenant_id, OLD.entry_id, OLD.phone_e164, OLD.reason, OLD.registered_at, OLD.registered_by,
                          OLD.import_id)) THEN
    RAISE EXCEPTION '発信禁止の登録は、取り消すこと（1回）しかできません' USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  IF NOT salescall.cur_is_person() THEN
    RAISE EXCEPTION '契約者の共通の発信禁止を登録・取り消しできるのは、管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'contractor_dnc_by_person';
  END IF;
  RETURN NEW;
END $$;

-- salescall.decision_answer_guard（0161_review26b_decisions_releases_waits.sql の形から）
CREATE OR REPLACE FUNCTION salescall.decision_answer_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.answered_by IS DISTINCT FROM salescall.cur_actor() THEN
    RAISE EXCEPTION '答えた者は、取引の管理者か契約者です' USING ERRCODE = 'check_violation', CONSTRAINT = 'decision_answer_guard';
  END IF;
  IF NEW.by_kind = 'recording' THEN
    RAISE EXCEPTION '録音の確かめは、答えでなく、締め直しが保留の版を決め直して残します（0154）' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'decision_answer_guard';
  END IF;
  IF NOT salescall.cur_is_person() THEN
    RAISE EXCEPTION '取り消せない決定に答えられる（承認・差し戻し・取り消し）のは、管理者か契約者だけです'
      USING ERRCODE = 'insufficient_privilege', CONSTRAINT = 'decision_answer_guard';
  END IF;
  IF NEW.by_kind = 'approval' AND NOT (
       salescall.decision_asks_contractor(NEW.attempt_id)
       AND NEW.wait_seq = salescall.decision_wait_seq(NEW.attempt_id)
       AND NEW.parts <@ salescall.decision_open(NEW.attempt_id) AND salescall.decision_open(NEW.attempt_id) <@ NEW.parts) THEN
    RAISE EXCEPTION 'その通話の、いまの保留（承認か差し戻しを求めている版の中身）に答えてください' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'decision_answer_guard';
  END IF;
  IF NEW.by_kind = 'revoke' AND NOT (cardinality(NEW.parts) > 0 AND NEW.parts <@ salescall.revoked_parts(NEW.attempt_id)) THEN
    RAISE EXCEPTION '取り消しの答えは、その通話から入れた発信禁止をすべて取り消したときの、その登録の中身だけです'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'decision_answer_guard';
  END IF;
  RETURN NEW;
END $$;

-- salescall.deficit_warning_guard（0118_review16_counts_failed_numbers_and_deficit.sql の形から）
CREATE OR REPLACE FUNCTION salescall.deficit_warning_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION '赤字の警告は消せません' USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  IF (NEW.tenant_id, NEW.client_id, NEW.warning_id, NEW.warned_at, NEW.deadline, NEW.figures)
     IS DISTINCT FROM (OLD.tenant_id, OLD.client_id, OLD.warning_id, OLD.warned_at, OLD.deadline, OLD.figures)
     OR (OLD.confirmed_at IS NOT NULL AND (NEW.confirmed_at, NEW.confirmed_by) IS DISTINCT FROM (OLD.confirmed_at, OLD.confirmed_by))
     OR (OLD.closed_at IS NOT NULL AND (NEW.closed_at, NEW.closed_why) IS DISTINCT FROM (OLD.closed_at, OLD.closed_why))
     OR (OLD.reminded_at IS NOT NULL AND NEW.reminded_at IS DISTINCT FROM OLD.reminded_at) THEN
    RAISE EXCEPTION '赤字の警告は、確認（1回）・もう一度の知らせ（1回）・閉じる（1回）しかできません'
      USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  IF OLD.confirmed_at IS NULL AND NEW.confirmed_at IS NOT NULL
     AND NOT salescall.cur_is_person() THEN
    RAISE EXCEPTION '赤字の警告を確認できるのは、管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'deficit_confirmed_by_person';
  END IF;
  RETURN NEW;
END $$;

-- salescall.dnc_import_by_person（0065_dnc_list_import.sql の形から）
CREATE OR REPLACE FUNCTION salescall.dnc_import_by_person() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT salescall.cur_is_person() THEN
    RAISE EXCEPTION '発信禁止の一覧を取り込めるのは、管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'dnc_import_by_person';
  END IF;
  RETURN NEW;
END $$;

-- salescall.judgement_check_guard（0074_measure_per_call.sql の形から）
CREATE OR REPLACE FUNCTION salescall.judgement_check_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT salescall.cur_is_person() THEN
    RAISE EXCEPTION '正解を付けるのは、管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'judgement_check_by_person';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM salescall.task_results r WHERE r.task_id = NEW.task_id AND r.version_no = NEW.version_no
                 AND r.kind IN ('judged', 'rejudged') AND r.attempt_id = NEW.attempt_id)
     OR EXISTS (SELECT 1 FROM salescall.task_results r WHERE r.task_id = NEW.task_id AND r.attempt_id = NEW.attempt_id
                AND r.kind IN ('judged', 'rejudged') AND r.version_no > NEW.version_no) THEN
    RAISE EXCEPTION '正解を付けるのは、その通話の、システムの最新の判定の版だけです' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'judgement_check_of_system_version';
  END IF;
  IF NOT salescall.call_to_measure(NEW.attempt_id) THEN
    RAISE EXCEPTION '正解を付けるのは、お客様と話せた通話だけです' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'judgement_check_of_measured_call';
  END IF;
  NEW.source := CASE
    WHEN EXISTS (SELECT 1 FROM salescall.recording_holds h WHERE h.attempt_id = NEW.attempt_id AND h.reason = 'verification')
      THEN 'verification'
    WHEN EXISTS (SELECT 1 FROM salescall.sample_draw_calls d WHERE d.attempt_id = NEW.attempt_id) THEN 'sample'
    ELSE 'correction' END;
  RETURN NEW;
END $$;

-- salescall.line_before_update（0059_voice_types_clients_lines_review.sql の形から）
CREATE OR REPLACE FUNCTION salescall.line_before_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (NEW.tenant_id, NEW.line_id, NEW.method, NEW.caller_e164) IS DISTINCT FROM
     (OLD.tenant_id, OLD.line_id, OLD.method, OLD.caller_e164) THEN
    RAISE EXCEPTION '回線の接続方式・発信番号は変えられません（番号を替えるときは新しい回線にする）' USING ERRCODE = 'check_violation';
  END IF;
  IF OLD.retired_at IS NOT NULL AND (NEW.retired_at, NEW.retired_reason, NEW.client_id)
                                    IS DISTINCT FROM (OLD.retired_at, OLD.retired_reason, OLD.client_id) THEN
    RAISE EXCEPTION '使わなくなった回線は、名前のほかは変えられません（使わなくなった印も外せません）' USING ERRCODE = 'check_violation';
  END IF;
  IF (NEW.name IS DISTINCT FROM OLD.name OR NEW.client_id IS DISTINCT FROM OLD.client_id
      OR NEW.retired_at IS DISTINCT FROM OLD.retired_at)
     AND NOT salescall.cur_is_person() THEN
    RAISE EXCEPTION '回線の名前・依頼者の割り当て・使わなくなった印を変えられるのは、管理者か契約者だけです'
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF NEW.name IS DISTINCT FROM OLD.name THEN
    INSERT INTO salescall.name_changes (tenant_id, client_id, kind, target_id, old_name, new_name, changed_by)
    VALUES (NEW.tenant_id, NULL, 'line', NEW.line_id, OLD.name, NEW.name, salescall.cur_actor());
  END IF;
  RETURN NEW;
END $$;
ALTER FUNCTION salescall.line_before_update() SECURITY DEFINER SET search_path = salescall, pg_temp;

-- salescall.only_name_changes（0051_registry.sql の形から）
CREATE OR REPLACE FUNCTION salescall.only_name_changes() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE kind text := CASE TG_TABLE_NAME WHEN 'clients' THEN 'client' WHEN 'businesses' THEN 'business' ELSE 'run' END;
BEGIN
  IF (to_jsonb(NEW) - 'name') IS DISTINCT FROM (to_jsonb(OLD) - 'name') THEN
    RAISE EXCEPTION '直せるのは名前だけです' USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.name IS DISTINCT FROM OLD.name THEN
    IF NOT salescall.cur_is_person() THEN
      RAISE EXCEPTION '名前を直せるのは、管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege';
    END IF;
    INSERT INTO salescall.name_changes (tenant_id, client_id, kind, target_id, old_name, new_name, changed_by)
    VALUES (NEW.tenant_id, NEW.client_id, kind,
            CASE kind WHEN 'client' THEN NEW.client_id WHEN 'business' THEN (to_jsonb(NEW) ->> 'business_id')::uuid
                      ELSE (to_jsonb(NEW) ->> 'run_id')::uuid END,
            OLD.name, NEW.name, salescall.cur_actor());
  END IF;
  RETURN NEW;
END $$;
ALTER FUNCTION salescall.only_name_changes() SECURITY DEFINER SET search_path = salescall, pg_temp;

-- salescall.recording_approval_by_person（0109_review_1022.sql の形から）
CREATE OR REPLACE FUNCTION salescall.recording_approval_by_person() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT salescall.cur_is_person()
     OR NEW.approved_by IS DISTINCT FROM salescall.cur_actor() THEN
    RAISE EXCEPTION '録音を承認できるのは、管理者か契約者だけです（承認した者は、取引の管理者か契約者）'
      USING ERRCODE = 'insufficient_privilege', CONSTRAINT = 'recording_approval_by_person';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM salescall.recording_sets s
                 WHERE s.set_id = NEW.set_id AND s.client_id IS NOT DISTINCT FROM NEW.client_id) THEN
    RAISE EXCEPTION '録音の承認は、取引から見える組に、組と同じ依頼者でします' USING ERRCODE = 'check_violation';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM salescall.recordings WHERE set_id = NEW.set_id) THEN
    RAISE EXCEPTION '部分の無い録音の組は承認できません' USING ERRCODE = 'check_violation';
  END IF;
  IF EXISTS (SELECT 1 FROM salescall.recording_sets s JOIN salescall.voices v USING (voice_name)
             WHERE s.set_id = NEW.set_id AND v.retired_at IS NOT NULL) THEN
    RAISE EXCEPTION '使わない声の録音は承認できません' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;

-- salescall.result_release_guard（0161_review26b_decisions_releases_waits.sql の形から）
CREATE OR REPLACE FUNCTION salescall.result_release_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF salescall.release_blocked(NEW.task_id, NEW.version_no) THEN
    RAISE EXCEPTION 'その版には確定の印を付けられません（取り消せない決定がまだ決まっていない・あとの通話の結果がある・その通話の新しい版がある）'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'result_release_guard';
  END IF;
  IF NEW.stage IS DISTINCT FROM (CASE WHEN salescall.version_in_verification(NEW.task_id, NEW.version_no) THEN 'verification'
                                      ELSE 'production' END) THEN
    RAISE EXCEPTION '確定の印の段は、その版の通話をかけたときのモード（検証の段か本番か）と同じです'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'result_release_guard';
  END IF;
  IF NEW.stage = 'verification' AND NOT salescall.cur_is_person() THEN
    RAISE EXCEPTION '検証の段の結果を確定するのは、契約者か管理者の承認だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'result_release_guard';
  END IF;
  RETURN NEW;
END $$;

-- salescall.stop_emergency_pass_guard（0159_review25_one_place_for_ai_stops_and_kept_calls.sql の形から）
CREATE OR REPLACE FUNCTION salescall.stop_emergency_pass_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT salescall.cur_is_person() OR NEW.passed_by IS DISTINCT FROM salescall.cur_actor()
     OR NEW.actor_kind::text IS DISTINCT FROM salescall.cur_actor_kind()::text THEN
    RAISE EXCEPTION '緊急の道で再開できるのは、契約者か管理者だけです（出した人は取引の人）' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'stop_emergency_pass_guard';
  END IF;
  IF NEW.tenant_id IS DISTINCT FROM salescall.cur_tenant() OR NOT salescall.emergency_stop(NEW.stop_event_id) THEN
    RAISE EXCEPTION '緊急の道で再開できるのは、まだ解かれていない、システムが止めた、その契約者の AIの不具合の停止だけです'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'stop_emergency_pass_guard';
  END IF;
  RETURN NEW;
END $$;

-- salescall.system_values_by_admin（0190_sub_promise_and_plan_by_admin.sql の形から）
CREATE OR REPLACE FUNCTION salescall.system_values_by_admin() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF current_user <> 'salescall_owner' AND salescall.cur_actor_kind() IS DISTINCT FROM 'admin'
     AND ((TG_TABLE_NAME = 'price_versions' AND to_jsonb(NEW) ->> 'tenant_id' IS NULL)
          OR to_jsonb(NEW) ->> 'scope' = 'system') THEN
    RAISE EXCEPTION 'システム全体の値（すべての契約者に効く初めの値を含む）を変えられるのは、管理者だけです'
      USING ERRCODE = 'insufficient_privilege', CONSTRAINT = 'system_values_by_admin';
  END IF;
  -- 管理者だけが書ける設定（契約プランなど。どの範囲の値でも）
  IF current_user <> 'salescall_owner' AND TG_TABLE_NAME = 'setting_values'
     AND salescall.cur_actor_kind() IS DISTINCT FROM 'admin'
     AND EXISTS (SELECT 1 FROM salescall.setting_definitions d WHERE d.key = to_jsonb(NEW) ->> 'key' AND d.admin_writes) THEN
    RAISE EXCEPTION 'この設定（%）を変えられるのは、管理者だけです', to_jsonb(NEW) ->> 'key'
      USING ERRCODE = 'insufficient_privilege', CONSTRAINT = 'setting_admin_writes';
  END IF;
  IF current_user <> 'salescall_owner' AND TG_TABLE_NAME = 'price_versions'
     AND NOT salescall.cur_is_person() THEN
    RAISE EXCEPTION '単価を入れられるのは、管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'system_values_by_admin';
  END IF;
  RETURN NEW;
END $$;

-- salescall.task_approval_by_operator（0043_plan_cross_check_2.sql の形から）
CREATE OR REPLACE FUNCTION salescall.task_approval_by_operator() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT salescall.cur_is_person()
     OR NEW.approved_by IS DISTINCT FROM salescall.cur_actor() THEN
    RAISE EXCEPTION '行を承認できるのは管理者か契約者だけです（承認した者は、取引の管理者か契約者）' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'task_approvals_by_operator';
  END IF;
  RETURN NEW;
END $$;

-- salescall.task_contact_guard（0175_customer_contacts.sql の形から）
CREATE OR REPLACE FUNCTION salescall.task_contact_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.origin = 'dashboard' THEN
    IF NOT salescall.cur_is_person() THEN
      RAISE EXCEPTION 'ダッシュボードでお客様の連絡先を直せるのは、管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege',
        CONSTRAINT = 'customer_contact_by_person';
    END IF;
    IF NEW.recorded_by IS DISTINCT FROM salescall.cur_actor() THEN
      RAISE EXCEPTION '記録した方は、いま操作している方です' USING ERRCODE = 'check_violation',
        CONSTRAINT = 'customer_contact_recorded_by';
    END IF;
  END IF;
  RETURN NEW;
END $$;

-- salescall.task_exclusion_guard（0071_task_exclusions.sql の形から）
CREATE OR REPLACE FUNCTION salescall.task_exclusion_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.source = 'dashboard' AND NOT salescall.cur_is_person() THEN
    RAISE EXCEPTION 'ダッシュボードで「かけない」を付け外しできるのは、管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'task_exclusion_by_person';
  END IF;
  RETURN NEW;
END $$;

-- salescall.task_fix_guard（0178_task_fixes.sql の形から）
CREATE OR REPLACE FUNCTION salescall.task_fix_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT salescall.cur_is_person() THEN
    RAISE EXCEPTION 'お客様リストの行を直せるのは、管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'task_fix_by_person';
  END IF;
  IF NEW.recorded_by IS DISTINCT FROM salescall.cur_actor() THEN
    RAISE EXCEPTION '記録した方は、いま操作している方です' USING ERRCODE = 'check_violation', CONSTRAINT = 'task_fix_recorded_by';
  END IF;
  RETURN NEW;
END $$;

-- salescall.task_number_record_guard（0175_customer_contacts.sql の形から）
CREATE OR REPLACE FUNCTION salescall.task_number_record_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.record_origin = 'dashboard' THEN
    IF NOT salescall.cur_is_person() THEN
      RAISE EXCEPTION 'ダッシュボードでお客様の番号を足せるのは、管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege',
        CONSTRAINT = 'customer_contact_by_person';
    END IF;
    IF NEW.recorded_by IS DISTINCT FROM salescall.cur_actor() THEN
      RAISE EXCEPTION '記録した方は、いま操作している方です' USING ERRCODE = 'check_violation',
        CONSTRAINT = 'customer_contact_recorded_by';
    END IF;
  END IF;
  RETURN NEW;
END $$;

-- salescall.task_trouble_restore_guard（0146_health_states_read.sql の形から）
CREATE OR REPLACE FUNCTION salescall.task_trouble_restore_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT salescall.cur_is_person() THEN
    RAISE EXCEPTION '休んだ行を戻せるのは、管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'task_trouble_restore_by_person';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM salescall.task_states s WHERE s.task_id = NEW.task_id AND s.stage IN ('resting', 'await_recovery')) THEN
    RAISE EXCEPTION 'その行は、こちらのプログラムの不具合で休んでいません（復旧も待っていません）' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'task_trouble_restore_only_resting';
  END IF;
  NEW.restored_by := salescall.cur_actor();
  NEW.actor_kind := salescall.cur_actor_kind()::salescall.actor_kind;
  NEW.restored_at := clock_timestamp();
  RETURN NEW;
END $$;

-- salescall.version_name_reading_by_person（0101_review7_fixes.sql の形から）
CREATE OR REPLACE FUNCTION salescall.version_name_reading_by_person() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.reading_by <> 'recording' THEN   -- 版の中身に書いた読み（版の中身を置いたときだけ。下の business_content_readings）
    RETURN NEW;
  END IF;
  IF NOT salescall.cur_is_person()
     OR NEW.chosen_by IS DISTINCT FROM salescall.cur_actor() THEN
    RAISE EXCEPTION '名前の読みを録音で選べるのは、管理者か契約者だけです（録音を聞いて選ぶ）'
      USING ERRCODE = 'insufficient_privilege', CONSTRAINT = 'version_name_reading_by_person';
  END IF;
  IF EXISTS (SELECT 1 FROM salescall.version_name_readings g WHERE g.version_id = NEW.version_id AND g.name_text = NEW.name_text
             AND g.reading_by = 'given') THEN
    RAISE EXCEPTION '名前「%」の読みは入口で入っています（録音では選びません）', NEW.name_text
      USING ERRCODE = 'check_violation', CONSTRAINT = 'version_name_reading_given';
  END IF;
  RETURN NEW;
END $$;
