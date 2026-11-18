-- 0142 意味の判定に使う AI（judge.models）の初めの値と意味を、2026-10-25 夜のご決定（CL-1 の連絡帳 4-57）に合わせる
--      （書いた人：CL-2。DESIGN.md 1-3・12-2）
--
-- ✅ 2026-10-25 夜 ユーザー（CL-1 の連絡帳 4-57）：「以前、JevやSonnet 5.5 の「考えない」形を使うと決めましたよね。同じことを聞くな！」
--   ＝10-23 に中身の判定で決まった形（Jev 1.14 と Sonnet 5.5 の考える・考えない形を同時に出し、先に届いた確かな読みで決める）を、
--   意味の2つ目の判定にも当てる。⇒ 初めの値は jev_and_sonnet（この組）。sonnet は2つの Sonnet の組（Jev は止まった判定の代わりだけ）。
--   選ぶのは契約者（2026-10-18 ユーザー：「契約者設定で、Jev も使うか、Sonnetだけにするかも選択できるようにした方がいいね」）。
--   どちらの組で呼ぶかを決めて呼ぶのは会話（CL-1 の meaning_judge の RACE_NAME・SONNETS_NAME）。
-- 設定の値は追記だけ（新しい行が効く）。意味の文は、0098 と同じく、この移行の中だけ仕掛けを外して直す。
ALTER TABLE salescall.setting_definitions DISABLE TRIGGER setting_definitions_frozen;
UPDATE salescall.setting_definitions
SET meaning = '答えの意味の2つ目の判定に使うAI。jev_and_sonnet＝Jev 1.14 と Claude Sonnet 5.5 の考える形・考えない形を同時に出し、'
              '先に届いた確かな読み（Jev は確からしさ 0.95 から上）で決める（初めの値）／sonnet＝2つの Sonnet 5.5（考える・考えない）だけを'
              '同時に出す（Jev は、止まった判定の代わりだけ）。どちらの組で呼ぶかを決めて呼ぶのは会話（CL-1）'
WHERE key = 'judge.models';
ALTER TABLE salescall.setting_definitions ENABLE TRIGGER setting_definitions_frozen;
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('judge.models', 'system', '"jev_and_sonnet"', 'decided',
 '2026-10-25 夜 ユーザー（CL-1 の連絡帳 4-57）：「以前、JevやSonnet 5.5 の「考えない」形を使うと決めましたよね。同じことを聞くな！」'
 '（10-23 の中身の判定の組を、意味の判定にも当てる。⇒ 2026-10-18 の「当面は、Sonnetだけ」から変わった）',
 'Jev の鍵の期限・Anthropic の使用量の上限が問題になったとき', 'ユーザー');
