-- 0116 見直し15：数え方・止めているあいだに通す電話・約束を果たす電話・判定できない通話の扱いを、それぞれデータベースの1か所に
-- （2026-10-22。書いた人：CL-2）
--
-- (1) 発信の数え方の種類を1か所で決める（salescall.dial_kind）。どの回数も、これを読む：
--       NULL           ＝発信まで進んでいない（予約のまま・取り消し）
--       'not_placed'   ＝発信に失敗（回線の部品が番号を受け付けなかった。お客様の電話は鳴っていない）
--       'line_failure' ＝お客様が出る前に、回線・こちらの仕組みの側で終わった（結果不明）
--       'cut_off'      ＝お客様が出たあと、お客様の意思と関係なく途中で切れた（締めの処理が試行に1回だけ残す cut_off＝judge.cut_off）
--       'reached'      ＝お客様に届いた1回（鳴った・出なかった・話し中・出て話した・お客様が切った・進行中）
--     - 行の回数・再発信の方針の回数・同じお客様の1日の回数（salescall.counts_as_try）：'reached' で、約束したかけ直しでないもの。
--     - 同じお客様への最小の間隔・番号の聞き直しとお詫びの電話の回数・業務ごとの1日の上限：'reached'（core/attempts.reached）。
--     - 回線の1日の上限・ほかの依頼者がかけた番号の間隔：'not_placed' でない発信（salescall.is_placed。番号の評判は回線ごと）。
--     ⛔ 見直し15：前は「お客様の意思と関係なく切れたか」を、切れ方 interrupted と、すべての結果不明（technical_error）で決めていた
--     （not_by_callee）。お客様が自分の言葉の途中で切った通話も数えず、終わりなくかけ続けた。判断は judge.cut_off の1か所
--     （試行の cut_off）だけにする。また、番号の聞き直し・お詫びの電話の回数と業務の1日の上限は「発信まで進んだもの全部」を
--     数え、発信に失敗・回線の失敗も数えていた（決まりが3つあった）。
--     発信に失敗は、どの回数にも数えない（同じ番号で続いたら、その番号へかけない＝0117・core/next_action）。
--     ⚠️ 0113 の説明の (1)「発信に失敗は…方針の回数で止まる＝悪い番号へかけ続けない」と (2)「not_by_callee」は、ここで置き換えた
--     （流した表の作りは書き換えない＝core/migrate の指紋。いまの決まりは、この説明）。
-- (2) 止めているあいだも通す、お客様との約束を果たす電話の種類（停止の原因ごと）を、表 stop_lets_through の1か所に置く
--     （2026-10-22 ユーザー：赤字では、約束の電話・途中で切れたお客様へのお詫びのかけ直し・お詫びの電話の3つだけはかける。
--     品質の理由では、前から、お詫びの電話だけ）。門（core/gate.py）・候補を出す処理（core/scheduler.py）・これからかける約束の一覧
--     （promises_waiting：回線の取り置き）・ほかの依頼者の約束（number_next_promise_elsewhere）・止めたときの予約の取り消し
--     （release_claims_for_stop）が、同じ関数（salescall.blocking_stops・salescall.lets_through）を読む。⛔ 見直し15：前は Python
--     の表だけにあり、データベースの3か所は「止まっていれば、約束の電話もかけない」と読んで、赤字のあいだの約束の電話が回線を
--     取り置かず、止めたときに約束の電話の予約も取り消していた。
-- (3) 約束を果たす電話の種類（salescall.keeping_kind）から、番号の聞き直しを外す（ユーザーが挙げたのは、約束したかけ直し・途中で
--     切れたお客様へのお詫びのかけ直し・お詫びの電話の3つだけ。⛔ 見直し15：前は聞き直しも「約束」として赤字の停止を通っていた）。
--     「新しくかける電話」（is_new_call：「かけない」の例外・業務ごとの1日の上限）は、これまでと同じ（聞き直しは新しい電話ではない）。
-- (4) ほかの依頼者の約束（number_next_promise_elsewhere）は、約束したかけ直し（keeping_kind が promised）だけ（⛔ 見直し15：前は
--     お詫び・続き・聞き直しの印 promised も「約束」と読み、契約者の設定「かけない」で行を終えていた）。
-- (5) 同じ番号への、ほかの依頼者の発信からの間隔は、データベースの中で計算し、待つ時刻だけを返す（number_dial_wait_elsewhere）。
--     ほかの契約者の分は、待つ時刻を1時間単位に切り上げる（⛔ 見直し15：前は、ほかの契約者がその番号へかけた正確な時刻を返していた）。
-- (6) 同じ番号に、お客様が時刻を指定した約束の電話が、約束の前に空ける長さのうちに来るか（number_promise_soon。同じ契約者の、
--     ほかの行。門の _pacing が読み、約束でない電話を待たせる）。そのため、これからかける約束の一覧に番号を足す。
-- (7) 判定できない結果が「まだ決まっていない」かを1か所で決める（salescall.unknown_pending）：判定できないの結果で、同じ行のあとの
--     発信がお客様につながって決まっていないもの。判定し直しの対象（core/settle.awaiting_rejudge）と、断られたかがまだ決まって
--     いない（refusal_undecided）の両方が読む。⛔ 見直し15：途中で切れて判定できなかった通話のあと、お詫びのかけ直しが完了しても、
--     前の通話が「まだ決まっていない」のまま残り（判定し直しの対象からは外れる）、同じお客様のほかの行が永久にかからなかった。
-- (8) いま進行中の発信がある番号か（number_live。候補を出す処理が、データベースの「進行中は1本」の守りと同じ条件で、門が必ず
--     断る行を出さない。期限の切れた予約は、門が取り消すので数えない）。
-- (9) 次の予定のお詫びの印（apology_owed）は、理由（apology_reasons）から作る列にする（同じことを2つの列に持たない）。

