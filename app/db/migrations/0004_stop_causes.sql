-- 0004 停止の原因と、自動の解除（2026-10-13 CL-2。DESIGN.md 9-1・Intent.md 10-3）
-- ユーザー：「運用や仕組み、判定の精度を上げることで、人がいない仕組みを構築して」。
-- - 止めるのも解くのも、原因ごとに決めた条件でシステムが行う。人が解くのは、人が自分で押した緊急停止だけ。
-- - 止まっているか（Q13）＝「まだ解かれていない停止が1つでもあるか」。解除は、どの停止を解いたかを指す（1回だけ）。
-- - システムが解けるのは、システムが止めた停止だけ（データベースが守る）。

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM salescall.stop_events) THEN
    RAISE EXCEPTION 'stop_events に行があります。この作りの直しは、まだ停止の記録が無いときだけ流せます';
  END IF;
END $$;

CREATE TYPE salescall.stop_cause AS ENUM (
  'manual',            -- 人が押した緊急停止（人だけが止め、人だけが解く）
  'storage_failure',   -- 保存の失敗
  'line_failure',      -- 回線の異常が続いた
  'ai_connection',     -- 会話AIにつながらない
  'project_denied',    -- プロジェクトの利用拒否・枠
  'forbidden_speech',  -- 言ってはいけないことを言った（2つの耳で確かめたもの）
  'quality_gate',      -- 品質の基準を外れた（段階的な立ち上げ）
  'budget'             -- 費用の上限
);

ALTER TABLE salescall.stop_events
  ADD COLUMN business_id       uuid,
  ADD COLUMN cause             salescall.stop_cause,
  ADD COLUMN resolves_event_id bigint REFERENCES salescall.stop_events (event_id),
  ADD COLUMN evidence          text CHECK (evidence IS NULL OR btrim(evidence) <> ''),  -- 止めた・解いた根拠（確かめの結果）
  ADD CONSTRAINT stop_events_business_fkey FOREIGN KEY (client_id, business_id)
      REFERENCES salescall.businesses (client_id, business_id),
  ADD CONSTRAINT stop_events_stop_has_cause CHECK ((action = 'stop') = (cause IS NOT NULL)),
  ADD CONSTRAINT stop_events_resume_points_to_stop CHECK ((action = 'resume') = (resolves_event_id IS NOT NULL)),
  ADD CONSTRAINT stop_events_manual_by_operator CHECK (cause IS DISTINCT FROM 'manual' OR actor_kind = 'operator');

-- 範囲の決まりを、業務を足した形に置き換える（前の決まりは名前が自動で付いていたので、中身で探して外す）
DO $$
DECLARE c text;
BEGIN
  FOR c IN SELECT conname FROM pg_constraint
           WHERE conrelid = 'salescall.stop_events'::regclass AND contype = 'c'
             AND pg_get_constraintdef(oid) LIKE '%scope%' LOOP
    EXECUTE format('ALTER TABLE salescall.stop_events DROP CONSTRAINT %I', c);
  END LOOP;
END $$;
ALTER TABLE salescall.stop_events
  ADD CONSTRAINT stop_events_scope_target CHECK (
       (scope = 'system'   AND tenant_id IS NULL     AND client_id IS NULL     AND business_id IS NULL)
    OR (scope = 'tenant'   AND tenant_id IS NOT NULL AND client_id IS NULL     AND business_id IS NULL)
    OR (scope = 'client'   AND tenant_id IS NOT NULL AND client_id IS NOT NULL AND business_id IS NULL)
    OR (scope = 'business' AND tenant_id IS NOT NULL AND client_id IS NOT NULL AND business_id IS NOT NULL));

-- 「停止を解くのは人だけ」を外し、「システムが解けるのは、システムが止めた停止だけ」に置き換える
ALTER TABLE salescall.stop_events DROP CONSTRAINT stop_events_resume_by_operator;
CREATE UNIQUE INDEX stop_events_resolved_once ON salescall.stop_events (resolves_event_id)
  WHERE resolves_event_id IS NOT NULL;

