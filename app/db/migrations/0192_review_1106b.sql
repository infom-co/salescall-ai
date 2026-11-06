-- 0192 2026-11-06 の2回目の見直し（第三者の見直し (3)。書いた人：CL-2。DESIGN.md 3-8-3・15-2 の504）
--
-- (1) 「まだ終えていない行か」を決める1か所 salescall.task_open（⛔ 同じ判断が Python の3か所＝業務設定の版の割り当て・使っている版・
--     録音の組を見る所に、別々の書き方で写されていた）。予定が無い行か、いまの予定が「終える」でない行。
-- (2) 依頼者の解決策の記録の時刻と並びは、データベースが決める（⛔ 記録の時刻を契約者の取引が好きに書け、1つの取引で同じ問いの
--     解決策を2つ足すと時刻が同じになり、取り込み直しで残す答えと版の答えが食い違った）。recorded_at はいまの時刻・並びは seq（足した順）。
-- (3) 解決策の流れの結果（client_solution_lines）の版は、その解決策の業務の版で、足した版は足す元の版か、そのつながりの先の版だけ。
-- (4) 版のつながりのきっかけと、記録した方の種類を結ぶ：標準の想定問答の版（standard）はシステムの処理だけ・解決策と上書き
--     （solution・override）は管理者か契約者だけ。

-- ═════════ (1) ═════════
CREATE FUNCTION salescall.task_open(p_task uuid) RETURNS boolean LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT coalesce((SELECT n.kind <> 'end' FROM salescall.current_next_actions n WHERE n.task_id = p_task), true)
$$;
REVOKE ALL ON FUNCTION salescall.task_open(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.task_open(uuid) TO salescall_app;

-- ═════════ (2) ═════════
ALTER TABLE salescall.client_solutions ADD COLUMN seq bigint GENERATED ALWAYS AS IDENTITY;
CREATE TRIGGER stamp_now BEFORE INSERT ON salescall.client_solutions FOR EACH ROW EXECUTE FUNCTION salescall.stamp_now('recorded_at');

-- ═════════ (3) ═════════
CREATE OR REPLACE FUNCTION salescall.client_solution_line_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM salescall.client_solutions c WHERE c.solution_id = NEW.solution_id
                 AND c.recorded_by = salescall.cur_actor()) THEN
    RAISE EXCEPTION '流れごとの結果を書けるのは、その解決策を足した方だけです' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'client_solution_line_by_recorder';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM salescall.client_solutions c JOIN salescall.tasks t ON t.task_id = c.task_id
                   JOIN salescall.runs r ON r.run_id = t.run_id
                   JOIN salescall.business_versions b ON b.version_id = NEW.base_version AND b.business_id = r.business_id
                 WHERE c.solution_id = NEW.solution_id)
     OR (NEW.version_id IS NOT NULL AND NEW.version_id <> NEW.base_version AND NOT EXISTS (
           WITH RECURSIVE chain(v) AS (SELECT s.to_version FROM salescall.version_successions s WHERE s.from_version = NEW.base_version
                                       UNION SELECT s.to_version FROM salescall.version_successions s JOIN chain c ON s.from_version = c.v)
           SELECT 1 FROM chain WHERE v = NEW.version_id)) THEN
    RAISE EXCEPTION '流れの版は、その解決策の業務の版で、足した版は足す元の版か、そのつながりの先の版だけです' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'client_solution_line_versions';
  END IF;
  RETURN NEW;
END $$;

-- ═════════ (4) ═════════
CREATE OR REPLACE FUNCTION salescall.version_succession_guard() RETURNS trigger LANGUAGE plpgsql AS $$
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
  IF (NEW.cause = 'standard') IS DISTINCT FROM (salescall.cur_actor_kind() = 'system') THEN
    RAISE EXCEPTION '標準の想定問答の版はシステムの処理だけ、解決策・上書きの版は管理者か契約者だけが足せます' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'version_succession_cause_actor';
  END IF;
  RETURN NEW;
END $$;
