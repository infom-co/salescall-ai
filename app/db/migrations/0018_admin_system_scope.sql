-- 0018 システム全体の緊急停止とシステム共通の発信禁止は、システムの管理者だけ（2026-10-15 ユーザー決定。
-- 「管理者（サブスクでは契約者）だけ」＝全体に効くものはシステムの管理者、契約者の範囲に効くものは、その契約者）。書いた人：CL-2
-- - 人が押す緊急停止：全体の範囲で止める・解くのはシステムの管理者だけ。契約者・依頼主・業務・回線の範囲は、その契約者の運営者
--   （システムの管理者も人なので、押せる）。システムの処理は、いままでどおり人の緊急停止を解けない。
-- - システム共通の発信禁止（すべての契約者に効く）に登録できるのは、システムの管理者だけ。
-- 表の作りを流す処理（表の持ち主の役）は除く。
ALTER TABLE salescall.stop_events DROP CONSTRAINT stop_events_manual_by_operator;
ALTER TABLE salescall.stop_events
  ADD CONSTRAINT stop_events_manual_by_person CHECK (cause IS DISTINCT FROM 'manual' OR actor_kind IN ('operator', 'admin'));

CREATE FUNCTION salescall.system_manual_stop_by_admin() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.scope = 'system' AND current_user <> 'salescall_owner' AND salescall.cur_actor_kind() IS DISTINCT FROM 'admin'
     AND ((NEW.action = 'stop' AND NEW.cause = 'manual')
          OR (NEW.action = 'resume' AND (SELECT s.cause FROM salescall.stop_events s
                                         WHERE s.event_id = NEW.resolves_event_id) = 'manual')) THEN
    RAISE EXCEPTION '全体の緊急停止を止める・解くのは、システムの管理者だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'system_manual_stop_by_admin';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER system_manual_stop_by_admin BEFORE INSERT ON salescall.stop_events
  FOR EACH ROW EXECUTE FUNCTION salescall.system_manual_stop_by_admin();

CREATE FUNCTION salescall.system_dnc_by_admin() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF current_user <> 'salescall_owner' AND salescall.cur_actor_kind() IS DISTINCT FROM 'admin' THEN
    RAISE EXCEPTION 'システム共通の発信禁止に登録できるのは、システムの管理者だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'system_dnc_by_admin';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER system_dnc_by_admin BEFORE INSERT ON salescall.system_dnc_entries
  FOR EACH ROW EXECUTE FUNCTION salescall.system_dnc_by_admin();
