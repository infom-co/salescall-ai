-- 0277 名前の録音の審査で、管理者が音を聞く入口（2026-11-21。書いた人：CL-2。DESIGN_15-2.md の942。CL-3 の連絡帳のお願い 68）
--
-- 名前の録音の審査（0173 の name_recordings_to_review・review_name_recordings）は、管理者がシステムの取引で、いろいろな契約者の録音をまとめて流して
-- レ点で決める（✅ 2026-11-04 管理者：「まとめて流して…」）。ところが音の置き場（0275 の recording_audio）は契約者・依頼者の範囲の守り（recording_scope）
-- なので、システムの取引からは契約者の録音の音が読めず、聞かずに決める形しか作れなかった。
-- ⇒ 審査を待っている組（name_review_pending の1か所＝一覧に出る組と同じ）の部分の音だけを、管理者のシステムの取引で読む関数。読む範囲の決まりは
--   録音の記録の守りと同じ（その組の契約者の音・その組の依頼者の音・システムの音）。聞いた記録（recording_listens）も残す（だれが＝取引の方）。
--   審査を待っていない組・無い部分・置き場に無い音は NULL（聞かせない）。管理者でない・システムの取引でないときは、審査の守りと同じ name_review_by_admin。
CREATE FUNCTION salescall.review_listen(p_set uuid, p_part text) RETURNS bytea
LANGUAGE plpgsql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
DECLARE heard bytea;
BEGIN
  IF salescall.cur_actor_kind() IS DISTINCT FROM 'admin' OR salescall.cur_tenant() IS NOT NULL THEN
    RAISE EXCEPTION '名前の録音をシステムに入れるかを決めるのは、管理者だけです（システムの取引で）'
      USING ERRCODE = 'insufficient_privilege', CONSTRAINT = 'name_review_by_admin';
  END IF;
  SELECT a.audio INTO heard
  FROM salescall.name_review_pending() p
  JOIN salescall.recording_sets s ON s.set_id = p.set_id
  JOIN salescall.recording_audio a ON a.audio_sha256 = p.audio_sha256
   AND (a.tenant_id IS NULL OR (a.tenant_id = s.tenant_id AND (a.client_id IS NULL OR a.client_id = s.client_id)))
  WHERE p.set_id = p_set AND p.part = p_part
  LIMIT 1;
  IF heard IS NOT NULL THEN
    INSERT INTO salescall.recording_listens (set_id, part) VALUES (p_set, p_part);
  END IF;
  RETURN heard;
END $$;
REVOKE ALL ON FUNCTION salescall.review_listen(uuid, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.review_listen(uuid, text) TO salescall_app;
