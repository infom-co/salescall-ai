-- 0141 見直し20a の直し：違う番号が続けて鳴らなくても、テスト番号で決められるならテスト番号で決める・通話ごとの口の長さ・戻らない
--      通話の部品（2026-10-25。書いた人：CL-2。DESIGN.md 5-4・7-2-1・9-1）
--
-- (1) 「テスト番号で決める」印（line_run_holds。見直し20a の (1)）。2026-10-24 管理者の決定：「明らかに番号に問題がある場合は、その行は
--     取りやめですが、回線の問題が明らかな場合は行は取りやめでなく一時中止だけ」「その回線にかける行が残っていないときは、『テスト番号』で
--     決めます」。⛔ 0136 まで：同じ続きで違う番号が2つ以上鳴らなかったら、すぐ回線のせい（unrung_blame）にした。テスト番号で決められる
--     契約者でも、かけられない番号XとYが並ぶと、テスト番号を挟まずに回線を止め、再開の確かめでテスト番号が鳴って再開し、またX→Yで
--     止まる、を終わりなくくり返した（見直しの場面 r5：5日で停止14回・知らせ28件・テスト番号47回。XとYは取りやめにならない）。
--     ⇒ 続きに「テスト番号で決める」印を置けるようにする。印のある続きは、違う番号が2つ以上鳴らなくても回線のせいと決めず、まだ分からない
--       （pending）のままにし、テスト番号の結果で決める（鳴った＝鳴った電話と同じ区切り＝番号のせい／鳴らなかった＝test_silent で閉じて
--       回線のせい）。印の無い続きは、今までどおり違う番号2つ以上で回線のせい（テスト番号で決められない契約者＝テスト番号が無い・
--       鳴らしてよい時間の外。この場合の扱いは管理者に伺っているので変えない）。
--     - 「テスト番号で決められるか」を決めるのは Python の1か所（core/ring_test.decides。設定の合成は Python の settings の1か所なので、
--       データベースに2つ目の決め方を作らない）。データベースは、その判断の結果を印として残し、だれのせいかを決める1か所（unrung_blame）が
--       読むだけ。印を置くのは salescall.hold_unrung_run だけ（呼ぶのは回線ごとの処理の1か所＝supervisor/line.py の `_unrung`）。
--     - 印を置くのは、まだ分からない続き（1つの番号だけが鳴らなかった）か、違う番号が2つ以上になったばかりで、まだ回線の異常として1本も
--       記録していない続き（回線のせいとして扱い始めた続きを、あとから変えない）。
--     - 印のある続きで、テスト番号で決められなくなった（鳴らしてよい時間が過ぎた）ら、回線ごとの処理が続きを
--       閉じて前の決まりで決める（close_unrung_run の no_rows：違う番号が2つ以上なら回線のせい＝印の無い続きと同じ決まり）。
--     - まだ分からない続き（line_pending）は、印のある「違う番号が2つ以上」の続きも返す（numbers＝違う番号の数。phone_e164 は番号が1つの
--       ときだけ）。回線ごとの処理は、テスト番号を鳴らせる時になったら、まもなくかける行の有無にかかわらず、すぐテスト番号で決める。
--       待つあいだも回線は止めずに次の行へかけ続ける（鳴れば、その電話で番号のせいと決まる。2026-10-24 管理者：「わからない場合は…
--       1回目から、次の行に進む…次の行がつながればすぐに判断できますよね」「作業を止めるな」）。
--     - 前の版のデータには印が無いので、だれのせいかは今までと同じ（移し替えは要らない）。
-- (2) 設定（見直し20a の (5)(6)）：
--     - call.end_write_wait：通話を終えると決める前に、通話の部品が頼んだ書き込み（話し終えた ending→ended・報告）を書き切るのを待つ
--       いちばん長い秒（⛔ 前は待たず、データベースへの書き込みが3秒より遅いと、話し終えた電話を「固まった」としておわびを流し、取れた
--       結果を消して、お客様にもう一度かけた）。
--     - call.stuck_after：こちらの不具合で通話を終えたあと（おわびして切り終えたあと）、通話を進める部品がこの秒数戻らなければ、その
--       回線の流れを捨てて立て直し、契約者と管理者に知らせ、CL-1 へ修正依頼を出す（⛔ 前は戻るまで待ち続け、回線が黙って止まった）。
-- (3) エラーの出どころ「通話の部品」（runner。core/errors.py の SOURCES と同じ）：(2) の戻らない通話の部品（種類 runner_stuck）。
-- (4) こちらの不具合で終えた電話は「復旧次第」かけ直す（salescall.recovered_after・awaiting_recovery。下の (4) の説明）。
-- (5) 「声が聞き取れない」はこちらの不具合に数えない（trouble_ours の1か所）・違うお客様で続けば回線の異常（下の (5)）。
-- (6) 休んだ行・止めた停止を戻すのは、その理由を直したと記録され、そのあと版が替わったときだけ（trouble_fixed_at の1か所。下の (6)）。
-- (7) 違う3行で同じ不具合：こちらの不具合なしに話し終えた電話があれば数え直す（last_good_talk。下の (7)）。
-- (8) テスト番号をあける長さ：鳴ったらあけない。鳴らなかった・見分けられなかった1回目のあと15分から（設定の説明の文。下の (8)）。
-- (9) 外す設定（下の (9)）：line.decide_horizon（次の電話で決まるのを待つ長さは、あける長さの並びの1つ目）・line.test_ring_unknown_limit
--     （テスト番号の答えの分け方を直したので、「分からない」が続いてもテスト番号で決めるのをやめない）・line.pending_alert_after
--     （テスト番号の答えが見分けられなかったら、その続きについてすぐ1回知らせる。1日待たない）。

