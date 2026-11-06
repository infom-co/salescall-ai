-- 0187 お詫びの理由「途中で切れた」と、どの電話についてのお詫びかを一緒に持つ（2026-11-06 CL-2 の見直し。書いた人：CL-2）
--
-- 0186 で、途中で切れた電話の続きの予約証に「前の電話が今日か」（apology_when）を入れたとき、前の電話を「その行でいちばん新しい、
-- 発信まで進んだ電話」で決めていた（core/apology.when）。⛔ 思い違い：お詫びするのは「切れた電話」。昨日切れたあと、今日かけて
-- 出なかった電話があると、今日の電話を見て「先ほどは…お電話が切れてしまい」と言ってしまう。
-- ⇒ お詫びの理由 cut_off を付ける所（core/next_action：途中で切れた通話・切れ方の分からなかった通話の確かめのかけ直しを決めた
--   通話＝decide_after_call、確かめのかけ直しが鳴らなかった＝_check_unrung）が、その電話（試行）を次の予定の cut_call に書き、
--   次の予定が代わっても理由と一緒に引き継ぐ（_append の1か所）。理由 cut_off があるときだけ、必ず持つ（下の守り）。
--   同じ日かは、この電話の日で決める（core/apology.when）。

-- その行の試行だけを指せるように（試行の番号は一意なので、行と組にしても一意）
ALTER TABLE salescall.attempts ADD CONSTRAINT attempts_task_attempt UNIQUE (task_id, attempt_id);

ALTER TABLE salescall.task_next_actions ADD COLUMN cut_call uuid;
ALTER TABLE salescall.task_next_actions ADD CONSTRAINT task_next_actions_cut_call_of_task
  FOREIGN KEY (task_id, cut_call) REFERENCES salescall.attempts (task_id, attempt_id);
COMMENT ON COLUMN salescall.task_next_actions.cut_call IS
  'お詫びの理由 cut_off（途中で切れた）の元の電話（試行）。理由 cut_off があるときだけ持つ。書くのは core/next_action._append だけ';

-- 前からある予定：理由 cut_off の元の電話を、その予定を決めたときまでの、その行でいちばん新しい「切れた電話」か「切れ方の分からなかった
-- 通話の確かめを決めた電話」にする（どちらも理由 cut_off を付ける電話だけ）。見つからない予定は、分かる言葉で止める（本番の
-- データベースはまだ無い）。予定は変えられない表なので、この移し替えの中だけ仕掛けを外す。
ALTER TABLE salescall.task_next_actions DISABLE TRIGGER task_next_actions_frozen;
UPDATE salescall.task_next_actions n SET cut_call = (
  SELECT a.attempt_id FROM salescall.attempts a
   WHERE a.task_id = n.task_id AND a.claimed_at <= n.decided_at
     AND (a.cut_off IS TRUE OR EXISTS (SELECT 1 FROM salescall.unclear_checks u WHERE u.attempt_id = a.attempt_id))
   ORDER BY a.claimed_at DESC LIMIT 1)
 WHERE 'cut_off' = ANY (n.apology_reasons);
ALTER TABLE salescall.task_next_actions ENABLE TRIGGER task_next_actions_frozen;
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM salescall.task_next_actions WHERE 'cut_off' = ANY (apology_reasons) AND cut_call IS NULL) THEN
    RAISE EXCEPTION '0187：お詫びの理由「途中で切れた」の元の電話が分からない予定があります（手で確かめてから移し替えてください）';
  END IF;
END $$;

ALTER TABLE salescall.task_next_actions ADD CONSTRAINT task_next_actions_cut_call
  CHECK (('cut_off' = ANY (apology_reasons)) = (cut_call IS NOT NULL));

-- いまの予定の見方に、新しい列を入れる（n.* は作ったときの列だけなので作り直す。列は後ろに足す）
CREATE OR REPLACE VIEW salescall.current_next_actions WITH (security_invoker = true) AS
  SELECT n.* FROM salescall.task_next_actions n
  WHERE n.seq = (SELECT max(m.seq) FROM salescall.task_next_actions m WHERE m.task_id = n.task_id);
