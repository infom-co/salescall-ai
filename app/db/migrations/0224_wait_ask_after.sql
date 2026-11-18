-- 0224 理由を言わずに待つ方へ、待つかかけ直すかを伺うまでの秒（2026-11-15。書いた人：CL-2。CL-1 の連絡帳 4-167 の (3)・DESIGN.md 15-2 の816）
--
-- 黙ったときに何秒で何を流すかは、会話の側の流れの制御の1か所（CL-1 の flowctl.QUIET_STEPS・Flow.on_silence）が決める。秒の値を持つのは設定の表の1か所。
-- (1) 新しい設定 call.wait_ask_after：理由を言わずに「待ってて」と言われ、1つ目の一言（「何か気になる点が…」）のあとも黙られたときに、待つかかけ直すかを
--     伺うまでの秒。call.silence_prompt_after と同じ形（業務ごと＝契約者が依頼者タブ・業務ごとに変えられる）。
--     ✅ 2026-11-15 日本時間23時08分40秒 ユーザー「伺い28：ア」＝20秒（初めの値・契約者が依頼者ごとに変えられる）。
-- (2) call.silence_prompt_after の説明を、今の使われ方に合わせる（前は「考えている相手への『いかがなさいましたか？』」だけを書いていた。今は、考えている途中・
--     ご承認のお知らせのあと・お返事を待つ間・理由を言わずに待つ間の、黙ったときの一言のすべてで使う）。値は変えない。

INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, finest_scope) VALUES
('call.wait_ask_after', 'override', 'seconds',
 'お客様が理由を言わずに「待ってて」と言われ、黙ったときの1つ目の一言（気になる点を伺う一言）のあとも黙っていらっしゃるときに、このまま待つか、'
 '改めてかけ直すかを伺うまでの秒数（1つ目の一言を流し終えてから数える）。1つ目の一言までの秒数は、黙ったときの一言を流すまでの秒数'
 '（call.silence_prompt_after）で決めます。流す言葉は会話の側が決めます',
 'DESIGN.md 6-15（持ち主 CL-1）', 'business');
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('call.wait_ask_after', 'system', '20', 'decided', '2026-11-15 日本時間23時08分40秒 ユーザー「伺い28：ア」（CL-1 の連絡帳 4-167 の (3)）：'
 '20秒（初めの値・契約者が依頼者ごとに変えられる）', '模擬通話と最初の実測で、待つと言われたあとの間を見て', 'ユーザー');

ALTER TABLE salescall.setting_definitions DISABLE TRIGGER setting_definitions_frozen;
UPDATE salescall.setting_definitions SET meaning =
 'お客様が黙ったときに、1つ目の一言（気になる点を伺う一言）を流すまでの秒数（AI の返事・一言を流し終えてから数える）。考えている途中・'
 'ご承認のお知らせのあと・お返事を待つ間・理由を言わずに待つ間のどれでも使います。そのあとも同じ秒数だけ黙っていらっしゃれば、2つ目の一言'
 '（どうなさったかを伺う一言）を流します。理由を言わずに待つ間の2つ目（待つかかけ直すかを伺う）までの秒数だけは call.wait_ask_after で'
 '決めます。流す言葉は会話の側が決めます'
WHERE key = 'call.silence_prompt_after';
ALTER TABLE salescall.setting_definitions ENABLE TRIGGER setting_definitions_frozen;
