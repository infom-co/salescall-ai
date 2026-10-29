-- 0144 見直し22 の直し：まだ分からない電話の行に、ほかの回線からかけられるように・声で止めた回線の止め直し（2026-10-29。書いた人：CL-2。DESIGN.md 5-4・7-2-1・9-1）
--
-- (1) 前の発信の締めを待つ行（task_awaiting_decision。門の awaiting_decision と、候補を出す処理が読む1か所）：番号のせいか回線のせいかが
--     まだ分からない電話（unrung_blame が pending）は、その電話をかけた回線でだけ待つ。ほかの回線は、ふつうの決まり（最短の間隔・約束の
--     時刻など）でかけてよい。2026-10-24 管理者：「明らかに番号に問題がある場合は、その行は取りやめですが、回線の問題が明らかな場合は
--     行は取りやめでなく一時中止だけですよ」＝止めるのは回線であって行ではない。
--     ⛔ 0143：まだ分からない電話の行を、どの回線にも出さなかった（見直し22 の 5。場面 n2b：回線 A が壊れ、B は元気なのに、9:30 の約束を
--       12時まで待たせた）。
--     - 回線を渡さない（p_line が NULL）ときは、今までどおり、どの回線の締めも待つ（締めの処理の「判定し直し」の待ちなど）。
--     - ほかの回線でかけたあとは、まだ分からない電話も待たずに締める（行の予定はあとの発信の締めが決める＝core/settle.py の `_later_dial`）。
--       ほかの回線で鳴っても、その番号を番号のせいとは決めない（番号のせいは、同じ回線のあとの証拠で決める＝unrung_blame のまま）。
-- (2) 声が聞き取れずに止めた回線を、テスト番号で再開したあと、また聞き取れずに止め直したときの、続けて止めた回数（audio_stops_in_a_row。
--     2026-10-29 管理者の決定。下の (2)）。

-- ═════════ (1) 締めを待つ行は、まだ分からない電話なら、その電話をかけた回線でだけ待つ ═════════
DROP FUNCTION salescall.task_awaiting_decision(uuid);
CREATE FUNCTION salescall.task_awaiting_decision(p_task uuid, p_line uuid DEFAULT NULL) RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT EXISTS (SELECT 1 FROM salescall.attempts a WHERE a.task_id = p_task AND salescall.is_dialed(a.state)
                 AND NOT salescall.is_settled(a.attempt_id)
                 AND (p_line IS NULL OR a.line_id = p_line OR salescall.unrung_blame(a.attempt_id) IS DISTINCT FROM 'pending'))
$$;
GRANT EXECUTE ON FUNCTION salescall.task_awaiting_decision(uuid, uuid) TO salescall_app;

-- ═════════ (2) 声が聞き取れずに止めた回線の止め直し（2026-10-29 管理者の決定） ═════════
-- 管理者：「テスト番号が鳴れば再開のあと、また聞き取れなければ止め直し、次に試すまで 15→30→60→120→240分あけます。+管理者か契約者が、
-- 直ったことを確かめて手で再開も可能」。テスト番号は声が通るかを確かめられないので、鳴れば再開し、また聞き取れなければ止め直す。続けて
-- 止めた回数（この関数の1か所）に応じて、次に再開を試してよいまであける（あける長さは、テスト番号の長さの並び line.test_ring_gaps を
-- そのまま使う＝core/watch.checkable。新しい設定は足さない）。声以外の理由（鳴らない）で止めた回線は今のまま（0）。
-- 声が聞き取れずに止めた停止＝止めた時刻までに記録した、その回線のいちばん新しい回線の異常が、声の受け渡しの理由（trouble_counts_line）の
-- 電話。続けて＝その回線で、声が聞き取れて話し終えた電話（こちらの不具合なしに ended まで進んだ電話）のあと。
CREATE FUNCTION salescall.audio_stops_in_a_row(p_stop bigint) RETURNS integer
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  WITH s AS (SELECT x.event_id, x.line_id, x.at FROM salescall.stop_events x
             WHERE x.event_id = p_stop AND x.tenant_id = salescall.cur_tenant() AND x.scope = 'line' AND x.action = 'stop'
               AND x.cause::text = 'line_failure'),
       audio AS (SELECT x.event_id, x.at FROM salescall.stop_events x, s
                 WHERE x.line_id = s.line_id AND x.scope = 'line' AND x.action = 'stop' AND x.cause::text = 'line_failure' AND x.at <= s.at
                   AND coalesce((SELECT salescall.trouble_counts_line(a.trouble) FROM salescall.error_events e
                                 JOIN salescall.attempts a ON a.attempt_id = e.attempt_id
                                 WHERE e.line_id = s.line_id AND e.source = 'line' AND e.occurred_at <= x.at
                                 ORDER BY e.error_id DESC LIMIT 1), false)),
       good AS (SELECT max(a.ended_at) AS t FROM salescall.attempts a, s
                WHERE a.line_id = s.line_id AND a.state = 'ended' AND a.trouble IS NULL)
  SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM audio, s WHERE audio.event_id = s.event_id) THEN 0
              ELSE (SELECT count(*) FROM audio, good WHERE audio.at > coalesce(good.t, '-infinity'::timestamptz))::integer END
$$;
REVOKE ALL ON FUNCTION salescall.audio_stops_in_a_row(bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.audio_stops_in_a_row(bigint) TO salescall_app;

-- ═════════ (3) テスト番号の使い方（2026-10-29 管理者） ═════════
-- 管理者がシステムの初めの値として仮の番号を入れ、契約者が自分の番号を入れたら、その契約者ではそちらを使う（今の設定の作り＝override の
-- まま。値は変えない）。説明の文に書く（あわせて 0143 で外した「無いあいだは、その前の電話で決め」を直す）
ALTER TABLE salescall.setting_definitions DISABLE TRIGGER setting_definitions_frozen;
UPDATE salescall.setting_definitions
SET meaning = 'テスト番号：回線で電話が鳴るかを試す相手の番号（お客様ではない電話）。管理者がシステムの初めの値として仮の番号を入れ、'
              '契約者が自分の番号を入れたら、その契約者ではそちらを使う。その回線にまもなくかける行が無いとき、番号のせいか回線のせいかが'
              '分からない電話を決める（鳴れば番号のせい・鳴らなければ回線のせい）のと、止めた回線を再開してよいかの確かめに使う。鳴ったら'
              '出る前に切る。どちらも無いあいだは、分からない電話は決めずに待ち（契約者と管理者に1回知らせる）、再開の確かめはアプリが'
              '応答するかだけで行う'
WHERE key = 'line.test_number';
ALTER TABLE salescall.setting_definitions ENABLE TRIGGER setting_definitions_frozen;
