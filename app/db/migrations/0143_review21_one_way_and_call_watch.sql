-- 0143 見直し21a・21b の直し：鳴らなかった電話のせいを1つの道に・通話ごとの口の待ち・復旧の証拠・「声が聞き取れない」の扱いの1か所・
--      通話を終えた根拠の文・意味の判定の組の意味（2026-10-28。書いた人：CL-2。DESIGN.md 5-3・5-4・7-2-1・9-1）
--
-- (1) 鳴らなかった電話のせい（2026-10-24 管理者の決定）：「回線か番号かどちらかわからない場合は、その行に固執するのでなく、1回目から、
--     次の行に進むようにしてください。次の行に進んで、次の行がつながればすぐに回線の問題か番号の問題か判断できますよね」「その回線に
--     かける行が残っていないときは、『テスト番号』で決めます」。
--     ⛔ 0129〜0141：決定に無い「違う番号が2つ続けて鳴らなかったら回線のせい」の決まりと、それを止めるための「テスト番号で決める」印
--       （line_run_holds・hold_unrung_run）の2つの道があった。元気な回線でも、かけられない番号が2つ並ぶと回線を止め（見直し21a の場面
--       a3・a7・a2：5日で停止29回）、次の行がすぐつながる場面でも管理者の電話（テスト番号）を余分に鳴らした（場面 a9）。
--     ⇒ 1つの道にする：分からない電話のあとは、いつも次の行へ進む。その回線でお客様の電話が鳴れば（鳴ったテストも）、それまでの
--       分からない電話はどれも番号のせい（unrung_blame の区切り。今のまま）。その回線に、まもなく（あける長さの並びの1つ目＝15分のうちに）
--       かける行が無いときだけテスト番号で決める（鳴った＝番号のせい／鳴らなかった＝回線のせい＝close_unrung_run の test_silent／
--       見分けられない＝すぐ1回知らせて待つ＝core/ring_test）。テスト番号で決められない（無い・鳴らしてよい時間の外）ときは、決めずに
--       待つ（⛔ 前の区切りで番号のせいと決めると、壊れた回線のお客様の行を失う。前の閉じる理由 no_rows は外す）。
--     - 「違う番号の数」の決まり・印の表・印を置く関数は外す（line_run_holds・hold_unrung_run）。
--     - まだ分からない続き（line_pending）は、違う番号の数にかかわらず返す（since＝続きの最初の電話の時刻・phones＝続きの番号）。
-- (2) 通話ごとの口の仮の設定を外す（見直し21a の 5・6・7・21b の 1）：call.end_write_wait（書き込みを書き切るのを決まった秒数待った）・
--     call.stuck_after（戻らない部品を捨てるまでの長さ）。待つのは書き手が進んでいるあいだだけにし、戻らない部品は、すでにある値
--     （おわびを流し終えるまでの protocol.APOLOGY_WAIT と call.frozen_after）で決める（supervisor/line.py の `_Call`）。
-- (3) 通話を終えた理由に「データベースへの書き込みが進まない」（storage_stuck）を足す：書き込み1つが call.frozen_after より長く終わらない
--     ときは、データベースが止まったとしてお詫びして終える（こちらの不具合＝is_ours。直す担当は保存の担当 CL-2）。
--     通話を終えた根拠の短い文（trouble_why。CL-1 の連絡帳 4-59：`call.end_for_trouble(理由, why)`。お客様の言葉・名前は入れない）を、
--     理由と同じ書き換えの中で1回だけ残す（attempt_trouble_fixed）。
-- (4) 復旧の証拠（見直し21b の 2）：⛔ 0141 の recovered_after は、どの版の入れ替えでも復旧にした（その種類を直したと記録していなくても）。
--     ⇒ 戻す条件と同じ1か所（trouble_fixed_at）を読む：同じ契約者で、こちらの不具合なしにお客様と話し終えた電話をかけ始めた・その種類を
--     直したと記録され、そのあと版が替わった（2026-10-25 夜 管理者の決定・2026-10-17 の CL-2 の説明「直った証拠（同じ契約者で、不具合なく
--     話し終えた電話か、直したプログラムへの入れ替え）」）。
-- (5) 「声が聞き取れない」の特別扱いを1か所に（見直し21a の 12）：⛔ 0141 は trouble_ours・line_good_rings・poor_audio_line・
--     line_faults_unrecorded と Python に文字 'poor_audio' を書き、続きの区切りを2か所（poor_audio_line と line_faults_unrecorded）で
--     決めていた。⇒ その電話を回線の異常に数えうるか（＝こちらの不具合に数えないか）は trouble_counts_line の1か所、区切りは
--     poor_audio_line の1か所。line_faults_unrecorded は、記録の理由の文を選べるよう、声が聞き取れない電話かを返す（by_audio）。
-- (6) 意味の判定に使う AI（judge.models）の意味（CL-1 の連絡帳 4-59・main の e3019c4。2026-10-29 ユーザー：「(1) では、Jev が『確かでない』
--     と返したら、その時点で Sonnet を出します。OK」）：jev_and_sonnet＝Jev を先に出し、確かな読みが届けばそれで決め、確かな読みが無い・
--     失敗・0.3秒で届かないときだけ2つの Sonnet を出す／sonnet＝2つの Sonnet だけ。値は変えない（説明の文だけ直す）。

