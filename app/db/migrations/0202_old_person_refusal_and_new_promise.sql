-- 0202 前の担当の方の断り・新しい約束（2026-11-07。書いた人：CL-2。DESIGN.md 3-4-1 の【0202】・3-6・15-2 の590〜）
--
-- (1) ✅ 2026-11-07 ユーザー（計画書 7-1 (22)。伺い「前の方の断り」）：番号を直すとき「担当の方は違う」を選び「約束も守る」を選んで、前の番号で
--     前の担当の方が「もうかけないで」と断ったら「鈴木様へはかける」（断ったのは前の担当の方なので、止めるのは前の担当の方の番号。新しい
--     担当の方へは引き継ぎを確かめる電話をかける）。
--     ⇒ 「その通話は、『違う』を選んだ直しのあとの、前の担当の方への通話か」をここの1か所（former_person_call）にする。決め方は「かけた番号が、
--     いま（締めるとき）前の担当の方の番号か」（task_former_number）：基準は、その時刻までに「違う」を選んだ、いちばん新しい直し（そのあとに
--     「同じ」で直しても、担当の方は替わったまま）。前の担当の方の番号＝その直しより前から行にあった番号（直す前のかける番号・直す前に前の担当の
--     方が言った番号・記録だけの番号）と、前の担当の方の番号への通話で言われた番号。その直しとそのあとの直しの番号（新しい担当の方の番号）は
--     入らない。かけた時刻ではなく締めるときで見る：直す前に予約して直したあとにつながった通話も、電話口は前の番号の前の担当の方（2026-11-07
--     本体の指示 (3)）。発信禁止の止める範囲・待たせる範囲（core/dnc.refusal_reach・undecided_reach）・行を終えるか（core/next_action.answer_ends_row）・
--     サブにするか（call_makes_sub）・番号が使えないときの代わり（core/next_action.numbers_of）が、どれもこれを読む。
-- (2) ✅ 2026-11-07 ユーザー（計画書 7-1 (23)。伺い「前の方の新約束」）：「約束も守る」で前の番号へかけ、前の担当の方が新しいかけ直しの約束を
--     したら「契約者に知らせて選んでもらう」（前の担当の方との約束を守るか、新しい担当の方へかけるかを、番号を直すときと同じ選びで契約者が
--     選ぶ。選ぶまで、その行はかけない）。
--     ⇒ 選びの記録 task_old_promise_choices（番号を直すときの選び task_dial_fixes.keep_old_promise と同じ意味の欄）。選びを待っているかは
--     task_old_promise_waiting の1か所（門・候補を出す処理・選びの守りが読む）。二つの選びは見方 old_promise_choices で1つに読む。
-- (3) 守ると選んだ前の担当の方の新しい約束は、まだかけていない約束（task_promise_tried が、いちばん新しい「守る」の選びのあとから数える。
--     選びを待っているあいだも、まだかけていない約束）。そのあいだに番号を直せば、もう一度「守るか」を選ぶ（0201 の task_dial_fix_guard）。

-- ═════════ (1) 前の担当の方の番号・前の担当の方への通話（1か所） ═════════
CREATE FUNCTION salescall.task_former_number(p_task uuid, p_contact uuid, p_at timestamptz) RETURNS boolean LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT coalesce((
    SELECT NOT EXISTS (SELECT 1 FROM salescall.task_dial_fixes g                                       -- その直しとあとの直しの番号でない
                       WHERE g.task_id = p_task AND g.seq >= f.seq AND g.contact_id = p_contact)
       AND EXISTS (SELECT 1 FROM salescall.task_numbers n WHERE n.task_id = p_task AND n.contact_id = p_contact
                     AND (n.added_at < f.recorded_at                                                   -- 直す前から行にあった番号
                          OR EXISTS (SELECT 1 FROM salescall.attempts s                                -- 前の担当の方の番号への通話で言われた番号
                                     JOIN salescall.task_numbers o ON o.task_id = s.task_id AND o.contact_id = s.contact_id
                                     WHERE s.attempt_id = n.source_attempt_id AND o.added_at < f.recorded_at
                                       AND NOT EXISTS (SELECT 1 FROM salescall.task_dial_fixes h
                                                       WHERE h.task_id = p_task AND h.seq >= f.seq AND h.contact_id = s.contact_id))))
    FROM (SELECT d.seq, d.recorded_at FROM salescall.task_dial_fixes d                                -- その時刻までに「違う」を選んだ、いちばん新しい直し
          WHERE d.task_id = p_task AND NOT d.same_person AND d.recorded_at <= p_at ORDER BY d.seq DESC LIMIT 1) f), false)
