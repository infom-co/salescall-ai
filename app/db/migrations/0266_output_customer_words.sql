-- 0266 設定「お客様の言葉の書き方」output.customer_words（2026-11-20。書いた人：CL-2。DESIGN_15-2 の918。CL-3 の連絡帳 4-1c の 54・CL-2 の連絡帳 4-445）
--
-- ✅ 2026-11-20 管理者（CL-3 の伺い）：取れなかった答えの欄に添えるお客様の言葉を 40字で切るのは「場合によっては残した方がいいね」・「ウ」（選べるようにする）・「業務ごと」。
-- ⇒ 業務ごとに選ぶ設定（short＝縮める＝最後の2つの発話まで・1つ40字まで／full＝全部残す）。下が勝つ（override）。システムの初めの値は short。
--    読むのは CL-3 の出力の1か所（output/result_rows.words_mode）。
INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, finest_scope, choices) VALUES
('output.customer_words', 'override', 'choice',
 '依頼者へ渡す結果で、取れなかった答えの欄に添えるお客様の言葉の書き方です。short＝縮める（初め。最後の2つの発話まで・1つ40字まで）／full＝全部残す（その答えの発話をすべて）。',
 'DESIGN.md 7-3-2', 'business', ARRAY['short', 'full']);
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('output.customer_words', 'system', '"short"', 'decided',
 '2026-11-20 管理者（CL-3 の伺い）：40字で切るのは「場合によっては残した方がいいね」・「ウ」（選べるようにする）・「業務ごと」。初めは今の縮め方',
 '業務ごとに契約者が選ぶとき', 'ユーザー');
