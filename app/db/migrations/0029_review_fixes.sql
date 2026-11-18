-- 0029 見直し（2026-10-16 ユーザー：「ここまでの実装で、食い違いや不備、修正漏れなどは起こっていないですか？」）で見つけた穴を直す。
-- 書いた人：CL-2。DESIGN.md 15-2 の88〜。
--
-- (0) まだ解かれていない停止（判断は1か所。2026-10-16 に、同じ問い合わせがデータベースとプログラムの9か所に書かれていたのを、
--     ここ1つにまとめた）。呼ぶ側の権限・行ごとの制限のまま読む（security_invoker。持ち主の権限で動く関数の中では、持ち主として読む）。
CREATE VIEW salescall.open_stop_events WITH (security_invoker = true) AS
  SELECT s.* FROM salescall.stop_events s
  WHERE s.action = 'stop' AND NOT EXISTS (SELECT 1 FROM salescall.stop_events r WHERE r.resolves_event_id = s.event_id);
GRANT SELECT ON salescall.open_stop_events TO salescall_app;

CREATE OR REPLACE FUNCTION salescall.open_stops(p_tenant uuid, p_client uuid, p_business uuid, p_line uuid DEFAULT NULL)
RETURNS SETOF salescall.stop_events LANGUAGE sql STABLE AS $$
  SELECT s.* FROM salescall.open_stop_events s
  WHERE s.scope = 'system'
     OR (s.scope = 'tenant' AND s.tenant_id = p_tenant)
     OR (s.scope = 'client' AND s.tenant_id = p_tenant AND s.client_id = p_client)
     OR (s.scope = 'business' AND s.tenant_id = p_tenant AND s.client_id = p_client AND s.business_id = p_business)
     OR (s.scope = 'line' AND s.tenant_id = p_tenant AND s.line_id = p_line)
$$;

CREATE OR REPLACE FUNCTION salescall.release_claims_for_stop(p_event_id bigint) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
DECLARE s salescall.stop_events; n integer;
BEGIN
  SELECT * INTO s FROM salescall.open_stop_events e WHERE e.event_id = p_event_id;
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

-- (1) 行ごとの制限を越えて読む関数は、その関数を呼んでよい取引だけが使える（ほかの契約者のデータを見せない）：
--     全体の数（全体の今日の費用・システムが止めた停止の一覧）は全体の取引だけ。回線のエラーの数は、その回線の契約者の取引だけ。
CREATE OR REPLACE FUNCTION salescall.spent_today_all() RETURNS numeric
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT CASE WHEN salescall.cur_tenant() IS NOT NULL THEN NULL ELSE
    (SELECT coalesce(sum(amount_yen), 0) FROM salescall.usage_entries
     WHERE salescall.jst(occurred_at)::date = salescall.jst(now())::date) END
$$;
CREATE OR REPLACE FUNCTION salescall.open_system_stops(p_cause text) RETURNS TABLE (event_id bigint, scope text, tenant_id uuid,
                                                                                   client_id uuid, stopped_on date)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT s.event_id, s.scope::text, s.tenant_id, s.client_id, salescall.jst(s.at)::date FROM salescall.open_stop_events s
  WHERE salescall.cur_tenant() IS NULL AND s.cause::text = p_cause AND s.actor_kind = 'system'
$$;

