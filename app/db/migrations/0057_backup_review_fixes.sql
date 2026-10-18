-- 0057 控えの見直し（2026-10-18 CL-2。DESIGN.md 3-10・15-2 の153）
--
-- (1) 記録の種類に「古い控えの片付け」（prune）と「控え用のハードディスク」（drive）を足す。片付けが落ちても、記録・修正依頼・
--     知らせが出るように（前は落ちても何も残らなかった）。依頼者ごとの種類でも、依頼者が決まる前の段の失敗（依頼者の一覧が
--     読めない）は依頼者なしで残す。
-- (2) 「戻せることまで確かめた」の決まり（ファイルの名前・指紋・行の数・置き場）は、控えのファイルを作る種類（system・client_ai）
--     だけに当てる（片付けの成功も記録するため）。
-- (3) 残す日数の設定は1日以上（0にすると、取ったばかりの控えを片付けが消していた）。
-- (4) ほかの契約者の情報も読める関数は、システムの処理（控え）だけが呼べる（前は誰の取引からでも呼べ、ほかの契約者の依頼者の
--     名前があるか・契約者の ID・すべての依頼者の ID が分かった）。

-- ═════════ (1)(2) 記録の種類 ═════════
ALTER TABLE salescall.backup_runs DROP CONSTRAINT backup_runs_kind_check;
ALTER TABLE salescall.backup_runs DROP CONSTRAINT backup_runs_kind_client;
ALTER TABLE salescall.backup_runs DROP CONSTRAINT backup_runs_done_needs_place;
ALTER TABLE salescall.backup_runs
  ADD CONSTRAINT backup_runs_kind_check CHECK (kind IN ('system', 'client_ai', 'submission', 'prune', 'drive')),
  ADD CONSTRAINT backup_runs_kind_client CHECK (client_id IS NULL OR kind IN ('client_ai', 'submission')),
  ADD CONSTRAINT backup_runs_done_needs_place CHECK (
    NOT restore_ok OR kind NOT IN ('system', 'client_ai')
    OR (file_name IS NOT NULL AND sha256 IS NOT NULL AND table_counts IS NOT NULL AND cardinality(destinations) >= 1)),
  ADD CONSTRAINT backup_runs_only_files_succeed CHECK (NOT restore_ok OR kind IN ('system', 'client_ai', 'prune'));

-- ═════════ (3) 残す日数は1日以上 ═════════
ALTER TABLE salescall.setting_values ADD CONSTRAINT setting_values_keep_days_at_least_one
  CHECK (key NOT LIKE '%.keep_days_%' OR (jsonb_typeof(value) = 'number' AND (value #>> '{}')::numeric >= 1));

-- ═════════ (4) システムの処理だけが呼べる ═════════
CREATE FUNCTION salescall.only_backup_system() RETURNS void LANGUAGE plpgsql STABLE AS $$
BEGIN
  IF salescall.cur_actor_kind() IS DISTINCT FROM 'system' THEN
    RAISE EXCEPTION '控えの処理（システム）だけが呼べます' USING ERRCODE = 'insufficient_privilege';
  END IF;
END $$;
CREATE OR REPLACE FUNCTION salescall.backup_folder_taken(p_name text) RETURNS boolean
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
BEGIN
  PERFORM salescall.only_backup_system();
  RETURN EXISTS (SELECT 1 FROM salescall.client_backup_folders WHERE lower(folder_name) = lower(p_name));
END $$;
CREATE OR REPLACE FUNCTION salescall.backup_folder_owner(p_name text) RETURNS uuid
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
BEGIN
  PERFORM salescall.only_backup_system();
  RETURN (SELECT tenant_id FROM salescall.client_backup_folders WHERE folder_name = p_name);
END $$;
CREATE OR REPLACE FUNCTION salescall.submissions_waiting() RETURNS TABLE (tenant_id uuid, client_id uuid)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
BEGIN
  PERFORM salescall.only_backup_system();
  RETURN QUERY SELECT DISTINCT f.tenant_id, f.client_id FROM salescall.submission_files f WHERE f.copied_at IS NULL ORDER BY 1, 2;
END $$;
-- 呼べないときに「印なし」を返すと、片付けが測り終わる前の録音を消してしまうので、返さずに止める
CREATE OR REPLACE FUNCTION salescall.recordings_held(p_attempts uuid[]) RETURNS SETOF uuid
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
BEGIN
  PERFORM salescall.only_backup_system();
  RETURN QUERY SELECT DISTINCT h.attempt_id FROM salescall.recording_holds h
               WHERE h.attempt_id = ANY(p_attempts) AND h.released_at IS NULL;
END $$;
