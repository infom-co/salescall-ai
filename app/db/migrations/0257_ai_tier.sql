-- 0257 会話 AI・業務を取り込む AI の、無料枠と有料枠の選び（2026-11-19。書いた人：CL-2。Intent.md 12-3・DESIGN.md 8章・15-2 の898。CL-3 の連絡帳 4-1c の 36）
-- ✅ 2026-10-16 ユーザー（案2）「会話AIの API は、契約者が無料枠と有料枠を選べる。契約者がビジネスで使うなら、有料枠をおすすめする（強制はしない）」・
-- 「おすすめの理由として画面に出すこと：無料枠は、入力が品質改善に使われうる条件である（要確認）。上限も低い」。
-- 選べるのは、契約者が自分で API を契約するプラン（ai.plan_*＝own＝契約者のキー）のときだけ意味がある（AI料金込み＝システムのキー・判定の AI は、システムの
-- 枠）。どの枠かを決めるのは core/ai_plans.tier の1か所。契約者が選ぶ前の初めの値は、おすすめの有料枠（仮。管理者に伺う）。
INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, finest_scope, choices) VALUES
('ai.tier_conversation', 'override', 'choice',
 '会話 AI を自分で契約したキーで使うとき（プランが own のとき）の、API の枠です。free＝無料枠／paid＝有料枠。ビジネスで使うなら有料枠を'
 'おすすめします（強制しません）。無料枠は、入力が品質改善に使われうる条件です（要確認）。上限も低くなります。契約者ごと',
 'Intent.md 12-3・DESIGN.md 8章', 'tenant', ARRAY['free', 'paid']),
('ai.tier_import', 'override', 'choice',
 '業務を取り込む専用の AI を自分で契約したキーで使うとき（プランが own のとき）の、API の枠です。free＝無料枠／paid＝有料枠。ビジネスで'
 '使うなら有料枠をおすすめします（強制しません）。無料枠は、入力が品質改善に使われうる条件です（要確認）。上限も低くなります。契約者ごと',
 'Intent.md 12-3・DESIGN.md 8章', 'tenant', ARRAY['free', 'paid']);
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('ai.tier_conversation', 'system', '"paid"', 'provisional', '2026-10-16 ユーザー：ビジネスで使うなら有料枠をおすすめ（初めの値はおすすめの枠。仮）',
 '管理者に初めの値を伺ったとき', 'CL-2'),
('ai.tier_import', 'system', '"paid"', 'provisional', '2026-10-16 ユーザー：ビジネスで使うなら有料枠をおすすめ（初めの値はおすすめの枠。仮）',
 '管理者に初めの値を伺ったとき', 'CL-2');
