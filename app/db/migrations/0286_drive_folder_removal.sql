-- 0286 契約が終わった依頼者のハードディスクの控えを、画面から消す（2026-11-23。書いた人：CL-2。DESIGN_15-2.md の961。設計書 3-10）
-- ✅ 2026-11-22 管理者：「ハードディスクに残す。消すのはハードディスクで消すだけ。ただし、絶対に不要なデータは、イ（画面で消す）もできるように
--    しておく。」＝イ：契約者が画面で「この依頼者のデータを消す」を押したときに、ハードディスクのその依頼者のフォルダを消す。
-- 頼めるのは、サーバーから外したあとの依頼者だけ（サーバーにまだ中身がある依頼者の控えを先に消さない）・管理者か契約者（いま操作している方）だけ・理由を書く。
-- 消すのはその契約者の PC（ハードディスクの中を見られるのは PC だけ＝agent.backup.remove_client_folder の1か所：その依頼者のフォルダの決まった種類だけ。
-- データベース全体の控えのフォルダ・人が置いたほかのファイルは消さない）。見守り役の仕事 drive_removals が PC に頼み、結果を残す（失敗は残して次の回にまた頼む）。
-- フォルダの名前の記録（client_backup_folders）は残す（名前をほかの依頼者に使い回さない）。
CREATE TABLE salescall.client_drive_removals (     -- 頼んだ（追記だけ）
  removal_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id    uuid NOT NULL,
  client_id    uuid NOT NULL,
  requested_at timestamptz NOT NULL DEFAULT now(),
  requested_by text NOT NULL DEFAULT salescall.cur_actor() CHECK (btrim(requested_by) <> ''),
  reason       text NOT NULL CHECK (btrim(reason) <> ''),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id)
);
CREATE TABLE salescall.client_drive_removal_results (   -- PC が消した結果（試すたびに1行。追記だけ）
  result_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  removal_id bigint NOT NULL REFERENCES salescall.client_drive_removals,
  tenant_id  uuid NOT NULL,
  client_id  uuid NOT NULL,
  ok         boolean NOT NULL,
  removed    integer CHECK (removed IS NULL OR removed >= 0),
  error      text CHECK (error IS NULL OR btrim(error) <> ''),
  at         timestamptz NOT NULL DEFAULT now(),
  CHECK (ok = (error IS NULL))
);
DO $$ DECLARE t text; BEGIN
  FOREACH t IN ARRAY ARRAY['client_drive_removals', 'client_drive_removal_results'] LOOP
    EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON salescall.%I FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change()',
                   t || '_frozen', t);
    EXECUTE format('ALTER TABLE salescall.%I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY client_scope ON salescall.%I
                      USING (tenant_id = salescall.cur_tenant() AND (salescall.cur_client() IS NULL OR client_id = salescall.cur_client()))
                      WITH CHECK (tenant_id = salescall.cur_tenant() AND (salescall.cur_client() IS NULL OR client_id = salescall.cur_client()))', t);
    EXECUTE format('GRANT SELECT, INSERT ON salescall.%I TO salescall_app', t);
  END LOOP;
END $$;

CREATE FUNCTION salescall.drive_removal_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT salescall.cur_is_person() THEN
    RAISE EXCEPTION 'ハードディスクの控えを消すのを頼めるのは、管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'drive_removal_by_person';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM salescall.client_forgets f WHERE f.client_id = NEW.client_id) THEN
    RAISE EXCEPTION 'ハードディスクの控えを消せるのは、サーバーから外したあとの依頼者だけです' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'drive_removal_after_forget';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER client_drive_removal_guard BEFORE INSERT ON salescall.client_drive_removals
  FOR EACH ROW EXECUTE FUNCTION salescall.drive_removal_guard();

-- 消した結果を書くのはシステムの処理だけ
CREATE FUNCTION salescall.drive_removal_result_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF salescall.cur_actor_kind() IS DISTINCT FROM 'system' THEN
    RAISE EXCEPTION 'ハードディスクの控えを消した結果を書くのは、システムの処理だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'drive_removal_result_by_system';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER client_drive_removal_result_guard BEFORE INSERT ON salescall.client_drive_removal_results
  FOR EACH ROW EXECUTE FUNCTION salescall.drive_removal_result_guard();

-- 外した依頼者の行は残す（頼んだ記録・結果は、外したあとにできる）
CREATE OR REPLACE FUNCTION salescall.forget_kept_tables() RETURNS text[] LANGUAGE sql IMMUTABLE AS $$
  SELECT ARRAY['attempts', 'attempt_state_history', 'dnc_entries', 'dnc_imports', 'lines', 'clients', 'client_states',
               'client_backup_folders', 'backup_runs', 'task_numbers', 'tasks', 'contacts', 'runs', 'businesses', 'business_versions',
               'recording_audio', 'client_contract_ends', 'client_contract_end_cancels', 'client_forgets',
               'client_drive_removals', 'client_drive_removal_results']
$$;
