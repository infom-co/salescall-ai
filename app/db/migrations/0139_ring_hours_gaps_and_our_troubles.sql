-- 0139 テスト番号を鳴らしてよい時間を何か所でも・あける長さの並び・こちらのプログラムの不具合を行だけか全体かで見極める
--      （2026-10-25 管理者の決定。書いた人：CL-2。DESIGN.md 5-4・7-2-1・9-1）
--
-- (1) テスト番号を鳴らしてよい時間（line.test_ring_hours）を、1日の中の時刻の範囲の並び（値の形 daily_ranges＝0138）にする。
--     2026-10-25 ユーザー：「止める回線を2択だけじゃなく、こちらが決められるようにして。昼間に私は寝ていることがあるし、夜中でも
--     起きていることがある。0～24時の選択は残したうえで。」
--     - 範囲は、いくつでも・日をまたいでもよい・0:00〜24:00（いつでも）も入れられる。範囲どうしの重なりと空の並びは、値の形の確かめの
--       1か所（setting_value_ok）で断る。初めの値は 7:00〜22:00 の1つのまま。
--     - 前の形（範囲1つ ["07:00","22:00"]）の値は、ここで並び（[["07:00","22:00"]]）に移す（システム・契約者の値のすべての記録）。
--       読み口（core/ring_test.hours）は並びの形だけを読む＝前の形を受ける2つ目の読み方を作らない（一本化）。
-- (2) テスト番号を前に鳴らしてからあける長さを、契約者ごとの長さの並び（line.test_ring_gaps。初め 15→30→60→120→240分）にする。
--     2026-10-25 ユーザー：「15⇒30⇒60⇒120⇒240分　がデフォルト契約者ごとに設定」。鳴らない・分からないが続くたびに並びの次の長さ、
--     最後の長さで止まる。鳴れば初めの長さに戻る（決めるのは core/ring_test.wait_seconds の1か所）。前の「初め30分・倍ずつ・最大4時間」
--     （line.test_ring_gap・line.test_ring_gap_max＝0136）は外す（前の設定は残さない）。契約者が前の2つを変えていたら、同じ長さの並び
--     （初めの長さから倍ずつ、いちばん長い間まで）に移す。
-- (3) こちらのプログラムの不具合（試行の trouble＝固まった・AI の異常・声が聞き取れない・会話を進められない・通話を進める部品が落ちた）を、
--     その行だけの問題か、すべての行にかかわる問題かで見極める。2026-10-25 ユーザー：「同じ行で、こちらのプログラムの不具合が続くとき：
--     その行だけの問題なのか、すべての行にかかわる問題なのかを見極めないといけないでしょ？その行だけなら、契約者(管理者含む)と、開発AIの
--     担当、記録にそれを伝え、2回続いたら、次の行へ。3行同じ問題なら、甚大な問題と判断。」
--     - こちらのせいの電話は、どれも番号・回線・行の回数のどれにも数えない（salescall.is_ours＝trouble がある。⛔ 0136：同じ行で続けて
--       2回までだけ数えず、3回目からはふつうの鳴らなかった電話として数えた＝その形をここで置き換える）。お客様が出たあとに終えた電話も、
--       途中で切れた数（cuts_in_a_row）・行の回数に入れず、いつもお詫びとともにかけ直す（復旧次第。行が休んでいればその間は待つ）。
--     - **行の休み**（salescall.task_resting の1か所。門と候補を出す処理が読む）：同じ行で、こちらのせいの電話が続けて
--       trouble_rest_after()（2回）になったら、その行は休む（次の行へ進む）。「続けて」が途切れるのは、こちらのせいでなく、お客様の
--       ところで鳴った電話だけ。数えるのは、戻した時より後だけ。戻す道は2つ：(a) 直したプログラムの版に替わった（program_versions。
--       中央の見守り役が、版が合っているときに、動いているプログラムの版を書く＝supervisor/updates.note。まだ書いていない版のときだけ。
--       版が替わった時刻は salescall.program_since の1か所）(b) 管理者・契約者が戻した（task_trouble_restores。入口は core/troubles.restore の1か所）。
--     - **すべての行にかかわる甚大な問題**：違う行で、同じ理由のこちらのせいの電話が、設定 stop.trouble.stop_after_rows（初め3行）に
--       なったら、その契約者の新しい電話を止める（止め方は watch.after_error → decide_stop の1か所。停止の原因 program_trouble＝0138）。
--       数えるのは salescall.consecutive_errors の1か所で、直った証拠「program_fixed」（直したプログラムの版に替わった時）と、同じ原因の
--       停止を解いた時より後の、違う行の数（同じ行の2回目は数えない）。止めているあいだに通す電話は無い（stop_lets_through に行を足さない）。