-- ═════════ (1) ═════════
CREATE FUNCTION salescall.dial_kind(s salescall.attempt_state, p_cut_off boolean, p_attempt uuid) RETURNS text
LANGUAGE sql STABLE AS $$
  SELECT CASE WHEN NOT salescall.is_dialed(s) THEN NULL
              WHEN NOT salescall.is_placed(s) THEN 'not_placed'
              WHEN coalesce(p_cut_off, false) THEN 'cut_off'
              WHEN s = 'technical_error' AND NOT salescall.reached_answer(p_attempt) THEN 'line_failure'
              ELSE 'reached' END
$$;
GRANT EXECUTE ON FUNCTION salescall.dial_kind(salescall.attempt_state, boolean, uuid) TO salescall_app;

DROP FUNCTION salescall.counts_as_try(salescall.attempt_state, text, boolean, uuid);
DROP FUNCTION salescall.not_by_callee(salescall.attempt_state, text, uuid);
CREATE FUNCTION salescall.counts_as_try(s salescall.attempt_state, p_cut_off boolean, p_promised boolean, p_attempt uuid)
RETURNS boolean LANGUAGE sql STABLE AS $$
  SELECT salescall.dial_kind(s, p_cut_off, p_attempt) = 'reached' AND NOT p_promised
$$;
REVOKE ALL ON FUNCTION salescall.counts_as_try(salescall.attempt_state, boolean, boolean, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.counts_as_try(salescall.attempt_state, boolean, boolean, uuid) TO salescall_app;

-- ═════════ (2) ═════════
CREATE TABLE salescall.stop_lets_through (
  cause salescall.stop_cause NOT NULL,
  kind  text NOT NULL CHECK (kind IN ('apology', 'resume', 'promised')),   -- salescall.keeping_kind の答え（core/stops.KEEPING_KINDS）
  PRIMARY KEY (cause, kind)
);
INSERT INTO salescall.stop_lets_through (cause, kind) VALUES
  ('forbidden_speech', 'apology'), ('quality_gate', 'apology'), ('judge_unknown', 'apology'),   -- 品質：決まった文面だけのお詫びの電話
  ('deficit', 'apology'), ('deficit', 'resume'), ('deficit', 'promised');                       -- 赤字：お客様との約束は果たす
CREATE TRIGGER stop_lets_through_frozen BEFORE INSERT OR UPDATE OR DELETE ON salescall.stop_lets_through
  FOR EACH STATEMENT EXECUTE FUNCTION salescall.forbid_change();
ALTER TABLE salescall.stop_lets_through ENABLE ROW LEVEL SECURITY;   -- すべての契約者に共通の決まり：サーバーの役だけが読む（0107 と同じ守り）
CREATE POLICY server_only ON salescall.stop_lets_through USING (salescall.server_session()) WITH CHECK (salescall.server_session());
GRANT SELECT ON salescall.stop_lets_through TO salescall_app;

-- その原因の停止でも、その種類の電話を通すか（kind が NULL＝営業として新しくかける電話・番号の聞き直しは、どの停止でも通さない）
CREATE FUNCTION salescall.lets_through(p_cause salescall.stop_cause, p_kind text) RETURNS boolean LANGUAGE sql STABLE AS
$$ SELECT EXISTS (SELECT 1 FROM salescall.stop_lets_through l WHERE l.cause = p_cause AND l.kind = p_kind) $$;
GRANT EXECUTE ON FUNCTION salescall.lets_through(salescall.stop_cause, text) TO salescall_app;

-- まだ解かれていない停止のうち、その種類の電話を止めるもの（open_stops の範囲。p_line を渡すと、その回線の停止も含める）
CREATE FUNCTION salescall.blocking_stops(p_tenant uuid, p_client uuid, p_business uuid, p_line uuid, p_kind text)
RETURNS SETOF salescall.stop_events LANGUAGE sql STABLE AS $$
  SELECT s.* FROM salescall.open_stops(p_tenant, p_client, p_business, p_line) s
  WHERE NOT salescall.lets_through(s.cause, p_kind)
$$;
GRANT EXECUTE ON FUNCTION salescall.blocking_stops(uuid, uuid, uuid, uuid, text) TO salescall_app;

-- ═════════ (3) ═════════
CREATE OR REPLACE FUNCTION salescall.keeping_kind(p_purpose text, p_promised boolean) RETURNS text LANGUAGE sql IMMUTABLE AS
$$ SELECT CASE WHEN p_purpose = 'apology' THEN 'apology'
               WHEN p_purpose = 'resume' THEN 'resume'
               WHEN p_purpose = 'ask_number_again' THEN NULL
               WHEN coalesce(p_promised, false) THEN 'promised' END $$;
-- 新しくかける電話＝約束を果たす電話でなく、すぐ・決まった時刻にかける続きの印（promised）も無い電話（番号の聞き直しは新しくない）
CREATE OR REPLACE FUNCTION salescall.is_new_call(p_purpose text, p_promised boolean) RETURNS boolean LANGUAGE sql IMMUTABLE AS
$$ SELECT salescall.keeping_kind(p_purpose, p_promised) IS NULL AND NOT coalesce(p_promised, false) $$;

-- ═════════ (2)(6) これからかける約束の一覧（番号を足す。止めているあいだは、その停止でも通す種類だけ） ═════════
DROP FUNCTION salescall.promises_waiting();
CREATE FUNCTION salescall.promises_waiting()
RETURNS TABLE (task_id uuid, client_id uuid, call_at timestamptz, callee_at timestamptz, phone_e164 text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT n.task_id, n.client_id, n.call_at, n.callee_at, k.phone_e164
  FROM salescall.task_next_actions n
  JOIN salescall.tasks t ON t.task_id = n.task_id
  JOIN salescall.runs r ON r.run_id = t.run_id
  JOIN salescall.task_numbers k ON k.task_id = n.task_id AND k.contact_id = n.contact_id
  WHERE n.tenant_id = salescall.cur_tenant() AND n.promised AND n.kind = 'call'
    AND n.seq = (SELECT c.seq FROM salescall.current_next_actions c WHERE c.task_id = n.task_id)
    AND salescall.client_active(n.client_id) AND NOT salescall.task_excluded_now(n.task_id)
    AND NOT EXISTS (SELECT 1 FROM salescall.blocking_stops(n.tenant_id, n.client_id, r.business_id, NULL,
                                                           salescall.keeping_kind(n.purpose, n.promised)))
    AND NOT EXISTS (SELECT 1 FROM salescall.attempts a
                    WHERE a.task_id = n.task_id AND salescall.is_live(a.state, a.claim_expires_at))
$$;
REVOKE ALL ON FUNCTION salescall.promises_waiting() FROM PUBLIC;   -- ほかの SECURITY DEFINER の関数の中だけで使う（0101）

-- (6) その番号へ、同じ契約者のほかの行の、お客様が時刻を指定した約束の電話で、p_until までに来る（過ぎてまだかけていないものを
--     含む）もののうち、いちばん早い約束の時刻
CREATE FUNCTION salescall.number_promise_soon(p_phone text, p_until timestamptz, p_except uuid) RETURNS timestamptz
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
BEGIN
  PERFORM salescall.server_only();
  RETURN (SELECT min(w.callee_at) FROM salescall.promises_waiting() w
          WHERE w.phone_e164 = p_phone AND w.task_id <> p_except AND w.callee_at <= p_until);
END $$;
REVOKE ALL ON FUNCTION salescall.number_promise_soon(text, timestamptz, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.number_promise_soon(text, timestamptz, uuid) TO salescall_app;

-- ═════════ (2)(4) ほかの依頼者の約束 ═════════
CREATE OR REPLACE FUNCTION salescall.number_next_promise_elsewhere(p_phone text) RETURNS TABLE (relation text, wait_until timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT x.relation, CASE WHEN x.relation = 'same_tenant' THEN max(x.until) END
  FROM (SELECT CASE WHEN n.tenant_id = salescall.cur_tenant() THEN 'same_tenant' ELSE 'other_tenant' END AS relation,
               coalesce(n.window_until, greatest(n.callee_at, n.call_at)) AS until
        FROM salescall.current_next_actions n
        JOIN salescall.task_numbers k ON k.task_id = n.task_id AND k.contact_id = n.contact_id
        JOIN salescall.tasks t ON t.task_id = n.task_id
        JOIN salescall.runs r ON r.run_id = t.run_id
        WHERE k.phone_e164 = p_phone AND n.client_id IS DISTINCT FROM salescall.cur_client()
          AND n.kind = 'call' AND salescall.keeping_kind(n.purpose, n.promised) = 'promised'
          AND NOT EXISTS (SELECT 1 FROM salescall.blocking_stops(n.tenant_id, n.client_id, r.business_id, NULL, 'promised'))) x
  GROUP BY x.relation
$$;

-- ═════════ (2) 止めたときの予約の取り消し：その停止でも通す電話の予約は残す ═════════
CREATE OR REPLACE FUNCTION salescall.release_claims_for_stop(p_event_id bigint) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
DECLARE s salescall.stop_events; n integer;
BEGIN
  SELECT * INTO s FROM salescall.open_stop_events e WHERE e.event_id = p_event_id
    AND (salescall.cur_tenant() IS NULL OR e.tenant_id IS NULL OR e.tenant_id = salescall.cur_tenant());
  IF NOT FOUND THEN
    RAISE EXCEPTION 'まだ解かれていない、その停止はありません' USING ERRCODE = 'check_violation';
  END IF;
  UPDATE salescall.attempts a SET state = 'released'
  WHERE a.state = 'claimed'
    AND NOT salescall.lets_through(s.cause, salescall.keeping_kind(a.purpose, a.promised))
    AND (s.scope = 'system'
         OR (s.scope = 'tenant' AND a.tenant_id = s.tenant_id)
         OR (s.scope = 'client' AND a.client_id = s.client_id)
         OR (s.scope = 'business' AND a.client_id = s.client_id
             AND a.version_id IN (SELECT v.version_id FROM salescall.business_versions v WHERE v.business_id = s.business_id))
         OR (s.scope = 'line' AND a.line_id = s.line_id));
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$;

-- ═════════ (5) ほかの依頼者の発信からの間隔 ═════════
-- p_same_gap＝同じ契約者のほかの依頼者（gate.cross_client_interval）・p_other_gap＝ほかの契約者（gate.other_contractor_interval。
-- NULL＝間隔を置かない）。まだ待つ関係だけを返す。ほかの契約者の待つ時刻は1時間単位に切り上げる（正確な時刻を出さない）
DROP FUNCTION salescall.number_last_dial_elsewhere(text);
CREATE FUNCTION salescall.number_dial_wait_elsewhere(p_phone text, p_same_gap interval, p_other_gap interval)
RETURNS TABLE (relation text, wait_until timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT x.relation, CASE WHEN x.relation = 'same_tenant' THEN x.until
                          ELSE date_trunc('hour', x.until) + interval '1 hour' END
  FROM (SELECT CASE WHEN a.tenant_id = salescall.cur_tenant() THEN 'same_tenant' ELSE 'other_tenant' END AS relation,
               max(a.claimed_at + CASE WHEN a.tenant_id = salescall.cur_tenant() THEN p_same_gap ELSE p_other_gap END) AS until
        FROM salescall.attempts a
        WHERE a.phone_e164 = p_phone AND a.client_id IS DISTINCT FROM salescall.cur_client()
          AND salescall.is_placed(a.state)
        GROUP BY 1) x
  WHERE x.until > now()
$$;
REVOKE ALL ON FUNCTION salescall.number_dial_wait_elsewhere(text, interval, interval) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.number_dial_wait_elsewhere(text, interval, interval) TO salescall_app;

-- ═════════ (7) 判定できない結果が、まだ決まっていないか ═════════
-- その試行のいちばん新しい結果の版が「判定できない」か（結果の版が無い試行＝お詫びの電話は false）
CREATE FUNCTION salescall.result_unknown(p_attempt uuid) RETURNS boolean
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT coalesce((SELECT r.business_state = 'unknown' FROM salescall.task_results r WHERE r.attempt_id = p_attempt
                   ORDER BY r.version_no DESC LIMIT 1), false)
$$;
CREATE FUNCTION salescall.unknown_pending(p_attempt uuid) RETURNS boolean
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT salescall.result_unknown(p_attempt)
     AND NOT EXISTS (SELECT 1 FROM salescall.attempts a JOIN salescall.attempts b ON b.task_id = a.task_id
                     WHERE a.attempt_id = p_attempt AND b.claimed_at > a.claimed_at
                       AND salescall.reached_answer(b.attempt_id) AND salescall.is_settled(b.attempt_id)
                       AND NOT salescall.result_unknown(b.attempt_id))
$$;
GRANT EXECUTE ON FUNCTION salescall.result_unknown(uuid) TO salescall_app;
GRANT EXECUTE ON FUNCTION salescall.unknown_pending(uuid) TO salescall_app;

CREATE OR REPLACE FUNCTION salescall.refusal_undecided(p_attempt uuid) RETURNS boolean
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT NOT salescall.is_settled(p_attempt) OR salescall.unknown_pending(p_attempt)
$$;

-- ═════════ (8) ═════════
CREATE FUNCTION salescall.number_live(p_phone text) RETURNS boolean
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
BEGIN
  PERFORM salescall.server_only();
  RETURN EXISTS (SELECT 1 FROM salescall.attempts a
                 WHERE a.phone_e164 = p_phone AND salescall.is_live(a.state, a.claim_expires_at));
END $$;
REVOKE ALL ON FUNCTION salescall.number_live(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.number_live(text) TO salescall_app;

-- ═════════ (9) ═════════
DROP VIEW salescall.current_next_actions;
ALTER TABLE salescall.task_next_actions DROP CONSTRAINT task_next_actions_apology_reasons;
ALTER TABLE salescall.task_next_actions DROP COLUMN apology_owed;
ALTER TABLE salescall.task_next_actions
  ADD COLUMN apology_owed boolean GENERATED ALWAYS AS (cardinality(apology_reasons) > 0) STORED,
  ADD CONSTRAINT task_next_actions_apology_reasons CHECK (apology_reasons <@ ARRAY['missed_promise', 'cut_off']::text[]);
CREATE VIEW salescall.current_next_actions WITH (security_invoker = true) AS
  SELECT n.* FROM salescall.task_next_actions n
  WHERE n.seq = (SELECT max(m.seq) FROM salescall.task_next_actions m WHERE m.task_id = n.task_id);
GRANT SELECT ON salescall.current_next_actions TO salescall_app;
