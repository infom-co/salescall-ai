-- 0112 想定問答の答えの録音の部分の名前を、日本語の問いの名前のまま受け付ける（2026-10-22 の見直し13）。書いた人：CL-2
--
-- ⛔ 2026-10-22 の見直し13：会話の側（CL-1）は、想定問答の答えの録音の部分を「answer_ ＋ 問いの名前」（例 answer_番号の出どころ）
-- にする（instruction.Business.replay_parts）。業務設定の確かめ（core/business_settings.py の _answers）は日本語の問いの名前を通すのに、
-- 録音の部分の名前の決まり（0103：英数字・「_」「-」だけ）が断ったので、その業務の単位の録音の組が丸ごと作れなかった（録音で話す
-- 業務は、かけられなくなった）。
-- ⇒ 部分の名前は、今までの英数字の名前か、「answer_ ＋ 問いの名前の決まり」（業務設定と同じ：30文字まで・Windows のファイル名に
--   使えない文字 \ / : * ? " < > | と制御文字を使わない）のどちらか。
ALTER TABLE salescall.recordings DROP CONSTRAINT recordings_part_check;
ALTER TABLE salescall.recordings ADD CONSTRAINT recordings_part_check
  CHECK (part ~ '^[A-Za-z0-9_-]{1,64}$' OR part ~ '^answer_[^\\/:*?"<>|[:cntrl:]]{1,30}$');