-- ═════════ (1) テスト番号を鳴らしてよい時間：範囲の並び ═════════
-- 1日の中の時刻の範囲1つの形（daily_range と daily_ranges の1つずつが使う1か所）
CREATE FUNCTION salescall.setting_daily_range_ok(v jsonb) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE WHEN jsonb_typeof(v) <> 'array' THEN false
              WHEN jsonb_array_length(v) <> 2 THEN false
              WHEN jsonb_typeof(v -> 0) <> 'string' OR jsonb_typeof(v -> 1) <> 'string' THEN false
              WHEN NOT ((v ->> 0) ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$') THEN false
              WHEN NOT ((v ->> 1) ~ '^(([01][0-9]|2[0-3]):[0-5][0-9]|24:00)$') THEN false   -- 終わりだけ 24:00（1日の終わり）を書ける
              ELSE (v ->> 0) <> (v ->> 1) END
$$;
-- 範囲の並びを、1日の中の分の区間（[lo, hi)。日をまたぐ範囲は、夜の側と朝の側の2つ）にしたもの（i＝並びの何番目か）。形の確かめを
-- 通った並びだけを渡す
CREATE FUNCTION salescall.daily_range_spans(v jsonb) RETURNS TABLE (i bigint, lo integer, hi integer)
LANGUAGE sql IMMUTABLE AS $$
  WITH r AS (SELECT x.o AS i,
                    split_part(x.e ->> 0, ':', 1)::integer * 60 + split_part(x.e ->> 0, ':', 2)::integer AS a,
                    split_part(x.e ->> 1, ':', 1)::integer * 60 + split_part(x.e ->> 1, ':', 2)::integer AS b
             FROM jsonb_array_elements(v) WITH ORDINALITY x(e, o))
  SELECT r.i, r.a, r.b FROM r WHERE r.a < r.b
  UNION ALL SELECT r.i, r.a, 1440 FROM r WHERE r.a > r.b
  UNION ALL SELECT r.i, 0, r.b FROM r WHERE r.a > r.b AND r.b > 0
$$;
GRANT EXECUTE ON FUNCTION salescall.setting_daily_range_ok(jsonb), salescall.daily_range_spans(jsonb) TO salescall_app;

CREATE OR REPLACE FUNCTION salescall.setting_value_ok(t salescall.setting_value_type, v jsonb) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE t
    WHEN 'boolean' THEN jsonb_typeof(v) = 'boolean'
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
    -- 1日の中の時刻の範囲（日本時間）。例：["09:00","19:00"]・["00:00","24:00"]（1日じゅう）。終わりが始まりより前なら日をまたぐ（["18:00","03:00"]＝翌3時まで）
    WHEN 'daily_range' THEN salescall.setting_daily_range_ok(v)
    -- 1日の中の時刻の範囲の並び（0139）。例：[["00:00","09:00"],["14:00","24:00"]]・[["22:00","02:00"]]・[["00:00","24:00"]]。
    -- 1つ以上・それぞれ daily_range の形・範囲どうしが重ならない（日をまたぐ範囲は、夜の側と朝の側の両方で比べる）
    WHEN 'daily_ranges' THEN CASE WHEN jsonb_typeof(v) <> 'array' THEN false
                                  WHEN jsonb_array_length(v) < 1 THEN false
                                  WHEN EXISTS (SELECT 1 FROM jsonb_array_elements(v) r WHERE NOT salescall.setting_daily_range_ok(r)) THEN false
                                  ELSE NOT EXISTS (SELECT 1 FROM salescall.daily_range_spans(v) x
                                                   JOIN salescall.daily_range_spans(v) y ON x.i < y.i AND x.lo < y.hi AND y.lo < x.hi) END
    -- 1日を分ける区切りの時（例：[12, 15]＝午前・昼過ぎ・夕方）。1〜23 の整数を、小さい順に重ならずに
    WHEN 'hour_list' THEN CASE WHEN jsonb_typeof(v) <> 'array' THEN false
                               WHEN jsonb_array_length(v) < 1 THEN false
                               WHEN EXISTS (SELECT 1 FROM jsonb_array_elements(v) e
                                            WHERE NOT salescall.setting_whole_number(e, 1) OR (e #>> '{}')::numeric > 23) THEN false
                               ELSE NOT EXISTS (SELECT 1 FROM jsonb_array_elements(v) WITH ORDINALITY a(e, i)
                                                JOIN jsonb_array_elements(v) WITH ORDINALITY b(e, i) ON b.i = a.i + 1
                                                WHERE (b.e #>> '{}')::numeric <= (a.e #>> '{}')::numeric) END
    -- 間隔か、かけない（2026-10-16 ユーザー：「○○時間、○○日、かけない」）。例：{"hours": 6}・{"days": 3}・"never"
    WHEN 'gap_or_never' THEN CASE WHEN v = '"never"'::jsonb THEN true
                                  WHEN jsonb_typeof(v) <> 'object' THEN false
                                  WHEN (SELECT count(*) FROM jsonb_object_keys(v)) <> 1 THEN false
                                  WHEN v ? 'hours' THEN salescall.setting_whole_number(v -> 'hours', 1)
                                  WHEN v ? 'days' THEN salescall.setting_whole_number(v -> 'days', 1)
                                  ELSE false END
    -- 上限（0069）：null＝上限なし、または1以上の整数
    WHEN 'limit' THEN jsonb_typeof(v) = 'null' OR salescall.setting_whole_number(v, 1)
    -- 電話番号（0133）：null＝まだ無い、またはそろえた番号（E.164。core/phone.normalize の形）
    WHEN 'phone' THEN jsonb_typeof(v) = 'null' OR (jsonb_typeof(v) = 'string' AND (v #>> '{}') ~ '^\+[1-9][0-9]{7,14}$')
  END
$$;

-- 前の形の値（範囲1つ）を、並びに移す（どの記録も同じ形にする＝読み口は1つの形だけを読む）
ALTER TABLE salescall.setting_values DISABLE TRIGGER setting_values_frozen;
ALTER TABLE salescall.setting_definitions DISABLE TRIGGER setting_definitions_frozen;
UPDATE salescall.setting_values SET value = jsonb_build_array(value)
WHERE key = 'line.test_ring_hours' AND jsonb_typeof(value -> 0) = 'string';
UPDATE salescall.setting_definitions
SET value_type = 'daily_ranges',
    meaning = 'テスト番号を鳴らしてよい時間（日本時間。毎日同じ）。1日に何か所でも決められます。この時間の外では、テスト番号を鳴らしません'
              '（止めた回線の再開の確かめは、次の時間の始まりまで待ちます。アプリが応答するかだけの確かめでは再開しません）。1つの時間は'
              '「始まり〜終わり」で、終わりが始まりより前なら日をまたぎます（22:00〜02:00＝夜10時から翌2時まで）。0:00〜24:00 は、いつでも'
              'です。時間どうしは重ねられません。使い方の例：昼に寝ていて夜は起きているなら「0:00〜9:00」と「14:00〜24:00」の2つ。'
              '初めは 7:00〜22:00 の1つ（いまのテスト番号は契約者自身の電話なので、深夜・早朝の歯止めと同じ）。この仕事専用の電話に替えたら、'
              '0:00〜24:00 にして夜も鳴らしてよい（2026-10-24 ユーザー：「私の電話です。軌道に乗れば、新しい電話をもうひとつ契約予定なので'
              'その後に考えます。この仕事専用なら夜でもOK」。2026-10-25 ユーザー：「止める回線を2択だけじゃなく、こちらが決められるように'
              'して。昼間に私は寝ていることがあるし、夜中でも起きていることがある。0～24時の選択は残したうえで。」）'
WHERE key = 'line.test_ring_hours';

-- ═════════ (2) テスト番号をあける長さ：並び ═════════
INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, finest_scope) VALUES
('line.test_ring_gaps', 'override', 'integer_list',
 'テスト番号を前に鳴らしてから、次に鳴らしてよいまでの長さ（秒）の並びです（どの回線で鳴らしたかによりません＝テスト番号の電話は1台）。'
 '鳴らなかった・分からないが続くたびに並びの次の長さにし、最後の長さで止まります。鳴れば、初めの長さに戻ります。初めは 15分→30分→'
 '60分→120分→240分（2026-10-25 ユーザー：「15⇒30⇒60⇒120⇒240分　がデフォルト契約者ごとに設定」）。「分からない」が続いてテスト番号で'
 '決めるのをやめたあとも、最後の長さがたてば、またテスト番号で試します', 'DESIGN.md 5-4・9-1', 'tenant');
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('line.test_ring_gaps', 'system', '[900, 1800, 3600, 7200, 14400]', 'decided',
 '2026-10-25 ユーザー：「15⇒30⇒60⇒120⇒240分　がデフォルト契約者ごとに設定」',
 '契約者が変えたいとき・この仕事専用の電話に替えたとき', 'ユーザー');
-- 契約者が前の2つ（初めの間・いちばん長い間）を変えていたら、同じ長さの並び（初めの間から倍ずつ、いちばん長い間まで）に移す
INSERT INTO salescall.setting_values (key, scope, tenant_id, value, status, rationale, review_when, set_by)
SELECT 'line.test_ring_gaps', 'tenant', t.tenant_id,
       (SELECT jsonb_agg(least(t.base * power(2::numeric, k), t.top)::bigint ORDER BY k) FROM generate_series(0, 62) k
        WHERE k = 0 OR t.base * power(2::numeric, k - 1) < t.top),
       'decided', '0139：前の設定（初めの間・倍ずつ・いちばん長い間）を、同じ長さの並びに移した',
       'この契約者が並びを見直すとき', '表の作り 0139'
FROM (SELECT x.tenant_id,
             coalesce((SELECT (v.value #>> '{}')::numeric FROM salescall.setting_values v
                       WHERE v.key = 'line.test_ring_gap' AND v.scope = 'tenant' AND v.tenant_id = x.tenant_id
                       ORDER BY v.value_id DESC LIMIT 1), 1800) AS base,
             coalesce((SELECT (v.value #>> '{}')::numeric FROM salescall.setting_values v
                       WHERE v.key = 'line.test_ring_gap_max' AND v.scope = 'tenant' AND v.tenant_id = x.tenant_id
                       ORDER BY v.value_id DESC LIMIT 1), 14400) AS top
      FROM (SELECT DISTINCT tenant_id FROM salescall.setting_values
            WHERE key IN ('line.test_ring_gap', 'line.test_ring_gap_max') AND scope = 'tenant') x) t;
DELETE FROM salescall.setting_values WHERE key IN ('line.test_ring_gap', 'line.test_ring_gap_max');
DELETE FROM salescall.setting_definitions WHERE key IN ('line.test_ring_gap', 'line.test_ring_gap_max');
ALTER TABLE salescall.setting_definitions ENABLE TRIGGER setting_definitions_frozen;
ALTER TABLE salescall.setting_values ENABLE TRIGGER setting_values_frozen;

-- ═════════ (3) こちらのプログラムの不具合 ═════════
-- エラーの出どころ「通話」（こちらのプログラムの不具合で通話を終えた。core/errors.py の SOURCES と同じ）
ALTER TABLE salescall.error_events DROP CONSTRAINT error_events_source_check;
ALTER TABLE salescall.error_events ADD CONSTRAINT error_events_source_check
  CHECK (source IN ('ai', 'ear', 'line', 'storage', 'delivery', 'recording', 'supervisor', 'judge', 'call'));
CREATE INDEX error_events_by_kind ON salescall.error_events (tenant_id, kind, occurred_at);

-- こちらのせいの電話か（1か所）：試行に trouble がある。どれも、番号・回線・行の回数のどれにも数えない（0136 の「同じ行で続けて2回まで」を
-- 置き換える＝数えないまま鳴らし続けない歯止めは、行の休み＝task_resting）
CREATE OR REPLACE FUNCTION salescall.is_ours(p_attempt uuid) RETURNS boolean
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT coalesce((SELECT a.trouble IS NOT NULL FROM salescall.attempts a WHERE a.attempt_id = p_attempt), false)
$$;

-- 続けて切れた数（0120 を作り直す）：こちらのせいで途中で終えた電話は数えない（お客様の電波などで切れた続きの数だけ）。数え直すのは今のまま、
-- お客様に届いて途中で切れなかった発信だけ（こちらのせいで途中で終えた電話は「途中で切れた」なので、数え直しもしない）
CREATE OR REPLACE FUNCTION salescall.cuts_in_a_row(p_attempt uuid) RETURNS integer
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT count(*)::integer FROM salescall.attempts a JOIN salescall.attempts b ON b.task_id = a.task_id
  WHERE a.attempt_id = p_attempt AND salescall.is_dialed(b.state) AND b.claimed_at <= a.claimed_at AND coalesce(b.cut_off, false)
    AND b.trouble IS NULL
    AND NOT EXISTS (SELECT 1 FROM salescall.attempts c
                    WHERE c.task_id = a.task_id AND salescall.is_placed(c.state) AND c.claimed_at > b.claimed_at
                      AND c.claimed_at <= a.claimed_at AND NOT coalesce(c.cut_off, false)
                      AND NOT salescall.is_line_failure(c.state, c.attempt_id))
$$;

-- 数え方の種類（0120 を作り直す）：お客様が出たあとにこちらのせいで終えた電話は、いつも「途中で切れた」（行の回数に数えない）
CREATE OR REPLACE FUNCTION salescall.dial_kind(s salescall.attempt_state, p_cut_off boolean, p_attempt uuid) RETURNS text
LANGUAGE sql STABLE AS $$
  SELECT CASE WHEN NOT salescall.is_dialed(s) THEN NULL
              WHEN NOT salescall.is_placed(s) THEN 'not_placed'
              WHEN coalesce(p_cut_off, false)
                   AND (salescall.is_ours(p_attempt) OR salescall.cuts_in_a_row(p_attempt) <= salescall.resume_in_a_row()) THEN 'cut_off'
              WHEN salescall.is_line_failure(s, p_attempt) THEN 'line_failure'
              ELSE 'reached' END
$$;

-- お詫びしてすぐかけ直してよいか（0123 を作り直す）：こちらのせいで途中で終えた電話は、いつもかけ直す（復旧次第。行が休んでいれば、
-- 戻るまで門と候補を出す処理が待たせる）
CREATE OR REPLACE FUNCTION salescall.resume_allowed(p_attempt uuid) RETURNS boolean
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT coalesce((SELECT a.cut_off FROM salescall.attempts a WHERE a.attempt_id = p_attempt), false)
     AND (salescall.is_ours(p_attempt) OR salescall.cuts_in_a_row(p_attempt) <= salescall.resume_in_a_row())
$$;

-- 動いているプログラムの版（中央の見守り役が、データベースと版が合っているときに書く。書くのは salescall.note_program だけ）
CREATE TABLE salescall.program_versions (
  seen_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  version text NOT NULL CHECK (btrim(version) <> '' AND length(version) <= 200),   -- 指紋（supervisor/updates.program_version）
  seen_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE UNIQUE INDEX program_versions_once ON salescall.program_versions (version);
CREATE TRIGGER program_versions_frozen BEFORE UPDATE OR DELETE ON salescall.program_versions
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
ALTER TABLE salescall.program_versions ENABLE ROW LEVEL SECURITY;   -- 版の指紋だけ（契約者のデータを持たない）：だれでも読める
CREATE POLICY program_read ON salescall.program_versions USING (true) WITH CHECK (salescall.cur_tenant() IS NULL);
GRANT SELECT ON salescall.program_versions TO salescall_app;

-- 版を書く（まだ書いていない版のときだけ。同じ版で立ち上げ直しても、前の版に戻しても、入れ替えのあいだ古い版と新しい版の見守り役が
-- 少し重なっても、版が替わったことにしない＝直していないのに休んだ行を戻さない）。書いたら true
CREATE FUNCTION salescall.note_program(p_version text) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
BEGIN
  IF salescall.cur_tenant() IS NOT NULL THEN   -- 書くのは中央の見守り役の全体の取引だけ（契約者の取引からは書けない）
    RAISE EXCEPTION 'プログラムの版は、全体の取引でだけ書けます' USING ERRCODE = 'insufficient_privilege';
  END IF;
  PERFORM pg_advisory_xact_lock(hashtext('salescall.note_program'));
  IF EXISTS (SELECT 1 FROM salescall.program_versions WHERE version = p_version) THEN
    RETURN false;
  END IF;
  INSERT INTO salescall.program_versions (version) VALUES (p_version);
  RETURN true;
END $$;
REVOKE ALL ON FUNCTION salescall.note_program(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.note_program(text) TO salescall_app;

-- いまのプログラムの版に替わった時刻（1か所。まだ書いていなければ、いちばん前）：行の休みの数え始め・甚大な問題の数え始め
-- （consecutive_errors の program_fixed）・甚大な問題の停止を解く時（core/watch.resume_fixed）が読む
CREATE FUNCTION salescall.program_since() RETURNS timestamptz
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT coalesce((SELECT seen_at FROM salescall.program_versions ORDER BY seen_id DESC LIMIT 1), '-infinity'::timestamptz)
$$;
GRANT EXECUTE ON FUNCTION salescall.program_since() TO salescall_app;

-- 管理者・契約者が、休んだ行を戻した記録（入口は core/troubles.restore の1か所。追記だけ）
CREATE TABLE salescall.task_trouble_restores (
  restore_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id   uuid NOT NULL,
  client_id   uuid NOT NULL,
  task_id     uuid NOT NULL,
  reason      text NOT NULL CHECK (btrim(reason) <> ''),
  restored_by text NOT NULL CHECK (btrim(restored_by) <> ''),
  actor_kind  salescall.actor_kind NOT NULL CHECK (actor_kind IN ('operator', 'admin')),
  restored_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, task_id) REFERENCES salescall.tasks (client_id, task_id)
);
CREATE INDEX task_trouble_restores_by_task ON salescall.task_trouble_restores (task_id, restored_at);
CREATE TRIGGER task_trouble_restores_frozen BEFORE UPDATE OR DELETE ON salescall.task_trouble_restores
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
ALTER TABLE salescall.task_trouble_restores ENABLE ROW LEVEL SECURITY;
CREATE POLICY client_scope ON salescall.task_trouble_restores
  USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
  WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client());
GRANT SELECT, INSERT ON salescall.task_trouble_restores TO salescall_app;

-- 同じ行で、こちらのせいの電話が続けてこの数になったら、その行を休ませる（2026-10-25 ユーザー：「2回続いたら、次の行へ」）
CREATE FUNCTION salescall.trouble_rest_after() RETURNS integer LANGUAGE sql IMMUTABLE AS $$ SELECT 2 $$;
GRANT EXECUTE ON FUNCTION salescall.trouble_rest_after() TO salescall_app;

-- その行の、こちらのせいの電話の続き（1か所）：いちばん新しい電話から続いた、trouble のある発信の数。続きが途切れるのは、こちらのせいで
-- なく、お客様のところで鳴った電話だけ（かけられなかった・お客様が出る前に回線で終わった電話は、プログラムが動いた証拠にならないので
-- 途切れさせない）。数えるのは、戻した時（直したプログラムの版に替わった時・管理者か契約者が戻した時）より後だけ
CREATE FUNCTION salescall.task_trouble_run(p_task uuid) RETURNS integer
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  WITH since AS (SELECT greatest(salescall.program_since(),
                                 coalesce((SELECT max(r.restored_at) FROM salescall.task_trouble_restores r WHERE r.task_id = p_task),
                                          '-infinity'::timestamptz)) AS t)
  SELECT count(*)::integer FROM salescall.attempts b, since
  WHERE b.task_id = p_task AND b.trouble IS NOT NULL AND b.claimed_at > since.t
    AND NOT EXISTS (SELECT 1 FROM salescall.attempts c
                    WHERE c.task_id = p_task AND c.claimed_at > b.claimed_at AND c.trouble IS NULL AND c.ended_at IS NOT NULL
                      AND salescall.rang(c.state, c.cut_off, c.attempt_id))
$$;
-- その行が、こちらのプログラムの不具合で休んでいるか（1か所。門の理由 task_resting と、候補を出す処理が読む）
CREATE FUNCTION salescall.task_resting(p_task uuid) RETURNS boolean
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT salescall.task_trouble_run(p_task) >= salescall.trouble_rest_after()
$$;
GRANT EXECUTE ON FUNCTION salescall.task_trouble_run(uuid), salescall.task_resting(uuid) TO salescall_app;

-- 戻すのは管理者か契約者だけ・休んでいる行だけ（「誰が」は取引のものをそのまま使う）
CREATE FUNCTION salescall.task_trouble_restore_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF salescall.cur_actor_kind() IS DISTINCT FROM 'operator' AND salescall.cur_actor_kind() IS DISTINCT FROM 'admin' THEN
    RAISE EXCEPTION '休んだ行を戻せるのは、管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'task_trouble_restore_by_person';
  END IF;
  IF NOT salescall.task_resting(NEW.task_id) THEN
    RAISE EXCEPTION 'その行は、こちらのプログラムの不具合で休んでいません' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'task_trouble_restore_only_resting';
  END IF;
  NEW.restored_by := salescall.cur_actor();
  NEW.actor_kind := salescall.cur_actor_kind()::salescall.actor_kind;
  NEW.restored_at := clock_timestamp();
  RETURN NEW;
END $$;
CREATE TRIGGER task_trouble_restore_guard BEFORE INSERT ON salescall.task_trouble_restores
  FOR EACH ROW EXECUTE FUNCTION salescall.task_trouble_restore_guard();

-- 続いたエラーの数（0134 を作り直す）：直った証拠 program_fixed（直したプログラムの版に替わった時）を足す。こちらのプログラムの不具合
-- （program_fixed で数える種類）は、エラーの数でなく、違う行の数で数える（同じ行の2回目は数えない＝その行だけの問題は行の休みで扱う）
CREATE OR REPLACE FUNCTION salescall.consecutive_errors(p_kinds text[], p_cause text, p_evidence text, p_line uuid DEFAULT NULL)
RETURNS integer
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  WITH allowed AS (     -- 契約者の数は、その契約者の取引だけ・回線の数は、その回線の契約者の取引だけ（全体の取引では数えない）
    SELECT salescall.cur_tenant() IS NOT NULL
           AND (p_line IS NULL OR EXISTS (SELECT 1 FROM salescall.lines l
                                          WHERE l.line_id = p_line AND l.tenant_id = salescall.cur_tenant())) AS ok
  ), since AS (
    SELECT greatest(
      CASE p_evidence
        WHEN 'in_conversation' THEN (SELECT coalesce(max(h.at), '-infinity'::timestamptz)
                                     FROM salescall.attempt_state_history h JOIN salescall.attempts a ON a.attempt_id = h.attempt_id
                                     WHERE h.to_state = 'in_conversation' AND a.tenant_id = salescall.cur_tenant())
        WHEN 'verified' THEN (SELECT coalesce(max(v.recorded_at), '-infinity'::timestamptz) FROM salescall.verification_summaries v
                              WHERE v.tenant_id = salescall.cur_tenant())
        WHEN 'line_ok' THEN (SELECT coalesce(max(r.ended), '-infinity'::timestamptz) FROM salescall.line_rings(p_line) r)
        WHEN 'program_fixed' THEN salescall.program_since()
        WHEN 'none' THEN '-infinity'::timestamptz
      END,
      (SELECT coalesce(max(r.at), '-infinity'::timestamptz)
       FROM salescall.stop_events r JOIN salescall.stop_events s ON s.event_id = r.resolves_event_id
       WHERE s.cause::text = p_cause AND s.tenant_id = salescall.cur_tenant()
         AND CASE WHEN p_line IS NULL THEN s.scope = 'tenant' ELSE s.scope = 'line' AND s.line_id = p_line END)) AS t
  )
  SELECT CASE WHEN NOT (SELECT ok FROM allowed)
                   OR p_evidence NOT IN ('in_conversation', 'verified', 'line_ok', 'program_fixed', 'none')
                   OR (p_evidence = 'line_ok' AND p_line IS NULL) THEN NULL
         ELSE (SELECT count(DISTINCT CASE WHEN p_evidence = 'program_fixed' THEN a.task_id::text ELSE e.error_id::text END)::integer
               FROM salescall.error_events e LEFT JOIN salescall.attempts a ON a.attempt_id = e.attempt_id
               CROSS JOIN since
               WHERE e.kind = ANY(p_kinds) AND e.occurred_at > since.t AND e.tenant_id = salescall.cur_tenant()
                 AND (p_line IS NULL OR e.line_id = p_line)) END
$$;

-- すべての行にかかわる甚大な問題とみなす、違う行の数（契約者ごと。2026-10-17 ユーザー：止める回数は契約者ごと）
INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, finest_scope) VALUES
('stop.trouble.stop_after_rows', 'override', 'integer',
 'こちらのプログラムの不具合（固まった・AI の異常・声が聞き取れない・決まりどおりの会話を進められない・通話を進める部品が落ちた）で'
 '終えた電話が、同じ理由で、違うこの数の行で起きたら（同じ行の2回目は数えません）、すべての行にかかわる甚大な問題として、その契約者の'
 '新しい電話を止めます（話している電話は切りません。契約者と管理者に知らせ、直す担当に修正依頼を出します。直したプログラムの版に'
 '替わったら解きます。管理者・契約者も解けます）。数えるのは、直したプログラムの版に替わった時と、この停止を解いた時より後だけです。'
 'その行だけの問題（同じ行で2回続いた）は、その行を休ませて次の行へ進みます（2026-10-25 ユーザー：「3行同じ問題なら、甚大な問題と'
 '判断。」）', 'DESIGN.md 9-1', 'tenant');
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('stop.trouble.stop_after_rows', 'system', '3', 'decided', '2026-10-25 ユーザー：「3行同じ問題なら、甚大な問題と判断。」',
 '甚大な問題の停止が、その行だけの問題で起きていないかを見て', 'ユーザー');
