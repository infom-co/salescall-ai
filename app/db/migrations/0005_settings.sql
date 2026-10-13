-- 0005 設定の定義と値（DESIGN.md 8章・9-1・10-1・5-2-1）。書いた人：CL-2（2026-10-13）
-- ユーザー：「忘れないように、最適だと思う値を入れておいた方がよくない？」
-- - 設定は、まず「定義」（名前・合成の種類・値の形・単位）を持つ。定義の無い設定の値は作れない（I-14）。
-- - 値は追記だけ（版と変更の記録。最新がいまの値）。値ごとに「仮か確定か」「根拠」「いつ見直すか」を必ず持つ。
--   ⇒ 黙って入る既定値ではない（原則10）。仮の値は一覧で見え、見直しの合図が書いてある。
-- - 値の正本はこの表だけ。設計書は、この表を指す（同じ数を2か所に書かない）。
-- - 2026-10-13 ユーザー：「この一覧の項目は、ダッシュボードの設定で、依頼主タブごとに設定できるように」。
--   ⇒ 依頼主ごとに意味のある設定は、契約者・依頼主・業務の範囲でも値を持てる（合成の種類で、システムの上限より緩められない）。
--   全依頼主が共有する設備（データベース・会話AIのプロジェクト・回線・見守り役）の値は、システムだけ（依頼主ごとに変えると、
--   同じ設備に別々の値が当たって、ぶつかる）。

CREATE TYPE salescall.merge_kind AS ENUM (
  'override',      -- 上書き：業務 ＞ 依頼主 ＞ 契約者 ＞ システム共通の順で、最初に値があるもの
  'min',           -- 厳しいほう（上限）：すべての階層のうち、いちばん小さい値
  'max',           -- 厳しいほう（下限）：すべての階層のうち、いちばん大きい値（間隔・本数・一致の割合の下限など）
  'intersect',     -- 厳しいほう（範囲）：すべての階層の範囲の重なり
  'union',         -- 足し合わせ（禁止）：すべての階層の和
  'system_only'    -- システムだけ：契約者・依頼主・業務からは変えられない
);
CREATE TYPE salescall.setting_value_type AS ENUM ('integer', 'ratio', 'seconds', 'integer_list', 'weekly_hours');
CREATE TYPE salescall.setting_status AS ENUM ('provisional', 'decided');  -- 仮／確定

CREATE TABLE salescall.setting_definitions (
  key         text PRIMARY KEY CHECK (key ~ '^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$'),
  merge_kind  salescall.merge_kind NOT NULL,
  value_type  salescall.setting_value_type NOT NULL,
  meaning     text NOT NULL CHECK (btrim(meaning) <> ''),   -- 何を決める値か（日本語）
  design_ref  text NOT NULL CHECK (btrim(design_ref) <> '') -- 設計書のどこで使うか
);

