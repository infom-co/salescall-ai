-- 0229 前の担当の方の「もうかけないで」で止めた番号が、いまの担当の方をお呼びする番号でもあるとき（2026-11-16。書いた人：CL-2。DESIGN.md 3-6・15-2 の836）
--
-- ✅ 2026-11-16 ユーザー（伺い「名前だけ『別の方』に直した行で、前の方が代表番号で『もうかけないで』と言ったとき」へ）：「契約者が業務ごとに選択できる。
--    デフォルトは佐藤様へはその番号でかける」。
-- ⇒ 設定 dnc.former_refusal_calls_new_person（業務ごと。初め：はい＝かける）。はい：その行の前の担当の方への通話（salescall.former_person_call）から
--    書いた発信禁止は、その行のいまの担当の方へかけるときだけ当てない（core/dnc.lookup の former_of＝core/target_policy の1か所）。発信禁止の台帳には
--    今までどおり書く＝同じお客様（前の担当の方）のほかの行・ほかの通話から書いた登録は、今までどおり止める。いいえ：その番号では、いまの担当の方にも
--    かけない（原則14：断られた番号には二度とかけない）。

INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, finest_scope) VALUES
('dnc.former_refusal_calls_new_person', 'override', 'boolean',
 'お呼びする方を「別の方に替わった」で直した行で、前の担当の方が「もうかけないで」と言った番号が、いまの担当の方をお呼びする番号でもある（会社の'
 '代表番号のまま名前だけを直した、など）とき、いまの担当の方へはその番号でかけるかです。はい（初め）＝かける（前の担当の方の断りは、その方だけのもの）'
 '／いいえ＝その番号ではかけない（断られた番号には二度とかけない。いまの担当の方へは、別の番号を足したらかけます）。前の担当の方をお呼びする'
 'ほかの行・ほかの電話は、どちらでも止めます',
 'DESIGN.md 3-6', 'business');
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('dnc.former_refusal_calls_new_person', 'system', 'true', 'decided',
 '2026-11-16 ユーザー：「契約者が業務ごとに選択できる。デフォルトは佐藤様へはその番号でかける」', '本番の取り消しの記録を見て', 'ユーザー');
