-- 0098 意味の判定が答えなかったときの分け方を、エラーの表に入れる（CL-1 の連絡帳 4-24。DESIGN.md 9章・15-2 の206。書いた人：CL-2）
--
-- CL-1（2026-10-19。✅ ユーザー）：意味の判定は、会社の違う2つ（3.5 Flash Lite・Claude Sonnet 5.5（考える））を同時に出し、早く
-- 届いたほうで決める。受け持ちの判定が答えなかったら、3つ目の会社の判定（Jev 1.14）が代わりに入る。その分け方を、エラーの表の
-- 1か所に（試作の meaning_judge.outage は本番では使わない＝その場で独自に再試行・切り替えを書かない）：
--   止まっている（時間切れ・つながらない・上限・キーが止められた・混雑）⇒ 決めた秒のあいだ、その判定に問い合わせず代わりへ
--   問い合わせの中身が断られた（400 など）⇒ その発話だけ代わりへ
-- どちらも発信は止めない（代わりの判定と、通話後の検証がある＝お客様に影響が出ない）。

ALTER TABLE salescall.error_events DROP CONSTRAINT error_events_source_check;
ALTER TABLE salescall.error_events ADD CONSTRAINT error_events_source_check
  CHECK (source IN ('ai', 'ear', 'line', 'storage', 'delivery', 'recording', 'supervisor', 'judge'));

INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, finest_scope) VALUES
('judge.outage_bypass', 'override', 'seconds',
 '意味の判定が止まっている（時間切れ・つながらない・上限・キーが止められた・混雑）とみなしたとき、その判定に問い合わせずに代わりの判定'
 '（Jev）へ回す長さ（毎回の発話で、止まった判定の答えを待たない）',
 'DESIGN.md 9章・6-10 の (6)', 'tenant');
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('judge.outage_bypass', 'system', '60', 'provisional',
 'CL-1 の連絡帳 4-24（試作 meaning_judge.DOWN_S＝60秒。判定の形はユーザー承認、秒は CL-1 が決めた初めの値）',
 'フェーズ1で、止まった判定が戻るまでの長さを測って', 'CL-2');

-- 契約者の設定 judge.models（0082）の意味を、いまの判定の形に合わせる（値はそのまま）
ALTER TABLE salescall.setting_definitions DISABLE TRIGGER setting_definitions_frozen;
UPDATE salescall.setting_definitions
SET meaning = '答えの意味の判定の受け持ちに使うAI（sonnet＝3.5 Flash Lite と Claude Sonnet 5.5（考える）の2つを同時に出し、早く届いた'
              'ほうで決める。Jev は、止まった判定の代わりだけ／jev_and_sonnet＝Jev も受け持ちに振り分ける（Jev の確からしさ 0.95 以上は'
              ' Jev）。使い分けて呼ぶのは会話（CL-1）'
WHERE key = 'judge.models';
ALTER TABLE salescall.setting_definitions ENABLE TRIGGER setting_definitions_frozen;
