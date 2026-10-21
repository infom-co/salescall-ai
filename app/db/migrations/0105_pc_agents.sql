-- 0105 サーバーと PC の分け方の (A)：PC ごとの通行証と、PC が動いているかの見張り（DESIGN.md 5-5。2026-10-21 ユーザー承認：
-- 契約者の PC には電話をつなぐ部分だけを置き、それ以外はサーバーで動かす。「PC が止まったままのとき…誰も気づきません」への答え）。
-- 書いた人：CL-2

-- (1) PC ごとの通行証。PC はデータベースにつながず、サーバー（中央の見守り役）にだけつなぐ。サーバーは通行証の指紋で、どの契約者の
--     どの PC かを決める。通行証そのものは保存しない（指紋だけ。払い出すのは1回だけ＝core/agents.py）。
--     行は消さない（止めた印を1回だけ付ける＝いつ・誰が払い出し、止めたかの記録を兼ねる）。
CREATE TABLE salescall.pc_agents (
  pc_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id    uuid NOT NULL REFERENCES salescall.tenants,
  label        text NOT NULL CHECK (btrim(label) <> ''),                 -- 契約者に見せる PC の名前
  token_sha256 text NOT NULL UNIQUE CHECK (token_sha256 ~ '^[0-9a-f]{64}$'),
  issued_at    timestamptz NOT NULL DEFAULT now(),
  issued_by    text NOT NULL DEFAULT salescall.cur_actor() CHECK (btrim(issued_by) <> ''),
  revoked_at   timestamptz,
  revoked_by   text,
  last_seen_at timestamptz,                                              -- 最後の動いている合図（サーバーが書く）
  lines        uuid[] NOT NULL DEFAULT '{}',                             -- 合図で PC が伝えた、つないでいる回線（その契約者の回線だけ）
  CHECK ((revoked_at IS NULL) = (revoked_by IS NULL)),
  CHECK (revoked_by IS NULL OR btrim(revoked_by) <> '')
);
CREATE INDEX pc_agents_by_tenant ON salescall.pc_agents (tenant_id) WHERE revoked_at IS NULL;
ALTER TABLE salescall.pc_agents ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON salescall.pc_agents
  USING (tenant_id = salescall.cur_tenant()) WITH CHECK (tenant_id = salescall.cur_tenant());
GRANT SELECT, INSERT, UPDATE ON salescall.pc_agents TO salescall_app;

-- 払い出すのは、ログインを確かめるシステムの処理か管理者だけ。止めるのは管理者か契約者（画面の「この PC のログインを外す」）か
-- システムの処理（同じ PC でログインし直したとき・契約の終わり）。止めた印は1回だけ。合図の時刻と回線は、サーバーの関数だけが書く。
CREATE FUNCTION salescall.pc_agents_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF current_user = 'salescall_owner' THEN
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
  END IF;
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'PC の通行証は消せません（止めた印を付けます）' USING ERRCODE = 'check_violation';
  END IF;
  IF TG_OP = 'INSERT' THEN
    IF salescall.cur_actor_kind() NOT IN ('system', 'admin') THEN
      RAISE EXCEPTION 'PC の通行証を払い出すのは、ログインを確かめるシステムの処理か管理者だけです'
        USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.revoked_at IS NOT NULL OR NEW.last_seen_at IS NOT NULL OR NEW.lines <> '{}' THEN
      RAISE EXCEPTION '払い出したばかりの通行証です' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
  END IF;
  IF OLD.revoked_at IS NOT NULL OR NEW.revoked_at IS NULL
     OR (NEW.pc_id, NEW.tenant_id, NEW.label, NEW.token_sha256, NEW.issued_at, NEW.issued_by, NEW.last_seen_at, NEW.lines)
        IS DISTINCT FROM (OLD.pc_id, OLD.tenant_id, OLD.label, OLD.token_sha256, OLD.issued_at, OLD.issued_by, OLD.last_seen_at, OLD.lines) THEN
    RAISE EXCEPTION 'PC の通行証で変えてよいのは、止めた印を1回付けることだけです' USING ERRCODE = 'check_violation';
  END IF;
  IF salescall.cur_actor_kind() NOT IN ('system', 'admin', 'operator') THEN
    RAISE EXCEPTION 'PC の通行証を止めるのは、管理者か契約者かシステムの処理だけです' USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER pc_agents_guard BEFORE INSERT OR UPDATE OR DELETE ON salescall.pc_agents
  FOR EACH ROW EXECUTE FUNCTION salescall.pc_agents_guard();

