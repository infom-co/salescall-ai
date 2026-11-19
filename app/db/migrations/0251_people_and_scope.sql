-- 0251 管理者・契約者のログインと、取引の範囲をデータベースが確かめる（2026-11-19。書いた人：CL-2。DESIGN.md 11章・15-2 の891。
--      CL-3 の連絡帳 4-1 の 1・4-1b の 14。計画書 14-1 の「認証」・1-4）
--
-- ⛔ 前の作り：取引の「誰が・どの契約者の」は、サーバーの読み書きの層が渡す値をデータベースがそのまま信じていた。ログインした方が本当にその契約者かは
--    確かめず、画面の誤り1つで、契約者がほかの契約者のデータを読み書きできた（CL-3 の 14）。ログインの表も無かった（CL-3 の 1）。
-- ⇒ (1) ログイン（people）：管理者・契約者のログイン1つにつき1行。所属の契約者（管理者も、自分の契約者を持つ＝計画書 1-4「管理者が最初の契約者」）・
--        種類（admin／operator）・記録に残る名前（cur_actor）・ログインID（一意）。パスワードの写し（元に戻せない形）は person_passwords に足していく
--        （いちばん新しいもの）。使えるか（契約が終わった など）は person_states に足していく（いちばん新しいもの）。
--    (2) 取引の範囲の確かめ（salescall.check_scope の1か所。読み書きの層 core/access._run が、どの取引でも、範囲を決めたすぐあとに呼ぶ）：
--        管理者・契約者の取引は、渡されたログイン（salescall.person_id）が、使える・同じ種類・同じ名前のログインでなければ断る。契約者は、自分の
--        契約者の取引だけ（ほかの契約者の取引・全体の取引は開けない）。管理者は、どの契約者の取引も全体の取引も開ける（✅ 2026-11-18 管理者「管理者は
--        全てを見れて操作できる」）。システムの処理は、ログインを持たない。
--    (3) ログインを作れるのは管理者だけ（全体の取引）。管理者がまだ1人もいないときだけ、システムの処理が最初の管理者を、その契約者と一緒に作れる
--        （core/people.first_admin。サーバーを用意するとき）。
--    (4) ログインの確かめ（core/people.login）：ログインIDとパスワード。決めた回数続けて誤ると、決めた長さのあいだ断る（設定 login.*）。確かめた
--        記録は person_login_events に残す（パスワードは残さない）。ログインの印（person_sessions）は、印そのものでなく指紋だけを残す。
--    ⏳ 2段階の確かめ（認証アプリ・パスキー）と Google のアカウントでのログインは、次の段で足す（計画書 14-1 の 2026-10-18 のご決定）。

