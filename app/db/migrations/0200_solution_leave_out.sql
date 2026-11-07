-- 0200 手で外した依頼者の解決策を、契約者が実施回ごとに確かめる（承認）（✅ 2026-11-07 ユーザー：伺い「古い版で実施回を作ったとき、
--     入っていない解決策を契約者が手で外したとみなすか」に「みなす（契約者が承認をしないと自動で入らず、新規の電話もかけられません、
--     ダッシュボードに承認ボタンを分かりやすく表示）」。✅ 2026-11-07 本体（CL-2）の決定「確かめは実施回ごと」。書いた人：CL-2。
--     DESIGN.md 3-8-3 の【0200】・15-2 の540）
--
-- (1) 「手で外した」（0197 の client_solution_removals）は、手で版を替えたときに加えて、管理者か契約者が実施回を作るときに、その業務の
--     つながりの先でない版（古い版）を選んだときにも当たる（決めるのは core/client_solutions.removed_by_hand の1か所）。
-- (2) 外すのも確かめるのも、契約者が実施回ごとにする操作なので、どれも実施回ごとに持つ：
--     - 外した印に実施回（run_id）を持つ（前に書いた印は、その割り当ての記録＝同じ取引・同じ方・同じ版から実施回を埋める）。
--     - 「入れる」＝いまある add_earlier の流れの結果に、どの実施回の承認か（chosen_run）を持つ（前に選んだ結果は、その前のいちばん新しい
--       外した印の実施回で埋める）。
--     - 「入れない」＝ここで作る client_solution_leave_outs（実施回ごと。core/client_solutions.leave_out だけが書く）。
--     その実施回で、まだ確かめていない＝その実施回の外した印があり、いちばん新しい外した印のあとに、その実施回の「入れない」が無い（時刻で
--     比べる。読むのは core/client_solutions._marks の1か所）。「入れる」を選べば、その解決策はその実施回の流れに入るので、一覧に出なく
--     なる（入っているかは core/client_solutions._outside の1か所）。⛔ 業務ごとに1つにすると、ほかの実施回の操作で、確かめ済みの実施回の電話が
--     また止まり、ある実施回で「入れる」を押すと、まだ選んでいないほかの実施回まで確かめ済みになった（契約者がしていない判断を作りが
--     代わりにする）。
-- (3) まだ確かめていない解決策がある実施回では、まだ一度もかけていないお客様の行へ新しい電話をかけない（門の理由 solutions_unconfirmed。
--     約束したかけ直し・続き・お詫びの電話は止めない）。データベースの変更は無い。

-- ═════════ 外した印の実施回 ═════════
ALTER TABLE salescall.client_solution_removals ADD COLUMN run_id uuid;
ALTER TABLE salescall.client_solution_removals DISABLE TRIGGER client_solution_removals_frozen;
UPDATE salescall.client_solution_removals d SET run_id = (
  SELECT v.run_id FROM salescall.run_versions v
  WHERE v.version_id = d.version_id AND NOT v.auto AND v.assigned_at = d.recorded_at AND v.assigned_by = d.recorded_by
  ORDER BY v.seq DESC LIMIT 1);
ALTER TABLE salescall.client_solution_removals ENABLE TRIGGER client_solution_removals_frozen;
ALTER TABLE salescall.client_solution_removals
  ADD CONSTRAINT client_solution_removals_run CHECK (run_id IS NOT NULL) NOT VALID,
  ADD FOREIGN KEY (client_id, run_id) REFERENCES salescall.runs (client_id, run_id);
CREATE INDEX client_solution_removals_run ON salescall.client_solution_removals (run_id, solution_id, recorded_at DESC);

-- 書けるのは、その取引で、その実施回にその版を、いま操作している方が手で割り当てたときだけ（0197 の形に、実施回を足した）
CREATE OR REPLACE FUNCTION salescall.client_solution_removal_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.recorded_at := now();
  IF NEW.recorded_by IS DISTINCT FROM salescall.cur_actor() THEN
    RAISE EXCEPTION '記録した方は、いま操作している方です' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'client_solution_removal_recorded_by';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM salescall.run_versions v WHERE v.run_id = NEW.run_id AND v.version_id = NEW.version_id AND NOT v.auto
                   AND v.assigned_at = now() AND v.assigned_by = salescall.cur_actor())
     OR NOT EXISTS (SELECT 1 FROM salescall.client_solutions c JOIN salescall.tasks t ON t.task_id = c.task_id
                      JOIN salescall.runs r ON r.run_id = t.run_id
                      JOIN salescall.business_versions b ON b.version_id = NEW.version_id AND b.business_id = r.business_id
                    WHERE c.solution_id = NEW.solution_id) THEN
    RAISE EXCEPTION '手で外した解決策を残せるのは、その取引でその実施回に、その業務の版を手で割り当てたときだけです'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'client_solution_removal_by_assignment';
  END IF;
  RETURN NEW;
