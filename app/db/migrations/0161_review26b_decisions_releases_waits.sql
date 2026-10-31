-- 0161 見直し26b（2026-10-31 管理者：「まだ、不具合が多すぎます」）：取り消せない決定・確定の印・保留の守りの穴を直す。書いた人：CL-2。
-- 設計書 10-2-2・2章 Q53〜Q57・15-2 の386〜。
--
-- (1) 確定の印を付けられない版（salescall.release_blocked の1か所）に「その通話に、もっと新しいシステムの判定の版がある」を足す。⛔ 差し戻した
--     あとに、画面に残っていた前の断りの版を、結果の承認で確定できた（契約者が差し戻した断りが、依頼者へ渡る版になった＝2026-10-30 (6)
--     「承認を押した場合だけ営業拒否と確定」に反する）。結果を直す入口（approval.correct_result）も同じ1か所を読む。
-- (2) 検証の段の版か（salescall.version_in_verification の1か所）：その版の通話を、検証モードでかけた（門が、かけたときのモードで付ける
--     検証の印＝recording_holds の verification）か。契約者の版（直した版）は、その前の、いちばん新しいシステムの版の通話で決める。
--     確定の印の段は、これと同じでなければならず、検証の段の印を付けるのは管理者か契約者だけ（データベースが守る）。⛔ 検証モードの業務の
--     版に「本番」の確定の印を付けられ、システムの処理が「契約者の承認」で確定できた（0043 の「検証の段階の結果を依頼者へ渡さない」が、
--     コードが渡す段の値に頼っていた）。締めのあとの確定（approval.after_judged）と、印の段（approval._release）も、これを読む（かけた
--     あとにモードを替えても、かけたときのモードで決まる）。
-- (3) 取り消しの答えの中身（salescall.revoked_parts の1か所）：その通話から入れた発信禁止がすべて取り消されたとき、その登録の理由から決まる
--     中身（断り・番号違い・FAX）だけ。⛔ 取り消しの答えに、取り消していない中身（怒っていた）を入れられ、その保留が録音でも契約者でも
--     確かめないまま閉じた。発信禁止の取り消しの処理（core/dnc._revoke）も、同じ関数で中身を決める（前は Python の表だけにあった）。
-- (4) 保留の版を書くのは、締めの入口（core/approval.record_wait）だけ：書く前に、その通話を締めている印（salescall.settling）を立て、守りが
--     確かめる。⛔ 契約者・管理者の取引から、中身が空の版を直接書け、承認を通らずに保留が閉じた（締め直しが契約者の取引でも走るので、
--     書く人の種類だけでは分けられなかった）。

-- ═════════ (1) 確定の印を付けられない版 ═════════
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
               AND EXISTS (SELECT 1 FROM salescall.attempts b JOIN salescall.call_judgements j ON j.attempt_id = b.attempt_id
                           WHERE b.task_id = a.task_id AND b.claimed_at > a.claimed_at AND j.business_state <> 'unknown'))))
$$;

-- ═════════ (2) 検証の段の版か ═════════
-- その通話を、検証モードでかけたか（1か所。門が、かけたときのモードで付ける検証の印＝recording_holds の verification。外したあとも印は
-- 残る＝released_at）。取り消せない決定を、その場で決めるか・契約者の承認を待つか（core/approval.decision_wait・decides_on_spot）も、
-- これを読む（かけたあとにモードを替えても、その通話は、かけたときのモードのまま＝⛔ 前は、締めるときの業務のモードで決めていた）
CREATE FUNCTION salescall.attempt_in_verification(p_attempt uuid) RETURNS boolean
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT EXISTS (SELECT 1 FROM salescall.recording_holds h WHERE h.attempt_id = p_attempt AND h.reason = 'verification')
$$;
GRANT EXECUTE ON FUNCTION salescall.attempt_in_verification(uuid) TO salescall_app;

CREATE FUNCTION salescall.version_in_verification(p_task uuid, p_version integer) RETURNS boolean
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT coalesce(salescall.attempt_in_verification(
    (SELECT r.attempt_id FROM salescall.task_results r
     WHERE r.task_id = p_task AND r.version_no <= p_version AND r.attempt_id IS NOT NULL ORDER BY r.version_no DESC LIMIT 1)), false)
$$;
GRANT EXECUTE ON FUNCTION salescall.version_in_verification(uuid, integer) TO salescall_app;

CREATE OR REPLACE FUNCTION salescall.result_release_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF salescall.release_blocked(NEW.task_id, NEW.version_no) THEN
    RAISE EXCEPTION 'その版には確定の印を付けられません（取り消せない決定がまだ決まっていない・あとの通話の結果がある・その通話の新しい版がある）'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'result_release_guard';
  END IF;
  IF NEW.stage IS DISTINCT FROM (CASE WHEN salescall.version_in_verification(NEW.task_id, NEW.version_no) THEN 'verification'
                                      ELSE 'production' END) THEN
    RAISE EXCEPTION '確定の印の段は、その版の通話をかけたときのモード（検証の段か本番か）と同じです'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'result_release_guard';
  END IF;
  IF NEW.stage = 'verification' AND coalesce(salescall.cur_actor_kind()::text, '') NOT IN ('operator', 'admin') THEN
    RAISE EXCEPTION '検証の段の結果を確定するのは、契約者か管理者の承認だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'result_release_guard';
  END IF;
  RETURN NEW;
