-- 0189 見直し(B)：版のつながりと自動の割り当ての守り・解決策を業務の版の流れごとに残す（2026-11-06 CL-2 の見直し。書いた人：CL-2。
-- DESIGN.md 3-8-3）
--
-- (1) ✅ 2026-11-06 ユーザー「業務のすべての実施回」：依頼者の解決策は、その業務のまだ終えていない実施回の版の流れすべてに足す
--     （core/client_solutions.add）。0184 の client_solutions は足した版を1つしか持てなかった ⇒ 流れごとの結果を client_solution_lines に
--     持つ（足す元にした版・足した版・置き換えたか・確かめに通らなかった理由）。中身が変わらない流れは新しい版を作らず、足した版＝元の版。
-- (2) 版のつながり（0183）の守りがデータベースに無かった（見直し(B) の4：契約者の取引で、新しい版→古い版・輪を直に書けた。輪が
--     できると、つながりをたどる問い合わせが終わらず、版を自動で替える仕事と解決策が止まった。記録の時刻・記録した方も渡した値のまま）：
--     to_version は from_version より後に作った版（版の番号が大きい＝逆向き・輪を作れない）・recorded_at はデータベースが決める・
--     recorded_by は操作している方。
-- (3) 実施回の版の割り当て（run_versions）：自動の印 auto＝真はシステムの処理だけ（決まった間隔の仕事。契約者が手の割り当てを
--     自動に見せかけられた）・assigned_at はデータベースが決める。

-- ═════════ (1) ═════════
CREATE TABLE salescall.client_solution_lines (
  tenant_id    uuid NOT NULL,
  client_id    uuid NOT NULL,
  solution_id  uuid NOT NULL REFERENCES salescall.client_solutions (solution_id),
  base_version uuid NOT NULL,                       -- 足す元にした版（その流れのつながりのいちばん後の版）
  version_id   uuid,                                -- 足した版（中身が同じなら base_version のまま。無い＝確かめに通らなかった）
  replaced     boolean NOT NULL,                    -- 同じ問いの答えを置き換えた（偽＝新しい問いを足した）
  problems     text[] NOT NULL DEFAULT '{}',
  PRIMARY KEY (solution_id, base_version),
  CHECK ((version_id IS NULL) = (cardinality(problems) > 0)),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, base_version) REFERENCES salescall.business_versions (client_id, version_id),
  FOREIGN KEY (client_id, version_id) REFERENCES salescall.business_versions (client_id, version_id)
);
-- 流れごとの結果を書けるのは、その解決策を足した方だけ（足せるのが管理者か契約者だけなのは 0184 の client_solution_guard の1か所）
CREATE FUNCTION salescall.client_solution_line_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM salescall.client_solutions c WHERE c.solution_id = NEW.solution_id
                 AND c.recorded_by = salescall.cur_actor()) THEN
    RAISE EXCEPTION '流れごとの結果を書けるのは、その解決策を足した方だけです' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'client_solution_line_by_recorder';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER client_solution_line_guard BEFORE INSERT ON salescall.client_solution_lines
  FOR EACH ROW EXECUTE FUNCTION salescall.client_solution_line_guard();
CREATE TRIGGER client_solution_lines_frozen BEFORE UPDATE OR DELETE ON salescall.client_solution_lines
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
ALTER TABLE salescall.client_solution_lines ENABLE ROW LEVEL SECURITY;
CREATE POLICY client_scope ON salescall.client_solution_lines
  USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
  WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client());
GRANT SELECT, INSERT ON salescall.client_solution_lines TO salescall_app;

-- 前の形の結果（版1つ）は流れの表へ移す（本番のデータベースはまだ無い。試験の作り直しでは行が無い）
INSERT INTO salescall.client_solution_lines (tenant_id, client_id, solution_id, base_version, version_id, replaced, problems)
  SELECT c.tenant_id, c.client_id, c.solution_id, coalesce(c.version_id, salescall.run_current_version(t.run_id)), c.version_id,
         c.replaced, c.problems
  FROM salescall.client_solutions c JOIN salescall.tasks t ON t.task_id = c.task_id
  WHERE coalesce(c.version_id, salescall.run_current_version(t.run_id)) IS NOT NULL;
ALTER TABLE salescall.client_solutions DROP COLUMN version_id, DROP COLUMN problems, DROP COLUMN replaced;

-- ═════════ (2) ═════════
CREATE FUNCTION salescall.version_succession_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.recorded_at := now();
  IF NEW.recorded_by IS DISTINCT FROM salescall.cur_actor() THEN
    RAISE EXCEPTION '記録した方は、いま操作している方です' USING ERRCODE = 'check_violation', CONSTRAINT = 'version_succession_recorded_by';
  END IF;
  IF (SELECT version_no FROM salescall.business_versions WHERE version_id = NEW.to_version)
     <= (SELECT version_no FROM salescall.business_versions WHERE version_id = NEW.from_version) THEN
    RAISE EXCEPTION '後の版は、前の版より後に作った版だけです（逆向き・輪は作れない）' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'version_succession_forward';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER version_succession_guard BEFORE INSERT ON salescall.version_successions
  FOR EACH ROW EXECUTE FUNCTION salescall.version_succession_guard();

-- ═════════ (3) ═════════
CREATE FUNCTION salescall.run_version_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.assigned_at := now();
  IF NEW.auto AND salescall.cur_actor_kind() IS DISTINCT FROM 'system' THEN
    RAISE EXCEPTION '自動の割り当ては、システムの処理だけです' USING ERRCODE = 'insufficient_privilege', CONSTRAINT = 'run_version_auto_by_system';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER run_version_guard BEFORE INSERT ON salescall.run_versions
  FOR EACH ROW EXECUTE FUNCTION salescall.run_version_guard();
