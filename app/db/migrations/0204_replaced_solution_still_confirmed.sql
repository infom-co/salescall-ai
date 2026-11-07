-- 0204 取り込み直しで置き換えた解決策も、置き換えた取り込みの答えとして、実施回ごとの確かめに数える（2026-11-07。書いた人：CL-2。
--      DESIGN.md 3-8-3 の【0204】・2章 Q95〜Q97・15-2 の640〜645）。下の (4) は、同じ日の見直し H（データベースの監査）の指摘で足した
--      （手で割り当てる版の流れを分けるかを、データベースの1か所にし、守りが読む）
--
-- ✅ 計画書 6-1 (5)（手で外した解決策は、取り込み直しでも自動では入れない＝契約者が確かめる）・(6)（手で前の版に戻した実施回・古い版で
--    始めた実施回では、契約者が実施回ごとに承認するまで、約束・続き・お詫びのほかはかけない）にそろえる（本体 CL-2 の決定）。
--
-- ⛔ 前の作りの食い違い（第三者の見直しが動かして確かめた）：解決策「料金」を足したあと、実施回 R1 を手で前の版に戻した（写し C。承認待ちで、
--    門は solutions_unconfirmed）。契約者が業務設定を取り込み直し、取り込んだ中身に「料金」の答えを書いた。R1 は写し C のまま（料金の答えは、
--    解決策のも取り込んだのも無い）なのに、確かめの一覧が空になり、門が通った。0201 の business_solutions が、取り込みで置き換えた解決策
--    （0197 の client_solution_replacements）を一覧から除いていたため（「流れに入っていない前の解決策」の一覧・手で外した印・門が、どれも
--    これを読む）。
--
-- (1) その業務の、問いの名前ごとの「いまの中身」（business_solutions の1か所）：いちばん新しく足せた解決策。取り込み直しで置き換えられて
--     いれば、置き換えた取り込みの版の、その問いの答え（version_answer の1か所＝業務設定の重ね方と同じ）にする（replaced＝真）。置き換えた
--     取り込みが、その問いに答えを置かなかった（「使わない」にした・あとの取り込みで消した）ときは、入れる中身が無いので出さない。
--     これを読むところは、どれも同じ答えになる：手で外した印（hand_removals）・流れに入っていない一覧と承認待ち（core/client_solutions の
--     not_in_line・unconfirmed＝門 gate.unconfirmed_solutions）・知らせ solution_earlier・流れを分けるか（business_settings._shared_apart）。
--     「入れる」（add_earlier）を選ぶと、置き換えた取り込みの答えが、その実施回の流れに入る（解決策の古い文で、取り込んだ中身を上書きしない）。
--     取り込み直しで残す（core/client_solutions.keep_in）のは、置き換えられていない解決策だけ（置き換えた答えは取り込みの中身なので、
--     解決策として残さない＝よみがえらせない）。
-- (2) 置き換えは、取り込み直すたびに残す（1つの解決策に、いくつも）。⛔ 前は1つの解決策に1つだけ（主キーが解決策）で、あとの取り込み直しが
--     答えを変えても、確かめの一覧は最初の取り込みの答えのままになりえた。いちばん新しい置き換えの版の答えを読む。
-- (3) 実施回ごとの確かめ（core/client_solutions._marks の1か所）は、「入れない」に加えて、その実施回で「入れる」を選んだ（流れに足せた）ことも
--     数える（データベースの変更は無い）。前は「入れる」を選べば流れに入るので一覧に出なくなり、それで確かめたことにしていたが、あとの
--     取り込み直しで中身が変わると、もう選んだ実施回が、また承認待ちになった。

-- ═════════ (2) 置き換えを、取り込み直すたびに残す ═════════
ALTER TABLE salescall.client_solution_replacements DROP CONSTRAINT client_solution_replacements_pkey;
ALTER TABLE salescall.client_solution_replacements ADD COLUMN seq bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY;
ALTER TABLE salescall.client_solution_replacements
  ADD CONSTRAINT client_solution_replacements_once UNIQUE (solution_id, version_id);

-- ═════════ (1) その業務の、問いの名前ごとのいまの中身（1か所） ═════════
-- 返す形が変わる（replaced を足す）ので作り直す。0201 の形に、置き換えの答えを足した
DROP FUNCTION salescall.business_solutions(uuid);
CREATE FUNCTION salescall.business_solutions(p_business uuid)
RETURNS TABLE (solution_id uuid, attempt_id uuid, answer_name text, solution_text text, point_text text, recorded_at timestamptz,
               replaced boolean)
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT l.solution_id, l.attempt_id, l.answer_name,
         CASE WHEN x.version_id IS NULL THEN l.solution_text ELSE x.answer END,   -- 置き換えられていれば、置き換えた取り込みの答え
         l.point_text, l.recorded_at, x.version_id IS NOT NULL
  FROM (
    SELECT DISTINCT ON (c.answer_name) c.solution_id, c.attempt_id, c.answer_name, c.solution_text, c.point_text, c.recorded_at
    FROM salescall.client_solutions c JOIN salescall.tasks t ON t.task_id = c.task_id JOIN salescall.runs r ON r.run_id = t.run_id
    WHERE r.business_id = p_business AND EXISTS (SELECT 1 FROM salescall.client_solution_lines l
                                                 WHERE l.solution_id = c.solution_id AND l.version_id IS NOT NULL)
    ORDER BY c.answer_name, c.seq DESC) l   -- 足した順（データベースが振る＝0192）
  LEFT JOIN LATERAL (SELECT y.version_id, salescall.version_answer(y.version_id, l.answer_name) AS answer
                     FROM salescall.client_solution_replacements y WHERE y.solution_id = l.solution_id
                     ORDER BY y.recorded_at DESC, y.seq DESC LIMIT 1) x ON true   -- いちばん新しい置き換え
  WHERE x.version_id IS NULL OR x.answer IS NOT NULL   -- 置き換えた取り込みが答えを置かなかった：入れる中身が無い
  ORDER BY l.answer_name