-- ═════════ (1) テスト番号で決める印 ═════════
CREATE TABLE salescall.line_run_holds (
  hold_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id uuid NOT NULL,
  line_id   uuid NOT NULL,
  held_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
  held_by   text NOT NULL DEFAULT salescall.cur_actor() CHECK (btrim(held_by) <> ''),
  FOREIGN KEY (tenant_id, line_id) REFERENCES salescall.lines (tenant_id, line_id)
);
CREATE INDEX line_run_holds_by_line ON salescall.line_run_holds (line_id, held_at);
ALTER TABLE salescall.line_run_holds ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON salescall.line_run_holds
  USING (tenant_id = salescall.cur_tenant()) WITH CHECK (tenant_id = salescall.cur_tenant());
GRANT SELECT ON salescall.line_run_holds TO salescall_app;   -- 書くのは hold_unrung_run だけ（持ち主の権限）
CREATE TRIGGER line_run_holds_frozen BEFORE UPDATE OR DELETE ON salescall.line_run_holds
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();

-- その回線のいまの続きの始まり（1か所。いちばん新しい区切り＝鳴った電話・鳴ったテスト・閉じた印。無ければいちばん前）。読むのは持ち主の
-- 権限で動く関数だけ
CREATE FUNCTION salescall.line_run_start(p_line uuid) RETURNS timestamptz
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT greatest(
    coalesce((SELECT max(r.began) FROM salescall.line_rings(p_line) r), '-infinity'::timestamptz),
    coalesce((SELECT max(c.closed_at) FROM salescall.line_run_closures c WHERE c.line_id = p_line), '-infinity'::timestamptz))
$$;
REVOKE ALL ON FUNCTION salescall.line_run_start(uuid) FROM PUBLIC;

-- まだ分からない続き（0136 を作り直す。1か所）：いまの続きの、こちらのせいでない鳴らなかった電話が、1つの番号だけか、違う番号が2つ以上で
-- 「テスト番号で決める」印があるとき。phone_e164＝番号が1つのときだけ、その番号・since＝続きの最初の電話の時刻・numbers＝違う番号の数
DROP FUNCTION salescall.line_pending(uuid);
CREATE FUNCTION salescall.line_pending(p_line uuid) RETURNS TABLE (phone_e164 text, since timestamptz, numbers integer)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  WITH l AS (SELECT line_id, salescall.line_run_start(line_id) AS t FROM salescall.lines
             WHERE line_id = p_line AND tenant_id = salescall.cur_tenant()),
       run AS (SELECT b.phone_e164, b.claimed_at FROM salescall.attempts b, l
               WHERE b.line_id = l.line_id AND b.claimed_at > l.t AND NOT salescall.is_ours(b.attempt_id)
                 AND salescall.is_dialed(b.state) AND b.ended_at IS NOT NULL)
  SELECT CASE WHEN count(DISTINCT run.phone_e164) = 1 THEN min(run.phone_e164) END, min(run.claimed_at),
         count(DISTINCT run.phone_e164)::integer
  FROM run
  HAVING count(DISTINCT run.phone_e164) = 1
      OR (count(DISTINCT run.phone_e164) > 1
          AND EXISTS (SELECT 1 FROM salescall.line_run_holds h, l WHERE h.line_id = l.line_id AND h.held_at > l.t))