END $$;

-- ═════════ 「入れる」の実施回 ═════════
ALTER TABLE salescall.client_solution_lines ADD COLUMN chosen_run uuid;
ALTER TABLE salescall.client_solution_lines DISABLE TRIGGER client_solution_lines_frozen;
UPDATE salescall.client_solution_lines l SET chosen_run = (
  SELECT d.run_id FROM salescall.client_solution_removals d
  WHERE d.solution_id = l.solution_id AND d.recorded_at <= l.recorded_at ORDER BY d.recorded_at DESC, d.seq DESC LIMIT 1)
WHERE l.chosen_by IS NOT NULL;
ALTER TABLE salescall.client_solution_lines ENABLE TRIGGER client_solution_lines_frozen;
ALTER TABLE salescall.client_solution_lines
  ADD CONSTRAINT client_solution_lines_chosen_run CHECK ((chosen_by IS NULL) = (chosen_run IS NULL)) NOT VALID,
  ADD FOREIGN KEY (client_id, chosen_run) REFERENCES salescall.runs (client_id, run_id);

-- 選んだ実施回は、その解決策の業務の実施回だけ（選んだ方の守りは 0197 の client_solution_line_guard のまま）
CREATE FUNCTION salescall.client_solution_line_run_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.chosen_run IS NOT NULL AND NOT EXISTS (
       SELECT 1 FROM salescall.client_solutions c JOIN salescall.tasks t ON t.task_id = c.task_id
         JOIN salescall.runs r ON r.run_id = t.run_id
         JOIN salescall.runs o ON o.run_id = NEW.chosen_run AND o.business_id = r.business_id
       WHERE c.solution_id = NEW.solution_id) THEN
    RAISE EXCEPTION '「入れる」を選んだ実施回は、その解決策の業務の実施回だけです' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'client_solution_line_chosen_run';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER client_solution_line_run_guard BEFORE INSERT ON salescall.client_solution_lines
  FOR EACH ROW EXECUTE FUNCTION salescall.client_solution_line_run_guard();

-- ═════════ 「入れない」 ═════════
CREATE TABLE salescall.client_solution_leave_outs (
  tenant_id   uuid NOT NULL,
  client_id   uuid NOT NULL,
  seq         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  solution_id uuid NOT NULL REFERENCES salescall.client_solutions (solution_id),
  run_id      uuid NOT NULL,                         -- どの実施回の承認か
  recorded_by text NOT NULL CHECK (btrim(recorded_by) <> ''),
  recorded_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, run_id) REFERENCES salescall.runs (client_id, run_id)
);
CREATE INDEX client_solution_leave_outs_run ON salescall.client_solution_leave_outs (run_id, solution_id, recorded_at DESC);

-- 書けるのは、いま操作している管理者か契約者だけ（流れの結果の chosen_by と同じ。0194）。その実施回で外した印のある解決策だけ。
-- 時刻はデータベースが決める
CREATE FUNCTION salescall.client_solution_leave_out_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.recorded_at := now();
  IF NEW.recorded_by IS DISTINCT FROM salescall.cur_actor()
     OR (salescall.cur_actor_kind() IS DISTINCT FROM 'operator' AND salescall.cur_actor_kind() IS DISTINCT FROM 'admin') THEN
    RAISE EXCEPTION '手で外した解決策を「入れない」と確かめられるのは、いま操作している管理者か契約者だけです'
      USING ERRCODE = 'insufficient_privilege', CONSTRAINT = 'client_solution_leave_out_by_person';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM salescall.client_solution_removals d WHERE d.solution_id = NEW.solution_id AND d.run_id = NEW.run_id) THEN
    RAISE EXCEPTION '「入れない」と確かめられるのは、その実施回で手で外した解決策だけです' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'client_solution_leave_out_removed';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER client_solution_leave_out_guard BEFORE INSERT ON salescall.client_solution_leave_outs
  FOR EACH ROW EXECUTE FUNCTION salescall.client_solution_leave_out_guard();
CREATE TRIGGER client_solution_leave_outs_frozen BEFORE UPDATE OR DELETE ON salescall.client_solution_leave_outs
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
ALTER TABLE salescall.client_solution_leave_outs ENABLE ROW LEVEL SECURITY;
CREATE POLICY client_scope ON salescall.client_solution_leave_outs
  USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
  WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client());
GRANT SELECT, INSERT ON salescall.client_solution_leave_outs TO salescall_app;
