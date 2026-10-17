-- 0048 修正依頼（仕組みの不具合は、止めずに担当へ修正依頼を出す。人は確かめ・承認だけ。2026-10-17。書いた人：CL-2）
--
-- ユーザー（2026-10-17）：「また止めて人に介在してもらってるじゃないか。何度も言わせるなよ。止めずに CL1 か CL2 に修正依頼を
-- 出さないといけないだろ。人はそのプロセスをチェックもしくは承認するだけだよ」（10-16 にも「理由を記載とあなたへの修正依頼をして、
-- 次の行に進んだらいい」）。
-- ⇒ 前は「管理者に直しを依頼する」として、管理者あての知らせを出していた（直すのが人になっていた）。
--   これからは、不具合ごとに直す担当（CL-1＝会話・別の耳・録音／CL-2＝業務の土台・保存・回線の部品／CL-3＝画面と出力）へ修正依頼を
--   出す（この表）。担当が直したら「直した」を書き、管理者は、それを確かめて承認するだけ。
--   どの不具合をどの担当が直すかは、処理の1か所（エラーの表 core/errors.py の fixer・判定できない原因 judge.UNKNOWN_CAUSE_FIXERS）。
--   修正依頼には、お客様の電話番号・名前・発話の中身を入れない（何の不具合かと、記録の ID だけ）。

CREATE TABLE salescall.fix_requests (
  request_id   bigserial PRIMARY KEY,
  assignee     text NOT NULL CHECK (assignee IN ('CL-1', 'CL-2', 'CL-3')),
  kind         text NOT NULL CHECK (kind ~ '^[a-z_]+$'),     -- 何の不具合か（エラーの種類・判定できない原因の名前）
  event_key    text NOT NULL UNIQUE,                        -- 同じ不具合の同じ出来事は1つ（呼ぶ側が決める）
  summary      text NOT NULL CHECK (btrim(summary) <> ''),
  refs         jsonb NOT NULL DEFAULT '{}'::jsonb,          -- 記録の ID（どの契約者・依頼者の出来事かも、ここ）
  requested_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE salescall.fix_reports (       -- 担当が直した（何を直したか）
  request_id  bigint PRIMARY KEY REFERENCES salescall.fix_requests (request_id),
  note        text NOT NULL CHECK (btrim(note) <> ''),
  reported_by text NOT NULL CHECK (btrim(reported_by) <> ''),
  reported_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE salescall.fix_approvals (     -- 管理者が確かめて承認した
  request_id  bigint PRIMARY KEY REFERENCES salescall.fix_reports (request_id),
  approved_by text NOT NULL CHECK (btrim(approved_by) <> ''),
  approved_at timestamptz NOT NULL DEFAULT now()
);

-- 入れるのは、この関数だけ（契約者・依頼者の取引の中からも入れられるように。どの契約者・依頼者の出来事かは refs に足す）
CREATE FUNCTION salescall.request_fix(p_assignee text, p_kind text, p_key text, p_summary text, p_refs jsonb) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
DECLARE got bigint;
BEGIN
  INSERT INTO salescall.fix_requests (assignee, kind, event_key, summary, refs)
  VALUES (p_assignee, p_kind, p_key, p_summary,
          coalesce(p_refs, '{}'::jsonb) || jsonb_build_object('tenant_id', salescall.cur_tenant(), 'client_id', salescall.cur_client()))
  ON CONFLICT (event_key) DO NOTHING RETURNING request_id INTO got;
  RETURN got IS NOT NULL;
END $$;
REVOKE ALL ON FUNCTION salescall.request_fix(text, text, text, text, jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.request_fix(text, text, text, text, jsonb) TO salescall_app;

-- 「直した」を書くのはシステムの処理（担当の作業）、承認は管理者だけ（人）。どれも変えられない
CREATE FUNCTION salescall.fix_report_by_system() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF salescall.cur_actor_kind() IS DISTINCT FROM 'system' OR NEW.reported_by IS DISTINCT FROM salescall.cur_actor() THEN
    RAISE EXCEPTION '「直した」を書くのは、担当の作業（システムの処理）だけです' USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER fix_report_by_system BEFORE INSERT ON salescall.fix_reports
  FOR EACH ROW EXECUTE FUNCTION salescall.fix_report_by_system();
CREATE FUNCTION salescall.fix_approval_by_admin() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF salescall.cur_actor_kind() IS DISTINCT FROM 'admin' OR NEW.approved_by IS DISTINCT FROM salescall.cur_actor() THEN
    RAISE EXCEPTION '修正を承認できるのは管理者だけです' USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER fix_approval_by_admin BEFORE INSERT ON salescall.fix_approvals
  FOR EACH ROW EXECUTE FUNCTION salescall.fix_approval_by_admin();
CREATE TRIGGER fix_requests_frozen BEFORE UPDATE OR DELETE ON salescall.fix_requests
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
CREATE TRIGGER fix_reports_frozen BEFORE UPDATE OR DELETE ON salescall.fix_reports
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
CREATE TRIGGER fix_approvals_frozen BEFORE UPDATE OR DELETE ON salescall.fix_approvals
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();

-- 読めるのは、管理者と担当の作業（システムの処理）だけ（契約者には、ほかの契約者の不具合を見せない）
ALTER TABLE salescall.fix_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE salescall.fix_reports ENABLE ROW LEVEL SECURITY;
ALTER TABLE salescall.fix_approvals ENABLE ROW LEVEL SECURITY;
CREATE POLICY fix_scope ON salescall.fix_requests
  USING (salescall.cur_tenant() IS NULL AND salescall.cur_actor_kind() IN ('admin', 'system'));
CREATE POLICY fix_scope ON salescall.fix_reports
  USING (salescall.cur_tenant() IS NULL AND salescall.cur_actor_kind() IN ('admin', 'system'))
  WITH CHECK (salescall.cur_tenant() IS NULL);
CREATE POLICY fix_scope ON salescall.fix_approvals
  USING (salescall.cur_tenant() IS NULL AND salescall.cur_actor_kind() IN ('admin', 'system'))
  WITH CHECK (salescall.cur_tenant() IS NULL);
GRANT SELECT ON salescall.fix_requests TO salescall_app;
GRANT SELECT, INSERT ON salescall.fix_reports, salescall.fix_approvals TO salescall_app;
