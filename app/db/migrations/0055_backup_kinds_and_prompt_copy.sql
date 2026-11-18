-- 0055 控えの見直しで見つけた食い違いを直す（2026-10-17 CL-2。DESIGN.md 3-10・15-2 の150）
--
-- (1) 提出用の控えの失敗が、記録では「依頼者ごとの AI用」と同じ種類になっていた ⇒ 種類に submission を足す。
-- (2) 提出用のファイルは「Fly.io には保存しない」（2026-10-17 ユーザー）のに、写すのが毎日1回だけで、最長1日 Fly.io に残っていた
--     ⇒ 見守り役が数分ごとに写せるよう、まだ写していないファイルのある依頼者を返す（中身は返さない）。
-- (3) フォルダの名前は Windows では大文字・小文字を区別しないのに、表は区別して重なりを見ていた（処理の側だけで見ていた）
--     ⇒ 表でも、大文字・小文字を区別せずに1つにする。

-- ═════════ (1) 控えの記録の種類 ═════════
ALTER TABLE salescall.backup_runs DROP CONSTRAINT backup_runs_kind_check;
ALTER TABLE salescall.backup_runs DROP CONSTRAINT backup_runs_kind_client;
ALTER TABLE salescall.backup_runs
  ADD CONSTRAINT backup_runs_kind_check CHECK (kind IN ('system', 'client_ai', 'submission')),
  ADD CONSTRAINT backup_runs_kind_client CHECK ((kind <> 'system') = (client_id IS NOT NULL));

-- ═════════ (2) まだ写していない提出用のファイルのある依頼者 ═════════
CREATE FUNCTION salescall.submissions_waiting() RETURNS TABLE (tenant_id uuid, client_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT DISTINCT tenant_id, client_id FROM salescall.submission_files WHERE copied_at IS NULL ORDER BY 1, 2
$$;
REVOKE ALL ON FUNCTION salescall.submissions_waiting() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.submissions_waiting() TO salescall_app;

-- ═════════ (3) フォルダの名前は大文字・小文字を区別せずに1つ ═════════
CREATE UNIQUE INDEX client_backup_folders_one_name ON salescall.client_backup_folders (lower(folder_name));
