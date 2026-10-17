-- 0046 通話後の確かめは、済むまで待つ（「確かめられなかった」で行を終える決まりを外す。2026-10-17。書いた人：CL-2）
--
-- ユーザー（2026-10-17）：「通話後の確かめができないとはどういうこと？そんな状態自体が問題では？」
-- ⇒ 確かめができないのは、仕組みの不具合だけ（別の耳につながらない・別の耳の誤り・録音が残っていない・確かめの処理の誤り）。
--   通話の出来事（お客様がどうしたか）ではないので、通話の結果として行を終えない。不具合はエラーの表（core/errors.py）が扱い、
--   確かめは「待ち」のまま、直ってからやり直す。待っているあいだは、門が同じお客様にかけない（断られたかが決まっていない）。
--   判定できないときの扱い（2026-10-16 ユーザー決定：終えずに残し、原因が直ったら判定し直す）と同じ1つの決まりにする。
-- 前は、決まった回数やり直しても駄目なら「確かめられなかった」として、その行を終えていた（2026-10-13。人を待たない代わりの決まり）。
-- そのため、仕組みの不具合で、見込みのあるお客様の行が黙って終わり、品質の割合（quality.max_verification_failed）でしか見えなかった。

-- 確かめの状態は「待ち」から「済み」へだけ（型の「確かめられなかった」は、型から外せないので残るが、使わない）
ALTER TABLE salescall.attempts ADD CONSTRAINT attempts_verification_never_gives_up
  CHECK (verification IS DISTINCT FROM 'failed');

-- 使わなくなった設定：やり直しの上限（上限で諦めない）と、確かめられなかった割合の上限（その状態が無い）
ALTER TABLE salescall.setting_values DISABLE TRIGGER setting_values_frozen;
ALTER TABLE salescall.setting_definitions DISABLE TRIGGER setting_definitions_frozen;
DELETE FROM salescall.setting_values WHERE key IN ('verification.max_retries', 'quality.max_verification_failed');
DELETE FROM salescall.setting_definitions WHERE key IN ('verification.max_retries', 'quality.max_verification_failed');
UPDATE salescall.setting_definitions
SET meaning = '通話後の検証を、仕組みの不具合（別の耳につながらない・誤り・録音が無い）のあとにやり直すまでの待ち（上限は無い：済むまでやり直す）'
WHERE key = 'verification.retry_wait';
ALTER TABLE salescall.setting_definitions ENABLE TRIGGER setting_definitions_frozen;
ALTER TABLE salescall.setting_values ENABLE TRIGGER setting_values_frozen;

-- 録音が残っていないこと（通話後の確かめができない原因の1つ）を、エラーの出どころ「録音」として記録できるようにする
-- （処置はエラーの表 core/errors.py の recording：その回線を止め、管理者に直しを依頼する）
ALTER TABLE salescall.error_events DROP CONSTRAINT error_events_source_check;
ALTER TABLE salescall.error_events ADD CONSTRAINT error_events_source_check
  CHECK (source IN ('ai', 'ear', 'line', 'storage', 'delivery', 'recording'));
