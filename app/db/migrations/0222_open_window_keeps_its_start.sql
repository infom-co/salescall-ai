-- 0222 終わりの無い幅の約束も、始まり（これより前にはかけない）を持てる（2026-11-15。書いた人：CL-2。DESIGN.md 7-2・2章 Q248・15-2 の807）
--
-- お客様のほうから折り返すお約束（「〇時に戻ります」「来年戻ります」）のこちらからかける約束は、終わり無しの幅で、その始まりが戻りの30分後に期限が
-- 来る予約の時刻（next_action._theirs_at・gate.earliest_claim）。⛔ ここまで：始まり window_from を持てるのは終わり window_until のある幅だけ（0089）で、
-- 終わりの無い幅は始まりを捨てて書いた。かけてよい時間帯が見つからず1日後に確かめ直す予定（暦の外＝「来年戻ります」など）・番号を直したあとの
-- かけ始めでは、門の幅の確かめ（calling_hours.in_window の since）に始まりが無く、戻る前の電話を通した（「〇日以降」も同じ道）。
-- 回の決め方（window_repeat）は、今までどおり終わりのある幅だけ（回の無い幅は、次の回の決め方を持たない）。

ALTER TABLE salescall.task_next_actions DROP CONSTRAINT task_next_actions_window_from;
ALTER TABLE salescall.task_next_actions ADD CONSTRAINT task_next_actions_window_from CHECK (
  window_from IS NULL OR window_until IS NULL OR window_from < window_until);
