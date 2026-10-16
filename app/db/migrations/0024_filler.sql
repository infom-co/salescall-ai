-- 0024 つなぎの一言の秒数（2026-10-16 ユーザー決定）。書いた人：CL-2
-- - call.filler_after：相手が話し終えてから、相手に何も聞こえない時間がこの秒数続いたら、つなぎの一言を流す（CL-1 の連絡帳 4-13。
--   使うのは会話の担当 CL-1。DESIGN.md 6-15・6-7）。
-- - （2026-10-16 の同じ日に、名前の読みの確かめの設定 names.ai_reading_by_person をここに置いたが、検証モード（approval.human）と
--   切り替えを1つにする（ユーザー承認）ため外した。この移行はまだどのデータベースにも流していない＝書き換えてよい段階だった）
INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, bound_key, bound_side) VALUES
('call.filler_after', 'override', 'seconds', '相手が話し終えてから、相手に何も聞こえない時間がこの秒数続いたら、電話接続モジュールが「はい、」を流す。そのあと、さらにこの秒数 AI の声が始まらなければ「少々お待ちいただいてもよろしいでしょうか？」（軽いほう）を流す', 'DESIGN.md 6-15・6-7（持ち主 CL-1）', NULL, NULL);
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('call.filler_after', 'system', '2', 'decided', '2026-10-16 ユーザー確定：「つなぎありの方が自然、2秒でOK」（CL-1 の連絡帳 4-13）', '模擬通話と最初の実測で、つなぎの一言が早すぎ・遅すぎにならないかを見て', 'ユーザー');
