-- 0106 サーバーと PC の分け方の (B)(C)：控えの片付けの決まりはサーバー、ハードディスクの中を消すのは PC（DESIGN.md 5-5）。
-- 書いた人：CL-2

-- (1) 測り終わるまで消さない録音：前は、PC がハードディスクの中で見つけた通話の番号を渡して聞いた。PC はデータベースにつながないので、
--     サーバーが、その契約者の消さない通話をまとめて PC へ渡す（片付けの決まりの中。中身は返さない）。
DROP FUNCTION salescall.recordings_held(uuid[]);
CREATE FUNCTION salescall.recordings_held() RETURNS SETOF uuid
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
BEGIN
  PERFORM salescall.only_backup_system();
  IF salescall.cur_tenant() IS NULL THEN
    RAISE EXCEPTION '契約者の取引で呼びます' USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN QUERY SELECT DISTINCT h.attempt_id FROM salescall.recording_holds h
               WHERE h.tenant_id = salescall.cur_tenant() AND h.released_at IS NULL ORDER BY 1;
END $$;
REVOKE ALL ON FUNCTION salescall.recordings_held() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.recordings_held() TO salescall_app;

-- (2) 通行証から、PC の名前も返す（PC の上で押した緊急停止を、どの PC で押したかつきで記録するため。0105 の関数を置き換える）
DROP FUNCTION salescall.agent_by_token(text);
CREATE FUNCTION salescall.agent_by_token(p_sha text) RETURNS TABLE (pc_id uuid, tenant_id uuid, label text)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
BEGIN
  PERFORM salescall.hub_only();
  RETURN QUERY SELECT a.pc_id, a.tenant_id, a.label FROM salescall.pc_agents a WHERE a.token_sha256 = p_sha AND a.revoked_at IS NULL;
END $$;
REVOKE ALL ON FUNCTION salescall.agent_by_token(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.agent_by_token(text) TO salescall_app;
