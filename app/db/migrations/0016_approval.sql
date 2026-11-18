-- 0016 検証モード（人の承認。DESIGN.md 10-2）と「送ってよい」の印の付け方（10-1 の3）。書いた人：CL-2
-- - 人の承認は、運営者が業務ごとに「使う」と決めたときだけ（設定 approval.human。初めは使わない＝2026-10-14 ユーザー）。
--   モードを読むのは core/approval.py の1か所だけ（I-13。機械が見張る）。門と締めの処理は、承認の部品に問い合わせるだけ。
-- - 発信の前の承認（task_approvals）：運営者がその行を承認した記録（変更不可。運営者だけが書ける＝データベースが守る）。
-- - 送ってよい印（result_releases。0001）を付けるのも core/approval.py だけ。
CREATE TABLE salescall.task_approvals (
  tenant_id   uuid NOT NULL,
  client_id   uuid NOT NULL,
  task_id     uuid PRIMARY KEY,
  approved_at timestamptz NOT NULL DEFAULT now(),
  approved_by text NOT NULL CHECK (btrim(approved_by) <> ''),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, task_id) REFERENCES salescall.tasks (client_id, task_id)
);
CREATE FUNCTION salescall.task_approval_by_operator() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF salescall.cur_actor_kind() IS DISTINCT FROM 'operator' OR NEW.approved_by IS DISTINCT FROM salescall.cur_actor() THEN
    RAISE EXCEPTION '行を承認できるのは運営者だけです（承認した人は取引の人）' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'task_approvals_by_operator';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER task_approval_by_operator BEFORE INSERT ON salescall.task_approvals
  FOR EACH ROW EXECUTE FUNCTION salescall.task_approval_by_operator();
CREATE TRIGGER task_approvals_frozen BEFORE UPDATE OR DELETE ON salescall.task_approvals
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
ALTER TABLE salescall.task_approvals ENABLE ROW LEVEL SECURITY;
CREATE POLICY client_scope ON salescall.task_approvals
  USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
  WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client());
GRANT SELECT, INSERT ON salescall.task_approvals TO salescall_app;

INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, bound_key, bound_side) VALUES
('approval.human', 'override', 'boolean', '人の承認（検証モード）を使うか：発信の前に運営者が行を承認し、結果を送る前に運営者が確かめる。運営者が業務ごとに決める', 'DESIGN.md 10-2', NULL, NULL);
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('approval.human', 'system', 'false', 'decided', '2026-10-14 ユーザー：人の承認は、運営者が業務ごとに「使う」と決めたときだけ（既定は使わない。人のいない仕組み）', '変えない見込み（業務ごとに運営者が決める）', 'ユーザー');
