-- 0017 システムの管理者（CL-3 の 4-1f の3。Intent.md 5-9・S-21）。書いた人：CL-2
-- システムの値（設定のシステム全体の値・単価の表）は、すべての契約者に効く。変えられるのはシステムの管理者（actor_kind = 'admin'。
-- いまはユーザー）だけにし、契約者の利用者（運営者）やシステムの処理には変えさせない（画面で隠すだけでなく、データベースが守る）。
-- 表の作りを流す処理（表の持ち主の役）は、最初の値を入れるので除く。
CREATE FUNCTION salescall.system_values_by_admin() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF current_user <> 'salescall_owner' AND salescall.cur_actor_kind() IS DISTINCT FROM 'admin'
     AND (TG_TABLE_NAME = 'price_versions' OR to_jsonb(NEW) ->> 'scope' = 'system') THEN
    RAISE EXCEPTION 'システム全体の値を変えられるのは、システムの管理者だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'system_values_by_admin';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER setting_values_system_by_admin BEFORE INSERT ON salescall.setting_values
  FOR EACH ROW EXECUTE FUNCTION salescall.system_values_by_admin();
CREATE TRIGGER price_versions_by_admin BEFORE INSERT ON salescall.price_versions
  FOR EACH ROW EXECUTE FUNCTION salescall.system_values_by_admin();
