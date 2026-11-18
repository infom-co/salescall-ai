-- 0037 会話AIの1分の上限は、門で待たせず、キーを併用して進める（2026-10-17 ユーザー決定）。
-- 書いた人：CL-2
-- ユーザー：「なぜそんな時間の無駄の仕組みを作るんですか？1分制限を超えそうになったら、きりのいいところで、次のキーと併用して
--   進んだらいいんじゃないんですか。前から何度も言ってるけど、作業を止めるなって」。
-- - 前（0005・0032）は、門が「契約者の進行中の発信の数 × 1本の見込み」が上限を超えるなら発信を通さなかった（理由 ai_capacity）。
--   これを外す。1分の上限が近いときは、キーの選択（CL-1・Q15）が、きりのいいところ（新しいセッションを開くとき）で次のキーを
--   併せて使う。上限の値は、そのための目安として残す（設定の名前を gate. から ai. に変える。門は読まない）。
-- - 使わなくなった、契約者の進行中の発信を数える関数を外す。

DROP FUNCTION salescall.active_attempts_of_tenant();

-- 設定の名前を変える（定義と値は変えない決まりなので、表の持ち主が、この移行の中でだけ拒みを外して直す）
ALTER TABLE salescall.setting_values DROP CONSTRAINT setting_values_key_fkey;
ALTER TABLE salescall.setting_definitions DISABLE TRIGGER setting_definitions_frozen;
ALTER TABLE salescall.setting_values DISABLE TRIGGER setting_values_frozen;
UPDATE salescall.setting_definitions SET key = replace(key, 'gate.ai_tpm_', 'ai.tpm_'),
  design_ref = 'DESIGN.md 5-2 の9（キーの選択 Q15 が使う。門は使わない）'
WHERE key LIKE 'gate.ai\_tpm\_%';
UPDATE salescall.setting_definitions SET meaning =
  '会話AIの1つのプロジェクトの、1分あたりの入力トークン数の上限（契約者ごと。これに近づいたら、キーの選択が次のキーを併せて使う）'
WHERE key = 'ai.tpm_limit';
UPDATE salescall.setting_values SET key = replace(key, 'gate.ai_tpm_', 'ai.tpm_') WHERE key LIKE 'gate.ai\_tpm\_%';
ALTER TABLE salescall.setting_values ENABLE TRIGGER setting_values_frozen;
ALTER TABLE salescall.setting_definitions ENABLE TRIGGER setting_definitions_frozen;
ALTER TABLE salescall.setting_values ADD CONSTRAINT setting_values_key_fkey
  FOREIGN KEY (key) REFERENCES salescall.setting_definitions (key);
