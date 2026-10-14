-- 0006 発信対象の取込と、発信禁止台帳の登録（DESIGN.md 3-4・Q3）。書いた人：CL-2（2026-10-14）
-- - 取込の記録（取込1回・行ごとの結果）は追記だけ。依頼主のファイルそのものは保存しない（指紋だけ。個人情報を余計に持たない）。
-- - 行ごとの結果は「登録した／止めた（理由）／もう登録済み／空の行」。止めた行は、行番号と理由だけを残す（値は残さない）。
--   件数は行ごとの結果から数える（取込1回の表に件数を写さない＝同じことを2か所に持たない）。
-- - 1つの実施回で、同じ依頼主の管理番号は1つのタスクだけ（依頼主が結果を自分の表と突き合わせる鍵が重ならない）。
-- - 発信禁止に登録したら、同じ取引で、その番号のまだ発信していない予約を取り消す（DESIGN.md 5-2-1 の2）。
-- - かけてよい番号の種類（フリーダイヤル・ナビダイヤル）は設定で決める（依頼主ごとに上書きできる）。

CREATE TABLE salescall.imports (            -- 取込1回（変更不可）
  tenant_id     uuid NOT NULL,
  client_id     uuid NOT NULL,
  import_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id        uuid NOT NULL,
  file_name     text NOT NULL CHECK (btrim(file_name) <> ''),
  file_sha256   text NOT NULL CHECK (file_sha256 ~ '^[0-9a-f]{64}$'),  -- 同じファイルかを見分ける指紋（中身は持たない）
  file_kind     text NOT NULL CHECK (file_kind IN ('csv', 'xlsx')),
  encoding      text,                       -- CSV の文字の決まり（utf-8 / cp932）。xlsx は NULL
  sheet_name    text,                       -- xlsx の読んだシートの名前。CSV は NULL
  mapping       jsonb NOT NULL,             -- 列の対応付け（どの見出しの列が番号・管理番号・名前か）
  plan_digest   text NOT NULL CHECK (plan_digest ~ '^[0-9a-f]{64}$'),  -- 人が確かめたプレビューの指紋（登録の結果と同じもの）
  created_at    timestamptz NOT NULL DEFAULT now(),
  created_by    text NOT NULL CHECK (btrim(created_by) <> ''),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, run_id) REFERENCES salescall.runs (client_id, run_id),
  UNIQUE (client_id, import_id)
);

ALTER TABLE salescall.tasks
  ADD COLUMN import_id uuid,
  ADD CONSTRAINT tasks_import_fkey FOREIGN KEY (client_id, import_id) REFERENCES salescall.imports (client_id, import_id);
-- 1つの実施回で、同じ管理番号は1つのタスクだけ
CREATE UNIQUE INDEX tasks_one_ref_per_run ON salescall.tasks (run_id, client_ref) WHERE client_ref IS NOT NULL;

CREATE TABLE salescall.import_rows (        -- 行ごとの結果（変更不可）
  tenant_id   uuid NOT NULL,
  client_id   uuid NOT NULL,
  import_id   uuid NOT NULL,
  row_no      integer NOT NULL CHECK (row_no >= 1),   -- 依頼主のファイルの行番号（表計算ソフトで見える番号）
  outcome     text NOT NULL CHECK (outcome IN ('registered', 'stopped', 'already', 'empty')),
  reason_code text,
  reason      text,
  task_id     uuid,
  zero_added  boolean NOT NULL,                       -- 番号の先頭の 0 が消えたと決めて 0 を足したか（DESIGN.md 3-4）
  CHECK ((outcome IN ('registered', 'already')) = (task_id IS NOT NULL)),   -- 登録済みは、その行のタスクを指す
  CHECK ((outcome IN ('stopped', 'already')) = (reason_code IS NOT NULL AND reason IS NOT NULL)),
  PRIMARY KEY (import_id, row_no),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, import_id) REFERENCES salescall.imports (client_id, import_id),
  FOREIGN KEY (client_id, task_id) REFERENCES salescall.tasks (client_id, task_id)
);

CREATE TRIGGER imports_frozen BEFORE UPDATE OR DELETE ON salescall.imports
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
CREATE TRIGGER import_rows_frozen BEFORE UPDATE OR DELETE ON salescall.import_rows
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['imports', 'import_rows'] LOOP
    EXECUTE format('ALTER TABLE salescall.%I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY client_scope ON salescall.%I
                      USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
                      WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())', t);
  END LOOP;
END $$;
GRANT SELECT, INSERT ON salescall.imports, salescall.import_rows TO salescall_app;

-- システム共通の発信禁止に番号を入れたとき、すべての依頼主の、その番号のまだ発信していない予約を取り消す
-- （取引の依頼主の範囲の外も取り消すので、表の持ち主の権限で動く。できることは「その番号の予約のままの試行を released に」だけ）
CREATE FUNCTION salescall.release_claims_for_system_dnc(p_phone text) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
DECLARE n integer;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM salescall.system_dnc_entries WHERE phone_e164 = p_phone) THEN
    RAISE EXCEPTION 'システムの発信禁止に無い番号です' USING ERRCODE = 'check_violation';
  END IF;
  UPDATE salescall.attempts SET state = 'released' WHERE phone_e164 = p_phone AND state = 'claimed';
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$;
REVOKE ALL ON FUNCTION salescall.release_claims_for_system_dnc(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.release_claims_for_system_dnc(text) TO salescall_app;

-- かけてよい番号の種類（依頼主ごとに上書きできる）
INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, bound_key, bound_side) VALUES
('calling.allow_toll_free', 'override', 'boolean', 'フリーダイヤル（0120・0800）へかけてよいか', 'DESIGN.md 3-4・5-2 の4', NULL, NULL),
('calling.allow_navi_dial', 'override', 'boolean', 'ナビダイヤル（0570）へかけてよいか（通話料がかかる。Rakuten Link の無料通話の対象外）', 'DESIGN.md 3-4・5-2 の4', NULL, NULL);
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('calling.allow_toll_free', 'system', 'true', 'provisional', '法人の代表番号がフリーダイヤルのことがある。通話料はかからない', 'ユーザーが決めたとき', 'CL-2'),
('calling.allow_navi_dial', 'system', 'false', 'provisional', 'ナビダイヤルは通話料がかかり、Rakuten Link の無料通話の対象外（Intent.md 8-4）。自動でかけ続けると費用が膨らむ', 'ユーザーが決めたとき', 'CL-2');
