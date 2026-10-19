-- 0093 時刻ちょうどの約束で、お客様が出なかったときの回（DESIGN.md 7-1d・15-2 の200。2026-10-19 ユーザー決定。書いた人：CL-2）
--
-- ユーザー（2026-10-19）：「「明日の15時」ちょうどに出られなければ、15時から16時に数回かける。出なければ、翌日の15時、翌々日の
-- 15時で打ち止め」。
-- ⇒ 時刻ちょうどの約束も「回」で数える（幅の約束と同じ作り＝0089。次の動きの _keep_promise の1か所）：
--   (1) 回＝約束の時刻から、この長さ（callback.exact_slot。初め1時間）。その中で、約束の時刻を含めて決めた回数
--       （callback.exact_tries。初め3回＝15時・15時20分・15時40分）かける。
--   (2) 話せなければ、次の日の同じ時刻の回へ。約束の日を含めて決めた日数（callback.exact_days。初め3日＝当日・翌日・翌々日）で
--       打ち止め（終える理由 callback_exact_unreached）。
--   (3) 回に一度もかけられなければ、次につながったときにお詫びから始める（幅の約束と同じ）。

INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, finest_scope) VALUES
('callback.exact_slot', 'override', 'seconds',
 'お客様が時刻ちょうどで約束したかけ直し（「明日の15時」）に出なかったとき、その時刻からかけ直す時間の長さ（この中で決めた回数かける）',
 'DESIGN.md 7-1d', 'client'),
('callback.exact_tries', 'override', 'integer',
 '時刻ちょうどの約束の、その日の時間（callback.exact_slot）の中でかける回数（約束の時刻の1回を含む。等しい間をあけてかける）',
 'DESIGN.md 7-1d', 'client'),
('callback.exact_days', 'override', 'integer',
 '時刻ちょうどの約束に出なかったとき、次の日の同じ時刻にかけ直す日数（約束の日を含む。超えたら終える）',
 'DESIGN.md 7-1d', 'client');
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('callback.exact_slot', 'system', '3600', 'decided', '2026-10-19 ユーザー：「15時から16時に数回かける」', '運用で見直すとき', 'CL-2'),
('callback.exact_tries', 'system', '3', 'provisional', '2026-10-19 ユーザー「数回」：3回（15時・15時20分・15時40分）（仮）',
 'お客様が出る割合を見て', 'CL-2'),
('callback.exact_days', 'system', '3', 'decided', '2026-10-19 ユーザー：「翌日の15時、翌々日の15時で打ち止め」', '運用で見直すとき', 'CL-2');

-- 回数・日数・長さは1以上（0なら約束を一度もかけずに終えてしまう）
ALTER TABLE salescall.setting_values ADD CONSTRAINT setting_values_exact_promise_at_least_one
  CHECK (key NOT IN ('callback.exact_slot', 'callback.exact_tries', 'callback.exact_days')
         OR (jsonb_typeof(value) = 'number' AND (value #>> '{}')::numeric >= 1));
