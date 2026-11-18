-- 0054 控えを依頼者ごとに分ける（AI用・提出用）・残す日数・録音の残し方（2026-10-17 ユーザー決定。書いた人：CL-2）
--
-- ユーザー（2026-10-17）：
--   「E:\salescall-backup\client1　バックアップファイル保存場所（クライアントごと）」
--   「AI用と、提出用で分ける。AI用はFly.ioに1か月分　HDに1年　E:\salescall-backup\client1\AI。
--     提出用はFly.ioには保存しない　HDに3年　E:\salescall-backup\client1\submission」
--   AI用＝システムが使っているデータの控え（その依頼者の業務設定・発信先の一覧・通話の結果・やり取りの記録）。
--   提出用＝依頼者へ渡した結果のファイルの控え（渡した回ごと）。
--   「録音の残し方は、Fly.io 7日分、HDに2か月分（両方、状況に応じて変更できるようにする：契約者ごと）」
--     （⇒ 2026-10-16 の「Fly.io に最低半年分、1年分たまったら古い半年分を無料の保管庫へ」を置き換える）
--   「フォルダの名前『client1』『client2』がデフォルト、依頼者タブの自社情報からフォルダ名が自動でできるように」
--
-- (1) 依頼者ごとのフォルダの名前（client_backup_folders）：最初に控えを置くときに、依頼者の名前（依頼者タブの自社情報の会社名）
--     から自動で決め、あとから変えない（名前を直しても、控えが別のフォルダに分かれない）。名前がフォルダ名に使えない・ほかと重なる
--     ときは client1・client2…（決め方は core/backup.py の1か所）。
-- (2) 控えの記録に、依頼者と種類（system＝データベース全体／client_ai＝依頼者ごとのAI用）を持つ。
-- (3) 提出用のファイル（submission_files）：依頼者へ渡したファイルを、出力（CL-3）が渡したときに1回置く。PC の控えの処理が
--     ハードディスクへ写したら、中身を消す（Fly.io には残さない＝ユーザー）。写した記録（いつ・どの名前で）は残す。
-- (4) 残す日数の設定：AI用 ハードディスク 365日・Fly.io 30日（Fly.io の置き場の毎日の写しの設定に写す）、提出用 ハードディスク
--     1,095日、録音 Fly.io 7日・ハードディスク 60日（契約者ごと）。

-- ═════════ (1) 依頼者ごとのフォルダの名前 ═════════
CREATE TABLE salescall.client_backup_folders (
  tenant_id   uuid NOT NULL,
  client_id   uuid PRIMARY KEY,
  folder_name text NOT NULL UNIQUE CHECK (folder_name ~ '^[^\\/:*?"<>|[:cntrl:]]{1,60}$' AND folder_name !~ '[ .]$'
                                          AND lower(folder_name) <> 'system'),
  decided_at  timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id)
);
CREATE TRIGGER client_backup_folders_frozen BEFORE UPDATE OR DELETE ON salescall.client_backup_folders
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
ALTER TABLE salescall.client_backup_folders ENABLE ROW LEVEL SECURITY;
CREATE POLICY client_list ON salescall.client_backup_folders
  USING (tenant_id = salescall.cur_tenant() AND (salescall.cur_client() IS NULL OR client_id = salescall.cur_client()))
  WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client());
GRANT SELECT, INSERT ON salescall.client_backup_folders TO salescall_app;

