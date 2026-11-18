-- 0240 契約者の直しで電話をかけることになるときは、直した契約者が「かけてよい」と確かめるまでかけない（2026-11-18 の7回目の見直し。書いた人：CL-2。
--      DESIGN.md 10章・15-2 の864。✅ 2026-11-18 ユーザー：伺い2「契約者が直したことは優先：ただし、間違て契約者が直した可能性もあるので、本当に折り返して
--      よいのか確認と承認をもらう。承認なく勝手にかけたらクレームになる可能性があるため。」・続き(1)「直した契約者（お勧め）」）
--
-- (1) 確かめ待ち（correction_calls）：契約者が結果を直し（core/approval.correct_result）、直しに従った行の次の予定が「かける」になったら、その直しの版ごとに
--     1つ書く（直した取引の中で）。
-- (2) 確かめ（correction_call_confirms）：管理者か契約者だけ（いま操作している方）。確かめられるのは、いま確かめ待ちの直しだけ。
-- (3) 確かめ待ちか（salescall.task_correction_unconfirmed の1か所）：その行のいちばん新しい確かめ待ちが、まだ確かめられておらず、いまの予定が「かける」。
--     直し直して「かけない」結果にした（予定が終わり・保留）なら待たない。直し直して、また「かける」になれば、新しい確かめ待ち。
--     門は、契約者の承認の1か所（core/approval.uncleared）で読む（候補にも出さない）。

CREATE TABLE salescall.correction_calls (
  tenant_id    uuid NOT NULL,
  client_id    uuid NOT NULL,
  seq          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  task_id      uuid NOT NULL,
  version_no   integer NOT NULL,           -- 直した契約者の版
  asked_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (task_id, version_no),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, task_id) REFERENCES salescall.tasks (client_id, task_id)
);
CREATE TRIGGER stamp_now BEFORE INSERT ON salescall.correction_calls FOR EACH ROW EXECUTE FUNCTION salescall.stamp_now('asked_at');
CREATE TRIGGER correction_calls_frozen BEFORE UPDATE OR DELETE ON salescall.correction_calls
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
ALTER TABLE salescall.correction_calls ENABLE ROW LEVEL SECURITY;
CREATE POLICY client_scope ON salescall.correction_calls
  USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
  WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client());
GRANT SELECT, INSERT ON salescall.correction_calls TO salescall_app;

CREATE TABLE salescall.correction_call_confirms (
  tenant_id     uuid NOT NULL,
  client_id     uuid NOT NULL,
  seq           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  task_id       uuid NOT NULL,
  version_no    integer NOT NULL,
  confirmed_by  text NOT NULL CHECK (btrim(confirmed_by) <> ''),
  confirmed_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (task_id, version_no),
  FOREIGN KEY (task_id, version_no) REFERENCES salescall.correction_calls (task_id, version_no),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id)
);
CREATE TRIGGER stamp_now BEFORE INSERT ON salescall.correction_call_confirms FOR EACH ROW EXECUTE FUNCTION salescall.stamp_now('confirmed_at');
CREATE TRIGGER correction_call_confirms_frozen BEFORE UPDATE OR DELETE ON salescall.correction_call_confirms
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
ALTER TABLE salescall.correction_call_confirms ENABLE ROW LEVEL SECURITY;
CREATE POLICY client_scope ON salescall.correction_call_confirms
  USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
  WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client());
GRANT SELECT, INSERT ON salescall.correction_call_confirms TO salescall_app;

-- 確かめ待ちの直しの版（無ければ NULL。1か所）
CREATE FUNCTION salescall.task_correction_unconfirmed(p_task uuid) RETURNS integer
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT c.version_no FROM salescall.correction_calls c
  WHERE c.task_id = p_task
    AND c.seq = (SELECT max(m.seq) FROM salescall.correction_calls m WHERE m.task_id = p_task)
    AND NOT EXISTS (SELECT 1 FROM salescall.correction_call_confirms k WHERE k.task_id = c.task_id AND k.version_no = c.version_no)
    AND EXISTS (SELECT 1 FROM salescall.current_next_actions n WHERE n.task_id = p_task AND n.kind = 'call')
$$;
REVOKE ALL ON FUNCTION salescall.task_correction_unconfirmed(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.task_correction_unconfirmed(uuid) TO salescall_app;

-- 確かめられるのは、管理者か契約者（いま操作している方）で、いま確かめ待ちの直しだけ
CREATE FUNCTION salescall.correction_call_confirm_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT salescall.cur_is_person() THEN
    RAISE EXCEPTION '直しでかける電話を確かめられるのは、管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'correction_call_confirm_by_person';
  END IF;
  IF NEW.confirmed_by IS DISTINCT FROM salescall.cur_actor() THEN
    RAISE EXCEPTION '確かめた方は、いま操作している方です' USING ERRCODE = 'check_violation', CONSTRAINT = 'correction_call_confirmed_by';
  END IF;
  IF NEW.version_no IS DISTINCT FROM salescall.task_correction_unconfirmed(NEW.task_id) THEN
    RAISE EXCEPTION '確かめられるのは、いま確かめ待ちの直しだけです' USING ERRCODE = 'check_violation', CONSTRAINT = 'correction_call_confirm_waiting';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER correction_call_confirm_guard BEFORE INSERT ON salescall.correction_call_confirms
  FOR EACH ROW EXECUTE FUNCTION salescall.correction_call_confirm_guard();
