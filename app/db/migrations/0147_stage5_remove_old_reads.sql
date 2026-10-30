-- 0147 作り直しの5段目：もう読まれない古い関数・表・仮の設定を外す（DESIGN.md 17-6。2026-10-30。書いた人：CL-2）
--
-- 2026-10-29 管理者の承認：「回線・行・契約者の状態を、それぞれ1つの欄に持ちます。OK」。4段目（0146）で、読み手（締め・次の動き・門・候補・
-- 回線ごとの処理・回線の空き・休んだ行の戻しの守り）は、みな状態の欄（line_states・task_states・number_states・tenant_states・
-- attempt_blames）を読む形に替わった。下の関数・表は、読むたびに時刻を比べて導く今までの作りで、もうどこからも読まれていない
-- （Python の部品・データベースの関数・引き金・見る形・試験のどれも。外す前に1つずつ確かめた）。
--
-- (1) 外す関数（17-6 の表のとおり。どれも、表のどこが代わるかは 17-6）：
--     unrung_blame（試行のせいの欄 V-1〜V-5）・line_run_start・line_pending・line_run_pending（回線の欄の「確かめ中」の中身）・
--     close_unrung_run と表 line_run_closures（L-10・L-14）・line_faults_unrecorded（L-10・L-3）・poor_audio_line・line_good_rings・
--     line_rings（回線の中身の m・j）・audio_stops_in_a_row（回線の中身の j）・task_trouble_run・trouble_rest_after・task_resting・
--     awaiting_recovery・recovered_after・last_good_talk（行の欄の休み・復旧待ちと、出来事 E5・E11）・task_awaiting_decision（行の欄の
--     判定待ち＝0146 の task_unsettled）・failed_run（番号の欄の n と N-4 の印）。
-- (2) consecutive_errors（エラーが続いた数）から、回線の数え方（直った証拠 line_ok・回線の範囲）と、こちらのプログラムの不具合の数え方
--     （直った証拠 program_fixed＝違う行の数）を外す。回線の休みは回線の欄、こちらの不具合の止めは契約者の欄（C-1）が決める。
--     残すのは会話AIの異常の数え方（証拠 in_conversation＝その契約者のどこかの通話が会話に入った。core/watch.decide_stop が読む）だけ。
--     ⛔ 4段目まで：回線を数える引数（p_line）と、どの種類も使っていない証拠（verified・none）も持っていた。
-- (3) 回線の仮の設定（stop.line.*。どれも管理者の決定ではない＝CL-2 が 0005 で仮に決めた値）を外す（17-6 の「仮の値」の表）：
--     stop.line.stop_after_failures（回線の異常が何件で止めるか。回線の状態は1回のテストの答えで変わる）・stop.line.probe_interval・
--     stop.line.resume_after_successes（止めた回線のアプリの確かめ。回線の休みは、もう停止の記録を作らない。テスト番号が無ければ知らせて
--     待つ＝【決 10-24・10-29】）。ばたつきの設定（stop.flapping.*）は、会話AIの停止の確かめが今のまま使うので残す（回線の分は、確かめで
--     解く原因から回線を外したので、もう読まれない＝core/watch.PROBE_SETTINGS）。
-- (4) 数え方の種類（dial_kind）の「途中で切れた」の決まりを、すぐのかけ直しの1か所（resume_allowed）に寄せる。2つの関数が同じ式
--     （こちらのせいで終えた電話か＝is_ours、か、続けて切れた数が resume_in_a_row() まで）をそれぞれ持っていた（0118・0139。食い違えば、
--     数え方と予定が食い違う＝壊し試しの 0118 の題）。状態の表は、この答えを E4 の resume_allowed として受ける（17章の読み方 (5)）。
--     決まりは変えない（呼ぶ側は、どれもその試行の行の cut_off を渡す）。

-- ═════════ (1) もう読まれない関数と表 ═════════
DROP FUNCTION salescall.close_unrung_run(uuid, text);
DROP FUNCTION salescall.line_run_pending(uuid);
DROP FUNCTION salescall.line_pending(uuid);
DROP FUNCTION salescall.line_run_start(uuid);
DROP FUNCTION salescall.line_faults_unrecorded(uuid);
DROP FUNCTION salescall.poor_audio_line(uuid);
DROP FUNCTION salescall.failed_run(uuid);
DROP FUNCTION salescall.task_awaiting_decision(uuid, uuid);
DROP FUNCTION salescall.unrung_blame(uuid);
DROP FUNCTION salescall.line_rings(uuid);
DROP FUNCTION salescall.line_good_rings(uuid);
DROP FUNCTION salescall.audio_stops_in_a_row(bigint);
DROP FUNCTION salescall.task_resting(uuid);
DROP FUNCTION salescall.task_trouble_run(uuid);
DROP FUNCTION salescall.trouble_rest_after();
DROP FUNCTION salescall.awaiting_recovery(uuid);
DROP FUNCTION salescall.recovered_after(uuid);
DROP FUNCTION salescall.consecutive_errors(text[], text, text, uuid);   -- (2) で、会話AIの分だけの形に作り直す
DROP FUNCTION salescall.last_good_talk();
DROP TABLE salescall.line_run_closures;   -- 続きを閉じる印（0131。理由 migration・決め方 none の印を含む）。書いた close_unrung_run も上で外した