-- ほかの依頼者・契約者の名前と重ならないかを確かめる（中身は返さない：使われているか だけ）
CREATE FUNCTION salescall.backup_folder_taken(p_name text) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT EXISTS (SELECT 1 FROM salescall.client_backup_folders WHERE lower(folder_name) = lower(p_name))
$$;
REVOKE ALL ON FUNCTION salescall.backup_folder_taken(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.backup_folder_taken(text) TO salescall_app;

-- ═════════ (2) 控えの記録に、依頼者と種類 ═════════
ALTER TABLE salescall.backup_runs
  ADD COLUMN kind text NOT NULL DEFAULT 'system' CHECK (kind IN ('system', 'client_ai')),
  ADD COLUMN client_id uuid,
  ADD CONSTRAINT backup_runs_kind_client CHECK ((kind = 'client_ai') = (client_id IS NOT NULL));
ALTER TABLE salescall.backup_runs ALTER COLUMN kind DROP DEFAULT;

-- ═════════ (3) 提出用のファイル ═════════
CREATE TABLE salescall.submission_files (
  submission_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id     uuid NOT NULL,
  client_id     uuid NOT NULL,
  delivered_at  timestamptz NOT NULL DEFAULT now(),
  file_name     text NOT NULL CHECK (file_name ~ '^[^\\/:*?"<>|[:cntrl:]]{1,120}$'),
  sha256        text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  content       bytea,                       -- 写すまでの間だけ（写したら消す＝Fly.io には残さない）
  copied_at     timestamptz,
  copied_as     text,
  CHECK ((content IS NULL) = (copied_at IS NOT NULL)),
  CHECK ((copied_at IS NULL) = (copied_as IS NULL)),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id)
);
CREATE INDEX submission_files_not_copied ON salescall.submission_files (client_id) WHERE copied_at IS NULL;
-- 変えてよいのは、写したときに1回（中身を消し、いつ・どの名前で写したかを書く）だけ
CREATE FUNCTION salescall.submission_copied_once() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' OR OLD.copied_at IS NOT NULL
     OR (NEW.submission_id, NEW.tenant_id, NEW.client_id, NEW.delivered_at, NEW.file_name, NEW.sha256)
        IS DISTINCT FROM (OLD.submission_id, OLD.tenant_id, OLD.client_id, OLD.delivered_at, OLD.file_name, OLD.sha256) THEN
    RAISE EXCEPTION '提出用のファイルの記録は、写したときに1回だけ（中身を消す）変えられます' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER submission_copied_once BEFORE UPDATE OR DELETE ON salescall.submission_files
  FOR EACH ROW EXECUTE FUNCTION salescall.submission_copied_once();
ALTER TABLE salescall.submission_files ENABLE ROW LEVEL SECURITY;
CREATE POLICY client_scope ON salescall.submission_files
  USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
  WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client());
GRANT SELECT, INSERT, UPDATE ON salescall.submission_files TO salescall_app;

-- ═════════ (4) 残す日数 ═════════
INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, finest_scope) VALUES
('backup.ai.keep_days_drive', 'override', 'integer',
 'AI用の控え（システムが使っているデータ）を、PC に接続したハードディスクに残す日数（過ぎたら消す）', 'DESIGN.md 3-10', 'system'),
('backup.ai.keep_days_cloud', 'override', 'integer',
 'AI用の控えを Fly.io に残す日数（Fly.io の置き場の毎日の写しを残す日数。Fly.io の設定に写す）', 'DESIGN.md 3-10', 'system'),
('backup.submission.keep_days_drive', 'override', 'integer',
 '提出用の控え（依頼者へ渡した結果のファイル）を、PC に接続したハードディスクに残す日数（Fly.io には残さない）', 'DESIGN.md 3-10', 'system'),
('recording.keep_days_cloud', 'override', 'integer', '通話の録音を Fly.io に残す日数（契約者ごと）', 'DESIGN.md 3-8', 'tenant'),
('recording.keep_days_drive', 'override', 'integer', '通話の録音を、PC に接続したハードディスクに残す日数（契約者ごと）', 'DESIGN.md 3-8', 'tenant');
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('backup.ai.keep_days_drive', 'system', '365', 'decided', '2026-10-17 ユーザー決定：「AI用は…HDに1年」', 'サブスク契約のとき', 'ユーザー'),
('backup.ai.keep_days_cloud', 'system', '30', 'decided', '2026-10-17 ユーザー決定：「AI用はFly.ioに1か月分」', 'サブスク契約のとき', 'ユーザー'),
('backup.submission.keep_days_drive', 'system', '1095', 'decided', '2026-10-17 ユーザー決定：「提出用はFly.ioには保存しない　HDに3年」', 'サブスク契約のとき', 'ユーザー'),
('recording.keep_days_cloud', 'system', '7', 'decided', '2026-10-17 ユーザー決定：「Fly.io 7日分…状況に応じて変更できるようにする：契約者ごと」', '契約者が変えたいとき', 'ユーザー'),
('recording.keep_days_drive', 'system', '60', 'decided', '2026-10-17 ユーザー決定：「HDに2か月分…契約者ごと」', '契約者が変えたいとき', 'ユーザー');

-- そのフォルダの名前の依頼者の契約者（古い控えを消すとき、録音の残す日数＝契約者ごとの設定を読むため。中身は返さない）
CREATE FUNCTION salescall.backup_folder_owner(p_name text) RETURNS uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT tenant_id FROM salescall.client_backup_folders WHERE folder_name = p_name
$$;
REVOKE ALL ON FUNCTION salescall.backup_folder_owner(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.backup_folder_owner(text) TO salescall_app;