-- (2) 続いたエラーの数は、エラーの種類ごとの「直った証拠」から数え直す（2026-10-16 に見つけた穴：別の耳＝通話後の検証に
--     つながらないエラーも、どこかの通話が会話に入るたびに数え直していたので、1回線では止まらなかった）。
--     証拠：in_conversation＝どこかの通話が会話に入った（会話AI）／verified＝どこかの通話の検証が済んだ（別の耳）／
--     ringing＝その回線でお客様を呼び出せた（回線）／none＝直った証拠を持たない（1回で止める種類。保存の失敗など）。
--     どの種類がどの証拠かは core/errors.py の1か所（EVIDENCE）。どれでも、同じ原因・同じ範囲の停止が解かれた時刻より後を数える。
DROP FUNCTION salescall.consecutive_errors(text[], text, uuid);
CREATE FUNCTION salescall.consecutive_errors(p_kinds text[], p_cause text, p_evidence text, p_line uuid DEFAULT NULL)
RETURNS integer
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  WITH allowed AS (     -- 全体の数は全体の取引だけ・回線の数はその回線の契約者の取引だけ
    SELECT CASE WHEN p_line IS NULL THEN salescall.cur_tenant() IS NULL
                ELSE EXISTS (SELECT 1 FROM salescall.lines l WHERE l.line_id = p_line AND l.tenant_id = salescall.cur_tenant())
           END AS ok
  ), since AS (
    SELECT greatest(
      CASE p_evidence
        WHEN 'in_conversation' THEN (SELECT coalesce(max(h.at), '-infinity'::timestamptz) FROM salescall.attempt_state_history h
                                     WHERE h.to_state = 'in_conversation')
        WHEN 'verified' THEN (SELECT coalesce(max(v.recorded_at), '-infinity'::timestamptz) FROM salescall.verification_summaries v)
        WHEN 'ringing' THEN (SELECT coalesce(max(h.at), '-infinity'::timestamptz)
                             FROM salescall.attempt_state_history h JOIN salescall.attempts a ON a.attempt_id = h.attempt_id
                             WHERE a.line_id = p_line AND h.to_state = 'ringing')
        WHEN 'none' THEN '-infinity'::timestamptz
      END,
      (SELECT coalesce(max(r.at), '-infinity'::timestamptz)
       FROM salescall.stop_events r JOIN salescall.stop_events s ON s.event_id = r.resolves_event_id
       WHERE s.cause::text = p_cause
         AND CASE WHEN p_line IS NULL THEN s.scope = 'system' ELSE s.scope = 'line' AND s.line_id = p_line END)) AS t
  )
  SELECT CASE WHEN NOT (SELECT ok FROM allowed) OR p_evidence NOT IN ('in_conversation', 'verified', 'ringing', 'none')
                                     OR (p_evidence = 'ringing' AND p_line IS NULL) THEN NULL
         ELSE (SELECT count(*)::integer FROM salescall.error_events e, since
               WHERE e.kind = ANY(p_kinds) AND e.occurred_at > since.t AND (p_line IS NULL OR e.line_id = p_line)) END
$$;
REVOKE ALL ON FUNCTION salescall.consecutive_errors(text[], text, text, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.consecutive_errors(text[], text, text, uuid) TO salescall_app;

-- (3) 停止を解く確かめの記録は、その停止の範囲の取引だけが書ける（契約者の処理が、全体の停止に確かめを書いて、
--     自動の解除を遅らせない）。全体の停止＝全体の取引／それ以外＝その停止の契約者の取引。
DROP POLICY check_scope ON salescall.stop_checks;
CREATE POLICY check_scope ON salescall.stop_checks
  USING (EXISTS (SELECT 1 FROM salescall.stop_events s WHERE s.event_id = stop_checks.stop_event_id))
  WITH CHECK (EXISTS (SELECT 1 FROM salescall.stop_events s WHERE s.event_id = stop_checks.stop_event_id
                      AND CASE WHEN s.scope = 'system' THEN salescall.cur_tenant() IS NULL
                               ELSE s.tenant_id = salescall.cur_tenant() END));

-- (4) 発信禁止の取り消しは、管理者か契約者（システムの処理はできない）。前は契約者だけを通していて、断る文（「管理者か契約者だけ」）
--     とも、停止を解ける決まり（契約者の範囲の停止は、その範囲の取引の管理者か契約者が解ける。0019）とも合っていなかった。
--     中身のほかは 0026 と同じ。
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
  IF NEW.revoked_at IS NOT NULL AND salescall.cur_actor_kind() IS DISTINCT FROM 'operator'
     AND salescall.cur_actor_kind() IS DISTINCT FROM 'admin' THEN
    RAISE EXCEPTION '発信禁止の取り消しは、管理者か契約者だけができます（システムの処理はできません）' USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN NEW;
END $$;

-- (6) 行のいまの結果の版（業務の状態の正本＝最新の版。DESIGN.md 4-1）を選ぶのは、この見方の1か所（core/results.py の latest と、
--     報酬の「成果1件」が読む）。2026-10-16 見直し：報酬は「はじめて完了になった版」を数えていて、契約者が完了でないと直した行も
--     成果に数えていた（多めの報酬で、赤字の判断が遅れる）。
CREATE VIEW salescall.latest_task_results WITH (security_invoker = true) AS
  SELECT DISTINCT ON (r.task_id) r.* FROM salescall.task_results r ORDER BY r.task_id, r.version_no DESC;
GRANT SELECT ON salescall.latest_task_results TO salescall_app;

-- (5) 0019 で外した仕掛けの関数が、0026 で中身だけ作り直されて残っていた（どこからも使われない）。消す。
DROP FUNCTION IF EXISTS salescall.system_manual_stop_by_admin();
