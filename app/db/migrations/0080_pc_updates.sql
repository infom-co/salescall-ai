-- 0080 PC のプログラムの更新（DESIGN.md 5-4-1・15-2 の177。2026-10-18 ユーザー決定。書いた人：CL-2）
--
-- ユーザー（2026-10-18）：「インストーラーは作ります。入れるだけで済む形にします。⇒はい、ただし、プログラム更新やバージョンアップが
-- 自動もしくは手動で反映されるようにしてください。」
-- (1) 自動か手動か（契約者ごとの設定 pc.auto_update。初めは自動）。手動なら、契約者に「更新してください」と知らせ、画面の「更新」で
--     反映する。どちらでも、通話の途中では入れ替えない（いまの通話を終えて、空いているときに）。
-- (2) PC のプログラムとデータベースの版がずれたまま、新しい電話をかけない（見守り役が、`salescall.schema_version` と、プログラムが
--     持つ表の作りの版を比べる＝supervisor/updates.py）。古いプログラムが新しいデータベースの決まりを知らずにかけると、守りが外れうる。

INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, finest_scope) VALUES
('pc.auto_update', 'override', 'boolean',
 'PC のプログラムの更新を、自動で反映するか（はい＝空いているときに自動で入れ替える／いいえ＝契約者に知らせ、画面の「更新」で反映する）',
 'DESIGN.md 5-4-1', 'tenant');
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('pc.auto_update', 'system', 'true', 'provisional',
 '2026-10-18 ユーザー：「自動もしくは手動で反映されるように」。初めは自動（更新を待つあいだ、版がずれた PC は新しい電話をかけないので、待つ時間を短くする）',
 'インストーラーを作るとき', 'CL-2');
