-- 0242 契約者の直しを、1か所で読む（2026-11-18 の8回目の見直し。書いた人：CL-2。DESIGN.md 10章・15-2 の868〜870。
--      ✅ 2026-11-18 ユーザー：伺い2「契約者が直したことは優先：ただし、間違て契約者が直した可能性もあるので、本当に折り返してよいのか確認と承認をもらう。
--      承認なく勝手にかけたらクレームになる可能性があるため。」・続き(1)「直した契約者（お勧め）」・続き(2)「食い違う答えだけ外す（お勧め）」）
--
-- ⛔ 0240：(a) 確かめ待ちは「直した時点の予定がかける」で決め、直しが予定を書かなかった（あとの発信がある）ときも、別の理由の「かける」予定に付いた
--    (b) 直し直して「かけない」にしても待ちの記録は増えず、あとでほかの道で予定が「かける」に戻ると、古い直しの確かめ待ちが生き返った
--    (c) 確かめられるのは「管理者か契約者のどなたでも」で、直した方に限っていなかった（管理者が直しても契約者が確かめれば、かかった）
--    (d) 「繋がらなかった」に直した通話は、答えだけを外し、引き継ぎの確かめ・ほかのご希望・お客様が言った連絡先などは生きたままだった
--       （「目的のお客様だと確かめられなかった通話」は 0234 の salescall.call_unconfirmed が全部を外す＝同じ判断が2か所）
-- ⇒ (1) 直しの確かめの記録（correction_calls）は、契約者の直し（直した版）ごとに1つ：直しに従って行の予定を書いた締めが書く（core/settle の1か所。
--        予定を書かなかった締めは書かない）。calls＝その予定が「かける」か。asked_by＝直した方（確かめられるのは、この方だけ）。
--    (2) 確かめ待ち（task_correction_unconfirmed の1か所）：いちばん新しい直しの記録が「かける」で、まだ確かめられておらず、いまの予定が「かける」。
--        直し直して「かけない」にしたら、いちばん新しい記録が「かけない」なので、もう待たない。
--    (3) 目的のお客様だと確かめられなかった通話（call_unconfirmed の1か所）に、契約者の直しを重ねる：いま効いている直し（通話のいまの結果＝call_results が
--        契約者の版）があれば、それが「繋がらなかった」で、システムは目的のお客様と話したと判定していた（または確かめ直しで確かめられなかった）ときだけ
--        確かめられなかった通話。直しが「繋がらなかった」でなければ、確かめられた通話（契約者が、目的のお客様の答えと決めた）。直しが無ければ今までどおり。

ALTER TABLE salescall.correction_calls ADD COLUMN asked_by text, ADD COLUMN calls boolean NOT NULL DEFAULT true;
UPDATE salescall.correction_calls SET asked_by = '（0242 の前の確かめ待ち）' WHERE asked_by IS NULL;   -- 前の確かめ待ちは、どなたも確かめられない（直し直す）
ALTER TABLE salescall.correction_calls ALTER COLUMN asked_by SET NOT NULL,
  ADD CONSTRAINT correction_calls_asked_by CHECK (btrim(asked_by) <> ''), ALTER COLUMN calls DROP DEFAULT;

CREATE OR REPLACE FUNCTION salescall.task_correction_unconfirmed(p_task uuid) RETURNS integer
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT c.version_no FROM salescall.correction_calls c
  WHERE c.task_id = p_task
    AND c.seq = (SELECT max(m.seq) FROM salescall.correction_calls m WHERE m.task_id = p_task)
    AND c.calls
    AND NOT EXISTS (SELECT 1 FROM salescall.correction_call_confirms k WHERE k.task_id = c.task_id AND k.version_no = c.version_no)
    AND EXISTS (SELECT 1 FROM salescall.current_next_actions n WHERE n.task_id = p_task AND n.kind = 'call')
$$;

