-- 0265 連絡先の出どころ（2026-11-20。書いた人：CL-2。DESIGN_15-2 の917。CL-1 の連絡帳 4-212 の listed_contacts・CL-2 の連絡帳 4-445）
--
-- ✅ 2026-11-20 ユーザー（計画書 9-4「担当者が不在」の行・伺い66）「ア(お客様から伺ったものや会社の方から伺ったもの)、こちらが無断で取ったメールならウ 伺い直す。」
-- ＝お客様・お客様の会社の方から伺った連絡先は、会話の側がこちらから言って確かめる。こちらが無断で取った・分からない連絡先は、今のまま伺い直す。
-- ✅ 2026-11-20 ユーザー（出どころの欄の伺い）「基本アだが、行ごとに変更も可能」＝お客様リストの連絡先ごとの欄（と、行ごとの欄）。
-- ⇒ 行の連絡先の値ごとの出どころの記録（書き換えない。いまの出どころは、その値のいちばん新しい記録）。番号の表・連絡先の表は書き換えられず、かける番号は
--    行を作るときにデータベースが足すので、出どころは別の記録に持つ。分かった出どころだけを書く（無い＝分からない）。書くのは core/customer_contacts の1か所
--    （一覧の欄・通話で伺った連絡先＝customer）。
CREATE TABLE salescall.task_contact_sources (
  tenant_id   uuid NOT NULL,
  client_id   uuid NOT NULL,
  task_id     uuid NOT NULL REFERENCES salescall.tasks,
  seq         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind        text NOT NULL CHECK (kind IN ('number', 'email', 'fax', 'address', 'line_id')),   -- number＝電話番号（値は E.164）
  value       text NOT NULL CHECK (btrim(value) <> ''),
  heard_from  text NOT NULL CHECK (heard_from IN ('customer', 'company')),   -- お客様から伺った／お客様の会社の方から伺った
  import_id   uuid,
  attempt_id  uuid,
  set_at      timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id)
);
CREATE INDEX task_contact_sources_task ON salescall.task_contact_sources (task_id, kind, value, seq DESC);
CREATE TRIGGER stamp_now BEFORE INSERT ON salescall.task_contact_sources FOR EACH ROW EXECUTE FUNCTION salescall.stamp_now('set_at');
CREATE TRIGGER task_contact_sources_frozen BEFORE UPDATE OR DELETE ON salescall.task_contact_sources
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
ALTER TABLE salescall.task_contact_sources ENABLE ROW LEVEL SECURITY;
CREATE POLICY client_scope ON salescall.task_contact_sources
  USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
  WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client());
GRANT SELECT, INSERT ON salescall.task_contact_sources TO salescall_app;
