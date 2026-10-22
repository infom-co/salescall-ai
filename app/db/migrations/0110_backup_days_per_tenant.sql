-- 0110 データベースの控えを残す日数を、契約者ごとに変えられるようにする（2026-10-22 ユーザー決定）。書いた人：CL-2
--
-- ユーザー（2026-10-22）：「設定で契約者が変えられるようにしてください」。
-- - 前は、AI用の控え（ハードディスク 365日・Fly.io 30日）と提出用の控え（ハードディスク 1,095日）の日数が、システム全体で1つの値
--   だった（録音の日数は、2026-10-17 から契約者ごと）。
-- - いまは、録音と同じく契約者ごと（契約者が設定で変える。決めていなければシステムの値＝初めの値は前のまま）。読むのは
--   core/backup.py の prune_rules の1か所（その契約者の依頼者のフォルダは、その契約者の値。データベース全体のフォルダはシステムの値）。
-- - 1日より短くはできない（0057 の setting_values_keep_days_at_least_one がそのまま守る）。
ALTER TABLE salescall.setting_definitions DISABLE TRIGGER setting_definitions_frozen;
UPDATE salescall.setting_definitions SET finest_scope = 'tenant', meaning = v.meaning FROM (VALUES
  ('backup.ai.keep_days_drive',
   'AI用の控え（システムが使っているデータ）を、PC に接続したハードディスクに残す日数（過ぎたら消す。契約者ごと）'),
  ('backup.ai.keep_days_cloud',
   'AI用の控えを Fly.io に残す日数（Fly.io の置き場の毎日の写しを残す日数。契約者ごと）'),
  ('backup.submission.keep_days_drive',
   '提出用の控え（依頼者へ渡した結果のファイル）を、PC に接続したハードディスクに残す日数（Fly.io には残さない。契約者ごと）')
) AS v(key, meaning) WHERE salescall.setting_definitions.key = v.key;
ALTER TABLE salescall.setting_definitions ENABLE TRIGGER setting_definitions_frozen;
