-- 0179 かける番号の直し（ダッシュボード。2026-11-06 ユーザー決定。書いた人：CL-2。DESIGN.md 3-4-1）
--
-- ✅ ユーザー：「はい、取り込んだ電話番号は残したまま、新しい電話番号が旧番号のすぐ下に入力できるようにします。新番号を登録すると、旧電話
-- 番号はグレーアウトして、新番号がくっきり表示されます。」
-- (1) かける番号の直し（task_dial_fixes）：その行の、いまかける番号を、行の番号の表の1つに付け替えた記録（足していくだけ。いちばん新しい
--     ものが効く）。取り込んだ番号（一覧の番号）は残る。直した番号は、行の番号の表に入っている番号だけ（足すのは core/tasks.py の1か所）。
--     直せるのは管理者か契約者だけ（記録した方を残す）。
-- (2) いまかける番号は関数 salescall.task_dial_contact の1か所（直しがあれば、いちばん新しい直しの番号・無ければ一覧の番号）。門の
--     「予定の無い行の番号」・取込のあとの最初の予定・電話のあとの番号の選び（次の動き）は、どれもこれを読む。
-- (3) 記録だけの番号（0175）への守りを、いまかける番号に直した番号だけは通すように直す（ダッシュボードで記録だけの番号を、かける番号に
--     直せる＝お客様リストの携帯電話番号をあとからかける番号にする、など）。

CREATE TABLE salescall.task_dial_fixes (
  tenant_id   uuid NOT NULL,
  client_id   uuid NOT NULL,
  seq         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  task_id     uuid NOT NULL,
  contact_id  uuid NOT NULL,
  recorded_by text NOT NULL CHECK (btrim(recorded_by) <> ''),
  recorded_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, task_id, contact_id) REFERENCES salescall.task_numbers (client_id, task_id, contact_id)
);
CREATE INDEX task_dial_fixes_latest ON salescall.task_dial_fixes (task_id, seq DESC);
CREATE FUNCTION salescall.task_dial_fix_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF salescall.cur_actor_kind() IS DISTINCT FROM 'operator' AND salescall.cur_actor_kind() IS DISTINCT FROM 'admin' THEN
    RAISE EXCEPTION 'かける番号を直せるのは、管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'task_dial_fix_by_person';
  END IF;
  IF NEW.recorded_by IS DISTINCT FROM salescall.cur_actor() THEN
    RAISE EXCEPTION '記録した方は、いま操作している方です' USING ERRCODE = 'check_violation', CONSTRAINT = 'task_dial_fix_recorded_by';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER task_dial_fix_guard BEFORE INSERT ON salescall.task_dial_fixes
  FOR EACH ROW EXECUTE FUNCTION salescall.task_dial_fix_guard();
CREATE TRIGGER task_dial_fixes_frozen BEFORE UPDATE OR DELETE ON salescall.task_dial_fixes
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
ALTER TABLE salescall.task_dial_fixes ENABLE ROW LEVEL SECURITY;
CREATE POLICY client_scope ON salescall.task_dial_fixes
  USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
  WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client());
GRANT SELECT, INSERT ON salescall.task_dial_fixes TO salescall_app;

-- いまかける番号（決めるのはここだけ。直しが無ければ一覧の番号）。守りの関数（下）からも読むので、行ごとの制限を越えて読む
CREATE FUNCTION salescall.task_dial_contact(p_task uuid) RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = salescall, pg_temp AS $$
  SELECT coalesce((SELECT f.contact_id FROM salescall.task_dial_fixes f WHERE f.task_id = p_task ORDER BY f.seq DESC LIMIT 1),
                  (SELECT t.contact_id FROM salescall.tasks t WHERE t.task_id = p_task))
$$;
REVOKE ALL ON FUNCTION salescall.task_dial_contact(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.task_dial_contact(uuid) TO salescall_app;

-- 記録だけの番号には、かけない。ただし、いまかける番号に直した番号は通す（0175 の置き換え）
CREATE OR REPLACE FUNCTION salescall.dial_number_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = salescall, pg_temp AS $$
BEGIN
  IF NEW.contact_id IS NOT NULL AND EXISTS (
       SELECT 1 FROM salescall.task_numbers n
       WHERE n.task_id = NEW.task_id AND n.contact_id = NEW.contact_id AND n.source = 'record')
     AND NEW.contact_id IS DISTINCT FROM salescall.task_dial_contact(NEW.task_id) THEN
    RAISE EXCEPTION '記録だけの番号（お客様が指定していない携帯電話番号など）には、かけません' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'dial_only_dialable_numbers';
  END IF;
  RETURN NEW;
END $$;
