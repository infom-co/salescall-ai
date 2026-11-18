-- 0014 品質の段（Q22）と模擬通話の合格（DESIGN.md 10-1。2026-10-14 ユーザー承認）。書いた人：CL-2
-- - 通話後の検証の結果の数（verification_summaries）：検証を「済み」にするとき、確かめた項目の数・2つの耳が食い違った項目の数・
--   言ってはいけないことの数（2つの耳で確かめたもの）を必ず一緒に残す（無ければ「済み」にできない。データベースが守る）。
--   書くのは core/attempts.py の record_verification だけ（検証 Q21＝CL-1 が呼ぶ）。品質の段の指標はここから数える。
-- - 模擬通話の結果（mock_runs）：流した本数・正解と一致した値と判定の数・言ってはいけないことの数・合否と基準。書くのは
--   core/quality_stage.py だけ（流すのは CL-1。合否を決めるのは CL-2 の1か所）。
-- - 品質の段の評価（quality_evaluations）：評価のたびに、指標ごとの値（何件中何件）・基準・内側か外か・結果（続ける／次の段へ／止める）。
--   画面はこれを読んで出す（割合を計算し直さない。CL-3 の 4-1e の2）。止めたときは、どの版で止めたか（その版では再開しない）。
CREATE TABLE salescall.verification_summaries (
  tenant_id        uuid NOT NULL,
  client_id        uuid NOT NULL,
  attempt_id       uuid PRIMARY KEY,
  items_checked    integer NOT NULL CHECK (items_checked >= 0),
  items_disagreed  integer NOT NULL CHECK (items_disagreed >= 0),
  forbidden_speech integer NOT NULL CHECK (forbidden_speech >= 0),
  recorded_at      timestamptz NOT NULL DEFAULT now(),
  CHECK (items_disagreed <= items_checked),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, attempt_id) REFERENCES salescall.attempts (client_id, attempt_id)
);

-- 検証を「済み」にするのは、結果の数を残してから（「確かめられなかった」は数を持たない）
CREATE FUNCTION salescall.attempt_verification_needs_summary() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.verification = 'done' AND OLD.verification IS DISTINCT FROM 'done'
     AND NOT EXISTS (SELECT 1 FROM salescall.verification_summaries v WHERE v.attempt_id = NEW.attempt_id) THEN
    RAISE EXCEPTION '通話後の検証を済ませる前に、検証の結果の数を残してください' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'attempts_verification_needs_summary';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER attempt_verification_needs_summary BEFORE UPDATE ON salescall.attempts
  FOR EACH ROW EXECUTE FUNCTION salescall.attempt_verification_needs_summary();

CREATE TABLE salescall.mock_runs (
  tenant_id        uuid NOT NULL,
  client_id        uuid NOT NULL,
  mock_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  version_id       uuid NOT NULL,
  calls            integer NOT NULL CHECK (calls >= 1),
  value_hits       integer NOT NULL CHECK (value_hits >= 0),
  value_total      integer NOT NULL CHECK (value_total >= 1),
  judgment_hits    integer NOT NULL CHECK (judgment_hits >= 0),
  judgment_total   integer NOT NULL CHECK (judgment_total >= 1),
  forbidden_speech integer NOT NULL CHECK (forbidden_speech >= 0),
  passed           boolean NOT NULL,
  thresholds       jsonb NOT NULL,                  -- 決めたときの基準（本数・一致の下限）
  evidence         text NOT NULL CHECK (btrim(evidence) <> ''),
  recorded_at      timestamptz NOT NULL DEFAULT now(),
  recorded_by      text NOT NULL CHECK (btrim(recorded_by) <> ''),
  CHECK (value_hits <= value_total AND judgment_hits <= judgment_total AND judgment_total = calls),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, version_id) REFERENCES salescall.business_versions (client_id, version_id)
);

CREATE TABLE salescall.quality_evaluations (
  tenant_id     uuid NOT NULL,
  client_id     uuid NOT NULL,
  eval_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  version_id    uuid NOT NULL,
  stage_no      integer NOT NULL CHECK (stage_no >= 1),
  cap           integer,
  used          integer NOT NULL CHECK (used >= 0),
  metrics       jsonb NOT NULL,                     -- 指標ごと：{hits, total, value, limit, inside}
  outcome       text NOT NULL CHECK (outcome IN ('continue', 'advance', 'stop')),
  stop_event_id bigint REFERENCES salescall.stop_events,   -- 止めたとき：その停止（この版では再開しない）
  evaluated_at  timestamptz NOT NULL DEFAULT now(),
  CHECK ((outcome = 'stop') = (stop_event_id IS NOT NULL)),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, version_id) REFERENCES salescall.business_versions (client_id, version_id)
);
CREATE INDEX quality_evaluations_by_version ON salescall.quality_evaluations (version_id, eval_id DESC);

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['verification_summaries', 'mock_runs', 'quality_evaluations'] LOOP
    EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON salescall.%I
                      FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change()', t || '_frozen', t);
    EXECUTE format('ALTER TABLE salescall.%I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY client_scope ON salescall.%I
                      USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
                      WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())', t);
  END LOOP;
END $$;
GRANT SELECT, INSERT ON salescall.verification_summaries, salescall.mock_runs, salescall.quality_evaluations TO salescall_app;