$$;
REVOKE ALL ON FUNCTION salescall.line_pending(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.line_pending(uuid) TO salescall_app;

-- 「テスト番号で決める」印を置く（書くのはここだけ。呼ぶのは回線ごとの処理が、その契約者がいまテスト番号で決められるときだけ）。
-- いまの続きに印があれば何もしない（true）。置くのは、まだ分からない続き（1つの番号だけ）か、違う番号が2つ以上になったばかりで、
-- その続きの電話をまだ1本も回線の異常として記録していない続き（回線のせいとして扱い始めた続きを、あとから変えない）。置いた・あったら true
CREATE FUNCTION salescall.hold_unrung_run(p_line uuid) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
DECLARE t timestamptz; n integer;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM salescall.lines WHERE line_id = p_line AND tenant_id = salescall.cur_tenant()) THEN
    RAISE EXCEPTION 'その回線はありません（または、取引の範囲の外）' USING ERRCODE = 'check_violation';
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended('line_run_holds:' || p_line::text, 0));
  t := salescall.line_run_start(p_line);
  IF EXISTS (SELECT 1 FROM salescall.line_run_holds WHERE line_id = p_line AND held_at > t) THEN
    RETURN true;
  END IF;
  SELECT count(DISTINCT b.phone_e164) INTO n FROM salescall.attempts b
  WHERE b.line_id = p_line AND b.claimed_at > t AND NOT salescall.is_ours(b.attempt_id)
    AND salescall.is_dialed(b.state) AND b.ended_at IS NOT NULL;
  IF n = 0 OR EXISTS (SELECT 1 FROM salescall.attempts b JOIN salescall.error_events e ON e.attempt_id = b.attempt_id
                      WHERE b.line_id = p_line AND b.claimed_at > t AND e.source = 'line') THEN
    RETURN false;
  END IF;
  INSERT INTO salescall.line_run_holds (tenant_id, line_id) VALUES (salescall.cur_tenant(), p_line);
  RETURN true;
END $$;
REVOKE ALL ON FUNCTION salescall.hold_unrung_run(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.hold_unrung_run(uuid) TO salescall_app;

-- 鳴らなかった電話のせい（0136 を作り直す）：違う番号が2つ以上でも、その続きに「テスト番号で決める」印があれば回線のせいと決めない
-- （区切り＝鳴った電話・鳴ったテスト・閉じた印で決まる）。印の無い続きは今までどおり
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
  ), up AS (   -- 続きの終わり（無ければ、まだ閉じていない）
    SELECT coalesce(least(hi.rang_at, hi.closed_at), 'infinity'::timestamptz) AS t FROM hi
  )
  SELECT CASE WHEN me.ours THEN 'ours'
              WHEN (SELECT count(DISTINCT b.phone_e164) FROM salescall.attempts b, lo, up
                    WHERE b.line_id = me.line_id AND b.claimed_at > lo.t AND b.claimed_at < up.t
                      AND NOT salescall.is_ours(b.attempt_id) AND salescall.is_dialed(b.state) AND b.ended_at IS NOT NULL) > 1
                   AND NOT EXISTS (SELECT 1 FROM salescall.line_run_holds h, lo, up
                                   WHERE h.line_id = me.line_id AND h.held_at > lo.t AND h.held_at < up.t) THEN 'line'
              WHEN hi.rang_at IS NOT NULL AND (hi.closed_at IS NULL OR hi.rang_at < hi.closed_at) THEN 'number'
              WHEN hi.closed_at IS NOT NULL THEN (SELECT c.verdict FROM salescall.line_run_closures c
                                                  WHERE c.line_id = me.line_id AND c.closed_at = hi.closed_at LIMIT 1)
              WHEN (SELECT l.retired_at IS NOT NULL FROM salescall.lines l WHERE l.line_id = me.line_id) THEN 'line'
              ELSE 'pending' END
  FROM me, hi
$$;

-- 続きを閉じる（0136 を作り直す）：前の決まりで決めるとき（no_rows）、違う番号が2つ以上の続き（「テスト番号で決める」印のある続きで、
-- テスト番号で決められなくなった）は回線のせい（印の無い続きと同じ決まり）。1つの番号の続きは今までどおり前の区切りで決める
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
  ELSIF (SELECT p.numbers FROM salescall.line_pending(p_line) p) > 1 THEN
    v := 'line';   -- 0141：違う番号が2つ以上（印があってもテスト番号で決められなかった）＝前の決まりで回線のせい
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

