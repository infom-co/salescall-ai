-- 0045 設定ごとに、いちばん細かく決められる範囲を持つ（2026-10-17 ユーザー決定。書いた人：CL-2）
--
-- ユーザー（2026-10-17）：
--   「1回線から1日 300本　依頼者ごと」「同じお客様への最小の間隔 5分　依頼者ごと」「依頼者をまたいだ間隔 3日　契約者」
--   「会話AI・回線の異常で止める回数と、解くまでの確かめ　契約者」「深夜・早朝の歯止め 7〜22時 など　依頼者ごと」
-- ⇒ 前は「システムだけ」（合成の種類 system_only）で、管理者しか変えられなかった（CL-2 が 10-16 に「もともとこの作り」と答えたのは誤り）。
--
-- 形：「どこまで細かく決められるか」（finest_scope：system／tenant＝契約者／client＝依頼者／business＝業務）と、「階層をどう
-- 合成するか」（merge_kind）を分けて持つ。「システムだけ」は「いちばん細かく決められるのがシステム」と同じなので、表し方を1つにする
-- （合成の種類の system_only は使わない。決まりはデータベースの1か所：値を足すときの確かめ）。
--   - 契約者ごと（tenant）：契約者のキー・プロジェクト・回線の扱い・依頼者をまたぐ決まり。依頼者ごとに違う値を持つと、契約者の中で
--     同じものに別々の値が当たる（例：異常の数え方は契約者ごとに数える＝0041）。
--   - 依頼者ごと（client）：依頼者を単位に数える・決めるもの（依頼者の費用・同時の本数・管理番号の扱い）。
--     業務ごとの値を持たせると、読む所（依頼者の単位で読む）が業務の値を黙って読み落とす。
--   - 業務ごと（business）：これまでどおり（上書きの設定の初めの形）。
-- 枠（超えられない歯止め）は、枠の側が、収める設定より細かく決められてはいけない（業務の値で枠を動かせたら、枠にならない）。
-- 枠は、収める設定と同じ範囲で読む（依頼者ごとの歯止めは、その依頼者の値。settings.resolve の1か所）。

ALTER TABLE salescall.setting_definitions ADD COLUMN finest_scope salescall.setting_scope;
ALTER TABLE salescall.setting_definitions DISABLE TRIGGER setting_definitions_frozen;

-- これまで「システムだけ」だったもの：上書きの合成で、いちばん細かく決められるのはシステム（値はシステムの行しか無いので、読む値は同じ）
UPDATE salescall.setting_definitions SET finest_scope = 'system', merge_kind = 'override' WHERE merge_kind = 'system_only';
UPDATE salescall.setting_definitions SET finest_scope = 'business' WHERE finest_scope IS NULL;

-- 2026-10-17 ユーザー決定：依頼者ごと
UPDATE salescall.setting_definitions SET finest_scope = 'client'
WHERE key IN ('line.max_dials_per_day', 'gate.same_contact_min_interval', 'calling.hours_limit');
-- 2026-10-17 ユーザー決定：契約者ごと（解くまでの確かめには、ばたつきで伸ばす待ちも入る）
UPDATE salescall.setting_definitions SET finest_scope = 'tenant'
WHERE key IN ('gate.cross_client_interval',
              'stop.ai.stop_after_failures', 'stop.ai.probe_interval', 'stop.ai.resume_after_successes',
              'stop.line.stop_after_failures', 'stop.line.probe_interval', 'stop.line.resume_after_successes',
              'stop.flapping.window', 'stop.flapping.wait_multiplier', 'stop.flapping.max_wait');
-- 2026-10-16 ユーザー決定（契約者が自分のキー・プロジェクトの値を決める・ほかの契約者との間隔は契約者が決める）：契約者ごと
-- （前は上書きの初めの形で、依頼者・業務の値も入れられ、契約者の中で別々の値が当たりえた）
UPDATE salescall.setting_definitions SET finest_scope = 'tenant'
WHERE key IN ('ai.tpm_limit', 'ai.tpm_per_call', 'ai.tpm_safety', 'ai.ear_tpm_limit', 'gate.other_contractor_interval');
-- 依頼者を単位に数える・決めるもの：依頼者ごと（読む所が依頼者の単位で読む。業務の値を入れられると、黙って読み落とした）
UPDATE salescall.setting_definitions SET finest_scope = 'client'
WHERE key IN ('budget.client_daily_yen', 'budget.max_cost_ratio', 'budget.deficit_grace', 'budget.deficit_confirm_valid',
              'calling.max_concurrent_calls', 'dnc.client_ref_is_stable_id');

