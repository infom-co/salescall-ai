-- 0182 元の番号を「サブ」にする（2026-11-06 ユーザー決定。書いた人：CL-2。DESIGN.md 3-4-1）
--
-- ✅ ユーザー：「話せたら、新番号がメイン、旧番号はサブ」・「かけ直しの約束をしていて、旧番号でつながった場合、新しい番号が
--    つながらないときに元の番号へもかけるという意味」。
-- (1) サブの番号（task_sub_numbers）：かける番号を直したあと、前にかける番号だった番号でお客様と話せた（会話の報告の1か所
--     call_report.talked_in）ときに、その番号を記録する（足していくだけ。書くのは core/tasks.py の1か所）。いまのサブの番号は関数
--     salescall.task_sub_contact の1か所（いちばん新しい直しより後に記録した、いまかける番号でない、いちばん新しいもの）。
-- (2) 前にかける番号だった番号（0181 の task_number_replaced）から、サブの番号を外す（サブへの電話は、新しい番号の回数に数える）。
-- (3) 記録だけの番号への守り（0175・0179）を、いままでに一度でもかける番号だった番号は通すように直す（⛔ 0179 の穴：番号を2回直すと、
--     まだかけていない約束の電話＝まず前の番号でかける電話が、前の番号が記録だけの番号のとき、データベースに断られた）。

CREATE TABLE salescall.task_sub_numbers (
  tenant_id   uuid NOT NULL,
  client_id   uuid NOT NULL,
  seq         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  task_id     uuid NOT NULL,
  contact_id  uuid NOT NULL,
  attempt_id  uuid NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, task_id, contact_id) REFERENCES salescall.task_numbers (client_id, task_id, contact_id),
  FOREIGN KEY (client_id, attempt_id, task_id) REFERENCES salescall.attempts (client_id, attempt_id, task_id)
);
CREATE INDEX task_sub_numbers_latest ON salescall.task_sub_numbers (task_id, seq DESC);
CREATE TRIGGER task_sub_numbers_frozen BEFORE UPDATE OR DELETE ON salescall.task_sub_numbers
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
ALTER TABLE salescall.task_sub_numbers ENABLE ROW LEVEL SECURITY;
CREATE POLICY client_scope ON salescall.task_sub_numbers
  USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
  WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client());
GRANT SELECT, INSERT ON salescall.task_sub_numbers TO salescall_app;

-- いまのサブの番号（決めるのはここだけ）
CREATE FUNCTION salescall.task_sub_contact(p_task uuid) RETURNS uuid LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT s.contact_id FROM salescall.task_sub_numbers s
  WHERE s.task_id = p_task AND s.recorded_at >= salescall.task_count_from(p_task)
    AND s.contact_id IS DISTINCT FROM salescall.task_dial_contact(p_task)
  ORDER BY s.seq DESC LIMIT 1
$$;
REVOKE ALL ON FUNCTION salescall.task_sub_contact(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.task_sub_contact(uuid) TO salescall_app;

-- 前にかける番号だった番号（0181 の置き換え）：サブの番号は外す
CREATE OR REPLACE FUNCTION salescall.task_number_replaced(p_task uuid, p_contact uuid) RETURNS boolean LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT p_contact IS DISTINCT FROM salescall.task_dial_contact(p_task)
     AND p_contact IS DISTINCT FROM salescall.task_sub_contact(p_task)
     AND (p_contact = (SELECT t.contact_id FROM salescall.tasks t WHERE t.task_id = p_task)
          OR EXISTS (SELECT 1 FROM salescall.task_dial_fixes f WHERE f.task_id = p_task AND f.contact_id = p_contact))
$$;

-- 記録だけの番号には、かけない。ただし、いままでに一度でもかける番号に直した番号（いまかける番号・前にかける番号だった番号・
-- サブの番号）は通す（0179 の置き換え）
CREATE OR REPLACE FUNCTION salescall.dial_number_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = salescall, pg_temp AS $$
BEGIN
  IF NEW.contact_id IS NOT NULL AND EXISTS (
       SELECT 1 FROM salescall.task_numbers n
       WHERE n.task_id = NEW.task_id AND n.contact_id = NEW.contact_id AND n.source = 'record')
     AND NOT EXISTS (SELECT 1 FROM salescall.task_dial_fixes f WHERE f.task_id = NEW.task_id AND f.contact_id = NEW.contact_id) THEN
    RAISE EXCEPTION '記録だけの番号（お客様が指定していない携帯電話番号など）には、かけません' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'dial_only_dialable_numbers';
  END IF;
  RETURN NEW;
END $$;
