-- 0194 番号の直しと終えた行・依頼者の解決策と前の版の写しへの、2026-11-06 の2回目の伺いのお答え（書いた人：CL-2。DESIGN.md 3-4-1・3-8-3）
--
-- (1) 計画書 7-1 (12)（✅ 2026-11-06 ユーザー）：言ってはいけないことのあとのお詫びの電話が番号のせいでかけられずに終えた行を、番号を
--     直して戻すとき「言ってはいけないことを言った同じ相手なら、新しい番号へお詫びの電話をかけます。違う相手の場合は、お詫びなしで、
--     どこまで引き継がれているのかを確認しつつ、続きからか、途中から営業を続けます。」⇒ 違う相手へ営業を続ける電話には、引き継ぎを
--     確かめる印（handover）を次の予定に持たせ、門が予約証（gate.Claim.handover）へ写す（今の successor と同じ道）。印は、目的の
--     お客様と話せるまで引き継ぐ（core/next_action の `_handover` の1か所）。持てるのは、ふつうの電話・続きの電話の予定だけ。
-- (2) 計画書 7-1 (13)（✅ 同）：お客様が「あとで」を方針の回数より多く言って終えた行は「営業拒否でなければ戻せます。」⇒ 番号を直したとき、
--     その行が終わったときの「あとで」の数（行の回数の数え始め＝いちばん新しい直しから）を数える。数え始めは「その時刻」で読む
--     1か所（task_count_from_at・task_latest_dial_fix_at）にし、いまの数え始め（task_count_from・task_latest_dial_fix）もこれを読む
--     （⛔ 番号を直す取引の中では、数え始めがいまになり、終えたときに数えた「あとで」が0に見えた）。
-- (3) 計画書 6-1 (3)（✅ 同）：契約者が手で前の版に戻した流れに、あとで別の解決策が来たとき、戻す前に足した解決策も入れるかは「契約者に
--     それをダッシュボードで確認します。」⇒ 契約者が一覧から選んで足した流れの結果は、選んだ方（chosen_by＝いま操作している管理者か
--     契約者）を残す。解決策を足した方でなくても書ける（それ以外は、今までどおり解決策を足した方だけ）。

-- ═════════ (1) ═════════
ALTER TABLE salescall.task_next_actions ADD COLUMN handover boolean NOT NULL DEFAULT false;
ALTER TABLE salescall.task_next_actions
  ADD CONSTRAINT task_next_actions_handover_calls CHECK (NOT handover OR (kind = 'call' AND purpose IN ('normal', 'resume')));

CREATE OR REPLACE VIEW salescall.current_next_actions WITH (security_invoker = true) AS
  SELECT n.* FROM salescall.task_next_actions n
  WHERE n.seq = (SELECT max(m.seq) FROM salescall.task_next_actions m WHERE m.task_id = n.task_id);

-- ═════════ (2) ═════════
CREATE FUNCTION salescall.task_latest_dial_fix_at(p_task uuid, p_at timestamptz) RETURNS salescall.task_dial_fixes LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT f.* FROM salescall.task_dial_fixes f WHERE f.task_id = p_task AND f.recorded_at <= p_at ORDER BY f.seq DESC LIMIT 1
$$;
REVOKE ALL ON FUNCTION salescall.task_latest_dial_fix_at(uuid, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.task_latest_dial_fix_at(uuid, timestamptz) TO salescall_app;

CREATE OR REPLACE FUNCTION salescall.task_latest_dial_fix(p_task uuid) RETURNS salescall.task_dial_fixes LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT salescall.task_latest_dial_fix_at(p_task, 'infinity'::timestamptz)
$$;

CREATE FUNCTION salescall.task_count_from_at(p_task uuid, p_at timestamptz) RETURNS timestamptz LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT coalesce((salescall.task_latest_dial_fix_at(p_task, p_at)).recorded_at, '-infinity'::timestamptz)
$$;
REVOKE ALL ON FUNCTION salescall.task_count_from_at(uuid, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.task_count_from_at(uuid, timestamptz) TO salescall_app;

CREATE OR REPLACE FUNCTION salescall.task_count_from(p_task uuid) RETURNS timestamptz LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT salescall.task_count_from_at(p_task, 'infinity'::timestamptz)
$$;

-- ═════════ (3) ═════════
ALTER TABLE salescall.client_solution_lines ADD COLUMN chosen_by text CHECK (chosen_by IS NULL OR btrim(chosen_by) <> '');

CREATE OR REPLACE FUNCTION salescall.client_solution_line_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.chosen_by IS NOT NULL THEN
    -- 契約者が「流れに入っていない前の解決策」の一覧から選んで足した（core/client_solutions.add_earlier）：選んだのは、いま操作している
    -- 管理者か契約者
    IF NEW.chosen_by IS DISTINCT FROM salescall.cur_actor()
       OR (salescall.cur_actor_kind() IS DISTINCT FROM 'operator' AND salescall.cur_actor_kind() IS DISTINCT FROM 'admin') THEN
      RAISE EXCEPTION '前の解決策を選んで足せるのは、いま操作している管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege',
        CONSTRAINT = 'client_solution_line_chosen_by_person';
    END IF;
  ELSIF NOT EXISTS (SELECT 1 FROM salescall.client_solutions c WHERE c.solution_id = NEW.solution_id
                    AND c.recorded_by = salescall.cur_actor()) THEN
    RAISE EXCEPTION '流れごとの結果を書けるのは、その解決策を足した方だけです' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'client_solution_line_by_recorder';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM salescall.client_solutions c JOIN salescall.tasks t ON t.task_id = c.task_id
                   JOIN salescall.runs r ON r.run_id = t.run_id
                   JOIN salescall.business_versions b ON b.version_id = NEW.base_version AND b.business_id = r.business_id
                 WHERE c.solution_id = NEW.solution_id)
     OR (NEW.version_id IS NOT NULL AND NEW.version_id <> NEW.base_version AND NOT EXISTS (
           WITH RECURSIVE chain(v) AS (SELECT s.to_version FROM salescall.version_successions s WHERE s.from_version = NEW.base_version
                                       UNION SELECT s.to_version FROM salescall.version_successions s JOIN chain c ON s.from_version = c.v)
           SELECT 1 FROM chain WHERE v = NEW.version_id)) THEN
    RAISE EXCEPTION '流れの版は、その解決策の業務の版で、足した版は足す元の版か、そのつながりの先の版だけです' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'client_solution_line_versions';
  END IF;
  RETURN NEW;
END $$;
