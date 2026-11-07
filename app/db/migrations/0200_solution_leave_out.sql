-- 0200 手で外した依頼者の解決策を、契約者が「入れない」と確かめた記録（✅ 2026-11-07 ユーザー：伺い「古い版で実施回を作ったとき、
--     入っていない解決策を契約者が手で外したとみなすか」に「みなす（契約者が承認をしないと自動で入らず、新規の電話もかけられません、
--     ダッシュボードに承認ボタンを分かりやすく表示）」。書いた人：CL-2。DESIGN.md 3-8-3 の【0200】・15-2 の540）
--
-- (1) 「手で外した」（0197 の client_solution_removals）は、手で版を替えたときに加えて、管理者か契約者が実施回を作るときに、その業務の
--     つながりの先でない版（古い版）を選んだときにも当たる（決めるのは core/client_solutions.removed_by_hand の1か所。書く守りは 0197 の
--     client_solution_removal_guard のまま＝実施回を作るときの割り当ても、手の割り当て）。
-- (2) 手で外した解決策は、契約者が解決策ごとに「入れる」か「入れない」かを選んで承認する。「入れる」＝いまある add_earlier（流れの結果に
--     選んだ方が残る＝0194）。「入れない」＝ここで作る client_solution_leave_outs（core/client_solutions.leave_out だけが書く）。
--     まだ確かめていない＝外した印のあとに、どちらもされていない（時刻で比べる。読むのは core/client_solutions._latest の1か所）。
-- (3) まだ確かめていない解決策が、ある実施回の版の流れに入っていない間は、その実施回で、まだ一度もかけていないお客様の行へ新しい電話を
--     かけない（門の理由 solutions_unconfirmed。約束したかけ直し・続き・お詫びの電話は止めない）。データベースの変更は無い。

CREATE TABLE salescall.client_solution_leave_outs (
  tenant_id   uuid NOT NULL,
  client_id   uuid NOT NULL,
  seq         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  solution_id uuid NOT NULL REFERENCES salescall.client_solutions (solution_id),
  version_id  uuid NOT NULL,                         -- 契約者が見ていた版の流れ（一覧 not_in_line を出した版）
  recorded_by text NOT NULL CHECK (btrim(recorded_by) <> ''),
  recorded_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, version_id) REFERENCES salescall.business_versions (client_id, version_id)
);
CREATE INDEX client_solution_leave_outs_solution ON salescall.client_solution_leave_outs (solution_id, recorded_at DESC);

-- 書けるのは、いま操作している管理者か契約者だけ（流れの結果の chosen_by と同じ。0194）。外した印のある解決策で、版はその解決策の業務の版。
-- 時刻はデータベースが決める
CREATE FUNCTION salescall.client_solution_leave_out_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.recorded_at := now();
  IF NEW.recorded_by IS DISTINCT FROM salescall.cur_actor()
     OR (salescall.cur_actor_kind() IS DISTINCT FROM 'operator' AND salescall.cur_actor_kind() IS DISTINCT FROM 'admin') THEN
    RAISE EXCEPTION '手で外した解決策を「入れない」と確かめられるのは、いま操作している管理者か契約者だけです'
      USING ERRCODE = 'insufficient_privilege', CONSTRAINT = 'client_solution_leave_out_by_person';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM salescall.client_solution_removals d WHERE d.solution_id = NEW.solution_id)
     OR NOT EXISTS (SELECT 1 FROM salescall.client_solutions c JOIN salescall.tasks t ON t.task_id = c.task_id
                      JOIN salescall.runs r ON r.run_id = t.run_id
                      JOIN salescall.business_versions b ON b.version_id = NEW.version_id AND b.business_id = r.business_id
                    WHERE c.solution_id = NEW.solution_id) THEN
    RAISE EXCEPTION '「入れない」と確かめられるのは、手で外した解決策を、その業務の版の流れで見たときだけです' USING ERRCODE = 'check_violation',
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
