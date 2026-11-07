-- 0205 「管理者か契約者か（人か）」を、誰が、の種類から決める1か所（2026-11-07。書いた人：CL-2。DESIGN.md 11章の【0205】・
--      2章 Q100・15-2 の620〜。⛔ はじめ 610〜 と書き、0203 の行と重なったので付け替えた）
--
-- データベースの監査で見つけた：0201 で「いま操作している方は管理者か契約者か」を cur_is_person の1か所にしたが、同じ組（operator と
-- admin）が、ほかにも書き写したまま残っていた。
--   - CHECK stop_events_manual_by_person（0018。緊急停止を押せるのは管理者か契約者）
--   - CHECK task_trouble_restores_actor_kind_check（0139。休んだ行を戻した記録の種類は管理者か契約者）
--   - stop_event_before_insert（0026。管理者か契約者が押した緊急停止は、システムの処理では解けない＝「人でない」を「system と同じ」と
--     書いていた。種類が増えると、人でない種類が緊急停止を解けてしまう）
--   - Python の core/health.py の BY（手で戻す・解く・外すのは管理者か契約者か）
-- また、pc_agents_guard（0105）の「払い出すのはシステムの処理か管理者」「止めるのは管理者か契約者かシステムの処理」は NOT IN で比べて
-- いたので、誰が、の種類の無い取引（NULL）を断らなかった（0201 で stop_emergency_pass_guard に見つけたのと同じ穴）。
--
-- (1) 種類から「人か」を返す1か所 is_person_kind（IMMUTABLE。種類が無ければ偽＝断る側）。cur_is_person・2つの CHECK・
--     stop_event_before_insert が読む。Python は、データベースの cur_is_person に問い合わせる（core/health.py の _by）。
-- (2) pc_agents_guard は、種類の無い取引を断る（coalesce で空の文字にしてから比べる）。
-- この組を、ほかに書き写していないことは試験が見張る（tests/test_foundation.PersonKinds。データベースの関数・CHECK・行の見え方の
-- 決まり・見方を全部見る）。Python 側は決まりの機械の見張り（checks/rules.py の person_kinds_only_in_the_database）。

-- ═════════ (1) 種類から「人か」（1か所） ═════════
CREATE FUNCTION salescall.is_person_kind(p_kind text) RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
  SELECT coalesce(p_kind IN ('operator', 'admin'), false)
$$;
REVOKE ALL ON FUNCTION salescall.is_person_kind(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.is_person_kind(text) TO salescall_app;

-- いま操作している方は管理者か契約者か（0201 の形から、種類の組を1か所へ移した）
CREATE OR REPLACE FUNCTION salescall.cur_is_person() RETURNS boolean LANGUAGE sql STABLE AS $$
  SELECT salescall.is_person_kind(salescall.cur_actor_kind())
$$;

-- 緊急停止を押せるのは管理者か契約者だけ（0018 の CHECK を、1か所を読む形にした。名前は同じ）
ALTER TABLE salescall.stop_events DROP CONSTRAINT stop_events_manual_by_person;
ALTER TABLE salescall.stop_events
  ADD CONSTRAINT stop_events_manual_by_person CHECK (cause IS DISTINCT FROM 'manual' OR salescall.is_person_kind(actor_kind::text));

-- 休んだ行を戻した記録は、管理者か契約者のもの（0139 の CHECK を、1か所を読む形にした。名前は同じ）
ALTER TABLE salescall.task_trouble_restores DROP CONSTRAINT task_trouble_restores_actor_kind_check;
ALTER TABLE salescall.task_trouble_restores
  ADD CONSTRAINT task_trouble_restores_actor_kind_check CHECK (salescall.is_person_kind(actor_kind::text));

-- salescall.stop_event_before_insert（0026_people_words.sql の形から。「人でない」を system と比べず、1か所を読む）
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
    IF NOT salescall.is_person_kind(NEW.actor_kind::text) AND s.cause = 'manual' THEN
      RAISE EXCEPTION '管理者か契約者が押した緊急停止は、システムでは解けません' USING ERRCODE = 'insufficient_privilege';
    END IF;
  END IF;
  RETURN NEW;
END $$;

-- ═════════ (2) PC の通行証の守りが、種類の無い取引を断る ═════════
-- salescall.pc_agents_guard（0105_pc_agents.sql の形から。比べる前に、種類の無い取引を空の文字にする＝どの組にも入らず断る）
CREATE OR REPLACE FUNCTION salescall.pc_agents_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF current_user = 'salescall_owner' THEN
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
  END IF;
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'PC の通行証は消せません（止めた印を付けます）' USING ERRCODE = 'check_violation';
  END IF;
  IF TG_OP = 'INSERT' THEN
    IF coalesce(salescall.cur_actor_kind(), '') NOT IN ('system', 'admin') THEN
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
  IF coalesce(salescall.cur_actor_kind(), '') NOT IN ('system', 'admin', 'operator') THEN
    RAISE EXCEPTION 'PC の通行証を止めるのは、管理者か契約者かシステムの処理だけです' USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN NEW;
END $$;
