-- 0247 管理者が、すべての契約者の一覧を読む道・画面の操作の1回きりの札（2026-11-19。書いた人：CL-2。DESIGN.md 11章・15-2 の881・882。
--      CL-3 の連絡帳 4-1b の 9・4-1 の 2）

-- ═════════ (1) 管理者が、すべての契約者の一覧（名前と番号）を読む（CL-3 の 9） ═════════
-- ✅ 2026-11-18 管理者「管理者は全てを見れて操作できる」。前は salescall.all_tenants（0105）がサーバーのシステムの処理だけに答え、管理者が画面で
-- 契約者を選べなかった。読めるのは、全体の取引（契約者を決めていない取引）の管理者だけ（契約者の取引・システムの処理・契約者の方は断る）。
CREATE FUNCTION salescall.admin_tenants() RETURNS TABLE (tenant_id uuid, name text)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
BEGIN
  IF salescall.cur_tenant() IS NOT NULL OR salescall.cur_actor_kind() IS DISTINCT FROM 'admin' THEN
    RAISE EXCEPTION 'すべての契約者の一覧を読めるのは、全体の取引の管理者だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'admin_tenants_by_admin';
  END IF;
  RETURN QUERY SELECT t.tenant_id, t.name FROM salescall.tenants t ORDER BY t.created_at, t.tenant_id;
END $$;
REVOKE ALL ON FUNCTION salescall.admin_tenants() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.admin_tenants() TO salescall_app;

-- ═════════ (2) 画面の操作の1回きりの札（CL-3 の 2。DASHBOARD.md 8-1「同じ札で2回送ると、2回目は何も変わらない」） ═════════
-- 画面を開いたときに札を出し（action_tokens）、送ったら、その操作と同じ取引で使用済みにする（action_token_uses。札ごとに1つ＝一意の約束）。
-- 操作が断られて取引が戻れば、札も使っていないまま（もう一度送れる）。同じ札で同時に2回送っても、使用済みになるのは1つだけ（後のほうは
-- 何も変えない＝core/once.use が偽を返す）。出せるのも使えるのも、管理者か契約者（いま操作している方）だけで、出した方だけが、出した
-- 操作の名前でだけ使える。札は、出した取引の契約者のもの（全体の取引の札は契約者を持たない）。
CREATE TABLE salescall.action_tokens (
  token      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id  uuid REFERENCES salescall.tenants,                    -- 出した取引の契約者（全体の取引＝無い）
  action     text NOT NULL CHECK (btrim(action) <> ''),           -- 操作の名前（画面が決める。使うときに同じ名前か確かめる）
  issued_by  text NOT NULL CHECK (btrim(issued_by) <> ''),
  issued_at  timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE salescall.action_token_uses (
  token      uuid PRIMARY KEY REFERENCES salescall.action_tokens,
  tenant_id  uuid REFERENCES salescall.tenants,
  action     text NOT NULL,
  used_by    text NOT NULL CHECK (btrim(used_by) <> ''),
  used_at    timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER stamp_now BEFORE INSERT ON salescall.action_tokens FOR EACH ROW EXECUTE FUNCTION salescall.stamp_now('issued_at');
CREATE TRIGGER stamp_now BEFORE INSERT ON salescall.action_token_uses FOR EACH ROW EXECUTE FUNCTION salescall.stamp_now('used_at');
CREATE TRIGGER action_tokens_frozen BEFORE UPDATE OR DELETE ON salescall.action_tokens FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
CREATE TRIGGER action_token_uses_frozen BEFORE UPDATE OR DELETE ON salescall.action_token_uses
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();

CREATE FUNCTION salescall.action_token_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT salescall.cur_is_person() THEN
    RAISE EXCEPTION '操作の札を出す・使うのは、管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'action_token_by_person';
  END IF;
  IF NEW.tenant_id IS DISTINCT FROM salescall.cur_tenant() THEN
    RAISE EXCEPTION '操作の札は、いまの取引の契約者のものだけです' USING ERRCODE = 'check_violation', CONSTRAINT = 'action_token_tenant';
  END IF;
  IF TG_TABLE_NAME = 'action_tokens' THEN
    IF NEW.issued_by IS DISTINCT FROM salescall.cur_actor() THEN
      RAISE EXCEPTION '札を出した方は、いま操作している方です' USING ERRCODE = 'check_violation', CONSTRAINT = 'action_token_issued_by';
    END IF;
  ELSE
    IF NEW.used_by IS DISTINCT FROM salescall.cur_actor() OR NOT EXISTS (
         SELECT 1 FROM salescall.action_tokens k WHERE k.token = NEW.token AND k.issued_by = NEW.used_by AND k.action = NEW.action
           AND k.tenant_id IS NOT DISTINCT FROM NEW.tenant_id) THEN
      RAISE EXCEPTION '使えるのは、いま操作している方が、この操作のために出した札だけです' USING ERRCODE = 'check_violation',
        CONSTRAINT = 'action_token_use_own';
    END IF;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER action_token_guard BEFORE INSERT ON salescall.action_tokens FOR EACH ROW EXECUTE FUNCTION salescall.action_token_guard();
CREATE TRIGGER action_token_guard BEFORE INSERT ON salescall.action_token_uses FOR EACH ROW EXECUTE FUNCTION salescall.action_token_guard();

ALTER TABLE salescall.action_tokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE salescall.action_token_uses ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON salescall.action_tokens
  USING (tenant_id IS NOT DISTINCT FROM salescall.cur_tenant()) WITH CHECK (tenant_id IS NOT DISTINCT FROM salescall.cur_tenant());
CREATE POLICY tenant_scope ON salescall.action_token_uses
  USING (tenant_id IS NOT DISTINCT FROM salescall.cur_tenant()) WITH CHECK (tenant_id IS NOT DISTINCT FROM salescall.cur_tenant());
GRANT SELECT, INSERT ON salescall.action_tokens, salescall.action_token_uses TO salescall_app;
