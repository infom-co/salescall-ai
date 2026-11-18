-- 0036 本番（フルオート）の品質の見方（2026-10-16 ユーザー決定）。書いた人：CL-2
-- ユーザー：「言ってはいけないことを言った・品質の基準を外れた・判定できない ⇒ 検証モードでやるべきこと。本番環境では、1件だけなら、
--   次の行に進む」「その日のうちに同じ原因が3件になったら、その業務を止め、管理者へ知らせる。件数は設定で変えられる：オッケー」
--   「段階的な立ち上げ（最初は20件→次に100件→全部）⇒これは検証モードのみ」。
-- 判断するのは core/quality_stage.py の1か所（evaluate）。この設定は、その件数だけ。
INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, bound_key, bound_side) VALUES
('quality.production_same_cause_per_day', 'override', 'integer',
 '本番（検証モードでない業務）で、その日のうちに同じ原因（言ってはいけないこと・判定できないの原因ごと）がこの件数になったら、その業務を止めて管理者へ知らせる',
 'DESIGN.md 10-1', NULL, NULL);
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('quality.production_same_cause_per_day', 'system', '3', 'decided',
 '2026-10-16 ユーザー決定：「その日のうちに同じ原因が3件になったら、その業務を止め、管理者へ知らせる。件数は設定で変えられる」', '運用の実績を見て',
 'ユーザー');