CREATE FUNCTION salescall.stop_event_before_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE s salescall.stop_events;
BEGIN
  -- 誰が、は読み書きの層が取引に渡したものと同じでなければならない（人のふりをして、人の緊急停止を解かせない）
  IF NEW.actor IS DISTINCT FROM salescall.cur_actor() OR NEW.actor_kind::text IS DISTINCT FROM salescall.cur_actor_kind() THEN
    RAISE EXCEPTION '停止の記録の「誰が」が、取引の「誰が」と違います' USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF NEW.action = 'resume' THEN
    SELECT * INTO s FROM salescall.stop_events WHERE event_id = NEW.resolves_event_id;
    IF NOT FOUND OR s.action <> 'stop' THEN
      RAISE EXCEPTION '解除が指す停止がありません' USING ERRCODE = 'check_violation';
    END IF;
    IF (s.scope, s.tenant_id, s.client_id, s.business_id) IS DISTINCT FROM
       (NEW.scope, NEW.tenant_id, NEW.client_id, NEW.business_id) THEN
      RAISE EXCEPTION '解除の範囲が、停止の範囲と違います' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.actor_kind = 'system' AND s.cause = 'manual' THEN
      RAISE EXCEPTION '人が押した緊急停止は、システムでは解けません' USING ERRCODE = 'insufficient_privilege';
    END IF;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER stop_events_before_insert BEFORE INSERT ON salescall.stop_events
  FOR EACH ROW EXECUTE FUNCTION salescall.stop_event_before_insert();

-- 行ごとの制限：業務の範囲も、依頼主の範囲と同じく、その依頼主の取引でだけ書ける
DROP POLICY stop_scope ON salescall.stop_events;
CREATE POLICY stop_scope ON salescall.stop_events
  USING (scope = 'system' OR tenant_id = salescall.cur_tenant())
  WITH CHECK ((scope = 'system' AND salescall.cur_tenant() IS NULL)
              OR (scope = 'tenant' AND tenant_id = salescall.cur_tenant())
              OR (scope IN ('client', 'business') AND tenant_id = salescall.cur_tenant()
                  AND client_id = salescall.cur_client()));

-- まだ解かれていない停止（Q13 の答え。門だけが読む）
CREATE FUNCTION salescall.open_stops(p_tenant uuid, p_client uuid, p_business uuid)
RETURNS SETOF salescall.stop_events LANGUAGE sql STABLE AS $$
  SELECT s.* FROM salescall.stop_events s
  WHERE s.action = 'stop'
    AND NOT EXISTS (SELECT 1 FROM salescall.stop_events r WHERE r.resolves_event_id = s.event_id)
    AND (s.scope = 'system'
         OR (s.scope = 'tenant' AND s.tenant_id = p_tenant)
         OR (s.scope = 'client' AND s.tenant_id = p_tenant AND s.client_id = p_client)
         OR (s.scope = 'business' AND s.tenant_id = p_tenant AND s.client_id = p_client AND s.business_id = p_business))
$$;

-- 止めるときに、まだ発信していない予約を取り消す（DESIGN.md 5-2-1 の2）。
-- 全体・契約者の停止は、取引の依頼主の範囲の外の予約も取り消す必要があるので、表の持ち主の権限で動く。
-- できることは「範囲の中の、予約のままの試行を released にする」だけ（移り方の決まりは、試行の仕掛けがそのまま守る）。
CREATE FUNCTION salescall.release_claims_for_stop(p_event_id bigint) RETURNS integer
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
             AND a.version_id IN (SELECT v.version_id FROM salescall.business_versions v WHERE v.business_id = s.business_id)));
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$;
REVOKE ALL ON FUNCTION salescall.release_claims_for_stop(bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.release_claims_for_stop(bigint), salescall.open_stops(uuid, uuid, uuid),
                          salescall.stop_event_before_insert() TO salescall_app;
