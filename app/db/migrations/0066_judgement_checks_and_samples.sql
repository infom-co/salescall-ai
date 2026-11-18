-- 0066 誤判定率の正解と抜き取り（Intent.md 16-3・11-4 第2版。DESIGN.md 10-3・15-2 の164。2026-10-18。書いた人：CL-2）
--
-- Intent.md 16-3：「誤判定率：通話のサンプルを管理者か契約者が聞いて正解を付け（検証モードで契約者が直した結果も、正解として使う）、
-- システムの判定と比べて出す。サンプルの件数と抜き方を、記録に残す」。
-- 前は、正解を残す所も、抜き取る処理も無く、測り終わるまで消さない録音の印（0056）が、いつまでも外れなかった。
--
-- (1) 正解（judgement_checks）：システムが判定した結果の版1つごとに、管理者か契約者が付けた正解（業務の状態と、システムが渡した
--     項目のうち違っていたもの）。追記だけ（あとで付け直したら、新しい正解を足す。同じ版の正解は最新が正本）。
--     検証モードでは、契約者が結果を承認・直したときに入る。本番では、抜き取った通話を聞いて付ける。
-- (2) 抜き取り（sample_draws・sample_draw_calls）：業務ごと・週ごとに1回。前の週の、お客様と話せた本番の通話から無作為に選ぶ。
--     母数・選んだ数・乱数の種・選び方を記録する（同じ種で同じ選び方をやり直せる）。
-- (3) 抜き取る本数（業務ごとの設定 measure.sample_per_week）。

-- ═════════ (1) 正解 ═════════
CREATE TABLE salescall.judgement_checks (
  tenant_id   uuid NOT NULL,
  client_id   uuid NOT NULL,
  check_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  task_id     uuid NOT NULL,
  version_no  integer NOT NULL,                 -- 確かめた、システムが判定した結果の版
  attempt_id  uuid NOT NULL,                    -- その版のもとにした通話
  -- 検証モードの確かめ（すべての結果を確かめる）・抜き取り（無作為）・本番で契約者が気づいて直した（偏った材料。誤判定率には混ぜない）
  source      text NOT NULL CHECK (source IN ('verification', 'sample', 'correction')),
  truth_state salescall.business_state NOT NULL CHECK (truth_state NOT IN ('open', 'unknown')),   -- 正しい業務の状態
  wrong_items text[] NOT NULL DEFAULT '{}' CHECK (array_position(wrong_items, NULL) IS NULL),     -- 違っていた項目（質問の番号）
  checked_by  text NOT NULL CHECK (btrim(checked_by) <> ''),
  checked_at  timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, task_id, version_no) REFERENCES salescall.task_results (client_id, task_id, version_no),
  FOREIGN KEY (client_id, attempt_id) REFERENCES salescall.attempts (client_id, attempt_id)
);
CREATE INDEX judgement_checks_by_version ON salescall.judgement_checks (task_id, version_no, check_id);
-- 正解を付けるのは管理者か契約者だけ。確かめるのは、システムが判定した版（人の版ではない）で、その版のもとにした通話を指す
CREATE FUNCTION salescall.judgement_check_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF salescall.cur_actor_kind() IS DISTINCT FROM 'operator' AND salescall.cur_actor_kind() IS DISTINCT FROM 'admin' THEN
    RAISE EXCEPTION '正解を付けるのは、管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'judgement_check_by_person';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM salescall.task_results r WHERE r.task_id = NEW.task_id AND r.version_no = NEW.version_no
                 AND r.kind IN ('judged', 'rejudged') AND r.attempt_id = NEW.attempt_id) THEN
    RAISE EXCEPTION '正解を付けるのは、システムが判定した結果の版（その版の通話）だけです' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'judgement_check_of_system_version';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER judgement_check_guard BEFORE INSERT ON salescall.judgement_checks
  FOR EACH ROW EXECUTE FUNCTION salescall.judgement_check_guard();
CREATE TRIGGER judgement_checks_frozen BEFORE UPDATE OR DELETE ON salescall.judgement_checks
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();

-- ═════════ (2) 抜き取り ═════════
CREATE TABLE salescall.sample_draws (
  tenant_id   uuid NOT NULL,
  client_id   uuid NOT NULL,
  draw_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id uuid NOT NULL,
  week_start  date NOT NULL CHECK (extract(isodow FROM week_start) = 1),   -- 抜き取った週の月曜（日本時間）
  pool_size   integer NOT NULL CHECK (pool_size >= 0),   -- その週の、お客様と話せた本番の通話の数
  wanted      integer NOT NULL CHECK (wanted >= 0),      -- 設定の本数
  drawn       integer NOT NULL CHECK (drawn >= 0),
  seed        bigint NOT NULL,                           -- 乱数の種（同じ種で、同じ選び方をやり直せる）
  method      text NOT NULL CHECK (btrim(method) <> ''), -- 選び方（日本語）
  drawn_at    timestamptz NOT NULL DEFAULT now(),
  CHECK (drawn = least(pool_size, wanted)),
  UNIQUE (business_id, week_start),
  UNIQUE (client_id, draw_id),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, business_id) REFERENCES salescall.businesses (client_id, business_id)
);
CREATE TABLE salescall.sample_draw_calls (
  tenant_id  uuid NOT NULL,
  client_id  uuid NOT NULL,
  draw_id    uuid NOT NULL,
  attempt_id uuid NOT NULL,
  PRIMARY KEY (draw_id, attempt_id),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, draw_id) REFERENCES salescall.sample_draws (client_id, draw_id),
  FOREIGN KEY (client_id, attempt_id) REFERENCES salescall.attempts (client_id, attempt_id)
);
CREATE TRIGGER sample_draws_frozen BEFORE UPDATE OR DELETE ON salescall.sample_draws
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
CREATE TRIGGER sample_draw_calls_frozen BEFORE UPDATE OR DELETE ON salescall.sample_draw_calls
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['judgement_checks', 'sample_draws', 'sample_draw_calls'] LOOP
    EXECUTE format('ALTER TABLE salescall.%I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY client_scope ON salescall.%I
                      USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
                      WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())', t);
  END LOOP;
END $$;
GRANT SELECT, INSERT ON salescall.judgement_checks, salescall.sample_draws, salescall.sample_draw_calls TO salescall_app;

-- ═════════ (3) 抜き取る本数 ═════════
INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, finest_scope) VALUES
('measure.sample_per_week', 'override', 'integer',
 '誤判定率を測るために、業務ごとに1週間に抜き取る本番の通話の数（前の週の、お客様と話せた通話から無作為に選ぶ。管理者か契約者が録音を聞いて正解を付ける。0 なら抜き取らない）',
 'DESIGN.md 10-3', 'business');
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('measure.sample_per_week', 'system', '10', 'provisional',
 '1本を聞いて正解を付けるのに約3分として、1業務あたり週30分。6週で60本になり、誤りが0本なら「誤判定率は5%より低い」と言える（95%の確かさ）。誤りが見つかれば、本数を増やして測り直す',
 'ユーザーに決めていただく。最初の本番の業務の実測を見て', 'CL-2');
