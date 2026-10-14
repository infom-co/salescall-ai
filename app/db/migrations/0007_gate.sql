-- 0007 発信の門と出口（DESIGN.md 5章・Q2・Q12・Q13）。書いた人：CL-2（2026-10-14）
-- - 門が読む材料：回線ごとの停止・祝日の暦・業務の版の段（品質の段 Q22）・依頼主をまたいだ番号の状態（中身を出さない確かめだけ）。
-- - 門の不許可は記録する（画面で理由ごとに数える。CL-3 のお願い）。
-- - 「その番号へ最後にかけた時刻」は試行の記録から数える（番号の台帳は持たない。15-2 の33）。

-- ═════════ 回線ごとの停止（9-1：回線の異常は、その回線だけを止める） ═════════
ALTER TABLE salescall.stop_events
  ADD COLUMN line_id uuid,
  ADD CONSTRAINT stop_events_line_fkey FOREIGN KEY (tenant_id, line_id) REFERENCES salescall.lines (tenant_id, line_id),
  DROP CONSTRAINT stop_events_scope_target,
  ADD CONSTRAINT stop_events_scope_target CHECK (
       (scope = 'system'   AND tenant_id IS NULL     AND client_id IS NULL     AND business_id IS NULL     AND line_id IS NULL)
    OR (scope = 'tenant'   AND tenant_id IS NOT NULL AND client_id IS NULL     AND business_id IS NULL     AND line_id IS NULL)
    OR (scope = 'client'   AND tenant_id IS NOT NULL AND client_id IS NOT NULL AND business_id IS NULL     AND line_id IS NULL)
    OR (scope = 'business' AND tenant_id IS NOT NULL AND client_id IS NOT NULL AND business_id IS NOT NULL AND line_id IS NULL)
    OR (scope = 'line'     AND tenant_id IS NOT NULL AND client_id IS NULL     AND business_id IS NULL     AND line_id IS NOT NULL));

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
      RAISE EXCEPTION '人が押した緊急停止は、システムでは解けません' USING ERRCODE = 'insufficient_privilege';
    END IF;
  END IF;
  RETURN NEW;
END $$;

DROP POLICY stop_scope ON salescall.stop_events;
CREATE POLICY stop_scope ON salescall.stop_events
  USING (scope = 'system'
         OR (scope IN ('tenant', 'line') AND tenant_id = salescall.cur_tenant())
         OR (scope IN ('client', 'business') AND tenant_id = salescall.cur_tenant()
             AND client_id = salescall.cur_client()))
  WITH CHECK ((scope = 'system' AND salescall.cur_tenant() IS NULL)
              OR (scope IN ('tenant', 'line') AND tenant_id = salescall.cur_tenant())
              OR (scope IN ('client', 'business') AND tenant_id = salescall.cur_tenant()
                  AND client_id = salescall.cur_client()));

DROP FUNCTION salescall.open_stops(uuid, uuid, uuid);
-- まだ解かれていない停止（Q13 の答え）。p_line を渡すと、その回線の停止も含める
CREATE FUNCTION salescall.open_stops(p_tenant uuid, p_client uuid, p_business uuid, p_line uuid DEFAULT NULL)
RETURNS SETOF salescall.stop_events LANGUAGE sql STABLE AS $$
  SELECT s.* FROM salescall.stop_events s
  WHERE s.action = 'stop'
    AND NOT EXISTS (SELECT 1 FROM salescall.stop_events r WHERE r.resolves_event_id = s.event_id)
    AND (s.scope = 'system'
         OR (s.scope = 'tenant' AND s.tenant_id = p_tenant)
         OR (s.scope = 'client' AND s.tenant_id = p_tenant AND s.client_id = p_client)
         OR (s.scope = 'business' AND s.tenant_id = p_tenant AND s.client_id = p_client AND s.business_id = p_business)
         OR (s.scope = 'line' AND s.tenant_id = p_tenant AND s.line_id = p_line))
$$;

