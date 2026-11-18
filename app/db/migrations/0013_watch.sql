-- 0013 見守り：エラーの記録と、停止を自動で解く確かめの記録（DESIGN.md 9章・9-1。Q9・Q13）。書いた人：CL-2
-- - エラーの記録（error_events）：起きたエラーと、分類した種類（core/errors.py の1か所）。書くのは core/watch.py だけ。変更不可。
--   「何回続いたか」は、この記録と試行の履歴から数える（数えるのは salescall.consecutive_errors の1か所）：
--   回線＝その回線で、相手を呼び出せた（ringing）発信より後のエラー／会話AI・別の耳＝どこかの通話が会話に入った（in_conversation）
--   時刻より後のエラー。⇒ 別に「続けた回数」の欄を持たない（同じことを2か所に持たない）。
-- - 停止を解く確かめの記録（stop_checks）：時刻・結果・続けて何回成功したか・次に確かめてよい時刻（ばたつきで伸びた待ちを含む）。
--   CL-3 の 4-1e の1。書くのは core/watch.py だけ。変更不可。
-- - どちらも、相手の電話番号・名前・発話の中身を入れない（エラーの文面と ID だけ）。
CREATE TABLE salescall.error_events (
  error_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id   uuid REFERENCES salescall.tenants,             -- NULL＝全体の取引で起きた（試しの確かめなど）
  client_id   uuid,
  line_id     uuid,
  attempt_id  uuid,
  source      text NOT NULL CHECK (source IN ('ai', 'ear', 'line', 'storage', 'delivery')),
  kind        text NOT NULL CHECK (kind ~ '^[a-z_]+$'),       -- 種類（一覧の正本は core/errors.py の ACTIONS）
  code        integer,
  message     text CHECK (message IS NULL OR btrim(message) <> ''),
  occurred_at timestamptz NOT NULL DEFAULT now(),
  CHECK (client_id IS NULL OR tenant_id IS NOT NULL),
  CHECK (line_id IS NULL OR tenant_id IS NOT NULL),
  CHECK (attempt_id IS NULL OR client_id IS NOT NULL),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (tenant_id, line_id) REFERENCES salescall.lines (tenant_id, line_id),
  FOREIGN KEY (client_id, attempt_id) REFERENCES salescall.attempts (client_id, attempt_id)
);
CREATE INDEX error_events_by_source ON salescall.error_events (source, occurred_at);
CREATE INDEX error_events_by_line ON salescall.error_events (line_id, occurred_at);

CREATE TABLE salescall.stop_checks (
  check_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  stop_event_id bigint NOT NULL REFERENCES salescall.stop_events,
  ok            boolean NOT NULL,
  streak        integer NOT NULL CHECK (streak >= 0),           -- 続けて何回成功したか（失敗なら 0）
  checked_at    timestamptz NOT NULL DEFAULT now(),
  next_check_at timestamptz NOT NULL,                           -- 次に確かめてよい時刻（これより前の確かめは受け付けない）
  evidence      text NOT NULL CHECK (btrim(evidence) <> ''),
  CHECK (ok OR streak = 0),
  CHECK (next_check_at > checked_at)
);
CREATE INDEX stop_checks_by_stop ON salescall.stop_checks (stop_event_id, check_id DESC);

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['error_events', 'stop_checks'] LOOP
    EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON salescall.%I
                      FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change()', t || '_frozen', t);
    EXECUTE format('ALTER TABLE salescall.%I ENABLE ROW LEVEL SECURITY', t);
  END LOOP;
END $$;
CREATE POLICY error_scope ON salescall.error_events
  USING ((tenant_id IS NULL AND salescall.cur_tenant() IS NULL)
         OR (tenant_id = salescall.cur_tenant()
             AND (client_id IS NULL OR salescall.cur_client() IS NULL OR client_id = salescall.cur_client())))
  WITH CHECK ((tenant_id IS NULL AND salescall.cur_tenant() IS NULL)
              OR (tenant_id = salescall.cur_tenant() AND client_id IS NOT DISTINCT FROM salescall.cur_client()));
-- 確かめの記録は、その停止が見える取引でだけ読める・書ける（停止の見える範囲に従う）
CREATE POLICY check_scope ON salescall.stop_checks
  USING (EXISTS (SELECT 1 FROM salescall.stop_events s WHERE s.event_id = stop_checks.stop_event_id))
  WITH CHECK (EXISTS (SELECT 1 FROM salescall.stop_events s WHERE s.event_id = stop_checks.stop_event_id));
GRANT SELECT, INSERT ON salescall.error_events, salescall.stop_checks TO salescall_app;

-- 何回続いたか（1か所）。依頼主の範囲の外のエラー・通話も数える（会話AI・回線はみんなで使うもの）ので、表の持ち主の権限で数え、
-- 数だけを返す（中身は返さない）。p_line を渡すと、その回線のエラーを、その回線で相手を呼び出せた発信より後から数える。
-- 渡さなければ、その出どころのエラーを、どこかの通話が会話に入った時刻より後から数える。
CREATE FUNCTION salescall.consecutive_errors(p_kinds text[], p_line uuid DEFAULT NULL) RETURNS integer
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  WITH since AS (
    SELECT coalesce(max(h.at), '-infinity'::timestamptz) AS t
    FROM salescall.attempt_state_history h JOIN salescall.attempts a ON a.attempt_id = h.attempt_id
    WHERE CASE WHEN p_line IS NULL THEN h.to_state = 'in_conversation'
               ELSE a.line_id = p_line AND h.to_state = 'ringing' END
  )
  SELECT count(*)::integer FROM salescall.error_events e, since
  WHERE e.kind = ANY(p_kinds) AND e.occurred_at > since.t AND (p_line IS NULL OR e.line_id = p_line)
$$;
REVOKE ALL ON FUNCTION salescall.consecutive_errors(text[], uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.consecutive_errors(text[], uuid) TO salescall_app;
