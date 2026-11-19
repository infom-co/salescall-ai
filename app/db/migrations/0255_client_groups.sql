-- 0255 依頼者のグループ（2026-11-19。書いた人：CL-2。DESIGN.md 3-9・15-2 の896。CL-3 の連絡帳 4-1b の 15）
-- ✅ 2026-11-18 管理者「依頼者が増えた場合は『グループ』を作れるといいね」：契約者ごとのグループ（名前・並び順）と、グループと依頼者の組（1つの依頼者が
-- 複数のグループに入れる）。グループは画面の並べ方だけに使い、データの分離・権限には使わない（分離は契約者・依頼者の2段のまま＝11章）。
-- 作る・直す・やめるは管理者か契約者（画面）。書くのは core/registry の1か所。消さない（やめたグループは removed_at・出入りは足していく記録の
-- いちばん新しいもの）。名前は、同じ契約者の、やめていないグループの中で重ならない（表記をそろえた形＝0248 と同じ）。
CREATE TABLE salescall.client_groups (
  tenant_id   uuid NOT NULL REFERENCES salescall.tenants,
  group_id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name        text NOT NULL CHECK (btrim(name) <> ''),
  position    integer NOT NULL CHECK (position >= 1),
  created_at  timestamptz NOT NULL DEFAULT now(),
  removed_at  timestamptz,
  UNIQUE (tenant_id, group_id)
);
CREATE UNIQUE INDEX client_groups_same_name ON salescall.client_groups (tenant_id, salescall.compare_form(name)) WHERE removed_at IS NULL;
CREATE TABLE salescall.client_group_changes (
  tenant_id   uuid NOT NULL,
  seq         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  group_id    uuid NOT NULL,
  client_id   uuid NOT NULL,
  member      boolean NOT NULL,                                     -- 真＝入れた／偽＝外した
  set_by      text NOT NULL CHECK (btrim(set_by) <> ''),
  set_at      timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, group_id) REFERENCES salescall.client_groups (tenant_id, group_id),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id)
);
CREATE TRIGGER stamp_now BEFORE INSERT ON salescall.client_group_changes FOR EACH ROW EXECUTE FUNCTION salescall.stamp_now('set_at');
CREATE TRIGGER client_group_changes_frozen BEFORE UPDATE OR DELETE ON salescall.client_group_changes
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
CREATE FUNCTION salescall.client_group_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT salescall.cur_is_person() THEN
    RAISE EXCEPTION '依頼者のグループを作る・直すのは、管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'client_group_by_person';
  END IF;
  IF TG_TABLE_NAME = 'client_group_changes' THEN
    IF NEW.set_by IS DISTINCT FROM salescall.cur_actor() THEN
      RAISE EXCEPTION '書いた方は、いま操作している方です' USING ERRCODE = 'check_violation', CONSTRAINT = 'client_group_set_by';
    END IF;
  ELSIF TG_OP = 'UPDATE' THEN
    IF OLD.removed_at IS NOT NULL THEN
      RAISE EXCEPTION 'やめたグループは直せません' USING ERRCODE = 'check_violation', CONSTRAINT = 'client_group_removed';
    END IF;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER client_group_guard BEFORE INSERT OR UPDATE ON salescall.client_groups FOR EACH ROW EXECUTE FUNCTION salescall.client_group_guard();
CREATE TRIGGER client_group_guard BEFORE INSERT ON salescall.client_group_changes FOR EACH ROW EXECUTE FUNCTION salescall.client_group_guard();
ALTER TABLE salescall.client_groups ENABLE ROW LEVEL SECURITY;
ALTER TABLE salescall.client_group_changes ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON salescall.client_groups
  USING (tenant_id = salescall.cur_tenant()) WITH CHECK (tenant_id = salescall.cur_tenant());
CREATE POLICY tenant_scope ON salescall.client_group_changes
  USING (tenant_id = salescall.cur_tenant()) WITH CHECK (tenant_id = salescall.cur_tenant());
GRANT SELECT, INSERT ON salescall.client_groups, salescall.client_group_changes TO salescall_app;
GRANT UPDATE (name, position, removed_at) ON salescall.client_groups TO salescall_app;