CREATE OR REPLACE FUNCTION salescall.release_claims_for_stop(p_event_id bigint) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
DECLARE s salescall.stop_events; n integer;
BEGIN
  SELECT * INTO s FROM salescall.stop_events e WHERE e.event_id = p_event_id AND e.action = 'stop'
    AND NOT EXISTS (SELECT 1 FROM salescall.stop_events r WHERE r.resolves_event_id = e.event_id);
  IF NOT FOUND THEN
    RAISE EXCEPTION 'まだ解かれていない、その停止はありません' USING ERRCODE = 'check_violation';
  END IF;
  UPDATE salescall.attempts a SET state = 'released'
  WHERE a.state = 'claimed'
    AND (s.scope = 'system'
         OR (s.scope = 'tenant' AND a.tenant_id = s.tenant_id)
         OR (s.scope = 'client' AND a.client_id = s.client_id)
         OR (s.scope = 'business' AND a.client_id = s.client_id
             AND a.version_id IN (SELECT v.version_id FROM salescall.business_versions v WHERE v.business_id = s.business_id))
         OR (s.scope = 'line' AND a.line_id = s.line_id));
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$;
REVOKE ALL ON FUNCTION salescall.release_claims_for_stop(bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.release_claims_for_stop(bigint), salescall.open_stops(uuid, uuid, uuid, uuid)
  TO salescall_app;

-- ═════════ 祝日の暦（Q12。5-2-1 の5：持っている範囲の外の日は「判断できない」） ═════════
-- 正本は内閣府の公式の一覧（CSV）。取り込むのは core/calendar.py だけ。取り込みは追記だけで、年ごとに「その年を含む
-- いちばん新しい取り込み」が効く（法改正などの直しは、取り込み直しで行う。前の記録は残る）。
CREATE TABLE salescall.calendar_loads (
  load_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  file_sha256 text NOT NULL CHECK (file_sha256 ~ '^[0-9a-f]{64}$'),
  first_year  integer NOT NULL,
  last_year   integer NOT NULL CHECK (last_year >= first_year),
  loaded_at   timestamptz NOT NULL DEFAULT now(),
  loaded_by   text NOT NULL CHECK (btrim(loaded_by) <> '')
);
CREATE TABLE salescall.calendar_holidays (
  load_id bigint NOT NULL REFERENCES salescall.calendar_loads,
  day     date NOT NULL,
  name    text NOT NULL CHECK (btrim(name) <> ''),
  PRIMARY KEY (load_id, day)
);
CREATE TRIGGER calendar_loads_frozen BEFORE UPDATE OR DELETE ON salescall.calendar_loads
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
CREATE TRIGGER calendar_holidays_frozen BEFORE UPDATE OR DELETE ON salescall.calendar_holidays
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
GRANT SELECT, INSERT ON salescall.calendar_loads, salescall.calendar_holidays TO salescall_app;

-- ═════════ 業務の版の段（品質の段 Q22。DESIGN.md 10-1） ═════════
-- 段を開くのは品質の段の処理だけ（core/quality_stage.py）。門は「いまの段の件数の内側か」を読むだけ。
-- 記録が無い版は、まだかけられない（模擬通話の合格・最初の段を開く前）。cap が NULL の段は「全部」。
CREATE TABLE salescall.version_stages (
  tenant_id  uuid NOT NULL,
  client_id  uuid NOT NULL,
  stage_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,   -- 版ごとの最新（いちばん大きい番号）が、いまの段
  version_id uuid NOT NULL,
  stage_no   integer NOT NULL CHECK (stage_no >= 1),
  cap        integer CHECK (cap IS NULL OR cap >= 1),          -- この段で会話に至ってよい通話の数（NULL＝全部）
  evidence   text NOT NULL CHECK (btrim(evidence) <> ''),      -- 開いた根拠（模擬通話の結果・前の段の指標）
  opened_at  timestamptz NOT NULL DEFAULT now(),
  opened_by  text NOT NULL CHECK (btrim(opened_by) <> ''),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, version_id) REFERENCES salescall.business_versions (client_id, version_id)
);
CREATE INDEX version_stages_current ON salescall.version_stages (version_id, stage_id DESC);

