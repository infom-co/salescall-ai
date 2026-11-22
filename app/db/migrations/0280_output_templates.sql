-- 0280 出力の型（依頼者へ渡す形）の置き場（2026-11-22。書いた人：CL-2。DESIGN_15-2.md の951。CL-3 の連絡帳 4-1c の 66 と 5-0000000000000000 の答え・
--      CL-2 の連絡帳 4-468 の案。設計書 7-3-5・7-3-5b）
-- ✅ 2026-11-21 管理者「依頼者への出力はエクセルの事もあれば、システムに登録する形もあるので業務ごとに設定します。基本の形はデータベースを出力することも
--    できるようにします。」（計画書 11-3）
--
-- (1) 型は、持ち主（依頼者＝business_id が NULL／業務）ごとの版の並び。足すだけ・書き換えない・消さない（だれが・いつ・なぜ変えたかが残る）。
--     いまの型は、その持ち主のいちばん新しい版。中身 body が NULL の版＝その持ち主は型を持たない（業務なら依頼者の型に従う・依頼者なら既定の型＝CL-3）。
--     前の版へ戻すのは、前の版の中身を新しい版として足す（業務設定・送り先と同じ扱い）。版の番号は持ち主ごとに1から続く（振るのはここの1か所）。
-- (2) 使う型を決めるのは core/output_templates.current の1か所：業務のいまの版に中身があれば、それ（依頼者の型を**まるごと置き換える**＝混ぜると、
--     どの値がどこから来たか見えなくなる。✅ CL-3 の答え）、無ければ依頼者のいまの版の中身、どちらも無ければ既定の型。
-- (3) 中身の形と、登録するときの確かめ（7-3-5 の (1)〜(6)）は CL-3 の持ち物（output/templates.check の1か所）。登録の入口 core/output_templates.save が
--     確かめを必ず通す（通らなければ登録しない）。データベースが守るのは：中身が JSON の物（または NULL）であること・持ち主の範囲（その契約者・その依頼者・
--     その依頼者の業務）・書くのは管理者か契約者（いま操作している方）だけ・版の番号の振り方。
-- (4) 提出用の控え submission_files に、どの型の版で作ったか template_id を残す（NULL＝既定の型。その依頼者の型の版だけを指せる）。写したときにも変えない。
CREATE TABLE salescall.output_templates (
  template_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id   uuid NOT NULL,
  client_id   uuid NOT NULL,
  business_id uuid,                                                   -- NULL＝依頼者の型（その依頼者の業務の初めの値）
  version_no  integer NOT NULL CHECK (version_no >= 1),               -- 持ち主ごとに1から（振るのは下の output_template_before_insert）
  body        jsonb CHECK (body IS NULL OR jsonb_typeof(body) = 'object'),   -- NULL＝型を持たない（上に従う）
  reason      text NOT NULL CHECK (btrim(reason) <> ''),
  made_by     text NOT NULL CHECK (btrim(made_by) <> ''),
  made_at     timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT output_templates_one_version UNIQUE NULLS NOT DISTINCT (client_id, business_id, version_no),
  UNIQUE (client_id, template_id),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, business_id) REFERENCES salescall.businesses (client_id, business_id)
);
CREATE TRIGGER stamp_now BEFORE INSERT ON salescall.output_templates FOR EACH ROW EXECUTE FUNCTION salescall.stamp_now('made_at');
CREATE TRIGGER output_templates_frozen BEFORE UPDATE OR DELETE ON salescall.output_templates
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
CREATE FUNCTION salescall.output_template_before_insert() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT salescall.cur_is_person() OR NEW.made_by IS DISTINCT FROM salescall.cur_actor() THEN
    RAISE EXCEPTION '出力の型を登録できるのは、管理者か契約者（いま操作している方）だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'output_template_by_person';
  END IF;
  -- 版の番号は、持ち主ごとに1から続く（同じ持ち主に同時に2つ届いても、番号が重ならないよう、持ち主ごとに順に振る）
  PERFORM pg_advisory_xact_lock(hashtextextended('output_templates:' || NEW.client_id::text || ':' || coalesce(NEW.business_id::text, ''), 0));
  SELECT coalesce(max(t.version_no), 0) + 1 INTO NEW.version_no FROM salescall.output_templates t
   WHERE t.client_id = NEW.client_id AND t.business_id IS NOT DISTINCT FROM NEW.business_id;
  RETURN NEW;
END $$;
CREATE TRIGGER output_template_before_insert BEFORE INSERT ON salescall.output_templates
  FOR EACH ROW EXECUTE FUNCTION salescall.output_template_before_insert();
CREATE INDEX output_templates_owner ON salescall.output_templates (client_id, business_id, version_no DESC);
ALTER TABLE salescall.output_templates ENABLE ROW LEVEL SECURITY;
CREATE POLICY client_scope ON salescall.output_templates
  USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
  WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client());
GRANT SELECT, INSERT ON salescall.output_templates TO salescall_app;

-- (4) 提出用の控えに、どの型の版で作ったか（NULL＝既定の型）。その依頼者の型の版だけを指せる（client_id を含めて指す）。
ALTER TABLE salescall.submission_files ADD COLUMN template_id bigint;
ALTER TABLE salescall.submission_files ADD CONSTRAINT submission_files_template
  FOREIGN KEY (client_id, template_id) REFERENCES salescall.output_templates (client_id, template_id);
-- 変えてよいのは、写したときに1回（中身を消し、いつ・どの名前で写したかを書く）だけ。どの型の版で作ったかも変えない（0054 の守りに足す）
CREATE OR REPLACE FUNCTION salescall.submission_copied_once() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' OR OLD.copied_at IS NOT NULL
     OR (NEW.submission_id, NEW.tenant_id, NEW.client_id, NEW.delivered_at, NEW.file_name, NEW.sha256, NEW.template_id)
        IS DISTINCT FROM (OLD.submission_id, OLD.tenant_id, OLD.client_id, OLD.delivered_at, OLD.file_name, OLD.sha256, OLD.template_id) THEN
    RAISE EXCEPTION '提出用のファイルの記録は、写したときに1回だけ（中身を消す）変えられます' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