$$;
REVOKE ALL ON FUNCTION salescall.task_former_number(uuid, uuid, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.task_former_number(uuid, uuid, timestamptz) TO salescall_app;

-- その通話は、「違う」を選んだ直しのあとの、前の担当の方への通話か（かけた番号が、いま前の担当の方の番号か。締めるときの立場で見る）
CREATE FUNCTION salescall.former_person_call(p_attempt uuid) RETURNS boolean LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT coalesce((SELECT salescall.task_former_number(a.task_id, a.contact_id, now())
                   FROM salescall.attempts a WHERE a.attempt_id = p_attempt AND a.contact_id IS NOT NULL), false)
$$;
REVOKE ALL ON FUNCTION salescall.former_person_call(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.former_person_call(uuid) TO salescall_app;

-- サブにするのは、前の担当の方への通話でないときだけ（0201 の形の「その通話のときのいちばん新しい直しで、担当の方は同じ」を、前の担当の方への
-- 通話の1か所に替えた：「違う」のあとに「同じ」で直しても、前の担当の方の番号はサブにしない）
CREATE OR REPLACE FUNCTION salescall.call_makes_sub(p_attempt uuid) RETURNS boolean LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT coalesce((SELECT salescall.promise_call(a.purpose, a.promised)                         -- お客様と約束したかけ直しの電話
                          AND a.claimed_at >= salescall.task_count_from(a.task_id)              -- いちばん新しい直しのあとの通話
                          AND salescall.task_number_replaced_at(a.task_id, a.contact_id, a.claimed_at)   -- かけたとき前の番号
                          AND NOT salescall.former_person_call(a.attempt_id)                    -- 前の担当の方への通話でない
                   FROM salescall.attempts a WHERE a.attempt_id = p_attempt), false)
$$;

-- 番号を直すとき、前の担当の方との約束を守るかの選びが要るか（1か所。core/row_fixes.fix_dial_number と task_dial_fix_guard が読む）：まだ守って
-- いない約束が残っていて（task_promise_open）、その約束の電話の番号が、いま直す番号（新しい担当の方の番号）でない。約束の番号が直す番号なら、
-- 前の担当の方へかける道が無いので「守る」は選べない（2026-11-07 本体の決定＝伺い (a)。約束はやめ、新しい担当の方へ引き継ぎ＝_onto_dial）
CREATE FUNCTION salescall.task_old_promise_choice_needed(p_task uuid, p_phone text) RETURNS boolean LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT salescall.task_promise_open(p_task)
     AND NOT EXISTS (SELECT 1 FROM salescall.current_next_actions n JOIN salescall.task_numbers k
                       ON k.task_id = n.task_id AND k.contact_id = n.contact_id
                     WHERE n.task_id = p_task AND k.phone_e164 = p_phone)
$$;
REVOKE ALL ON FUNCTION salescall.task_old_promise_choice_needed(uuid, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.task_old_promise_choice_needed(uuid, text) TO salescall_app;

-- 0201 の形の選びの守りを、上の1か所に替えた
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
        NEW.task_id, (SELECT c.phone_e164 FROM salescall.contacts c WHERE c.contact_id = NEW.contact_id))) THEN
    RAISE EXCEPTION '前の担当の方との約束を守るか（keep_old_promise）は、「担当の方は違う」を選び、まだ守っていない約束が残っていて、その約束の番号が直す番号でないときだけ、必ず選びます'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'task_dial_fix_old_promise_choice';
  END IF;
  IF EXISTS (SELECT 1 FROM salescall.tasks me JOIN salescall.tasks o ON o.run_id = me.run_id AND o.task_id <> me.task_id
             WHERE me.task_id = NEW.task_id AND NEW.contact_id IN (SELECT salescall.task_dialable(o.task_id))) THEN
    RAISE EXCEPTION 'その番号は、同じ実施回のほかの行がかけうる番号です' USING ERRCODE = 'unique_violation',
      CONSTRAINT = 'task_dial_fix_one_row_per_number';
  END IF;
  RETURN NEW;
END $$;

-- ═════════ (2) 前の担当の方の新しい約束の選び ═════════
CREATE TABLE salescall.task_old_promise_choices (
  tenant_id        uuid NOT NULL,
  client_id        uuid NOT NULL,
  seq              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  task_id          uuid NOT NULL,
  attempt_id       uuid NOT NULL UNIQUE,     -- 前の担当の方が新しい約束をした通話（その通話ごとに1回選ぶ）
  keep_old_promise boolean NOT NULL,        -- 真＝前の担当の方との約束を守る・偽＝新しい担当の方へかける（task_dial_fixes.keep_old_promise と同じ意味）
  recorded_by      text NOT NULL CHECK (btrim(recorded_by) <> ''),
  recorded_at      timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, task_id) REFERENCES salescall.tasks (client_id, task_id),
  FOREIGN KEY (client_id, attempt_id, task_id) REFERENCES salescall.attempts (client_id, attempt_id, task_id)
);
CREATE INDEX task_old_promise_choices_task ON salescall.task_old_promise_choices (task_id, recorded_at DESC);
CREATE TRIGGER stamp_now BEFORE INSERT ON salescall.task_old_promise_choices
  FOR EACH ROW EXECUTE FUNCTION salescall.stamp_now('recorded_at');
