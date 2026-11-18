-- 0032 契約者ごとのもの・共有するもの（2026-10-16 ユーザー）。書いた人：CL-2
-- ユーザー：「システム共通の発信禁止　契約者／単価　依頼者タブ／祝日の暦　管理者／会話AIの枠　契約者／契約者をまたいで同じ番号に
--   かける間隔　契約者の設定で、『他のこのシステム契約者と発信がダブった場合の間隔』○○時間、○○日、かけない」。
--   あわせて「契約者に開くときは、データベースを契約者ごとに分け、共通の部分は共有する」（Intent.md 1-4）。
-- (1) 共通の発信禁止は、契約者ごと（その契約者のすべての依頼者に効く）。登録も取り消しも、その契約者（か管理者）が記録つきで。
--     前のシステム共通の発信禁止（全契約者に効き、管理者だけが登録し、取り消す道が無かった）は外す。
-- (2) 会話AIの枠は契約者ごと（契約者が自分のキー・プロジェクトを持つ）。数えるのは、その契約者の進行中の発信だけ。値は契約者が決める
--     （システムの値は初めの値）。
-- (3) ほかの契約者と同じ番号にかけた場合の間隔は、契約者の設定（○○時間・○○日・かけない）。同じ契約者の中で依頼者をまたいだ間隔は、
--     これまでどおりシステムの設定（gate.cross_client_interval）。
-- (4) 祝日の暦は管理者（共有）＝これまでどおり。単価は、ユーザーに意味を確かめてから（⏳）。

