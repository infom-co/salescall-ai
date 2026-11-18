-- 0129 お客様のところで鳴らなかった電話が、番号のせいか回線のせいかを、証拠で1か所に決める（2026-10-24。書いた人：CL-2）
--
-- 2026-10-24 ユーザー決定：「明らかに番号に問題がある場合は、その行は取りやめですが、回線の問題が明らかな場合は行は取りやめでなく
-- 一時中止だけですよ。」
-- ⛔ 見直し18a の問題1〜3：
--   問題1：どの電話もお客様が出る前に終わるほど壊れた回線でも、回線の確かめ（アプリが応答するか）は通るので、回線の停止は自動で解け、
--          番号の続き（0124 の failed_run）は回線の健康を見ずに鳴らなかった電話を数えたので、4日で回線のお客様の行がすべて
--          「番号が使えない」で終わった。0124 の説明の「回線の故障だけで番号が使えないと決まることはない」は、停止が解けない場合に
--          しか成り立たない誤りだった。
--   問題2：どちらのせいか分からない「かけられない」（電話の部品の CallNotPlaced）が1つの番号だけで続くと、番号には数えず回線の異常と
--          して数えたので、その行が候補の先頭に残り続け、回線が止まり続けて、ほかの行へかけられなかった。
--   問題3：どの番号も「番号を受け付けない」と答える回線（アプリの設定の誤りなど）では、回線の異常に数えなかったので、4日で回線の
--          お客様の行がすべて終わり、だれにも知らされなかった。
-- ⇒ 電話の部品（アプリ）の言い分では決めない。同じ回線の上の、前後の電話の証拠で決める（`unrung_blame` の1か所）。
--
-- (1) せい（unrung_blame）：発信まで進んで終わったのに鳴らなかった試行について、その試行の回線の「続き」（その回線の、発信まで
--     進んで終わった試行を、かけた順に並べ、鳴った試行で区切った1かたまり）を見る。
--       line＝続きの中に違う番号が2つ以上ある（鳴った電話を挟まずに、違う番号が続けて鳴らなかった＝回線のせいが明らか）
--       number＝番号が1つだけで、続きのあとに同じ回線で鳴った電話がある（回線は元気だった＝番号のせいが明らか）
--       pending＝どちらでもない（続きがまだ閉じていない。その回線がほかの番号へかければ、どちらかに決まる）
--     line と number は、あとから変わらない（続きの中身は、閉じれば変わらない。違う番号が入れば number には戻らない）。
--     回線の上のほかの依頼者の試行も見るので、データベースの持ち主の権限で読み、返すのは名前だけ（ほかの依頼者の番号を出さない）。
-- (2) 番号の続き（failed_run）：その番号のいちばん新しい鳴った発信より後の、鳴らなかった発信のうち、回線のせい（line）は数えない
--     （続きを切りもしない＝回線が壊れていたあいだの電話は、番号を試したことにならない）。n＝number と pending の数（次にかける時刻の
--     決まった順に使う）、n_sure＝number の数（その番号へかけないと決めるのは、こちらだけ＝core/attempts.number_unusable）。
-- (3) 回線の異常として、まだ記録していない「回線のせい」の試行（line_faults_unrecorded）：回線ごとの処理が、鳴らなかった電話の
--     あとに読んで1つずつ記録する（記録済みかは error_events.attempt_id で見る＝何度呼んでも二重にならない）。続きが初めて line に
--     なったときは、前の pending の分もまとめて記録される＝「鳴った電話を挟まずに違う番号が続けて鳴らなかった数」が回線の異常の
--     数になる（止める回数の設定 stop.line.stop_after_failures の意味は、そのまま）。
-- (4) 設定 dial.failed_retry_days の説明（i マーク）を直す。

-- ═════════ (1) ═════════
CREATE FUNCTION salescall.unrung_blame(p_attempt uuid) RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  WITH me AS (
    SELECT a.line_id, a.claimed_at FROM salescall.attempts a
    WHERE a.attempt_id = p_attempt AND a.tenant_id = salescall.cur_tenant()
      AND salescall.is_dialed(a.state) AND a.ended_at IS NOT NULL
      AND NOT salescall.rang(a.state, a.cut_off, a.attempt_id)
  ), lo AS (   -- 前のいちばん近い、鳴った電話（無ければ回線の初め）
    SELECT coalesce((SELECT b.claimed_at FROM salescall.attempts b, me
                     WHERE b.line_id = me.line_id AND b.claimed_at < me.claimed_at
                       AND salescall.is_dialed(b.state) AND b.ended_at IS NOT NULL
                       AND salescall.rang(b.state, b.cut_off, b.attempt_id)
                     ORDER BY b.claimed_at DESC LIMIT 1), '-infinity'::timestamptz) AS t
  ), hi AS (   -- あとのいちばん近い、鳴った電話（無ければ NULL＝続きはまだ閉じていない）
    SELECT (SELECT b.claimed_at FROM salescall.attempts b, me
            WHERE b.line_id = me.line_id AND b.claimed_at > me.claimed_at
              AND salescall.is_dialed(b.state) AND b.ended_at IS NOT NULL
              AND salescall.rang(b.state, b.cut_off, b.attempt_id)
            ORDER BY b.claimed_at LIMIT 1) AS t
  )
  SELECT CASE WHEN (SELECT count(DISTINCT b.phone_e164) FROM salescall.attempts b, me, lo, hi
                    WHERE b.line_id = me.line_id AND b.claimed_at > lo.t AND b.claimed_at < coalesce(hi.t, 'infinity'::timestamptz)
                      AND salescall.is_dialed(b.state) AND b.ended_at IS NOT NULL) > 1 THEN 'line'
              WHEN (SELECT hi.t FROM hi) IS NOT NULL THEN 'number'
              ELSE 'pending' END
  FROM me