-- ═════════ (1) 鳴らなかった電話のせい：1つの道 ═════════
DROP FUNCTION salescall.hold_unrung_run(uuid);
DROP TABLE salescall.line_run_holds;

-- 鳴らなかった電話のせい（1か所。0141 を作り直す）：あとの区切り（鳴った電話・鳴ったテスト・閉じた印）で決まる。違う番号の数では決めない
CREATE OR REPLACE FUNCTION salescall.unrung_blame(p_attempt uuid) RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  WITH me AS (
    SELECT a.line_id, a.claimed_at, salescall.is_ours(a.attempt_id) AS ours FROM salescall.attempts a
    WHERE a.attempt_id = p_attempt AND a.tenant_id = salescall.cur_tenant()
      AND salescall.is_dialed(a.state) AND a.ended_at IS NOT NULL
      AND NOT salescall.rang(a.state, a.cut_off, a.attempt_id)
  ), hi AS (   -- あとのいちばん近い鳴った電話・鳴ったテストと閉じた印（どちらも無ければ、続きはまだ閉じていない）
    SELECT (SELECT min(r.began) FROM me, salescall.line_rings(me.line_id) r WHERE r.began > me.claimed_at) AS rang_at,
           (SELECT c.closed_at FROM salescall.line_run_closures c, me
            WHERE c.line_id = me.line_id AND c.closed_at > me.claimed_at
            ORDER BY c.closed_at LIMIT 1) AS closed_at
  )
  SELECT CASE WHEN me.ours THEN 'ours'
              WHEN hi.rang_at IS NOT NULL AND (hi.closed_at IS NULL OR hi.rang_at < hi.closed_at) THEN 'number'
              WHEN hi.closed_at IS NOT NULL THEN (SELECT c.verdict FROM salescall.line_run_closures c
                                                  WHERE c.line_id = me.line_id AND c.closed_at = hi.closed_at LIMIT 1)
              WHEN (SELECT l.retired_at IS NOT NULL FROM salescall.lines l WHERE l.line_id = me.line_id) THEN 'line'
              ELSE 'pending' END
  FROM me, hi
$$;

-- まだ分からない続き（1か所。0141 を作り直す）：いまの続き（いちばん新しい区切りのあと）の、こちらのせいでない鳴らなかった電話。
-- since＝続きの最初の電話の時刻・phones＝続きの番号（その番号の行は、続きが決まるのを待つ行＝まもなくかける行に数えない）
DROP FUNCTION salescall.line_pending(uuid);
CREATE FUNCTION salescall.line_pending(p_line uuid) RETURNS TABLE (since timestamptz, phones text[])
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  WITH l AS (SELECT line_id, salescall.line_run_start(line_id) AS t FROM salescall.lines
             WHERE line_id = p_line AND tenant_id = salescall.cur_tenant()),
       run AS (SELECT b.phone_e164, b.claimed_at FROM salescall.attempts b, l
               WHERE b.line_id = l.line_id AND b.claimed_at > l.t AND NOT salescall.is_ours(b.attempt_id)
                 AND salescall.is_dialed(b.state) AND b.ended_at IS NOT NULL)
  SELECT min(run.claimed_at), array_agg(DISTINCT run.phone_e164 ORDER BY run.phone_e164)
  FROM run
  HAVING count(*) > 0
