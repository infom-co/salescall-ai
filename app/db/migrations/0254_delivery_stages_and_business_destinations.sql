-- 0254 依頼者へ渡せる結果の段・送り先の書き手と自動の書き戻し・業務ごとの送り先（2026-11-19。書いた人：CL-2。DESIGN.md 4-5・10-1・15-2 の893〜895。
--      CL-3 の連絡帳 4-1 の 5・4-1b の 17・19）
--
-- (1) 渡せる段（CL-3 の 17）：✅ 2026-11-18 管理者「サンプル検証の段階は依頼者に何も渡さない：渡さないが、出力はできる。実検証は渡せる（人が見ている
--     確認する実際の業務検証）。実検証が終わった後フルオートへ移行」・「サンプル検証＝模擬通話」。⛔ 前（0043・2026-10-16 ユーザー「依頼者に途中作業
--     していることは絶対に知らせてはいけない」）は、本番で確定した版にしか送り先への連携を作れなかった。⇒ 確定した版（実検証で契約者が確かめた
--     版も、本番の版も）なら作れる。模擬通話は、行の結果を作らない（品質の段の合否 mock_runs だけ）ので、どの道でも依頼者へ渡らない。守りは
--     「確定した版にしか作れない」の1つ（0044 と同じく、同じ守りを2つ持たない）。
ALTER TABLE salescall.deliveries DROP CONSTRAINT deliveries_production_release;
ALTER TABLE salescall.deliveries DROP COLUMN release_stage;
ALTER TABLE salescall.result_releases DROP CONSTRAINT result_releases_with_stage;
ALTER TABLE salescall.deliveries ADD CONSTRAINT deliveries_released
  FOREIGN KEY (client_id, task_id, version_no) REFERENCES salescall.result_releases (client_id, task_id, version_no);

-- (2) 自動の書き戻し先（CL-3 の 5。DESIGN.md 4-5）：確定するたびに自動で書き戻す送り先は、「同じ鍵なら上書き」で受けられる送り先だけ
--     （受けられない先へ自動で送ると、結果を直したとき依頼者の側に2件残る）。
ALTER TABLE salescall.destinations ADD COLUMN auto_writeback boolean NOT NULL DEFAULT false;
ALTER TABLE salescall.destinations ADD CONSTRAINT destinations_auto_only_upsert CHECK (NOT auto_writeback OR upsert_capable);

-- (3) 業務ごとの送り先（CL-3 の 19。✅ 2026-11-18 管理者「業務によって変わりえます」）：業務が送り先を選んでいれば、その業務の結果はその送り先へだけ。
--     選んでいなければ、依頼者の送り先のすべて（決めるのは core/destinations.for_business の1か所）。選ぶ・外すは足していく記録（いちばん新しいもの）。
CREATE TABLE salescall.business_destinations (
  tenant_id      uuid NOT NULL,
  client_id      uuid NOT NULL,
  seq            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  business_id    uuid NOT NULL,
  destination_id uuid NOT NULL,
  chosen         boolean NOT NULL,                                  -- 真＝この業務はこの送り先へ渡す／偽＝選ぶのをやめた
  set_by         text NOT NULL CHECK (btrim(set_by) <> ''),
  set_at         timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, business_id) REFERENCES salescall.businesses (client_id, business_id),
  FOREIGN KEY (client_id, destination_id) REFERENCES salescall.destinations (client_id, destination_id)
);
CREATE TRIGGER stamp_now BEFORE INSERT ON salescall.business_destinations FOR EACH ROW EXECUTE FUNCTION salescall.stamp_now('set_at');
CREATE TRIGGER business_destinations_frozen BEFORE UPDATE OR DELETE ON salescall.business_destinations
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
CREATE FUNCTION salescall.business_destination_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT salescall.cur_is_person() OR NEW.set_by IS DISTINCT FROM salescall.cur_actor() THEN
    RAISE EXCEPTION '業務の送り先を選べるのは、管理者か契約者（いま操作している方）だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'business_destination_by_person';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER business_destination_guard BEFORE INSERT ON salescall.business_destinations
  FOR EACH ROW EXECUTE FUNCTION salescall.business_destination_guard();
ALTER TABLE salescall.business_destinations ENABLE ROW LEVEL SECURITY;
CREATE POLICY client_scope ON salescall.business_destinations
  USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
  WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client());
GRANT SELECT, INSERT ON salescall.business_destinations TO salescall_app;

-- (4) 送り先を作る・直すのは、管理者か契約者（画面＝出力の部品が書く。CL-3 の 5）。直せるのは、名前・「同じ鍵なら上書き」で受けられるか・自動で書き戻すか・
--     認証情報の参照だけ（方式は作るときに決め、変えるなら新しい送り先にする＝連携の台帳が、どの方式で送ったかを指せるように）。
GRANT UPDATE (name, upsert_capable, auto_writeback, credential_ref) ON salescall.destinations TO salescall_app;
CREATE FUNCTION salescall.destination_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT salescall.cur_is_person() THEN
    RAISE EXCEPTION '送り先を作る・直すのは、管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege', CONSTRAINT = 'destination_by_person';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER destination_guard BEFORE INSERT OR UPDATE ON salescall.destinations FOR EACH ROW EXECUTE FUNCTION salescall.destination_guard();
