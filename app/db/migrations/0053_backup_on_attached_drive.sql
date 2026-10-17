-- 0053 データベースの控えは、サブスク契約までは PC に接続したハードディスクに置く（2026-10-17 ユーザー決定。書いた人：CL-2）
-- ユーザー：「クラウドだけでなく、暫定的にPCに接続のハードディスクに保存してください。サブスク契約まで」
-- ⇒ クラウド（Fly.io）の側は、データベースの置き場を Fly.io が毎日自動で写す（ボリュームのスナップショット）。こちらが取る控え
--   （暗号化・戻せるかを毎回確かめる）は、Fly.io とは別の場所＝PC に接続したハードディスクに置く。
--   前は「こちらの控えを2か所以上（Fly.io の中を含む）に置く」をデータベースが守っていたが、こちらの控えを取る処理は PC で動くので、
--   Fly.io の中へは置けない。⇒「1か所以上」に直す（Fly.io とは別の場所があることは、処理の1か所 core/backup.py が守る）。
ALTER TABLE salescall.backup_runs DROP CONSTRAINT backup_runs_check1;
ALTER TABLE salescall.backup_runs ADD CONSTRAINT backup_runs_done_needs_place
  CHECK (NOT restore_ok OR (file_name IS NOT NULL AND sha256 IS NOT NULL AND table_counts IS NOT NULL
                            AND cardinality(destinations) >= 1));
