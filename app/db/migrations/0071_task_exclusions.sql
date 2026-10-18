-- 0071 行ごとの「かけない」（除外。2026-10-18 ユーザー決定：「電話番号＆お客様のリストから、かけないを入力できる列や、取り込んだ
-- データをダッシュボードから、レ点や一括チェックでかけないを選択できるようにしとくのがいい」。Intent.md 5-3 (8)・7-2 の除外条件。
-- DESIGN.md 3-4-2・15-2 の167。書いた人：CL-2）
--
-- 発信禁止（お客様が断った・依頼者の「かけないで」の一覧）とは別のもの：発信禁止は、お客様（番号）を止め、取り消すのは依頼者から
-- 正式に求められたときだけ。「かけない」は、依頼者や契約者の都合で、その一覧の行をかけないでおく印で、あとから戻せる
-- （ダッシュボードで外す）。
-- (1) 印（task_exclusions）：行ごとに、かけない／かけてよいに戻す、の記録を足していく（最新が正本）。入口は2つ：
--     一覧の取込の「かけない」の列（source import。取込の記録を指す）・ダッシュボードのレ点や一括（source dashboard。管理者か契約者だけ）。
-- (2) 行がいま「かけない」かは、この関数の1か所（salescall.task_excluded）。門も、候補を出す処理も、これを読む。

CREATE TABLE salescall.task_exclusions (
  tenant_id  uuid NOT NULL,
  client_id  uuid NOT NULL,
  seq        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  task_id    uuid NOT NULL,
  excluded   boolean NOT NULL,                        -- true＝かけない・false＝かけてよいに戻す
  reason     text NOT NULL CHECK (btrim(reason) <> ''),
  source     text NOT NULL CHECK (source IN ('import', 'dashboard')),
  import_id  uuid,                                    -- 取込の「かけない」の列から付けたとき、その取込
  decided_by text NOT NULL CHECK (btrim(decided_by) <> ''),
  decided_at timestamptz NOT NULL DEFAULT now(),
  CHECK ((source = 'import') = (import_id IS NOT NULL)),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, task_id) REFERENCES salescall.tasks (client_id, task_id),
  FOREIGN KEY (client_id, import_id) REFERENCES salescall.imports (client_id, import_id)
);
CREATE INDEX task_exclusions_latest ON salescall.task_exclusions (task_id, seq DESC);
CREATE FUNCTION salescall.task_exclusion_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.source = 'dashboard' AND salescall.cur_actor_kind() IS DISTINCT FROM 'operator'
     AND salescall.cur_actor_kind() IS DISTINCT FROM 'admin' THEN
    RAISE EXCEPTION 'ダッシュボードで「かけない」を付け外しできるのは、管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'task_exclusion_by_person';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER task_exclusion_guard BEFORE INSERT ON salescall.task_exclusions
  FOR EACH ROW EXECUTE FUNCTION salescall.task_exclusion_guard();
CREATE TRIGGER task_exclusions_frozen BEFORE UPDATE OR DELETE ON salescall.task_exclusions
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
ALTER TABLE salescall.task_exclusions ENABLE ROW LEVEL SECURITY;
CREATE POLICY client_scope ON salescall.task_exclusions
  USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
  WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client());
GRANT SELECT, INSERT ON salescall.task_exclusions TO salescall_app;

-- 行がいま「かけない」か（最新の印。印が無ければ、かけてよい）。判断はこの1か所
CREATE FUNCTION salescall.task_excluded(p_task uuid) RETURNS boolean LANGUAGE sql STABLE AS $$
  SELECT coalesce((SELECT e.excluded FROM salescall.task_exclusions e WHERE e.task_id = p_task ORDER BY e.seq DESC LIMIT 1), false)
$$;
