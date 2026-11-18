-- 0176 契約プラン（AI の料金。2026-11-05 ユーザー決定。書いた人：CL-2。Intent.md 1-4・12章・DESIGN.md 8章）
--
-- ✅ ユーザー：「(ア) で使う API のキーは…：契約者の契約プランで選べるようにする。AI料金込みプランと、自分でAPIを契約するプラン」
-- ・「判定は企業秘密なのでこちらのみ。会話は選べるように。」
-- - 会話 AI と、業務を取り込む専用の AI（業務設定に組む AI）は、契約者ごとに選ぶ：included＝AI料金込み（システムのキーで呼び、料金は
--   その契約者の分として数える）／own＝契約者が自分で API を契約する（その契約者のキーで呼ぶ）。
-- - 判定の AI は、いつもシステムのキー（企業秘密＝契約者のキーでは呼ばない）。設定を作らない（選べないもの＝読む1か所
--   core/ai_plans.py が決まりとして持つ）。
-- - ⏳ 初めの値（契約者が選ぶ前）は、まだ決まっていない（仮に included。管理者に伺う）。

INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, finest_scope, choices) VALUES
('ai.plan_conversation', 'override', 'choice',
 '会話 AI の料金のプランです。included＝AI料金込み（システムのキーで呼び、料金はこの契約者の分として数えます）／own＝契約者が自分で'
 ' API を契約する（この契約者のキーで呼びます）。契約者ごと',
 'DESIGN.md 8章・12章', 'tenant', ARRAY['included', 'own']),
('ai.plan_import', 'override', 'choice',
 '業務を取り込む専用の AI（依頼者の資料一式から業務設定に組む AI）の料金のプランです。included＝AI料金込み（システムのキーで呼び、'
 '料金はこの契約者の分として数えます）／own＝契約者が自分で API を契約する（この契約者のキーで呼びます）。契約者ごと',
 'DESIGN.md 3-7・8章・12章', 'tenant', ARRAY['included', 'own']);
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('ai.plan_conversation', 'system', '"included"', 'provisional',
 '2026-11-05 ユーザー：「契約者の契約プランで選べるようにする。AI料金込みプランと、自分でAPIを契約するプラン」「会話は選べるように。」。'
 '契約者が選ぶ前の初めの値は、まだ決まっていない（仮）', '管理者に初めの値を伺ったとき', 'CL-2'),
('ai.plan_import', 'system', '"included"', 'provisional',
 '2026-11-05 ユーザー：「業務を取り込む専用のAIをAPI(opus5.5)で作成し、それに作らせるか、CL-1にやらせるのを選べる形」「契約者の契約'
 'プランで選べるようにする。AI料金込みプランと、自分でAPIを契約するプラン」。契約者が選ぶ前の初めの値は、まだ決まっていない（仮）',
 '管理者に初めの値を伺ったとき', 'CL-2');
