-- 0211 目的のお客様と話せたかの守り・どの場面でも効かない条件を消す（2026-11-08。書いた人：CL-2。データベースの監査＝見直し K の (c)(d)。
--      DESIGN.md 3-4-1 の【0210】・15-2 の690〜）
--
-- (1) 目的のお客様と話せたか（attempts.reached_target。引き継ぎを確かめる印が読む＝0203）に守りが無く、どこからでも書き換えられた。
--     ⇒ 書き換えられるのは、その通話の締めの記録（結果の版・お詫びの電話の結び）を書く取引の中だけ（守りの1か所 attempt_reached_target_guard。
--        同じ取引かは、その記録の時刻が取引の時刻＝now() と同じか。0201 の client_solution_removal_guard と同じ見方）。Python で書くのは
--        締めの処理（core/settle）だけ（決まりの機械の見張り PY_ENTRY_CALLERS）で、締めの記録を書いたあとに書く。
-- (2) どの場面でも効かない条件を消す（消す前に、効かないことを確かめた。理由は条件ごとに書く）。

-- ═════════ (1) 目的のお客様と話せたかの守り ═════════
CREATE FUNCTION salescall.attempt_reached_target_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.reached_target IS DISTINCT FROM OLD.reached_target
     AND NOT EXISTS (SELECT 1 FROM salescall.task_results r WHERE r.attempt_id = NEW.attempt_id AND r.decided_at = now())
     AND NOT EXISTS (SELECT 1 FROM salescall.apology_outcomes o WHERE o.attempt_id = NEW.attempt_id AND o.recorded_at = now()) THEN
    RAISE EXCEPTION '目的のお客様と話せたか（reached_target）を書けるのは、その通話の締めの記録（結果の版・お詫びの電話の結び）を書く取引の中だけです'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'attempt_reached_target_by_settle';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER attempt_reached_target_guard BEFORE UPDATE OF reached_target ON salescall.attempts
  FOR EACH ROW EXECUTE FUNCTION salescall.attempt_reached_target_guard();

-- ═════════ (2) どの場面でも効かない条件を消す ═════════
-- いまの担当の方の番号：⛔ 0203 の `WHERE s.since IS NOT NULL` は、読む所が前の担当の方の番号（task_former_number）だけで、そちらが同じ条件で
-- 先に偽を返すので、効かなかった
CREATE OR REPLACE FUNCTION salescall.task_current_person_number(p_task uuid, p_contact uuid, p_at timestamptz) RETURNS boolean
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT coalesce((
    SELECT p_contact = salescall.task_dial_contact_at(p_task, p_at)
        OR EXISTS (SELECT 1 FROM salescall.task_dial_fixes f WHERE f.task_id = p_task AND f.contact_id = p_contact
                     AND f.recorded_at >= s.since AND f.recorded_at <= p_at)
        OR EXISTS (SELECT 1 FROM salescall.task_designations d JOIN salescall.attempts a ON a.attempt_id = d.attempt_id
                   WHERE d.task_id = p_task AND d.contact_id = p_contact AND d.recorded_at <= p_at AND a.person_since = s.since)
    FROM (SELECT salescall.task_person_since(p_task, p_at) AS since) s), false)
$$;

-- 約束が残っているか・前の担当の方が新しい約束をした通話：⛔ 0203 の `n.kind = 'call'` は、かけ直す予定でない予定（終える・判定待ち）は
-- 約束の印も目的も持たない（CHECK task_next_actions_promised・task_next_actions_purpose）ので promise_call が偽になり、効かなかった
CREATE OR REPLACE FUNCTION salescall.task_promise_alive(p_task uuid) RETURNS boolean LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT coalesce((SELECT salescall.promise_call(n.purpose, n.promised) FROM salescall.current_next_actions n
                   WHERE n.task_id = p_task), false)
$$;

CREATE OR REPLACE FUNCTION salescall.task_old_promise_call(p_task uuid) RETURNS uuid LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT c.attempt_id
  FROM salescall.current_next_actions n
  JOIN LATERAL (SELECT a.attempt_id FROM salescall.attempts a
                WHERE a.task_id = p_task AND salescall.former_person_call(a.attempt_id)
                ORDER BY a.claimed_at DESC, a.attempt_id DESC LIMIT 1) c ON true
  WHERE n.task_id = p_task AND salescall.promise_call(n.purpose, n.promised)
    AND n.person_since IS DISTINCT FROM salescall.task_person_since(p_task, now())   -- いまの予定が、前の担当の方の期の予定
    AND salescall.reached_answer(c.attempt_id)
    AND salescall.settled_at(c.attempt_id) IS NOT NULL
$$;

-- 手の割り当ての流れを分けるか：⛔ 0204 の `r.business_id = b.business_id` は、実施回に割り当てられるのはその実施回の業務の版だけ
-- （run_versions の外部キー）なので、その版をいま使う実施回は、もとから同じ業務で、効かなかった
CREATE OR REPLACE FUNCTION salescall.version_shared_without_solutions(p_run uuid, p_version uuid) RETURNS boolean LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT coalesce((
    SELECT EXISTS (SELECT 1 FROM salescall.runs r
                   WHERE r.run_id IS DISTINCT FROM p_run
                     AND salescall.run_current_version(r.run_id) = p_version AND salescall.run_in_use(r.run_id))
       AND EXISTS (SELECT 1 FROM salescall.business_solutions(b.business_id) s
                   WHERE salescall.solution_outside(p_version, s.answer_name, s.solution_text))
    FROM salescall.business_versions b WHERE b.version_id = p_version), false)
$$;

-- 置き換えの一意：⛔ 0204 の client_solution_replacements_once（解決策と版の組で1つ）は、書く所（core/client_solutions.replaced）が登録した
-- 新しい版ごとに1回だけ書き、読む所（business_solutions・_latest）は、いちばん新しい置き換えの版の答えを読むだけで、重なっても答えが
-- 変わらないので、効く場面が無かった
ALTER TABLE salescall.client_solution_replacements DROP CONSTRAINT client_solution_replacements_once;
