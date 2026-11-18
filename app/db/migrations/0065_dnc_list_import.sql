-- 0065 発信禁止の一覧の取込（Intent.md 原則14・5-3 (8)・7-2。DESIGN.md 3-6-2・15-2 の163。2026-10-18。書いた人：CL-2）
--
-- 前は、発信禁止の台帳に入れる道が、通話の中の「もうかけないで」・通話後の検証・判定・1件ずつの登録だけだった。依頼者から
-- 「この番号にはかけないで」という一覧を受け取っても、入れる道が無かった（台帳の理由「依頼者の一覧」（client_list）だけがあった）。
-- ⇒ 一覧のファイルを取り込む（core/dnc_import.py）。依頼者の取引なら依頼者の台帳、契約者の取引なら契約者の共通の台帳に入れる
--    （入口は1つ）。取込1回と行ごとの結果を記録し、一覧から入った登録は、どの取込から入ったかを持つ（依頼者から正式に取り消しを
--    求められたとき、その取込の登録をたどれる）。
-- 取り込むのは管理者か契約者だけ（依頼者の一覧を受け取って入れるのは人の操作。システムの処理は一覧を作らない）。

CREATE TABLE salescall.dnc_imports (            -- 発信禁止の一覧の取込1回（変更不可）
  tenant_id   uuid NOT NULL REFERENCES salescall.tenants (tenant_id),
  client_id   uuid,                              -- NULL＝契約者の共通の発信禁止の一覧
  import_id   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  ledger      text NOT NULL CHECK (ledger IN ('client', 'contractor')),
  file_name   text NOT NULL CHECK (btrim(file_name) <> ''),
  file_sha256 text NOT NULL CHECK (file_sha256 ~ '^[0-9a-f]{64}$'),
  file_kind   text NOT NULL CHECK (file_kind IN ('csv', 'xlsx')),
  encoding    text,
  sheet_name  text,
  mapping     jsonb NOT NULL,
  plan_digest text NOT NULL CHECK (plan_digest ~ '^[0-9a-f]{64}$'),   -- 管理者か契約者が確かめたプレビューの指紋
  created_at  timestamptz NOT NULL DEFAULT now(),
  created_by  text NOT NULL CHECK (btrim(created_by) <> ''),
  CHECK ((ledger = 'client') = (client_id IS NOT NULL)),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  UNIQUE (tenant_id, import_id, ledger),
  UNIQUE (client_id, import_id)
);
CREATE FUNCTION salescall.dnc_import_by_person() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF salescall.cur_actor_kind() IS DISTINCT FROM 'operator' AND salescall.cur_actor_kind() IS DISTINCT FROM 'admin' THEN
    RAISE EXCEPTION '発信禁止の一覧を取り込めるのは、管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'dnc_import_by_person';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER dnc_import_by_person BEFORE INSERT ON salescall.dnc_imports
  FOR EACH ROW EXECUTE FUNCTION salescall.dnc_import_by_person();
CREATE TRIGGER dnc_imports_frozen BEFORE UPDATE OR DELETE ON salescall.dnc_imports
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();

CREATE TABLE salescall.dnc_import_rows (        -- 行ごとの結果（変更不可。番号そのものは持たない：台帳の登録が持つ）
  tenant_id   uuid NOT NULL,
  client_id   uuid,
  import_id   uuid NOT NULL,
  row_no      integer NOT NULL CHECK (row_no >= 1),
  outcome     text NOT NULL CHECK (outcome IN ('registered', 'already', 'stopped', 'empty')),
  reason_code text,
  reason      text,
  zero_added  boolean NOT NULL,                  -- 番号の先頭の 0 が消えたと決めて 0 を足したか（DESIGN.md 3-4）
  new_numbers integer NOT NULL DEFAULT 0 CHECK (new_numbers >= 0),   -- この行で新しく止めた番号の数（広げた分も）
  new_refs    integer NOT NULL DEFAULT 0 CHECK (new_refs >= 0),      -- この行で新しく止めた管理番号の数
  CHECK ((outcome = 'registered') = (new_numbers + new_refs > 0)),
  CHECK ((outcome IN ('stopped', 'already')) = (reason_code IS NOT NULL AND reason IS NOT NULL)),
  PRIMARY KEY (import_id, row_no),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, import_id) REFERENCES salescall.dnc_imports (client_id, import_id)
);
-- 契約者の一覧の行（client_id が NULL）は、上の外部キーが効かないので、取込の契約者と同じかをここで守る
ALTER TABLE salescall.dnc_import_rows ADD COLUMN ledger text GENERATED ALWAYS AS
  (CASE WHEN client_id IS NULL THEN 'contractor' ELSE 'client' END) STORED;
