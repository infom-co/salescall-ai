-- 0184 依頼者からの解決策を、その業務の想定問答に足す（(h)。2026-11-02 5時56分 ユーザー決定。書いた人：CL-2。DESIGN.md 3-8-3）
--
-- ✅ ユーザー：「依頼者から、解決策の連絡がフィードバックされたら、こちらのトークスクリプトに追加する。」・2026-11-02 21時37分の流れ
--    （契約者が画面に書く・依頼者が書き込んで返した報告のファイルを取り込む、の2つの入口を同じ1か所へ。問いの名前は AI が決める＝今ある
--    想定問答と同じ問いなら置き換え、新しい問いなら足す。人は確かめない。取込と同じ確かめを通して新しい版を作る。どの点にどの解決策を
--    足したかを残す）。CL-1 の連絡帳 4-66 のあとの (h)：点＝通話の報告の not_agreed のうち explained が偽のもの。
-- 書くのは core/client_solutions.py の1か所（足していくだけ）。version_id＝足した版（0183 の版のつながり。cause solution）。
-- 取込と同じ確かめに通らなかった（検証モードで AI の読みを契約者が確かめていない など）ときは、version_id が無く problems に理由が残る
-- （契約者が確かめてから、同じ1か所でもう一度足す＝そのときは新しい行）。

CREATE TABLE salescall.client_solutions (
  tenant_id     uuid NOT NULL,
  client_id     uuid NOT NULL,
  solution_id   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  attempt_id    uuid NOT NULL,
  task_id       uuid NOT NULL,
  point_index   integer NOT NULL CHECK (point_index >= 0),    -- 通話の報告の not_agreed の何番目か（0から）
  point_text    text NOT NULL CHECK (btrim(point_text) <> ''),   -- ご納得いただけなかった点（報告の point、無ければ said の写し）
  solution_text text NOT NULL CHECK (btrim(solution_text) <> ''),
  answer_name   text NOT NULL CHECK (btrim(answer_name) <> ''),  -- AI が決めた問いの名前
  replaced      boolean NOT NULL,                                -- 同じ問いの答えを置き換えた（偽＝新しい問いを足した）
  origin        text NOT NULL CHECK (origin IN ('dashboard', 'report_file')),
  version_id    uuid,                                            -- 足した版（無い＝確かめに通らなかった）
  problems      text[] NOT NULL DEFAULT '{}',
  recorded_by   text NOT NULL CHECK (btrim(recorded_by) <> ''),
  recorded_at   timestamptz NOT NULL DEFAULT now(),
  CHECK ((version_id IS NULL) = (cardinality(problems) > 0)),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, attempt_id, task_id) REFERENCES salescall.attempts (client_id, attempt_id, task_id),
  FOREIGN KEY (client_id, version_id) REFERENCES salescall.business_versions (client_id, version_id)
);
CREATE INDEX client_solutions_point ON salescall.client_solutions (attempt_id, point_index, recorded_at DESC);
CREATE FUNCTION salescall.client_solution_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF salescall.cur_actor_kind() IS DISTINCT FROM 'operator' AND salescall.cur_actor_kind() IS DISTINCT FROM 'admin' THEN
    RAISE EXCEPTION '依頼者の解決策を足せるのは、管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'client_solution_by_person';
  END IF;
  IF NEW.recorded_by IS DISTINCT FROM salescall.cur_actor() THEN
    RAISE EXCEPTION '記録した方は、いま操作している方です' USING ERRCODE = 'check_violation', CONSTRAINT = 'client_solution_recorded_by';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER client_solution_guard BEFORE INSERT ON salescall.client_solutions
  FOR EACH ROW EXECUTE FUNCTION salescall.client_solution_guard();
CREATE TRIGGER client_solutions_frozen BEFORE UPDATE OR DELETE ON salescall.client_solutions
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
ALTER TABLE salescall.client_solutions ENABLE ROW LEVEL SECURITY;
CREATE POLICY client_scope ON salescall.client_solutions
  USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
  WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client());
GRANT SELECT, INSERT ON salescall.client_solutions TO salescall_app;
