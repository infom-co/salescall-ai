-- 0024 名前の読みを人が確かめるのは検証中の AI の案だけ・つなぎの一言の秒数（2026-10-16 ユーザー決定）。書いた人：CL-2
-- - names.ai_reading_by_person：AI が出した名前の読みの案を、人が確かめてから業務設定に使うか（Intent.md 6-8・DESIGN.md 3-7）。
--   ユーザー：「検証中はAIが認識したものを私が確認します」。人は介在しない仕組みなので、検証が終われば false にする。
--   入口（ファイル・依頼主の画面・画面の入力）が持ってきた読みは、確かめない。読むのは core/business_settings.py だけ（機械が見張る）。
-- - call.filler_after：相手が話し終えてから、相手に何も聞こえない時間がこの秒数続いたら、つなぎの一言を流す（CL-1 の連絡帳 4-13。
--   使うのは会話の担当 CL-1。DESIGN.md 6-15・6-7）。
INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, bound_key, bound_side) VALUES
('names.ai_reading_by_person', 'override', 'boolean', 'AI が出した名前の読みの案を、人が確かめてから業務設定に使うか（検証中は確かめる。入口が持ってきた読みは確かめない）', 'Intent.md 6-8・DESIGN.md 3-7', NULL, NULL),
('call.filler_after', 'override', 'seconds', '相手が話し終えてから、相手に何も聞こえない時間がこの秒数続いたら、電話接続モジュールが「はい、」を流す。そのあと、さらにこの秒数 AI の声が始まらなければ「少々お待ちいただいてもよろしいでしょうか？」（軽いほう）を流す', 'DESIGN.md 6-15・6-7（持ち主 CL-1）', NULL, NULL);
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('names.ai_reading_by_person', 'system', 'true', 'decided', '2026-10-16 ユーザー：「検証中はAIが認識したものを私が確認します」（計画書 6-8「AI がどれくらい読み違えるかを測ってから決める」への答え）', 'CL-1 が AI の読み違いを測り、検証を終えるとき（false にする）', 'ユーザー'),
('call.filler_after', 'system', '2', 'decided', '2026-10-16 ユーザー確定：「つなぎありの方が自然、2秒でOK」（CL-1 の連絡帳 4-13）', '模擬通話と最初の実測で、つなぎの一言が早すぎ・遅すぎにならないかを見て', 'ユーザー');
