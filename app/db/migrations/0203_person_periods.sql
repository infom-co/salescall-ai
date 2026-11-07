-- 0203 担当の期（2026-11-07。書いた人：CL-2。DESIGN.md 3-4-1 の【0203】・2章 Q83〜・15-2 の600〜）
--
-- ⛔ 0202 までの作りは「その通話は前の担当の方へのものか」を、番号と時刻の比べで推し量っていた（かけた番号が、いま前の担当の方の番号か＝
--    task_former_number・お呼びする方が替わった直しが、前のお客様とのことの元の時刻より後か＝next_action._target_changed_since・_carried_since）。
--    第三者の見直しが、実際に動かして次の食い違いを見つけた：
--    (1) 新しい担当の方（鈴木様）が前の番号 O（代表番号）を指定し、O で鈴木様が「もうかけないで」と断ると、O への通話が前の担当の方への通話と
--        みなされ、断った鈴木様の直通 N へまたかけた（取り返しのつかない害）。O で直す前に受付と話していた行は、門が選び待ちで止まるのに、
--        契約者への知らせが出なかった。
--    (2) 「守る」の約束の電話で前の担当の方（田中様）に言ってはいけないことを言い、お詫びが届かないと、お詫びが新しい番号の鈴木様へ行った。
--    (3) 前の番号への番号の聞き直しで前の会社の受付の方と話すと、鈴木様への電話に引き継ぎの印が付かなかった。
--    (4) 引き継ぎの印が、番号の聞き直しをはさむと消えた。
--    (7) 田中様の「あとで」が、鈴木様の「あとで」の回数に入った。
--    (8) 同じ依頼者のほかの行で田中様が断ると、鈴木様の N まで止まった。
-- ⇒ 「その予定・その通話は、どの担当の方の期のものか」を1つの値（person_since）で持つ。
--
-- (1) 担当の期：その行で、お呼びする方が替わった時刻（「担当の方は違う」を選んだ番号の直し・お呼びする方の名前／フルネーム／役職／部署の直し）。
--     その時刻までのいちばん新しい替わりの時刻が、その時刻の期の始まり（最初の担当の方の期は NULL）。決めるのは task_person_since の1か所。
-- (2) 予定（task_next_actions.person_since）：予定を書く1か所（core/next_action._append）が決める。通話（attempts.person_since）：予約する1か所
--     （門の SQL）が予定から写す（あとから変えられない＝attempt_record_fixed）。
-- (3) 前の担当の方への通話＝通話の期が、いま（締めるとき）の期と違う（former_person_call）。直す前に予約して直したあとにつながった通話も、
--     期が古いので前の担当の方への通話（2026-11-07 本体の指示 (3)）。読むのは、いままでどおり断りの範囲・待たせる範囲（core/dnc）・行を終えるか
--     （core/next_action.answer_ends_row）・サブにするか（call_makes_sub）・選び待ち（task_old_promise_waiting）。
-- (4) 前の担当の方の番号（task_former_number）は「止める番号の範囲・代わりの番号」のために残す（前の担当の方への通話かの判断には使わない）。
--     いまの担当の方の番号（task_current_person_number＝いまかける番号・期の始まりのあとの直しの番号・いまの期の通話で指定された番号）でなく、期の始まりより前から行にあった番号か、前の期の通話で言われた番号。
-- (5) 目的のお客様と話せたか（attempts.reached_target）：締めの処理が判定の入力の1か所（judge.JudgeInput.reached_target）を写す。引き継ぎを
--     確かめる印（core/next_action の1か所）が「いまの期の通話で目的のお客様とまだ話していない」を読む（受付の方とだけ話した通話は入らない）。
-- (6) 選び待ち（task_old_promise_waiting）：いまの予定が前の担当の方の期の約束で、その期のいちばん新しい通話（締めたとき、もう前の担当の方への
--     通話だったもの）のあとの予定のときだけ。⛔ 0202：番号で見ていたので、新しい担当の方が指定した前の番号への約束でも選びを待った。

-- ═════════ (1) 担当の期（1か所） ═════════
CREATE FUNCTION salescall.task_person_since(p_task uuid, p_at timestamptz) RETURNS timestamptz LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT max(x.at) FROM (
    SELECT d.recorded_at AS at FROM salescall.task_dial_fixes d                                   -- 「担当の方は違う」を選んだ番号の直し
    WHERE d.task_id = p_task AND NOT d.same_person AND d.recorded_at <= p_at
    UNION ALL
    SELECT f.recorded_at FROM salescall.task_fixes f                                              -- お呼びする方の名前・役職・部署の直し
    WHERE f.task_id = p_task AND f.field IN ('target_name', 'target_full_name', 'target_role', 'target_department')
      AND f.recorded_at <= p_at) x
