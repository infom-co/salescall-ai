-- 0165 2026-11-01 見直し28（main に入れる前の見直し）の直し。書いた人：CL-2。設計書 9-1・17章・15-2 の400〜。
--
-- (1) AIの停止を解く確かめの回数（stop.ai.resume_after_successes）を 2 から 1 にする。ご決定は「確かめが通れば自動で再開します」
--     （2026-10-31 管理者：別の耳が止まったとき。2026-10-30 深夜 管理者：「AI の不具合として回復するまで一時停止してください」）、会話の側の
--     回復の確かめも「全部 ok なら回復」（CL-1 の連絡帳 4-62 の (b)）。⛔ 2回は CL-2 が仮に決めた値で、通った確かめを1回むだに待たせていた。
--     設定の値は足すだけの記録なので、新しい値を足す（いちばん新しいシステムの値が初めの値＝core/settings.initial_value）。
-- (2) テスト番号をあける長さ（line.test_ring_gaps）の説明を、いまの動きに合わせる：数えるのは回線ごと（その回線で続けて鳴らなかった・
--     見分けられなかった回数で並びを進める＝core/health_rules._choose_wait）。⛔ 0139・0141 の説明は「どの回線で鳴らしたかによりません」と
--     書いていた（契約者が画面で読む説明と動きが違った）。値は変えない。
-- (3) 名前の録音の台帳の続く言葉に unavailable_confirm を足す（CL-1 が main で足した「（お名前）様は今はお電話に出られない状況という
--     ことでよろしいでしょうか？」＝受付の方に理由を言わずに取り次げないと言われたときの確かめの一言。2026-11-01 ユーザーのご指示。
--     名前の正本は CL-1 の instruction.Business.replay_parts。Python の一覧は core/recordings.NAME_TAILS）。⛔ 前は守りに無く、台帳に
--     録音を入れられず、その一言を流せなかった。

-- ═════════ (1) AIの停止を解く確かめの回数 ═════════
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('stop.ai.resume_after_successes', 'system', '1', 'decided',
 '2026-10-31 管理者：「確かめが通れば自動で再開します」。2026-10-30 深夜 管理者：「AI の不具合として回復するまで一時停止してください」。'
 '会話の側の回復の確かめは、使うAIすべてが答えれば回復（CL-1 の連絡帳 4-62 の (b)）。前の 2 は CL-2 の仮の値だった',
 '管理者が変えるとき', 'ユーザー');

-- ═════════ (2) テスト番号をあける長さの説明 ═════════
ALTER TABLE salescall.setting_definitions DISABLE TRIGGER setting_definitions_frozen;
UPDATE salescall.setting_definitions
SET meaning = 'テスト番号が鳴らなかった・見分けられなかったとき、その回線で次にテスト番号を鳴らしてよいまであける長さ（秒）の並びです。'
              '回線ごとに数えます：その回線で続けて1回目のあとは並びの1つ目、2回目のあとは2つ目…と進み、最後の長さで止まります（ほかの回線が'
              'テスト番号を鳴らしているあいだは、その答えが出るまで待ちます＝テスト番号の電話は1台）。鳴ったら（話し中・電源オフ・圏外のお知らせが'
              '返ったときも＝回線から発信できた）、テストはOKで終わり、あける長さはありません（次に要るとき、すぐ鳴らします）。初めは 15分→30分→'
              '60分→120分→240分（2026-10-25 ユーザー：「15⇒30⇒60⇒120⇒240分　がデフォルト契約者ごとに設定」）。並びの1つ目は、回線のせいか'
              '番号のせいかがまだ分からないとき、その回線で次にかける電話の結果を待つ長さにも使います（2026-10-25 夜 ユーザー：「その回線で次に'
              'かけるのは15分後と決めたんだから、15分になるんじゃないの？新たな設定はいらないでしょ？」）'
WHERE key = 'line.test_ring_gaps';
ALTER TABLE salescall.setting_definitions ENABLE TRIGGER setting_definitions_frozen;

-- ═════════ (3) 名前の録音の台帳の続く言葉 ═════════
ALTER TABLE salescall.recording_sets DROP CONSTRAINT recording_sets_name_key;
ALTER TABLE salescall.recording_sets
  ADD CONSTRAINT recording_sets_name_key CHECK (CASE unit
    WHEN 'name_role' THEN btrim(coalesce(name_role, '')) <> '' AND name_reading IS NULL AND tail IS NULL
    WHEN 'name' THEN tail IS NULL AND ((name_reading ~ '^[ぁ-ゖー・]+$' AND name_role IS NULL)
                                       OR (name_reading IS NULL AND btrim(coalesce(name_role, '')) <> ''))
    WHEN 'name_tail' THEN tail IN ('target', 'target_individual', 'target_confirm', 'target_reconfirm', 'promise_again',
                                   'target_successor', 'unavailable_confirm')
                          AND name_role IS NULL AND name_reading IS NULL
    ELSE name_role IS NULL AND name_reading IS NULL AND tail IS NULL END);
