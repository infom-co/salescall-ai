-- 0123 見直し17b：同じ判断を1か所に寄せる直しと、記録の理由の直し（2026-10-23。書いた人：CL-2）

-- ═════════ (1) 誤判定率の抜き取りで測る通話 ═════════
-- 決まった文面だけの電話（いまはお詫びの電話）を測らないかは、門・候補を出す処理・品質の段と同じ1か所（fixed_text_kind・
-- keeping_kind）で決める。⛔ 見直し17b：前は purpose = 'apology' を直書きしていた（決まった文面の電話の種類を増やすと食い違う）。
CREATE OR REPLACE FUNCTION salescall.call_to_measure(p_attempt uuid) RETURNS boolean LANGUAGE sql STABLE AS $$
  SELECT EXISTS (SELECT 1 FROM salescall.attempts a
                 WHERE a.attempt_id = p_attempt
                   AND NOT salescall.fixed_text_kind(salescall.keeping_kind(a.purpose, a.promised))
                   AND EXISTS (SELECT 1 FROM salescall.attempt_state_history h
                               WHERE h.attempt_id = a.attempt_id AND h.to_state = 'in_conversation'))
$$;

-- ═════════ (2) 赤字の警告を閉じた理由 ═════════
-- 'confirm_expired'＝確認の有効期間（設定 budget.deficit_confirm_valid）が切れたが、まだ赤字（閉じて、すぐ新しく警告する）。
-- ⛔ 見直し17b：前は、この場合も 'not_deficit'（赤字でなくなった）と書いていた（記録が事実と違う）。
-- 'stopped'（赤字で止めた）は、赤字では止めなくなった（2026-10-22 ユーザー「止めない。知らせるだけでいい」・0118）ので、新しく
-- 書かせない。前に書かれた行は、その時の事実なので書き換えない（NOT VALID：いまある行は確かめず、これから書く行だけ確かめる）。
ALTER TABLE salescall.deficit_warnings DROP CONSTRAINT deficit_warnings_closed_why_check;
ALTER TABLE salescall.deficit_warnings ADD CONSTRAINT deficit_warnings_closed_why_check
  CHECK (closed_why IN ('not_deficit', 'confirm_expired')) NOT VALID;

-- ═════════ (3) 「最新が正本」の記録の順番は、データベースの1か所（next_seq）が決める ═════════
-- 結果の版（task_results）と業務設定の版（business_versions）の番号も、ほかの記録と同じく next_seq が決める（渡された値を使わない）。
-- ⛔ 見直し17b：前はこの2つだけ、書く側（results.py・business_settings.py）が「最大＋1」を自分で書いていた（同じ判断が3か所。
-- 大きな番号を直に入れると、あとの「最大＋1」が桁あふれで失敗し続ける＝0109 で直した穴が、この2つに残っていた）。
-- next_seq の引数を (埋める列, 束ねる列…) にする（前は埋める列が seq に決まっていた）。いまの4つのトリガーも新しい形で作り直す。
CREATE OR REPLACE FUNCTION salescall.next_seq() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE col text := TG_ARGV[0]; cond text := ''; keys text[] := '{}'; n bigint;
BEGIN
  FOR i IN 1 .. TG_NARGS - 1 LOOP
    cond := cond || format(' AND %I::text = $1[%s]', TG_ARGV[i], i);
    keys := keys || (to_jsonb(NEW) ->> TG_ARGV[i]);
  END LOOP;
  EXECUTE format('SELECT coalesce(max(%I), 0) + 1 FROM %I.%I WHERE true%s', col, TG_TABLE_SCHEMA, TG_TABLE_NAME, cond)
    INTO n USING keys;
  NEW := jsonb_populate_record(NEW, jsonb_build_object(col, n));
  RETURN NEW;
END $$;
DROP TRIGGER next_seq ON salescall.task_next_actions;
DROP TRIGGER next_seq ON salescall.run_versions;
DROP TRIGGER next_seq ON salescall.client_states;
DROP TRIGGER next_seq ON salescall.version_name_readings;
CREATE TRIGGER next_seq BEFORE INSERT ON salescall.task_next_actions
  FOR EACH ROW EXECUTE FUNCTION salescall.next_seq('seq', 'task_id');
CREATE TRIGGER next_seq BEFORE INSERT ON salescall.run_versions
  FOR EACH ROW EXECUTE FUNCTION salescall.next_seq('seq', 'run_id');
