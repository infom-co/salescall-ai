-- 0114 お呼びした方が在籍していないとき、後任の方を追う（2026-10-22。CL-2）。書いた人：CL-2
--
-- ユーザー：「基本は後任へつないでもらう。選択可能にしたはずです」（ご決定＝Intent.md 9-4「在籍していない（退職・異動）：基本は後任の
-- 方をお呼びする＋業務ごとに選べる（お礼を言って終える）」）。前は、在籍していないと言われた通話のあと、業務の応じ方に関係なく行を
-- 終えていた。業務の応じ方が「後任の方をお呼びする」（既定）で、後任の方に繋がらないまま終わった通話のあとは、行を終えず、次の電話で
-- 「後任のご担当者様」をお呼びする。その印を次の予定に持たせる（successor）。印は、行が終わるまで引き継ぐ（後任の方と話せたあとのかけ直しでも、在籍していない方をお呼びしない）
-- （決めるのは core/next_action の choose_number の1か所。予約証 gate.Claim.successor で会話の処理へ渡す）。

ALTER TABLE salescall.task_next_actions ADD COLUMN successor boolean NOT NULL DEFAULT false;
ALTER TABLE salescall.task_next_actions
  ADD CONSTRAINT task_next_actions_successor_calls CHECK (NOT successor OR kind = 'call');

CREATE OR REPLACE VIEW salescall.current_next_actions WITH (security_invoker = true) AS
  SELECT n.* FROM salescall.task_next_actions n
  WHERE n.seq = (SELECT max(m.seq) FROM salescall.task_next_actions m WHERE m.task_id = n.task_id);
