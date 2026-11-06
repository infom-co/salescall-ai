-- 0190 サブは約束の電話だけ・契約プランを書けるのは管理者だけ（2026-11-06 ユーザーのお答え。書いた人：CL-2。DESIGN.md 3-4-1・8章）
--
-- (1) ✅ 2026-11-06 ユーザー（伺い「サブの範囲」）：「約束の電話だけ」。番号を直したあと、前の番号でお客様と話せたら前の番号をサブにするのは、
--     お客様と約束したかけ直しの電話（目的 normal・約束の印）だけ（計画書 7-1 の (5)「かけ直しの約束をしていて、旧番号でつながった場合」）。
--     続きの電話・番号の聞き直し・お詫び・確かめのかけ直しなどで話せても、前の番号は灰色のまま、かけない。
--     ⛔ 前（0182・0188）は、話せた電話の種類を見ずにサブにしていた。
--     その通話がサブを作るかを決めるのは salescall.call_makes_sub の1か所（守り＝task_sub_number_guard と、書く所＝tasks.record_sub_number の
--     両方がこれを読む。前は同じ条件を2か所に書いていた）。
-- (2) ✅ 2026-11-06 ユーザー（伺い「プランの切替」）：「管理者だけ」。契約プラン（ai.plan_conversation・ai.plan_import）は契約者ごとの値の
--     まま、書けるのは管理者だけ（料金に関わる）。設定の定義に「管理者だけが書ける」印 admin_writes を足し、どの範囲の値でも守る
--     （守りはシステム全体の値の守り system_values_by_admin の1か所）。

-- ═════════ (1) ═════════
CREATE FUNCTION salescall.call_makes_sub(p_attempt uuid) RETURNS boolean LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT coalesce((SELECT a.purpose = 'normal' AND a.promised                                  -- お客様と約束したかけ直しの電話
                          AND a.claimed_at >= salescall.task_count_from(a.task_id)              -- いちばん新しい直しのあとの通話
                          AND salescall.task_number_replaced_at(a.task_id, a.contact_id, a.claimed_at)   -- かけたとき前の番号
                   FROM salescall.attempts a WHERE a.attempt_id = p_attempt), false)
$$;
REVOKE ALL ON FUNCTION salescall.call_makes_sub(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.call_makes_sub(uuid) TO salescall_app;

CREATE OR REPLACE FUNCTION salescall.task_sub_number_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM salescall.attempts a WHERE a.attempt_id = NEW.attempt_id AND a.task_id = NEW.task_id
                   AND a.contact_id = NEW.contact_id)
     OR NOT salescall.call_makes_sub(NEW.attempt_id) THEN
    RAISE EXCEPTION 'サブの番号にできるのは、直したあと前にかける番号だった番号へかけた、お客様との約束の電話の番号だけです'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'task_sub_number_of_call';
  END IF;
  RETURN NEW;
END $$;

-- ═════════ (2) ═════════
ALTER TABLE salescall.setting_definitions ADD COLUMN admin_writes boolean NOT NULL DEFAULT false;
ALTER TABLE salescall.setting_definitions DISABLE TRIGGER setting_definitions_frozen;
UPDATE salescall.setting_definitions SET admin_writes = true WHERE key IN ('ai.plan_conversation', 'ai.plan_import');
ALTER TABLE salescall.setting_definitions ENABLE TRIGGER setting_definitions_frozen;

CREATE OR REPLACE FUNCTION salescall.system_values_by_admin() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF current_user <> 'salescall_owner' AND salescall.cur_actor_kind() IS DISTINCT FROM 'admin'
     AND ((TG_TABLE_NAME = 'price_versions' AND to_jsonb(NEW) ->> 'tenant_id' IS NULL)
          OR to_jsonb(NEW) ->> 'scope' = 'system') THEN
    RAISE EXCEPTION 'システム全体の値（すべての契約者に効く初めの値を含む）を変えられるのは、管理者だけです'
      USING ERRCODE = 'insufficient_privilege', CONSTRAINT = 'system_values_by_admin';
  END IF;
  -- 管理者だけが書ける設定（契約プランなど。どの範囲の値でも）
  IF current_user <> 'salescall_owner' AND TG_TABLE_NAME = 'setting_values'
     AND salescall.cur_actor_kind() IS DISTINCT FROM 'admin'
     AND EXISTS (SELECT 1 FROM salescall.setting_definitions d WHERE d.key = to_jsonb(NEW) ->> 'key' AND d.admin_writes) THEN
    RAISE EXCEPTION 'この設定（%）を変えられるのは、管理者だけです', to_jsonb(NEW) ->> 'key'
      USING ERRCODE = 'insufficient_privilege', CONSTRAINT = 'setting_admin_writes';
  END IF;
  IF current_user <> 'salescall_owner' AND TG_TABLE_NAME = 'price_versions'
     AND salescall.cur_actor_kind() IS DISTINCT FROM 'admin' AND salescall.cur_actor_kind() IS DISTINCT FROM 'operator' THEN
    RAISE EXCEPTION '単価を入れられるのは、管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'system_values_by_admin';
  END IF;
  RETURN NEW;
END $$;
