-- 0262 「〇時までなら大丈夫です」の読み方の設定 callback.until_available（2026-11-20。書いた人：CL-2。DESIGN_15-2 の914。CL-1 の連絡帳 4-212・CL-2 の連絡帳 4-445）
--
-- 会話の側は「3時までなら大丈夫です」と言われたとき、理由を伺い直し（「…それ以降はお電話が難しいということでしょうか？」）、「はい」なら「〇時まで」の理由を
-- available_until（それまでなら出られる）で報告する（✅ 2026-11-20 ユーザー「伺い63：ア」）。理由ごとの読み方は業務ごとの設定の1か所（0127 と同じ形。
-- 読むのは gate.callee_settings・幅を作るのは spoken_time.read）。それまでなら出られるので、既定は now（今から〇時まで＝それより前にかける）。
INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, finest_scope, choices) VALUES
('callback.until_available', 'override', 'choice',
 'お客様が「〇時までなら大丈夫です」のように、それまでなら出られる（それ以降はお電話が難しい）と言ったときの、かけてよい時間です。'
 'now＝今から〇時まで（既定。それより前にかけます）／after＝〇時から／slot_after＝〇時から1回分（callback.exact_slot）。',
 'DESIGN.md 7-1d', 'business', ARRAY['now', 'after', 'slot_after']);
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('callback.until_available', 'system', '"now"', 'decided',
 '2026-11-20 ユーザー「伺い63：ア」（「〇時までなら大丈夫です」の理由を伺い直し「それ以降はお電話が難しい」に「はい」＝それまでなら出られる＝それより前にかける）',
 '業務ごとに', 'ユーザー');