-- ═════════ 門の不許可の記録（画面で理由ごとに数える。5-2-1 の11） ═════════
CREATE TABLE salescall.gate_denials (
  tenant_id  uuid NOT NULL,
  client_id  uuid NOT NULL,
  denial_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  task_id    uuid NOT NULL,
  line_id    uuid,
  reason     text NOT NULL CHECK (reason ~ '^[a-z_]+$'),         -- 理由の名前（一覧の正本は core/gate.py の REASONS）
  denied_at  timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, task_id) REFERENCES salescall.tasks (client_id, task_id),
  FOREIGN KEY (tenant_id, line_id) REFERENCES salescall.lines (tenant_id, line_id)
);
CREATE INDEX gate_denials_by_client ON salescall.gate_denials (client_id, denied_at);

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['version_stages', 'gate_denials'] LOOP
    EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON salescall.%I
                      FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change()', t || '_frozen', t);
    EXECUTE format('ALTER TABLE salescall.%I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY client_scope ON salescall.%I
                      USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
                      WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())', t);
  END LOOP;
END $$;
GRANT SELECT, INSERT ON salescall.version_stages, salescall.gate_denials TO salescall_app;

-- ═════════ 日本時間（時間帯・日ごとの上限は、すべて日本時間の日で数える。直し方はここ1か所） ═════════
CREATE FUNCTION salescall.jst(t timestamptz) RETURNS timestamp LANGUAGE sql IMMUTABLE AS
$$ SELECT t AT TIME ZONE 'Asia/Tokyo' $$;
-- 日本時間のその日の始まり（0:00）の時刻
CREATE FUNCTION salescall.jst_day_start(d date) RETURNS timestamptz LANGUAGE sql IMMUTABLE AS
$$ SELECT d::timestamp AT TIME ZONE 'Asia/Tokyo' $$;
GRANT EXECUTE ON FUNCTION salescall.jst(timestamptz), salescall.jst_day_start(date) TO salescall_app;

-- ═════════ 「いま生きている」進行中の試行（同時の本数・段の件数に数えるもの） ═════════
-- 期限の切れた予約は、出口で発信へ進めない（5-3）ので、取り消される前でも数えない（数えると、ほかの相手への発信まで止める）。
-- ⚠️ 部分一意（3-1）は、期限切れの予約も進行中として扱う（門が、その相手・番号・回線の分を取引の最初に取り消す）。
CREATE FUNCTION salescall.is_live(s salescall.attempt_state, expires timestamptz) RETURNS boolean LANGUAGE sql STABLE AS
$$ SELECT salescall.is_active(s) AND NOT (s = 'claimed' AND expires <= now()) $$;
GRANT EXECUTE ON FUNCTION salescall.is_live(salescall.attempt_state, timestamptz) TO salescall_app;

-- ═════════ 相手の意思と関係なく、繋がらなかった・切れた発信（2026-10-14 ユーザー決定） ═════════
-- 「かけた回数」に数えない（行の回数の上限・同じ相手への最小の間隔・1日の回数のどれも。数え方は1つ）：通信の問題などで繋がらなかった（発信に失敗・相手が出る前の結果不明）／
-- 相手の意思と関係なく切れた（回線の側で切れた）。「切れ方が不明」は数える（相手が切ったかもしれない＝止める側）。
CREATE FUNCTION salescall.not_by_callee(s salescall.attempt_state, side salescall.hangup_side, p_attempt uuid)
RETURNS boolean LANGUAGE sql STABLE AS $$
  SELECT s = 'call_failed'
      OR (s = 'technical_error' AND NOT salescall.reached_answer(p_attempt))
      OR (s = 'dropped' AND side = 'line')
$$;
GRANT EXECUTE ON FUNCTION salescall.not_by_callee(salescall.attempt_state, salescall.hangup_side, uuid),
  salescall.reached_answer(uuid) TO salescall_app;

-- ═════════ 次の予定が「相手と約束したかけ直し」か（相手に頼まれた・こちらが約束した。2026-10-14 ユーザー決定） ═════════
-- 約束したかけ直しは、行の回数の上限・同じ相手への最小の間隔・1日の回数の対象外（門が読む）。決めるのは次の動きだけ（Q4）。
ALTER TABLE salescall.task_next_actions
  ADD COLUMN promised boolean,
  ADD CONSTRAINT task_next_actions_promised CHECK ((kind = 'call') = (promised IS NOT NULL));