-- (2) サーバーだけが呼ぶ関数（全体の取引のシステムの処理。返すのは、決めるのに要るものだけ）
CREATE FUNCTION salescall.hub_only() RETURNS void LANGUAGE plpgsql STABLE AS $$
BEGIN
  IF salescall.cur_tenant() IS NOT NULL OR salescall.cur_actor_kind() IS DISTINCT FROM 'system' THEN
    RAISE EXCEPTION 'サーバー（中央の見守り役＝全体の取引のシステムの処理）だけが呼びます' USING ERRCODE = 'insufficient_privilege';
  END IF;
END $$;

-- 通行証の指紋から、どの契約者のどの PC か（止めた通行証は返さない）
CREATE FUNCTION salescall.agent_by_token(p_sha text) RETURNS TABLE (pc_id uuid, tenant_id uuid)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
BEGIN
  PERFORM salescall.hub_only();
  RETURN QUERY SELECT a.pc_id, a.tenant_id FROM salescall.pc_agents a WHERE a.token_sha256 = p_sha AND a.revoked_at IS NULL;
END $$;

-- 動いている合図：最後の時刻と、つないでいる回線（その契約者の、使わなくなった印の無い回線だけ）。止めた通行証なら NULL
CREATE FUNCTION salescall.agent_beat(p_pc uuid, p_lines uuid[]) RETURNS uuid[]
LANGUAGE plpgsql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
DECLARE kept uuid[];
BEGIN
  PERFORM salescall.hub_only();
  UPDATE salescall.pc_agents a SET last_seen_at = now(),
         lines = coalesce((SELECT array_agg(l.line_id ORDER BY l.line_id) FROM salescall.lines l
                           WHERE l.tenant_id = a.tenant_id AND l.retired_at IS NULL AND l.line_id = ANY (p_lines)), '{}')
   WHERE a.pc_id = p_pc AND a.revoked_at IS NULL
  RETURNING a.lines INTO kept;
  RETURN kept;
END $$;

-- すべての契約者（中央の見守り役が、契約者ごとの仕事を回すため。返すのは番号だけ）
CREATE FUNCTION salescall.all_tenants() RETURNS SETOF uuid
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
BEGIN
  PERFORM salescall.hub_only();
  RETURN QUERY SELECT t.tenant_id FROM salescall.tenants t ORDER BY t.created_at, t.tenant_id;
END $$;

REVOKE ALL ON FUNCTION salescall.hub_only(), salescall.agent_by_token(text), salescall.agent_beat(uuid, uuid[]),
  salescall.all_tenants() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.hub_only(), salescall.agent_by_token(text), salescall.agent_beat(uuid, uuid[]),
  salescall.all_tenants() TO salescall_app;

-- (3) PC からの合図が、この長さより途切れたら、その契約者と管理者へ知らせる（1回の途切れに1回。止めない）
INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, finest_scope) VALUES
('pc.down_after', 'override', 'seconds',
 'PC（電話をつなぐ部品）からの動いている合図が、この長さより途切れたら、その契約者と管理者へ知らせる（止めない。その PC の回線は、'
 '借用の期限で門が通さない）', 'DESIGN.md 5-5', 'system');
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('pc.down_after', 'system', '180', 'provisional',
 'PC は15秒ごとに合図を送る。通信の一瞬の切れでは知らせず、止まったことには数分で気づく長さ（CL-2 の初めの値）',
 'サーバーへ移して、合図の途切れ方を測って', 'CL-2');
