-- 0134 テスト番号・甚大な問題の電話を終える・固まった見張り（3秒）・名前の録音の台帳のシステムの単位（2026-10-24 夜。書いた人：CL-2）
--
-- (1) テスト番号（2026-10-24 ユーザー決定：「その回線にかける行が残っていないときは、『テスト番号』で決めます。前に鳴った電話があれば
--     番号のせい、一度も鳴らせていなければ回線のせいです。（テスト番号はフェーズ1で知らせます。）」「回線が止まったあとの再開の確かめ：OK」）
--     - 設定 line.test_number（契約者ごと。値の形「電話番号」＝0133。初めは無い＝⏳ フェーズ1でユーザーが入れる）。お客様の番号ではない
--       （門・発信禁止・番号の種類の決まりは通らない。その回線で電話が鳴るかを試すだけ）。
--     - 鳴るか試した記録 line_ring_tests（回線ごと。だれが・いつ・何のため・結果＝鳴った／鳴らなかった／分からない）。書くのは
--       core/ring_test.py だけ。
--     - **鳴ったテストは、その回線で「鳴った電話」と同じ証拠**（1か所＝salescall.line_rings）：番号のせいか回線のせいかの続きの区切り
--       （unrung_blame・line_run_pending・close_unrung_run・line_faults_unrecorded）と、回線が直った証拠（consecutive_errors の line_ok）が、
--       どれもここを読む。鳴らなかったテストで、まだ分からない続きがあれば、続きを閉じて回線のせいにする（close_unrung_run の
--       test_silent）。⛔ 0131 の no_rows（前の区切りで決める）は、テスト番号が無いあいだだけの形（⏳）。
-- (2) 甚大な問題の電話をお詫びして終える（2026-10-24 ユーザー決定：「プログラムやAIがこちらの意図しない相手がおかしいと思うような状況に
--     陥った場合全般や、回線が悪く、声がお互いや片方がほとんど聞き取れない場合など規定通りの会話が進められない状態です。」）：
--     試行に、なぜ終えたか（trouble）を残す。frozen＝こちらのプログラム（通話を進める部品）が固まった（回線ごとの見張りが決める＝(3)）／
--     ai_malfunction＝AI・プログラムが意図しない状態（英語・関係のない文を話し続ける・声が出ない など）／poor_audio＝声がお互い・片方に
--     ほとんど聞き取れない／cannot_proceed＝そのほか、決まりどおりの会話が進められない。残すのは、進行中の試行を「結果不明」
--     （technical_error）にする同じ書き換えの中だけ（1回だけ・あとで変えない）。お客様が出たあとなら、判定は「途中で切れた」と読み
--     （judge.cut_off の1か所）、復旧次第お詫びとともにかけ直す（同じ行で続けて2回まで＝今のまま）。
-- (3) 固まった見張り（2026-10-24 ユーザー：「20秒でも長すぎるでしょ」→ 3秒の案に「はい」）：見張りはデータベースを通さない（回線ごとの
--     処理の中＝supervisor/line.py）。⛔ 0130 の「通話の進んだ時刻」（progress_at・call_stalled・line_call_progressing＝90秒）は外す
--     （データベースの書き込みの遅れで元気な電話を切りうる・90秒の無言）。固まったとみなす長さは設定 call.frozen_after（システム。3秒）。
-- (4) 名前の録音の台帳の、代表的な名前をシステムの単位に（2026-10-19 ユーザー：「代表的な名前を事前にシステム単位で用意しておき、それにない
--     物だけ、録音すればいいんじゃないですか？」）：管理者がシステムの取引で作って承認した名前の部分と続く言葉（契約者の無い組）は、
--     すべての契約者が読む。書けて承認できるのは管理者だけ。契約者が作って承認したもの（それに無いものだけ）は、その契約者の中だけ。
--     使う録音を決めるのは見方 current_name_recordings の1か所：同じ鍵なら、契約者が承認したものを先にする（その契約者が聞いて選んだ
--     もの）。⛔ 0132 は契約者の単位だけで、契約者ごとに同じ名字を録り直すことになった。
-- (5) 名前の録音の台帳を3つの部分に分ける（2026-10-24 ユーザー：「『営業部部長の〇〇様は』も『営業部部長』『〇〇様』『は』に分けて録音して
--     おいた方が汎用性があるんじゃないですか？『営業部部長の〇〇様は』だけだと、総務課長の同じ名前の人が必要な時に、また録音し直さねば
--     ならず、永遠に録音が必要ですよ。」）：
--     - 役職の部分（単位 name_role）：「営業部部長の」。鍵＝部署と役職（name_role）・声。文は「部署と役職＋の」。
--     - 名前の部分（単位 name）：「「おかだ」様」。鍵＝読み（name_reading）・声（同じ読みなら漢字が違っても同じ）。役職のみの目的のお客様の
--       呼び方（「社長様」「院長先生」。CL-1 の phrases.ROLE_HONORIFIC）も、名前と同じ所に入るので名前の部分（鍵＝name_role・声。読みは無い）。
--     - 続く言葉（単位 name_tail）：助詞から始まる文（「はいらっしゃいますでしょうか？」）。鍵＝続く言葉の名前・声。助詞は単独で録らず、
--       続く言葉の頭に付ける（どの続く言葉も助詞が1つに決まっているので汎用性は同じで、つなぎ目が1つ減る）。続く言葉ごとの助詞は
--       salescall.name_tail_particle の1か所。
--     ⛔ 0132 は「（部署・役職の）名前＋助詞」を1つの部分にしたので、同じ名前でも役職・助詞ごとに録り直すことになった（name_form は外す）。