ALTER TABLE salescall.dnc_import_rows ADD CONSTRAINT dnc_import_rows_same_import
  FOREIGN KEY (tenant_id, import_id, ledger) REFERENCES salescall.dnc_imports (tenant_id, import_id, ledger);
CREATE TRIGGER dnc_import_rows_frozen BEFORE UPDATE OR DELETE ON salescall.dnc_import_rows
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();

-- 見える範囲：依頼者の取引はその依頼者の取込、契約者の取引はその契約者のすべての取込（契約者の一覧と、依頼者の一覧）
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['dnc_imports', 'dnc_import_rows'] LOOP
    EXECUTE format('ALTER TABLE salescall.%I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY scope ON salescall.%I
                      USING (tenant_id = salescall.cur_tenant() AND (salescall.cur_client() IS NULL OR client_id = salescall.cur_client()))
                      WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id IS NOT DISTINCT FROM salescall.cur_client())', t);
  END LOOP;
END $$;
GRANT SELECT, INSERT ON salescall.dnc_imports, salescall.dnc_import_rows TO salescall_app;

-- 一覧から入った登録は、どの取込から入ったかを持つ。依頼者の一覧（理由 client_list）は、必ず取込から（1件ずつ入れる道は無い）。
-- 依頼者の台帳の登録は、同じ依頼者の取込だけを指す。契約者の台帳の登録は、契約者の一覧の取込だけを指す。
ALTER TABLE salescall.dnc_entries ADD COLUMN import_id uuid,
  ADD CONSTRAINT dnc_entries_list_from_import CHECK ((reason = 'client_list') = (import_id IS NOT NULL)),
  ADD CONSTRAINT dnc_entries_import_fkey FOREIGN KEY (client_id, import_id) REFERENCES salescall.dnc_imports (client_id, import_id);
ALTER TABLE salescall.contractor_dnc_entries ADD COLUMN import_id uuid,
  ADD COLUMN import_ledger text GENERATED ALWAYS AS ('contractor') STORED,
  ADD CONSTRAINT contractor_dnc_entries_import_fkey FOREIGN KEY (tenant_id, import_id, import_ledger)
    REFERENCES salescall.dnc_imports (tenant_id, import_id, ledger);

-- 取り消しのほかは変えられない（取込を指す欄も）
CREATE OR REPLACE FUNCTION salescall.dnc_before_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION '発信禁止の登録は消せません（取り消しは記録つきで）' USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  IF OLD.revoked_at IS NOT NULL
     OR (NEW.tenant_id, NEW.client_id, NEW.entry_id, NEW.phone_e164, NEW.client_ref, NEW.reason, NEW.source_attempt_id,
         NEW.registered_at, NEW.registered_by, NEW.import_id)
        IS DISTINCT FROM
        (OLD.tenant_id, OLD.client_id, OLD.entry_id, OLD.phone_e164, OLD.client_ref, OLD.reason, OLD.source_attempt_id,
         OLD.registered_at, OLD.registered_by, OLD.import_id) THEN
    RAISE EXCEPTION '発信禁止の登録は、取り消すこと（1回）しかできません' USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  IF NEW.revoked_at IS NOT NULL AND salescall.cur_actor_kind() IS DISTINCT FROM 'operator'
     AND salescall.cur_actor_kind() IS DISTINCT FROM 'admin' THEN
    RAISE EXCEPTION '発信禁止の取り消しは、管理者か契約者だけができます（システムの処理はできません）' USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN NEW;
END $$;
CREATE OR REPLACE FUNCTION salescall.contractor_dnc_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION '発信禁止の登録は消せません（取り消しは記録つきで）' USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  IF TG_OP = 'UPDATE' AND (OLD.revoked_at IS NOT NULL
     OR (NEW.tenant_id, NEW.entry_id, NEW.phone_e164, NEW.reason, NEW.registered_at, NEW.registered_by, NEW.import_id)
        IS DISTINCT FROM (OLD.tenant_id, OLD.entry_id, OLD.phone_e164, OLD.reason, OLD.registered_at, OLD.registered_by,
                          OLD.import_id)) THEN
    RAISE EXCEPTION '発信禁止の登録は、取り消すこと（1回）しかできません' USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  IF salescall.cur_actor_kind() IS DISTINCT FROM 'operator' AND salescall.cur_actor_kind() IS DISTINCT FROM 'admin' THEN
    RAISE EXCEPTION '契約者の共通の発信禁止を登録・取り消しできるのは、管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'contractor_dnc_by_person';
  END IF;
  RETURN NEW;
END $$;
