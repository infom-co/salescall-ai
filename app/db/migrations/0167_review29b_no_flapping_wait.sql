-- 0167 2026-11-01 見直し29b の 6。書いた人：CL-2。設計書 9-1・15-2 の411〜。
--
-- ばたつきの待ちの設定（stop.flapping.window・stop.flapping.wait_multiplier・stop.flapping.max_wait）を外す（契約者の値も含めて。0147 の
-- stop.line.* と同じ外し方）。AIの不具合で止めた停止を解く確かめの間隔は、stop.ai.probe_interval の1つだけ（core/watch.record_check）。
-- ご決定：2026-10-31 管理者「確かめが通れば自動で再開します」・2026-10-30 深夜 管理者「AI の不具合として回復するまで一時停止してください」。
-- ⛔ この3つは CL-2 が 0005 で仮に決めた値（1日の中で止まった回数に応じて確かめの間隔を倍々に延ばし、最長4時間）で、ご決定に無かった。
-- 1日に何度か短く止まると、AI がすぐ戻っても16分〜4時間は新しい電話をかけず、その間の時刻ちょうどの約束にかけられなかった。

ALTER TABLE salescall.setting_values DISABLE TRIGGER setting_values_frozen;
ALTER TABLE salescall.setting_definitions DISABLE TRIGGER setting_definitions_frozen;
DELETE FROM salescall.setting_values
WHERE key IN ('stop.flapping.window', 'stop.flapping.wait_multiplier', 'stop.flapping.max_wait');
DELETE FROM salescall.setting_definitions
WHERE key IN ('stop.flapping.window', 'stop.flapping.wait_multiplier', 'stop.flapping.max_wait');
ALTER TABLE salescall.setting_definitions ENABLE TRIGGER setting_definitions_frozen;
ALTER TABLE salescall.setting_values ENABLE TRIGGER setting_values_frozen;