$$;
REVOKE ALL ON FUNCTION salescall.line_pending(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.line_pending(uuid) TO salescall_app;

-- 続きを閉じる（0141 を作り直す）：閉じるのは、回線を使わなくした（retired）・テスト番号が鳴らなかった（test_silent）の2つだけで、
-- どちらも回線のせい。⛔ 0131〜0141 の no_rows（テスト番号で決められないとき前の区切りで決める）は外した（決めずに待つ）
CREATE OR REPLACE FUNCTION salescall.close_unrung_run(p_line uuid, p_reason text) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
BEGIN
  IF p_reason IS NULL OR p_reason NOT IN ('retired', 'test_silent') THEN
    RAISE EXCEPTION '続きを閉じる理由が違います：%', p_reason USING ERRCODE = 'check_violation';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM salescall.lines WHERE line_id = p_line AND tenant_id = salescall.cur_tenant()) THEN
    RAISE EXCEPTION 'その回線はありません（または、取引の範囲の外）' USING ERRCODE = 'check_violation';
  END IF;
  -- テストが鳴らなかった：その回線のいちばん新しいテストが、鳴らなかったテストであること（記録の無い印を作らない）
  IF p_reason = 'test_silent' AND NOT coalesce((SELECT NOT t.rang FROM salescall.line_ring_tests t WHERE t.line_id = p_line
                                                ORDER BY t.tested_at DESC, t.test_id DESC LIMIT 1), false) THEN
    RAISE EXCEPTION 'テストが鳴らなかった記録がありません' USING ERRCODE = 'check_violation';
  END IF;
  -- 話している電話がある：続きはまだ終わっていない。使わなくした回線は、それでも閉じる（あとで終わる電話も、閉じた続きに入る＝回線のせい）
  IF p_reason <> 'retired' AND EXISTS (SELECT 1 FROM salescall.attempts WHERE line_id = p_line AND salescall.is_active(state)) THEN
    RETURN false;
  END IF;
  IF NOT salescall.line_run_pending(p_line) THEN
    RETURN false;
  END IF;
  INSERT INTO salescall.line_run_closures (tenant_id, line_id, reason, verdict)
  VALUES (salescall.cur_tenant(), p_line, p_reason, 'line');
  RETURN true;
END $$;

ALTER TABLE salescall.line_run_closures DROP CONSTRAINT line_run_closures_reason_check;
ALTER TABLE salescall.line_run_closures ADD CONSTRAINT line_run_closures_reason_check
  CHECK (reason IN ('retired', 'test_silent', 'migration'));
ALTER TABLE salescall.line_run_closures DROP CONSTRAINT line_run_closures_verdict_check;
ALTER TABLE salescall.line_run_closures ADD CONSTRAINT line_run_closures_verdict_check
  CHECK (verdict IN ('line', 'none') AND (verdict = 'none') = (reason = 'migration'));

-- テスト番号で確かめる回線の停止は、1回鳴れば解く（2026-10-25 夜 管理者：「鳴ったら、テストＯＫなだけ」。決めるのは core/watch.record_check
-- の1か所。⛔ 0136〜0142：この回数（初め3）をテスト番号にも使い、再開のたびに約2分で3回、管理者の電話を鳴らした＝見直し21a の 3）。
-- 値は変えない（説明の文だけ直す。アプリが応答するかの確かめ＝確かめの部品は、今までどおりこの回数）
ALTER TABLE salescall.setting_definitions DISABLE TRIGGER setting_definitions_frozen;
UPDATE salescall.setting_definitions
SET meaning = '回線の確かめ（アプリが応答するか＝確かめの部品）が何回続けて正常なら解くか（契約者ごと）。テスト番号で確かめる停止は、'
              'テスト番号が1回鳴れば解きます（2026-10-25 夜 ユーザー：「鳴ったら、テストＯＫなだけ」）'
WHERE key = 'stop.line.resume_after_successes';
ALTER TABLE salescall.setting_definitions ENABLE TRIGGER setting_definitions_frozen;

