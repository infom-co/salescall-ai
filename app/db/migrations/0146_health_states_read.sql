-- 0146 回線・行・番号・契約者の状態の欄を、読み手が読む（DESIGN.md 17章の作り直しの4段目。2026-10-29。書いた人：CL-2）
--
-- 2026-10-29 管理者の承認：「回線・行・契約者の状態を、それぞれ1つの欄に持ちます。OK」。0145 で影として書き始めた状態の欄を、門・候補・
-- 回線の空き・休んだ行の戻しが読む（今の作りの、読むたびに時刻を比べて導く関数＝unrung_blame・task_awaiting_decision の「分からない」の
-- 部分・task_resting・awaiting_recovery は、ここから読まない。外すのは5段目）。
--
-- (1) 前の発信の締めを待つ行（task_unsettled。門の awaiting_decision と、候補を出す処理が読む1か所）：締めが済んでいない発信があれば待つ。
--     ただし、その行の欄が「判定待ち」（17-1-2）で、待っている試行がその発信で、待っている回線がほかの回線なら、待たない（判定待ちは、
--     その電話をかけた回線だけが待つ＝0144 の決まりを、行の欄で読む）。回線を渡さない（p_line が NULL）ときは、どの発信の締めも待つ。
-- (2) 休んだ行を戻す守り（task_trouble_restore_guard）：戻せるのは、行の欄が「休み」か「復旧待ち」の行（17-3-2 の R-7・R-10。【当(7)】
--     復旧待ちの行も、管理者か契約者が手で戻せる。⛔ 0139〜0145：休みの行だけ＝復旧待ちには手の出口が無かった＝17-8 の (8)）。
-- (3) 回線の空き（lines_serving。約束の電話のために空けておく回線を数える1か所）：回線の欄が休み（回線のせい・声の不調）か使用停止の
--     回線は、かけられない回線として数えない（回線の休みは、もう停止の記録を作らない＝17-1-1。⛔ 前は止めた停止の記録だけを見た）。
-- (4) 表を引く入口が、印の見直しの時刻が来た番号（17-3-3 の N-7。番号の待つ印は、次の日の始まりで見直す＝17-3-6 の【当】）を、取引の
--     契約者の中から、鍵を取って読めるようにする（health_take の {"s": "number", "due": true}）。
-- (5) 管理者がシステムの初めの値のテスト番号を入れた・変えたとき、自分の値を持たない契約者の一覧（tenants_without_own_value。全体の取引
--     だけ）。テスト番号が入った（E14）を、その契約者ごとに1回ずつ渡す（17-3-6 の【当】(a)）。
-- (6) 試行のせいを書く（health_save）：1回の表引きの中で、空から「分からない」（V-1）を経て番号か回線（V-3・V-4）へ移った試行
--     （たとえば使用停止の回線で、かけている途中だった電話が鳴らずに終わった＝E1 と、回線の L-33 が出した「決めた」＝E16 が同じ取引）は、
--     守り（attempt_blame_once）に1つずつ通して書く（守りはゆるめない。⛔ 0145：終わりの値だけを書いたので、空から回線へは移せないと断った）。
-- (7) 表が読む設定（table_setting_layers）：状態の表は、取引の依頼者と違う依頼者の行・番号も引く（回線の続きは依頼者をまたぎ、テスト
--     番号の答えや回線ごとの処理の頭は契約者の取引で引く）。依頼者の範囲の設定の行は、行ごとの制限でその依頼者の取引からしか見えない
--     ので、表が読む決まりの値（core/health_rules.SETTING_KEYS の5つだけ）を、取引の契約者の依頼者について、範囲ごとに返す（合成は
--     core/settings の1か所）。⛔ 0145 の影：契約者の取引で引くと依頼者の値が見えず、システムの値で引いた（依頼者ごとの番号の決まった
--     順が効かなかった）。

