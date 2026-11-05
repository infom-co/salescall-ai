-- 0175 お客様リストの番号と連絡先（2026-11-05 ユーザー決定。書いた人：CL-2。DESIGN.md 3-4・3-5）
--
-- ✅ ユーザー：「携帯電話番号とLINE IDの項目も必要でないですか？お客様が電話で答えてくれた場合は、このシートを元にしたデータベースに
-- 加える必要があるでしょ？後、オンライン面談ようのID（ズームなど）」「その会社の目的の担当にかけたい業務の場合は、会社の番号のみ。
-- お問い合わせフォームからの返信や、相手からの問い合わせで携帯電話番号が指定されている場合は、携帯番号が優先番号になります。」
--
-- (1) 行の番号（task_numbers）に「記録だけの番号」（source record）を足す。かける番号は今までどおり、一覧の番号（list＝1つ）と
--     お客様が言った別の番号（callee）だけ。記録だけの番号は、試行にも次の予定にもならない（下の守り）。そのお客様が断ったら、
--     記録だけの番号も止める（原則14「そのお客様の行の番号すべて」＝発信禁止は行の番号の表の1か所を読む）。
--     番号の名札（label）：company＝会社の番号・designated＝お客様が指定した番号・mobile＝携帯電話番号・line＝LINE のご招待を
--     送る番号・said＝ほかのご連絡の方法で伺えたお電話の番号。一覧の番号（かける番号）は、お客様が指定した番号があればそれ
--     （designated）、無ければ会社の番号（company）。どちらにするかは取込（core/importer.py）の1か所が決めて、行の list_label に書く。
-- (2) 番号ではない連絡先（task_contacts）：メールアドレス・ファクス番号・住所・LINE ID・オンライン面談用の ID（サービスの名前つき）。
--     記録を足していくだけ（最新が正本＝task_contacts_now）。出どころは3つ：一覧の取込（どの取込の何行目）・通話（どの通話のどの発言）・
--     ダッシュボードの直し（管理者か契約者だけ）。書くのは core/customer_contacts.py の1か所。

-- ═════════ (1) 行の番号の名札と、記録だけの番号 ═════════
ALTER TABLE salescall.tasks
  ADD COLUMN list_label text NOT NULL DEFAULT 'company' CHECK (list_label IN ('company', 'designated'));

DO $$
DECLARE c record;
BEGIN
  FOR c IN SELECT conname FROM pg_constraint
           WHERE conrelid = 'salescall.task_numbers'::regclass AND contype = 'c' LOOP
    EXECUTE format('ALTER TABLE salescall.task_numbers DROP CONSTRAINT %I', c.conname);
  END LOOP;
END $$;

ALTER TABLE salescall.task_numbers
  ADD COLUMN label text,
  ADD COLUMN record_origin text,
  ADD COLUMN recorded_by text;

ALTER TABLE salescall.task_numbers DISABLE TRIGGER task_numbers_frozen;
UPDATE salescall.task_numbers SET label = 'company' WHERE source = 'list';
ALTER TABLE salescall.task_numbers ENABLE TRIGGER task_numbers_frozen;

ALTER TABLE salescall.task_numbers
  ADD CONSTRAINT task_numbers_source CHECK (source IN ('list', 'callee', 'record')),
  ADD CONSTRAINT task_numbers_label CHECK (
    CASE source WHEN 'list' THEN label IN ('company', 'designated')
                WHEN 'record' THEN label IN ('company', 'mobile', 'line', 'said')
                ELSE label IS NULL END),
  ADD CONSTRAINT task_numbers_record_origin CHECK (
    (source = 'record') = (record_origin IS NOT NULL) AND record_origin IN ('list', 'call', 'dashboard')),
  -- 通話から入った番号（お客様が言った別の番号・通話で伺えた番号）は、どの通話かを必ず持つ。ほかは持たない
  ADD CONSTRAINT task_numbers_attempt CHECK (
    (source_attempt_id IS NOT NULL) = (source = 'callee' OR coalesce(record_origin, '') = 'call')),
  ADD CONSTRAINT task_numbers_recorded_by CHECK ((coalesce(record_origin, '') = 'dashboard') = (recorded_by IS NOT NULL));

-- 一覧の番号の名札は、行の list_label（取込が決めたもの）
CREATE OR REPLACE FUNCTION salescall.task_after_insert() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO salescall.task_numbers (tenant_id, client_id, task_id, contact_id, phone_e164, source, label)
  SELECT NEW.tenant_id, NEW.client_id, NEW.task_id, NEW.contact_id, c.phone_e164, 'list', NEW.list_label
  FROM salescall.contacts c WHERE c.client_id = NEW.client_id AND c.contact_id = NEW.contact_id;
  RETURN NULL;
END $$;

-- ダッシュボードで足せるのは管理者か契約者だけ（記録したのは、その方）
CREATE FUNCTION salescall.task_number_record_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.record_origin = 'dashboard' THEN
    IF salescall.cur_actor_kind() IS DISTINCT FROM 'operator' AND salescall.cur_actor_kind() IS DISTINCT FROM 'admin' THEN
      RAISE EXCEPTION 'ダッシュボードでお客様の番号を足せるのは、管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege',
        CONSTRAINT = 'customer_contact_by_person';
    END IF;
    IF NEW.recorded_by IS DISTINCT FROM salescall.cur_actor() THEN
      RAISE EXCEPTION '記録した方は、いま操作している方です' USING ERRCODE = 'check_violation',
        CONSTRAINT = 'customer_contact_recorded_by';
    END IF;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER task_number_record_guard BEFORE INSERT ON salescall.task_numbers
  FOR EACH ROW EXECUTE FUNCTION salescall.task_number_record_guard();

