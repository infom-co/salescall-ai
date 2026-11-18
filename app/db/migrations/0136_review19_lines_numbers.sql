-- 0136 見直し19 の直し：回線・番号・テスト番号・通話の見張り（2026-10-24 夜。書いた人：CL-2）
--
-- (1) こちらのせいの電話は、番号にも回線にも数えない（見直し19a の3・19b の6）：お客様が出る前に、こちらのプログラムが固まった・甚大な
--     問題で終えた・通話を進める部品が落ちた電話（試行の trouble がある）は、だれのせいかを決める1か所（unrung_blame）が「ours」
--     （こちらのせい）と答え、番号の続き（failed_run）・回線の続き（同じ続きの違う番号の数）・回線の異常の数（line_faults_unrecorded）の
--     どれにも入れない。⛔ 0134 まで：固まった電話も「鳴らなかった電話」として番号の続きに入り、ほかの番号が鳴れば番号のせい＝4回で
--     お客様の行を取りやめにした（こちらの不具合なのに）。通話を進める部品が落ちた・終えずに戻った電話も trouble を残す（runner_error。
--     書くのは supervisor/line.py の通話ごとの口の1か所＝見守り役の誤りの修正依頼は前から出ている）。
--     こちらのせいとして数えないのは、同じ行で続けて resume_in_a_row()（2回）まで（salescall.is_ours の1か所。途中で切れた通話の
--     すぐのかけ直しと同じ決まり＝0118）。それを超えて続いたら、ふつうの鳴らなかった電話として証拠で決める（同じ行でこちらの不具合が
--     続くとき、数えないままお客様へ鳴らし続けない。⛔ 見直しの一生の試験 1o：数えないと、5日で 270 回かけ続けた）。
-- (2) 使わなくした回線でかけている途中だった電話（見直し19a の2 の(1)）：回線を使わなくしたあとに区切りが無い鳴らなかった電話は、回線の
--     せい（終わる時刻によらない。前は、使わなくしたときに終わっていなかった電話は、閉じた印の前に終わっていないので続きに入らず、
--     あとで終わっても分からないまま、知らせも無く待ち続けた）。
-- (3) まだ分からない続きの番号と、続きの始まり（line_pending。1か所）：「かけても決まらない行」（まだ分からない続きと同じ番号の行）を
--     見分けるのと、分からないまま長く待っている続きの見張り（契約者と管理者に1回知らせる）が読む。line_run_pending はこれを読む。
-- (4) 0129 より前の鳴らなかった電話を、だれのせいとも決めない（見直し19b の8 の(a)）：前の作りは回線の異常を試行の番号なしで記録して
--     いたので、0129 のあと最初に鳴らなかった電話のとき、それより前の電話を回線のせいとして、いまの時刻でまとめて記録し直し、入れ替えた
--     直後に回線を止めて知らせうる。⇒ この表の作りを流すときに、どの回線にも区切り（閉じた印。理由 migration・決め方 none＝数えない）を
--     1つ置く。
-- (5) 番号を伺い直す理由の無い前の予定（0130 より前）の読み替えを1か所に（見直し19b の8 の(c)）：salescall.number_again_of。門（予約証）と
--     次の動き（かけた電話の印）の両方が読む（前は次の動きだけが wrong_party に読み替え、門は空のまま予約証へ写した）。
-- (6) 設定（テスト番号の鳴らし方・決める時・待つ長さの見張り。見直し19a の1・2・5、19c の2）：
--     - line.test_ring_gap：テスト番号を最後に鳴らしてから、次に鳴らしてよいまでの長さ（契約者ごと。初め30分。回線をまたいで1つ＝テスト
--       番号の電話は1台）。続けて「鳴った」以外のときは倍ずつ伸ばし、line.test_ring_gap_max（初め4時間）まで。⛔ 0134・0135：止めた回線の
--       確かめの間隔（stop.line.probe_interval＝60秒＝アプリの確かめ用）でテスト番号を鳴らし、壊れた回線のあいだ、ユーザーの電話を
--       1分ごと（1日 約900回）鳴らした。
--     - line.test_ring_unknown_limit：テスト番号が「分からない」（話し中・電源オフ・圏外・呼び出しの音が見分けられない＝テスト番号の電話の
--       側の都合かもしれない）が続けてこの回数になったら、テスト番号で決めるのをやめ、前の区切りで決める・アプリの確かめの部品で再開を
--       確かめる（初め3回。契約者と管理者に1回知らせる）。いちばん長い間（line.test_ring_gap_max）たったら、またテスト番号で試す。
--     - line.decide_horizon：その回線に、この長さのうちにかけられる行が無ければ、まだ分からない続きを決めにいく（初め30分）。
--     - line.pending_alert_after：まだ分からない続きが、テスト番号を鳴らしてよい時間の中で、この長さを超えて決まらなければ、契約者と
--       管理者に1回知らせる（初め1日）。