END $$;

-- ═════════ (3) 取り消しの答えの中身 ═════════
-- その通話から入れた発信禁止が1つ以上あり、すべて取り消されているときの、外す中身（登録の理由ごと。無ければ空）
CREATE FUNCTION salescall.revoked_parts(p_attempt uuid) RETURNS text[]
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT CASE WHEN EXISTS (SELECT 1 FROM salescall.dnc_entries e WHERE e.source_attempt_id = p_attempt AND e.revoked_at IS NULL)
              THEN '{}'::text[]
              ELSE coalesce((SELECT array_agg(DISTINCT p ORDER BY p) FROM (
                SELECT CASE e.reason::text WHEN 'refused_in_call' THEN 'refusal' WHEN 'refused_unclear_checked' THEN 'refusal'
                                           WHEN 'wrong_party' THEN 'wrong_party' WHEN 'fax_line' THEN 'fax' END AS p
                FROM salescall.dnc_entries e WHERE e.source_attempt_id = p_attempt) x WHERE p IS NOT NULL), '{}'::text[]) END
$$;
GRANT EXECUTE ON FUNCTION salescall.revoked_parts(uuid) TO salescall_app;

CREATE OR REPLACE FUNCTION salescall.decision_answer_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.answered_by IS DISTINCT FROM salescall.cur_actor() THEN
    RAISE EXCEPTION '答えた者は、取引の管理者か契約者です' USING ERRCODE = 'check_violation', CONSTRAINT = 'decision_answer_guard';
  END IF;
  IF NEW.by_kind = 'recording' THEN
    RAISE EXCEPTION '録音の確かめは、答えでなく、締め直しが保留の版を決め直して残します（0154）' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'decision_answer_guard';
  END IF;
  IF coalesce(salescall.cur_actor_kind()::text, '') NOT IN ('operator', 'admin') THEN
    RAISE EXCEPTION '取り消せない決定に答えられる（承認・差し戻し・取り消し）のは、管理者か契約者だけです'
      USING ERRCODE = 'insufficient_privilege', CONSTRAINT = 'decision_answer_guard';
  END IF;
  IF NEW.by_kind = 'approval' AND NOT (
       salescall.decision_asks_contractor(NEW.attempt_id)
       AND NEW.wait_seq = salescall.decision_wait_seq(NEW.attempt_id)
       AND NEW.parts <@ salescall.decision_open(NEW.attempt_id) AND salescall.decision_open(NEW.attempt_id) <@ NEW.parts) THEN
    RAISE EXCEPTION 'その通話の、いまの保留（承認か差し戻しを求めている版の中身）に答えてください' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'decision_answer_guard';
  END IF;
  IF NEW.by_kind = 'revoke' AND NOT (cardinality(NEW.parts) > 0 AND NEW.parts <@ salescall.revoked_parts(NEW.attempt_id)) THEN
    RAISE EXCEPTION '取り消しの答えは、その通話から入れた発信禁止をすべて取り消したときの、その登録の中身だけです'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'decision_answer_guard';
  END IF;
  RETURN NEW;
END $$;

-- ═════════ (4) 保留の版を書くのは、締めの入口だけ ═════════
-- 締めの入口（core/approval.record_wait）が、その通話の保留を書く前に呼ぶ（この取引の中だけの印）
CREATE FUNCTION salescall.settling(p_attempt uuid) RETURNS void
LANGUAGE sql VOLATILE SET search_path = salescall, pg_temp AS $$
  SELECT set_config('salescall.settling', p_attempt::text, true) WHERE p_attempt IS NOT NULL
$$;
GRANT EXECUTE ON FUNCTION salescall.settling(uuid) TO salescall_app;

-- （書く人の種類の確かめは、この印に替えた：取引の人の種類は system・operator・admin のどれかで、締め直しは契約者の取引でも走るので、
-- 種類では分けられない）
CREATE OR REPLACE FUNCTION salescall.decision_wait_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF current_setting('salescall.settling', true) IS DISTINCT FROM NEW.attempt_id::text THEN
    RAISE EXCEPTION '保留を書くのは、その通話を締めている締めの入口だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'decision_wait_guard';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM salescall.attempts a WHERE a.attempt_id = NEW.attempt_id AND a.task_id = NEW.task_id) THEN
    RAISE EXCEPTION '保留の行は、その通話の行です' USING ERRCODE = 'check_violation', CONSTRAINT = 'decision_wait_guard';
  END IF;
  IF NEW.seq IS DISTINCT FROM coalesce(salescall.decision_wait_seq(NEW.attempt_id), 0) + 1 THEN
    RAISE EXCEPTION '保留の版は、順に足します' USING ERRCODE = 'check_violation', CONSTRAINT = 'decision_wait_guard';
  END IF;
  RETURN NEW;
END $$;