CREATE TRIGGER next_seq BEFORE INSERT ON salescall.client_states
  FOR EACH ROW EXECUTE FUNCTION salescall.next_seq('seq', 'client_id');
CREATE TRIGGER next_seq BEFORE INSERT ON salescall.version_name_readings
  FOR EACH ROW WHEN (NEW.reading_by = 'recording') EXECUTE FUNCTION salescall.next_seq('seq', 'version_id', 'name_text');
CREATE TRIGGER next_seq BEFORE INSERT ON salescall.task_results
  FOR EACH ROW EXECUTE FUNCTION salescall.next_seq('version_no', 'task_id');
CREATE TRIGGER next_seq BEFORE INSERT ON salescall.business_versions
  FOR EACH ROW EXECUTE FUNCTION salescall.next_seq('version_no', 'business_id');

-- ═════════ (4) お詫びしてすぐかけ直してよいか（resume_allowed）・判定できない通話がまだ決まっていないか（unknown_pending） ═════════
-- resume_allowed は「その試行が途中で切れた通話で、続けて切れた数が resume_in_a_row() 回まで」を、この関数だけで答える。⛔ 見直し17b：
-- 前は「続けて切れた数が1以上」で「その試行が切れた」の代わりにしていた（その試行が回線の失敗でも、前に切れた数が残れば真になり、
-- 呼ぶ側の「切れたか」の確かめに頼っていた。壊しても試験が落ちなかった）。呼ぶ側（core/next_action）は、ここだけを読む。
-- unknown_pending の「あとの発信の締めが済んでいる」は外さない：締めの済んでいない通話には結果がまだ無いので、それを「決まった」と
-- みなさない（いまの読む所＝settle.awaiting_rejudge は締め待ちの行を先に外し、refusal_undecided は途中で切れた通話を外すが、それに
-- 頼らず、この関数だけで正しい答えにする。試験：test_undecided_unknown）。
CREATE OR REPLACE FUNCTION salescall.resume_allowed(p_attempt uuid) RETURNS boolean
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT coalesce((SELECT a.cut_off FROM salescall.attempts a WHERE a.attempt_id = p_attempt), false)
     AND salescall.cuts_in_a_row(p_attempt) <= salescall.resume_in_a_row()
$$;

-- ═════════ (5) 報酬・時給・回線の応答率に数える電話（電話の種類 keeping_kind から、1か所で） ═════════
-- counts_for_reward(種類)：どれかの報酬（時給を含む）に数える電話か。お詫びの電話（こちらの誤りのため）は数えない。
-- counts_per_call(種類)：1本あたりに数える電話か（かけた1本・話せた1本の報酬・時給の「電話と電話の間」・回線の応答率）。お詫びに
--   加えて、途中で切れたあとの続きの電話も数えない（2026-10-18 ユーザー：「回線が切れてもシステムが途切れても、1人のお客さんに
--   対して、50円ですよね」）。続きの電話の時間は時給に入れる（1人のお客様への総時間）。
-- ⛔ 見直し17b：前は報酬（core/reward.py の2か所）・時給（work_seconds）・回線の応答率（line_answer_counts）が、それぞれ発信の目的の
-- 名前を直に書いていた（電話の種類を足すと食い違う）。働きは変えない（番号の聞き直し・約束の電話は、今までどおり数える）。
CREATE FUNCTION salescall.counts_for_reward(p_kind text) RETURNS boolean LANGUAGE sql IMMUTABLE AS
$$ SELECT p_kind IS DISTINCT FROM 'apology' $$;
CREATE FUNCTION salescall.counts_per_call(p_kind text) RETURNS boolean LANGUAGE sql IMMUTABLE AS
$$ SELECT salescall.counts_for_reward(p_kind) AND p_kind IS DISTINCT FROM 'resume' $$;
GRANT EXECUTE ON FUNCTION salescall.counts_for_reward(text), salescall.counts_per_call(text) TO salescall_app;

CREATE OR REPLACE FUNCTION salescall.work_seconds(p_from timestamptz, p_to timestamptz)
RETURNS TABLE (at timestamptz, seconds numeric)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT a.claimed_at,
         extract(epoch FROM a.ended_at - greatest(a.claimed_at, p_from))
         + CASE WHEN NOT salescall.counts_per_call(salescall.keeping_kind(a.purpose, a.promised)) THEN 0
                ELSE coalesce((SELECT t.hourly_gap_seconds FROM salescall.reward_terms t
                               WHERE t.client_id = a.client_id AND t.valid_from <= a.claimed_at
                               ORDER BY t.valid_from DESC LIMIT 1), 0) END
  FROM salescall.attempts a
  WHERE a.client_id = salescall.cur_client() AND salescall.cur_client() IS NOT NULL
    AND a.tenant_id = salescall.cur_tenant()
    AND salescall.is_dialed(a.state) AND a.ended_at IS NOT NULL
    AND salescall.counts_for_reward(salescall.keeping_kind(a.purpose, a.promised))
    AND a.ended_at > p_from AND a.claimed_at <= p_to