-- ═════════ (1) 締めを待つ行（判定待ちは、行の欄で読む） ═════════
CREATE FUNCTION salescall.task_unsettled(p_task uuid, p_line uuid DEFAULT NULL) RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT EXISTS (SELECT 1 FROM salescall.attempts a WHERE a.task_id = p_task AND salescall.is_dialed(a.state)
                 AND NOT salescall.is_settled(a.attempt_id)
                 AND NOT (p_line IS NOT NULL AND EXISTS (
                   SELECT 1 FROM salescall.task_states s WHERE s.task_id = p_task AND s.stage = 'await_decision'
                     AND s.state->>'attempt' = a.attempt_id::text AND s.state->>'line' IS DISTINCT FROM p_line::text)))
$$;
GRANT EXECUTE ON FUNCTION salescall.task_unsettled(uuid, uuid) TO salescall_app;

-- ═════════ (2) 休んだ行・復旧待ちの行を戻す守り（行の欄で読む） ═════════
CREATE OR REPLACE FUNCTION salescall.task_trouble_restore_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF salescall.cur_actor_kind() IS DISTINCT FROM 'operator' AND salescall.cur_actor_kind() IS DISTINCT FROM 'admin' THEN
    RAISE EXCEPTION '休んだ行を戻せるのは、管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'task_trouble_restore_by_person';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM salescall.task_states s WHERE s.task_id = NEW.task_id AND s.stage IN ('resting', 'await_recovery')) THEN
    RAISE EXCEPTION 'その行は、こちらのプログラムの不具合で休んでいません（復旧も待っていません）' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'task_trouble_restore_only_resting';
  END IF;
  NEW.restored_by := salescall.cur_actor();
  NEW.actor_kind := salescall.cur_actor_kind()::salescall.actor_kind;
  NEW.restored_at := clock_timestamp();
  RETURN NEW;
END $$;

-- ═════════ (3) 回線の空き：休み・使用停止の回線は数えない（回線の欄で読む） ═════════
CREATE OR REPLACE FUNCTION salescall.lines_serving()
RETURNS TABLE (line_id uuid, client_id uuid, idle boolean)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT o.line_id, o.client_id,
         NOT EXISTS (SELECT 1 FROM salescall.attempts a WHERE a.line_id = o.line_id AND salescall.is_live(a.state, a.claim_expires_at))
  FROM salescall.lines o
  WHERE o.tenant_id = salescall.cur_tenant() AND o.retired_at IS NULL AND o.lease_expires_at > now()
    AND NOT EXISTS (SELECT 1 FROM salescall.open_stops(o.tenant_id, NULL, NULL, o.line_id))
    AND NOT EXISTS (SELECT 1 FROM salescall.line_states s WHERE s.line_id = o.line_id
                    AND s.stage IN ('line_rest', 'audio_rest', 'out_of_use'))
$$;

