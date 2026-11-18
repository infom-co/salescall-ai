-- 0044 送り先への連携の守りを1つにする（2026-10-17。書いた人：CL-2）
-- 0043 の「本番で確定した版にしか連携を作れない」参照（deliveries_production_release）は、前からの「確定した版にしか作れない」参照を
-- 含む。同じ守りを2つ持つと、片方を外しても試験が気づけない（守りを外す確かめで分かった）ので、前の参照を外して1つにする。
ALTER TABLE salescall.deliveries DROP CONSTRAINT deliveries_client_id_task_id_version_no_fkey;