-- ═════════ (1) こちらのせい ═════════
ALTER TABLE salescall.attempts DROP CONSTRAINT attempts_trouble_check;
ALTER TABLE salescall.attempts ADD CONSTRAINT attempts_trouble_check
  CHECK (trouble IS NULL OR trouble IN ('frozen', 'ai_malfunction', 'poor_audio', 'cannot_proceed', 'runner_error'));

-- ═════════ (4) 閉じた印の理由 migration・決め方 none ═════════
ALTER TABLE salescall.line_run_closures DROP CONSTRAINT line_run_closures_reason_check;
ALTER TABLE salescall.line_run_closures ADD CONSTRAINT line_run_closures_reason_check
  CHECK (reason IN ('no_rows', 'retired', 'test_silent', 'migration'));
ALTER TABLE salescall.line_run_closures DROP CONSTRAINT line_run_closures_verdict_check;
ALTER TABLE salescall.line_run_closures ADD CONSTRAINT line_run_closures_verdict_check
  CHECK (verdict IN ('number', 'line', 'none') AND (verdict = 'none') = (reason = 'migration'));
INSERT INTO salescall.line_run_closures (tenant_id, line_id, reason, verdict)
SELECT l.tenant_id, l.line_id, 'migration', 'none' FROM salescall.lines l
WHERE EXISTS (SELECT 1 FROM salescall.attempts a WHERE a.line_id = l.line_id);

-- こちらのせいとして、どの数にも入れない電話か（1か所）：試行に trouble があり、その行で、trouble のある発信だけが続いた数（この発信
-- まで）が resume_in_a_row() 以下
CREATE FUNCTION salescall.is_ours(p_attempt uuid) RETURNS boolean
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT coalesce((
    SELECT a.trouble IS NOT NULL AND (
      SELECT count(*) FROM salescall.attempts b
      WHERE b.task_id = a.task_id AND salescall.is_dialed(b.state) AND b.claimed_at <= a.claimed_at AND b.trouble IS NOT NULL
        AND NOT EXISTS (SELECT 1 FROM salescall.attempts c
                        WHERE c.task_id = a.task_id AND salescall.is_dialed(c.state) AND c.trouble IS NULL
                          AND c.claimed_at > b.claimed_at AND c.claimed_at <= a.claimed_at)) <= salescall.resume_in_a_row()
    FROM salescall.attempts a WHERE a.attempt_id = p_attempt), false)
