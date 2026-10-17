-- 0051 依頼者・業務・実施回・回線を作る・直す部品の守り（Intent.md 4章 A・C・5-3 (4)(11)。2026-10-17。書いた人：CL-2）
--
-- 作る・名前を直す・無効にするのは core/registry.py の1か所（I-12。いままでは試験だけが表に直接書いていた）。
-- (1) 依頼者の有効／無効（Intent.md 4章 A「登録・編集・無効化」）：記録だけを足す（client_states。最新が正本・記録が無ければ有効）。
--     無効の依頼者の発信は、門が通さない（client_inactive）。無効にしたとき、まだ発信していない予約は取り消す。
-- (2) 名前を直した記録（Intent.md 5-3 (11)「変更内容・日時・対象を記録する」）：依頼者・業務・実施回・回線の名前を直すと、データベースが
--     前と後の名前・誰が・いつを記録する（name_changes）。名前のほかは直せない（回線の借用と依頼者の割り当ては、これまでどおり）。
-- (3) 回線の発信番号（Intent.md 5-3 (4)「発信番号」・8-7）：回線ごとに1つ（caller_e164。国際の形）。あとから変えられない
--     （お客様の着信の履歴に残る番号と、どの通話をどの番号でかけたかが食い違わないよう。番号を替えるなら新しい回線）。
--     同じ番号の回線は2つ作れない（契約者をまたいでも）。
-- (4) 回線を使わなくなった（端末を返した など）：印を1回だけ付ける（retired_at。消さない：前の通話の記録が指している）。
--     使わない回線では、門が通さない（line_retired）。

-- ═════════ (1) 依頼者の有効／無効 ═════════
CREATE TABLE salescall.client_states (
  tenant_id uuid NOT NULL,
  client_id uuid NOT NULL,
  seq       integer NOT NULL CHECK (seq >= 1),
  active    boolean NOT NULL,
  reason    text NOT NULL CHECK (btrim(reason) <> ''),
  set_by    text NOT NULL CHECK (btrim(set_by) <> ''),
  set_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (client_id, seq),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id)
);
CREATE TRIGGER client_states_frozen BEFORE UPDATE OR DELETE ON salescall.client_states
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
CREATE FUNCTION salescall.client_state_by_person() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF coalesce(salescall.cur_actor_kind()::text, '') NOT IN ('operator', 'admin')
     OR NEW.set_by IS DISTINCT FROM salescall.cur_actor() THEN
    RAISE EXCEPTION '依頼者を有効・無効にできるのは、管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER client_state_by_person BEFORE INSERT ON salescall.client_states
  FOR EACH ROW EXECUTE FUNCTION salescall.client_state_by_person();
ALTER TABLE salescall.client_states ENABLE ROW LEVEL SECURITY;
CREATE POLICY client_list ON salescall.client_states
  USING (tenant_id = salescall.cur_tenant() AND (salescall.cur_client() IS NULL OR client_id = salescall.cur_client()))
  WITH CHECK (tenant_id = salescall.cur_tenant() AND (salescall.cur_client() IS NULL OR client_id = salescall.cur_client()));
GRANT SELECT, INSERT ON salescall.client_states TO salescall_app;

-- その依頼者が、いま有効か（記録が無ければ有効。判断はここだけ）
CREATE FUNCTION salescall.client_active(p_client uuid) RETURNS boolean LANGUAGE sql STABLE AS $$
  SELECT coalesce((SELECT s.active FROM salescall.client_states s WHERE s.client_id = p_client ORDER BY s.seq DESC LIMIT 1), true)
$$;
GRANT EXECUTE ON FUNCTION salescall.client_active(uuid) TO salescall_app;

