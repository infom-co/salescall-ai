-- 0035 費用の上限は注意だけ・赤字は警告して、確認ボタンが1時間押されなければ止める（2026-10-16 ユーザー：「費用の1日の上限を超えた
-- ときの停止：注意喚起のみ」「赤字のときの停止：警告喚起後、確認ボタンを1時間押さなければ止める」）。書いた人：CL-2
-- - 赤字の警告（deficit_warnings）：赤字になったら警告を1つ入れ、契約者へ知らせる。期限（警告から設定 budget.deficit_grace の秒。
--   1時間）までに管理者か契約者が確認すれば止めない。確認しなければ、期限のあとで止める（原因 deficit）。確認は設定
--   budget.deficit_confirm_valid の秒だけ効き、そのあともまだ赤字なら、もう一度警告する。赤字でなくなったら、警告を閉じる。
-- - 書くのは core/usage.py だけ。確認は管理者か契約者だけ（データベースが守る）。
CREATE TABLE salescall.deficit_warnings (
  tenant_id    uuid NOT NULL,
  client_id    uuid NOT NULL,
  warning_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  warned_at    timestamptz NOT NULL DEFAULT now(),
  deadline     timestamptz NOT NULL,                 -- この時刻までに確認されなければ止める
  figures      text NOT NULL CHECK (btrim(figures) <> ''),   -- 警告したときの今月の費用と報酬
  confirmed_at timestamptz,
  confirmed_by text,
  closed_at    timestamptz,                          -- 赤字でなくなった・止めた（もう効かない）
  closed_why   text CHECK (closed_why IN ('not_deficit', 'stopped')),
  CHECK ((confirmed_at IS NULL) = (confirmed_by IS NULL)),
  CHECK ((closed_at IS NULL) = (closed_why IS NULL)),
  CHECK (deadline > warned_at),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id)
);
CREATE UNIQUE INDEX deficit_warnings_one_open ON salescall.deficit_warnings (client_id) WHERE closed_at IS NULL;
-- 変えてよいのは、確認（管理者か契約者が1回）と、閉じる（1回）だけ
CREATE FUNCTION salescall.deficit_warning_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION '赤字の警告は消せません' USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  IF (NEW.tenant_id, NEW.client_id, NEW.warning_id, NEW.warned_at, NEW.deadline, NEW.figures)
     IS DISTINCT FROM (OLD.tenant_id, OLD.client_id, OLD.warning_id, OLD.warned_at, OLD.deadline, OLD.figures)
     OR (OLD.confirmed_at IS NOT NULL AND (NEW.confirmed_at, NEW.confirmed_by) IS DISTINCT FROM (OLD.confirmed_at, OLD.confirmed_by))
     OR (OLD.closed_at IS NOT NULL AND (NEW.closed_at, NEW.closed_why) IS DISTINCT FROM (OLD.closed_at, OLD.closed_why)) THEN
    RAISE EXCEPTION '赤字の警告は、確認（1回）と閉じる（1回）しかできません' USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  IF OLD.confirmed_at IS NULL AND NEW.confirmed_at IS NOT NULL
     AND salescall.cur_actor_kind() IS DISTINCT FROM 'operator' AND salescall.cur_actor_kind() IS DISTINCT FROM 'admin' THEN
    RAISE EXCEPTION '赤字の警告を確認できるのは、管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'deficit_confirmed_by_person';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER deficit_warning_guard BEFORE UPDATE OR DELETE ON salescall.deficit_warnings
  FOR EACH ROW EXECUTE FUNCTION salescall.deficit_warning_guard();
ALTER TABLE salescall.deficit_warnings ENABLE ROW LEVEL SECURITY;
CREATE POLICY client_scope ON salescall.deficit_warnings
  USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
  WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client());
GRANT SELECT, INSERT, UPDATE ON salescall.deficit_warnings TO salescall_app;

-- 見守り（全体の取引）が、期限を過ぎた・確認が切れた警告のある依頼者を見つけるため（中身は依頼者の取引で確かめる）
CREATE FUNCTION salescall.open_deficit_warnings() RETURNS TABLE (tenant_id uuid, client_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT w.tenant_id, w.client_id FROM salescall.deficit_warnings w
  WHERE salescall.cur_tenant() IS NULL AND w.closed_at IS NULL
$$;
REVOKE ALL ON FUNCTION salescall.open_deficit_warnings() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.open_deficit_warnings() TO salescall_app;

INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, bound_key, bound_side) VALUES
('budget.deficit_grace', 'override', 'seconds', '赤字の警告から、確認されなければ止めるまでの秒数', 'DESIGN.md 12-1-2', NULL, NULL),
('budget.deficit_confirm_valid', 'override', 'seconds', '赤字の警告を確認したあと、まだ赤字でも、もう一度警告するまでの秒数', 'DESIGN.md 12-1-2', NULL, NULL);
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('budget.deficit_grace', 'system', '3600', 'decided', '2026-10-16 ユーザー決定：「警告喚起後、確認ボタンを1時間押さなければ止める」', '運用の実績を見て', 'ユーザー'),
('budget.deficit_confirm_valid', 'system', '86400', 'provisional',
 '確認したあとも赤字が続くなら、1日に1回は確かめてもらう（確認した時点の赤字を、ずっと見過ごさないため）', '運用の実績を見て', 'CL-2');