CREATE TABLE salescall.people (
  person_id   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   uuid NOT NULL REFERENCES salescall.tenants,              -- 所属の契約者
  kind        text NOT NULL CHECK (salescall.is_person_kind(kind)),   -- 管理者か契約者か（種類の組は salescall.is_person_kind の1か所＝0205）
  name        text NOT NULL CHECK (btrim(name) <> ''),                  -- 記録に残る名前（取引の cur_actor）
  login_id    text NOT NULL UNIQUE CHECK (login_id = lower(btrim(login_id)) AND login_id <> ''),
  created_by  text NOT NULL CHECK (btrim(created_by) <> ''),
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE salescall.person_passwords (
  seq         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  person_id   uuid NOT NULL REFERENCES salescall.people,
  hash        text NOT NULL CHECK (hash ~ '^scrypt\$[0-9]+\$[0-9]+\$[0-9]+\$[0-9a-f]{32}\$[0-9a-f]{128}$'),
  set_by      text NOT NULL CHECK (btrim(set_by) <> ''),
  set_at      timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE salescall.person_states (
  seq         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  person_id   uuid NOT NULL REFERENCES salescall.people,
  active      boolean NOT NULL,
  reason      text NOT NULL CHECK (btrim(reason) <> ''),
  set_by      text NOT NULL CHECK (btrim(set_by) <> ''),
  set_at      timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE salescall.person_login_events (
  seq         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  login_id    text NOT NULL,                                            -- 入れられたログインID（無いログインIDも残す）
  person_id   uuid REFERENCES salescall.people,
  ok          boolean NOT NULL,
  why         text NOT NULL CHECK (why IN ('ok', 'no_such_login', 'wrong_password', 'no_password', 'inactive', 'locked')),
  at          timestamptz NOT NULL DEFAULT now(),
  CHECK (ok = (why = 'ok'))
);
CREATE INDEX person_login_events_recent ON salescall.person_login_events (login_id, at);
CREATE TABLE salescall.person_sessions (
  token_sha   text PRIMARY KEY CHECK (token_sha ~ '^[0-9a-f]{64}$'),    -- ログインの印の指紋（印そのものは残さない）
  person_id   uuid NOT NULL REFERENCES salescall.people,
  started_at  timestamptz NOT NULL DEFAULT now(),
  expires_at  timestamptz NOT NULL,
  CHECK (expires_at > started_at)
);
CREATE TABLE salescall.person_session_ends (
  token_sha   text PRIMARY KEY REFERENCES salescall.person_sessions,
  why         text NOT NULL CHECK (why IN ('logout', 'password_changed', 'inactive')),
  ended_at    timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER stamp_now BEFORE INSERT ON salescall.people FOR EACH ROW EXECUTE FUNCTION salescall.stamp_now('created_at');
CREATE TRIGGER stamp_now BEFORE INSERT ON salescall.person_passwords FOR EACH ROW EXECUTE FUNCTION salescall.stamp_now('set_at');
CREATE TRIGGER stamp_now BEFORE INSERT ON salescall.person_states FOR EACH ROW EXECUTE FUNCTION salescall.stamp_now('set_at');
CREATE TRIGGER stamp_now BEFORE INSERT ON salescall.person_login_events FOR EACH ROW EXECUTE FUNCTION salescall.stamp_now('at');
CREATE TRIGGER stamp_now BEFORE INSERT ON salescall.person_sessions FOR EACH ROW EXECUTE FUNCTION salescall.stamp_now('started_at');
CREATE TRIGGER stamp_now BEFORE INSERT ON salescall.person_session_ends FOR EACH ROW EXECUTE FUNCTION salescall.stamp_now('ended_at');
CREATE TRIGGER people_frozen BEFORE UPDATE OR DELETE ON salescall.people FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
CREATE TRIGGER person_passwords_frozen BEFORE UPDATE OR DELETE ON salescall.person_passwords FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
CREATE TRIGGER person_states_frozen BEFORE UPDATE OR DELETE ON salescall.person_states FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
CREATE TRIGGER person_login_events_frozen BEFORE UPDATE OR DELETE ON salescall.person_login_events
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
CREATE TRIGGER person_sessions_frozen BEFORE UPDATE OR DELETE ON salescall.person_sessions FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
CREATE TRIGGER person_session_ends_frozen BEFORE UPDATE OR DELETE ON salescall.person_session_ends
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();

-- いまそのログインを使えるか（1か所。いちばん新しい状態。状態が無ければ使える）
CREATE FUNCTION salescall.person_active(p_person uuid) RETURNS boolean LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT coalesce((SELECT s.active FROM salescall.person_states s WHERE s.person_id = p_person ORDER BY s.seq DESC LIMIT 1), true)
$$;

-- ═════════ 取引の範囲の確かめ（1か所） ═════════
CREATE FUNCTION salescall.check_scope() RETURNS void LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = salescall, pg_temp AS $$
DECLARE
  k text := salescall.cur_actor_kind();
  pid uuid := nullif(current_setting('salescall.person_id', true), '')::uuid;
  p salescall.people;
BEGIN
  IF salescall.is_person_kind(k) THEN
    SELECT * INTO p FROM salescall.people x WHERE x.person_id = pid;
    IF NOT FOUND OR p.kind IS DISTINCT FROM k OR p.name IS DISTINCT FROM salescall.cur_actor() OR NOT salescall.person_active(p.person_id) THEN
      RAISE EXCEPTION 'ログインした管理者・契約者の取引ではありません' USING ERRCODE = 'insufficient_privilege', CONSTRAINT = 'scope_person';
    END IF;
    IF k = 'operator' AND salescall.cur_tenant() IS DISTINCT FROM p.tenant_id THEN
      RAISE EXCEPTION '契約者は、自分の契約者の取引だけを開けます' USING ERRCODE = 'insufficient_privilege', CONSTRAINT = 'scope_own_tenant';
    END IF;
  ELSIF pid IS NOT NULL THEN
    RAISE EXCEPTION 'システムの処理の取引は、ログインを持ちません' USING ERRCODE = 'insufficient_privilege', CONSTRAINT = 'scope_person';
  END IF;
END $$;

-- ═════════ 読める範囲・書ける方 ═════════
ALTER TABLE salescall.people ENABLE ROW LEVEL SECURITY;
ALTER TABLE salescall.person_passwords ENABLE ROW LEVEL SECURITY;
ALTER TABLE salescall.person_states ENABLE ROW LEVEL SECURITY;
ALTER TABLE salescall.person_login_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE salescall.person_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE salescall.person_session_ends ENABLE ROW LEVEL SECURITY;
-- ログインの一覧：その契約者の取引ではその契約者のログイン、全体の取引の管理者は全部（パスワードの写し・ログインの記録・印は、全体の取引の
-- 管理者とシステムの処理だけ＝契約者の取引からは読めない）
CREATE POLICY scope ON salescall.people
  USING (tenant_id = salescall.cur_tenant() OR (salescall.cur_tenant() IS NULL AND salescall.cur_actor_kind() IN ('admin', 'system')))
  WITH CHECK (salescall.cur_tenant() IS NULL AND salescall.cur_actor_kind() IN ('admin', 'system'));
CREATE POLICY scope ON salescall.person_passwords
  USING (salescall.cur_tenant() IS NULL AND salescall.cur_actor_kind() IN ('admin', 'system'))
  WITH CHECK (salescall.cur_tenant() IS NULL AND salescall.cur_actor_kind() IN ('admin', 'system'));
CREATE POLICY scope ON salescall.person_states
  USING (salescall.cur_tenant() IS NULL AND salescall.cur_actor_kind() IN ('admin', 'system'))
  WITH CHECK (salescall.cur_tenant() IS NULL AND salescall.cur_actor_kind() = 'admin');
CREATE POLICY scope ON salescall.person_login_events
  USING (salescall.cur_tenant() IS NULL AND salescall.cur_actor_kind() IN ('admin', 'system'))
  WITH CHECK (salescall.cur_tenant() IS NULL AND salescall.cur_actor_kind() = 'system');
CREATE POLICY scope ON salescall.person_sessions
  USING (salescall.cur_tenant() IS NULL AND salescall.cur_actor_kind() = 'system')
  WITH CHECK (salescall.cur_tenant() IS NULL AND salescall.cur_actor_kind() = 'system');
CREATE POLICY scope ON salescall.person_session_ends
  USING (salescall.cur_tenant() IS NULL AND salescall.cur_actor_kind() = 'system')
  WITH CHECK (salescall.cur_tenant() IS NULL AND salescall.cur_actor_kind() = 'system');
GRANT SELECT, INSERT ON salescall.people, salescall.person_passwords, salescall.person_states, salescall.person_login_events,
  salescall.person_sessions, salescall.person_session_ends TO salescall_app;

-- ログインを作れるのは全体の取引の管理者だけ。管理者がまだ1人もいないときだけ、システムの処理が最初の管理者を作れる。
-- パスワードを替えられるのは、全体の取引の管理者と、ログインの確かめ（システムの処理＝本人がいまのパスワードを確かめたあと。core/people）だけ。
CREATE FUNCTION salescall.people_guard() RETURNS trigger LANGUAGE plpgsql SET search_path = salescall, pg_temp AS $$
BEGIN
  IF TG_TABLE_NAME = 'people' THEN
    IF NEW.created_by IS DISTINCT FROM salescall.cur_actor() THEN
      RAISE EXCEPTION '作った方は、いま操作している方です' USING ERRCODE = 'check_violation', CONSTRAINT = 'people_created_by';
    END IF;
    IF NOT (salescall.cur_actor_kind() = 'admin'
            OR (salescall.cur_actor_kind() = 'system' AND NEW.kind = 'admin'
                AND NOT EXISTS (SELECT 1 FROM salescall.people x WHERE x.kind = 'admin'))) THEN
      RAISE EXCEPTION 'ログインを作れるのは管理者だけです（最初の管理者だけは、サーバーを用意するときにシステムの処理が作ります）'
        USING ERRCODE = 'insufficient_privilege', CONSTRAINT = 'people_by_admin';
    END IF;
  ELSE
    IF NEW.set_by IS DISTINCT FROM salescall.cur_actor() THEN
      RAISE EXCEPTION '書いた方は、いま操作している方です' USING ERRCODE = 'check_violation', CONSTRAINT = 'people_set_by';
    END IF;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER people_guard BEFORE INSERT ON salescall.people FOR EACH ROW EXECUTE FUNCTION salescall.people_guard();
CREATE TRIGGER people_guard BEFORE INSERT ON salescall.person_passwords FOR EACH ROW EXECUTE FUNCTION salescall.people_guard();
CREATE TRIGGER people_guard BEFORE INSERT ON salescall.person_states FOR EACH ROW EXECUTE FUNCTION salescall.people_guard();

REVOKE ALL ON FUNCTION salescall.check_scope(), salescall.person_active(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.check_scope(), salescall.person_active(uuid) TO salescall_app;

-- 契約者を作るのは、全体の取引の管理者（契約者のサブスクの申し込みのあと）と、最初の管理者を作るシステムの処理（core/people.first_admin）。
-- 前は、まだ無い契約者の取引を開いて作る形だけだった（契約者の取引は、その契約者のログインでしか開けなくなるので、作る道を全体の取引に置く）
CREATE POLICY tenants_created_whole ON salescall.tenants FOR INSERT
  WITH CHECK (salescall.cur_tenant() IS NULL AND salescall.cur_actor_kind() IN ('admin', 'system'));

-- ログインの決まりの値（どれも全体の値。決め打ちにしない＝8章）
INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, finest_scope) VALUES
('login.max_failures', 'override', 'integer',
 '同じログインIDでパスワードを続けて誤ってよい回数。これを超えると、決めた長さ（login.lock_seconds）のあいだログインを断る（パスワードを当てずっぽうで試されないように）',
 'DESIGN.md 11章', 'system'),
('login.lock_seconds', 'override', 'seconds',
 'パスワードを続けて誤ったログインIDの、ログインを断る長さ', 'DESIGN.md 11章', 'system'),
('login.session_seconds', 'override', 'seconds',
 'ログインしてから、ログインし直しが要るまでの長さ（ログインの印の期限）', 'DESIGN.md 11章', 'system'),
('login.password_min_length', 'override', 'integer',
 'パスワードの最も短い文字数', 'DESIGN.md 11章', 'system');
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('login.max_failures', 'system', '5', 'provisional', '一般的なサイトの回数（CL-2 の初めの値）', '管理者・契約者の使い方を見て', 'CL-2'),
('login.lock_seconds', 'system', '900', 'provisional', '当てずっぽうを遅らせ、本人が待てる長さ＝15分（CL-2 の初めの値）', '管理者・契約者の使い方を見て', 'CL-2'),
('login.session_seconds', 'system', '43200', 'provisional', '1日の仕事のあいだ＝12時間（CL-2 の初めの値）', '管理者・契約者の使い方を見て', 'CL-2'),
('login.password_min_length', 'system', '12', 'provisional', 'お客様情報を含むので、長めの12文字（CL-2 の初めの値）', '管理者に伺う', 'CL-2');
