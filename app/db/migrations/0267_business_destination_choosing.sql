-- 0267 業務が送り先を自分で選ぶか（2026-11-20。書いた人：CL-2。DESIGN_15-2 の919。CL-3 の連絡帳 4-1c の 56・CL-2 の連絡帳 4-445）
--
-- ✅ 2026-11-18 管理者「渡し方（ファイルか書き戻しか）は業務によって変わりえます」。⛔ 0254 の決め方は「選んだ送り先が1つも無い＝依頼者の送り先すべて」だったので、
-- 「この業務はどこへも書き戻さない（ファイルだけで渡す）」を表せず、依頼者に送り先が1つでもあれば、どの業務の結果もそこへ渡った。画面からは、全部に印の付いた業務が
-- 「選んで全部」か「選んでいない」かも分からなかった。
-- ⇒ 業務ごとの「送り先を自分で選ぶか」の記録（書き換えない・いちばん新しい記録がいま）。選ぶ＝選んだ送り先だけ（1つも無ければファイルだけ）／選ばない（記録が無い）＝
--    依頼者の送り先すべて（今まで）。決めるのは core/destinations の1か所（for_business・chosen）。0254 で送り先を選んだ業務は、選ぶにしておく（今の渡し方を変えない）。
CREATE TABLE salescall.business_destination_choosing (
  tenant_id    uuid NOT NULL,
  client_id    uuid NOT NULL,
  business_id  uuid NOT NULL,
  seq          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  choosing     boolean NOT NULL,                      -- 真＝業務が送り先を選ぶ（選んだものだけ。無ければファイルだけ）／偽＝依頼者の送り先すべて
  set_by       text NOT NULL CHECK (btrim(set_by) <> ''),
  set_at       timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, business_id) REFERENCES salescall.businesses (client_id, business_id)
);
CREATE INDEX business_destination_choosing_latest ON salescall.business_destination_choosing (business_id, seq DESC);
CREATE TRIGGER stamp_now BEFORE INSERT ON salescall.business_destination_choosing FOR EACH ROW EXECUTE FUNCTION salescall.stamp_now('set_at');
CREATE TRIGGER business_destination_choosing_frozen BEFORE UPDATE OR DELETE ON salescall.business_destination_choosing
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
INSERT INTO salescall.business_destination_choosing (tenant_id, client_id, business_id, choosing, set_by)
SELECT DISTINCT ON (d.business_id) d.tenant_id, d.client_id, d.business_id, true, '移行 0267（0254 で送り先を選んでいた業務）'
FROM salescall.business_destinations d ORDER BY d.business_id;
CREATE TRIGGER business_destination_choosing_guard BEFORE INSERT ON salescall.business_destination_choosing
  FOR EACH ROW EXECUTE FUNCTION salescall.business_destination_guard();   -- 0254 と同じ守り（管理者か契約者・いま操作している方）
ALTER TABLE salescall.business_destination_choosing ENABLE ROW LEVEL SECURITY;
CREATE POLICY client_scope ON salescall.business_destination_choosing
  USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
  WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client());
GRANT SELECT, INSERT ON salescall.business_destination_choosing TO salescall_app;