-- ═════════ (2) 通話ごとの口の仮の設定を外す ═════════
ALTER TABLE salescall.setting_values DISABLE TRIGGER setting_values_frozen;
ALTER TABLE salescall.setting_definitions DISABLE TRIGGER setting_definitions_frozen;
DELETE FROM salescall.setting_values WHERE key IN ('call.end_write_wait', 'call.stuck_after');
DELETE FROM salescall.setting_definitions WHERE key IN ('call.end_write_wait', 'call.stuck_after');
ALTER TABLE salescall.setting_definitions ENABLE TRIGGER setting_definitions_frozen;
ALTER TABLE salescall.setting_values ENABLE TRIGGER setting_values_frozen;

-- ═════════ (3) データベースへの書き込みが進まない・通話を終えた根拠の文 ═════════
ALTER TABLE salescall.attempts DROP CONSTRAINT attempts_trouble_check;
ALTER TABLE salescall.attempts ADD CONSTRAINT attempts_trouble_check
  CHECK (trouble IS NULL OR trouble IN ('frozen', 'ai_malfunction', 'poor_audio', 'cannot_proceed', 'runner_error', 'storage_stuck'));
ALTER TABLE salescall.attempts ADD COLUMN trouble_why text;
ALTER TABLE salescall.attempts ADD CONSTRAINT attempts_trouble_why
  CHECK (trouble_why IS NULL OR (trouble IS NOT NULL AND btrim(trouble_why) <> '' AND char_length(trouble_why) <= 200));

-- 終えた理由と根拠の文は、進行中の通話を結果不明にするときに1回だけ残す（0134 を作り直す。根拠の文も同じ決まり）
CREATE OR REPLACE FUNCTION salescall.attempt_trouble_fixed() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (NEW.trouble IS DISTINCT FROM OLD.trouble OR NEW.trouble_why IS DISTINCT FROM OLD.trouble_why)
     AND NOT (OLD.trouble IS NULL AND salescall.is_active(OLD.state) AND NEW.state = 'technical_error') THEN
    RAISE EXCEPTION '終えた理由は、進行中の通話を結果不明にするときに1回だけ残します' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'attempt_trouble_fixed';
  END IF;
  RETURN NEW;
END $$;

-- ═════════ (5) 「声が聞き取れない」の扱いの1か所 ═════════
-- その終えた理由の電話を、回線の異常に数えうるか（1か所）：声が聞き取れない（poor_audio）。お詫びして終えるのは同じだが、こちらの不具合には
-- 数えず（trouble_ours）、途中で切れた電話と同じに扱い、違うお客様で続けば回線の異常（poor_audio_line）。回線が元気だった証拠
-- （line_good_rings）にもしない（2026-10-25 夜 管理者の決定 案A）
CREATE FUNCTION salescall.trouble_counts_line(p_trouble text) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
  SELECT coalesce(p_trouble = 'poor_audio', false)
$$;
GRANT EXECUTE ON FUNCTION salescall.trouble_counts_line(text) TO salescall_app;

CREATE OR REPLACE FUNCTION salescall.trouble_ours(p_trouble text) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
  SELECT p_trouble IS NOT NULL AND NOT salescall.trouble_counts_line(p_trouble)
$$;

CREATE OR REPLACE FUNCTION salescall.line_good_rings(p_line uuid) RETURNS TABLE (began timestamptz, ended timestamptz)
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT a.claimed_at, a.ended_at FROM salescall.attempts a
  WHERE a.line_id = p_line AND salescall.is_dialed(a.state) AND a.ended_at IS NOT NULL
    AND salescall.rang(a.state, a.cut_off, a.attempt_id) AND NOT salescall.trouble_counts_line(a.trouble)
  UNION ALL
  SELECT t.tested_at, t.tested_at FROM salescall.line_ring_tests t WHERE t.line_id = p_line AND t.rang
$$;

