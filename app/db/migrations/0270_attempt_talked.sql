-- 0270 その通話でお客様（人）と話したか attempts.talked（2026-11-20。書いた人：CL-2。DESIGN_15-2 の927。CL-3 の連絡帳 4-1c の 41・CL-2 の連絡帳 4-455）
--
-- ✅ 2026-11-20 管理者（CL-2 の伺い「今日話せた電話の数え方」）「「お客様と話した」と「目的のお客様と話せた」に加え、「留守番電話・自動音声」などを加えたトータル通話数も
-- ダッシュボードで出す」。⇒ 3つの数：電話に出た通話（応答まで進んだ＝salescall.reached_answer）・お客様と話した通話（この列）・目的のお客様と話せた通話（reached_target＝0203）。
-- 人と話したかの決め方は会話の報告の人の証の表（core/call_report.CallReport.talked）の1か所。締めの処理が、目的のお客様と話せたかと同じ取引で写す（守りも同じ）。
-- 0270 より前に締めた通話は NULL（数えない）。
ALTER TABLE salescall.attempts ADD COLUMN talked boolean;              -- お客様（人）と話したか（締めが書く。NULL＝まだ締めていない）
CREATE OR REPLACE FUNCTION salescall.attempt_reached_target_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (NEW.reached_target IS DISTINCT FROM OLD.reached_target OR NEW.talked IS DISTINCT FROM OLD.talked)
     AND NOT EXISTS (SELECT 1 FROM salescall.task_results r WHERE r.attempt_id = NEW.attempt_id AND r.decided_at = now()
                     AND (r.kind IN ('judged', 'rejudged') OR (r.kind = 'human' AND salescall.cur_actor_kind() = 'system')))
     AND NOT EXISTS (SELECT 1 FROM salescall.apology_outcomes o WHERE o.attempt_id = NEW.attempt_id AND o.recorded_at = now()) THEN
    RAISE EXCEPTION '目的のお客様と話せたか（reached_target）・お客様と話したか（talked）を書けるのは、その通話の締めの記録（結果の版・お詫びの電話の結び）を書く取引の中だけです'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'attempt_reached_target_by_settle';
  END IF;
  RETURN NEW;
END $$;
DROP TRIGGER attempt_reached_target_guard ON salescall.attempts;
CREATE TRIGGER attempt_reached_target_guard BEFORE UPDATE OF reached_target, talked ON salescall.attempts
  FOR EACH ROW EXECUTE FUNCTION salescall.attempt_reached_target_guard();
