-- 0155 AIの不具合での一時停止と、契約者以上の緊急の道での再開（2026-10-30 深夜 管理者のご決定。書いた人：CL-2。CL-1 の連絡帳 4-62。
-- 計画書 10-3・DESIGN.md 9-1）
--
-- ご決定：「基本的にアンソロピックが使えなくなったりその他に使う AI のどれかが止まったら、AI の不具合として回復するまで一時停止して
-- ください。ただし、どうしても使わなければいけないシーンが出たときは、契約者以上の権限でOpenRouter を通して Google が預かる Claudeを
-- 使って再開することができるようにしておいてください。」
--
-- 止める原因は今の ai_connection（範囲＝その契約者。会話AI・判定のキーと枠は契約者ごと＝2026-10-16）を使い、意味を「AIの不具合」に
-- 広げる。止めるか・緊急の道で補えるかは処置の表（core/errors.ACTIONS）の1か所、止めるのは core/watch.decide_stop の1か所。
--
-- (1) 通話で止まったAIの事実（表 attempt_ai_troubles）：会話の報告の ai_trouble（CL-1 の Flow.on_ai_trouble の1か所。データの形 版 1.08）と、
--     通話の途中の知らせ（通話ごとの口 call.ai_trouble）が、同じ1つの入口（core/ai_troubles.note）で書く。同じAI・同じ道・同じ出来事は
--     1つ（2回目は書かない＝止める判断を二重にしない）。システムの処理だけが書く。書き換えない。
-- (2) 続いたエラーの数え方に「解いた時からだけ」（none）を足す：AIが止まった（ai_trouble）は1回で止めるので、どこかの通話が会話に
--     入ったこと（in_conversation）では数え直さない（⚠️ 数え直すと、止まったAIを使う通話と同じ時刻にほかの通話が会話に入っただけで、
--     止まらなくなる）。数え直すのは、同じ原因の停止が解かれた時刻だけ。
-- (3) 緊急の道での再開（表 stop_emergency_passes。停止ごとに1つ）：契約者か管理者だけが、理由を書いて出せる。出せるのは、まだ解かれて
--     いない、システムが止めた、その契約者の AIの不具合の停止だけ（補えないAI＝会話AI・緊急の道そのものが止まっていないかは、処置の
--     表を読む core/stops.emergency_possible の1か所が決める）。書き換えない。
-- (4) 出ている停止では止めない（salescall.blocking_stops の1か所＝門・候補を出す処理・約束の一覧・止めたときの予約の取り消しが読む）。
--     停止が解ければ（確かめが通った・契約者が解いた）、再開も一緒に終わる。
-- (5) 試行の判定の道（attempts.judge_route）：予約のとき、その契約者に緊急の道での再開が出ていれば 'emergency'（データベースが決める
--     1か所＝salescall.emergency_route。門は選べない）。会話の処理は、予約証の judge_route を run_call へ渡す。変えられない。

-- ═════════ (1) 通話で止まったAIの事実 ═════════
CREATE TABLE salescall.attempt_ai_troubles (
  tenant_id   uuid NOT NULL,
  client_id   uuid NOT NULL,
  attempt_id  uuid NOT NULL REFERENCES salescall.attempts (attempt_id),
  ai          text NOT NULL CHECK (length(ai) BETWEEN 1 AND 100),           -- AIの名前（CL-1 の名前＝check_ais と同じ。「会話AI」「別の耳（…）」・判定の名前）
  route       text NOT NULL CHECK (length(route) BETWEEN 1 AND 100),        -- 「直接」か、緊急の道の名前
  what        text NOT NULL CHECK (what IN ('止まった', '緊急の道で答えた')),
  error       text CHECK (length(error) <= 300),                            -- 最初の失敗の中身（お客様の言葉・名前・キーの値は入れない）
  turn        integer CHECK (turn >= 0),                                    -- 最初に起きたときの、相手の発話の番号
  noted_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (attempt_id, ai, route, what),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id)
);
CREATE TRIGGER attempt_ai_troubles_frozen BEFORE UPDATE OR DELETE ON salescall.attempt_ai_troubles
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
ALTER TABLE salescall.attempt_ai_troubles ENABLE ROW LEVEL SECURITY;
CREATE POLICY client_scope ON salescall.attempt_ai_troubles
  USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
  WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client());
-- 契約者の取引からも読む（停止の説明＝止まったAIの名前と時刻。契約者のすべての依頼者の分）
CREATE POLICY tenant_read ON salescall.attempt_ai_troubles FOR SELECT
  USING (salescall.cur_client() IS NULL AND tenant_id = salescall.cur_tenant());
GRANT SELECT, INSERT ON salescall.attempt_ai_troubles TO salescall_app;

