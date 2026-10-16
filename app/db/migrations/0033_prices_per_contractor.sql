-- 0033 費用の単価は契約者ごと（2026-10-16 ユーザー：「費用の単価…⇒契約者　使用するAIのAPIと回線を選べます。楽天リンクは初期設定」）。
-- 書いた人：CL-2
-- - 単価は「どのサービス（会話AIの API・別の耳・回線の方式）か」ごとに持つ（service。契約者が選んだサービスの単価を使う）。
--   回線の方式は回線の表の method（初期は rakuten_link）。会話AI・別の耳は、使ったサービスを利用量を記録する処理が渡す。
-- - 管理者が入れる単価（tenant_id が空）は、初めの値（どの契約者にも効く）。契約者が入れた単価（その契約者だけ）が、それより強い
--   （設定と同じく、川下が強い）。単価を決めるのは core/usage.py の1か所（price_for）。
-- - 会話AI・別の耳は、無料枠か有料枠かでも単価が違う（2026-10-16 ユーザー：「APIも無料枠と有料枠が選択可能」）＝ tier。
--   回線は tier を持たない。
-- - 報酬の単価（成果1件など）は依頼者ごと（reward_terms）＝これまでどおり。
ALTER TABLE salescall.price_versions
  ADD COLUMN tenant_id uuid REFERENCES salescall.tenants,
  ADD COLUMN service text,
  ADD COLUMN tier text CHECK (tier IN ('free', 'paid'));
-- 単価は変えない決まり（変更の拒み）なので、表の持ち主が、この移行の中でだけ拒みを外して、前からの行にサービスと枠を入れる
ALTER TABLE salescall.price_versions DISABLE TRIGGER price_versions_frozen;
UPDATE salescall.price_versions SET service = CASE source WHEN 'ai' THEN 'gemini-3.8-live'
                                                        WHEN 'ear' THEN 'gemini-3.5-transcribe-live'
                                                        ELSE 'rakuten_link' END,
                                   tier = CASE WHEN source IN ('ai', 'ear') THEN 'free' END;
ALTER TABLE salescall.price_versions ENABLE TRIGGER price_versions_frozen;
ALTER TABLE salescall.price_versions
  ALTER COLUMN service SET NOT NULL,
  ADD CONSTRAINT price_versions_service CHECK (btrim(service) <> ''),
  ADD CONSTRAINT price_versions_tier CHECK ((source IN ('ai', 'ear')) = (tier IS NOT NULL)),
  DROP CONSTRAINT price_versions_source_unit_valid_from_key,
  ADD CONSTRAINT price_versions_one_per_time UNIQUE NULLS NOT DISTINCT (tenant_id, source, service, tier, unit, valid_from);

-- 初めの値（tenant_id が空）は管理者だけ、契約者の単価は、その契約者（か管理者）。システムの処理は単価を入れない
CREATE OR REPLACE FUNCTION salescall.system_values_by_admin() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF current_user <> 'salescall_owner' AND salescall.cur_actor_kind() IS DISTINCT FROM 'admin'
     AND ((TG_TABLE_NAME = 'price_versions' AND to_jsonb(NEW) ->> 'tenant_id' IS NULL)
          OR to_jsonb(NEW) ->> 'scope' = 'system') THEN
    RAISE EXCEPTION 'システム全体の値（すべての契約者に効く初めの値を含む）を変えられるのは、管理者だけです'
      USING ERRCODE = 'insufficient_privilege', CONSTRAINT = 'system_values_by_admin';
  END IF;
  IF current_user <> 'salescall_owner' AND TG_TABLE_NAME = 'price_versions'
     AND salescall.cur_actor_kind() IS DISTINCT FROM 'admin' AND salescall.cur_actor_kind() IS DISTINCT FROM 'operator' THEN
    RAISE EXCEPTION '単価を入れられるのは、管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'system_values_by_admin';
  END IF;
  RETURN NEW;
END $$;

DROP POLICY price_read ON salescall.price_versions;
CREATE POLICY price_scope ON salescall.price_versions
  USING (tenant_id IS NULL OR tenant_id = salescall.cur_tenant())
  WITH CHECK (tenant_id IS NOT DISTINCT FROM salescall.cur_tenant());
