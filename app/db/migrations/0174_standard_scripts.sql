-- 0174 2026-11-04 システムの標準の想定問答（標準の版）と、業務の上書き（設計書 3-8-3 の作る順の (4)・15-2 の471。書いた人：CL-2）。
--
-- ✅ 2026-11-02 管理者 伺い2「案1でOKだが、システムの想定問答やトークスクリプトにデフォルトの想定をダッシュボードのトークスクリプト設定
--   画面に表示しておき、基本はそれを使う。上書きが必要な業務は、トークスクリプト設定画面か、エクセルなどのシートで上書きし、その分の
--   録音を実施する。」・「業務で持つのは明らかにその業務特有のトークスクリプトや問答だけになります。」
--
-- (1) 標準の版（standard_versions）：システムの持ち物（すべての契約者が読む）。作れるのは管理者だけ（システムの取引で）。変えられない
--     （直すのは新しい版）。いまの標準は、いちばん新しい版（standard_no は、データベースが付ける＝書く側が渡せない）。中身の形と確かめ、
--     業務への重ね方は業務設定の1か所（business_settings.check_standard・STANDARD_ITEMS）。
-- (2) 業務の版は、作ったときの標準の版を指す（business_versions.standard_id。版と同じく変えられない）。どの通話がどの文で話したかは、
--     業務の版から決まる（6-6 の追えること）。その版が標準から流す録音の部分は、版の中身（業務の想定問答・使わない標準の問い）と、
--     指す標準の版から、業務設定の1か所（business_settings._with_standard）が決める（表に写しを持たない＝食い違わない）。
--     前の版は標準を指さない（standard_id が無い＝標準を使わない＝今までどおり）。
-- (3) 標準の録音（単位 standard）：その標準の版の、その声の録音の組。システムの持ち物（管理者が作って承認する＝0134・0171 の守りのまま）。
--     組は標準の版を指す（standard_id）。流す組・話してよい声は、業務の版が指す標準の版の組を読む（recordings の1か所）。

CREATE TABLE salescall.standard_versions (
  standard_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  standard_no bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
  content     jsonb NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  created_by  text NOT NULL DEFAULT salescall.cur_actor() CHECK (btrim(created_by) <> '')
);
ALTER TABLE salescall.standard_versions ENABLE ROW LEVEL SECURITY;
CREATE POLICY standard_read ON salescall.standard_versions FOR SELECT USING (true);
CREATE POLICY standard_write ON salescall.standard_versions FOR INSERT WITH CHECK (true);   -- 誰が作れるかは standard_by_admin の1か所
GRANT SELECT, INSERT ON salescall.standard_versions TO salescall_app;
CREATE TRIGGER standard_versions_frozen BEFORE UPDATE OR DELETE ON salescall.standard_versions
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
CREATE TRIGGER stamp_now BEFORE INSERT ON salescall.standard_versions
  FOR EACH ROW EXECUTE FUNCTION salescall.stamp_now('created_at');
CREATE FUNCTION salescall.standard_by_admin() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF salescall.cur_actor_kind() IS DISTINCT FROM 'admin' OR salescall.cur_tenant() IS NOT NULL THEN
    RAISE EXCEPTION '標準の想定問答の版を作れるのは、管理者だけです（システムの取引で）'
      USING ERRCODE = 'insufficient_privilege', CONSTRAINT = 'standard_by_admin';
  END IF;
  NEW.created_by := salescall.cur_actor();
  RETURN NEW;
END $$;
CREATE TRIGGER standard_by_admin BEFORE INSERT ON salescall.standard_versions
  FOR EACH ROW EXECUTE FUNCTION salescall.standard_by_admin();

-- (2) 業務の版が指す標準の版（版と同じく変えられない＝business_versions_frozen のまま）
ALTER TABLE salescall.business_versions ADD COLUMN standard_id uuid REFERENCES salescall.standard_versions (standard_id);

-- (3) 標準の録音の組（システムの持ち物・標準の版を指す）
ALTER TABLE salescall.recording_sets ADD COLUMN standard_id uuid REFERENCES salescall.standard_versions (standard_id);
ALTER TABLE salescall.recording_sets DROP CONSTRAINT recording_sets_unit;
ALTER TABLE salescall.recording_sets ADD CONSTRAINT recording_sets_unit
  CHECK (unit IN ('contractor', 'business', 'apology', 'name_role', 'name', 'name_tail', 'standard'));
ALTER TABLE salescall.recording_sets ADD CONSTRAINT recording_sets_standard
  CHECK ((unit = 'standard') = (standard_id IS NOT NULL)
         AND (unit <> 'standard' OR (tenant_id IS NULL AND client_id IS NULL AND version_id IS NULL AND order_id IS NULL
                                     AND business_id IS NULL)));
ALTER TABLE salescall.recording_sets DROP CONSTRAINT recording_sets_system_units;
ALTER TABLE salescall.recording_sets ADD CONSTRAINT recording_sets_system_units
  CHECK (tenant_id IS NOT NULL OR unit IN ('contractor', 'name_role', 'name', 'name_tail', 'standard'));

-- 流す組（0172 を作り直す）：標準の録音も、標準の版・声ごとに、そろった組のうちいちばん新しく作った組
CREATE OR REPLACE VIEW salescall.current_recording_sets WITH (security_invoker = true) AS
  SELECT c.set_id, g.tenant_id, c.client_id, g.unit, g.voice_name, g.version_id, g.standard_id
  FROM (SELECT DISTINCT s.tenant_id, s.unit, s.voice_name, s.version_id, s.standard_id FROM salescall.recording_sets s
        WHERE s.unit IN ('contractor', 'business', 'standard')) g
  CROSS JOIN LATERAL (
    SELECT s.set_id, s.client_id FROM salescall.recording_sets s
    WHERE s.voice_name = g.voice_name AND s.unit = g.unit
      AND s.tenant_id IS NOT DISTINCT FROM g.tenant_id AND s.version_id IS NOT DISTINCT FROM g.version_id
      AND s.standard_id IS NOT DISTINCT FROM g.standard_id
      AND salescall.set_ready(s.set_id)
    ORDER BY s.created_at DESC, s.set_id DESC LIMIT 1) c;

-- 標準の録音の部分は、その標準の版の想定問答の答えだけ（名前＝answer_＋問いの名前・文＝答えの文。業務設定の1か所
-- business_settings.ANSWER_PART_PREFIX・_answers の形と同じ）。⛔ 守りが無いと、版の答えと違う文の録音が、その答えとして流れる
CREATE FUNCTION salescall.standard_recording_check() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM salescall.recording_sets s WHERE s.set_id = NEW.set_id AND s.unit = 'standard')
     AND NOT EXISTS (SELECT 1 FROM salescall.recording_sets s JOIN salescall.standard_versions v USING (standard_id),
                            jsonb_array_elements(v.content -> 'answers') a
                     WHERE s.set_id = NEW.set_id AND 'answer_' || (a ->> 'name') = NEW.part AND a ->> 'text' = NEW.spoken_text) THEN
    RAISE EXCEPTION '標準の録音「%」は、その標準の版の想定問答の答え（名前と文）と合いません', NEW.part
      USING ERRCODE = 'check_violation', CONSTRAINT = 'standard_recording_matches';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER standard_recording_check BEFORE INSERT OR UPDATE ON salescall.recordings
  FOR EACH ROW EXECUTE FUNCTION salescall.standard_recording_check();