-- ═════════ (1) テスト番号 ═════════
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
    WHEN 'daily_range' THEN CASE WHEN jsonb_typeof(v) <> 'array' THEN false
                                 WHEN jsonb_array_length(v) <> 2 THEN false
                                 WHEN jsonb_typeof(v -> 0) <> 'string' OR jsonb_typeof(v -> 1) <> 'string' THEN false
                                 WHEN NOT ((v ->> 0) ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$') THEN false
                                 WHEN NOT ((v ->> 1) ~ '^(([01][0-9]|2[0-3]):[0-5][0-9]|24:00)$') THEN false   -- 終わりだけ 24:00（1日の終わり）を書ける
                                 ELSE (v ->> 0) <> (v ->> 1) END
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

INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, finest_scope) VALUES
('line.test_number', 'override', 'phone', 'テスト番号：回線で電話が鳴るかを試す相手の番号（契約者が用意した、お客様ではない電話）。その回線にかける行が'
 '残っていないとき、番号のせいか回線のせいかが分からない電話を決める（鳴れば番号のせい・鳴らなければ回線のせい）のと、止めた回線を'
 '再開してよいかの確かめに使う。鳴ったら出る前に切る。無いあいだは、その前の電話で決め、再開の確かめはアプリが応答するかだけで行う',
 'DESIGN.md 5-4・7-2-1・9-1', 'tenant'),
('call.frozen_after', 'override', 'seconds', '通話を進める部品（会話）から「動いています」の知らせが、この秒数途切れたら、こちらのプログラムが'
 '固まったとみなし、すぐにお詫びの録音を流して終える（復旧次第お詫びとともにかけ直す）。会話は、お客様に何も聞こえない時間が2秒続くと'
 '「はい、」を流すので、動いていればこの秒数のうちに必ず何かが起きる。通話の長さ・保留の長さ（call.hold_wait）とは別のもの',
 'DESIGN.md 5-4', 'system');
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('line.test_number', 'system', 'null', 'provisional', '2026-10-24 ユーザー決定：「テスト番号はフェーズ1で知らせます。」（契約者ごとに入れる）',
 'フェーズ1でユーザーから番号が届いたとき', 'ユーザー'),
