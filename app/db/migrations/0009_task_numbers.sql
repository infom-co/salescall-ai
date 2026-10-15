-- 0009 行（タスク）の番号の表（DESIGN.md 3-5・3-6・5-2-2・7-2-1）。書いた人：CL-2（2026-10-15）
-- - 依頼主の一覧の1行（タスク）は1つのまま、その行の番号を表に持つ：一覧の番号（取込のとき。1つ）＋相手が言った別の番号。
--   一覧の番号の行は、タスクを作ったときにデータベースが自分で足す（書き漏れが起きない）。別の番号を足すのは core/tasks.py だけ。
-- - 試行（発信）と次の予定（かけ直す）は、**その行の番号の表にある番号しか指せない**（ほかの行・ほかの依頼主の番号へは、
--   処理に誤りがあってもかけられない）。どの番号へかけるかを決めるのは次の動き（Q4）だけで、予定の行に持つ。門はその番号へかける。
-- - 1つの行に進行中の発信は1つだけ（番号が2つになっても、同じ相手へ同時にかけない）。

CREATE TABLE salescall.task_numbers (       -- 行の番号（変更不可）
  tenant_id         uuid NOT NULL,
  client_id         uuid NOT NULL,
  task_id           uuid NOT NULL,
  contact_id        uuid NOT NULL,
  phone_e164        text NOT NULL,
  source            text NOT NULL CHECK (source IN ('list', 'callee')),   -- list＝依頼主の一覧／callee＝相手が言った別の番号
  source_attempt_id uuid,                                                  -- callee のとき：どの通話で言われたか
  added_at          timestamptz NOT NULL DEFAULT now(),
  CHECK ((source = 'callee') = (source_attempt_id IS NOT NULL)),
  PRIMARY KEY (task_id, contact_id),
  UNIQUE (client_id, task_id, contact_id),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, task_id) REFERENCES salescall.tasks (client_id, task_id),
  FOREIGN KEY (client_id, contact_id, phone_e164) REFERENCES salescall.contacts (client_id, contact_id, phone_e164),
  -- 言われた通話は、同じ行の通話でなければならない
  FOREIGN KEY (client_id, source_attempt_id, task_id) REFERENCES salescall.attempts (client_id, attempt_id, task_id)
);
CREATE UNIQUE INDEX task_numbers_one_list_number ON salescall.task_numbers (task_id) WHERE source = 'list';
CREATE INDEX task_numbers_by_phone ON salescall.task_numbers (client_id, phone_e164);

-- タスクを作ったら、一覧の番号の行を同じ取引で足す（呼ぶ側が足し忘れる道を作らない）。中身はタスクの連絡先そのもの。
-- 一覧の番号は1行に1つ（上の一意の約束）なので、ほかの番号を「一覧の番号」と名乗らせることもできない
CREATE FUNCTION salescall.task_after_insert() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO salescall.task_numbers (tenant_id, client_id, task_id, contact_id, phone_e164, source)
  SELECT NEW.tenant_id, NEW.client_id, NEW.task_id, NEW.contact_id, c.phone_e164, 'list'
  FROM salescall.contacts c WHERE c.client_id = NEW.client_id AND c.contact_id = NEW.contact_id;
  RETURN NULL;
END $$;
CREATE TRIGGER task_after_insert AFTER INSERT ON salescall.tasks
  FOR EACH ROW EXECUTE FUNCTION salescall.task_after_insert();

-- いまあるタスクの分（本番の前なので、ふつうは空）
INSERT INTO salescall.task_numbers (tenant_id, client_id, task_id, contact_id, phone_e164, source)
SELECT t.tenant_id, t.client_id, t.task_id, t.contact_id, c.phone_e164, 'list'
FROM salescall.tasks t JOIN salescall.contacts c ON c.client_id = t.client_id AND c.contact_id = t.contact_id;

CREATE TRIGGER task_numbers_frozen BEFORE UPDATE OR DELETE ON salescall.task_numbers
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
ALTER TABLE salescall.task_numbers ENABLE ROW LEVEL SECURITY;
CREATE POLICY client_scope ON salescall.task_numbers
  USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
  WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client());
GRANT SELECT, INSERT ON salescall.task_numbers TO salescall_app;

-- ═════════ 試行は、その行の番号の表にある番号にしかかけられない ═════════
ALTER TABLE salescall.attempts DROP CONSTRAINT attempts_client_id_task_id_contact_id_fkey;
ALTER TABLE salescall.attempts ADD CONSTRAINT attempts_number_of_task
  FOREIGN KEY (client_id, task_id, contact_id) REFERENCES salescall.task_numbers (client_id, task_id, contact_id);
-- 1つの行に進行中の発信は1つだけ（番号ごと・連絡先ごと・回線ごとの約束に加えて）
CREATE UNIQUE INDEX attempts_one_active_per_task ON salescall.attempts (task_id) WHERE salescall.is_active(state);
CREATE INDEX attempts_by_task ON salescall.attempts (task_id, claimed_at);

-- ═════════ 次の予定（かけ直す）は、どの番号へかけるかを持つ（決めるのは次の動きだけ） ═════════
ALTER TABLE salescall.task_next_actions
  ADD COLUMN contact_id uuid,
  ADD CONSTRAINT task_next_actions_number CHECK ((kind = 'call') = (contact_id IS NOT NULL)),
  ADD CONSTRAINT task_next_actions_number_of_task
    FOREIGN KEY (client_id, task_id, contact_id) REFERENCES salescall.task_numbers (client_id, task_id, contact_id);

-- ═════════ 期限切れの予約を戻す範囲に「同じ行」を足す（1つの行に進行中は1つ、の約束を足したため。0007 の置き換え） ═════════
DROP FUNCTION salescall.release_expired_claims(uuid, text, uuid);
CREATE FUNCTION salescall.release_expired_claims(p_task uuid, p_contact uuid, p_phone text, p_line uuid) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
DECLARE n integer;
BEGIN
  UPDATE salescall.attempts SET state = 'released'
  WHERE state = 'claimed' AND claim_expires_at <= now()
    AND (task_id = p_task OR contact_id = p_contact OR phone_e164 = p_phone OR line_id = p_line);
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$;
REVOKE ALL ON FUNCTION salescall.release_expired_claims(uuid, uuid, text, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.release_expired_claims(uuid, uuid, text, uuid) TO salescall_app;
