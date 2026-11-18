-- 0127 「〇時まで」の言い方は、いま電話を続けられなかった理由で、かけてよい時間を決める（DESIGN.md 7-1d・7-2。2026-10-23。書いた人：CL-2）
--
-- ユーザー（2026-10-23）：「かけ直さないといけない理由によるでしょ。3時までに別の番号にかけてなら、すぐでいいし、3時まで出られない
-- なら、3時以降になるし、3時までに担当者へ聞いて折り返してなら、確認後になるだろうし」「『3時までなら大丈夫です』とかなら、なぜ、電話を
-- 切ったの？電話を切った理由があるから、電話を続けられなかったと考えるべきでしょ。その理由を確かめないと、何時から3時までの何時が
-- 決められないでしょ！」「別の番号・別の方へ：これは、すぐかけても問題ないんじゃない？」⇒ 理由と決め方の表に「OK」。
-- ⇒ 理由（会話の報告の callback.why と、それだけで決まらないときの callback.until_why＝call_report.Callback.until_reason の1か所）
--    ごとの、かけてよい時間の決め方を、業務ごとの設定にする（選べるのは calling_hours.UNTIL_RULES と同じ3つ）。幅を作るのは
--    時刻の読み（spoken_time.read）の1か所、設定を読むのはお客様の時間の設定（gate.callee_settings）の1か所。理由が分からなければ
--    決めずに、通話の中で確かめる（判定できない＝CL-1 へ修正依頼）。⛔ 前は理由を見ずに、いつも「今から〇時まで」にしていた。
INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, finest_scope, choices) VALUES
('callback.until_other_party', 'override', 'choice',
 'お客様が「〇時までにこちらの番号へ」のように、別の番号・別の方へのかけ直しを「〇時まで」と言ったときの、かけてよい時間です。'
 'now＝今から〇時まで（既定。別の電話なので、切った直後でもかけます）／after＝〇時から／slot_after＝〇時から1回分（callback.exact_slot）。',
 'DESIGN.md 7-1d', 'business', ARRAY['now', 'after', 'slot_after']),
('callback.until_unavailable', 'override', 'choice',
 'お客様が「〇時までには戻ります」「〇時まで会議です」のように、それまでは出られないと言ったときの、かけてよい時間です。'
 'after＝〇時から（既定。「〇時以降」と同じで、終わりはお客様指定の歯止め時間）／now＝今から〇時まで／slot_after＝〇時から1回分。',
 'DESIGN.md 7-1d', 'business', ARRAY['now', 'after', 'slot_after']),
('callback.until_preparing', 'override', 'choice',
 'お客様が「〇時までに担当者に確認しておきます」のように、それまでに確認・準備をしておくと言ったときの、かけてよい時間です。'
 'slot_after＝確認が済んだあと、〇時から1回分（callback.exact_slot。既定）／after＝〇時から／now＝今から〇時まで。',
 'DESIGN.md 7-1d', 'business', ARRAY['now', 'after', 'slot_after']);
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('callback.until_other_party', 'system', '"now"', 'decided',
 '2026-10-23 ユーザー：「3時までに別の番号にかけてなら、すぐでいい」「別の番号・別の方へ：これは、すぐかけても問題ないんじゃない？」',
 '業務ごとに', 'ユーザー'),
('callback.until_unavailable', 'system', '"after"', 'decided',
 '2026-10-23 ユーザー：「3時まで出られないなら、3時以降になる」「『3時までには戻ります。』とか」',
 '業務ごとに', 'ユーザー'),
('callback.until_preparing', 'system', '"slot_after"', 'decided',
 '2026-10-23 ユーザー：「3時までに担当者へ聞いて折り返してなら、確認後になるだろう」⇒ 3時から1回分（表に「OK」）',
 '業務ごとに', 'ユーザー');