CREATE TRIGGER task_old_promise_choices_frozen BEFORE UPDATE OR DELETE ON salescall.task_old_promise_choices
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
ALTER TABLE salescall.task_old_promise_choices ENABLE ROW LEVEL SECURITY;
CREATE POLICY client_scope ON salescall.task_old_promise_choices
  USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
  WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client());
GRANT SELECT, INSERT ON salescall.task_old_promise_choices TO salescall_app;

-- 前の担当の方との約束を守るかの選び（番号を直すときの選びと、前の担当の方が新しい約束をしたあとの選び）を1つに読む
CREATE VIEW salescall.old_promise_choices WITH (security_invoker = true) AS
  SELECT f.task_id, f.keep_old_promise, f.recorded_at FROM salescall.task_dial_fixes f WHERE f.keep_old_promise IS NOT NULL
  UNION ALL
  SELECT c.task_id, c.keep_old_promise, c.recorded_at FROM salescall.task_old_promise_choices c;
GRANT SELECT ON salescall.old_promise_choices TO salescall_app;

-- 契約者の選びを待っている、前の担当の方が新しい約束をした通話（無ければ NULL）：いまの予定が、前の担当の方の番号へのお客様と約束した
-- かけ直しで、その行のいちばん新しい前の担当の方への通話（お客様が出た・締めた）のあとに決めた予定で、その通話を締めたあとに、まだ選んでいない
-- （締める前の選び＝その通話の途中で番号を直したときの選びは、前の約束についての選び）
CREATE FUNCTION salescall.task_old_promise_waiting(p_task uuid) RETURNS uuid LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT c.attempt_id
  FROM salescall.current_next_actions n
  JOIN LATERAL (SELECT a.attempt_id, a.claimed_at,
                       (SELECT min(r.decided_at) FROM salescall.task_results r WHERE r.attempt_id = a.attempt_id) AS settled_at
                FROM salescall.attempts a
                WHERE a.task_id = p_task AND salescall.former_person_call(a.attempt_id)
                ORDER BY a.claimed_at DESC, a.attempt_id DESC LIMIT 1) c ON true
  WHERE n.task_id = p_task AND n.kind = 'call' AND salescall.promise_call(n.purpose, n.promised)
    AND n.decided_at > c.claimed_at
    AND salescall.reached_answer(c.attempt_id)
    AND salescall.task_former_number(p_task, n.contact_id, now())
    AND c.settled_at IS NOT NULL
    AND NOT EXISTS (SELECT 1 FROM salescall.old_promise_choices k WHERE k.task_id = p_task AND k.recorded_at > c.settled_at)
$$;
REVOKE ALL ON FUNCTION salescall.task_old_promise_waiting(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.task_old_promise_waiting(uuid) TO salescall_app;

CREATE FUNCTION salescall.task_old_promise_choice_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT salescall.cur_is_person() THEN
    RAISE EXCEPTION '前の担当の方との約束を守るかを選べるのは、管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'task_old_promise_choice_by_person';
  END IF;
  IF NEW.recorded_by IS DISTINCT FROM salescall.cur_actor() THEN
    RAISE EXCEPTION '記録した方は、いま操作している方です' USING ERRCODE = 'check_violation', CONSTRAINT = 'task_old_promise_choice_recorded_by';
  END IF;
  IF NEW.attempt_id IS DISTINCT FROM salescall.task_old_promise_waiting(NEW.task_id) THEN
    RAISE EXCEPTION '選べるのは、前の担当の方が新しい約束をして、選びを待っている行の、その通話だけです' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'task_old_promise_choice_waiting';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER task_old_promise_choice_guard BEFORE INSERT ON salescall.task_old_promise_choices
  FOR EACH ROW EXECUTE FUNCTION salescall.task_old_promise_choice_guard();

-- ═════════ (3) まだかけていない約束（0201 の形に、「守る」の選びのあとから数えることと、選びを待っているあいだを足した） ═════════
CREATE OR REPLACE FUNCTION salescall.task_promise_tried(p_task uuid) RETURNS boolean LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT salescall.task_old_promise_waiting(p_task) IS NULL
     AND coalesce((SELECT EXISTS (SELECT 1 FROM salescall.attempts a WHERE a.task_id = p_task AND a.claimed_at >= s.since
                                    AND salescall.rang(a.state, a.cut_off, a.attempt_id))
                   FROM (SELECT greatest(p.since, (SELECT max(k.recorded_at) FROM salescall.old_promise_choices k
                                                   WHERE k.task_id = p_task AND k.keep_old_promise)) AS since
                         FROM (SELECT salescall.task_promise_since(p_task) AS since) p WHERE p.since IS NOT NULL) s), false)
$$;
