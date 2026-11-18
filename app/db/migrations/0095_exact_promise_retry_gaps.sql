-- 0095 時刻ちょうどの約束に出なかったときの、その時間の中のかけ直しは「5分後→さらに10分後→さらに20分後」（DESIGN.md 7-1d・
-- 15-2 の203。2026-10-19 ユーザー決定。書いた人：CL-2）
--
-- ユーザー（2026-10-19）：「（3回＝15時・15時20分・15時40分は）5分後→さらに10分後→さらに20分後くらいがいいんじゃない？」。
-- ⇒ 0093 の「回数（callback.exact_tries）で時間を等しく分ける」をやめ、前の電話からの間の並び（callback.exact_retry_gaps。秒。
--   初め 300・600・1200＝15時・15時05分・15時15分・15時35分）にする。並びの数だけかけ直す（約束の時刻の1回＋並びの数）。
--   決めるのは next_action._exact_next_try の1か所。

ALTER TABLE salescall.setting_values DISABLE TRIGGER setting_values_frozen;
ALTER TABLE salescall.setting_definitions DISABLE TRIGGER setting_definitions_frozen;
DELETE FROM salescall.setting_values WHERE key = 'callback.exact_tries';
DELETE FROM salescall.setting_definitions WHERE key = 'callback.exact_tries';
ALTER TABLE salescall.setting_definitions ENABLE TRIGGER setting_definitions_frozen;
ALTER TABLE salescall.setting_values ENABLE TRIGGER setting_values_frozen;

INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, finest_scope) VALUES
('callback.exact_retry_gaps', 'override', 'integer_list',
 'お客様が時刻ちょうどで約束したかけ直し（「明日の15時」）に出なかったとき、その時間（callback.exact_slot）の中で、前の電話から'
 'あける間（秒）を順に並べたもの。並びの数だけかけ直す（初め：5分後・さらに10分後・さらに20分後）',
 'DESIGN.md 7-1d', 'client');
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('callback.exact_retry_gaps', 'system', '[300, 600, 1200]', 'decided',
 '2026-10-19 ユーザー：「5分後→さらに10分後→さらに20分後くらいがいいんじゃない？」', '運用で見直すとき', 'CL-2');

-- 1以上の決まり（0093）から、外した設定を除く（並びの1つずつが1以上なのは、値の形 integer_list が守る）
ALTER TABLE salescall.setting_values DROP CONSTRAINT setting_values_exact_promise_at_least_one;
ALTER TABLE salescall.setting_values ADD CONSTRAINT setting_values_exact_promise_at_least_one
  CHECK (key NOT IN ('callback.exact_slot', 'callback.exact_days')
         OR (jsonb_typeof(value) = 'number' AND (value #>> '{}')::numeric >= 1));