-- ═════════ (4) 印の見直しの時刻が来た番号を、鍵を取って読む（0145 の health_take に足す） ═════════
CREATE OR REPLACE FUNCTION salescall.health_take(p_want jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
DECLARE
  v_tenant uuid := salescall.cur_tenant();
  v_out jsonb := '[]'::jsonb;
  w jsonb;
  k uuid;
  n integer;
  r record;
BEGIN
  IF v_tenant IS NULL THEN
    RAISE EXCEPTION '状態の欄は、契約者の取引でだけ扱えます' USING ERRCODE = 'insufficient_privilege';
  END IF;
  FOR w IN SELECT * FROM jsonb_array_elements(p_want) LOOP
    IF w->>'s' = 'task' AND w ? 'stages' THEN
      FOR r IN SELECT t.task_id, t.client_id, t.stage, t.state FROM salescall.task_states t
               WHERE t.tenant_id = v_tenant AND t.stage IN (SELECT jsonb_array_elements_text(w->'stages'))
               ORDER BY t.task_id FOR UPDATE LOOP
        v_out := v_out || jsonb_build_array(jsonb_build_object('s', 'task', 'k', r.task_id, 'client', r.client_id,
                                                               'stage', r.stage, 'state', r.state, 'created', false));
      END LOOP;
      CONTINUE;
    END IF;
    IF w->>'s' = 'number' AND w ? 'due' THEN   -- 印の見直しの時刻（中身の next_at）が来た番号（N-7）
      FOR r IN SELECT x.contact_id, x.client_id, x.stage, x.state FROM salescall.number_states x
               WHERE x.tenant_id = v_tenant AND (x.state->>'hold')::boolean
                 AND (x.state->'next_at'->>'$t')::timestamptz <= now()
               ORDER BY x.contact_id FOR UPDATE LOOP
        v_out := v_out || jsonb_build_array(jsonb_build_object('s', 'number', 'k', r.contact_id, 'client', r.client_id,
                                                               'stage', r.stage, 'state', r.state, 'created', false));
      END LOOP;
      CONTINUE;
    END IF;
    k := (w->>'k')::uuid;
    CASE w->>'s'
      WHEN 'line' THEN
        INSERT INTO salescall.line_states (line_id, tenant_id, stage, state)
        SELECT l.line_id, l.tenant_id, w->'init'->>'stage', w->'init'->'state' FROM salescall.lines l
        WHERE l.line_id = k AND l.tenant_id = v_tenant ON CONFLICT DO NOTHING;
        GET DIAGNOSTICS n = ROW_COUNT;
        SELECT s.line_id AS key, NULL::uuid AS client_id, s.stage, s.state INTO r FROM salescall.line_states s
        WHERE s.line_id = k AND s.tenant_id = v_tenant FOR UPDATE;
      WHEN 'tenant' THEN
        IF k IS DISTINCT FROM v_tenant THEN
          RAISE EXCEPTION 'ほかの契約者の欄は扱えません' USING ERRCODE = 'insufficient_privilege';
        END IF;
        INSERT INTO salescall.tenant_states (tenant_id, stage, state) VALUES (k, w->'init'->>'stage', w->'init'->'state')
        ON CONFLICT DO NOTHING;
        GET DIAGNOSTICS n = ROW_COUNT;
        SELECT s.tenant_id AS key, NULL::uuid AS client_id, s.stage, s.state INTO r FROM salescall.tenant_states s
        WHERE s.tenant_id = k FOR UPDATE;
      WHEN 'task' THEN
        INSERT INTO salescall.task_states (task_id, tenant_id, client_id, stage, state)
        SELECT t.task_id, t.tenant_id, t.client_id, w->'init'->>'stage', w->'init'->'state' FROM salescall.tasks t
        WHERE t.task_id = k AND t.tenant_id = v_tenant ON CONFLICT DO NOTHING;
        GET DIAGNOSTICS n = ROW_COUNT;
        SELECT s.task_id AS key, s.client_id, s.stage, s.state INTO r FROM salescall.task_states s
        WHERE s.task_id = k AND s.tenant_id = v_tenant FOR UPDATE;
      WHEN 'number' THEN
        INSERT INTO salescall.number_states (contact_id, tenant_id, client_id, stage, state)
        SELECT c.contact_id, c.tenant_id, c.client_id, w->'init'->>'stage', w->'init'->'state' FROM salescall.contacts c
        WHERE c.contact_id = k AND c.tenant_id = v_tenant ON CONFLICT DO NOTHING;
        GET DIAGNOSTICS n = ROW_COUNT;
        SELECT s.contact_id AS key, s.client_id, s.stage, s.state INTO r FROM salescall.number_states s
        WHERE s.contact_id = k AND s.tenant_id = v_tenant FOR UPDATE;
      WHEN 'attempt' THEN
        INSERT INTO salescall.attempt_blames (attempt_id, tenant_id, client_id, blame)
        SELECT a.attempt_id, a.tenant_id, a.client_id, w->'init'->>'stage' FROM salescall.attempts a
        WHERE a.attempt_id = k AND a.tenant_id = v_tenant ON CONFLICT DO NOTHING;
        GET DIAGNOSTICS n = ROW_COUNT;
        SELECT b.attempt_id AS key, b.client_id, b.blame AS stage, NULL::jsonb AS state INTO r FROM salescall.attempt_blames b
        WHERE b.attempt_id = k AND b.tenant_id = v_tenant FOR UPDATE;
      ELSE
        RAISE EXCEPTION '状態の欄の名前が違います：%', w->>'s' USING ERRCODE = 'check_violation';
    END CASE;
    IF NOT FOUND THEN
      RAISE EXCEPTION 'その%は無い（または、取引の契約者の外）：%', w->>'s', k USING ERRCODE = 'check_violation';
    END IF;
    v_out := v_out || jsonb_build_array(jsonb_build_object('s', w->>'s', 'k', r.key, 'client', r.client_id, 'stage', r.stage,
                                                           'state', r.state, 'created', n > 0)
      || CASE WHEN w->>'s' = 'attempt' THEN
           (SELECT jsonb_build_object('task', a.task_id, 'contact', a.contact_id, 'line', a.line_id, 'ended_at', a.ended_at)
            FROM salescall.attempts a WHERE a.attempt_id = k)
         ELSE '{}'::jsonb END);
  END LOOP;
  RETURN v_out;
END $$;

-- ═════════ (5) 管理者がシステムの初めの値のテスト番号を入れた・変えた：自分の値を持たない契約者の一覧（17-2 の E14。17-3-6 の【当】(a)） ═════════
-- 全体の取引（管理者かシステムの処理）だけが読める。テスト番号が入った（E14）を、その契約者ごとに1回ずつ渡すため（core/health の
-- number_set_everywhere。契約者の取引に切り替えるのは読み書きの層の1か所＝core/access の Tx.as_tenant）。
CREATE FUNCTION salescall.tenants_without_own_value(p_key text) RETURNS SETOF uuid
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
BEGIN
  IF salescall.cur_tenant() IS NOT NULL OR coalesce(salescall.cur_actor_kind(), '') NOT IN ('admin', 'system') THEN
    RAISE EXCEPTION '自分の値を持たない契約者の一覧は、全体の取引（管理者かシステムの処理）でだけ読めます' USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN QUERY SELECT t.tenant_id FROM salescall.tenants t
               WHERE NOT EXISTS (SELECT 1 FROM salescall.setting_values v WHERE v.key = p_key AND v.scope = 'tenant'
                                 AND v.tenant_id = t.tenant_id)
               ORDER BY t.tenant_id;
END $$;
REVOKE ALL ON FUNCTION salescall.tenants_without_own_value(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.tenants_without_own_value(text) TO salescall_app;

-- ═════════ (6) 試行のせいを、移った順に守りへ通して書く（0145 の health_save を替える） ═════════
CREATE OR REPLACE FUNCTION salescall.health_save(p_rows jsonb, p_moves jsonb) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
DECLARE
  v_tenant uuid := salescall.cur_tenant();
  w jsonb;
  k uuid;
  v_client uuid;
BEGIN
  IF v_tenant IS NULL THEN
    RAISE EXCEPTION '状態の欄は、契約者の取引でだけ扱えます' USING ERRCODE = 'insufficient_privilege';
  END IF;
  FOR w IN SELECT * FROM jsonb_array_elements(p_rows) LOOP
    k := (w->>'k')::uuid;
    CASE w->>'s'
      WHEN 'line' THEN
        UPDATE salescall.line_states SET stage = w->>'stage', state = w->'state', moved_at = clock_timestamp()
        WHERE line_id = k AND tenant_id = v_tenant;
      WHEN 'tenant' THEN
        UPDATE salescall.tenant_states SET stage = w->>'stage', state = w->'state', moved_at = clock_timestamp()
        WHERE tenant_id = k AND tenant_id = v_tenant;
      WHEN 'task' THEN
        UPDATE salescall.task_states SET stage = w->>'stage', state = w->'state', moved_at = clock_timestamp()
        WHERE task_id = k AND tenant_id = v_tenant;
      WHEN 'number' THEN
        UPDATE salescall.number_states SET stage = w->>'stage', state = w->'state', moved_at = clock_timestamp()
        WHERE contact_id = k AND tenant_id = v_tenant;
      WHEN 'attempt' THEN
        -- 空から番号・回線へ1回の表引きで移った（V-1 のあと V-3・V-4）：「分からない」を先に書いてから、守りを1つずつ通す
        IF w->>'stage' IN ('number', 'line') THEN
          UPDATE salescall.attempt_blames SET blame = 'unknown', blamed_at = clock_timestamp()
          WHERE attempt_id = k AND tenant_id = v_tenant AND blame = 'empty'
            AND EXISTS (SELECT 1 FROM jsonb_array_elements(p_moves) m
                        WHERE m->>'s' = 'attempt' AND (m->>'k')::uuid = k AND m->>'from' = 'empty' AND m->>'to' = 'unknown');
        END IF;
        UPDATE salescall.attempt_blames SET blame = w->>'stage', blamed_at = clock_timestamp()
        WHERE attempt_id = k AND tenant_id = v_tenant;
      ELSE
        RAISE EXCEPTION '状態の欄の名前が違います：%', w->>'s' USING ERRCODE = 'check_violation';
    END CASE;
    IF NOT FOUND THEN
      RAISE EXCEPTION 'その%の欄は無い（鍵を取ってから書きます）：%', w->>'s', k USING ERRCODE = 'check_violation';
    END IF;
  END LOOP;
  FOR w IN SELECT * FROM jsonb_array_elements(p_moves) LOOP
    k := (w->>'k')::uuid;
    v_client := CASE w->>'s'
      WHEN 'task' THEN (SELECT client_id FROM salescall.task_states WHERE task_id = k AND tenant_id = v_tenant)
      WHEN 'number' THEN (SELECT client_id FROM salescall.number_states WHERE contact_id = k AND tenant_id = v_tenant)
      WHEN 'attempt' THEN (SELECT client_id FROM salescall.attempt_blames WHERE attempt_id = k AND tenant_id = v_tenant)
      ELSE NULL END;
    INSERT INTO salescall.state_moves (tenant_id, client_id, subject, key, event, event_at, from_stage, to_stage, table_rows,
                                       actions, mistake, failure, once, origin)
    VALUES (v_tenant, v_client, w->>'s', k, w->>'event', (w->>'at')::timestamptz, w->>'from', w->>'to',
            coalesce(ARRAY(SELECT jsonb_array_elements_text(w->'rows')), '{}'), coalesce(w->'actions', '[]'::jsonb),
            CASE WHEN jsonb_typeof(w->'mistake') = 'object' THEN w->'mistake' END, w->>'failure', w->>'once', w->>'origin');
  END LOOP;
END $$;
REVOKE ALL ON FUNCTION salescall.health_save(jsonb, jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.health_save(jsonb, jsonb) TO salescall_app;

-- ═════════ (7) 表が読む設定を、その依頼者の範囲で（取引の契約者の依頼者だけ・表の5つの決まりだけ） ═════════
CREATE FUNCTION salescall.table_setting_layers(p_client uuid) RETURNS TABLE (key text, scope text, value jsonb)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
DECLARE
  v_tenant uuid := salescall.cur_tenant();
BEGIN
  IF v_tenant IS NULL THEN
    RAISE EXCEPTION '表が読む設定は、契約者の取引でだけ読めます' USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF p_client IS NOT NULL AND NOT EXISTS (SELECT 1 FROM salescall.clients c WHERE c.client_id = p_client AND c.tenant_id = v_tenant) THEN
    RAISE EXCEPTION 'その依頼者は、取引の契約者の依頼者ではありません' USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN QUERY
    SELECT DISTINCT ON (v.key, v.scope) v.key, v.scope::text, v.value FROM salescall.setting_values v
    WHERE v.key IN ('line.test_ring_gaps', 'line.test_ring_hours', 'stop.trouble.stop_after_rows', 'dial.failed_retry_days',
                    'gate.same_contact_min_interval')   -- core/health_rules.SETTING_KEYS と同じ（試験 test_health が比べる）
      AND (v.scope = 'system' OR (v.scope = 'tenant' AND v.tenant_id = v_tenant)
           OR (v.scope = 'client' AND v.tenant_id = v_tenant AND v.client_id = p_client))
    ORDER BY v.key, v.scope, v.value_id DESC;
END $$;
REVOKE ALL ON FUNCTION salescall.table_setting_layers(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.table_setting_layers(uuid) TO salescall_app;