-- ═════════ 門だけが使う、依頼主の範囲の外を見る確かめ（中身は返さない。表の持ち主の権限で動く） ═════════
-- どれも、取引の契約者・依頼主（cur_tenant・cur_client）と比べた「関係」だけを返す。ほかの依頼主の行の中身・IDは返さない。

-- 期限切れの予約を戻す（5-2-1 の3）。その連絡先・番号・回線の分だけ（ほかの依頼主の予約も、期限切れなら戻してよい）
CREATE FUNCTION salescall.release_expired_claims(p_contact uuid, p_phone text, p_line uuid) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
DECLARE n integer;
BEGIN
  UPDATE salescall.attempts SET state = 'released'
  WHERE state = 'claimed' AND claim_expires_at <= now()
    AND (contact_id = p_contact OR phone_e164 = p_phone OR line_id = p_line);
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$;

-- 門の判定に効く変更（設定の値・祝日の暦）をした取引の中で、その範囲のまだ発信していない予約を取り消す（5-2-1 の2）。
-- どの設定が門に効くかを一覧にすると漏れるので、設定を変えたら、その範囲の予約をすべて取り消す（予約は次の回にまた取れる）。
CREATE FUNCTION salescall.release_claims_in_scope(p_scope text, p_tenant uuid, p_client uuid, p_business uuid) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
DECLARE n integer;
BEGIN
  IF p_scope NOT IN ('system', 'tenant', 'client', 'business') THEN
    RAISE EXCEPTION '範囲の名前が違います：%', p_scope USING ERRCODE = 'check_violation';
  END IF;
  -- 依頼主の範囲より上を取り消せるのは、その範囲の取引だけ（ほかの契約者の予約を、契約者の取引から取り消させない）
  IF (p_scope = 'system' AND salescall.cur_tenant() IS NOT NULL)
     OR (p_scope = 'tenant' AND p_tenant IS DISTINCT FROM salescall.cur_tenant())
     OR (p_scope IN ('client', 'business') AND (p_tenant IS DISTINCT FROM salescall.cur_tenant()
                                                OR p_client IS DISTINCT FROM salescall.cur_client())) THEN
    RAISE EXCEPTION '取引の範囲の外の予約は取り消せません' USING ERRCODE = 'insufficient_privilege';
  END IF;
  UPDATE salescall.attempts a SET state = 'released'
  WHERE a.state = 'claimed'
    AND (p_scope = 'system'
         OR (p_scope = 'tenant' AND a.tenant_id = p_tenant)
         OR (p_scope = 'client' AND a.client_id = p_client)
         OR (p_scope = 'business' AND a.client_id = p_client
             AND a.version_id IN (SELECT v.version_id FROM salescall.business_versions v WHERE v.business_id = p_business)));
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$;

-- その番号に進行中の試行があるか。あれば、取引の依頼主から見た関係（same_client / same_tenant / other_tenant）
CREATE FUNCTION salescall.number_active_relation(p_phone text) RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT CASE WHEN a.client_id = salescall.cur_client() THEN 'same_client'
              WHEN a.tenant_id = salescall.cur_tenant() THEN 'same_tenant'
              ELSE 'other_tenant' END
  FROM salescall.attempts a WHERE a.phone_e164 = p_phone AND salescall.is_active(a.state) LIMIT 1
$$;