-- ═════════ (2) 設定 ═════════
INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, finest_scope) VALUES
('call.end_write_wait', 'override', 'seconds',
 '通話を途中で終えると決める前に（「動いています」の知らせが途切れた・通話を進める部品が甚大な問題を知らせた・落ちた）、通話を進める部品が'
 '頼んだデータベースへの書き込み（話し終えたこと・報告など）を書き切るのを待つ、いちばん長い秒です。書き切れば、話し終えた電話におわびを'
 '流さず、取れた結果も消しません。データベースが元気なら待ちません（頼んだ書き込みが残っているときだけ待つ）', 'DESIGN.md 5-4', 'system'),
('call.stuck_after', 'override', 'seconds',
 'こちらの不具合で通話をおわびして終えたあと、通話を進める部品（会話）がこの秒数たっても戻らなければ、その回線の処理を立て直し'
 '（次の電話をかけられるようにする）、契約者と管理者に知らせ、会話の担当に修正依頼を出します（回線が黙って止まったままにしない）',
 'DESIGN.md 5-4・9-1', 'system');
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('call.end_write_wait', 'system', '5', 'provisional',
 '見直し20a の場面（書き込み1つに2秒かかる混み）で、話し終えたことの書き込みが書き切れる長さ。固まった電話のおわびが遅れるのは、'
 '書き込みが残っているときだけ（データベースが元気なら0秒）',
 'ユーザーに伺う（見直し20a）・本番のデータベースの書き込みの長さを測って', 'CL-2'),
('call.stuck_after', 'system', '60', 'provisional',
 '通話を進める部品は、終えられたら（call.ended）すぐ戻る約束。片付け（報告を作る・声の流れを閉じる）に数秒から数十秒かかっても足りる'
 '長さで、回線を1分より長く黙って止めない',
 'ユーザーに伺う（見直し20a）・CL-1 の通話を進める部品の片付けの長さを測って', 'CL-2');

-- ═════════ (3) エラーの出どころ「通話の部品」 ═════════
ALTER TABLE salescall.error_events DROP CONSTRAINT error_events_source_check;
ALTER TABLE salescall.error_events ADD CONSTRAINT error_events_source_check
  CHECK (source IN ('ai', 'ear', 'line', 'storage', 'delivery', 'recording', 'supervisor', 'judge', 'call', 'runner'));

-- ═════════ (4) こちらの不具合で終えた電話は「復旧次第」かけ直す ═════════
-- 2026-10-17 管理者の決定：「仕組みが途中で止まった通話や、回線が途中で切れた通話は復旧次第、お詫びとともにすぐかけ直さないといけない
-- でしょ！」。⛔ 0139 まで：こちらの不具合（試行の trouble＝salescall.is_ours）でお客様が出たあとに終えた電話は、締めてすぐ（1秒ほどで）
-- かけ直し、直っていなければ、お客様が続けて2回お詫びの電話を受けた。お客様が出る前に終えた電話は、同じお客様への最短の間隔のあとに
-- かけ直した（どちらも「復旧」を見ていない＝2か所で別々の決め方）。
-- ⇒ 「復旧した」を1か所で決める（salescall.recovered_after）：その不具合のあと、(a) 同じ契約者で、こちらの不具合なしにお客様と話し終えた
--   電話（その不具合の電話が終わったあとにかけ始め、ふつうに終えた＝ended）がある、または (b) 動いているプログラムの版が替わった
--   （salescall.program_since）。その行のいちばん新しい発信がこちらの不具合で、まだ復旧していなければ、その行は待つ（salescall.
--   awaiting_recovery の1か所。門の理由 awaiting_recovery と、候補を出す処理が読む＝候補に出さない）。復旧したら、すぐ（お客様が出た
--   あとなら、お詫びとともに＝resume_allowed のまま）かけ直す。次の動き（next_action._line_fault_again の ours）も、かけた予定のまま
--   いまにする（最短の間隔で待たない＝待つのは復旧だけ）。回線が途中で切れた通話（こちらの不具合ではない）の扱いは変えない。
-- 定義は下の (7)（復旧したか＝recovered_after・復旧を待つか＝awaiting_recovery。話し終えた電話の読み方を (7) と共有する）