('call.frozen_after', 'system', '3', 'decided', '2026-10-24 ユーザー：「20秒でも長すぎるでしょ」→ 3秒の案に「はい」',
 '模擬通話と最初の実測で、元気な通話を誤って切っていないかを見て', 'ユーザー');

CREATE TABLE salescall.line_ring_tests (
  test_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id uuid NOT NULL,
  line_id   uuid NOT NULL,
  tested_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  purpose   text NOT NULL CHECK (purpose IN ('no_rows', 'resume_check')),   -- かける行が残っていない／止めた回線の再開の確かめ
  rang      boolean,                                                      -- 鳴った／鳴らなかった／NULL＝分からない（電話の部品が答えない）
  tested_by text NOT NULL DEFAULT salescall.cur_actor() CHECK (btrim(tested_by) <> ''),
  FOREIGN KEY (tenant_id, line_id) REFERENCES salescall.lines (tenant_id, line_id)
);
CREATE INDEX line_ring_tests_by_line ON salescall.line_ring_tests (line_id, tested_at);
ALTER TABLE salescall.line_ring_tests ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON salescall.line_ring_tests
  USING (tenant_id = salescall.cur_tenant()) WITH CHECK (tenant_id = salescall.cur_tenant());
GRANT SELECT, INSERT ON salescall.line_ring_tests TO salescall_app;   -- 書くのは core/ring_test.py だけ（checks/rules.py）
CREATE TRIGGER line_ring_tests_frozen BEFORE UPDATE OR DELETE ON salescall.line_ring_tests
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();

-- その回線で「お客様のところで鳴った電話」と「鳴ったテスト」（1か所。began＝続きの並びの時刻・ended＝終わった時刻）。読むのは、持ち主の
-- 権限で動く関数だけ（回線の上のほかの依頼者の試行も見る）
CREATE FUNCTION salescall.line_rings(p_line uuid) RETURNS TABLE (began timestamptz, ended timestamptz)
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT a.claimed_at, a.ended_at FROM salescall.attempts a
  WHERE a.line_id = p_line AND salescall.is_dialed(a.state) AND a.ended_at IS NOT NULL
    AND salescall.rang(a.state, a.cut_off, a.attempt_id)
  UNION ALL
  SELECT t.tested_at, t.tested_at FROM salescall.line_ring_tests t WHERE t.line_id = p_line AND t.rang
$$;
REVOKE ALL ON FUNCTION salescall.line_rings(uuid) FROM PUBLIC;

ALTER TABLE salescall.line_run_closures DROP CONSTRAINT line_run_closures_reason_check;
ALTER TABLE salescall.line_run_closures ADD CONSTRAINT line_run_closures_reason_check
  CHECK (reason IN ('no_rows', 'retired', 'test_silent'));

