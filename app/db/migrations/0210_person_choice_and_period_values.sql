-- 0210 名前などの直しでも「担当の方は同じか・違うか」を契約者が選ぶ・その通話の担当の期のお呼びする方の値・期の置き直し・締めた時刻
--      （2026-11-08。書いた人：CL-2。DESIGN.md 3-4-1 の【0210】・2章 Q160〜・15-2 の690〜）
--
-- ✅ 2026-11-08 ユーザー＝計画書 7-1 (28)：お呼びする方の名前・役職・部署を直すとき、同じ方の誤字の直し（「斎藤様」を「齊藤様」に）もあるので
--    「画面で選ぶ」（番号の直しと同じく、契約者が「担当の方は同じか・違うか」を選ぶ。「同じ」なら予定も約束もそのまま。7-1 (19) の「違う相手」は、
--    契約者が「違う」を選んだとき）。
-- ⛔ 0208・0209 は、値が（そろえた形で）変われば、それだけでお呼びする方が替わった（担当の期が替わった）。空欄を埋める直し（受付に
--    「お名前だけではどの方か分からない」と言われてフルネーム・部署を埋める）でも期が替わり、同じお客様へ引き継ぎを確かめる電話がかかった。
-- ⛔ 第三者の見直し J：
--    J-1 会話が読むお呼びする方の値（business_settings.target_of・門の材料）は、いまの値（tasks_now）だった。前の担当の方への約束・お詫び・
--        番号の聞き直しの電話でも、会話は新しい担当の方の名前でお呼びしていた。
--    J-2 0203 の移行で埋めた予定・通話の期は 0203 の決まり（欄の直しの記録があれば替わり）のままで、0208・0209 の決まりと食い違った（読み
--        だけの直しの行で、ご本人の「もうかけないで」が前の担当の方の断りになり、ほかの番号が止まらなかった）。
--    J-3 締めた時刻（settled_at）に、判定待ち（保留）の結果の版も入った：保留のあいだに「違う」で直し、そのあと断りと決まると、いまの担当の方の
--        断りになり、新しい担当の方の番号まで止まった（7-1 (22) と食い違い）。
-- ⇒ (1) 直しの記録（task_fixes）に、契約者の選び same_person（番号の直しの task_dial_fixes.same_person＝0199 と同じ意味）を持つ。
--        値が本当に変わる直しか＝person_fix_differs（0209 の person_fix_changes の中身。空欄を埋める・消すも入る）。替わる直しか＝
--        person_fix_changes（値が本当に変わり、契約者が「違う」を選んだ）。値が変わらない・表記の揺れだけの直しは、選びを受けない（守り）。
--    (2) お呼びする方が替わった時刻の組を1か所（task_person_changes）にし、担当の期（task_person_since）と、その期の終わり（task_person_until）が
--        読む。その期のお呼びする方の値（task_person_values）＝その期の終わりの直前までに効いていた欄の値。行の値の決め方は1か所（task_values。
--        いまの値の見方 tasks_now も、これを読む）。会話・門・録音の名前の部分が読む期は task_call_period（予約した通話の期。予約の前は予定の期）。
--    (3) 通話の期は、予約するとき、いまの予定の期をデータベースが入れる（attempt_before_insert の1か所。⛔ 前は門の Python が予定から写した）。
--    (4) 締めた時刻は、行の結果が決まった最初の版（保留でない＝result_decided）か、お詫びの電話の結び。
--    (5) 「その通話は、その時刻にいまの期のものか」（attempt_in_period）・「締めたときの立場の時刻」（call_standing_at）を1か所にする。
--    (6) 記録してある予定・通話の期を、いまの決まりで「その期の始まりの時刻に効いていた期」に置き直す（repoint_person_periods。決まりを変える
--        移行は、置き直しも必ず行う）。

-- ═════════ (1) 名前などの直しの「担当の方は同じか・違うか」 ═════════
ALTER TABLE salescall.task_fixes ADD COLUMN same_person boolean;   -- 値が本当に変わる直しだけ（契約者の選び）。ほかは NULL

-- 値が本当に変わる直しか（0209 の person_fix_changes の中身。比べるのは表記をそろえた形）
CREATE FUNCTION salescall.person_fix_differs(p_task uuid, p_field text, p_value text, p_seq bigint) RETURNS boolean LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT salescall.person_fix_field(p_field)
     AND salescall.compare_form(p_value) IS DISTINCT FROM salescall.compare_form(salescall.task_field_before(p_task, p_field, p_seq))