-- 意味をいまの決まりに合わせる
UPDATE salescall.setting_definitions SET meaning = v.meaning FROM (VALUES
  ('calling.hours_limit',
   'かけてよい時間帯の歯止め（深夜・早朝。依頼者ごと。業務ごとの時間帯の入力の誤り（21:00 を 01:00 と打つ など）で、この外にかけない。曜日は時間帯の設定で自由に決める。Intent.md 5-4・13章）'),
  ('line.max_dials_per_day',
   '1つの回線（番号）から、日本時間の1日に発信してよい回数の上限（依頼者ごと。番号の評判を守る。数えるのはその回線の発信すべて：共有の回線では、かける依頼者の値で数える。Intent.md 8-6）'),
  ('gate.same_contact_min_interval',
   '同じ連絡先への前の発信が終わってから、次に予約できるまでの最小の間隔（依頼者ごと。次の動きの誤りでかけ続けないための歯止め。ふつうの間隔は再発信の方針で決める）。お客様と約束したかけ直し・お客様の意思と関係なく繋がらなかった／切れた発信は対象外'),
  ('gate.cross_client_interval',
   '同じ契約者の中で、依頼者をまたいで、同じ番号へかけるまでの最小の間隔（契約者ごと）'),
  ('stop.ai.stop_after_failures',
   '会話AIの異常（つながらない・Google の側の誤り・原因の分からない誤り）が何回続いたら、その契約者の新しい発信を止めるか（契約者ごと。直ったら自動で解く）'),
  ('stop.ai.probe_interval', '会話AIの異常で止めたあと、その契約者のキーで試しのセッションを開く間隔（契約者ごと）'),
  ('stop.ai.resume_after_successes', '試しのセッションが何回続けて開けたら解くか（契約者ごと）'),
  ('stop.line.stop_after_failures', '回線の失敗が何回続いたら、その回線を止めるか（契約者ごと）'),
  ('stop.line.probe_interval', '止めた回線の状態を確かめる間隔（契約者ごと）'),
  ('stop.line.resume_after_successes', '回線の確かめが何回続けて正常なら解くか（契約者ごと）'),
  ('stop.flapping.window', 'ばたつき（止まる・解けるの繰り返し）を数える期間（契約者ごと）'),
  ('stop.flapping.wait_multiplier', 'ばたつくたびに、解く前の待ちを何倍にするか（契約者ごと）'),
  ('stop.flapping.max_wait', 'ばたつきで伸ばす待ちの上限（契約者ごと）')
) AS v(key, meaning) WHERE salescall.setting_definitions.key = v.key;

ALTER TABLE salescall.setting_definitions ALTER COLUMN finest_scope SET NOT NULL;
-- 「システムだけ」の表し方は finest_scope の1つ（合成の種類の system_only は、型から外せないので残るが、使わない）
ALTER TABLE salescall.setting_definitions ADD CONSTRAINT setting_definitions_one_way_for_system_only
  CHECK (merge_kind <> 'system_only');
ALTER TABLE salescall.setting_definitions ENABLE TRIGGER setting_definitions_frozen;

-- いまある値が、決められる範囲の内側か（外の値があれば、この移行を通さない）
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM salescall.setting_values v JOIN salescall.setting_definitions d USING (key)
             WHERE v.scope > d.finest_scope) THEN
    RAISE EXCEPTION '決められる範囲より細かい設定の値があります（先に、その値の扱いを決めてください）';
  END IF;
END $$;

-- 枠：同じ値の形で、収める設定より細かく決められない設定でなければならない
CREATE OR REPLACE FUNCTION salescall.setting_definition_before_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE b salescall.setting_definitions;
BEGIN
  IF NEW.bound_key IS NOT NULL THEN
    SELECT * INTO b FROM salescall.setting_definitions WHERE key = NEW.bound_key;
    IF NOT FOUND OR b.finest_scope > NEW.finest_scope OR b.value_type <> NEW.value_type THEN
      RAISE EXCEPTION '設定 % の枠 % は、同じ値の形で、収める設定より細かく決められない設定でなければなりません', NEW.key, NEW.bound_key
        USING ERRCODE = 'check_violation';
    END IF;
    IF (NEW.value_type = 'weekly_hours') <> (NEW.bound_side = 'within') THEN
      RAISE EXCEPTION '時間帯の枠は within、それ以外は upper か lower です' USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  RETURN NEW;
END $$;

-- 値：決められる範囲より細かい範囲には入れられない（決まりはここの1か所）
CREATE OR REPLACE FUNCTION salescall.setting_value_before_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE d salescall.setting_definitions;
BEGIN
  SELECT * INTO d FROM salescall.setting_definitions WHERE key = NEW.key;
  IF NOT salescall.setting_value_ok(d.value_type, NEW.value) THEN
    RAISE EXCEPTION '設定 % の値の形が違います（%）', NEW.key, d.value_type USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.scope > d.finest_scope THEN
    RAISE EXCEPTION '設定 % は、%までしか決められません（%の値は入れられない）', NEW.key,
      CASE d.finest_scope WHEN 'system' THEN 'システム' WHEN 'tenant' THEN '契約者' WHEN 'client' THEN '依頼者' ELSE '業務' END,
      CASE NEW.scope WHEN 'tenant' THEN '契約者' WHEN 'client' THEN '依頼者' ELSE '業務' END
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
