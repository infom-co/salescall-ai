-- 0023 業務設定の中身（引き継ぎの (10)。Intent.md 6章・DESIGN.md 6-3・3-2）。書いた人：CL-2
-- - 業務設定の版の中身（business_contents）：名乗り（表記と読み）・用件の一文・業務の目的・目的の人・忙しいときの一言・声の型・
--   質問の文と聞くことと選択肢と分岐・名前の一覧。形と確かめは core/business_settings.py の1か所（読むときも書くときも同じ確かめ）。
--   質問の番号・並び・型・必須か（questions）と業務の決まり（business_rules）は、同じ版を作る取引の中で、同じ部品が書く。
-- - 版を作る・実施回に版を割り当てるのは、core/business_settings.py だけ（機械が見張る）。版は変えない（直すときは新しい版）。
CREATE TABLE salescall.business_contents (
  tenant_id  uuid NOT NULL,
  client_id  uuid NOT NULL,
  version_id uuid PRIMARY KEY,
  content    jsonb NOT NULL,
  saved_at   timestamptz NOT NULL DEFAULT now(),
  saved_by   text NOT NULL CHECK (btrim(saved_by) <> ''),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, version_id) REFERENCES salescall.business_versions (client_id, version_id)
);
CREATE TRIGGER business_contents_frozen BEFORE UPDATE OR DELETE ON salescall.business_contents
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
ALTER TABLE salescall.business_contents ENABLE ROW LEVEL SECURITY;
CREATE POLICY client_scope ON salescall.business_contents
  USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
  WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client());
GRANT SELECT, INSERT ON salescall.business_contents TO salescall_app;
