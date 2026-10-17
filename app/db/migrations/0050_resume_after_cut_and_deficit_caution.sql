-- 0050 途中で切れた通話は、お詫びとともにすぐかけ直す・赤字の知らせの慎重さを、初めての契約者にも分かる3つの選び方にする
-- （2026-10-17 ユーザー決定。書いた人：CL-2）
--
-- (1) ユーザー：「仕組みが途中で止まった通話や、回線が途中で切れた通話は復旧次第、お詫びとともにすぐかけ直さないといけないでしょ！」
--     ⇒ 発信の目的に「続き」（resume）を足す：お客様が出たあと、お客様の意思と関係なく切れた通話（こちらの仕組みが止まった・
--       回線が切れた）のあと、すぐ（復旧次第＝門が通す最も早い時刻）同じ番号へかける。会話は「先ほどは途中でお電話が切れてしまい、
--       申し訳ございません」のお詫びから始めて続ける（言い方・録音は CL-1）。回数・間隔には数えない（約束したかけ直しと同じ）。
--       決めるのは次の動きの1か所（next_action.decide_after_call）。
-- (2) ユーザー：「『0.95』も分かりにくいし、『取り違えを20回に1回より少なく』は何を言っているのかすら分からない。初めての契約者でも
--     分かるように！」⇒ 報酬の決まりには、数ではなく選び方（deficit_caution）を持つ：
--       early（早めに知らせる）／normal（ふつう）／careful（よく確かめてから知らせる）。
--     中の数（確かさ 0.80／0.95／0.99）は処理の1か所（core/reward.py の CAUTIONS）だけが持つ。前の judge_confidence は外す。

-- ═════════ (1) 発信の目的「続き」 ═════════
ALTER TABLE salescall.attempts DROP CONSTRAINT attempts_purpose_kind;
ALTER TABLE salescall.attempts
  ADD CONSTRAINT attempts_purpose_kind CHECK (purpose IN ('normal', 'ask_number_again', 'apology', 'resume'));
ALTER TABLE salescall.task_next_actions DROP CONSTRAINT task_next_actions_purpose_kind;
ALTER TABLE salescall.task_next_actions
  ADD CONSTRAINT task_next_actions_purpose_kind CHECK (purpose IS NULL OR purpose IN ('normal', 'ask_number_again', 'apology', 'resume'));

-- ═════════ (2) 赤字の知らせの慎重さ ═════════
ALTER TABLE salescall.reward_terms DISABLE TRIGGER reward_terms_frozen;
ALTER TABLE salescall.reward_terms
  ADD COLUMN deficit_caution text CHECK (deficit_caution IN ('early', 'normal', 'careful'));
UPDATE salescall.reward_terms
SET deficit_caution = CASE WHEN judge_confidence < 0.9 THEN 'early' WHEN judge_confidence < 0.98 THEN 'normal' ELSE 'careful' END;
ALTER TABLE salescall.reward_terms ALTER COLUMN deficit_caution SET NOT NULL;
ALTER TABLE salescall.reward_terms DROP COLUMN judge_confidence;
ALTER TABLE salescall.reward_terms ENABLE TRIGGER reward_terms_frozen;
