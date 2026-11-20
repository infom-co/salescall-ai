-- 0264 依頼者が受けられる日時の空き（accept_free）を業務ごとに置く（2026-11-20。書いた人：CL-2。DESIGN_15-2 の916。CL-1 の連絡帳 4-212・CL-2 の連絡帳 4-445）
--
-- ✅ 2026-11-20 ユーザー（計画書 9-4「担当者が不在」の行・伺い60）「1.空スケジュールの中から2つ　2.別の空スケジュールの2つ」：アポと依頼者の日時の質問の2択は、
-- 依頼者が受けられる日時の空きから作る。空きは毎日変わる運用の値なので、版を持つ業務設定には入れない（入れると空きを直すたびに版と承認が増える）。
-- ⇒ 業務ごとの記録（書き換えない・消さない。いまの空きは、その業務のいちばん新しい記録）。形の確かめは core/free_slots.check の1か所。書くのは管理者か契約者だけ。
-- 会話へは、その通話の日と時刻を渡す1か所（core/conversation_io.call_dates）が渡す。
CREATE TABLE salescall.business_free_slots (
  tenant_id    uuid NOT NULL,
  client_id    uuid NOT NULL,
  business_id  uuid NOT NULL,
  seq          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  slots        jsonb NOT NULL CHECK (jsonb_typeof(slots) = 'array'),   -- [["YYYY-MM-DD","HH:MM","HH:MM"], …]（日・時刻の順・重ならない）
  set_by       text NOT NULL CHECK (btrim(set_by) <> ''),
  set_at       timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (client_id, business_id) REFERENCES salescall.businesses (client_id, business_id),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id)
);
CREATE INDEX business_free_slots_latest ON salescall.business_free_slots (business_id, seq DESC);
CREATE TRIGGER stamp_now BEFORE INSERT ON salescall.business_free_slots FOR EACH ROW EXECUTE FUNCTION salescall.stamp_now('set_at');
CREATE TRIGGER business_free_slots_frozen BEFORE UPDATE OR DELETE ON salescall.business_free_slots
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
CREATE FUNCTION salescall.business_free_slots_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT salescall.cur_is_person() THEN
    RAISE EXCEPTION '依頼者が受けられる日時の空きを書くのは、管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'business_free_slots_by_person';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER business_free_slots_guard BEFORE INSERT ON salescall.business_free_slots
  FOR EACH ROW EXECUTE FUNCTION salescall.business_free_slots_guard();
ALTER TABLE salescall.business_free_slots ENABLE ROW LEVEL SECURITY;
CREATE POLICY client_scope ON salescall.business_free_slots
  USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
  WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client());
GRANT SELECT, INSERT ON salescall.business_free_slots TO salescall_app;
