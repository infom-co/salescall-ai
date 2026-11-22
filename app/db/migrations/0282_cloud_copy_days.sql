-- 0282 サーバーの置き場まるごとの毎日の写しを残す日数の決め直し（2026-11-22。書いた人：CL-2。DESIGN_15-2.md の953。DESIGN.md 3-10。
--      ⏳ 2026-10-23 ユーザー「控えを Fly.io に残す日数：現状のハードディスクの控えの仕組みを確認しながら、再度決め直そう」の答え）
-- ⛔ 前の書き方：設定 backup.ai.keep_days_cloud を「AI用の控えを Fly.io に残す日数」と書いていた。けれども Fly.io の毎日の写しは置き場まるごと（データベース
--    全体）の写しで、日数は1つしか持てない。写しの中には、お客様と AI のやり取り（通話の録音・やり取りの文字。置き場からは recording.keep_days_cloud＝
--    初め7日で消す）も、ハードディスクへ写す前の数分の提出用のファイルも入る（説明を読んだ契約者が、写しに何が入るかを取り違えた）。
-- ✅ 2026-11-22 管理者（日本時間の朝）：「録音とはどの録音の事を言っているの。こちらの業務での発信につかう録音ならずっと残さないといけないし、お客様と
--    AIとのやり取りは7日程度で、その後はHDで大丈夫。」・「Fly.io に1か月分でも十分に無料の範囲内で使えるなら、1か月でいいし、容量が不足しそうなら、
--    7日でもOK」
--    ＝(1) 業務で流す録音（recording_audio）はずっと残す（いまの作りのまま＝0275 の recording_audio_frozen が消させない。写しの日数とは関係ない）
--      (2) お客様と AI のやり取りは、置き場に7日（いまの recording.keep_days_cloud のまま）・そのあとはハードディスク
--      (3) 写しの日数は30日のまま。Fly.io に出すとき、無料の範囲と置き場の大きさを確かめ、足りなければ7日にする（見直しの合図に書く）
ALTER TABLE salescall.setting_definitions DISABLE TRIGGER USER;
UPDATE salescall.setting_definitions d SET meaning = v.meaning, help = v.help FROM (VALUES
  ('backup.ai.keep_days_cloud',
   'サーバーの置き場まるごと（データベース全体）の毎日の写しを残す日数（Fly.io の置き場の写しの設定に写す。写しにはお客様と AI のやり取り・業務の記録も入る）',
   'サーバーの置き場まるごとの毎日の写しを残す日数です。写しには、業務の記録のほか、お客様とのやり取り（通話の録音・やり取りの文字）も入ります（やり取りそのものは、決めた日数でサーバーから消えます）。'),
  ('recording.keep_days_cloud',
   'お客様と AI のやり取り（通話の録音・やり取りの文字）を Fly.io の置き場に残す日数（契約者ごと。過ぎたら消す。業務で流す録音は消さない）',
   'お客様とのやり取り（通話の録音・やり取りの文字）を、サーバーの置き場に残す日数です。過ぎたらサーバーから消します（業務で電話に流す録音は消しません）。')
  ) AS v(key, meaning, help)
 WHERE d.key = v.key;
ALTER TABLE salescall.setting_definitions ENABLE TRIGGER USER;
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('backup.ai.keep_days_cloud', 'system', '30', 'decided',
 '2026-11-22 管理者：「Fly.io に1か月分でも十分に無料の範囲内で使えるなら、1か月でいいし、容量が不足しそうなら、7日でもOK」（置き場まるごとの写し。お客様と AI のやり取りそのものは置き場から7日で消す＝recording.keep_days_cloud）',
 'Fly.io に出すとき（写しの無料の範囲と置き場の大きさを確かめ、足りなければ7日にする）', 'ユーザー');
