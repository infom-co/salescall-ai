-- 0022 お詫びの電話（2026-10-16 ユーザー決定：A）。書いた人：CL-2
-- 言ってはいけないことを言った通話のあと、知らせを見た管理者が「お詫びする」と決めたら（その誤りの言葉と正しい内容を入れる）、
-- AI がその相手にもう一度電話して、決まった文面で伝えて終える：
--   「先ほどのお電話で、誤って「○○」というご案内をしてしまいましたが、正しくは「○○」となります。
--    不正確なご案内をしてしまいまして、誠に申し訳ございませんでした。」（文面の正本は core/apology.py の1か所）
-- - お詫びの指示（apology_orders）：言ってはいけないことを言った通話1つに1つ。出せるのは人（運営者・システムの管理者）だけ。変更不可。
-- - お詫びの電話の結果（apology_outcomes）：伝えられたか。書くのは締めの処理（core/settle.py）だけ。変更不可。
-- - 発信の目的に「お詫び」（apology）を足す。お詫びの電話は、業務の結果を変えない（業務の判定をしない）。
-- - 「締めが済んだか」は、結果の版があるか、お詫びの電話の結果があるか（データベースの1か所 salescall.is_settled）。

-- 発信の目的の決まりを、お詫びを足した形に置き換える（0010 の決まりは名前が自動で付いたので、中身で探して外す）
DO $$
DECLARE r record;
BEGIN
  FOR r IN SELECT conrelid::regclass AS t, conname FROM pg_constraint
           WHERE conrelid IN ('salescall.attempts'::regclass, 'salescall.task_next_actions'::regclass) AND contype = 'c'
             AND pg_get_constraintdef(oid) LIKE '%purpose%' AND pg_get_constraintdef(oid) LIKE '%normal%' LOOP
    EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', r.t, r.conname);
  END LOOP;
END $$;
ALTER TABLE salescall.attempts
  ADD CONSTRAINT attempts_purpose_kind CHECK (purpose IN ('normal', 'ask_number_again', 'apology'));
ALTER TABLE salescall.task_next_actions
  ADD CONSTRAINT task_next_actions_purpose_kind CHECK (purpose IS NULL OR purpose IN ('normal', 'ask_number_again', 'apology'));

CREATE TABLE salescall.apology_orders (
  tenant_id         uuid NOT NULL,
  client_id         uuid NOT NULL,
  order_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  task_id           uuid NOT NULL,
  source_attempt_id uuid NOT NULL UNIQUE,            -- 言ってはいけないことを言った通話（1つに1つ）
  wrong_text        text NOT NULL CHECK (btrim(wrong_text) <> ''),     -- 誤って案内したこと（文面の1つ目の「○○」）
  correct_text      text NOT NULL CHECK (btrim(correct_text) <> ''),   -- 正しい内容（文面の2つ目の「○○」）
  ordered_by        text NOT NULL CHECK (btrim(ordered_by) <> ''),
  ordered_at        timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, source_attempt_id, task_id) REFERENCES salescall.attempts (client_id, attempt_id, task_id)
);
CREATE FUNCTION salescall.apology_order_check() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF salescall.cur_actor_kind() NOT IN ('operator', 'admin') OR NEW.ordered_by IS DISTINCT FROM salescall.cur_actor() THEN
    RAISE EXCEPTION 'お詫びの電話を決めるのは人（運営者・システムの管理者）だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'apology_order_by_person';
  END IF;
  IF NOT salescall.said_forbidden(NEW.source_attempt_id) THEN
    RAISE EXCEPTION 'お詫びの電話は、言ってはいけないことを言った通話のあとだけです' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'apology_order_after_forbidden';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER apology_order_check BEFORE INSERT ON salescall.apology_orders
  FOR EACH ROW EXECUTE FUNCTION salescall.apology_order_check();

CREATE TABLE salescall.apology_outcomes (
  tenant_id   uuid NOT NULL,
  client_id   uuid NOT NULL,
  attempt_id  uuid PRIMARY KEY,                      -- お詫びの電話（目的 apology の試行）
  order_id    bigint NOT NULL REFERENCES salescall.apology_orders,
  delivered   boolean NOT NULL,                      -- 相手（その通話で話した人）に、文面を伝えられたか
  recorded_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, attempt_id) REFERENCES salescall.attempts (client_id, attempt_id)
);

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['apology_orders', 'apology_outcomes'] LOOP
    EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON salescall.%I
                      FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change()', t || '_frozen', t);
    EXECUTE format('ALTER TABLE salescall.%I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY client_scope ON salescall.%I
                      USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
                      WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())', t);
    EXECUTE format('GRANT SELECT, INSERT ON salescall.%I TO salescall_app', t);
  END LOOP;
END $$;

-- 締めが済んだか（1か所）：結果の版がある（業務の判定・判定し直し）か、お詫びの電話の結果がある
CREATE FUNCTION salescall.is_settled(p_attempt uuid) RETURNS boolean
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT EXISTS (SELECT 1 FROM salescall.task_results r WHERE r.attempt_id = p_attempt)
      OR EXISTS (SELECT 1 FROM salescall.apology_outcomes o WHERE o.attempt_id = p_attempt)
$$;
GRANT EXECUTE ON FUNCTION salescall.is_settled(uuid) TO salescall_app;

INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, bound_key, bound_side) VALUES
('retry.apology_max', 'override', 'integer', 'お詫びの電話に相手が出なかったとき、別の時間帯にかけ直す回数の上限（最初の1本を含む）', 'DESIGN.md 10-1', NULL, NULL);
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('retry.apology_max', 'system', '3', 'provisional', '番号の聞き直しと同じ（2026-10-15 ユーザー決定の retry.ask_number_again_max＝3。時間帯により出られないことはよくある）', 'お詫びの電話の実績を見て', 'CL-2');

-- 時給の時間の材料に、お詫びの電話を数えない（こちらの誤りのため。0020 の salescall.work_seconds を置き換える）
CREATE OR REPLACE FUNCTION salescall.work_seconds(p_from timestamptz, p_to timestamptz, p_gap_max integer)
RETURNS TABLE (at timestamptz, seconds numeric)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  WITH line_calls AS (
    SELECT a.client_id, a.purpose, a.claimed_at, a.ended_at,
           lead(a.client_id) OVER w AS next_client, lead(a.purpose) OVER w AS next_purpose, lead(a.claimed_at) OVER w AS next_at
    FROM salescall.attempts a
    WHERE a.state NOT IN ('claimed', 'released') AND a.ended_at IS NOT NULL
      AND a.line_id IN (SELECT b.line_id FROM salescall.attempts b WHERE b.client_id = salescall.cur_client())
    WINDOW w AS (PARTITION BY a.line_id ORDER BY a.claimed_at)
  )
  SELECT c.claimed_at,
         extract(epoch FROM c.ended_at - greatest(c.claimed_at, p_from))
         + CASE WHEN c.next_client = c.client_id AND c.next_purpose <> 'apology'
                     AND c.next_at - c.ended_at <= p_gap_max * interval '1 second'
                THEN extract(epoch FROM least(c.next_at, p_to) - c.ended_at) ELSE 0 END
  FROM line_calls c
  WHERE c.client_id = salescall.cur_client() AND salescall.cur_client() IS NOT NULL AND c.purpose <> 'apology'
    AND c.ended_at > p_from AND c.claimed_at <= p_to
$$;
