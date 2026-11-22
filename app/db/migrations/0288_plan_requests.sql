-- 0288 契約プランの切り替えの申し込み（2026-11-23。書いた人：CL-2。DESIGN_15-2.md の963。CL-3 の連絡帳 4-1c の 79）
-- 計画書 1-4（2026-11-06 ユーザー）：「プランを切り替えられるのは管理者だけ（料金に関わる）。契約者は画面から申し込み、管理者が確かめて切り替える」。
-- 申し込む：契約者（いま操作している方）が、AI の種類ごとに開いた申し込みを1つだけ持てる（ai_plan_requests。追記だけ）。管理者へ知らせる（core/ai_plans）。
-- 閉じる（ai_plan_request_closes。1回だけ）：管理者がその契約者のそのプランに切り替えたとき（設定の値を足す1か所＝setting_values）に、データベースが
--   自動で閉じる（switched）。管理者が見送るときは理由を書いて閉じる（declined。salescall.decline_plan_request）。切り替えるのは今の設定の1か所のまま。
-- 開いている申し込みを全部の契約者について読めるのは管理者だけ（salescall.plan_requests_open）。
CREATE TABLE salescall.ai_plan_requests (
  request_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id    uuid NOT NULL REFERENCES salescall.tenants,
  ai           text NOT NULL CHECK (ai IN ('conversation', 'import')),
  plan         text NOT NULL CHECK (plan IN ('included', 'own')),
  note         text CHECK (note IS NULL OR btrim(note) <> ''),
  requested_at timestamptz NOT NULL DEFAULT now(),
  requested_by text NOT NULL DEFAULT salescall.cur_actor() CHECK (btrim(requested_by) <> ''),
  UNIQUE (tenant_id, request_id)
);
CREATE TABLE salescall.ai_plan_request_closes (
  request_id bigint PRIMARY KEY,
  tenant_id  uuid NOT NULL,
  why        text NOT NULL CHECK (why IN ('switched', 'declined')),
  note       text CHECK (note IS NULL OR btrim(note) <> ''),
  closed_at  timestamptz NOT NULL DEFAULT now(),
  closed_by  text NOT NULL DEFAULT coalesce(salescall.cur_actor(), 'データベース'),
  FOREIGN KEY (tenant_id, request_id) REFERENCES salescall.ai_plan_requests (tenant_id, request_id),
  CHECK (why <> 'declined' OR note IS NOT NULL)
);
DO $$ DECLARE t text; BEGIN
  FOREACH t IN ARRAY ARRAY['ai_plan_requests', 'ai_plan_request_closes'] LOOP
    EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON salescall.%I FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change()',
                   t || '_frozen', t);
    EXECUTE format('ALTER TABLE salescall.%I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY tenant_scope ON salescall.%I USING (tenant_id = salescall.cur_tenant())
                      WITH CHECK (tenant_id = salescall.cur_tenant())', t);
    EXECUTE format('GRANT SELECT ON salescall.%I TO salescall_app', t);
  END LOOP;
END $$;
GRANT INSERT ON salescall.ai_plan_requests TO salescall_app;

-- いま開いている申し込み（1か所）
CREATE FUNCTION salescall.plan_request_open(p_tenant uuid, p_ai text) RETURNS bigint LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = salescall, pg_temp AS $$
  SELECT r.request_id FROM salescall.ai_plan_requests r
   WHERE r.tenant_id = p_tenant AND r.ai = p_ai AND r.tenant_id = coalesce(salescall.cur_tenant(), r.tenant_id)
     AND NOT EXISTS (SELECT 1 FROM salescall.ai_plan_request_closes c WHERE c.request_id = r.request_id)
   ORDER BY r.request_id DESC LIMIT 1
$$;
REVOKE ALL ON FUNCTION salescall.plan_request_open(uuid, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.plan_request_open(uuid, text) TO salescall_app;

CREATE FUNCTION salescall.plan_request_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT salescall.cur_is_person() OR salescall.cur_tenant() IS NULL THEN
    RAISE EXCEPTION '契約プランの切り替えを申し込めるのは、契約者の取引の管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'plan_request_by_person';
  END IF;
  IF salescall.plan_request_open(NEW.tenant_id, NEW.ai) IS NOT NULL THEN
    RAISE EXCEPTION 'この AI の切り替えの申し込みは、もう出ています（管理者が確かめるのを待ってください）' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'plan_request_one_open';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER ai_plan_request_guard BEFORE INSERT ON salescall.ai_plan_requests FOR EACH ROW EXECUTE FUNCTION salescall.plan_request_guard();

-- 管理者がその契約者のそのプランに切り替えたら、開いている申し込みを閉じる（設定の値を足す1か所に付ける）
CREATE FUNCTION salescall.close_plan_request_on_switch() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = salescall, pg_temp AS $$
DECLARE v_ai text := CASE NEW.key WHEN 'ai.plan_conversation' THEN 'conversation' WHEN 'ai.plan_import' THEN 'import' END;
        open_id bigint;
BEGIN
  IF v_ai IS NOT NULL AND NEW.scope = 'tenant' THEN
    SELECT r.request_id INTO open_id FROM salescall.ai_plan_requests r
     WHERE r.tenant_id = NEW.tenant_id AND r.ai = v_ai AND to_jsonb(r.plan) = NEW.value
       AND NOT EXISTS (SELECT 1 FROM salescall.ai_plan_request_closes c WHERE c.request_id = r.request_id);
    IF open_id IS NOT NULL THEN
      INSERT INTO salescall.ai_plan_request_closes (request_id, tenant_id, why) VALUES (open_id, NEW.tenant_id, 'switched');
    END IF;
  END IF;
  RETURN NULL;
END $$;
REVOKE ALL ON FUNCTION salescall.close_plan_request_on_switch() FROM PUBLIC;
CREATE TRIGGER setting_value_closes_plan_request AFTER INSERT ON salescall.setting_values
  FOR EACH ROW EXECUTE FUNCTION salescall.close_plan_request_on_switch();

-- 管理者が見送る（理由を書く）
CREATE FUNCTION salescall.decline_plan_request(p_request bigint, p_note text) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = salescall, pg_temp AS $$
DECLARE r salescall.ai_plan_requests;
BEGIN
  IF salescall.cur_actor_kind() IS DISTINCT FROM 'admin' THEN
    RAISE EXCEPTION '契約プランの申し込みを見送れるのは、管理者だけです' USING ERRCODE = 'insufficient_privilege', CONSTRAINT = 'plan_request_admin';
  END IF;
  SELECT * INTO r FROM salescall.ai_plan_requests WHERE request_id = p_request
     AND tenant_id = coalesce(salescall.cur_tenant(), tenant_id);
  IF r.request_id IS NULL OR salescall.plan_request_open(r.tenant_id, r.ai) IS DISTINCT FROM r.request_id THEN
    RAISE EXCEPTION '開いている申し込みではありません' USING ERRCODE = 'check_violation', CONSTRAINT = 'plan_request_open_only';
  END IF;
  INSERT INTO salescall.ai_plan_request_closes (request_id, tenant_id, why, note) VALUES (r.request_id, r.tenant_id, 'declined', p_note);
END $$;
REVOKE ALL ON FUNCTION salescall.decline_plan_request(bigint, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.decline_plan_request(bigint, text) TO salescall_app;

-- 開いている申し込み（全部の契約者。管理者だけ）
CREATE FUNCTION salescall.plan_requests_open() RETURNS TABLE (request_id bigint, tenant_id uuid, ai text, plan text, note text,
                                                                requested_at timestamptz, requested_by text)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
BEGIN
  IF salescall.cur_actor_kind() IS DISTINCT FROM 'admin' OR salescall.cur_tenant() IS NOT NULL THEN
    RAISE EXCEPTION '全部の契約者の申し込みを読めるのは、管理者だけです（全体の取引で）' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'plan_request_admin';
  END IF;
  RETURN QUERY SELECT r.request_id, r.tenant_id, r.ai, r.plan, r.note, r.requested_at, r.requested_by FROM salescall.ai_plan_requests r
                WHERE NOT EXISTS (SELECT 1 FROM salescall.ai_plan_request_closes c WHERE c.request_id = r.request_id)
                ORDER BY r.request_id;
END $$;
REVOKE ALL ON FUNCTION salescall.plan_requests_open() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.plan_requests_open() TO salescall_app;
