-- 0145 回線・行・番号・契約者の状態の欄と、試行のせいの欄と、移った記録（DESIGN.md 17章の作り直しの3段目＝影。2026-10-29。書いた人：CL-2）
--
-- 2026-10-29 管理者の承認：「回線・行・契約者の状態を、それぞれ1つの欄に持ちます。OK」。
-- 17章の表（core/health_rules.py）を引く入口（core/health.py）が、出来事を受けて、ここに今の状態を持ち、表の1行を引いて次の状態に移す。
-- **今回は影**：入口は今の作り（unrung_blame・task_awaiting_decision・task_resting・awaiting_recovery・consecutive_errors・回線の停止 など）
-- の横で動き、状態と移った記録を書くだけ。することの並び（知らせ・修正依頼・予定・テスト番号を鳴らす など）は記録するだけで行わない。
-- 今の作りの答えは、まだ何も変えない（読み手を表に替えるのは4段目）。
--
-- (1) 状態の欄（17-1）：回線（line_states）・行（task_states）・番号（number_states。依頼者の連絡先の番号ごと）・契約者（tenant_states）。
--     段（stage）と、その段の中身（state。core/health.py が表の値の形のまま書く）。
-- (2) 試行のせいの欄（attempt_blames。17-1-5）：空・分からない・番号・回線・こちらの5つ。**1回だけ書ける**：移ってよいのは
--     空→分からない（V-1）・空→こちら（V-2）・分からない→番号（V-3）・分からない→回線（V-4）だけ。一度、番号・回線・こちらになったら、
--     データベースが書き直しを断る（V-5。読むたびに導き直さない。決まりを変えても、過去の電話の扱いは変わらない）。消すこともできない。
-- (3) 移った記録（state_moves）：どの出来事で、どの欄が、どの段からどの段へ移ったか・引いた表の行番号・することの並び（記録だけ）・
--     表を引けなかったとき（誤りの値）・影の書き込みが失敗したとき。追記だけ（書き換え・消すは断る）。
-- (4) **書けるのは表を引く入口だけ**：アプリの役には読む権限だけを渡し、書くのはデータベースの2つの関数（health_take＝鍵を取って
--     読む・無ければ初めの状態で作る／health_save＝次の状態と移った記録を書く）だけにする。この2つを呼んでよいのは core/health.py だけ
--     （checks/rules.py の WRITE_FUNCTIONS）。回線の続きの試行は、同じ契約者のほかの依頼者の試行も入る（回線は依頼者をまたいで使う）
--     ので、2つの関数は持ち主の権限で動き、取引の契約者の行だけを扱う（ほかの契約者の行は断る）。読む側（4段目の門・候補）は、
--     行ごとの制限のとおり、回線・契約者は契約者の取引、行・番号・試行のせいは依頼者の取引で読む。

-- ═════════ (1) 状態の欄 ═════════
CREATE TABLE salescall.line_states (          -- 回線の欄（17-1-1。out_of_use＝使用停止）
  line_id   uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  stage     text NOT NULL CHECK (stage IN ('healthy', 'check_a', 'check_b', 'check_c', 'line_rest', 'audio_rest', 'out_of_use')),
  state     jsonb NOT NULL,                    -- 段の中身（core/health_rules.Line の値の形のまま）
  moved_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
  FOREIGN KEY (tenant_id, line_id) REFERENCES salescall.lines (tenant_id, line_id)
);
CREATE TABLE salescall.tenant_states (        -- 契約者の欄（17-1-4。こちらのプログラムの不具合の分）
  tenant_id uuid PRIMARY KEY REFERENCES salescall.tenants,
  stage     text NOT NULL CHECK (stage IN ('normal', 'stopped')),
  state     jsonb NOT NULL,
  moved_at  timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE salescall.task_states (          -- 行の欄（17-1-2。この章の分だけ。業務の終わりは今のまま）
  task_id   uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  client_id uuid NOT NULL,
  stage     text NOT NULL CHECK (stage IN ('on_plan', 'await_decision', 'await_recovery', 'resting', 'voice_redial')),
  state     jsonb NOT NULL,
  moved_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, task_id) REFERENCES salescall.tasks (client_id, task_id)
);
CREATE INDEX task_states_by_stage ON salescall.task_states (tenant_id, stage);
CREATE TABLE salescall.number_states (        -- 番号の欄（17-1-3。依頼者の連絡先の番号ごと。どの行・どの回線でも）
  contact_id uuid PRIMARY KEY,
  tenant_id  uuid NOT NULL,
  client_id  uuid NOT NULL,
  stage      text NOT NULL CHECK (stage IN ('none', 'run', 'dead')),
  state      jsonb NOT NULL,
  moved_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, contact_id) REFERENCES salescall.contacts (client_id, contact_id)
);