CREATE FUNCTION salescall.attempt_ai_trouble_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF salescall.cur_actor_kind() IS DISTINCT FROM 'system' THEN
    RAISE EXCEPTION '止まったAIを書くのは、システムの処理だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'attempt_ai_trouble_guard';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM salescall.attempts a WHERE a.attempt_id = NEW.attempt_id
                   AND a.tenant_id = NEW.tenant_id AND a.client_id = NEW.client_id) THEN
    RAISE EXCEPTION 'その通話は、この依頼者の通話ではありません' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'attempt_ai_trouble_guard';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER attempt_ai_trouble_guard BEFORE INSERT ON salescall.attempt_ai_troubles
  FOR EACH ROW EXECUTE FUNCTION salescall.attempt_ai_trouble_guard();

-- 出どころ「止まったAI」（ai_trouble）をエラーの記録に足す
ALTER TABLE salescall.error_events DROP CONSTRAINT error_events_source_check;
ALTER TABLE salescall.error_events ADD CONSTRAINT error_events_source_check
  CHECK (source IN ('ai', 'ear', 'line', 'storage', 'delivery', 'recording', 'supervisor', 'judge', 'call', 'runner', 'ai_trouble'));

-- ═════════ (2) 続いたエラーの数え方：解いた時からだけ（none） ═════════
CREATE OR REPLACE FUNCTION salescall.consecutive_errors(p_kinds text[], p_cause text, p_evidence text) RETURNS integer
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  WITH since AS (
    SELECT greatest(
      CASE WHEN p_evidence = 'in_conversation' THEN
        (SELECT coalesce(max(h.at), '-infinity'::timestamptz)
         FROM salescall.attempt_state_history h JOIN salescall.attempts a ON a.attempt_id = h.attempt_id
         WHERE h.to_state = 'in_conversation' AND a.tenant_id = salescall.cur_tenant())
      ELSE '-infinity'::timestamptz END,
      (SELECT coalesce(max(r.at), '-infinity'::timestamptz)
       FROM salescall.stop_events r JOIN salescall.stop_events s ON s.event_id = r.resolves_event_id
       WHERE s.cause::text = p_cause AND s.tenant_id = salescall.cur_tenant() AND s.scope = 'tenant')) AS t
  )
  SELECT CASE WHEN salescall.cur_tenant() IS NULL OR p_evidence IS NULL OR p_evidence NOT IN ('in_conversation', 'none') THEN NULL
         ELSE (SELECT count(*)::integer FROM salescall.error_events e CROSS JOIN since
               WHERE e.kind = ANY(p_kinds) AND e.occurred_at > since.t AND e.tenant_id = salescall.cur_tenant()) END
$$;

-- ═════════ (3) 緊急の道での再開 ═════════
CREATE TABLE salescall.stop_emergency_passes (
  stop_event_id bigint PRIMARY KEY REFERENCES salescall.stop_events (event_id),
  tenant_id     uuid NOT NULL REFERENCES salescall.tenants (tenant_id),
  reason        text NOT NULL CHECK (length(btrim(reason)) BETWEEN 1 AND 500),   -- なぜ、どうしても今かけるのか
  passed_by     text NOT NULL,
  actor_kind    salescall.actor_kind NOT NULL,
  at            timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER stop_emergency_passes_frozen BEFORE UPDATE OR DELETE ON salescall.stop_emergency_passes
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
ALTER TABLE salescall.stop_emergency_passes ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON salescall.stop_emergency_passes
  USING (tenant_id = salescall.cur_tenant())
  WITH CHECK (tenant_id = salescall.cur_tenant());
GRANT SELECT, INSERT ON salescall.stop_emergency_passes TO salescall_app;

CREATE FUNCTION salescall.stop_emergency_pass_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF salescall.cur_actor_kind() NOT IN ('operator', 'admin') OR NEW.passed_by IS DISTINCT FROM salescall.cur_actor()
     OR NEW.actor_kind::text IS DISTINCT FROM salescall.cur_actor_kind()::text THEN
    RAISE EXCEPTION '緊急の道で再開できるのは、契約者か管理者だけです（出した人は取引の人）' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'stop_emergency_pass_guard';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM salescall.open_stop_events s WHERE s.event_id = NEW.stop_event_id AND s.cause = 'ai_connection'
                   AND s.scope = 'tenant' AND s.actor_kind = 'system' AND s.tenant_id = NEW.tenant_id) THEN
    RAISE EXCEPTION '緊急の道で再開できるのは、まだ解かれていない、システムが止めた、その契約者の AIの不具合の停止だけです'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'stop_emergency_pass_guard';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER stop_emergency_pass_guard BEFORE INSERT ON salescall.stop_emergency_passes
  FOR EACH ROW EXECUTE FUNCTION salescall.stop_emergency_pass_guard();