-- ═════════ (5) 「声が聞き取れない」はこちらの不具合に数えない（2026-10-25 夜 管理者の決定） ═════════
-- お詫びして終えるのは今のまま。こちらのプログラムの不具合には数えない（行の休み・全体の停止・修正依頼・番号の4回・回線のどれにも、
-- こちらの不具合としては数えない）。お客様と話している途中で切れた電話と同じ（途中で切れた数＝cuts_in_a_row・resume_in_a_row：続けて2回
-- まではお詫びしてすぐかけ直し、超えたら、ふつうのかけ直しの決まりで回数に数える）。同じ回線で、違うお客様の「声が聞き取れない」が続く
-- ときは、回線の異常（回線の異常の数え方の1か所＝line_faults_unrecorded。何回で止めるかは回線の既存の設定 stop.line.stop_after_failures）。
-- ⇒ こちらの不具合か（どの終えた理由が、こちらのプログラムの不具合か）を決めるのは salescall.trouble_ours の1か所。is_ours・行の休み・
--   全体の停止の数え方・お客様が出たあとの途中で切れた数・復旧を待つかは、どれもここを読む。Python の処置の表（core/errors.TROUBLES・
--   ACTIONS）は、こちらの不具合の理由だけに種類を持ち（声が聞き取れないは持たない）、通話ごとの口（supervisor/line.py）は、終えた電話が
--   こちらの不具合かを、この関数（is_ours）に聞いて伝え方を決める（判断を2か所に持たない）。
CREATE FUNCTION salescall.trouble_ours(p_trouble text) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
  SELECT p_trouble IS NOT NULL AND p_trouble <> 'poor_audio'
$$;
GRANT EXECUTE ON FUNCTION salescall.trouble_ours(text) TO salescall_app;

CREATE OR REPLACE FUNCTION salescall.is_ours(p_attempt uuid) RETURNS boolean
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT coalesce((SELECT salescall.trouble_ours(a.trouble) FROM salescall.attempts a WHERE a.attempt_id = p_attempt), false)
$$;

-- 続けて切れた数（0139 を作り直す）：こちらの不具合で途中で終えた電話は数えない。声が聞き取れずに終えた電話は、途中で切れた電話として数える
CREATE OR REPLACE FUNCTION salescall.cuts_in_a_row(p_attempt uuid) RETURNS integer
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT count(*)::integer FROM salescall.attempts a JOIN salescall.attempts b ON b.task_id = a.task_id
  WHERE a.attempt_id = p_attempt AND salescall.is_dialed(b.state) AND b.claimed_at <= a.claimed_at AND coalesce(b.cut_off, false)
    AND NOT salescall.trouble_ours(b.trouble)
    AND NOT EXISTS (SELECT 1 FROM salescall.attempts c
                    WHERE c.task_id = a.task_id AND salescall.is_placed(c.state) AND c.claimed_at > b.claimed_at
                      AND c.claimed_at <= a.claimed_at AND NOT coalesce(c.cut_off, false)
                      AND NOT salescall.is_line_failure(c.state, c.attempt_id))
$$;

-- 回線が元気だった証拠（0141）：その回線で鳴った電話（声が聞き取れずに終えた電話を除く）と、鳴ったテスト。回線が直った証拠（consecutive_errors の
-- line_ok）と、声が聞き取れない電話が続いたかの区切りが読む（番号のせいか回線のせいかの区切り＝line_rings は今のまま：声が聞き取れなくても、
-- 電話は鳴った）
CREATE FUNCTION salescall.line_good_rings(p_line uuid) RETURNS TABLE (began timestamptz, ended timestamptz)
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT a.claimed_at, a.ended_at FROM salescall.attempts a
  WHERE a.line_id = p_line AND salescall.is_dialed(a.state) AND a.ended_at IS NOT NULL
    AND salescall.rang(a.state, a.cut_off, a.attempt_id) AND a.trouble IS DISTINCT FROM 'poor_audio'
  UNION ALL
  SELECT t.tested_at, t.tested_at FROM salescall.line_ring_tests t WHERE t.line_id = p_line AND t.rang
$$;
REVOKE ALL ON FUNCTION salescall.line_good_rings(uuid) FROM PUBLIC;