-- 値の形の確かめ（1か所。読み込む処理も、ここと同じ形を前提にする）
CREATE FUNCTION salescall.setting_whole_number(v jsonb, minimum numeric) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
  -- ⚠️ 「かつ」の左から順に調べるとは限らないので、CASE で順を決める（数でない値を数に直そうとして落ちない）
  SELECT CASE WHEN jsonb_typeof(v) <> 'number' THEN false
              ELSE (v #>> '{}')::numeric = trunc((v #>> '{}')::numeric) AND (v #>> '{}')::numeric >= minimum END
$$;

CREATE FUNCTION salescall.setting_hours_ok(d jsonb) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE WHEN jsonb_typeof(d) <> 'array' THEN false
              ELSE NOT EXISTS (
                SELECT 1 FROM jsonb_array_elements(d) r
                WHERE CASE WHEN jsonb_typeof(r) <> 'array' THEN true
                           WHEN jsonb_array_length(r) <> 2 THEN true
                           WHEN jsonb_typeof(r -> 0) <> 'string' OR jsonb_typeof(r -> 1) <> 'string' THEN true
                           WHEN NOT ((r ->> 0) ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$') THEN true
                           WHEN NOT ((r ->> 1) ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$') THEN true
                           ELSE (r ->> 0) >= (r ->> 1) END) END
$$;

-- 値の形の確かめ（1か所。読み込む処理も、ここと同じ形を前提にする）
CREATE FUNCTION salescall.setting_value_ok(t salescall.setting_value_type, v jsonb) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE t
    WHEN 'integer' THEN salescall.setting_whole_number(v, 0)
    WHEN 'seconds' THEN salescall.setting_whole_number(v, 1)
    WHEN 'ratio'   THEN CASE WHEN jsonb_typeof(v) <> 'number' THEN false
                             ELSE (v #>> '{}')::numeric BETWEEN 0 AND 1 END
    WHEN 'integer_list' THEN CASE WHEN jsonb_typeof(v) <> 'array' THEN false
                                  WHEN jsonb_array_length(v) < 1 THEN false
                                  ELSE NOT EXISTS (SELECT 1 FROM jsonb_array_elements(v) e
                                                   WHERE NOT salescall.setting_whole_number(e, 1)) END
    -- 曜日ごとの時間帯（日本時間）。例：{"mon": [["09:00","18:00"]], ..., "sun": [], "holiday": []}。8つの鍵が全部要る
    WHEN 'weekly_hours' THEN CASE WHEN jsonb_typeof(v) <> 'object' THEN false
                                  WHEN (SELECT count(*) FROM jsonb_object_keys(v)) <> 8 THEN false
                                  WHEN NOT (v ?& ARRAY['mon','tue','wed','thu','fri','sat','sun','holiday']) THEN false
                                  ELSE NOT EXISTS (SELECT 1 FROM jsonb_each(v) d WHERE NOT salescall.setting_hours_ok(d.value)) END
  END
$$;

CREATE TABLE salescall.setting_values (     -- 値（追記だけ。範囲ごと・鍵ごとの最新が、いまの値）
  value_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  key         text NOT NULL REFERENCES salescall.setting_definitions,
  scope       salescall.stop_scope NOT NULL,     -- system / tenant / client / business（停止と同じ範囲の名前）
  tenant_id   uuid REFERENCES salescall.tenants,
  client_id   uuid,
  business_id uuid,
  value       jsonb NOT NULL,
  status      salescall.setting_status NOT NULL,
  rationale   text NOT NULL CHECK (btrim(rationale) <> ''),     -- 根拠（なぜこの値か）
  review_when text NOT NULL CHECK (btrim(review_when) <> ''),   -- いつ見直すか（仮の値は必ず。確定でも見直しの合図を書く）
  set_at      timestamptz NOT NULL DEFAULT now(),
  set_by      text NOT NULL CHECK (btrim(set_by) <> ''),
  CONSTRAINT setting_values_scope_target CHECK (
       (scope = 'system'   AND tenant_id IS NULL     AND client_id IS NULL     AND business_id IS NULL)
    OR (scope = 'tenant'   AND tenant_id IS NOT NULL AND client_id IS NULL     AND business_id IS NULL)
    OR (scope = 'client'   AND tenant_id IS NOT NULL AND client_id IS NOT NULL AND business_id IS NULL)
    OR (scope = 'business' AND tenant_id IS NOT NULL AND client_id IS NOT NULL AND business_id IS NOT NULL)),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, business_id) REFERENCES salescall.businesses (client_id, business_id)
);
CREATE INDEX setting_values_current ON salescall.setting_values (key, scope, tenant_id, client_id, business_id, value_id DESC);

CREATE FUNCTION salescall.setting_value_before_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE d salescall.setting_definitions;
BEGIN
  SELECT * INTO d FROM salescall.setting_definitions WHERE key = NEW.key;
  IF NOT salescall.setting_value_ok(d.value_type, NEW.value) THEN
    RAISE EXCEPTION '設定 % の値の形が違います（%）', NEW.key, d.value_type USING ERRCODE = 'check_violation';
  END IF;
  IF d.merge_kind = 'system_only' AND NEW.scope <> 'system' THEN
    RAISE EXCEPTION '設定 % はシステムだけが決めます（契約者・依頼主・業務からは変えられない）', NEW.key
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER setting_values_before_insert BEFORE INSERT ON salescall.setting_values
  FOR EACH ROW EXECUTE FUNCTION salescall.setting_value_before_insert();
CREATE TRIGGER setting_values_frozen BEFORE UPDATE OR DELETE ON salescall.setting_values
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
CREATE TRIGGER setting_definitions_frozen BEFORE UPDATE OR DELETE ON salescall.setting_definitions
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();

ALTER TABLE salescall.setting_values ENABLE ROW LEVEL SECURITY;
CREATE POLICY setting_scope ON salescall.setting_values
  -- 依頼主・業務の範囲の行は、その依頼主の取引でだけ読める（同じ契約者のほかの依頼主からも見えない。2026-10-13 に締めた）
  USING (scope = 'system'
         OR (scope = 'tenant' AND tenant_id = salescall.cur_tenant())
         OR (scope IN ('client', 'business') AND tenant_id = salescall.cur_tenant()
             AND client_id = salescall.cur_client()))
  WITH CHECK ((scope = 'system' AND salescall.cur_tenant() IS NULL)
              OR (scope = 'tenant' AND tenant_id = salescall.cur_tenant())
              OR (scope IN ('client', 'business') AND tenant_id = salescall.cur_tenant()
                  AND client_id = salescall.cur_client()));
GRANT SELECT ON salescall.setting_definitions, salescall.setting_values TO salescall_app;
GRANT INSERT ON salescall.setting_values TO salescall_app;
GRANT EXECUTE ON FUNCTION salescall.setting_value_ok(salescall.setting_value_type, jsonb),
  salescall.setting_whole_number(jsonb, numeric), salescall.setting_hours_ok(jsonb) TO salescall_app;

-- ═════════ 定義 ═════════
INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref) VALUES
-- 停止と自動の解除（9-1）
('stop.storage.probe_interval', 'system_only', 'seconds', '保存の失敗で止めたあと、試しの書き込みをする間隔', 'DESIGN.md 9-1'),
('stop.storage.resume_after_successes', 'system_only', 'integer', '試しの書き込みが何回続けて成功したら解くか', 'DESIGN.md 9-1'),
('stop.line.stop_after_failures', 'system_only', 'integer', '回線の失敗が何回続いたら、その回線を止めるか', 'DESIGN.md 9-1'),
('stop.line.probe_interval', 'system_only', 'seconds', '止めた回線の状態を確かめる間隔', 'DESIGN.md 9-1'),
('stop.line.resume_after_successes', 'system_only', 'integer', '回線の確かめが何回続けて正常なら解くか', 'DESIGN.md 9-1'),
('stop.ai.stop_after_failures', 'system_only', 'integer', '会話AIにつながらないことが何回続いたら、全体を止めるか', 'DESIGN.md 9-1'),
('stop.ai.probe_interval', 'system_only', 'seconds', '止めたあと、試しのセッションを開く間隔', 'DESIGN.md 9-1'),
('stop.ai.resume_after_successes', 'system_only', 'integer', '試しのセッションが何回続けて開けたら解くか', 'DESIGN.md 9-1'),
('stop.ai_rate_limit.backoff', 'system_only', 'integer_list', '会話AIの混雑・1分あたりの上限に当たったとき、次に試すまでの待ち（秒）を、当たるたびに順に使う（最後の値のあとは最後の値のまま）', 'DESIGN.md 9-1・9章'),
('stop.flapping.window', 'system_only', 'seconds', 'ばたつき（止まる・解けるの繰り返し）を数える期間', 'DESIGN.md 9-1'),
('stop.flapping.wait_multiplier', 'system_only', 'integer', 'ばたつくたびに、解く前の待ちを何倍にするか', 'DESIGN.md 9-1'),
('stop.flapping.max_wait', 'system_only', 'seconds', 'ばたつきで伸ばす待ちの上限', 'DESIGN.md 9-1'),
-- 自動の品質の門（10-1）
('quality.mock.calls', 'max', 'integer', '依頼主の最初の業務を本番に上げる前に流す模擬通話の本数（2026-10-13 ユーザー：依頼主ごとに最初のみ）', 'DESIGN.md 10-1'),
('quality.mock.min_value_match', 'max', 'ratio', '模擬通話で、取れた値が正解と一致した割合の下限', 'DESIGN.md 10-1'),
('quality.mock.min_judgment_match', 'max', 'ratio', '模擬通話で、業務の判定が正解と一致した割合の下限', 'DESIGN.md 10-1'),
('quality.stage.sizes', 'system_only', 'integer_list', '段階的な立ち上げの各段の件数（会話に至った通話の数。最後の段のあとは全部）', 'DESIGN.md 10-1'),
('quality.max_ear_disagreement', 'min', 'ratio', '2つの耳が食い違った項目の割合の上限', 'DESIGN.md 10-1'),
('quality.max_unknown_judgments', 'min', 'integer', '判定できない（unknown）の件数の上限', 'DESIGN.md 10-1'),
('quality.max_forbidden_speech', 'min', 'integer', '言ってはいけないことの検出（2つの耳で確かめたもの）の件数の上限', 'DESIGN.md 10-1'),
('quality.max_complaint', 'min', 'ratio', '苦情があった通話の割合の上限', 'DESIGN.md 10-1'),
('quality.max_technical_error', 'min', 'ratio', '技術的な失敗の割合の上限', 'DESIGN.md 10-1'),
('quality.max_verification_failed', 'min', 'ratio', '通話後の検証が確かめられなかった割合の上限', 'DESIGN.md 10-1'),
-- 門（5-2・5-2-1）
('gate.claim_lifetime', 'system_only', 'seconds', '予約証の期限（予約から発信までの間）', 'DESIGN.md 5-2-1'),
('gate.unknown_result_min_interval', 'max', 'seconds', '結果が分からない試行（technical_error）のあと、同じ連絡先へかけるまでの最小の間隔', 'DESIGN.md 4-2・5-2'),
('gate.cross_client_interval', 'max', 'seconds', '依頼主をまたいで、同じ番号へかけるまでの最小の間隔', 'DESIGN.md 5-2 の5'),
('gate.denied_retry_wait', 'system_only', 'seconds', '門が待てば変わる理由で通さなかったあと、次に候補に出すまでの待ち', 'DESIGN.md 7-2'),
('gate.ai_tpm_limit', 'system_only', 'integer', '会話AIの1つのプロジェクトの、1分あたりの入力トークン数の上限', 'DESIGN.md 5-2 の9'),
('gate.ai_tpm_per_call', 'system_only', 'integer', '1本の通話の、1分あたりの入力トークン数の見込み', 'DESIGN.md 5-2 の9'),
('gate.ai_tpm_safety', 'system_only', 'ratio', '上限のうち、実際に使ってよい割合（見込みの外れへの余裕）', 'DESIGN.md 5-2 の9'),
('gate.ear_tpm_limit', 'system_only', 'integer', '別の耳（通話後の検証）の1分あたりの入力トークン数の上限', 'DESIGN.md 5-2 の9・6-12'),
('calling.hours', 'intersect', 'weekly_hours', 'かけてよい曜日と時間帯（日本時間。祝日は holiday）', 'DESIGN.md 5-2 の2・Intent.md 13章'),
('calling.max_concurrent_calls', 'min', 'integer', '同時に流す通話の本数の上限（会話AIの枠から決まる上限。実際の本数は、登録した回線の数でも絞られる：1回線＝同時1本はデータベースの約束）', 'DESIGN.md 5-2・Intent.md 5-4'),
('retry.max_attempts_per_task', 'min', 'integer', '1つのタスク（行）に発信してよい回数の上限（再発信の方針はこの内側）', 'DESIGN.md 7-2'),
-- 通話後の検証と回線の借用
('verification.max_retries', 'system_only', 'integer', '通話後の検証を自動でやり直す回数の上限', 'DESIGN.md 5-2-1 の9'),
('verification.retry_wait', 'system_only', 'seconds', '通話後の検証をやり直すまでの待ち', 'DESIGN.md 5-2-1 の9'),
('line.lease', 'system_only', 'seconds', '見守り役が回線を借りる期限', 'DESIGN.md 4-2'),
('line.lease_renew', 'system_only', 'seconds', '回線の借用を延ばす間隔', 'DESIGN.md 4-2');

-- ═════════ 最初の値（どれも「仮」。根拠と、いつ見直すかつき） ═════════
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('stop.storage.probe_interval', 'system', '30', 'provisional', '保存が戻ったことを早く知りたいが、データベースに負担をかけない間隔', '本番の最初の1か月の停止の記録を見て', 'CL-2'),
('stop.storage.resume_after_successes', 'system', '3', 'provisional', '1回の成功は偶然でありうる。3回続けば戻ったとみなせる（30秒間隔で約1分半）', '同上', 'CL-2'),
('stop.line.stop_after_failures', 'system', '3', 'provisional', '1〜2回の失敗は相手側の事情でもありうる。3回続けば回線側を疑う', 'Rakuten Link の確かめ（フェーズ0 の R）のあと', 'CL-2'),
('stop.line.probe_interval', 'system', '60', 'provisional', 'アプリ・端末の再起動にかかる時間（1分前後）に合わせた', '同上', 'CL-2'),
('stop.line.resume_after_successes', 'system', '3', 'provisional', '保存と同じ考え（偶然の1回で戻さない）', '同上', 'CL-2'),
('stop.ai.stop_after_failures', 'system', '3', 'provisional', '1回の失敗は通話中のつなぎ直し（6-7）で吸収する。3本続けば会話AI側の障害とみなす', '本番の最初の1か月のエラーの記録を見て', 'CL-2'),
('stop.ai.probe_interval', 'system', '120', 'provisional', '試しのセッションも利用量を使うので、2分に1回に抑えた', '同上', 'CL-2'),
('stop.ai.resume_after_successes', 'system', '2', 'provisional', '試しのセッションは本物に近い確かめなので、2回で戻す', '同上', 'CL-2'),
('stop.ai_rate_limit.backoff', 'system', '[10, 30, 120, 600, 1800]', 'decided', '2026-10-13 ユーザー決定：10秒→30秒→2分→10分→30分。10/12 の 1008 は長い利用拒否ではなく、1分あたりの上限に当たっただけ（ユーザー）', '混雑の記録を見て', 'ユーザー'),
('stop.flapping.window', 'system', '86400', 'provisional', '1日の中で繰り返すものを、ばたつきとみなす', '本番の最初の1か月の停止の記録を見て', 'CL-2'),
('stop.flapping.wait_multiplier', 'system', '2', 'provisional', '繰り返すたびに待ちを2倍にする（よく使われる伸ばし方）', '同上', 'CL-2'),
('stop.flapping.max_wait', 'system', '14400', 'provisional', '4時間を上限にする（それ以上は、その日の業務が実質止まる）', '同上', 'CL-2'),
('quality.mock.calls', 'system', '10', 'decided', '2026-10-13 ユーザー決定：依頼主ごとに最初のみ、10本', '模擬通話の土台ができて、一度流したあと', 'ユーザー'),
('quality.mock.min_value_match', 'system', '0.95', 'provisional', '20本に1本の取り違えまで。取り違えた値は「取れた」にならず「-」に落ちる作り（4-4）なので、害の向きは安全側', '模擬通話の実測を見て（上げられるなら上げる）', 'CL-2'),
('quality.mock.min_judgment_match', 'system', '0.95', 'provisional', '値と同じ考え', '同上', 'CL-2'),
('quality.stage.sizes', 'system', '[20, 100]', 'provisional', '最初の20件で大きな誤りを見つけ、100件で割合を見てから全部へ（20件では割合のぶれが大きい）', '最初の業務の立ち上げのあと', 'CL-2'),
('quality.max_ear_disagreement', 'system', '0.10', 'provisional', '別の耳の1回の聞き違いが約5%（フェーズ0 G-16・G-22）。それを超える食い違いが倍あれば、業務設定か声の型を疑う', '最初の100件の実測を見て下げる', 'CL-2'),
('quality.max_unknown_judgments', 'system', '0', 'provisional', '判定できないは、判定の処理か入力の誤り（7-1）。1件でも出たら止める', '変えない見込み', 'CL-2'),
('quality.max_forbidden_speech', 'system', '0', 'provisional', '言ってはいけないことは取り消せない害（Intent.md 1-3）。2つの耳で確かめたものが1件でも出たら止める', '変えない見込み', 'CL-2'),
('quality.max_complaint', 'system', '0.02', 'provisional', '50本に1本を超える苦情は、話し方か業務設定に問題があるとみなす', '最初の100件の実測を見て', 'CL-2'),
('quality.max_technical_error', 'system', '0.03', 'provisional', 'システムの健康の目安。結果不明の試行はかけ直しも遅れるので、低く抑える', '同上', 'CL-2'),
('quality.max_verification_failed', 'system', '0.02', 'provisional', '検証できない行は処理済み（かけない側）になり、機会を失う。2%を超えたら仕組みを疑う', '同上', 'CL-2'),
('gate.claim_lifetime', 'system', '30', 'provisional', '予約から発信までは本来すぐ。長いと、予約のあとに変わったこと（停止・時間帯）を古い判定で通してしまう', '発信の部品ができて、実際の間を測ったあと', 'CL-2'),
('gate.unknown_result_min_interval', 'system', '86400', 'provisional', '結果不明は実は話していた可能性がある（4-2）。同じ日にかけ直すと二重発信と同じ害になるので、翌日以降にする', '本番の最初の1か月の結果不明の件数を見て', 'CL-2'),
('gate.cross_client_interval', 'system', '259200', 'provisional', '別々の依頼主から同じ会社へ、続けて営業の電話がかからないよう3日あける（2026-10-12 ユーザー確定：システムで制限する。値は未決だった）', '依頼主が増えて、この理由で止めた件数を見て', 'CL-2'),
('gate.denied_retry_wait', 'system', '300', 'provisional', '待てば変わる理由（回線・進行中・枠）は数分で変わることが多い。5分あける', '門の不許可の記録を見て', 'CL-2'),
('gate.ai_tpm_limit', 'system', '65000', 'provisional', 'ユーザーが管理画面で確かめた値（会話の gemini-3.8-live。無料枠の値と見られる。Intent.md 12-2）', '有料枠に移ったとき・公式の値を確かめたとき（G-10）', 'CL-2'),
('gate.ai_tpm_per_call', 'system', '7500', 'provisional', '実測：短い通話で1分あたり約7,500（DESIGN.md 5-2 の9）。長い通話ではもっと増える', '模擬通話と最初の実測で、通話の長さごとに測り直す', 'CL-2'),
('gate.ai_tpm_safety', 'system', '0.8', 'provisional', '見込みの外れで利用拒否（1008）にならないよう、上限の8割までにする（65,000 × 0.8 ÷ 7,500 ＝ 同時6本ほど）', '利用拒否が起きないことを確かめながら上げる', 'CL-2'),
('gate.ear_tpm_limit', 'system', '20000', 'provisional', 'ユーザーが管理画面で確かめた値（別の耳 gemini-3.5-transcribe-live）', '有料枠に移ったとき', 'CL-2'),
('calling.hours', 'system', '{"mon": [["09:00","18:00"]], "tue": [["09:00","18:00"]], "wed": [["09:00","18:00"]], "thu": [["09:00","18:00"]], "fri": [["09:00","18:00"]], "sat": [], "sun": [], "holiday": []}', 'provisional', '発信の相手は法人（Intent.md 3-1）。法人の営業時間に合わせ、平日の9時〜18時、土日祝はかけない。⚠️ Intent.md 18-2 の17（ユーザーが決めること）の仮の値', 'ユーザーが 18-2 の17 を決めたとき', 'CL-2'),
('calling.max_concurrent_calls', 'system', '6', 'provisional', '会話AIの枠から決まる上限（gate.ai_tpm_safety の計算）。⚠️ 実際の同時の本数は回線の数で決まる：2026-10-13 ユーザー「1PC 1本。Rakuten Link は1つずつしか流せない。増やすときはスマホを足す」⇒ いまは回線1つ＝同時1本', '有料枠に移ったとき・回線を増やしたとき', 'CL-2'),
('retry.max_attempts_per_task', 'system', '3', 'decided', '2026-10-13 ユーザー決定：3回（5回は迷惑電話と受け取られやすい）。再発信の方針（業務ごと）はこの内側', '応答率の実測を見て', 'ユーザー'),
('verification.max_retries', 'system', '3', 'provisional', '別の耳の一時的な混雑（503 など）は数回で通ることが多い（CL-1 の G-23）', '検証の失敗の記録を見て', 'CL-2'),
('verification.retry_wait', 'system', '600', 'provisional', '混雑が引くのを待つ。10分あけても、次の発信の判断（翌日以降が多い）には間に合う', '同上', 'CL-2'),
('line.lease', 'system', '60', 'provisional', 'PCが落ちたと判断するまでの時間。短すぎると、少しの遅れで生きている回線の通話を「結果不明」にしてしまう', '見守り役ができて、実際の延長の間隔を測ったあと', 'CL-2'),
('line.lease_renew', 'system', '20', 'provisional', '期限の3分の1ごとに延ばす（2回続けて延ばし損ねても、まだ期限内）', '同上', 'CL-2');
