-- 0248 同じ契約者の中で同じ名前の依頼者を、同じ依頼者の中で同じ名前の業務を作れない（2026-11-19。書いた人：CL-2。DESIGN.md 3-9・15-2 の883。
--      CL-3 の連絡帳 4-1c の 44）
-- ⛔ 前は作れたので、タブに同じ名前が2つ並び、契約者が見分けられなかった（二度押し・同じ依頼者をもう一度作ったとき）。
-- ⇒ 名前が同じかは、表記をそろえた形（salescall.compare_form＝行の直しで値が違うかを比べるのと同じ1か所。全角と半角・空白の違いは同じ）で
--    比べ、データベースの一意の約束で守る（同時に2つ作っても1つだけ）。断りの文は core/registry の1か所（`NameTaken`）。
CREATE UNIQUE INDEX clients_same_name ON salescall.clients (tenant_id, salescall.compare_form(name));
CREATE UNIQUE INDEX businesses_same_name ON salescall.businesses (client_id, salescall.compare_form(name));