$$;
REVOKE ALL ON FUNCTION salescall.is_ours(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.is_ours(uuid) TO salescall_app;

-- ═════════ (1)(2)(3) だれのせいか・まだ分からない続き ═════════
-- まだ分からない続き（1か所）：その回線の、いちばん新しい区切り（鳴った電話・鳴ったテスト・閉じた印）のあとの、こちらのせいでない
-- 鳴らなかった電話が1つの番号だけなら、その番号と続きの始まり。無ければ行が無い。読むのは持ち主の権限で動く関数（返すのは自分の契約者の
-- 回線の番号だけ）
CREATE FUNCTION salescall.line_pending(p_line uuid) RETURNS TABLE (phone_e164 text, since timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  WITH l AS (SELECT line_id FROM salescall.lines WHERE line_id = p_line AND tenant_id = salescall.cur_tenant()),
       lo AS (SELECT greatest(
                coalesce((SELECT max(r.began) FROM l, salescall.line_rings(l.line_id) r), '-infinity'::timestamptz),
                coalesce((SELECT max(c.closed_at) FROM salescall.line_run_closures c, l WHERE c.line_id = l.line_id),
                         '-infinity'::timestamptz)) AS t),
       run AS (SELECT b.phone_e164, b.claimed_at FROM salescall.attempts b, l, lo
               WHERE b.line_id = l.line_id AND b.claimed_at > lo.t AND NOT salescall.is_ours(b.attempt_id)
                 AND salescall.is_dialed(b.state) AND b.ended_at IS NOT NULL)
  SELECT min(run.phone_e164), min(run.claimed_at) FROM run HAVING count(DISTINCT run.phone_e164) = 1
$$;
REVOKE ALL ON FUNCTION salescall.line_pending(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.line_pending(uuid) TO salescall_app;

-- line_run_pending は、持ち主の権限で動く line_pending（契約者の守りつき）を読むだけ（自分は呼んだ役の権限で動く）
CREATE OR REPLACE FUNCTION salescall.line_run_pending(p_line uuid) RETURNS boolean
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT EXISTS (SELECT 1 FROM salescall.line_pending(p_line))
$$;

-- 鳴らなかった電話のせい（0134 を作り直す）：ours＝こちらのせい（salescall.is_ours）。使わなくした回線で、あとに区切りの無い電話は line。
-- 同じ続きの違う番号の数は、こちらのせいの電話を数えない
CREATE OR REPLACE FUNCTION salescall.unrung_blame(p_attempt uuid) RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  WITH me AS (
    SELECT a.line_id, a.claimed_at, salescall.is_ours(a.attempt_id) AS ours FROM salescall.attempts a
    WHERE a.attempt_id = p_attempt AND a.tenant_id = salescall.cur_tenant()
      AND salescall.is_dialed(a.state) AND a.ended_at IS NOT NULL
      AND NOT salescall.rang(a.state, a.cut_off, a.attempt_id)
  ), lo AS (   -- 前のいちばん近い区切り（鳴った電話・鳴ったテストか閉じた印。無ければ回線の初め）
    SELECT greatest(
      coalesce((SELECT max(r.began) FROM me, salescall.line_rings(me.line_id) r WHERE r.began < me.claimed_at),
               '-infinity'::timestamptz),
      coalesce((SELECT max(c.closed_at) FROM salescall.line_run_closures c, me
                WHERE c.line_id = me.line_id AND c.closed_at < me.claimed_at), '-infinity'::timestamptz)) AS t
  ), hi AS (   -- あとのいちばん近い鳴った電話・鳴ったテスト・閉じた印（どれも無ければ、続きはまだ閉じていない）
    SELECT (SELECT min(r.began) FROM me, salescall.line_rings(me.line_id) r WHERE r.began > me.claimed_at) AS rang_at,
           (SELECT c.closed_at FROM salescall.line_run_closures c, me
            WHERE c.line_id = me.line_id AND c.closed_at > me.claimed_at
            ORDER BY c.closed_at LIMIT 1) AS closed_at
  )
  SELECT CASE WHEN me.ours THEN 'ours'
              WHEN (SELECT count(DISTINCT b.phone_e164) FROM salescall.attempts b, lo
                    WHERE b.line_id = me.line_id AND b.claimed_at > lo.t
                      AND b.claimed_at < coalesce(least(hi.rang_at, hi.closed_at), 'infinity'::timestamptz)
                      AND NOT salescall.is_ours(b.attempt_id) AND salescall.is_dialed(b.state) AND b.ended_at IS NOT NULL) > 1 THEN 'line'
              WHEN hi.rang_at IS NOT NULL AND (hi.closed_at IS NULL OR hi.rang_at < hi.closed_at) THEN 'number'
              WHEN hi.closed_at IS NOT NULL THEN (SELECT c.verdict FROM salescall.line_run_closures c
                                                  WHERE c.line_id = me.line_id AND c.closed_at = hi.closed_at LIMIT 1)
              WHEN (SELECT l.retired_at IS NOT NULL FROM salescall.lines l WHERE l.line_id = me.line_id) THEN 'line'
              ELSE 'pending' END
  FROM me, hi
$$;

-- 続きを閉じる（0134 を作り直す）：前の区切りで決めるとき（no_rows）、移行の区切り（決め方 none）は証拠にしない
CREATE OR REPLACE FUNCTION salescall.close_unrung_run(p_line uuid, p_reason text) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
DECLARE last_ring timestamptz; last_close record; v text;
BEGIN
  IF p_reason IS NULL OR p_reason NOT IN ('no_rows', 'retired', 'test_silent') THEN
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
  -- 話している電話がある：続きはまだ終わっていない（かける行が無いのではない）。使わなくした回線は、それでも閉じる（あとで終わる
  -- 電話も、閉じた続きに入る＝回線のせい）
  IF p_reason <> 'retired' AND EXISTS (SELECT 1 FROM salescall.attempts WHERE line_id = p_line AND salescall.is_active(state)) THEN
    RETURN false;
  END IF;
  IF NOT salescall.line_run_pending(p_line) THEN
    RETURN false;
  END IF;
  IF p_reason IN ('retired', 'test_silent') THEN
    v := 'line';
  ELSE
    SELECT max(r.began) INTO last_ring FROM salescall.line_rings(p_line) r;
    SELECT closed_at, verdict INTO last_close FROM salescall.line_run_closures
    WHERE line_id = p_line AND verdict <> 'none' ORDER BY closed_at DESC LIMIT 1;   -- 0136：移行の区切りは証拠ではない
    v := CASE WHEN last_ring IS NOT NULL AND (last_close.closed_at IS NULL OR last_ring > last_close.closed_at) THEN 'number'
              WHEN last_close.closed_at IS NOT NULL THEN last_close.verdict
              ELSE 'line' END;
  END IF;
  INSERT INTO salescall.line_run_closures (tenant_id, line_id, reason, verdict)
  VALUES (salescall.cur_tenant(), p_line, p_reason, v);
  RETURN true;
END $$;

-- ═════════ (5) 番号を伺い直す理由の読み替え（1か所） ═════════
CREATE FUNCTION salescall.number_again_of(p_purpose text, p_number_again text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
  SELECT coalesce(p_number_again, CASE WHEN p_purpose = 'ask_number_again' THEN 'wrong_party' END)
$$;
GRANT EXECUTE ON FUNCTION salescall.number_again_of(text, text) TO salescall_app;

-- ═════════ (6) 設定 ═════════
INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, finest_scope) VALUES
('line.test_ring_gap', 'override', 'seconds',
 'テスト番号を最後に鳴らしてから、次に鳴らしてよいまでの長さ（どの回線で鳴らしたかによらない＝テスト番号の電話は1台）。続けて「鳴った」'
 '以外のときは倍ずつ伸ばし、いちばん長い間（line.test_ring_gap_max）まで。いまのテスト番号は契約者自身の電話なので、止めた回線の確かめの'
 'たびに鳴らし続けないため（⛔ 見直し19a：1分ごとに鳴らしていた）', 'DESIGN.md 5-4・9-1', 'tenant'),
('line.test_ring_gap_max', 'override', 'seconds',
 'テスト番号を鳴らす間の、いちばん長い間（続けて「鳴った」以外のときに伸ばす上限）。「分からない」が続いてテスト番号で決めるのをやめた'
 'あとも、この長さたてば、またテスト番号で試す', 'DESIGN.md 5-4・9-1', 'tenant'),
('line.test_ring_unknown_limit', 'override', 'integer',
 'テスト番号が「分からない」（話し中・電源オフ・圏外・呼び出しの音が見分けられない）が続けてこの回数になったら、テスト番号で決めるのを'
 'やめ、前の区切りで決める・アプリの確かめで再開を確かめる（契約者と管理者に1回知らせる）。テスト番号の電話の側の都合を、回線のせいに'
 'しないため', 'DESIGN.md 5-4・9-1', 'tenant'),
('line.decide_horizon', 'override', 'seconds',
 '番号のせいか回線のせいかがまだ分からない電話のあと、その回線に、この長さのうちにかけられる行が無ければ、テスト番号（無ければ前の'
 '区切り）で決めにいく（2026-10-24 ユーザー：「一つの行で止まり続けることがないようにしてください」）', 'DESIGN.md 7-2-1', 'tenant'),
('line.pending_alert_after', 'override', 'seconds',
 '番号のせいか回線のせいかがまだ分からない電話が、テスト番号を鳴らしてよい時間の中で、この長さを超えて決まらなければ、契約者と管理者に'
 '1回知らせる（だれにも知られずに行が待ち続けないため）', 'DESIGN.md 7-2-1', 'tenant');
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('line.test_ring_gap', 'system', '1800', 'provisional',
 'テスト番号はいまユーザー自身の電話。30分に1回までなら、壊れた回線が続いても1日（7〜22時）に最大30回ほど（続けて鳴らなければ伸びる）',
 'ユーザーに伺う（見直し19）・この仕事専用の電話に替えたとき', 'CL-2'),
('line.test_ring_gap_max', 'system', '14400', 'provisional',
 '壊れた回線が長く続くときは4時間に1回まで（1日 4回ほど）', 'ユーザーに伺う（見直し19）', 'CL-2'),
('line.test_ring_unknown_limit', 'system', '3', 'provisional',
 '偶然の1回（話し中など）で決め方を替えない', 'ユーザーに伺う（見直し19）', 'CL-2'),
('line.decide_horizon', 'system', '1800', 'provisional',
 'テスト番号の間（30分）と同じ。それより先にかける行は、待つより試したほうが早い', 'ユーザーに伺う（見直し19）', 'CL-2'),
('line.pending_alert_after', 'system', '86400', 'provisional',
 '1日（鳴らしてよい時間で数える）決まらなければ、仕組みの不具合として知らせる', 'ユーザーに伺う（見直し19）', 'CL-2');

-- テストの間・続いた「分からない」は、契約者ごと（回線をまたいで）新しい順に読む（core/ring_test.wait_seconds・unknown_run）
CREATE INDEX line_ring_tests_by_tenant ON salescall.line_ring_tests (tenant_id, tested_at DESC, test_id DESC);
