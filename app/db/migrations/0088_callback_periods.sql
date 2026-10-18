-- 0088 かけ直しの時間の言葉ごとの幅を、依頼者ごとの設定にする（DESIGN.md 7-1d・15-2 の185。2026-10-19。書いた人：CL-2）
--
-- 2026-10-19 に「言葉ごとの幅は、依頼者ごとに変えられる設定にする予定」とユーザーに伝え、幅（朝〜10時・午前中〜12時・午後13時〜・
-- 夕方16〜18時・夜18〜20時）は「OK」をいただいたのに、作りではプログラムに書き込んでいた（見直しで見つけた食い違い）。
-- ⇒ 依頼者ごとの設定（1日の中の時刻の範囲）にする。読むのは core/spoken_time（判定が設定を渡す）。終わりが始まりより前
--   （「13:00」〜「00:00」）は、その日の終わりまで（日をまたいでは読まない＝0時を過ぎた分は使わない）。
INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, finest_scope) VALUES
('callback.period_morning', 'override', 'daily_range',
 'お客様が「朝」「朝一」と言ったかけ直しの幅（1日の中の時刻。かけてよい時間帯と重なる所だけ使う）', 'DESIGN.md 7-1d', 'client'),
('callback.period_forenoon', 'override', 'daily_range',
 'お客様が「午前」「午前中」と言ったかけ直しの幅', 'DESIGN.md 7-1d', 'client'),
('callback.period_afternoon', 'override', 'daily_range',
 'お客様が「午後」と言ったかけ直しの幅（「午後一」は、その始まりから1時間）', 'DESIGN.md 7-1d', 'client'),
('callback.period_evening', 'override', 'daily_range',
 'お客様が「夕方」と言ったかけ直しの幅', 'DESIGN.md 7-1d', 'client'),
('callback.period_night', 'override', 'daily_range',
 'お客様が「夜」「晩」「今晩」と言ったかけ直しの幅', 'DESIGN.md 7-1d', 'client');
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('callback.period_morning', 'system', '["00:00", "10:00"]', 'decided', '2026-10-19 ユーザー「OK」：朝〜10時', '依頼者ごとに', 'ユーザー'),
('callback.period_forenoon', 'system', '["00:00", "12:00"]', 'decided', '2026-10-19 ユーザー「OK」：午前中〜12時', '依頼者ごとに', 'ユーザー'),
('callback.period_afternoon', 'system', '["13:00", "00:00"]', 'decided', '2026-10-19 ユーザー「OK」：午後13時〜（その日の終わりまで）', '依頼者ごとに', 'ユーザー'),
('callback.period_evening', 'system', '["16:00", "18:00"]', 'decided', '2026-10-19 ユーザー「OK」：夕方16〜18時', '依頼者ごとに', 'ユーザー'),
('callback.period_night', 'system', '["18:00", "20:00"]', 'decided', '2026-10-19 ユーザー「OK」：夜18〜20時', '依頼者ごとに', 'ユーザー');