-- ═════════ (2) エラーが続いた数：会話AIの異常の分だけ ═════════
-- 数える範囲は、その契約者の取引の、その契約者のエラーだけ（全体の取引では数えない＝NULL）。数え直すのは、直った証拠（in_conversation＝
-- その契約者のどこかの通話が会話に入った）と、同じ原因の契約者の範囲の停止が解かれた時刻の、遅いほうより後。
CREATE FUNCTION salescall.consecutive_errors(p_kinds text[], p_cause text, p_evidence text) RETURNS integer
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  WITH since AS (
    SELECT greatest(
      (SELECT coalesce(max(h.at), '-infinity'::timestamptz)
       FROM salescall.attempt_state_history h JOIN salescall.attempts a ON a.attempt_id = h.attempt_id
       WHERE h.to_state = 'in_conversation' AND a.tenant_id = salescall.cur_tenant()),
      (SELECT coalesce(max(r.at), '-infinity'::timestamptz)
       FROM salescall.stop_events r JOIN salescall.stop_events s ON s.event_id = r.resolves_event_id
       WHERE s.cause::text = p_cause AND s.tenant_id = salescall.cur_tenant() AND s.scope = 'tenant')) AS t
  )
  SELECT CASE WHEN salescall.cur_tenant() IS NULL OR p_evidence IS DISTINCT FROM 'in_conversation' THEN NULL
         ELSE (SELECT count(*)::integer FROM salescall.error_events e CROSS JOIN since
               WHERE e.kind = ANY(p_kinds) AND e.occurred_at > since.t AND e.tenant_id = salescall.cur_tenant()) END
$$;
REVOKE ALL ON FUNCTION salescall.consecutive_errors(text[], text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.consecutive_errors(text[], text, text) TO salescall_app;

-- ═════════ (3) 回線の仮の設定を外す（契約者の値も含めて。0141 の line.decide_horizon と同じ外し方） ═════════
ALTER TABLE salescall.setting_values DISABLE TRIGGER setting_values_frozen;
ALTER TABLE salescall.setting_definitions DISABLE TRIGGER setting_definitions_frozen;
DELETE FROM salescall.setting_values
WHERE key IN ('stop.line.stop_after_failures', 'stop.line.probe_interval', 'stop.line.resume_after_successes');
DELETE FROM salescall.setting_definitions
WHERE key IN ('stop.line.stop_after_failures', 'stop.line.probe_interval', 'stop.line.resume_after_successes');
-- ばたつきの設定の説明：会話AIの停止の確かめだけが読む（回線の休みは、回線の欄の次のテストの時刻＝17章）
UPDATE salescall.setting_definitions SET meaning = 'ばたつき（止まる・解けるの繰り返し）を数える期間（契約者ごと）。会話AIの異常で止めた'
       '停止の確かめが読む（回線の休みと再開は、回線の状態の表が、テスト番号をあける長さの並び line.test_ring_gaps で決める）'
WHERE key = 'stop.flapping.window';
UPDATE salescall.setting_definitions SET meaning = 'ばたつくたびに、解く前の待ちを何倍にするか（契約者ごと）。会話AIの異常で止めた停止の'
       '確かめが読む（回線の分は、回線の状態の表が決める）'
WHERE key = 'stop.flapping.wait_multiplier';
UPDATE salescall.setting_definitions SET meaning = 'ばたつきで伸ばす待ちの上限（契約者ごと）。会話AIの異常で止めた停止の確かめが読む'
       '（回線の分は、回線の状態の表が決める）'
WHERE key = 'stop.flapping.max_wait';
ALTER TABLE salescall.setting_definitions ENABLE TRIGGER setting_definitions_frozen;
ALTER TABLE salescall.setting_values ENABLE TRIGGER setting_values_frozen;

-- ═════════ (4) 数え方の種類の「途中で切れた」は、すぐのかけ直しの1か所（resume_allowed）を読む ═════════
CREATE OR REPLACE FUNCTION salescall.dial_kind(s salescall.attempt_state, p_cut_off boolean, p_attempt uuid) RETURNS text
LANGUAGE sql STABLE AS $$
  SELECT CASE WHEN NOT salescall.is_dialed(s) THEN NULL
              WHEN NOT salescall.is_placed(s) THEN 'not_placed'
              WHEN coalesce(p_cut_off, false) AND salescall.resume_allowed(p_attempt) THEN 'cut_off'
              WHEN salescall.is_line_failure(s, p_attempt) THEN 'line_failure'
              ELSE 'reached' END
$$;
