-- 0178 お客様リストの行の直し（ダッシュボード。2026-11-05 ユーザー：「それらはダッシュボードに取込、ダッシュボードでも進捗確認や修正が
-- できるようにすること」。書いた人：CL-2。DESIGN.md 3-4-1。書くのは core/row_fixes.py の1か所）
--
-- (1) 直し（task_fixes）：行の欄（会社名・お呼びする方の名前・フルネーム・役職・部署）の直しを足していくだけ（いちばん新しいものが効く）。
--     取込の値（tasks の欄）は変えない。名前の欄は読みも一緒に持つ（決め方は取込と同じ core/readings.py の1か所）。値が空（NULL）＝欄を消した。
--     直せるのは管理者か契約者だけ（記録した方を残す）。
-- (2) 行のいまの値（tasks_now）：tasks の欄を、その欄のいちばん新しい直しで置き換えたもの。行の名前・役職・部署を読む所（会話が読む
--     business_settings.target_of・門・目的のお客様の材料の数え）は、どれもこの見方だけを読む。名前の欄ごとに、直した時刻（*_fixed_at）も
--     出す：会話の中でお客様に確かめた読み（task_confirmed_readings）は、その名前を直したより後に確かめたものだけが効く（直す前の名前の
--     読みを、直したあとの名前に付けない）。

CREATE TABLE salescall.task_fixes (
  tenant_id     uuid NOT NULL,
  client_id     uuid NOT NULL,
  seq           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  task_id       uuid NOT NULL,
  field         text NOT NULL CHECK (field IN ('source_name', 'target_name', 'target_full_name', 'target_role', 'target_department')),
  value         text CHECK (value IS NULL OR (btrim(value) <> '' AND value = btrim(value))),
  reading       text CHECK (reading IS NULL OR reading ~ '^[ぁ-ゖー・]+$'),
  reading_by    text CHECK (reading_by IS NULL OR reading_by IN ('given', 'person', 'ai')),
  candidates    text[] NOT NULL DEFAULT '{}' CHECK (array_to_string(candidates, '') ~ '^[ぁ-ゖー・]*$'),
  likely        text CHECK (likely IS NULL OR likely ~ '^[ぁ-ゖー・]+$'),
  recorded_by   text NOT NULL CHECK (btrim(recorded_by) <> ''),
  recorded_at   timestamptz NOT NULL DEFAULT now(),
  -- 読みを持つのは名前の欄だけ・消した欄は読みも無い。読みがあれば、読みを決めたものもある
  CHECK (field LIKE '%\_name' OR (reading IS NULL AND reading_by IS NULL AND candidates = '{}' AND likely IS NULL)),
  CHECK (value IS NOT NULL OR (reading IS NULL AND reading_by IS NULL AND candidates = '{}' AND likely IS NULL)),
  CHECK ((reading IS NULL) = (reading_by IS NULL)),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, task_id) REFERENCES salescall.tasks (client_id, task_id)
);
CREATE INDEX task_fixes_latest ON salescall.task_fixes (task_id, field, seq DESC);
CREATE FUNCTION salescall.task_fix_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF salescall.cur_actor_kind() IS DISTINCT FROM 'operator' AND salescall.cur_actor_kind() IS DISTINCT FROM 'admin' THEN
    RAISE EXCEPTION 'お客様リストの行を直せるのは、管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'task_fix_by_person';
  END IF;
  IF NEW.recorded_by IS DISTINCT FROM salescall.cur_actor() THEN
    RAISE EXCEPTION '記録した方は、いま操作している方です' USING ERRCODE = 'check_violation', CONSTRAINT = 'task_fix_recorded_by';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER task_fix_guard BEFORE INSERT ON salescall.task_fixes
  FOR EACH ROW EXECUTE FUNCTION salescall.task_fix_guard();
CREATE TRIGGER task_fixes_frozen BEFORE UPDATE OR DELETE ON salescall.task_fixes
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
ALTER TABLE salescall.task_fixes ENABLE ROW LEVEL SECURITY;
CREATE POLICY client_scope ON salescall.task_fixes
  USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
  WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client());
