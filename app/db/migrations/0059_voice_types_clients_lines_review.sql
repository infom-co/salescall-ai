-- 0059 声・依頼者・回線の見直し（2026-10-18 CL-2。DESIGN.md 15-2 の155）
--
-- (1) 声の型（voice_types）を、管理者が足せる（前は表の持ち主＝移行のファイルでしか足せず、管理者が声を足しても、その声の
--     初めの声の型が無いので、いつまでも使えなかった。2026-10-17 ユーザー：「新しい声が増えるかもしれません」＝データを足すだけ）。
--     変えたり消したりはできない（前の通話の記録が指している）。
-- (2) 依頼者が有効か（client_active）を、取引の範囲で見えない所から聞くと「有効」と答えていた（記録が無い＝有効）。
--     ⇒ 契約者の中なら、行の制限を外して答える。範囲の外（ほかの契約者）には答えない（NULL＝分からない）。
-- (3) 使わなくなった回線は、名前のほかは変えられない（前は依頼者の割り当てを替えられた）。

-- ═════════ (1) 声の型 ═════════
CREATE FUNCTION salescall.voice_types_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP <> 'INSERT' THEN
    RAISE EXCEPTION '声の型は変えられません・消せません（前の通話の記録が指しています）' USING ERRCODE = 'check_violation';
  END IF;
  IF current_user <> 'salescall_owner' AND salescall.cur_actor_kind() IS DISTINCT FROM 'admin' THEN
    RAISE EXCEPTION '声の型を足せるのは管理者だけです' USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER voice_types_guard BEFORE INSERT OR UPDATE OR DELETE ON salescall.voice_types
  FOR EACH ROW EXECUTE FUNCTION salescall.voice_types_guard();
GRANT INSERT ON salescall.voice_types TO salescall_app;

-- ═════════ (2) 依頼者が有効か ═════════
CREATE OR REPLACE FUNCTION salescall.client_active(p_client uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM salescall.clients c WHERE c.client_id = p_client
                               AND (salescall.cur_tenant() IS NULL OR c.tenant_id = salescall.cur_tenant())) THEN NULL
              ELSE coalesce((SELECT s.active FROM salescall.client_states s WHERE s.client_id = p_client
                             ORDER BY s.seq DESC LIMIT 1), true) END
$$;

-- ═════════ (3) 使わなくなった回線 ═════════
CREATE OR REPLACE FUNCTION salescall.line_before_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (NEW.tenant_id, NEW.line_id, NEW.method, NEW.caller_e164) IS DISTINCT FROM
     (OLD.tenant_id, OLD.line_id, OLD.method, OLD.caller_e164) THEN
    RAISE EXCEPTION '回線の接続方式・発信番号は変えられません（番号を替えるときは新しい回線にする）' USING ERRCODE = 'check_violation';
  END IF;
  IF OLD.retired_at IS NOT NULL AND (NEW.retired_at, NEW.retired_reason, NEW.client_id)
                                    IS DISTINCT FROM (OLD.retired_at, OLD.retired_reason, OLD.client_id) THEN
    RAISE EXCEPTION '使わなくなった回線は、名前のほかは変えられません（使わなくなった印も外せません）' USING ERRCODE = 'check_violation';
  END IF;
  IF (NEW.name IS DISTINCT FROM OLD.name OR NEW.client_id IS DISTINCT FROM OLD.client_id
      OR NEW.retired_at IS DISTINCT FROM OLD.retired_at)
     AND coalesce(salescall.cur_actor_kind()::text, '') NOT IN ('operator', 'admin') THEN
    RAISE EXCEPTION '回線の名前・依頼者の割り当て・使わなくなった印を変えられるのは、管理者か契約者だけです'
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF NEW.name IS DISTINCT FROM OLD.name THEN
    INSERT INTO salescall.name_changes (tenant_id, client_id, kind, target_id, old_name, new_name, changed_by)
    VALUES (NEW.tenant_id, NULL, 'line', NEW.line_id, OLD.name, NEW.name, salescall.cur_actor());
  END IF;
  RETURN NEW;
END $$;