CREATE OR REPLACE FUNCTION salescall.correction_call_confirm_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT salescall.cur_is_person() THEN
    RAISE EXCEPTION '直しでかける電話を確かめられるのは、管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'correction_call_confirm_by_person';
  END IF;
  IF NEW.confirmed_by IS DISTINCT FROM salescall.cur_actor() THEN
    RAISE EXCEPTION '確かめた方は、いま操作している方です' USING ERRCODE = 'check_violation', CONSTRAINT = 'correction_call_confirmed_by';
  END IF;
  IF NEW.version_no IS DISTINCT FROM salescall.task_correction_unconfirmed(NEW.task_id) THEN
    RAISE EXCEPTION '確かめられるのは、いま確かめ待ちの直しだけです' USING ERRCODE = 'check_violation', CONSTRAINT = 'correction_call_confirm_waiting';
  END IF;
  IF NEW.confirmed_by IS DISTINCT FROM (SELECT c.asked_by FROM salescall.correction_calls c
                                       WHERE c.task_id = NEW.task_id AND c.version_no = NEW.version_no) THEN
    RAISE EXCEPTION '直した結果でかける電話を確かめられるのは、直した方だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'correction_call_confirm_by_corrector';
  END IF;
  RETURN NEW;
END $$;

-- システムが、目的のお客様だと確かめられなかった通話（確かめ直しの答えだけ。判定の材料＝その通話そのものの判定は、これを読む。直しは判定の結果に重ねる）
CREATE FUNCTION salescall.call_recheck_failed(p_attempt uuid) RETURNS boolean LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT EXISTS (SELECT 1 FROM salescall.target_rechecks t WHERE t.attempt_id = p_attempt AND t.outcome <> 'target')
$$;
REVOKE ALL ON FUNCTION salescall.call_recheck_failed(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.call_recheck_failed(uuid) TO salescall_app;

-- 目的のお客様だと確かめられなかった通話（いま効いている直しを重ねた答え。使う答え・いまの連絡先・解決策を求める点・目的のお客様と話せたかが読む）
CREATE OR REPLACE FUNCTION salescall.call_unconfirmed(p_attempt uuid) RETURNS boolean LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT CASE
    WHEN h.business_state IS NULL THEN x.rech                      -- 直しが無い（いま効いている契約者の版が無い）
    WHEN h.business_state = 'not_reached' THEN x.rech OR coalesce(s.business_state <> 'not_reached', false)
    ELSE false END                                                 -- 契約者が、目的のお客様の結果に直した
  FROM (SELECT salescall.call_recheck_failed(p_attempt) AS rech) x
  LEFT JOIN salescall.call_results h ON h.attempt_id = p_attempt AND h.kind = 'human'
  LEFT JOIN LATERAL (SELECT j.business_state FROM salescall.task_results j
                     WHERE j.attempt_id = p_attempt AND j.kind IN ('judged', 'rejudged')
                     ORDER BY j.version_no DESC LIMIT 1) s ON true
$$;

-- ═════════ 番号の持ち主の期は1か所（2026-11-18 の8回目の見直し。DESIGN.md 15-2 の871） ═════════
-- ⛔ 0230〜0241：前の担当の方の番号か（task_former_number）は、持ち主の期を自前の式（書いた持ち主・足した時点の担当の方・言われた通話の期）で決め、
--    0239 の salescall.task_number_period（同じ3つ）と2か所にあった。⇒ task_number_period を読む（決め方は同じ）。
CREATE OR REPLACE FUNCTION salescall.task_former_number(p_task uuid, p_contact uuid, p_at timestamptz) RETURNS boolean
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT coalesce((
    SELECT NOT salescall.task_current_person_number(p_task, p_contact, p_at)
       AND EXISTS (SELECT 1 FROM salescall.task_numbers n WHERE n.task_id = p_task AND n.contact_id = p_contact AND n.added_at <= p_at
                     AND salescall.task_number_period(p_task, p_contact) IS DISTINCT FROM s.since)
    FROM (SELECT salescall.task_person_since(p_task, p_at) AS since) s), false)
$$;