-- 声が聞き取れずに終えた電話が、回線のせいか（1か所。区切りもここだけ）：同じ回線で、回線が元気だった証拠（line_good_rings）を前後に挟まずに、
-- 違うお客様（番号）の声が聞き取れない電話が2つ以上続いた
CREATE OR REPLACE FUNCTION salescall.poor_audio_line(p_attempt uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  WITH me AS (
    SELECT a.line_id, a.claimed_at FROM salescall.attempts a
    WHERE a.attempt_id = p_attempt AND a.tenant_id = salescall.cur_tenant() AND salescall.trouble_counts_line(a.trouble)
      AND a.ended_at IS NOT NULL
  ), lo AS (
    SELECT coalesce((SELECT max(r.began) FROM me, salescall.line_good_rings(me.line_id) r WHERE r.began < me.claimed_at),
                    '-infinity'::timestamptz) AS t
  ), hi AS (
    SELECT coalesce((SELECT min(r.began) FROM me, salescall.line_good_rings(me.line_id) r WHERE r.began > me.claimed_at),
                    'infinity'::timestamptz) AS t
  )
  SELECT coalesce((SELECT count(DISTINCT b.phone_e164) >= 2 FROM salescall.attempts b, me, lo, hi
                   WHERE b.line_id = me.line_id AND salescall.trouble_counts_line(b.trouble) AND b.ended_at IS NOT NULL
                     AND b.claimed_at > lo.t AND b.claimed_at < hi.t), false)
$$;

-- 回線の異常として、まだ記録していない「回線のせい」の試行（0141 を作り直す。1か所）：鳴らなかった電話で回線のせいと分かったものと、声が
-- 聞き取れない電話が違うお客様で続いたもの（poor_audio_line。区切りは poor_audio_line の1か所）。by_audio＝声が聞き取れない電話
-- （記録の理由の文を選ぶ）
DROP FUNCTION salescall.line_faults_unrecorded(uuid);
CREATE FUNCTION salescall.line_faults_unrecorded(p_line uuid)
RETURNS TABLE (attempt_id uuid, client_id uuid, by_audio boolean)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT a.attempt_id, a.client_id, salescall.trouble_counts_line(a.trouble) FROM salescall.attempts a
  WHERE a.line_id = p_line AND a.tenant_id = salescall.cur_tenant()
    AND NOT EXISTS (SELECT 1 FROM salescall.error_events e WHERE e.attempt_id = a.attempt_id AND e.source = 'line')
    AND ((a.claimed_at > coalesce((SELECT max(r.began) FROM salescall.line_rings(p_line) r), '-infinity'::timestamptz)
          AND salescall.unrung_blame(a.attempt_id) = 'line')
         OR salescall.poor_audio_line(a.attempt_id))
  ORDER BY a.claimed_at
$$;
REVOKE ALL ON FUNCTION salescall.line_faults_unrecorded(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.line_faults_unrecorded(uuid) TO salescall_app;

-- ═════════ (4) 復旧の証拠 ═════════
-- こちらの不具合のあと、復旧したか（1か所。0141 を作り直す）：その電話が終わったあとに、(a) 同じ契約者で、こちらの不具合なしにお客様と
-- 話し終えた電話をかけ始めた（last_good_talk）、または (b) その種類の不具合を直したと記録され、そのあと版が替わった（trouble_fixed_at＝
-- 休んだ行を戻す・止めた停止を解くのと同じ1か所）。⛔ 0141：版が替われば、直したと記録していない種類でも復旧にした
CREATE OR REPLACE FUNCTION salescall.recovered_after(p_attempt uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT coalesce((
    SELECT salescall.trouble_fixed_at(salescall.trouble_kind(a.trouble)) > coalesce(a.ended_at, a.claimed_at)
        OR salescall.last_good_talk() > coalesce(a.ended_at, a.claimed_at)
    FROM salescall.attempts a WHERE a.attempt_id = p_attempt AND a.tenant_id = salescall.cur_tenant()), false)
$$;

-- ═════════ (6) 意味の判定に使う AI（judge.models）の意味 ═════════
ALTER TABLE salescall.setting_definitions DISABLE TRIGGER setting_definitions_frozen;
UPDATE salescall.setting_definitions
SET meaning = '答えの意味の2つ目の判定に使うAI。jev_and_sonnet＝Jev 1.14 を先に出し、確かな読み（確からしさ 0.95 から上）が届けばそれで'
              '決める。確かな読みが無い・失敗した・0.3秒たっても届かないときだけ、Claude Sonnet 5.5 の考える形・考えない形の2つを同時に'
              '出す（初めの値。2026-10-29 ユーザー：「(1) では、Jev が『確かでない』と返したら、その時点で Sonnet を出します。OK」）／'
              'sonnet＝2つの Sonnet 5.5（考える・考えない）だけを同時に出す。どちらの組で呼ぶかを決めて呼ぶのは会話（CL-1）'
WHERE key = 'judge.models';
ALTER TABLE salescall.setting_definitions ENABLE TRIGGER setting_definitions_frozen;