-- ═════════ (4) 出ている停止では止めない ═════════
CREATE OR REPLACE FUNCTION salescall.blocking_stops(p_tenant uuid, p_client uuid, p_business uuid, p_line uuid, p_kind text)
RETURNS SETOF salescall.stop_events LANGUAGE sql STABLE AS $$
  SELECT s.* FROM salescall.open_stops(p_tenant, p_client, p_business, p_line) s
  WHERE NOT salescall.lets_through(s.cause, p_kind)
    AND NOT EXISTS (SELECT 1 FROM salescall.stop_emergency_passes p WHERE p.stop_event_id = s.event_id)
$$;

-- 予約の取り消し（止めたとき）も同じ1か所：出ている停止では取り消さない（出すのは止めたあとなので、止めたときには出ていない。
-- 念のため blocking_stops と同じ決まりを読む）
CREATE OR REPLACE FUNCTION salescall.release_claims_for_stop(p_event_id bigint) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
DECLARE s salescall.stop_events; n integer;
BEGIN
  SELECT * INTO s FROM salescall.open_stop_events e WHERE e.event_id = p_event_id
    AND (salescall.cur_tenant() IS NULL OR e.tenant_id IS NULL OR e.tenant_id = salescall.cur_tenant());
  IF NOT FOUND THEN
    RAISE EXCEPTION 'まだ解かれていない、その停止はありません' USING ERRCODE = 'check_violation';
  END IF;
  IF EXISTS (SELECT 1 FROM salescall.stop_emergency_passes p WHERE p.stop_event_id = p_event_id) THEN
    RETURN 0;
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

-- ═════════ (5) 試行の判定の道 ═════════
-- 取引の契約者に、まだ解かれていない AIの不具合の停止で、緊急の道での再開が出ているものがあるか（1か所。見えるのは取引の契約者の分だけ）
CREATE FUNCTION salescall.emergency_route() RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT EXISTS (SELECT 1 FROM salescall.open_stop_events s JOIN salescall.stop_emergency_passes p ON p.stop_event_id = s.event_id
                 WHERE s.cause = 'ai_connection' AND s.scope = 'tenant' AND s.tenant_id = salescall.cur_tenant())
$$;
REVOKE ALL ON FUNCTION salescall.emergency_route() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.emergency_route() TO salescall_app;

ALTER TABLE salescall.attempts ADD COLUMN judge_route text CHECK (judge_route IN ('emergency'));

CREATE FUNCTION salescall.attempt_judge_route() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.judge_route := CASE WHEN NEW.tenant_id = salescall.cur_tenant() AND salescall.emergency_route() THEN 'emergency' END;
  RETURN NEW;
END $$;
CREATE TRIGGER attempt_judge_route BEFORE INSERT ON salescall.attempts
  FOR EACH ROW EXECUTE FUNCTION salescall.attempt_judge_route();

CREATE OR REPLACE FUNCTION salescall.attempt_record_fixed() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (NEW.promised, NEW.voice_type, NEW.judge_route) IS DISTINCT FROM (OLD.promised, OLD.voice_type, OLD.judge_route) THEN
    RAISE EXCEPTION '試行の、約束したかけ直しか・声の型・判定の道は変えられません' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;

-- ═════════ (6) 約束の電話を受け持てる回線の数えも、同じ1か所 ═════════
-- 回線の空き（salescall.lines_serving。約束の電話をどの回線が受け持つか）は、止まっているかを open_stops（まだ解かれていない停止の
-- すべて）で読んでいた。緊急の道で再開したあいだも、その契約者のどの回線も「止まっている」と数え、約束の電話の受け持ちが狂う。
-- 門・候補を出す処理・約束の一覧と同じ1か所（salescall.blocking_stops。種類は「新しい電話」＝NULL）で読む。
CREATE OR REPLACE FUNCTION salescall.lines_serving()
RETURNS TABLE (line_id uuid, client_id uuid, idle boolean)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT o.line_id, o.client_id,
         NOT EXISTS (SELECT 1 FROM salescall.attempts a WHERE a.line_id = o.line_id AND salescall.is_live(a.state, a.claim_expires_at))
  FROM salescall.lines o
  WHERE o.tenant_id = salescall.cur_tenant() AND o.retired_at IS NULL AND o.lease_expires_at > now()
    AND NOT EXISTS (SELECT 1 FROM salescall.blocking_stops(o.tenant_id, NULL, NULL, o.line_id, NULL))
    AND NOT EXISTS (SELECT 1 FROM salescall.line_states s WHERE s.line_id = o.line_id
                    AND s.stage IN ('line_rest', 'audio_rest', 'out_of_use'))
$$;
