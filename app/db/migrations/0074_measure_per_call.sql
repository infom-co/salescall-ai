-- 0074 誤判定率を通話ごとに測る（DESIGN.md 10-3・15-2 の170。2026-10-18 の見直し。書いた人：CL-2）
--
-- 見直しで見つかった4つの穴（0066 の作り）：
-- (1) お客様と話せなかった通話（留守番電話など）の判定は「会話に至らなかった」（open）。検証モードで契約者がそれを承認すると、
--     正解に open を入れられず誤りになり、承認そのものが取り消された。録音の印も外れなかった（かけずに取り消した予約も同じ）。
-- (2) 正解の出どころ（検証モード・抜き取り・本番で気づいて直した）を、呼ぶ側が渡していた。抜き取っていない通話を「抜き取り」として
--     入れると、偏った材料が誤判定率に混ざった（出どころを決める所が2か所あった）。
-- (3) 抜き取るのは通話なのに、正解は結果の版ごとに数えた：1つの通話に判定の版が2つ（判定できなかった版と判定し直した版）あると、
--     1本を2回数えるか、判定できなかった版に付けて数から黙って落ちた。
-- (4) 「測る通話」の決まりが、抜き取りの中にしか無かった（検証モードの印は、話していない通話にも付いたまま外れなかった）。
--
-- ⇒ (a) 測る通話の決まりをデータベースの1か所にする（`salescall.call_to_measure`：お客様と話せた通話＝会話に入った・お詫びの
--        電話でない）。抜き取りの母数・正解を付けられるか・測らない通話の印を外すかは、どれもこれを読む。
--    (b) 正解は「その通話の、システムの最新の判定の版」にだけ付けられる（通話ごとに1つの比べる相手）。
--    (c) 出どころは、データベースがその通話から決めて入れる（検証の印があれば verification、抜き取った通話なら sample、
--        どちらでもなければ correction）。呼ぶ側の値は使わない。
--    (d) 正解の業務の状態に「会話に至らなかった」（open）も入れられる（会話に入ったが、相手が人でなかった通話の正しい判定）。

-- ═════════ (d) 正解の業務の状態 ═════════
DO $$
DECLARE c text;
BEGIN
  SELECT conname INTO STRICT c FROM pg_constraint
  WHERE conrelid = 'salescall.judgement_checks'::regclass AND contype = 'c'
    AND pg_get_constraintdef(oid) LIKE '%truth_state%open%unknown%';
  EXECUTE format('ALTER TABLE salescall.judgement_checks DROP CONSTRAINT %I', c);
END $$;
ALTER TABLE salescall.judgement_checks
  ADD CONSTRAINT judgement_checks_truth_known CHECK (truth_state <> 'unknown');

-- ═════════ (a) 測る通話 ═════════
CREATE FUNCTION salescall.call_to_measure(p_attempt uuid) RETURNS boolean LANGUAGE sql STABLE AS $$
  SELECT EXISTS (SELECT 1 FROM salescall.attempts a
                 WHERE a.attempt_id = p_attempt AND a.purpose <> 'apology'
                   AND EXISTS (SELECT 1 FROM salescall.attempt_state_history h
                               WHERE h.attempt_id = a.attempt_id AND h.to_state = 'in_conversation'))
$$;
GRANT EXECUTE ON FUNCTION salescall.call_to_measure(uuid) TO salescall_app;

-- ═════════ (b)(c) 正解の守り ═════════
CREATE OR REPLACE FUNCTION salescall.judgement_check_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF salescall.cur_actor_kind() IS DISTINCT FROM 'operator' AND salescall.cur_actor_kind() IS DISTINCT FROM 'admin' THEN
    RAISE EXCEPTION '正解を付けるのは、管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'judgement_check_by_person';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM salescall.task_results r WHERE r.task_id = NEW.task_id AND r.version_no = NEW.version_no
                 AND r.kind IN ('judged', 'rejudged') AND r.attempt_id = NEW.attempt_id)
     OR EXISTS (SELECT 1 FROM salescall.task_results r WHERE r.task_id = NEW.task_id AND r.attempt_id = NEW.attempt_id
                AND r.kind IN ('judged', 'rejudged') AND r.version_no > NEW.version_no) THEN
    RAISE EXCEPTION '正解を付けるのは、その通話の、システムの最新の判定の版だけです' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'judgement_check_of_system_version';
  END IF;
  IF NOT salescall.call_to_measure(NEW.attempt_id) THEN
    RAISE EXCEPTION '正解を付けるのは、お客様と話せた通話だけです' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'judgement_check_of_measured_call';
  END IF;
  NEW.source := CASE
    WHEN EXISTS (SELECT 1 FROM salescall.recording_holds h WHERE h.attempt_id = NEW.attempt_id AND h.reason = 'verification')
      THEN 'verification'
    WHEN EXISTS (SELECT 1 FROM salescall.sample_draw_calls d WHERE d.attempt_id = NEW.attempt_id) THEN 'sample'
    ELSE 'correction' END;
  RETURN NEW;
END $$;