CREATE OR REPLACE FUNCTION salescall.setting_value_ok(t salescall.setting_value_type, v jsonb) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE t
    WHEN 'boolean' THEN jsonb_typeof(v) = 'boolean'
    WHEN 'integer' THEN salescall.setting_whole_number(v, 0)
    WHEN 'seconds' THEN salescall.setting_whole_number(v, 1)
    WHEN 'ratio'   THEN CASE WHEN jsonb_typeof(v) <> 'number' THEN false
                             ELSE (v #>> '{}')::numeric BETWEEN 0 AND 1 END
    WHEN 'integer_list' THEN CASE WHEN jsonb_typeof(v) <> 'array' THEN false
                                  WHEN jsonb_array_length(v) < 1 THEN false
                                  ELSE NOT EXISTS (SELECT 1 FROM jsonb_array_elements(v) e
                                                   WHERE NOT salescall.setting_whole_number(e, 1)) END
    -- 曜日ごとの時間帯（日本時間）。例：{"mon": [["09:00","18:00"]], ..., "sun": [], "holiday": []}。8つの鍵が全部要る
    WHEN 'weekly_hours' THEN CASE WHEN jsonb_typeof(v) <> 'object' THEN false
                                  WHEN (SELECT count(*) FROM jsonb_object_keys(v)) <> 8 THEN false
                                  WHEN NOT (v ?& ARRAY['mon','tue','wed','thu','fri','sat','sun','holiday']) THEN false
                                  ELSE NOT EXISTS (SELECT 1 FROM jsonb_each(v) d WHERE NOT salescall.setting_hours_ok(d.value)) END
    -- 1日の中の時刻の範囲（日本時間）。例：["09:00","19:00"]。終わりが始まりより前なら日をまたぐ（["18:00","03:00"]＝翌3時まで）
    WHEN 'daily_range' THEN CASE WHEN jsonb_typeof(v) <> 'array' THEN false
                                 WHEN jsonb_array_length(v) <> 2 THEN false
                                 WHEN jsonb_typeof(v -> 0) <> 'string' OR jsonb_typeof(v -> 1) <> 'string' THEN false
                                 WHEN NOT ((v ->> 0) ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$') THEN false
                                 WHEN NOT ((v ->> 1) ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$') THEN false
                                 ELSE (v ->> 0) <> (v ->> 1) END
    -- 1日を分ける区切りの時（例：[12, 15]＝午前・昼過ぎ・夕方）。1〜23 の整数を、小さい順に重ならずに
    WHEN 'hour_list' THEN CASE WHEN jsonb_typeof(v) <> 'array' THEN false
                               WHEN jsonb_array_length(v) < 1 THEN false
                               WHEN EXISTS (SELECT 1 FROM jsonb_array_elements(v) e
                                            WHERE NOT salescall.setting_whole_number(e, 1) OR (e #>> '{}')::numeric > 23) THEN false
                               ELSE NOT EXISTS (SELECT 1 FROM jsonb_array_elements(v) WITH ORDINALITY a(e, i)
                                                JOIN jsonb_array_elements(v) WITH ORDINALITY b(e, i) ON b.i = a.i + 1
                                                WHERE (b.e #>> '{}')::numeric <= (a.e #>> '{}')::numeric) END
    -- 間隔か、かけない（2026-10-16 ユーザー：「○○時間、○○日、かけない」）。例：{"hours": 6}・{"days": 3}・"never"
    WHEN 'gap_or_never' THEN CASE WHEN v = '"never"'::jsonb THEN true
                                  WHEN jsonb_typeof(v) <> 'object' THEN false
                                  WHEN (SELECT count(*) FROM jsonb_object_keys(v)) <> 1 THEN false
                                  WHEN v ? 'hours' THEN salescall.setting_whole_number(v -> 'hours', 1)
                                  WHEN v ? 'days' THEN salescall.setting_whole_number(v -> 'days', 1)
                                  ELSE false END
  END
$$;

-- ═════════ (1) 契約者の共通の発信禁止 ═════════
DROP TRIGGER system_dnc_by_admin ON salescall.system_dnc_entries;
DROP FUNCTION salescall.system_dnc_by_admin();
DROP FUNCTION salescall.release_claims_for_system_dnc(text);
DROP TABLE salescall.system_dnc_entries;

CREATE TABLE salescall.contractor_dnc_entries (   -- 契約者の共通の発信禁止（その契約者のすべての依頼者に効く。消さない）
  tenant_id     uuid NOT NULL REFERENCES salescall.tenants,
  entry_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  phone_e164    text NOT NULL CHECK (phone_e164 ~ '^\+[1-9][0-9]{6,14}$'),
  reason        text NOT NULL CHECK (btrim(reason) <> ''),
  registered_at timestamptz NOT NULL DEFAULT now(),
  registered_by text NOT NULL CHECK (btrim(registered_by) <> ''),
  revoked_at    timestamptz,
  revoked_by    text,
  revoke_reason text,
  CHECK ((revoked_at IS NULL) = (revoked_by IS NULL) AND (revoked_at IS NULL) = (revoke_reason IS NULL)),
  CHECK (revoke_reason IS NULL OR btrim(revoke_reason) <> '')
);
CREATE UNIQUE INDEX contractor_dnc_one_active ON salescall.contractor_dnc_entries (tenant_id, phone_e164) WHERE revoked_at IS NULL;
-- 登録も取り消しも、管理者か契約者だけ（システムの処理はしない）。取り消しは記録つきで1回だけ。消さない
CREATE FUNCTION salescall.contractor_dnc_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION '発信禁止の登録は消せません（取り消しは記録つきで）' USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  IF TG_OP = 'UPDATE' AND (OLD.revoked_at IS NOT NULL
     OR (NEW.tenant_id, NEW.entry_id, NEW.phone_e164, NEW.reason, NEW.registered_at, NEW.registered_by)
        IS DISTINCT FROM (OLD.tenant_id, OLD.entry_id, OLD.phone_e164, OLD.reason, OLD.registered_at, OLD.registered_by)) THEN
    RAISE EXCEPTION '発信禁止の登録は、取り消すこと（1回）しかできません' USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  IF salescall.cur_actor_kind() IS DISTINCT FROM 'operator' AND salescall.cur_actor_kind() IS DISTINCT FROM 'admin' THEN
    RAISE EXCEPTION '契約者の共通の発信禁止を登録・取り消しできるのは、管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'contractor_dnc_by_person';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER contractor_dnc_guard BEFORE INSERT OR UPDATE OR DELETE ON salescall.contractor_dnc_entries
  FOR EACH ROW EXECUTE FUNCTION salescall.contractor_dnc_guard();
ALTER TABLE salescall.contractor_dnc_entries ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON salescall.contractor_dnc_entries
  USING (tenant_id = salescall.cur_tenant()) WITH CHECK (tenant_id = salescall.cur_tenant());
GRANT SELECT, INSERT, UPDATE ON salescall.contractor_dnc_entries TO salescall_app;

-- 契約者の共通の発信禁止に番号を入れたとき、その契約者のすべての依頼者の、その番号のまだ発信していない予約を取り消す
-- （取引の依頼者の範囲の外も取り消すので、表の持ち主の権限で動く。できることは「その契約者の、その番号の予約を released に」だけ）
CREATE FUNCTION salescall.release_claims_for_contractor_dnc(p_phone text) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
DECLARE n integer;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM salescall.contractor_dnc_entries
                 WHERE tenant_id = salescall.cur_tenant() AND phone_e164 = p_phone AND revoked_at IS NULL) THEN
    RAISE EXCEPTION 'この契約者の共通の発信禁止に無い番号です' USING ERRCODE = 'check_violation';
  END IF;
  UPDATE salescall.attempts SET state = 'released'
  WHERE phone_e164 = p_phone AND tenant_id = salescall.cur_tenant() AND state = 'claimed';
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$;
REVOKE ALL ON FUNCTION salescall.release_claims_for_contractor_dnc(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.release_claims_for_contractor_dnc(text) TO salescall_app;

-- ═════════ (2) 会話AIの枠は契約者ごと ═════════
DROP FUNCTION salescall.active_attempts_total();
CREATE FUNCTION salescall.active_attempts_of_tenant() RETURNS integer    -- その契約者の、進行中の発信の数（契約者の枠で数える）
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT count(*)::integer FROM salescall.attempts
  WHERE tenant_id = salescall.cur_tenant() AND salescall.is_live(state, claim_expires_at)
$$;
REVOKE ALL ON FUNCTION salescall.active_attempts_of_tenant() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.active_attempts_of_tenant() TO salescall_app;
-- 枠の値（1分あたりのトークン数の上限・1本の見込み・余裕の割合）は、契約者が決められる（システムの値は初めの値）。
-- 設定の定義は変えない決まり（変更の拒み）なので、表の持ち主が、この移行の中でだけ拒みを外して直す
ALTER TABLE salescall.setting_definitions DISABLE TRIGGER setting_definitions_frozen;
UPDATE salescall.setting_definitions SET merge_kind = 'override',
  meaning = meaning || '（契約者ごと。契約者が自分のキー・プロジェクトの値を決める。2026-10-16 ユーザー）'
WHERE key IN ('gate.ai_tpm_limit', 'gate.ai_tpm_per_call', 'gate.ai_tpm_safety');
UPDATE salescall.setting_definitions SET meaning = '同じ契約者の中で、依頼者をまたいで、同じ番号へかけるまでの最小の間隔'
WHERE key = 'gate.cross_client_interval';
ALTER TABLE salescall.setting_definitions ENABLE TRIGGER setting_definitions_frozen;

-- ═════════ (3) ほかの契約者と同じ番号にかけた場合の間隔（契約者の設定） ═════════
INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, bound_key, bound_side) VALUES
('gate.other_contractor_interval', 'override', 'gap_or_never',
 'ほかの契約者（このシステムを使うほかの契約者）が同じ番号へかけた場合に、空ける間隔（{"hours": n}・{"days": n}）か、かけない（"never"）。契約者が決める（2026-10-16 ユーザー）',
 'DESIGN.md 5-2 の5', NULL, NULL);
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('gate.other_contractor_interval', 'system', '{"days": 3}', 'provisional',
 '契約者が決めるまでの初めの値。これまでの「依頼者をまたいだ間隔」と同じ3日（別々の契約者から同じ会社へ、続けて営業の電話がかからないよう）',
 '契約者に開くとき', 'CL-2');

-- かけない（"never"）のときに使う：ほかの契約者が、その番号へかけたことがあるか（中身は出さない。あるかだけ）
CREATE FUNCTION salescall.number_dialed_by_other_tenant(p_phone text) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT EXISTS (SELECT 1 FROM salescall.attempts a WHERE a.phone_e164 = p_phone AND a.tenant_id <> salescall.cur_tenant()
                 AND a.state NOT IN ('claimed', 'released'))
$$;
REVOKE ALL ON FUNCTION salescall.number_dialed_by_other_tenant(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.number_dialed_by_other_tenant(text) TO salescall_app;