-- 声が聞き取れずに終えた電話が、回線のせいか（1か所）：同じ回線で、回線が元気だった証拠（line_good_rings）を挟まずに、違うお客様（番号）の
-- 声が聞き取れない電話が2つ以上続いた
CREATE FUNCTION salescall.poor_audio_line(p_attempt uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  WITH me AS (
    SELECT a.line_id, a.claimed_at FROM salescall.attempts a
    WHERE a.attempt_id = p_attempt AND a.tenant_id = salescall.cur_tenant() AND a.trouble = 'poor_audio' AND a.ended_at IS NOT NULL
  ), lo AS (
    SELECT coalesce((SELECT max(r.began) FROM me, salescall.line_good_rings(me.line_id) r WHERE r.began < me.claimed_at),
                    '-infinity'::timestamptz) AS t
  ), hi AS (
    SELECT coalesce((SELECT min(r.began) FROM me, salescall.line_good_rings(me.line_id) r WHERE r.began > me.claimed_at),
                    'infinity'::timestamptz) AS t
  )
  SELECT coalesce((SELECT count(DISTINCT b.phone_e164) >= 2 FROM salescall.attempts b, me, lo, hi
                   WHERE b.line_id = me.line_id AND b.trouble = 'poor_audio' AND b.ended_at IS NOT NULL
                     AND b.claimed_at > lo.t AND b.claimed_at < hi.t), false)
$$;
REVOKE ALL ON FUNCTION salescall.poor_audio_line(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.poor_audio_line(uuid) TO salescall_app;

-- 回線の異常として、まだ記録していない「回線のせい」の試行（0134 を作り直す。1か所）：鳴らなかった電話で回線のせいと分かったもの（今のまま）と、
-- 声が聞き取れない電話が違うお客様で続いたもの（poor_audio_line）
CREATE OR REPLACE FUNCTION salescall.line_faults_unrecorded(p_line uuid)
RETURNS TABLE (attempt_id uuid, client_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT a.attempt_id, a.client_id FROM salescall.attempts a
  WHERE a.line_id = p_line AND a.tenant_id = salescall.cur_tenant()
    AND NOT EXISTS (SELECT 1 FROM salescall.error_events e WHERE e.attempt_id = a.attempt_id AND e.source = 'line')
    AND ((a.claimed_at > coalesce((SELECT max(r.began) FROM salescall.line_rings(p_line) r), '-infinity'::timestamptz)
          AND salescall.unrung_blame(a.attempt_id) = 'line')
         OR (a.trouble = 'poor_audio'
             AND a.claimed_at > coalesce((SELECT max(r.began) FROM salescall.line_good_rings(p_line) r), '-infinity'::timestamptz)
             AND salescall.poor_audio_line(a.attempt_id)))
  ORDER BY a.claimed_at
$$;


-- ═════════ (6) 直した証拠（2026-10-25 夜 管理者の決定） ═════════
-- 休んだ行・こちらの不具合で止めた停止を戻すのは、「その理由（trouble）の修正依頼が直したと記録され（fix_reports）、かつ、そのあとに動いて
-- いるプログラムの版が替わった」ときだけ（⛔ 0139：版が替われば、どの理由でも戻した）。手で戻す道（troubles.restore・停止を手で解く）は今のまま。
-- 決めるのは salescall.trouble_fixed_at の1か所（行の休み＝task_trouble_run・全体の停止の数え方＝consecutive_errors の program_fixed・停止を
-- 解く＝core/watch.resume_fixed が読む）。エラーの種類（修正依頼の種類と同じ）は、終えた理由から salescall.trouble_kind の1か所で作る
-- （core/errors.TROUBLES の値と同じ形。試験が確かめる）。
CREATE FUNCTION salescall.trouble_kind(p_trouble text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
  SELECT 'call_' || p_trouble
$$;
GRANT EXECUTE ON FUNCTION salescall.trouble_kind(text) TO salescall_app;

-- その種類の不具合を直したとみなす時刻（1か所）：その種類の修正依頼の「直した」の記録ごとに、そのあとの最初の版の替わり。そのいちばん新しい
-- もの（無ければ、いちばん前＝直していない）。修正依頼の表は契約者の取引から読めないので持ち主の権限で読み、返すのは時刻だけ（どの契約者の
-- 不具合かは返さない）。呼べるのはサーバーの処理だけ（cur_actor の守り）
CREATE FUNCTION salescall.trouble_fixed_at(p_kind text) RETURNS timestamptz
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT coalesce((SELECT max((SELECT min(v.seen_at) FROM salescall.program_versions v WHERE v.seen_at > r.reported_at))
                   FROM salescall.fix_reports r JOIN salescall.fix_requests q ON q.request_id = r.request_id
                   WHERE q.kind = p_kind AND salescall.cur_actor() IS NOT NULL), '-infinity'::timestamptz)
$$;
REVOKE ALL ON FUNCTION salescall.trouble_fixed_at(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.trouble_fixed_at(text) TO salescall_app;

-- その行の、こちらの不具合の電話の続き（0139 を作り直す）：こちらの不具合（trouble_ours）だけを数え、数えるのは、その理由を直した時
-- （trouble_fixed_at）と管理者・契約者が戻した時より後だけ。続きが途切れるのは、こちらの不具合でなく、お客様のところで鳴った電話
CREATE OR REPLACE FUNCTION salescall.task_trouble_run(p_task uuid) RETURNS integer
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  WITH restored AS (SELECT coalesce((SELECT max(r.restored_at) FROM salescall.task_trouble_restores r WHERE r.task_id = p_task),
                                    '-infinity'::timestamptz) AS t)
  SELECT count(*)::integer FROM salescall.attempts b, restored
  WHERE b.task_id = p_task AND salescall.trouble_ours(b.trouble)
    AND b.claimed_at > greatest(restored.t, salescall.trouble_fixed_at(salescall.trouble_kind(b.trouble)))
    AND NOT EXISTS (SELECT 1 FROM salescall.attempts c
                    WHERE c.task_id = p_task AND c.claimed_at > b.claimed_at AND NOT salescall.trouble_ours(c.trouble)
                      AND c.ended_at IS NOT NULL AND salescall.rang(c.state, c.cut_off, c.attempt_id))
$$;

-- ═════════ (7) 違う3行で同じ不具合：話し終えた電話で数え直す（2026-10-25 夜 管理者の決定） ═════════
-- その契約者で、こちらの不具合なしにお客様と話し終えた電話（ended）のいちばん新しいかけ始め（1か所。復旧したか＝recovered_after と、違う行の
-- 数え直し＝consecutive_errors の program_fixed が読む）
CREATE INDEX attempts_talked_by_tenant ON salescall.attempts (tenant_id, claimed_at) WHERE trouble IS NULL AND state = 'ended';
CREATE FUNCTION salescall.last_good_talk() RETURNS timestamptz
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT coalesce((SELECT max(b.claimed_at) FROM salescall.attempts b
                   WHERE b.tenant_id = salescall.cur_tenant() AND b.trouble IS NULL AND b.state = 'ended'), '-infinity'::timestamptz)
$$;
REVOKE ALL ON FUNCTION salescall.last_good_talk() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.last_good_talk() TO salescall_app;

-- こちらの不具合のあと、復旧したか（(4)。1か所）：その電話が終わったあとに、(a) 同じ契約者で、こちらの不具合なしにお客様と話し終えた電話を
-- かけ始めた（last_good_talk）、または (b) 動いているプログラムの版が替わった（program_since）
CREATE FUNCTION salescall.recovered_after(p_attempt uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT coalesce((
    SELECT salescall.program_since() > coalesce(a.ended_at, a.claimed_at)
        OR salescall.last_good_talk() > coalesce(a.ended_at, a.claimed_at)
    FROM salescall.attempts a WHERE a.attempt_id = p_attempt AND a.tenant_id = salescall.cur_tenant()), false)
$$;
REVOKE ALL ON FUNCTION salescall.recovered_after(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.recovered_after(uuid) TO salescall_app;

-- その行が、こちらの不具合のあと復旧を待っているか（(4)。1か所。門の理由 awaiting_recovery と候補を出す処理が読む）：いちばん新しい発信が
-- こちらの不具合（trouble_ours）で終わり、まだ復旧していない。声が聞き取れずに終えた電話は待たない（途中で切れた電話と同じ）
CREATE FUNCTION salescall.awaiting_recovery(p_task uuid) RETURNS boolean
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT coalesce((SELECT CASE WHEN NOT salescall.trouble_ours(a.trouble) THEN false
                               ELSE NOT salescall.recovered_after(a.attempt_id) END
                   FROM salescall.attempts a WHERE a.task_id = p_task AND salescall.is_dialed(a.state)
                   ORDER BY a.claimed_at DESC LIMIT 1), false)
$$;
GRANT EXECUTE ON FUNCTION salescall.awaiting_recovery(uuid) TO salescall_app;

-- 続いたエラーの数（0139 を作り直す）：line_ok は回線が元気だった証拠（line_good_rings。声が聞き取れない電話は、回線が直った証拠にしない）。
-- program_fixed は、その種類を直した時（trouble_fixed_at）と、こちらの不具合なしにお客様と話し終えた電話（last_good_talk）のあとだけ数える
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
        WHEN 'line_ok' THEN (SELECT coalesce(max(r.ended), '-infinity'::timestamptz) FROM salescall.line_good_rings(p_line) r)
        WHEN 'program_fixed' THEN greatest(salescall.last_good_talk(),
                                           (SELECT max(salescall.trouble_fixed_at(k)) FROM unnest(p_kinds) k))
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

-- ═════════ (8) テスト番号をあける長さの読み方（2026-10-25 夜 管理者の決定） ═════════
-- 鳴ったら、テストはOKで終わり、あける長さは無い（次に要るとき、すぐ鳴らしてよい）。鳴らなかった・見分けられなかったときだけ、続けて1回目のあと
-- 15分・2回目のあと30分・3回目60分・4回目120分・5回目以降240分（line.test_ring_gaps の並びを、この意味で読む＝core/ring_test.wait_seconds
-- の1か所）。⛔ 0139：「鳴ったあと15分・鳴らなかった1回目のあと30分」と読んでいた。値は変えない（説明の文だけ直す）
ALTER TABLE salescall.setting_definitions DISABLE TRIGGER setting_definitions_frozen;
UPDATE salescall.setting_definitions
SET meaning = 'テスト番号が鳴らなかった・見分けられなかったとき、次に鳴らしてよいまであける長さ（秒）の並びです（どの回線で鳴らしたかに'
              'よりません＝テスト番号の電話は1台）。続けて1回目のあとは並びの1つ目、2回目のあとは2つ目…と進み、最後の長さで止まります。鳴ったら'
              '（話し中・電源オフ・圏外のお知らせが返ったときも＝回線から発信できた）、テストはOKで終わり、あける長さはありません（次に要るとき、'
              'すぐ鳴らします）。初めは 15分→30分→60分→120分→240分（2026-10-25 ユーザー：「15⇒30⇒60⇒120⇒240分　がデフォルト契約者ごとに'
              '設定」）。並びの1つ目は、回線のせいか番号のせいかがまだ分からないとき、その回線で次にかける電話の結果を待つ長さにも使います'
              '（2026-10-25 夜 ユーザー：「その回線で次にかけるのは15分後と決めたんだから、15分になるんじゃないの？新たな設定はいらないでしょ？」）'
WHERE key = 'line.test_ring_gaps';
ALTER TABLE salescall.setting_definitions ENABLE TRIGGER setting_definitions_frozen;

-- ═════════ (9) 外す設定（2026-10-25 夜 管理者の決定） ═════════
-- line.decide_horizon（0136。まだ分からない続きを、その回線で次にかける電話で決まるのを待つ長さ。初め30分）：待つ長さは、テスト番号を
--   あける長さの並び（line.test_ring_gaps）の1つ目（初め15分）を使う（core/ring_test.decide_within の1か所）。管理者：「その回線で次に
--   かけるのは15分後と決めたんだから、15分になるんじゃないの？新たな設定はいらないでしょ？」
-- line.test_ring_unknown_limit（0136。テスト番号が「分からない」と続けて何回でテスト番号で決めるのをやめるか。初め3回）：話し中・電源オフ・
--   圏外は「回線から発信できた」（鳴ったと同じ）になり、見分けられないときは、あける長さのあとで、また鳴らす（決めるのをやめない）。
--   管理者：「テスト番号が話し中・電源オフ・圏外だと分かるのなら、回線の問題じゃないんじゃないの？」。
-- line.pending_alert_after（0136。まだ分からない続きが、鳴らしてよい時間で数えてこの長さ決まらなければ知らせる。初め1日）：テスト番号の答えが
--   見分けられなかったら、その続きについて契約者と管理者にすぐ1回知らせる（core/ring_test.run の1か所。知らせの種類 line_undecided・鍵は
--   回線と続きの始まり＝同じ続きで何度見分けられなくても1回）。2026-10-25 夜 管理者「OK」。テスト番号が無い・鳴らしてよい時間の外で
--   決まらないのは、前の区切りで決める・時間の始まりまで待つ（今のまま）。
-- 前の設定は残さない（契約者の値も含めて外す。0139 の line.test_ring_gap・line.test_ring_gap_max と同じ外し方）
ALTER TABLE salescall.setting_values DISABLE TRIGGER setting_values_frozen;
ALTER TABLE salescall.setting_definitions DISABLE TRIGGER setting_definitions_frozen;
DELETE FROM salescall.setting_values WHERE key IN ('line.decide_horizon', 'line.test_ring_unknown_limit', 'line.pending_alert_after');
DELETE FROM salescall.setting_definitions WHERE key IN ('line.decide_horizon', 'line.test_ring_unknown_limit', 'line.pending_alert_after');
ALTER TABLE salescall.setting_definitions ENABLE TRIGGER setting_definitions_frozen;
ALTER TABLE salescall.setting_values ENABLE TRIGGER setting_values_frozen;
