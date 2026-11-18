-- 0160 別の耳が止まったら緊急の道で再開しない・甚大でないAIのおかしな動きは警告だけ（2026-10-31 管理者のご決定。書いた人：CL-2。
-- 計画書 10-3・18-3、設計書 9-1・2章 Q58、CL-2 の連絡帳 4-174）
--
-- ご決定：(1) 別の耳が止まっているあいだも、緊急の道で再開できるようにするか ⇒「『再開できない』でいいが、ダッシュボードになぜ再開
-- できないのかの警告を出すようにして下さい」 (2) 会話AIのおかしな動き1回で、その契約者の新しい電話を止めるか ⇒「甚大な問題は止める。
-- 甚大でない場合は、ダッシュボードに警告を出す」。
-- (1) は Python の処置の表（errors.ACTIONS の ear_stopped・errors.EMERGENCY_CANNOT）だけで決まる（データベースは種類の名前を持たない）。
-- (2) 止まったAIの出来事に「おかしな動き」（データの形 版 1.09）を足し、エラーの出どころ「AIのおかしな動き」（ai_glitch）を足す。
ALTER TABLE salescall.attempt_ai_troubles DROP CONSTRAINT attempt_ai_troubles_what_check;
ALTER TABLE salescall.attempt_ai_troubles ADD CONSTRAINT attempt_ai_troubles_what_check
  CHECK (what IN ('止まった', '緊急の道で答えた', 'おかしな動き'));
ALTER TABLE salescall.error_events DROP CONSTRAINT error_events_source_check;
ALTER TABLE salescall.error_events ADD CONSTRAINT error_events_source_check
  CHECK (source IN ('ai', 'ear', 'line', 'storage', 'delivery', 'recording', 'supervisor', 'judge', 'call', 'runner', 'ai_trouble',
                    'ai_glitch'));