-- ═════════ (2) 試行のせいの欄（1回だけ書ける） ═════════
CREATE TABLE salescall.attempt_blames (       -- 17-1-5。行があること＝その電話の出来事を、もう表に渡した
  attempt_id uuid PRIMARY KEY,
  tenant_id  uuid NOT NULL,
  client_id  uuid NOT NULL,
  blame      text NOT NULL CHECK (blame IN ('empty', 'unknown', 'number', 'line', 'ours')),
  -- 空・分からない・番号・回線・こちら
  blamed_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, attempt_id) REFERENCES salescall.attempts (client_id, attempt_id)
);

CREATE FUNCTION salescall.attempt_blame_once() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION '試行のせいの欄は消せません' USING ERRCODE = 'check_violation', CONSTRAINT = 'attempt_blame_once';
  END IF;
  IF TG_OP = 'INSERT' THEN
    IF NEW.blame <> 'empty' THEN   -- 初めは空（そのあと表が V-1・V-2 で移す）
      RAISE EXCEPTION '試行のせいの欄は空から始めます' USING ERRCODE = 'check_violation', CONSTRAINT = 'attempt_blame_once';
    END IF;
    RETURN NEW;
  END IF;
  IF NEW.attempt_id <> OLD.attempt_id OR NEW.tenant_id <> OLD.tenant_id OR NEW.client_id <> OLD.client_id THEN
    RAISE EXCEPTION '試行のせいの欄の試行は変えられません' USING ERRCODE = 'check_violation', CONSTRAINT = 'attempt_blame_once';
  END IF;
  -- 移ってよいのは表の V-1〜V-4 だけ（空→分からない・空→こちら・分からない→番号・分からない→回線）。番号・回線・こちらは書き直せない（V-5）
  IF NEW.blame IS DISTINCT FROM OLD.blame
     AND NOT (OLD.blame = 'empty' AND NEW.blame IN ('unknown', 'ours'))
     AND NOT (OLD.blame = 'unknown' AND NEW.blame IN ('number', 'line')) THEN
    RAISE EXCEPTION '試行のせいは1回だけ書けます（% から % へは移せません）', OLD.blame, NEW.blame
      USING ERRCODE = 'check_violation', CONSTRAINT = 'attempt_blame_once';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER attempt_blame_once BEFORE INSERT OR UPDATE OR DELETE ON salescall.attempt_blames
  FOR EACH ROW EXECUTE FUNCTION salescall.attempt_blame_once();

-- ═════════ (3) 移った記録 ═════════
CREATE TABLE salescall.state_moves (
  move_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id   uuid NOT NULL REFERENCES salescall.tenants,
  client_id   uuid,                              -- 行・番号・試行のせいの記録は、その依頼者（回線・契約者は無し）
  subject     text CHECK (subject IN ('line', 'tenant', 'task', 'number', 'attempt')),
  key         uuid,                              -- 回線・契約者・行・番号（連絡先）・試行
  event       text CHECK (event ~ '^E[0-9]{1,2}$'),   -- 出来事の記号（17-2）
  event_at    timestamptz,                       -- 出来事の時刻
  from_stage  text,
  to_stage    text,
  table_rows  text[] NOT NULL DEFAULT '{}',      -- 引いた表の行番号（L-2・R-1 など）
  actions     jsonb NOT NULL DEFAULT '[]',       -- することの並び（影なので記録だけ。行わない）
  mistake     jsonb,                             -- 表を引けなかった（誤りの値。表に無い・重なり・時刻の前 など）
  failure     text,                              -- 影の書き込みが失敗した（誤りの種類の名前と文。今の作りは続けた）
  once        text,                              -- 1回だけ起こす出来事の鍵（直した版＝E11 など）
  origin      text NOT NULL CHECK (btrim(origin) <> ''),   -- 起こした所（core/health の入口の名前）
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CHECK ((failure IS NULL) = (subject IS NOT NULL AND key IS NOT NULL AND event IS NOT NULL AND event_at IS NOT NULL
                              AND from_stage IS NOT NULL AND to_stage IS NOT NULL)),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id)
);
CREATE INDEX state_moves_by_key ON salescall.state_moves (subject, key, move_id);
CREATE UNIQUE INDEX state_moves_once ON salescall.state_moves (tenant_id, subject, key, once) WHERE once IS NOT NULL;
CREATE TRIGGER state_moves_frozen BEFORE UPDATE OR DELETE ON salescall.state_moves
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();

