-- 0021 「判定できない」となった行の扱い（2026-10-16 ユーザー決定：前の案の (a)(b)(d)）。書いた人：CL-2
-- 「判定できない」＝通話は終わったのに、結果を決められない（原因は相手ではなく、こちらの誤り：会話の報告の書き漏れ・業務設定の抜け・
-- 判定の処理の誤り）。
-- (a) その行は終えずに残す：次の動きに「判定待ち」（hold）を足す。門は、判定待ちの行にかけない（judge_pending）。
--     原因が直ったら（判定の処理の版・業務設定の版・作り直した会話の報告のどれかが変わったら）、判定し直す（結果の版 rejudged）。
-- (b) 1件では止めない。同じ原因が2件になったら、その業務を止める（原因 judge_unknown。数えるのは品質の段の1か所）。
-- (d) 原因を直して、その場面を入れた模擬通話に合格したら、自動で再開する（同じ版でもよい：原因は業務設定とは限らないため）。
--     管理者の判断で再開することもできる（9-1）。
ALTER TYPE salescall.next_kind ADD VALUE 'hold';             -- 判定待ち（かけない・終えない）
ALTER TYPE salescall.stop_cause ADD VALUE 'judge_unknown';   -- 同じ原因の「判定できない」が続いた

-- 会話の報告の作り直し（通話の録音から作り直した報告。作るのは会話の担当 CL-1。入口は core/call_report.py の1か所）。
-- 元の報告は変えない（変更不可）。読むときは、いちばん新しい作り直しがあればそれを使う。
CREATE TABLE salescall.call_report_rebuilds (
  tenant_id   uuid NOT NULL,
  client_id   uuid NOT NULL,
  rebuild_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  attempt_id  uuid NOT NULL,
  report      jsonb NOT NULL,
  evidence    text NOT NULL CHECK (btrim(evidence) <> ''),   -- 何から作り直したか（録音の場所・別の耳の版 など。相手の発話は入れない）
  rebuilt_by  text NOT NULL CHECK (btrim(rebuilt_by) <> ''),
  rebuilt_at  timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, attempt_id) REFERENCES salescall.attempts (client_id, attempt_id)
);
CREATE INDEX call_report_rebuilds_by_attempt ON salescall.call_report_rebuilds (attempt_id, rebuild_id DESC);
CREATE TRIGGER call_report_rebuilds_frozen BEFORE UPDATE OR DELETE ON salescall.call_report_rebuilds
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
ALTER TABLE salescall.call_report_rebuilds ENABLE ROW LEVEL SECURITY;
CREATE POLICY client_scope ON salescall.call_report_rebuilds
  USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
  WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client());
GRANT SELECT, INSERT ON salescall.call_report_rebuilds TO salescall_app;

-- 模擬通話に入れた場面（実際の通話の試行。その場面を相手役の台本にして流した）。(d) の「その場面を入れた模擬通話」を確かめる
ALTER TABLE salescall.mock_runs ADD COLUMN scenes uuid[] NOT NULL DEFAULT '{}';