-- 記録だけの番号には、かけない（試行も次の予定も、その番号を指せない。処理に誤りがあってもデータベースが断る）
CREATE FUNCTION salescall.dial_number_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = salescall, pg_temp AS $$
BEGIN
  IF NEW.contact_id IS NOT NULL AND EXISTS (
       SELECT 1 FROM salescall.task_numbers n
       WHERE n.task_id = NEW.task_id AND n.contact_id = NEW.contact_id AND n.source = 'record') THEN
    RAISE EXCEPTION '記録だけの番号（お客様が指定していない携帯電話番号など）には、かけません' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'dial_only_dialable_numbers';
  END IF;
  RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION salescall.dial_number_guard() FROM PUBLIC;
CREATE TRIGGER attempts_dial_number_guard BEFORE INSERT ON salescall.attempts
  FOR EACH ROW EXECUTE FUNCTION salescall.dial_number_guard();
CREATE TRIGGER next_actions_dial_number_guard BEFORE INSERT ON salescall.task_next_actions
  FOR EACH ROW EXECUTE FUNCTION salescall.dial_number_guard();

-- ═════════ (2) 番号ではない連絡先 ═════════
CREATE TABLE salescall.task_contacts (
  tenant_id   uuid NOT NULL,
  client_id   uuid NOT NULL,
  seq         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  task_id     uuid NOT NULL,
  kind        text NOT NULL CHECK (kind IN ('email', 'fax', 'address', 'line_id', 'meeting')),
  value       text NOT NULL CHECK (btrim(value) <> '' AND value = btrim(value)),
  service     text CHECK (service IS NULL OR (btrim(service) <> '' AND service = btrim(service))),   -- オンライン面談のサービス（Zoom など）
  origin      text NOT NULL CHECK (origin IN ('list', 'call', 'dashboard')),
  import_id   uuid,                                   -- 一覧の取込から：どの取込の
  source_row  integer,                                -- 何行目
  attempt_id  uuid,                                   -- 通話から：どの通話の
  turn        integer,                                -- どの発言（報告の根拠の発話。無ければ null）
  recorded_by text,                                   -- ダッシュボードから：記録した方
  recorded_at timestamptz NOT NULL DEFAULT now(),
  CHECK ((kind = 'meeting') OR service IS NULL),
  CHECK ((origin = 'list') = (import_id IS NOT NULL AND source_row IS NOT NULL)),
  CHECK (origin = 'list' OR (import_id IS NULL AND source_row IS NULL)),
  CHECK ((origin = 'call') = (attempt_id IS NOT NULL)),
  CHECK (origin = 'call' OR turn IS NULL),
  CHECK ((origin = 'dashboard') = (recorded_by IS NOT NULL)),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, task_id) REFERENCES salescall.tasks (client_id, task_id),
  FOREIGN KEY (client_id, import_id) REFERENCES salescall.imports (client_id, import_id),
  -- 伺えた通話は、同じ行の通話でなければならない
  FOREIGN KEY (client_id, attempt_id, task_id) REFERENCES salescall.attempts (client_id, attempt_id, task_id)
);
CREATE INDEX task_contacts_latest ON salescall.task_contacts (task_id, kind, seq DESC);
CREATE FUNCTION salescall.task_contact_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.origin = 'dashboard' THEN
    IF salescall.cur_actor_kind() IS DISTINCT FROM 'operator' AND salescall.cur_actor_kind() IS DISTINCT FROM 'admin' THEN
      RAISE EXCEPTION 'ダッシュボードでお客様の連絡先を直せるのは、管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege',
        CONSTRAINT = 'customer_contact_by_person';
    END IF;
    IF NEW.recorded_by IS DISTINCT FROM salescall.cur_actor() THEN
      RAISE EXCEPTION '記録した方は、いま操作している方です' USING ERRCODE = 'check_violation',
        CONSTRAINT = 'customer_contact_recorded_by';
    END IF;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER task_contact_guard BEFORE INSERT ON salescall.task_contacts
  FOR EACH ROW EXECUTE FUNCTION salescall.task_contact_guard();
CREATE TRIGGER task_contacts_frozen BEFORE UPDATE OR DELETE ON salescall.task_contacts
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
ALTER TABLE salescall.task_contacts ENABLE ROW LEVEL SECURITY;
CREATE POLICY client_scope ON salescall.task_contacts
  USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
  WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client());
GRANT SELECT, INSERT ON salescall.task_contacts TO salescall_app;

-- いまの連絡先（種類ごと・オンライン面談はサービスごとに、最新の記録。読むのはこの1か所）
CREATE VIEW salescall.task_contacts_now WITH (security_invoker = true) AS
  SELECT DISTINCT ON (task_id, kind, coalesce(service, '')) tenant_id, client_id, task_id, kind, value, service, origin,
         import_id, source_row, attempt_id, turn, recorded_by, recorded_at
  FROM salescall.task_contacts
  ORDER BY task_id, kind, coalesce(service, ''), seq DESC;
GRANT SELECT ON salescall.task_contacts_now TO salescall_app;