-- ほかの依頼主が、その番号へ最後に発信した時刻（発信まで進んだ試行だけ。予約だけで戻ったものは数えない）。関係ごとに1行
CREATE FUNCTION salescall.number_last_dial_elsewhere(p_phone text)
RETURNS TABLE (relation text, last_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT CASE WHEN a.tenant_id = salescall.cur_tenant() THEN 'same_tenant' ELSE 'other_tenant' END, max(a.claimed_at)
  FROM salescall.attempts a
  WHERE a.phone_e164 = p_phone AND a.client_id IS DISTINCT FROM salescall.cur_client()
    AND a.state NOT IN ('claimed', 'released')
  GROUP BY 1
$$;

-- 全体で進行中の試行の数（会話AIの枠は全依頼主で共有。5-2 の9）
CREATE FUNCTION salescall.active_attempts_total() RETURNS integer
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT count(*)::integer FROM salescall.attempts WHERE salescall.is_live(state, claim_expires_at)
$$;

-- その回線で、日本時間のその日に発信まで進んだ試行の数（回線は契約者のもの。取引の契約者の回線だけ数える。Intent.md 8-6）
CREATE FUNCTION salescall.line_dials_on(p_line uuid, p_day date) RETURNS integer
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT count(*)::integer FROM salescall.attempts a
  WHERE a.line_id = p_line AND a.tenant_id = salescall.cur_tenant()
    AND a.state NOT IN ('claimed', 'released')
    AND salescall.jst(a.claimed_at)::date = p_day
$$;

REVOKE ALL ON FUNCTION salescall.release_claims_in_scope(text, uuid, uuid, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.release_claims_in_scope(text, uuid, uuid, uuid) TO salescall_app;
REVOKE ALL ON FUNCTION salescall.release_expired_claims(uuid, text, uuid), salescall.number_active_relation(text),
  salescall.number_last_dial_elsewhere(text), salescall.active_attempts_total(), salescall.line_dials_on(uuid, date)
  FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.release_expired_claims(uuid, text, uuid), salescall.number_active_relation(text),
  salescall.number_last_dial_elsewhere(text), salescall.active_attempts_total(), salescall.line_dials_on(uuid, date)
  TO salescall_app;

-- ═════════ 門の設定（5-2・5-2-1） ═════════
INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, bound_key, bound_side) VALUES
('gate.same_contact_min_interval', 'system_only', 'seconds', '同じ連絡先への前の発信が終わってから、次に予約できるまでの最小の間隔（次の動きの誤りでかけ続けないための歯止め。ふつうの間隔は再発信の方針で決める）。相手と約束したかけ直し・相手の意思と関係なく繋がらなかった／切れた発信は対象外', 'DESIGN.md 5-2 の5', NULL, NULL),
('gate.max_dials_per_contact_per_day', 'override', 'integer', '同じ連絡先へ、日本時間の1日に発信してよい回数の上限（実施回をまたいでも数える）。相手と約束したかけ直しは対象外。相手の意思と関係なく繋がらなかった／切れた発信は数えない', 'DESIGN.md 5-2 の5', NULL, NULL),
('judge.unclear_disconnect_is_refusal', 'override', 'boolean', '切れ方を決められない通話（DESIGN.md 7-1c の表のどれにも当てはまらない）を、営業拒否として扱うか（いいえ＝かけ直す）。契約者・依頼主・業務ごとに決められる', 'DESIGN.md 7-1c', NULL, NULL),
('line.max_dials_per_day', 'system_only', 'integer', '1つの回線（番号）から、日本時間の1日に発信してよい回数の上限（番号の評判を守る。Intent.md 8-6）', 'DESIGN.md 5-2 の7', NULL, NULL);
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('gate.same_contact_min_interval', 'system', '300', 'decided', '2026-10-14 ユーザー決定：5分。ただし、相手と約束したかけ直し・通信の問題などで繋がらなかった・話の途中で相手の意思と関係なく切れた発信は対象外', '苦情と応答率の実測を見て', 'ユーザー'),
('gate.max_dials_per_contact_per_day', 'system', '2', 'decided', '2026-10-14 ユーザー決定：2回。ただし、相手と約束したかけ直しは対象外。通信の問題などで繋がらなかった・相手の意思と関係なく切れた発信は数えない', '応答率と苦情の実測を見て', 'ユーザー'),
('judge.unclear_disconnect_is_refusal', 'system', 'true', 'provisional', '2026-10-14 ユーザー決定：決められないときに、かけ直すか営業拒否扱いにするかは、設定で決められるようにする。初めの値は営業拒否扱い（取り違えたときの害：わざと切った相手にまたかける＝取り消せない害 ＞ 見込みの相手を1件失う）', '運営者が業務ごとに決めるとき・切れ方の判定の実績を見て', 'CL-2'),
('line.max_dials_per_day', 'system', '300', 'decided', '2026-10-14 ユーザー決定：300本（番号の評判。Intent.md 8-6）', '回線ごとの応答率の推移を見て', 'ユーザー');
