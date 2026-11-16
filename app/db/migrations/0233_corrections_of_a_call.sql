-- 0233 契約者の直しを、直した通話の結果にする（2026-11-16 の4回目の見直し。書いた人：CL-2。DESIGN.md 10章・15-2 の842。
--      ✅ 2026-11-16 ユーザー「直しに従う（お勧め）」＝検証モードで契約者が結果を直したら、次に電話をかけるかも直しに従う）
--
-- ⛔ 0001〜0232：契約者の版（結果の版の kind＝human）は、どの通話の直しかを持たなかった（attempt_id が空）。0225 の通話のいまの結果
--    salescall.call_result は、版の番号の並び（その通話の判定のあと、ほかの通話の判定より前の契約者の版）から推し量った。直せる版は行の
--    いちばん新しい版だけではない（「もうかけないで」・苦情の版・あとの通話が「判定できない」だけの版も直せる＝release_blocked）ので、前の
--    通話の直しを、あとの通話の結果と読みえた（あとの通話の「断られた」が消え、断ったお客様へほかの行からかかる）。
-- ⇒ (1) 契約者の版は、直したシステムの判定の版の通話を持つ（core/approval.correct_result の1か所が書く）。前に書いた契約者の版は、その版の
--        すぐ前のシステムの判定の版の通話にする（前の推し量りと同じ通話＝結果は変わらない）。
--    (2) 通話のいまの結果＝その通話のいちばん新しい結果の版（判定・判定し直し・契約者の直しのどれでも）＝見方 salescall.call_results の1か所。
--        通話のいまの結果で決めること（ほかの行の止め・前の担当の方の新しい約束・確定の印の守り・行の回数・お詫び）は、ここを読む。
--        システムの判定そのもの（見方 call_judgements）を読むのは、判定の正しさを測る所・判定し直すかの比べ・品質の段だけ。
--        結果不明か（result_unknown）・断りの中身（refusal_known）は、もともと通話のいちばん新しい版を読む形なので、直しにも従う。
--    (3) 目的のお客様と話せたかを書ける取引（0211 の守り）は、システムの締めの版（judged・rejudged）を書く取引だけのまま（契約者の版に通話が
--        入っても、守りを緩めない）。
--    前に書いた契約者の版の通話は、その直しで付けた正解（judgement_checks：直したシステムの版の通話。同じ取引＝同じ時刻）から。正解の無い直し
--    （測らない通話）は、直した版のすぐ前のシステムの判定の版の通話（0225 の推し量りと同じ）。

ALTER TABLE salescall.task_results DISABLE TRIGGER task_results_frozen;
UPDATE salescall.task_results h
SET attempt_id = coalesce(
      (SELECT c.attempt_id FROM salescall.judgement_checks c
       WHERE c.task_id = h.task_id AND c.checked_at = h.decided_at ORDER BY c.version_no DESC LIMIT 1),
      (SELECT s.attempt_id FROM salescall.task_results s
       WHERE s.task_id = h.task_id AND s.kind IN ('judged', 'rejudged') AND s.version_no < h.version_no
       ORDER BY s.version_no DESC LIMIT 1))
WHERE h.kind = 'human' AND h.attempt_id IS NULL;
ALTER TABLE salescall.task_results ENABLE TRIGGER task_results_frozen;

-- 通話のいまの結果（1か所）
CREATE VIEW salescall.call_results WITH (security_invoker = true) AS
  SELECT r.* FROM salescall.task_results r
  WHERE r.attempt_id IS NOT NULL
    AND r.version_no = (SELECT max(m.version_no) FROM salescall.task_results m WHERE m.attempt_id = r.attempt_id);
GRANT SELECT ON salescall.call_results TO salescall_app;

CREATE OR REPLACE FUNCTION salescall.call_result(p_attempt uuid) RETURNS SETOF salescall.task_results LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT r.* FROM salescall.call_results r WHERE r.attempt_id = p_attempt
$$;

-- 前の担当の方が新しいかけ直しの約束をした通話（0214 の形から、その通話のいまの結果で読む）
CREATE OR REPLACE FUNCTION salescall.task_old_promise_call(p_task uuid) RETURNS uuid LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT c.attempt_id
  FROM salescall.current_next_actions n
  JOIN LATERAL (SELECT a.attempt_id FROM salescall.attempts a
                WHERE a.task_id = p_task AND salescall.former_person_call(a.attempt_id)
                ORDER BY a.claimed_at DESC, a.attempt_id DESC LIMIT 1) c ON true
  WHERE n.task_id = p_task AND salescall.promise_call(n.purpose, n.promised)
    AND n.person_since IS DISTINCT FROM salescall.task_person_since(p_task, now())   -- いまの予定が、前の担当の方の期の予定
    AND EXISTS (SELECT 1 FROM salescall.call_results j                                -- その通話で、新しいかけ直しの約束をした（締めた）
                WHERE j.attempt_id = c.attempt_id AND j.business_state = 'callback_scheduled')
$$;

-- 確定の印を付けられない版（0161 の形から、あとの通話の結果を、その通話のいまの結果で読む＝契約者が直したあとの通話も「結果がある」）
CREATE OR REPLACE FUNCTION salescall.release_blocked(p_task uuid, p_version integer) RETURNS boolean
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT EXISTS (
    SELECT 1 FROM salescall.task_results r JOIN salescall.attempts a ON a.attempt_id = r.attempt_id
    WHERE r.task_id = p_task AND r.version_no = p_version
      AND (salescall.decision_pending(r.attempt_id)
           OR (r.kind IN ('judged', 'rejudged')
               AND EXISTS (SELECT 1 FROM salescall.task_results n WHERE n.task_id = r.task_id AND n.attempt_id = r.attempt_id
                           AND n.kind IN ('judged', 'rejudged') AND n.version_no > r.version_no))
           OR (r.kind IN ('judged', 'rejudged') AND r.business_state <> 'do_not_call' AND NOT r.complaint
               AND EXISTS (SELECT 1 FROM salescall.attempts b JOIN salescall.call_results j ON j.attempt_id = b.attempt_id
                           WHERE b.task_id = a.task_id AND b.claimed_at > a.claimed_at AND j.business_state <> 'unknown'))))
$$;

-- 目的のお客様と話せたかの守り（0211 の形から、システムの締めの版を書く取引だけ）
CREATE OR REPLACE FUNCTION salescall.attempt_reached_target_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.reached_target IS DISTINCT FROM OLD.reached_target
     AND NOT EXISTS (SELECT 1 FROM salescall.task_results r WHERE r.attempt_id = NEW.attempt_id AND r.decided_at = now()
                     AND r.kind IN ('judged', 'rejudged'))
     AND NOT EXISTS (SELECT 1 FROM salescall.apology_outcomes o WHERE o.attempt_id = NEW.attempt_id AND o.recorded_at = now()) THEN
    RAISE EXCEPTION '目的のお客様と話せたか（reached_target）を書けるのは、その通話の締めの記録（結果の版・お詫びの電話の結び）を書く取引の中だけです'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'attempt_reached_target_by_settle';
  END IF;
  RETURN NEW;
END $$;