$$;
REVOKE ALL ON FUNCTION salescall.task_person_since(uuid, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.task_person_since(uuid, timestamptz) TO salescall_app;

-- ═════════ (2) 予定と通話の期 ═════════
ALTER TABLE salescall.task_next_actions ADD COLUMN person_since timestamptz;   -- その予定の担当の期の始まり（NULL＝最初の担当の方）
ALTER TABLE salescall.attempts ADD COLUMN person_since timestamptz;            -- その通話の担当の期の始まり（予約するとき予定から写す）
ALTER TABLE salescall.attempts ADD COLUMN reached_target boolean;              -- 目的のお客様と話せたか（締めが書く。NULL＝まだ締めていない）

-- 既にある行を埋める：予定は決めた時刻の期、通話は予約したときに効いていた予定の期（予定が無ければ予約した時刻の期）
ALTER TABLE salescall.task_next_actions DISABLE TRIGGER task_next_actions_frozen;
UPDATE salescall.task_next_actions n SET person_since = salescall.task_person_since(n.task_id, n.decided_at);
ALTER TABLE salescall.task_next_actions ENABLE TRIGGER task_next_actions_frozen;
ALTER TABLE salescall.attempts DISABLE TRIGGER USER;
UPDATE salescall.attempts a SET person_since = CASE
    WHEN EXISTS (SELECT 1 FROM salescall.task_next_actions p WHERE p.task_id = a.task_id AND p.decided_at <= a.claimed_at)
    THEN (SELECT p.person_since FROM salescall.task_next_actions p WHERE p.task_id = a.task_id AND p.decided_at <= a.claimed_at
          ORDER BY p.seq DESC LIMIT 1)
    ELSE salescall.task_person_since(a.task_id, a.claimed_at) END;
ALTER TABLE salescall.attempts ENABLE TRIGGER USER;

-- いまの予定の見方に、期の欄を足す（n.* は作ったときの欄だけ）
CREATE OR REPLACE VIEW salescall.current_next_actions WITH (security_invoker = true) AS
  SELECT n.* FROM salescall.task_next_actions n
  WHERE n.seq = (SELECT max(m.seq) FROM salescall.task_next_actions m WHERE m.task_id = n.task_id);

-- 0155 の形に、通話の期を足した（予約したときの予定の期のまま。あとから変えられない）
CREATE OR REPLACE FUNCTION salescall.attempt_record_fixed() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (NEW.promised, NEW.voice_type, NEW.judge_route, NEW.person_since)
     IS DISTINCT FROM (OLD.promised, OLD.voice_type, OLD.judge_route, OLD.person_since) THEN
    RAISE EXCEPTION '試行の、約束したかけ直しか・声の型・判定の道・担当の期は変えられません' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;

-- ═════════ (3) 前の担当の方への通話（1か所）：通話の期が、その通話を締めたときの期と違う ═════════
-- その通話を締めた時刻（いちばん早い締めの記録＝判定の結果の版・お詫びの電話の結び。is_settled と同じ2つ。まだ締めていなければ NULL）
CREATE FUNCTION salescall.settled_at(p_attempt uuid) RETURNS timestamptz LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT least((SELECT min(r.decided_at) FROM salescall.task_results r WHERE r.attempt_id = p_attempt),
               (SELECT o.recorded_at FROM salescall.apology_outcomes o WHERE o.attempt_id = p_attempt))
$$;
REVOKE ALL ON FUNCTION salescall.settled_at(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.settled_at(uuid) TO salescall_app;

-- 比べる期は、その通話を締めたときの期（まだ締めていなければ、いまの期）。締めたあとに、もう一度お呼びする方が替わっても、締めた通話の
-- 判断（行を終えたか・止めた範囲＝締め直し・止め直しの比べ）は変わらない（見直し H の (B3)。⛔ 0202 と 0203 の初めの形は「いま」で見ていた）
CREATE OR REPLACE FUNCTION salescall.former_person_call(p_attempt uuid) RETURNS boolean LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT coalesce((SELECT a.person_since IS DISTINCT FROM
                            salescall.task_person_since(a.task_id, coalesce(salescall.settled_at(a.attempt_id), now()))
                   FROM salescall.attempts a WHERE a.attempt_id = p_attempt AND a.contact_id IS NOT NULL), false)
$$;

-- ═════════ (4) いまの担当の方の番号・前の担当の方の番号 ═════════
-- いまの担当の方の番号：期の始まりがあり（お呼びする方が替わった）、いまかける番号か、期の始まりのあとの番号の直しの番号か、いまの期の
-- 通話で指定された番号（いまの期の通話で言われた新しい番号は、期の始まりのあとに足され、言われた通話がいまの期なので、もともと前の担当の方の
-- 番号に入らない＝条件に書かない）
CREATE FUNCTION salescall.task_current_person_number(p_task uuid, p_contact uuid, p_at timestamptz) RETURNS boolean
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT coalesce((
    SELECT p_contact = salescall.task_dial_contact_at(p_task, p_at)
        OR EXISTS (SELECT 1 FROM salescall.task_dial_fixes f WHERE f.task_id = p_task AND f.contact_id = p_contact
                     AND f.recorded_at >= s.since AND f.recorded_at <= p_at)
        OR EXISTS (SELECT 1 FROM salescall.task_designations d JOIN salescall.attempts a ON a.attempt_id = d.attempt_id
                   WHERE d.task_id = p_task AND d.contact_id = p_contact AND d.recorded_at <= p_at AND a.person_since = s.since)
    FROM (SELECT salescall.task_person_since(p_task, p_at) AS since) s WHERE s.since IS NOT NULL), false)
$$;
REVOKE ALL ON FUNCTION salescall.task_current_person_number(uuid, uuid, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.task_current_person_number(uuid, uuid, timestamptz) TO salescall_app;

-- 前の担当の方の番号：いまの担当の方の番号でなく、期の始まりより前から行にあった番号か、前の期の通話で言われた番号（止める番号の範囲・
-- 代わりの番号に使う。前の担当の方への通話かは former_person_call＝期で見る）
CREATE OR REPLACE FUNCTION salescall.task_former_number(p_task uuid, p_contact uuid, p_at timestamptz) RETURNS boolean
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT coalesce((
    SELECT NOT salescall.task_current_person_number(p_task, p_contact, p_at)
       AND EXISTS (SELECT 1 FROM salescall.task_numbers n WHERE n.task_id = p_task AND n.contact_id = p_contact
                     AND (n.added_at < s.since
                          OR EXISTS (SELECT 1 FROM salescall.attempts a WHERE a.attempt_id = n.source_attempt_id
                                       AND a.person_since IS DISTINCT FROM s.since)))
    FROM (SELECT salescall.task_person_since(p_task, p_at) AS since) s WHERE s.since IS NOT NULL), false)
$$;

-- ═════════ (6) 選び待ち（0202 の形を、期で見る形に替えた） ═════════
-- 契約者の選びを待っている、前の担当の方が新しい約束をした通話（無ければ NULL）：いまの予定が、前の担当の方の期の、お客様と約束したかけ直しで、
-- その行のいちばん新しい前の担当の方への通話（お客様が出た・締めた・締めたとき、もう前の担当の方への通話だった）のあとに決めた予定で、その通話を
-- 締めたあとに、まだ選んでいない（締める前の選び＝その通話の途中で番号を直したときの選びは、前の約束についての選び）
CREATE OR REPLACE FUNCTION salescall.task_old_promise_waiting(p_task uuid) RETURNS uuid LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT c.attempt_id
  FROM salescall.current_next_actions n
  CROSS JOIN (SELECT salescall.task_person_since(p_task, now()) AS since) s
  JOIN LATERAL (SELECT a.attempt_id, a.claimed_at,
                       salescall.settled_at(a.attempt_id) AS settled_at
                FROM salescall.attempts a
                WHERE a.task_id = p_task AND salescall.former_person_call(a.attempt_id)
                ORDER BY a.claimed_at DESC, a.attempt_id DESC LIMIT 1) c ON true
  WHERE n.task_id = p_task AND n.kind = 'call' AND salescall.promise_call(n.purpose, n.promised)
    AND n.person_since IS DISTINCT FROM s.since                                     -- いまの予定が、前の担当の方の期の予定
    AND n.decided_at > c.claimed_at
    AND salescall.reached_answer(c.attempt_id)
    AND c.settled_at IS NOT NULL
    AND NOT EXISTS (SELECT 1 FROM salescall.old_promise_choices k WHERE k.task_id = p_task AND k.recorded_at > c.settled_at)
$$;
