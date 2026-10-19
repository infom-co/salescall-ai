-- 0099 録音で選んだ名前の読み（Intent.md 6-2・6-8。CL-1 の連絡帳 4-16 の続き。CL-2 の連絡帳 4-66 で約束して作っていなかった＝
-- 2026-10-19 の計画書の読み直しで見つけた書き漏れ。DESIGN.md 3-7・15-2 の208。書いた人：CL-2）
--
-- Intent.md 6-2：「読みが決まらないこちら側の名前が入る文は、読みの候補ごとに録音を作り、契約者が聞いて選んだ録音の読みを、その名前の
-- 読みにする（同じ名前を別の録音で別の読みに選んだら、業務設定を登録せず契約者に知らせる）」。
-- (1) 録音の部分ごとに、その録音で選んだ名前の読み（recording_readings）。依頼者の単位（業務設定の版）の組だけ。
--     同じ組の中で、同じ名前に別の読みは入れられない。承認した組には足せない（部分と同じ）。
-- (2) いま使う録音の組（current_recording_sets）：単位・声・版ごとに、いちばん新しく承認した組（1か所。流すときも、読みも、これ）。
-- (3) 承認のとき、同じ版のほかの声のいま使う組と、同じ名前の読みが違えば、承認しない（＝その声では話さない。門は承認した録音の
--     ある声だけで話す）。止めるのは承認だけで、発信中の業務は止めない。
-- (4) その版の名前の、録音で選んだ読み（version_recording_readings）。業務設定を読む1か所（business_settings.load）が使う。

CREATE VIEW salescall.current_recording_sets WITH (security_invoker = true) AS
  SELECT DISTINCT ON (s.tenant_id, s.unit, s.voice_name, s.version_id)
         s.set_id, s.tenant_id, s.client_id, s.unit, s.voice_name, s.version_id
  FROM salescall.recording_sets s JOIN salescall.recording_approvals p USING (set_id)
  ORDER BY s.tenant_id, s.unit, s.voice_name, s.version_id, p.approved_at DESC, s.created_at DESC;
GRANT SELECT ON salescall.current_recording_sets TO salescall_app;

CREATE TABLE salescall.recording_readings (
  tenant_id uuid NOT NULL,
  client_id uuid NOT NULL,
  set_id    uuid NOT NULL,
  part      text NOT NULL,
  name_text text NOT NULL CHECK (btrim(name_text) <> ''),
  reading   text NOT NULL CHECK (reading ~ '^[ぁ-ゖー・]+$'),   -- ひらがな（読みの形は core/readings.py の normalize と同じ）
  PRIMARY KEY (set_id, part, name_text),
  FOREIGN KEY (set_id, part) REFERENCES salescall.recordings (set_id, part),
  FOREIGN KEY (tenant_id, set_id) REFERENCES salescall.recording_sets (tenant_id, set_id)
);

CREATE FUNCTION salescall.recording_reading_before_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE s record;
BEGIN
  SELECT unit, client_id INTO s FROM salescall.recording_sets WHERE set_id = NEW.set_id;
  IF s.unit IS DISTINCT FROM 'business' THEN
    RAISE EXCEPTION '録音で選んだ読みは、依頼者の単位（業務設定の版）の録音だけに付けます' USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.client_id IS DISTINCT FROM s.client_id THEN
    RAISE EXCEPTION '録音で選んだ読みの依頼者は、組と同じです' USING ERRCODE = 'check_violation';
  END IF;
  IF EXISTS (SELECT 1 FROM salescall.recording_approvals WHERE set_id = NEW.set_id) THEN
    RAISE EXCEPTION '承認した録音の組には、読みを足せません（作り直すときは新しい組）' USING ERRCODE = 'check_violation';
  END IF;
  IF EXISTS (SELECT 1 FROM salescall.recording_readings r WHERE r.set_id = NEW.set_id AND r.name_text = NEW.name_text
             AND r.reading <> NEW.reading) THEN
    RAISE EXCEPTION '名前「%」を、同じ組の別の録音で別の読みに選んでいます（どちらかに選び直してください）', NEW.name_text
      USING ERRCODE = 'check_violation', CONSTRAINT = 'recording_reading_one_per_name';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER recording_reading_before_insert BEFORE INSERT ON salescall.recording_readings
  FOR EACH ROW EXECUTE FUNCTION salescall.recording_reading_before_insert();
CREATE TRIGGER recording_readings_frozen BEFORE UPDATE OR DELETE ON salescall.recording_readings
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();

ALTER TABLE salescall.recording_readings ENABLE ROW LEVEL SECURITY;
CREATE POLICY recording_scope ON salescall.recording_readings
  USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
  WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client());
GRANT SELECT, INSERT ON salescall.recording_readings TO salescall_app;

-- 承認のとき：同じ版のほかの声の、いま使う組と、同じ名前の読みが違えば、承認しない（同じ版の承認は1つずつ確かめる）
CREATE FUNCTION salescall.recording_approval_readings() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE s record; clash record;
BEGIN
  SELECT unit, voice_name, version_id INTO s FROM salescall.recording_sets WHERE set_id = NEW.set_id;
  IF s.unit IS DISTINCT FROM 'business' THEN
    RETURN NEW;
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended('recording_readings:' || s.version_id::text, 0));
  SELECT mine.name_text, mine.reading AS mine, other.reading AS other INTO clash
  FROM salescall.recording_readings mine
  JOIN salescall.current_recording_sets c ON c.unit = 'business' AND c.version_id = s.version_id AND c.voice_name <> s.voice_name
  JOIN salescall.recording_readings other ON other.set_id = c.set_id AND other.name_text = mine.name_text
  WHERE mine.set_id = NEW.set_id AND other.reading <> mine.reading
  LIMIT 1;
  IF FOUND THEN
    RAISE EXCEPTION '名前「%」を、この録音では「%」、ほかの声の録音では「%」に選んでいます。同じ名前を別の読みに選んだ録音は承認しません'
                    '（どちらかに選び直してください。Intent.md 6-2）', clash.name_text, clash.mine, clash.other
      USING ERRCODE = 'check_violation', CONSTRAINT = 'recording_reading_same_across_voices';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER recording_approval_readings BEFORE INSERT ON salescall.recording_approvals
  FOR EACH ROW EXECUTE FUNCTION salescall.recording_approval_readings();

-- その版の名前の、録音で選んだ読み（いま使う組のもの。承認の確かめで、どの声でも同じ読み）
CREATE VIEW salescall.version_recording_readings WITH (security_invoker = true) AS
  SELECT DISTINCT c.version_id, r.name_text, r.reading
  FROM salescall.current_recording_sets c JOIN salescall.recording_readings r ON r.set_id = c.set_id
  WHERE c.unit = 'business';
GRANT SELECT ON salescall.version_recording_readings TO salescall_app;
