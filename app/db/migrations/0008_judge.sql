-- 0008 業務の判定と次の動き（DESIGN.md 7-1・7-1c・7-2・Q4・Q5）。書いた人：CL-2（2026-10-14）
-- - 会話から返る報告（CL-1 の連絡帳 2-2 の仮の形）を、試行ごとに1つ置く（書くのは core/call_report.py だけ。変更不可）。
-- - 業務の決まり（断りの答え・理由×扱い・再発信の方針）を、業務設定の版ごとに1つ置く（書くのは core/business_rules.py だけ。
--   変更不可。必須の質問は質問の表のまま）。中身の形は core/business_rules.py の1か所で決め、書くときも読むときも同じ確かめを通す。
-- - 項目の「わからない」「答えない」の理由（15-2-3）を、項目の値に持つ。

ALTER TABLE salescall.item_values
  ADD COLUMN reason text CHECK (reason IS NULL OR reason ~ '^[a-z_]+$'),
  ADD CONSTRAINT item_values_reason_only_when_not_answered CHECK (reason IS NULL OR state IN ('dont_know', 'refused'));

CREATE TABLE salescall.call_reports (       -- 会話から返る報告（試行ごとに1つ。変更不可）
  tenant_id  uuid NOT NULL,
  client_id  uuid NOT NULL,
  attempt_id uuid PRIMARY KEY,
  report     jsonb NOT NULL,                 -- 形は core/call_report.py（読むときも書くときも同じ確かめ）
  saved_at   timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, attempt_id) REFERENCES salescall.attempts (client_id, attempt_id)
);

CREATE TABLE salescall.business_rules (     -- 業務の決まり（業務設定の版ごとに1つ。変更不可）
  tenant_id  uuid NOT NULL,
  client_id  uuid NOT NULL,
  version_id uuid PRIMARY KEY,
  rules      jsonb NOT NULL,                 -- 形は core/business_rules.py（読むときも書くときも同じ確かめ）
  saved_at   timestamptz NOT NULL DEFAULT now(),
  saved_by   text NOT NULL CHECK (btrim(saved_by) <> ''),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, version_id) REFERENCES salescall.business_versions (client_id, version_id)
);

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['call_reports', 'business_rules'] LOOP
    EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON salescall.%I
                      FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change()', t || '_frozen', t);
    EXECUTE format('ALTER TABLE salescall.%I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY client_scope ON salescall.%I
                      USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
                      WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())', t);
  END LOOP;
END $$;
GRANT SELECT, INSERT ON salescall.call_reports, salescall.business_rules TO salescall_app;

-- 判定の結果の版は、1つの試行につき1つ（判定し直しは kind = rejudged で、別の取り扱い。いまは作らない）
CREATE UNIQUE INDEX task_results_one_judgement_per_attempt ON salescall.task_results (attempt_id) WHERE kind = 'judged';
