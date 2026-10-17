-- 0052 データベースの控えの記録（Intent.md 14-1：毎日自動で取り、Fly.io とは別の場所にも置き、戻せることを確かめ、取れなかったら
-- 知らせる。発信禁止の台帳を失うと、断ったお客様にまたかけるため。2026-10-17。書いた人：CL-2）
-- 書くのは core/backup.py だけ（全体の取引・システムの処理）。読めるのは管理者とシステムの処理だけ（中身は記録の数と指紋だけで、
-- お客様の情報は入れない）。取れなかったときは、止めずに CL-2 へ修正依頼を出し、管理者に知らせる（2026-10-17 ユーザー：
-- 「止めずに CL1 か CL2 に修正依頼を出さないといけないだろ。人はそのプロセスをチェックもしくは承認するだけ」）。
CREATE TABLE salescall.backup_runs (
  run_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  started_at    timestamptz NOT NULL DEFAULT now(),
  finished_at   timestamptz NOT NULL DEFAULT now(),
  file_name     text,                                  -- 控えのファイルの名前（暗号化したもの）
  sha256        text CHECK (sha256 IS NULL OR sha256 ~ '^[0-9a-f]{64}$'),   -- 暗号化したファイルの指紋
  size_bytes    bigint CHECK (size_bytes IS NULL OR size_bytes > 0),
  table_counts  jsonb,                                 -- 取ったときの表ごとの行の数（戻した側と比べる）
  destinations  text[] NOT NULL DEFAULT '{}',         -- 置いた場所の名前（置けた場所だけ）
  restore_ok    boolean NOT NULL,                     -- 戻して、行の数がすべて同じだったか
  error         text CHECK (error IS NULL OR btrim(error) <> ''),
  CHECK (restore_ok = (error IS NULL)),
  CHECK (NOT restore_ok OR (file_name IS NOT NULL AND sha256 IS NOT NULL AND table_counts IS NOT NULL
                            AND cardinality(destinations) >= 2))   -- 済んだ控えは、2か所以上（Fly.io とは別の場所を含む）に置いた
);
CREATE TRIGGER backup_runs_frozen BEFORE UPDATE OR DELETE ON salescall.backup_runs
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
ALTER TABLE salescall.backup_runs ENABLE ROW LEVEL SECURITY;
CREATE POLICY backup_scope ON salescall.backup_runs
  USING (salescall.cur_tenant() IS NULL AND salescall.cur_actor_kind() IN ('admin', 'system'))
  WITH CHECK (salescall.cur_tenant() IS NULL AND salescall.cur_actor_kind() = 'system');
GRANT SELECT, INSERT ON salescall.backup_runs TO salescall_app;