-- ═════════ (2) 名前を直した記録 ═════════
CREATE TABLE salescall.name_changes (
  change_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id  uuid NOT NULL,
  client_id  uuid,                                 -- 回線は依頼者を持たない
  kind       text NOT NULL CHECK (kind IN ('client', 'business', 'run', 'line')),
  target_id  uuid NOT NULL,
  old_name   text NOT NULL,
  new_name   text NOT NULL,
  changed_by text NOT NULL CHECK (btrim(changed_by) <> ''),
  changed_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER name_changes_frozen BEFORE UPDATE OR DELETE ON salescall.name_changes
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
ALTER TABLE salescall.name_changes ENABLE ROW LEVEL SECURITY;
CREATE POLICY name_change_scope ON salescall.name_changes
  USING (tenant_id = salescall.cur_tenant() AND (client_id IS NULL OR salescall.cur_client() IS NULL
                                                 OR client_id = salescall.cur_client()))
  WITH CHECK (tenant_id = salescall.cur_tenant());
GRANT SELECT, INSERT ON salescall.name_changes TO salescall_app;

-- 名前を直せるのは管理者か契約者だけ・名前のほかは直せない（この関数を各表の「直す前」に付ける）・直したら記録する
CREATE FUNCTION salescall.only_name_changes() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE kind text := CASE TG_TABLE_NAME WHEN 'clients' THEN 'client' WHEN 'businesses' THEN 'business' ELSE 'run' END;
BEGIN
  IF (to_jsonb(NEW) - 'name') IS DISTINCT FROM (to_jsonb(OLD) - 'name') THEN
    RAISE EXCEPTION '直せるのは名前だけです' USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.name IS DISTINCT FROM OLD.name THEN
    IF coalesce(salescall.cur_actor_kind()::text, '') NOT IN ('operator', 'admin') THEN
      RAISE EXCEPTION '名前を直せるのは、管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege';
    END IF;
    INSERT INTO salescall.name_changes (tenant_id, client_id, kind, target_id, old_name, new_name, changed_by)
    VALUES (NEW.tenant_id, NEW.client_id, kind,
            CASE kind WHEN 'client' THEN NEW.client_id WHEN 'business' THEN (to_jsonb(NEW) ->> 'business_id')::uuid
                      ELSE (to_jsonb(NEW) ->> 'run_id')::uuid END,
            OLD.name, NEW.name, salescall.cur_actor());
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER clients_only_name BEFORE UPDATE ON salescall.clients
  FOR EACH ROW EXECUTE FUNCTION salescall.only_name_changes();
CREATE TRIGGER businesses_only_name BEFORE UPDATE ON salescall.businesses
  FOR EACH ROW EXECUTE FUNCTION salescall.only_name_changes();
CREATE TRIGGER runs_only_name BEFORE UPDATE ON salescall.runs
  FOR EACH ROW EXECUTE FUNCTION salescall.only_name_changes();
GRANT UPDATE (name) ON salescall.clients, salescall.businesses, salescall.runs TO salescall_app;

-- ═════════ (3)(4) 回線の発信番号・使わなくなった印 ═════════
ALTER TABLE salescall.lines
  ADD COLUMN caller_e164 text CHECK (caller_e164 ~ '^\+[1-9][0-9]{7,14}$'),
  ADD COLUMN retired_at timestamptz,
  ADD COLUMN retired_reason text,
  ADD CONSTRAINT lines_retired_with_reason CHECK ((retired_at IS NULL) = (retired_reason IS NULL)),
  ADD CONSTRAINT lines_retired_reason_text CHECK (retired_reason IS NULL OR btrim(retired_reason) <> '');
ALTER TABLE salescall.lines ALTER COLUMN caller_e164 SET NOT NULL;     -- 発信番号の無い回線は作れない
CREATE UNIQUE INDEX lines_one_per_caller_number ON salescall.lines (caller_e164);

-- 回線：変えてよいのは、借用（見守り役）・依頼者の割り当て・名前（記録つき）・使わなくなった印（1回だけ）。発信番号は変えられない
CREATE FUNCTION salescall.line_before_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (NEW.tenant_id, NEW.line_id, NEW.method, NEW.caller_e164) IS DISTINCT FROM
     (OLD.tenant_id, OLD.line_id, OLD.method, OLD.caller_e164) THEN
    RAISE EXCEPTION '回線の接続方式・発信番号は変えられません（番号を替えるときは新しい回線にする）' USING ERRCODE = 'check_violation';
  END IF;
  IF OLD.retired_at IS NOT NULL AND (NEW.retired_at, NEW.retired_reason) IS DISTINCT FROM (OLD.retired_at, OLD.retired_reason) THEN
    RAISE EXCEPTION '使わなくなった印は、外せません（1回だけ）' USING ERRCODE = 'check_violation';
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
CREATE TRIGGER line_before_update BEFORE UPDATE ON salescall.lines
  FOR EACH ROW EXECUTE FUNCTION salescall.line_before_update();

-- その回線の、まだ発信していない予約を取り消す（回線の依頼者の割り当てを変えた・使わなくなったとき。門の前提が変わる）。
-- 予約は依頼者ごとの行なので、回線の契約者の取引から、この関数だけが取り消す（ほかの契約者の回線は取り消せない）
CREATE FUNCTION salescall.release_claims_on_line(p_line uuid) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
DECLARE n integer;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM salescall.lines l WHERE l.line_id = p_line AND l.tenant_id = salescall.cur_tenant()) THEN
    RAISE EXCEPTION '取引の契約者の回線ではありません' USING ERRCODE = 'insufficient_privilege';
  END IF;
  UPDATE salescall.attempts SET state = 'released' WHERE line_id = p_line AND state = 'claimed';
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$;
REVOKE ALL ON FUNCTION salescall.release_claims_on_line(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.release_claims_on_line(uuid) TO salescall_app;