$$;

-- ═════════ (6) お客様のところで鳴った発信・お客様に届いた発信の決まりを1か所に ═════════
-- 回線の応答率の「かけた本数」は、お客様に届いた発信（数え方の種類 dial_kind の 'reached'・'cut_off'＝core/attempts.rang と同じ）
-- で、終わったもの。⛔ 見直し17b：前は「呼び出し中・出た・話し中・応答なしに入った」を別に書いていた。そのため、呼び出し中のあと
-- 回線の側で失敗した発信（お客様が出る前の回線の失敗＝どの回数にも数えない＝2026-10-22 ユーザー「OK」）を、出てもらえなかった
-- 1本に数え、回線の失敗が続くと応答率が下がって見えた（この1つだけ働きが変わる）。
CREATE OR REPLACE FUNCTION salescall.line_answer_counts(p_line uuid, p_from date, p_to date)
RETURNS TABLE (day date, hour integer, dials integer, answers integer)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT salescall.jst(a.claimed_at)::date, extract(hour FROM salescall.jst(a.claimed_at))::integer,
         count(*)::integer,
         count(*) FILTER (WHERE salescall.reached_answer(a.attempt_id))::integer
  FROM salescall.attempts a
  WHERE a.line_id = p_line AND a.tenant_id = salescall.cur_tenant() AND salescall.cur_client() IS NULL
    AND salescall.counts_per_call(salescall.keeping_kind(a.purpose, a.promised))
    AND NOT salescall.is_active(a.state)
    AND salescall.jst(a.claimed_at)::date BETWEEN p_from AND p_to
    AND salescall.dial_kind(a.state, a.cut_off, a.attempt_id) IN ('reached', 'cut_off')
  GROUP BY 1, 2
$$;
-- この回線が、ほかの依頼者のためにこの番号へかけたか（お客様のところで鳴りえた発信＝salescall.is_placed。ほかの依頼者がかけた番号の
-- 間隔・ほかの契約者がかけた番号と同じ1か所）。⛔ 見直し17b：前は「呼び出し中に入った」で見ていたので、呼び出し中を知らせずに
-- 出た回線の発信を見落とした（0075 で応答率について直した穴が、ここに残っていた）。
CREATE OR REPLACE FUNCTION salescall.line_called_number_elsewhere(p_line uuid, p_phone text)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT EXISTS (SELECT 1 FROM salescall.attempts a
                 WHERE a.line_id = p_line AND a.tenant_id = salescall.cur_tenant() AND a.phone_e164 = p_phone
                   AND a.client_id IS DISTINCT FROM salescall.cur_client() AND salescall.is_placed(a.state))
$$;

-- ═════════ (7) かけられる回線の一覧（lines_serving）が見る停止 ═════════
-- 回線に効く停止は、停止の範囲の1か所（open_stops：全体・契約者・その回線）で見る。⛔ 見直し17b：前は回線の停止だけを直に書いて
-- いたので、全体・契約者の緊急停止のあいだも、その回線を「かけられる」と数え、約束の取り置きの割り当てに入れていた（かからない
-- 回線に約束を割り当てると、ほかの回線の取り置きが減る）。依頼者・業務の停止は回線ごとには決まらないので、ここでは見ない（依頼者・業務
-- の範囲は、約束の一覧 promises_waiting が blocking_stops で見る）。
CREATE OR REPLACE FUNCTION salescall.lines_serving()
RETURNS TABLE (line_id uuid, client_id uuid, idle boolean)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT o.line_id, o.client_id,
         NOT EXISTS (SELECT 1 FROM salescall.attempts a WHERE a.line_id = o.line_id AND salescall.is_live(a.state, a.claim_expires_at))
  FROM salescall.lines o
  WHERE o.tenant_id = salescall.cur_tenant() AND o.retired_at IS NULL AND o.lease_expires_at > now()
    AND NOT EXISTS (SELECT 1 FROM salescall.open_stops(o.tenant_id, NULL, NULL, o.line_id))
$$;
