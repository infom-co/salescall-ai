-- 0067 抜き取る本数は依頼者ごと（2026-10-18 ユーザー決定：「デフォルト、週10本」で、依頼者ごとの設定で選べるようにしてください。
-- 初期の依頼者の場合はチェックが必要ですし、安定しているなら0でもいい可能性があります」。DESIGN.md 10-3・8章。書いた人：CL-2）
--
-- 0066 では業務ごとに決められる形（いちばん細かい範囲＝業務）で、初めの値は仮だった。
-- ⇒ いちばん細かい範囲を依頼者にし（依頼者タブで決める。業務ごとの値は入れられない）、初めの値 10本を決定にする。
--    抜き取りそのものは、今までどおり業務ごと（誤判定率は業務ごとに違う：台本・質問が業務ごとのため）に、その依頼者の値の本数。

ALTER TABLE salescall.setting_definitions DISABLE TRIGGER setting_definitions_frozen;
UPDATE salescall.setting_definitions SET finest_scope = 'client',
  meaning = '誤判定率を測るために、1週間に抜き取って録音を聞く本番の通話の数（依頼者ごと。その依頼者の業務ごとに、前の週の、お客様と話せた通話から無作為に選ぶ。管理者か契約者が録音を聞いて正解を付ける。0 なら抜き取らない）'
WHERE key = 'measure.sample_per_week';
ALTER TABLE salescall.setting_definitions ENABLE TRIGGER setting_definitions_frozen;

INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('measure.sample_per_week', 'system', '10', 'decided',
 '2026-10-18 ユーザー決定：「デフォルト、週10本」で、依頼者ごとの設定で選べるようにしてください。初期の依頼者の場合はチェックが必要ですし、安定しているなら0でもいい可能性があります',
 '依頼者ごとに、誤判定率（上の限度）を見て', 'ユーザー');