GRANT SELECT, INSERT ON salescall.task_fixes TO salescall_app;

-- 行のいまの値（読むのはこの見方だけ）
CREATE VIEW salescall.tasks_now WITH (security_invoker = true) AS
SELECT t.tenant_id, t.client_id, t.task_id, t.run_id, t.contact_id, t.client_ref, t.source_phone, t.source_row, t.created_at,
       t.import_id, t.list_label,
       CASE WHEN s.seq IS NULL THEN t.source_name ELSE s.value END AS source_name,
       CASE WHEN s.seq IS NULL THEN t.source_name_reading ELSE s.reading END AS source_name_reading,
       CASE WHEN s.seq IS NULL THEN t.source_name_reading_by ELSE s.reading_by END AS source_name_reading_by,
       CASE WHEN s.seq IS NULL THEN t.source_name_reading_candidates ELSE s.candidates END AS source_name_reading_candidates,
       CASE WHEN s.seq IS NULL THEN t.source_name_reading_likely ELSE s.likely END AS source_name_reading_likely,
       s.recorded_at AS source_name_fixed_at,
       CASE WHEN n.seq IS NULL THEN t.target_name ELSE n.value END AS target_name,
       CASE WHEN n.seq IS NULL THEN t.target_name_reading ELSE n.reading END AS target_name_reading,
       CASE WHEN n.seq IS NULL THEN t.target_name_reading_by ELSE n.reading_by END AS target_name_reading_by,
       CASE WHEN n.seq IS NULL THEN t.target_name_reading_candidates ELSE n.candidates END AS target_name_reading_candidates,
       CASE WHEN n.seq IS NULL THEN t.target_name_reading_likely ELSE n.likely END AS target_name_reading_likely,
       n.recorded_at AS target_name_fixed_at,
       CASE WHEN f.seq IS NULL THEN t.target_full_name ELSE f.value END AS target_full_name,
       CASE WHEN f.seq IS NULL THEN t.target_full_name_reading ELSE f.reading END AS target_full_name_reading,
       CASE WHEN f.seq IS NULL THEN t.target_full_name_reading_by ELSE f.reading_by END AS target_full_name_reading_by,
       CASE WHEN f.seq IS NULL THEN t.target_full_name_reading_candidates ELSE f.candidates END AS target_full_name_reading_candidates,
       CASE WHEN f.seq IS NULL THEN t.target_full_name_reading_likely ELSE f.likely END AS target_full_name_reading_likely,
       CASE WHEN r.seq IS NULL THEN t.target_role ELSE r.value END AS target_role,
       CASE WHEN d.seq IS NULL THEN t.target_department ELSE d.value END AS target_department
FROM salescall.tasks t
LEFT JOIN LATERAL (SELECT * FROM salescall.task_fixes x WHERE x.task_id = t.task_id AND x.field = 'source_name'
                   ORDER BY x.seq DESC LIMIT 1) s ON true
LEFT JOIN LATERAL (SELECT * FROM salescall.task_fixes x WHERE x.task_id = t.task_id AND x.field = 'target_name'
                   ORDER BY x.seq DESC LIMIT 1) n ON true
LEFT JOIN LATERAL (SELECT * FROM salescall.task_fixes x WHERE x.task_id = t.task_id AND x.field = 'target_full_name'
                   ORDER BY x.seq DESC LIMIT 1) f ON true
LEFT JOIN LATERAL (SELECT * FROM salescall.task_fixes x WHERE x.task_id = t.task_id AND x.field = 'target_role'
                   ORDER BY x.seq DESC LIMIT 1) r ON true
LEFT JOIN LATERAL (SELECT * FROM salescall.task_fixes x WHERE x.task_id = t.task_id AND x.field = 'target_department'
                   ORDER BY x.seq DESC LIMIT 1) d ON true;
GRANT SELECT ON salescall.tasks_now TO salescall_app;