$$;
REVOKE ALL ON FUNCTION salescall.unrung_blame(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.unrung_blame(uuid) TO salescall_app;

-- ═════════ (2) ═════════
DROP FUNCTION salescall.failed_run(uuid);   -- 返す形が変わる（n_sure を足す）
CREATE FUNCTION salescall.failed_run(p_contact uuid)
RETURNS TABLE (n integer, n_sure integer, first_day date, last_end timestamptz)
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  WITH x AS (SELECT a.attempt_id, a.claimed_at, a.ended_at, salescall.rang(a.state, a.cut_off, a.attempt_id) AS rang
             FROM salescall.attempts a WHERE a.contact_id = p_contact AND salescall.is_dialed(a.state)),
       c AS (SELECT x.claimed_at, x.ended_at, salescall.unrung_blame(x.attempt_id) AS blame FROM x
             WHERE NOT x.rang AND x.claimed_at > coalesce((SELECT max(y.claimed_at) FROM x y WHERE y.rang), '-infinity'::timestamptz))
  SELECT (count(*) FILTER (WHERE c.blame IN ('number', 'pending')))::integer,
         (count(*) FILTER (WHERE c.blame = 'number'))::integer,
         salescall.jst(min(c.claimed_at) FILTER (WHERE c.blame IN ('number', 'pending')))::date,
         max(c.ended_at) FILTER (WHERE c.blame IN ('number', 'pending'))
  FROM c
$$;
GRANT EXECUTE ON FUNCTION salescall.failed_run(uuid) TO salescall_app;

-- ═════════ (3) ═════════
CREATE FUNCTION salescall.line_faults_unrecorded(p_line uuid)
RETURNS TABLE (attempt_id uuid, client_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT a.attempt_id, a.client_id FROM salescall.attempts a
  WHERE a.line_id = p_line AND a.tenant_id = salescall.cur_tenant()
    AND a.claimed_at > coalesce((SELECT b.claimed_at FROM salescall.attempts b
                                 WHERE b.line_id = p_line AND salescall.is_dialed(b.state) AND b.ended_at IS NOT NULL
                                   AND salescall.rang(b.state, b.cut_off, b.attempt_id)
                                 ORDER BY b.claimed_at DESC LIMIT 1), '-infinity'::timestamptz)
    AND salescall.unrung_blame(a.attempt_id) = 'line'
    AND NOT EXISTS (SELECT 1 FROM salescall.error_events e WHERE e.attempt_id = a.attempt_id AND e.source = 'line')
  ORDER BY a.claimed_at
$$;
REVOKE ALL ON FUNCTION salescall.line_faults_unrecorded(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.line_faults_unrecorded(uuid) TO salescall_app;

-- ═════════ (4) ═════════
ALTER TABLE salescall.setting_definitions DISABLE TRIGGER setting_definitions_frozen;
UPDATE salescall.setting_definitions SET meaning =
  '同じ番号へかけて、お客様のところで電話が鳴らなかった（回線の部品＝スマホのアプリがかけられなかった・お客様が出る前に回線や'
  'こちらの側で終わった）ときに、同じ番号へ何日目にかけ直すかの並びです（1＝最初に鳴らなかった日。例：[1, 1, 2, 4]＝その日に'
  'もう1回・次の日に1回・そのさらに2日後に1回）。番号のせいか回線のせいかは、同じ回線の前後の電話で決めます：同じ回線がそのあと'
  'ほかの番号を鳴らせたら番号のせい、鳴った電話を挟まずに違う番号も続けて鳴らなければ回線のせい（回線のせいの電話は数えず、'
  '行もやめません。回線を休ませ、続けば回線を止めて知らせます）。番号のせいと分かった電話が並びの数だけ続いたら、その番号へは'
  'かけません（お客様が言った別の番号があれば、そちらへかけます。無ければ、その行を終えます）。まだどちらか分からないあいだは、'
  'その行をかけずに待ちます。行の回数には数えません。途中でお客様のところで鳴れば、数え直します。同じ日のかけ直しは、同じ'
  'お客様への最短の間隔をあけます。'
 WHERE key = 'dial.failed_retry_days';
ALTER TABLE salescall.setting_definitions ENABLE TRIGGER setting_definitions_frozen;
