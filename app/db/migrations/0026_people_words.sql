-- 0026 断るときの文の、人の呼び方を直す（2026-10-16 ユーザー決定：人は管理者・サブスク契約者・依頼者・お客様のどれかで書く。
-- Intent.md 1-4）。書いた人：CL-2
-- - 関数の中身（何を断るか）は変えない。データベースが返す文だけを直す（画面に出るため）。
-- 0022_apology.sql の salescall.apology_approval_by_person と同じ中身で、断るときの文だけを直す
CREATE OR REPLACE FUNCTION salescall.apology_approval_by_person() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF salescall.cur_actor_kind() NOT IN ('operator', 'admin') OR NEW.approved_by IS DISTINCT FROM salescall.cur_actor() THEN
    RAISE EXCEPTION 'お詫びの電話を承認するのは管理者か契約者だけです（システムの処理は承認できません）' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'apology_approval_by_person';
  END IF;
  RETURN NEW;
END $$;

-- 0006_import_and_dnc.sql の salescall.dnc_before_change と同じ中身で、断るときの文だけを直す
CREATE OR REPLACE FUNCTION salescall.dnc_before_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION '発信禁止の登録は消せません（取り消しは記録つきで）' USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  IF OLD.revoked_at IS NOT NULL
     OR (NEW.tenant_id, NEW.client_id, NEW.entry_id, NEW.phone_e164, NEW.client_ref, NEW.reason, NEW.source_attempt_id,
         NEW.registered_at, NEW.registered_by)
        IS DISTINCT FROM
        (OLD.tenant_id, OLD.client_id, OLD.entry_id, OLD.phone_e164, OLD.client_ref, OLD.reason, OLD.source_attempt_id,
         OLD.registered_at, OLD.registered_by) THEN
    RAISE EXCEPTION '発信禁止の登録は、取り消すこと（1回）しかできません' USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  IF NEW.revoked_at IS NOT NULL AND salescall.cur_actor_kind() IS DISTINCT FROM 'operator' THEN
    RAISE EXCEPTION '発信禁止の取り消しは、管理者か契約者だけができます（システムの処理はできません）' USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN NEW;
END $$;

-- 0007_gate.sql の salescall.stop_event_before_insert と同じ中身で、断るときの文だけを直す
CREATE OR REPLACE FUNCTION salescall.stop_event_before_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE s salescall.stop_events;
BEGIN
  IF NEW.actor IS DISTINCT FROM salescall.cur_actor() OR NEW.actor_kind::text IS DISTINCT FROM salescall.cur_actor_kind() THEN
    RAISE EXCEPTION '停止の記録の「誰が」が、取引の「誰が」と違います' USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF NEW.action = 'resume' THEN
    SELECT * INTO s FROM salescall.stop_events WHERE event_id = NEW.resolves_event_id;
    IF NOT FOUND OR s.action <> 'stop' THEN
      RAISE EXCEPTION '解除が指す停止がありません' USING ERRCODE = 'check_violation';
    END IF;
    IF (s.scope, s.tenant_id, s.client_id, s.business_id, s.line_id) IS DISTINCT FROM
       (NEW.scope, NEW.tenant_id, NEW.client_id, NEW.business_id, NEW.line_id) THEN
      RAISE EXCEPTION '解除の範囲が、停止の範囲と違います' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.actor_kind = 'system' AND s.cause = 'manual' THEN
      RAISE EXCEPTION '管理者か契約者が押した緊急停止は、システムでは解けません' USING ERRCODE = 'insufficient_privilege';
    END IF;
  END IF;
  RETURN NEW;
END $$;

-- 0018_admin_system_scope.sql の salescall.system_dnc_by_admin と同じ中身で、断るときの文だけを直す
CREATE OR REPLACE FUNCTION salescall.system_dnc_by_admin() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF current_user <> 'salescall_owner' AND salescall.cur_actor_kind() IS DISTINCT FROM 'admin' THEN
    RAISE EXCEPTION 'システム共通の発信禁止に登録できるのは、管理者だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'system_dnc_by_admin';
  END IF;
  RETURN NEW;
END $$;

-- 0018_admin_system_scope.sql の salescall.system_manual_stop_by_admin と同じ中身で、断るときの文だけを直す
CREATE OR REPLACE FUNCTION salescall.system_manual_stop_by_admin() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.scope = 'system' AND current_user <> 'salescall_owner' AND salescall.cur_actor_kind() IS DISTINCT FROM 'admin'
     AND ((NEW.action = 'stop' AND NEW.cause = 'manual')
          OR (NEW.action = 'resume' AND (SELECT s.cause FROM salescall.stop_events s
                                         WHERE s.event_id = NEW.resolves_event_id) = 'manual')) THEN
    RAISE EXCEPTION '全体の緊急停止を止める・解くのは、管理者だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'system_manual_stop_by_admin';
  END IF;
  RETURN NEW;
END $$;

-- 0019_resume_by_person.sql の salescall.system_scope_by_admin と同じ中身で、断るときの文だけを直す
CREATE OR REPLACE FUNCTION salescall.system_scope_by_admin() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.scope = 'system' AND current_user <> 'salescall_owner'
     AND salescall.cur_actor_kind() IS DISTINCT FROM 'admin' AND salescall.cur_actor_kind() IS DISTINCT FROM 'system' THEN
    RAISE EXCEPTION '全体の範囲の停止を、管理者か契約者が止める・解くのは、管理者だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'system_scope_by_admin';
  END IF;
  RETURN NEW;
END $$;

-- 0017_admin.sql の salescall.system_values_by_admin と同じ中身で、断るときの文だけを直す
CREATE OR REPLACE FUNCTION salescall.system_values_by_admin() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF current_user <> 'salescall_owner' AND salescall.cur_actor_kind() IS DISTINCT FROM 'admin'
     AND (TG_TABLE_NAME = 'price_versions' OR to_jsonb(NEW) ->> 'scope' = 'system') THEN
    RAISE EXCEPTION 'システム全体の値を変えられるのは、管理者だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'system_values_by_admin';
  END IF;
  RETURN NEW;
END $$;

-- 0016_approval.sql の salescall.task_approval_by_operator と同じ中身で、断るときの文だけを直す
CREATE OR REPLACE FUNCTION salescall.task_approval_by_operator() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF salescall.cur_actor_kind() IS DISTINCT FROM 'operator' OR NEW.approved_by IS DISTINCT FROM salescall.cur_actor() THEN
    RAISE EXCEPTION '行を承認できるのは契約者だけです（承認した者は、取引の契約者）' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'task_approvals_by_operator';
  END IF;
  RETURN NEW;
END $$;