-- ═════════ 読む範囲（行ごとの制限）と権限：アプリの役は読むだけ ═════════
ALTER TABLE salescall.line_states ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON salescall.line_states
  USING (tenant_id = salescall.cur_tenant()) WITH CHECK (tenant_id = salescall.cur_tenant());
ALTER TABLE salescall.tenant_states ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON salescall.tenant_states
  USING (tenant_id = salescall.cur_tenant()) WITH CHECK (tenant_id = salescall.cur_tenant());
ALTER TABLE salescall.task_states ENABLE ROW LEVEL SECURITY;
CREATE POLICY client_scope ON salescall.task_states
  USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
  WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client());
ALTER TABLE salescall.number_states ENABLE ROW LEVEL SECURITY;
CREATE POLICY client_scope ON salescall.number_states
  USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
  WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client());
ALTER TABLE salescall.attempt_blames ENABLE ROW LEVEL SECURITY;
CREATE POLICY client_scope ON salescall.attempt_blames
  USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
  WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client());
ALTER TABLE salescall.state_moves ENABLE ROW LEVEL SECURITY;
CREATE POLICY scope ON salescall.state_moves
  USING (tenant_id = salescall.cur_tenant() AND (client_id IS NULL OR client_id = salescall.cur_client()))
  WITH CHECK (tenant_id = salescall.cur_tenant() AND (client_id IS NULL OR client_id = salescall.cur_client()));
GRANT SELECT ON salescall.line_states, salescall.tenant_states, salescall.task_states, salescall.number_states,
                salescall.attempt_blames, salescall.state_moves TO salescall_app;   -- 書くのは下の2つの関数だけ

-- ═════════ (4) 表を引く入口が使う2つの関数（持ち主の権限。取引の契約者の行だけ） ═════════
-- 鍵を取って読む（17章：回線→契約者の順。行・番号・試行のせいはそのあと。順は呼ぶ側＝core/health.py が並べて渡す）。無ければ、渡された
-- 初めの状態で作る（created＝いま作った）。p_want は [{"s": 欄, "k": 鍵, "init": {"stage", "state"}}] か、行だけ {"s": "task",
-- "stages": [段…]}（その段の行を、取引の契約者の中から全部。依頼者をまたぐ＝話し終えた電話・直した版を、復旧を待つ行へ渡すため）。
-- 試行は、その試行の行・連絡先・回線・終わった時刻も返す（回線の表が試行のせいを決めたとき、その試行の行と番号へ渡すため）。
CREATE FUNCTION salescall.health_take(p_want jsonb) RETURNS jsonb
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
REVOKE ALL ON FUNCTION salescall.health_take(jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.health_take(jsonb) TO salescall_app;

-- 次の状態と移った記録を書く（p_rows＝[{"s", "k", "stage", "state"}]・p_moves＝移った記録の並び）。欄の行は health_take で作ったものだけ
-- （無ければ断る）。試行のせいの欄は、1回だけ書ける守り（attempt_blame_once）を通る。記録の依頼者は、その欄の行から決める
CREATE FUNCTION salescall.health_save(p_rows jsonb, p_moves jsonb) RETURNS void
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