-- 鳴らなかった電話のせい（0131 を作り直す）：続きの区切りは、鳴った電話・鳴ったテスト（line_rings）と閉じた印
CREATE OR REPLACE FUNCTION salescall.unrung_blame(p_attempt uuid) RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  WITH me AS (
    SELECT a.line_id, a.claimed_at FROM salescall.attempts a
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
  SELECT CASE WHEN (SELECT count(DISTINCT b.phone_e164) FROM salescall.attempts b, me, lo, hi
                    WHERE b.line_id = me.line_id AND b.claimed_at > lo.t
                      AND b.claimed_at < coalesce(least(hi.rang_at, hi.closed_at), 'infinity'::timestamptz)
                      AND salescall.is_dialed(b.state) AND b.ended_at IS NOT NULL) > 1 THEN 'line'
              WHEN hi.rang_at IS NOT NULL AND (hi.closed_at IS NULL OR hi.rang_at < hi.closed_at) THEN 'number'
              WHEN hi.closed_at IS NOT NULL THEN (SELECT c.verdict FROM salescall.line_run_closures c
                                                  WHERE c.line_id = me.line_id AND c.closed_at = hi.closed_at LIMIT 1)
              ELSE 'pending' END
  FROM me, hi
$$;

-- その回線に、まだ分からない続きがあるか（0131 を作り直す。区切りは line_rings と閉じた印）
CREATE OR REPLACE FUNCTION salescall.line_run_pending(p_line uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  WITH l AS (SELECT line_id FROM salescall.lines WHERE line_id = p_line AND tenant_id = salescall.cur_tenant()),
       lo AS (SELECT greatest(
                coalesce((SELECT max(r.began) FROM l, salescall.line_rings(l.line_id) r), '-infinity'::timestamptz),
                coalesce((SELECT max(c.closed_at) FROM salescall.line_run_closures c, l WHERE c.line_id = l.line_id),
                         '-infinity'::timestamptz)) AS t)
  SELECT coalesce((SELECT count(DISTINCT b.phone_e164) = 1 FROM salescall.attempts b, l, lo
                   WHERE b.line_id = l.line_id AND b.claimed_at > lo.t
                     AND salescall.is_dialed(b.state) AND b.ended_at IS NOT NULL), false)
$$;

-- まだ分からない続きを閉じる（0131 を作り直す。書くのはここだけ。閉じたら true）。p_reason＝no_rows（テスト番号の無い契約者で、
-- その回線にかける行が残っていない。前の区切りで決める＝⏳ テスト番号が入るまでの形）・retired（回線を使わなくした＝回線のせい）・
-- test_silent（テスト番号が鳴らなかった＝回線のせい。呼ぶのは core/ring_test.py の1か所。鳴ったテストは、閉じる印ではなく鳴った電話と
-- 同じ区切り＝line_rings なので、ここは通らない）
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
    WHERE line_id = p_line ORDER BY closed_at DESC LIMIT 1;
    v := CASE WHEN last_ring IS NOT NULL AND (last_close.closed_at IS NULL OR last_ring > last_close.closed_at) THEN 'number'
              WHEN last_close.closed_at IS NOT NULL THEN last_close.verdict
              ELSE 'line' END;
  END IF;
  INSERT INTO salescall.line_run_closures (tenant_id, line_id, reason, verdict)
  VALUES (salescall.cur_tenant(), p_line, p_reason, v);
  RETURN true;
END $$;

-- 回線の異常として、まだ記録していない「回線のせい」の試行（0129 を作り直す。いちばん新しい鳴った電話・鳴ったテストのあと）
CREATE OR REPLACE FUNCTION salescall.line_faults_unrecorded(p_line uuid)
RETURNS TABLE (attempt_id uuid, client_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT a.attempt_id, a.client_id FROM salescall.attempts a
  WHERE a.line_id = p_line AND a.tenant_id = salescall.cur_tenant()
    AND a.claimed_at > coalesce((SELECT max(r.began) FROM salescall.line_rings(p_line) r), '-infinity'::timestamptz)
    AND salescall.unrung_blame(a.attempt_id) = 'line'
    AND NOT EXISTS (SELECT 1 FROM salescall.error_events e WHERE e.attempt_id = a.attempt_id AND e.source = 'line')
  ORDER BY a.claimed_at
$$;

-- 回線が直った証拠 line_ok も line_rings を読む（0124 を作り直す：鳴ったテストも、その回線で電話が鳴った証拠）
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
        WHEN 'none' THEN '-infinity'::timestamptz
      END,
      (SELECT coalesce(max(r.at), '-infinity'::timestamptz)
       FROM salescall.stop_events r JOIN salescall.stop_events s ON s.event_id = r.resolves_event_id
       WHERE s.cause::text = p_cause AND s.tenant_id = salescall.cur_tenant()
         AND CASE WHEN p_line IS NULL THEN s.scope = 'tenant' ELSE s.scope = 'line' AND s.line_id = p_line END)) AS t
  )
  SELECT CASE WHEN NOT (SELECT ok FROM allowed) OR p_evidence NOT IN ('in_conversation', 'verified', 'line_ok', 'none')
                                     OR (p_evidence = 'line_ok' AND p_line IS NULL) THEN NULL
         ELSE (SELECT count(*)::integer FROM salescall.error_events e, since
               WHERE e.kind = ANY(p_kinds) AND e.occurred_at > since.t AND e.tenant_id = salescall.cur_tenant()
                 AND (p_line IS NULL OR e.line_id = p_line)) END
$$;

-- ═════════ (2) 甚大な問題の電話をお詫びして終える ═════════
ALTER TABLE salescall.attempts ADD COLUMN trouble text
  CONSTRAINT attempts_trouble_check CHECK (trouble IS NULL OR trouble IN ('frozen', 'ai_malfunction', 'poor_audio', 'cannot_proceed'));
-- 理由を残せるのは、進行中の試行を結果不明（technical_error）にする同じ書き換えの中だけ（1か所の守り）。結果不明は終わりの状態で、
-- ほかへ移れない（0001 の移り方の表）ので、残した理由が別の状態に付くことは無い。試行を新しく書くのは持ち主の権限の関数だけで、
-- 理由を書かない
CREATE FUNCTION salescall.attempt_trouble_fixed() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.trouble IS DISTINCT FROM OLD.trouble
     AND NOT (OLD.trouble IS NULL AND salescall.is_active(OLD.state) AND NEW.state = 'technical_error') THEN
    RAISE EXCEPTION '終えた理由は、進行中の通話を結果不明にするときに1回だけ残します' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'attempt_trouble_fixed';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER attempt_trouble_fixed BEFORE UPDATE ON salescall.attempts
  FOR EACH ROW EXECUTE FUNCTION salescall.attempt_trouble_fixed();

-- ═════════ (3) 固まった見張りはデータベースを通さない（0130 の進んだ時刻を外す） ═════════
DROP TRIGGER attempt_progress ON salescall.attempts;
DROP FUNCTION salescall.attempt_progress();
DROP FUNCTION salescall.call_stalled(uuid, double precision);
DROP FUNCTION salescall.line_call_progressing(uuid, double precision);
ALTER TABLE salescall.attempts DROP COLUMN progress_at;

-- ═════════ (4)(5) 名前の録音の台帳：3つの部分・システムの単位 ═════════
DROP VIEW salescall.current_name_recordings;
DROP TRIGGER name_recording_check ON salescall.recordings;
DROP FUNCTION salescall.name_recording_check();
DROP INDEX salescall.recording_sets_name_key;
ALTER TABLE salescall.recording_sets DROP CONSTRAINT recording_sets_name_key, DROP CONSTRAINT recording_sets_unit;
ALTER TABLE salescall.recording_sets DROP COLUMN name_form;
ALTER TABLE salescall.recording_sets
  ADD CONSTRAINT recording_sets_unit CHECK (unit IN ('contractor', 'business', 'apology', 'name_role', 'name', 'name_tail')),
  ADD CONSTRAINT recording_sets_name_key CHECK (CASE unit
    WHEN 'name_role' THEN btrim(coalesce(name_role, '')) <> '' AND name_reading IS NULL AND tail IS NULL
    WHEN 'name' THEN tail IS NULL AND ((name_reading ~ '^[ぁ-ゖー・]+$' AND name_role IS NULL)
                                       OR (name_reading IS NULL AND btrim(coalesce(name_role, '')) <> ''))
    WHEN 'name_tail' THEN tail IN ('target', 'target_individual', 'target_confirm', 'target_reconfirm', 'promise_again',
                                   'target_successor')
                          AND name_role IS NULL AND name_reading IS NULL
    ELSE name_role IS NULL AND name_reading IS NULL AND tail IS NULL END);
CREATE INDEX recording_sets_name_key ON salescall.recording_sets (voice_name, unit, name_role, name_reading, tail)
  WHERE unit IN ('name_role', 'name', 'name_tail');

-- 続く言葉ごとの、頭の助詞（1か所。CL-1 の phrases の言い方：target「は」いらっしゃいますでしょうか・target_individual「の」お電話で…・
-- target_confirm「で」いらっしゃいますでしょうか・target_reconfirm「で」お間違えなかったでしょうか・promise_again「と」お電話の約束を…・
-- target_successor「の」後任のご担当者様は…）
CREATE FUNCTION salescall.name_tail_particle(p_tail text) RETURNS text LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE p_tail WHEN 'target' THEN 'は' WHEN 'target_individual' THEN 'の' WHEN 'target_confirm' THEN 'で'
                     WHEN 'target_reconfirm' THEN 'で' WHEN 'promise_again' THEN 'と' WHEN 'target_successor' THEN 'の' END
$$;

-- 台帳の部分（1か所）：組ごとに部分は1つ。役職の部分は「部署と役職＋の」（部分の名前 role）・名前の部分は「「読み」様」（読みの無い
-- 役職のみの呼び方は、助詞で終わらない文。部分の名前 name）・続く言葉は、その続く言葉の助詞から始まる文（部分の名前は続く言葉の名前）
CREATE FUNCTION salescall.name_recording_check() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE s record;
BEGIN
  SELECT unit, name_role, name_reading, tail INTO s FROM salescall.recording_sets WHERE set_id = NEW.set_id;
  IF s.unit NOT IN ('name_role', 'name', 'name_tail') OR s.unit IS NULL THEN
    RETURN NEW;
  END IF;
  IF NEW.part IS DISTINCT FROM (CASE s.unit WHEN 'name_role' THEN 'role' WHEN 'name' THEN 'name' ELSE s.tail END) THEN
    RAISE EXCEPTION '名前の録音の台帳の組の部分は1つで、名前は役職の部分 role・名前の部分 name・続く言葉はその名前です'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'name_recording_part';
  END IF;
  IF NOT (CASE s.unit
            WHEN 'name_role' THEN NEW.spoken_text = s.name_role || 'の'
            WHEN 'name' THEN (CASE WHEN s.name_reading IS NOT NULL THEN NEW.spoken_text = '「' || s.name_reading || '」様'
                                   ELSE btrim(NEW.spoken_text) <> '' AND right(NEW.spoken_text, 1) NOT IN ('は', 'で', 'と', 'の') END)
            ELSE left(NEW.spoken_text, 1) = salescall.name_tail_particle(s.tail) AND length(NEW.spoken_text) > 1 END) THEN
    RAISE EXCEPTION '台帳の文「%」は、鍵と合いません（役職の部分＝「部署と役職＋の」・名前の部分＝「「読み」様」・続く言葉＝助詞「%」から）',
                    NEW.spoken_text, coalesce(salescall.name_tail_particle(s.tail), '-')
      USING ERRCODE = 'check_violation', CONSTRAINT = 'name_recording_text';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER name_recording_check BEFORE INSERT ON salescall.recordings
  FOR EACH ROW EXECUTE FUNCTION salescall.name_recording_check();

ALTER TABLE salescall.recording_sets ALTER COLUMN tenant_id DROP NOT NULL;
ALTER TABLE salescall.recordings ALTER COLUMN tenant_id DROP NOT NULL;
ALTER TABLE salescall.recording_approvals ALTER COLUMN tenant_id DROP NOT NULL;
-- 契約者の無い組は、名前の録音の台帳だけ（1か所）
ALTER TABLE salescall.recording_sets ADD CONSTRAINT recording_sets_system_names
  CHECK (tenant_id IS NOT NULL OR unit IN ('name_role', 'name', 'name_tail'));
-- 契約者の無い行も、組と結ぶ（契約者と組の2つの鍵は、片方が無いと確かめられない）
ALTER TABLE salescall.recordings ADD CONSTRAINT recordings_set FOREIGN KEY (set_id) REFERENCES salescall.recording_sets (set_id);
ALTER TABLE salescall.recording_approvals ADD CONSTRAINT recording_approvals_set
  FOREIGN KEY (set_id) REFERENCES salescall.recording_sets (set_id);

-- システムの組・部分・承認を書けるのは、管理者だけ（1か所。システムの取引に限るのは、下の行ごとの決まり system_names_write）。部分・
-- 承認は、取引から見える組にだけ付けられる（0109 の recording_before_insert・recording_approval_by_person がそのまま守る：システムの取引
-- からは契約者の組が見えない＝管理者も契約者の組に部分を足せない・承認できない。契約者の取引からはシステムの組に書けない＝行ごとの決まり）
CREATE FUNCTION salescall.system_recording_check() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.tenant_id IS NULL AND salescall.cur_actor_kind() IS DISTINCT FROM 'admin' THEN
    RAISE EXCEPTION 'システムの単位の名前の録音を作れて承認できるのは、管理者だけです（システムの取引で）'
      USING ERRCODE = 'insufficient_privilege', CONSTRAINT = 'system_recording_by_admin';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER system_recording_check BEFORE INSERT ON salescall.recording_sets
  FOR EACH ROW EXECUTE FUNCTION salescall.system_recording_check();
CREATE TRIGGER system_recording_check BEFORE INSERT ON salescall.recordings
  FOR EACH ROW EXECUTE FUNCTION salescall.system_recording_check();
CREATE TRIGGER system_recording_check BEFORE INSERT ON salescall.recording_approvals
  FOR EACH ROW EXECUTE FUNCTION salescall.system_recording_check();

-- 見える範囲：システムの単位の名前の録音は、どの契約者の取引からも読める（書くのは管理者のシステムの取引だけ）
CREATE POLICY system_names_read ON salescall.recording_sets FOR SELECT USING (tenant_id IS NULL);
CREATE POLICY system_names_write ON salescall.recording_sets FOR INSERT
  WITH CHECK (tenant_id IS NULL AND salescall.cur_tenant() IS NULL);
CREATE POLICY system_names_read ON salescall.recordings FOR SELECT USING (tenant_id IS NULL);
CREATE POLICY system_names_write ON salescall.recordings FOR INSERT
  WITH CHECK (tenant_id IS NULL AND salescall.cur_tenant() IS NULL);
CREATE POLICY system_names_read ON salescall.recording_approvals FOR SELECT USING (tenant_id IS NULL);
CREATE POLICY system_names_write ON salescall.recording_approvals FOR INSERT
  WITH CHECK (tenant_id IS NULL AND salescall.cur_tenant() IS NULL);

-- 台帳のいまの録音（0132 を作り直す。1か所）：承認した組のうち、鍵ごとに1つ。契約者が承認したものを先に（その契約者が聞いて選んだ）、
-- 無ければ管理者が用意したシステムのもの。同じ単位の中では、いちばん新しく承認したもの。どの契約者のものが見えるかは、取引の範囲
-- （行ごとに読める範囲の決まり）で決まる＝契約者の取引では、その契約者のものとシステムのものだけ
CREATE VIEW salescall.current_name_recordings WITH (security_invoker = true) AS
  SELECT DISTINCT ON (s.unit, s.voice_name, s.name_role, s.name_reading, s.tail)
         s.set_id, s.tenant_id, s.unit, s.voice_name, s.name_role, s.name_reading, s.tail
  FROM salescall.recording_sets s JOIN salescall.recording_approvals p USING (set_id)
  WHERE s.unit IN ('name_role', 'name', 'name_tail')
  ORDER BY s.unit, s.voice_name, s.name_role, s.name_reading, s.tail, (s.tenant_id IS NULL), p.approved_at DESC,
           s.created_at DESC;
GRANT SELECT ON salescall.current_name_recordings TO salescall_app;
