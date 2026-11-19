-- 0244 時刻の無い幅の約束でも、途中で切れた電話のすぐのかけ直しの期限を延ばせる（2026-11-18 の8回目の見直し。書いた人：CL-2。DESIGN.md 15-2 の870）
--
-- ⛔ 0216：回の期限（window_recover_until）は、時刻ちょうどの約束の取り戻す電話（core/next_action._recover_today）のためだけに作り、約束の時刻
--    （callee_at）の無い予定には持てなかった。2026-11-18 の7回目の見直しで、途中で切れた約束の電話のすぐのかけ直し（_resume_window）にも同じ期限を
--    持たせたが、時刻の無い幅の約束（「3時以降」「夕方なら」）で回の終わりのあとに切れると、締めがこの守りで落ちた（試験は時刻ちょうどの約束だけだった）。
-- ⇒ 期限は、かける予定で幅があれば持てる（時刻の有無は問わない。門は期限まで歯止め時間の中でかける＝calling_hours.in_window の1か所）。
ALTER TABLE salescall.task_next_actions DROP CONSTRAINT task_next_actions_window_recover;
ALTER TABLE salescall.task_next_actions ADD CONSTRAINT task_next_actions_window_recover
  CHECK (window_recover_until IS NULL OR (kind = 'call' AND window_until IS NOT NULL));
