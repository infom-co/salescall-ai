-- 0130 CL-1 の 10-24 夕の頼み（CL-1 の連絡帳 4-34・4-41。書いた人：CL-2）
--
-- (1) 番号を伺い直す電話の理由（number_again）：予約証の目的が「番号の聞き直し」（ask_number_again）のとき、なぜ伺い直すのかを
--     次の予定に持たせ、門が予約証へ写す（今の successor・apology_reasons と同じ道）。会話（CL-1）は、理由ごとの一言で番号を伺う
--     （2026-10-24 ユーザー：「OK、ただし、別の方のお電話よりも、繋がらない番号のパターンも必要じゃないですか？」）。
--       wrong_party＝お客様が言った番号にかけたら、別の方のお電話だった（言われたとおり・確かめられない番号違い）
--       unreachable＝お客様が言った番号に繋がらなかった（お客様のところで鳴らない電話が続き、番号のせいと決まった＝salescall.unrung_blame）
--     目的が聞き直しの予定には必ず理由がある・ほかの目的の予定には無い（データベースが守る。前からの記録は確かめない＝NOT VALID）。
--     決めるのは core/next_action の choose_number の1か所。
-- (2) 試行に、使ったキーの識別名（keys_used）：[{"service": "live"|"judge"|"ear"|"own_check", "key": "名前#指紋8桁"}]。値は残さない
--     （名前は英大文字・数字・下線、指紋はキーの値の sha256 の頭8桁＝キーの値そのものは入らない形をデータベースが守る。CL-1 の 4-67）。
--     書くのは書き込む入口（core/conversation_io.record_keys → core/attempts）。
-- (3) 試行の「最後に進んだ時刻」（progress_at）：通話を進める部品（CL-1）が通話の途中で知らせる（core/conversation_io.progress）。
--     発信を始めたとき（予約 → 発信中）に、データベースが今の時刻を入れる。進行中の試行でだけ、前へだけ進む。待つ・見張る所
--     （止めるとき・通行証が止められたあと・通話を進める部品が止まった）は、時間の上限ではなく「決まった長さ進まない」で決める
--     （2026-10-24 ユーザー：時間で業務を打ち切らない。CL-1 の 4-41 の (1)(2)）。

-- ═════════ (1) ═════════
ALTER TABLE salescall.task_next_actions ADD COLUMN number_again text
  CHECK (number_again IS NULL OR number_again IN ('wrong_party', 'unreachable'));
ALTER TABLE salescall.task_next_actions ADD CONSTRAINT task_next_actions_number_again_reason
  CHECK ((purpose IS NOT DISTINCT FROM 'ask_number_again') = (number_again IS NOT NULL)) NOT VALID;
DROP VIEW salescall.current_next_actions;   -- 列を足したので作り直す（n.* は作ったときの列だけ）
CREATE VIEW salescall.current_next_actions WITH (security_invoker = true) AS
  SELECT n.* FROM salescall.task_next_actions n
  WHERE n.seq = (SELECT max(m.seq) FROM salescall.task_next_actions m WHERE m.task_id = n.task_id);
GRANT SELECT ON salescall.current_next_actions TO salescall_app;

-- ═════════ (2) ═════════
CREATE FUNCTION salescall.keys_used_ok(p jsonb) RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
  SELECT jsonb_typeof(p) = 'array'
     AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements(p) e(k)
                     WHERE jsonb_typeof(e.k) <> 'object'
                        OR (SELECT count(*) FROM jsonb_object_keys(e.k)) <> 2
                        OR e.k->>'service' IS NULL OR e.k->>'service' NOT IN ('live', 'judge', 'ear', 'own_check')
                        OR e.k->>'key' IS NULL OR e.k->>'key' !~ '^[A-Z][A-Z0-9_]{0,63}#[0-9a-f]{8}$')
$$;
ALTER TABLE salescall.attempts ADD COLUMN keys_used jsonb CHECK (keys_used IS NULL OR salescall.keys_used_ok(keys_used));

-- ═════════ (3) ═════════
ALTER TABLE salescall.attempts ADD COLUMN progress_at timestamptz;
CREATE FUNCTION salescall.attempt_progress() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.state = 'claimed' AND NEW.state = 'dialing' THEN
    NEW.progress_at := now();                 -- 発信を始めた時刻から数える
  ELSIF NEW.progress_at IS DISTINCT FROM OLD.progress_at THEN
    IF NOT salescall.is_active(OLD.state) OR NEW.progress_at IS NULL
       OR (OLD.progress_at IS NOT NULL AND NEW.progress_at < OLD.progress_at) OR NEW.progress_at > now() THEN
      RAISE EXCEPTION '通話の進んだ時刻は、進行中の試行でだけ、前へだけ進みます' USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER attempt_progress BEFORE UPDATE ON salescall.attempts
  FOR EACH ROW EXECUTE FUNCTION salescall.attempt_progress();

-- 進行中の試行が、決まった長さ（秒）より長く進んでいないか（こちらの仕組みが止まった。判断はここだけ＝core/attempts.stalled・
-- 止めるときの待ち・通行証が止められたあとの待ち・回線ごとの見張りが読む）。進行中でなければ偽。
CREATE FUNCTION salescall.call_stalled(p_attempt uuid, p_gap_seconds double precision) RETURNS boolean
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT coalesce((SELECT salescall.is_active(a.state) AND a.state <> 'claimed'
                          AND coalesce(a.progress_at, a.claimed_at) < now() - p_gap_seconds * interval '1 second'
                   FROM salescall.attempts a WHERE a.attempt_id = p_attempt), false)
$$;
GRANT EXECUTE ON FUNCTION salescall.call_stalled(uuid, double precision) TO salescall_app;

-- その回線に、進んでいる通話（進行中で、決まった長さのうちに進んだ）があるか（止めるとき・通行証が止められたあとに待つかの1か所）。
-- 回線の上のほかの依頼者の試行も見るので、データベースの持ち主の権限で読む（返すのは真偽だけ）。
CREATE FUNCTION salescall.line_call_progressing(p_line uuid, p_gap_seconds double precision) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT EXISTS (SELECT 1 FROM salescall.attempts a
                 WHERE a.line_id = p_line AND a.tenant_id = salescall.cur_tenant()
                   AND salescall.is_active(a.state) AND a.state <> 'claimed'
                   AND coalesce(a.progress_at, a.claimed_at) >= now() - p_gap_seconds * interval '1 second')
$$;
REVOKE ALL ON FUNCTION salescall.line_call_progressing(uuid, double precision) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.line_call_progressing(uuid, double precision) TO salescall_app;