$$;
REVOKE ALL ON FUNCTION salescall.person_fix_differs(uuid, text, text, bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.person_fix_differs(uuid, text, text, bigint) TO salescall_app;

-- 前に記録した直しは、選ぶ形が無かったので「違う」（前の動きのまま。0199 の番号の直しと同じ考え）
ALTER TABLE salescall.task_fixes DISABLE TRIGGER task_fixes_frozen;
UPDATE salescall.task_fixes f SET same_person = false WHERE salescall.person_fix_differs(f.task_id, f.field, f.value, f.seq);
ALTER TABLE salescall.task_fixes ENABLE TRIGGER task_fixes_frozen;

-- その直しの直前の値（0208 の形から、取り込んだ値を欄の名前で読む形にした＝直せる欄の組を書き写さない）
CREATE OR REPLACE FUNCTION salescall.task_field_before(p_task uuid, p_field text, p_seq bigint) RETURNS text LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT CASE WHEN p.seq IS NOT NULL THEN p.value ELSE to_jsonb(t) ->> p_field END
  FROM salescall.tasks t
  LEFT JOIN LATERAL (SELECT x.seq, x.value FROM salescall.task_fixes x
                     WHERE x.task_id = t.task_id AND x.field = p_field AND (p_seq IS NULL OR x.seq < p_seq)
                     ORDER BY x.seq DESC LIMIT 1) p ON true
  WHERE t.task_id = p_task
$$;

-- お呼びする方が替わる直しか（1か所）：値が本当に変わり、契約者が「違う」を選んだ
CREATE FUNCTION salescall.person_fix_changes(p_task uuid, p_field text, p_value text, p_seq bigint, p_same_person boolean) RETURNS boolean
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT p_same_person IS FALSE AND salescall.person_fix_differs(p_task, p_field, p_value, p_seq)
$$;
REVOKE ALL ON FUNCTION salescall.person_fix_changes(uuid, text, text, bigint, boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.person_fix_changes(uuid, text, text, bigint, boolean) TO salescall_app;

-- ═════════ (2) お呼びする方が替わった時刻・担当の期・その期の終わり ═════════
CREATE FUNCTION salescall.task_person_changes(p_task uuid) RETURNS SETOF timestamptz LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT d.recorded_at FROM salescall.task_dial_fixes d                         -- 「担当の方は違う」を選んだ番号の直し
  WHERE d.task_id = p_task AND NOT d.same_person
  UNION ALL
  SELECT f.recorded_at FROM salescall.task_fixes f                              -- 「担当の方は違う」を選んだ名前・役職・部署の中身の直し
  WHERE f.task_id = p_task AND salescall.person_fix_changes(f.task_id, f.field, f.value, f.seq, f.same_person)
$$;
REVOKE ALL ON FUNCTION salescall.task_person_changes(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.task_person_changes(uuid) TO salescall_app;

CREATE OR REPLACE FUNCTION salescall.task_person_since(p_task uuid, p_at timestamptz) RETURNS timestamptz LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT max(c.at) FROM salescall.task_person_changes(p_task) c(at) WHERE c.at <= p_at
$$;

-- その期（始まり p_since。NULL＝最初の担当の方）の終わり＝次にお呼びする方が替わった時刻（いまの期なら NULL）
CREATE FUNCTION salescall.task_person_until(p_task uuid, p_since timestamptz) RETURNS timestamptz LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT min(c.at) FROM salescall.task_person_changes(p_task) c(at) WHERE c.at > coalesce(p_since, '-infinity')
$$;
REVOKE ALL ON FUNCTION salescall.task_person_until(uuid, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.task_person_until(uuid, timestamptz) TO salescall_app;

-- 4つ引数の person_fix_changes を読む所は、上で作り直した（task_person_since）と下（task_fix_guard）だけ
DROP FUNCTION salescall.person_fix_changes(uuid, text, text, bigint);

-- 直しの守り（0208 の形に、担当の方は同じか・違うかの選びを足した）
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
        NEW.task_id, (SELECT c.phone_e164 FROM salescall.contacts c WHERE c.contact_id = salescall.task_dial_contact(NEW.task_id)))) THEN
    RAISE EXCEPTION '前の担当の方との約束を守るか（keep_old_promise）は、お呼びする方の名前・フルネーム・役職・部署を「担当の方は違う」で直し、約束が残っていて、その約束の番号がいまかける番号でないときだけ、必ず選びます'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'task_fix_old_promise_choice';
  END IF;
  RETURN NEW;
END $$;

-- ═════════ (2) 行の値（1か所）と、その期のお呼びする方の値 ═════════
-- p_before＝その時刻より前の直しだけを当てる（NULL＝すべての直し＝いまの値）。欄と型は 0178 の見方 tasks_now と同じ
CREATE FUNCTION salescall.task_values(p_task uuid, p_before timestamptz)
RETURNS TABLE (tenant_id uuid, client_id uuid, task_id uuid, run_id uuid, contact_id uuid, client_ref text, source_phone text, source_row integer,
               created_at timestamptz, import_id uuid, list_label text,
               source_name text, source_name_reading text, source_name_reading_by text, source_name_reading_candidates text[],
               source_name_reading_likely text, source_name_fixed_at timestamptz,
               target_name text, target_name_reading text, target_name_reading_by text, target_name_reading_candidates text[],
               target_name_reading_likely text, target_name_fixed_at timestamptz,
               target_full_name text, target_full_name_reading text, target_full_name_reading_by text,
               target_full_name_reading_candidates text[], target_full_name_reading_likely text,
               target_role text, target_department text)
LANGUAGE sql STABLE AS $$
SELECT t.tenant_id, t.client_id, t.task_id, t.run_id, t.contact_id, t.client_ref, t.source_phone, t.source_row, t.created_at,
       t.import_id, t.list_label,
       CASE WHEN s.seq IS NULL THEN t.source_name ELSE s.value END,
       CASE WHEN s.seq IS NULL THEN t.source_name_reading ELSE s.reading END,
       CASE WHEN s.seq IS NULL THEN t.source_name_reading_by ELSE s.reading_by END,
       CASE WHEN s.seq IS NULL THEN t.source_name_reading_candidates ELSE s.candidates END,
       CASE WHEN s.seq IS NULL THEN t.source_name_reading_likely ELSE s.likely END,
       s.recorded_at,
       CASE WHEN n.seq IS NULL THEN t.target_name ELSE n.value END,
       CASE WHEN n.seq IS NULL THEN t.target_name_reading ELSE n.reading END,
       CASE WHEN n.seq IS NULL THEN t.target_name_reading_by ELSE n.reading_by END,
       CASE WHEN n.seq IS NULL THEN t.target_name_reading_candidates ELSE n.candidates END,
       CASE WHEN n.seq IS NULL THEN t.target_name_reading_likely ELSE n.likely END,
       n.recorded_at,
       CASE WHEN f.seq IS NULL THEN t.target_full_name ELSE f.value END,
       CASE WHEN f.seq IS NULL THEN t.target_full_name_reading ELSE f.reading END,
       CASE WHEN f.seq IS NULL THEN t.target_full_name_reading_by ELSE f.reading_by END,
       CASE WHEN f.seq IS NULL THEN t.target_full_name_reading_candidates ELSE f.candidates END,
       CASE WHEN f.seq IS NULL THEN t.target_full_name_reading_likely ELSE f.likely END,
       CASE WHEN r.seq IS NULL THEN t.target_role ELSE r.value END,
       CASE WHEN d.seq IS NULL THEN t.target_department ELSE d.value END
FROM salescall.tasks t
LEFT JOIN LATERAL (SELECT * FROM salescall.task_fixes x WHERE x.task_id = t.task_id AND x.field = 'source_name'
                     AND (p_before IS NULL OR x.recorded_at < p_before) ORDER BY x.seq DESC LIMIT 1) s ON true
LEFT JOIN LATERAL (SELECT * FROM salescall.task_fixes x WHERE x.task_id = t.task_id AND x.field = 'target_name'
                     AND (p_before IS NULL OR x.recorded_at < p_before) ORDER BY x.seq DESC LIMIT 1) n ON true
LEFT JOIN LATERAL (SELECT * FROM salescall.task_fixes x WHERE x.task_id = t.task_id AND x.field = 'target_full_name'
                     AND (p_before IS NULL OR x.recorded_at < p_before) ORDER BY x.seq DESC LIMIT 1) f ON true
LEFT JOIN LATERAL (SELECT * FROM salescall.task_fixes x WHERE x.task_id = t.task_id AND x.field = 'target_role'
                     AND (p_before IS NULL OR x.recorded_at < p_before) ORDER BY x.seq DESC LIMIT 1) r ON true
LEFT JOIN LATERAL (SELECT * FROM salescall.task_fixes x WHERE x.task_id = t.task_id AND x.field = 'target_department'
                     AND (p_before IS NULL OR x.recorded_at < p_before) ORDER BY x.seq DESC LIMIT 1) d ON true
WHERE t.task_id = p_task
$$;
REVOKE ALL ON FUNCTION salescall.task_values(uuid, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.task_values(uuid, timestamptz) TO salescall_app;

-- 行のいまの値（0178 の形から、行の値の1か所を読む形にした。欄は同じ）
CREATE OR REPLACE VIEW salescall.tasks_now WITH (security_invoker = true) AS
  SELECT v.* FROM salescall.tasks t CROSS JOIN LATERAL salescall.task_values(t.task_id, NULL) v;

-- その期のお呼びする方の値（その期の終わりの直前までに効いていた欄の値。いまの期なら、いまの値）
CREATE FUNCTION salescall.task_person_values(p_task uuid, p_since timestamptz) RETURNS SETOF salescall.tasks_now LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT * FROM salescall.task_values(p_task, salescall.task_person_until(p_task, p_since))
$$;
REVOKE ALL ON FUNCTION salescall.task_person_values(uuid, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.task_person_values(uuid, timestamptz) TO salescall_app;

-- 会話・門・録音の名前の部分が読む期（1か所）：その行の予約した通話（進行中）の期。予約の前は、いまの予定の期。予定も無ければ、いまの期
CREATE FUNCTION salescall.task_call_period(p_task uuid) RETURNS timestamptz LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT CASE WHEN a.attempt_id IS NOT NULL THEN a.person_since
              WHEN n.task_id IS NOT NULL THEN n.person_since
              ELSE salescall.task_person_since(p_task, now()) END
  FROM (SELECT 1) one
  LEFT JOIN LATERAL (SELECT x.attempt_id, x.person_since FROM salescall.attempts x
                     WHERE x.task_id = p_task AND salescall.is_active(x.state) ORDER BY x.claimed_at DESC LIMIT 1) a ON true
  LEFT JOIN LATERAL (SELECT y.task_id, y.person_since FROM salescall.current_next_actions y WHERE y.task_id = p_task) n ON true
$$;
REVOKE ALL ON FUNCTION salescall.task_call_period(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.task_call_period(uuid) TO salescall_app;

CREATE FUNCTION salescall.task_call_values(p_task uuid) RETURNS SETOF salescall.tasks_now LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT * FROM salescall.task_person_values(p_task, salescall.task_call_period(p_task))
$$;
REVOKE ALL ON FUNCTION salescall.task_call_values(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.task_call_values(uuid) TO salescall_app;

-- ═════════ (3) 通話の期は、予約するとき、いまの予定の期をデータベースが入れる（0001 の形に足した） ═════════
CREATE OR REPLACE FUNCTION salescall.attempt_before_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_run uuid;
BEGIN
  IF NEW.state <> 'claimed' THEN
    RAISE EXCEPTION '試行は予約（claimed）からしか始まりません' USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.ended_at IS NOT NULL OR NEW.verification IS NOT NULL OR NEW.hangup_side IS NOT NULL THEN
    RAISE EXCEPTION '予約の試行は、終わった時刻・検証の状態・切れ方を持てません' USING ERRCODE = 'check_violation';
  END IF;
  NEW.claimed_at := now();   -- 時計はデータベースの時刻1つ（5-2-1 の1。渡された時刻は使わない）
  -- 通話の担当の期は、予約したときのいまの予定の期（1か所。渡された値は使わない。あとから変えられない＝0203 の attempt_record_fixed）
  NEW.person_since := (SELECT n.person_since FROM salescall.current_next_actions n WHERE n.task_id = NEW.task_id);
  SELECT run_id INTO v_run FROM salescall.tasks WHERE task_id = NEW.task_id;
  IF salescall.run_current_version(v_run) IS DISTINCT FROM NEW.version_id THEN
    RAISE EXCEPTION '試行の版が、実施回のいまの版と違います' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;

-- ═════════ (4) 締めた時刻＝行の結果が決まった最初の版（保留でない）か、お詫びの電話の結び ═════════
-- 結果の版が、行の結果を決めた版か（1か所）：判定できない（unknown）・取り消せない決定が決まるまでの保留（detail の decision_wait。
-- 書くのは締めの処理 core/settle の1か所）の版は、決めた版ではない
CREATE FUNCTION salescall.result_decided(p_state salescall.business_state, p_detail jsonb) RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
  SELECT p_state <> 'unknown' AND p_detail ->> 'decision_wait' IS NULL
$$;
REVOKE ALL ON FUNCTION salescall.result_decided(salescall.business_state, jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.result_decided(salescall.business_state, jsonb) TO salescall_app;

CREATE OR REPLACE FUNCTION salescall.settled_at(p_attempt uuid) RETURNS timestamptz LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT least((SELECT min(r.decided_at) FROM salescall.task_results r
                WHERE r.attempt_id = p_attempt AND salescall.result_decided(r.business_state, r.detail)),
               (SELECT o.recorded_at FROM salescall.apology_outcomes o WHERE o.attempt_id = p_attempt))
$$;

-- ═════════ (5) その通話は、その時刻にいまの期のものか・締めたときの立場の時刻（1か所） ═════════
CREATE FUNCTION salescall.attempt_in_period(p_attempt uuid, p_at timestamptz) RETURNS boolean LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT a.person_since IS NOT DISTINCT FROM salescall.task_person_since(a.task_id, p_at) FROM salescall.attempts a WHERE a.attempt_id = p_attempt
$$;
REVOKE ALL ON FUNCTION salescall.attempt_in_period(uuid, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.attempt_in_period(uuid, timestamptz) TO salescall_app;

-- 締めたときの立場の時刻（その通話を締めた時刻。まだ締めていない・通話が無い＝NULL なら、いま）
CREATE FUNCTION salescall.call_standing_at(p_attempt uuid) RETURNS timestamptz LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT coalesce(salescall.settled_at(p_attempt), now())
$$;
REVOKE ALL ON FUNCTION salescall.call_standing_at(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.call_standing_at(uuid) TO salescall_app;

-- 前の担当の方への通話（0203 の形から、2つの1か所を読む形にした。⛔ 0203 の `a.contact_id IS NOT NULL` は、試行の番号が必ずある＝NOT NULL
-- なので、どの場面でも効かなかった＝消した）
CREATE OR REPLACE FUNCTION salescall.former_person_call(p_attempt uuid) RETURNS boolean LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT coalesce(NOT salescall.attempt_in_period(p_attempt, salescall.call_standing_at(p_attempt)), false)
$$;

-- ═════════ (6) 記録してある予定・通話の期を、いまの決まりで置き直す ═════════
-- 記録してある期（その期の始まりの時刻）を、いまの決まりで、その時刻に効いていた期に置き直す（いまの決まりでも替わりの時刻なら、そのまま。
-- 決まりが変わって替わりでなくなった時刻＝読みだけの直し・表記の揺れだけの直しなどは、その前の替わりの時刻・最初の担当の方へ）。
-- 期を「記録」と「求め直し」の2か所で決めない：記録する欄は、書くときの1か所（task_person_since）の値だけ。お呼びする方が替わる決まりを
-- 変える移行は、ここを呼んで置き直す（持ち主だけが呼ぶ。アプリの役には渡さない）
CREATE FUNCTION salescall.repoint_person_periods() RETURNS void LANGUAGE plpgsql AS $$
BEGIN
  ALTER TABLE salescall.task_next_actions DISABLE TRIGGER task_next_actions_frozen;
  UPDATE salescall.task_next_actions n SET person_since = salescall.task_person_since(n.task_id, n.person_since)
  WHERE n.person_since IS DISTINCT FROM salescall.task_person_since(n.task_id, n.person_since) AND n.person_since IS NOT NULL;
  ALTER TABLE salescall.task_next_actions ENABLE TRIGGER task_next_actions_frozen;
  ALTER TABLE salescall.attempts DISABLE TRIGGER USER;
  UPDATE salescall.attempts a SET person_since = salescall.task_person_since(a.task_id, a.person_since)
  WHERE a.person_since IS DISTINCT FROM salescall.task_person_since(a.task_id, a.person_since) AND a.person_since IS NOT NULL;
  ALTER TABLE salescall.attempts ENABLE TRIGGER USER;
END $$;
REVOKE ALL ON FUNCTION salescall.repoint_person_periods() FROM PUBLIC;
SELECT salescall.repoint_person_periods();
