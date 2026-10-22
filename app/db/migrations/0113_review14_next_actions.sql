-- 0113 見直し14：次の動き・判定・締めの誤りの直し（2026-10-22。CL-2）。書いた人：CL-2
--
-- (1) PC の部品が電話をかけなかった（PC の上の緊急停止・PC がつながっていない＝お客様の電話は鳴っていない）発信は、発信まで
--     進まなかった予約と同じ（取り消し released）にする。前は「発信に失敗」（call_failed）にしていたので、締めの処理が「会話に
--     至らなかった」と判定し、再発信の方針の回数を使い、目的（続き・番号の聞き直し）も消えた（見直し14b の (7)）。回線の部品が
--     「この番号はかけられない」と答えた失敗（CallNotPlaced）は、これまでどおり「発信に失敗」（判定して、方針の回数で止まる＝悪い
--     番号へかけ続けない）。移ってよい組に「発信中 → 取り消し」を足す（使うのは core/attempts.py の withheld の1か所）。
-- (2) 行の回数に数える発信の決まりを1か所に（salescall.counts_as_try）：発信まで進み、お客様の意思と関係なく繋がらなかった・
--     切れた発信（not_by_callee）でなく、お客様と約束したかけ直し（promised）でもない。門（行の回数の上限・1日の回数）と、
--     次の動きの再発信の方針の回数の両方が、これを読む（2026-10-14 ユーザー：「統一した方がよくない？」。前は方針の回数が、その
--     結果の出た通話をすべて数え、回線の側の失敗4回で行が終わった＝見直し14c の (1)）。
-- (3) 途中で切れた（お客様が出たあと、お客様の意思と関係なく切れた）かを、締めの処理が1回だけ試行に残す（cut_off）。決めるのは
--     judge.cut_off の1か所。続けて何回、途中で切れたか（すぐのかけ直しは続けて2回まで＝2026-10-18 ユーザー 案A）は、これを
--     数える（ふつうの電話・続き・お詫びの電話・番号の聞き直しで同じ決まり）。
-- (4) 次の予定のお詫びの印に理由を持たせる（apology_reasons：missed_promise＝約束の日（回）に一度もかけられなかった／
--     cut_off＝途中で切れた）。途中で切れたお客様へのすぐのかけ直しに出なかったときも、次の電話でお詫びから始める（2026-10-17
--     ユーザー：「復旧次第、お詫びとともにすぐかけ直さないといけないでしょ！」。前は続きの電話に出ないと、お詫びが消えた＝見直し
--     14b の (6)）。外すのは、目的のお客様と話せた締めだけ（core/next_action の _apologised の1か所）。
-- (5) 約束の回のうち、こちらが一度もかけられなかった回の数（window_missed）。約束の打ち止め（時刻ちょうどの約束の日数・幅の約束の
--     回数）には、こちらがかけた回だけを数える（2026-10-19 ユーザー：「約束を入れた電話は優先枠」「一度もかけられなかった場合は、
--     次につながった際には深くお詫び」。前は止まっていた間に過ぎた回も数え、一度もかけずに約束を打ち切った＝見直し14b の (1)）。

-- (1)
ALTER TABLE salescall.attempt_transitions DISABLE TRIGGER attempt_transitions_frozen;
INSERT INTO salescall.attempt_transitions (from_state, to_state) VALUES ('dialing', 'released');
ALTER TABLE salescall.attempt_transitions ENABLE TRIGGER attempt_transitions_frozen;

-- (2)
CREATE FUNCTION salescall.counts_as_try(s salescall.attempt_state, disconnect text, promised boolean, p_attempt uuid)
RETURNS boolean LANGUAGE sql STABLE AS $$
  SELECT salescall.is_dialed(s) AND NOT salescall.not_by_callee(s, disconnect, p_attempt) AND NOT promised
$$;
REVOKE ALL ON FUNCTION salescall.counts_as_try(salescall.attempt_state, text, boolean, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.counts_as_try(salescall.attempt_state, text, boolean, uuid) TO salescall_app;

-- (3)
ALTER TABLE salescall.attempts ADD COLUMN cut_off boolean;
ALTER TABLE salescall.attempts ADD CONSTRAINT attempts_cut_off_after_end CHECK (cut_off IS NULL OR NOT salescall.is_active(state));
CREATE FUNCTION salescall.attempt_cut_off_once() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.cut_off IS DISTINCT FROM OLD.cut_off AND OLD.cut_off IS NOT NULL THEN
    RAISE EXCEPTION '途中で切れたかは1回だけ決まります' USING ERRCODE = 'check_violation', CONSTRAINT = 'attempts_cut_off_once';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER attempt_cut_off_once BEFORE UPDATE OF cut_off ON salescall.attempts
  FOR EACH ROW EXECUTE FUNCTION salescall.attempt_cut_off_once();

-- (4)・(5)
ALTER TABLE salescall.task_next_actions
  ADD COLUMN apology_reasons text[] NOT NULL DEFAULT '{}',
  ADD COLUMN window_missed integer NOT NULL DEFAULT 0;
ALTER TABLE salescall.task_next_actions DISABLE TRIGGER task_next_actions_frozen;
UPDATE salescall.task_next_actions SET apology_reasons = ARRAY['missed_promise'] WHERE apology_owed;
ALTER TABLE salescall.task_next_actions ENABLE TRIGGER task_next_actions_frozen;
ALTER TABLE salescall.task_next_actions
  ADD CONSTRAINT task_next_actions_apology_reasons CHECK (
    apology_owed = (cardinality(apology_reasons) > 0)
    AND apology_reasons <@ ARRAY['missed_promise', 'cut_off']::text[]),
  ADD CONSTRAINT task_next_actions_window_missed CHECK (window_missed >= 0 AND window_missed <= window_round);

CREATE OR REPLACE VIEW salescall.current_next_actions WITH (security_invoker = true) AS
  SELECT n.* FROM salescall.task_next_actions n
  WHERE n.seq = (SELECT max(m.seq) FROM salescall.task_next_actions m WHERE m.task_id = n.task_id);