$$;
REVOKE ALL ON FUNCTION salescall.business_solutions(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.business_solutions(uuid) TO salescall_app;

-- ═════════ (4) 手で割り当てる版の流れを、その実施回のために分けるか（2026-11-07 の見直し H。データベースの1か所） ═════════
-- ⛔ 前の作りの食い違い（見直し H が写しの上で確かめた）：流れを分けるか（core/business_settings._shared_apart）は Python の1か所だけで、
--    データベースの守り run_version_guard（0201）は「つながりの先の版か」しか見なかった。契約者の取引が run_versions へ直接書くと、2つの
--    実施回が同じ流れ（版）を使えてしまい、片方で「入れる」を選ぶと、もう片方の「まだ確かめていない」も消えて門が開いた（片方の承認が、
--    もう片方の承認になる＝確かめは実施回ごと、の決まりに反する）。
-- (4-1) まだ終えていない実施回か（run_in_use の1か所）：版があり、終わっていない行がある・まだ行が無い（0192 の task_open）。Python の
--       business_settings._versions_in_use も、これを読む（⛔ 前は Python の問い合わせだけが決めていた）。
-- (4-2) 流れを分けるか（version_shared_without_solutions の1か所）：手で割り当てる版を、ほかのまだ終えていない実施回がいま使っていて、その
--       業務の解決策（business_solutions＝(1) の中身）のどれかが、その版の流れに入っていない。真なら、手の割り当ては写しを作る
--       （business_settings.assign。_shared_apart は、これを読むだけ）。データベースの守り run_version_guard も、これを読み、真なら手の
--       割り当てを断る（守り run_version_manual_shared。自動の割り当ては当たらない）。
CREATE FUNCTION salescall.run_in_use(p_run uuid) RETURNS boolean LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT salescall.run_current_version(p_run) IS NOT NULL
     AND (EXISTS (SELECT 1 FROM salescall.tasks t WHERE t.run_id = p_run AND salescall.task_open(t.task_id))
          OR NOT EXISTS (SELECT 1 FROM salescall.tasks t WHERE t.run_id = p_run))
$$;
REVOKE ALL ON FUNCTION salescall.run_in_use(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.run_in_use(uuid) TO salescall_app;

CREATE FUNCTION salescall.version_shared_without_solutions(p_run uuid, p_version uuid) RETURNS boolean LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT coalesce((
    SELECT EXISTS (SELECT 1 FROM salescall.runs r
                   WHERE r.run_id IS DISTINCT FROM p_run AND r.business_id = b.business_id
                     AND salescall.run_current_version(r.run_id) = p_version AND salescall.run_in_use(r.run_id))
       AND EXISTS (SELECT 1 FROM salescall.business_solutions(b.business_id) s
                   WHERE salescall.solution_outside(p_version, s.answer_name, s.solution_text))
    FROM salescall.business_versions b WHERE b.version_id = p_version), false)
$$;
REVOKE ALL ON FUNCTION salescall.version_shared_without_solutions(uuid, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.version_shared_without_solutions(uuid, uuid) TO salescall_app;

-- 0201 の形に、(4-2) の守りを足した
CREATE OR REPLACE FUNCTION salescall.run_version_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.assigned_at := now();
  IF NEW.auto AND salescall.cur_actor_kind() IS DISTINCT FROM 'system' THEN
    RAISE EXCEPTION '自動の割り当ては、システムの処理だけです' USING ERRCODE = 'insufficient_privilege', CONSTRAINT = 'run_version_auto_by_system';
  END IF;
  IF NOT NEW.auto AND salescall.version_tip(NEW.version_id) IS DISTINCT FROM NEW.version_id THEN
    RAISE EXCEPTION '手で割り当てられるのは、つながりの先の版だけです（途中の版は、写しを作って割り当てます）' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'run_version_manual_tip';
  END IF;
  IF NOT NEW.auto AND salescall.version_shared_without_solutions(NEW.run_id, NEW.version_id) THEN
    RAISE EXCEPTION 'ほかのまだ終えていない実施回が使っていて、依頼者の解決策が入っていない版は、手で割り当てられません（写しを作って割り当てます）'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'run_version_manual_shared';
  END IF;
  RETURN NEW;
END $$;
