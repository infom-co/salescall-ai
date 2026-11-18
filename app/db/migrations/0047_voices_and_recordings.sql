-- 0047 声の一覧と、録音の版と承認の記録（定型の音声の台帳。DESIGN.md 6-13・5-2-2 の順10。2026-10-17。書いた人：CL-2）
--
-- ユーザー（2026-10-17）：「声は録音方式で進んでいて、A/Bテストより、事前の録音チェックになる方向性です。また、APIのモデルが
-- 進化したら、新しい声が増えるかもしれません」「（声を決める場所は）案Aでいい」＝門（CL-2）が決める。
-- ⇒ (1) 声の一覧（voices）：声の名前（Sadachbia・Leda）を表に持つ。新しい声は行を足すだけ（処理を書き換えない）。会話AIのモデルが
--        替わって使えなくなった声は「使わない」の印（retired_at。1回だけ）を付ける（消さない：前の通話の記録が指している）。
--        声の型（voice_types：声の名前＋話し方。中身は CL-1 の持ち物）は、この一覧の声だけを指せる。
--    (2) 録音の版の記録（recording_sets・recordings）：録音は2つの単位（2026-10-15 ユーザー）。
--        - 契約者の単位（unit = contractor）：契約者と声の名前ごと。相づち・締め・待ってもらう一言など、どの依頼者にも同じもの。
--        - 依頼者の単位（unit = business）：業務設定の版と声の名前ごと。トークスクリプト（名乗りの部分ごと・質問・聞き返し）。
--        録音は声の名前ごとに1組（声の型ごとではない＝2026-10-15 ユーザー「S4とS2を分ける必要はありません」）。
--        1つの組は変えられない（作り直しは新しい組）。部分（part）は流す単位（名乗りは部分ごと＝CL-1 の連絡帳 4-18）。
--        部分の一覧（どの部分が要るか）は、会話の側（CL-1）の1か所が決める（phrases.RECORDED・replay_parts）。
--    (3) 録音の承認（recording_approvals）：組ごとに1回。承認するのは管理者か契約者（人が聞いて選ぶ＝事前の録音チェック。
--        DESIGN.md 6-13 の 3.）。承認した組には、部分を足せない。
--    門は、承認した録音が「契約者の単位」と「その版の依頼者の単位」の両方にある声だけで話す（core/recordings.py の1か所）。
--    音声のファイルそのものの置き場は、まだ決まっていない（CL-3 の 4-1d）。ここに持つのは、置き場の参照と、中身の指紋（sha256）。

-- ═════════ (1) 声の一覧 ═════════
CREATE TABLE salescall.voices (
  voice_name     text PRIMARY KEY CHECK (voice_name ~ '^[A-Za-z][A-Za-z0-9_-]{0,31}$'),
  note           text NOT NULL CHECK (btrim(note) <> ''),
  added_at       timestamptz NOT NULL DEFAULT now(),
  retired_at     timestamptz,           -- 使わない（会話AIのモデルが替わって無くなった など）。1回だけ付ける
  retired_reason text,
  CHECK ((retired_at IS NULL) = (retired_reason IS NULL)),
  CHECK (retired_reason IS NULL OR btrim(retired_reason) <> '')
);
INSERT INTO salescall.voices (voice_name, note) VALUES
('Sadachbia', '男性の声（2026-10-15 ユーザーが男性の声をすべて試して選んだ。gemini-3.8-live）'),
('Leda',      '女性の声（ほかの依頼者が男性の声でかけたあとに使う＝2026-10-17 ユーザー。gemini-3.8-live）');
ALTER TABLE salescall.voice_types ADD CONSTRAINT voice_types_voice FOREIGN KEY (voice_name) REFERENCES salescall.voices (voice_name);

-- 声を足す・使わない印を付けるのは管理者だけ（すべての契約者に共通の一覧）。変えてよいのは、使わない印を1回付けることだけ
CREATE FUNCTION salescall.voices_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF current_user <> 'salescall_owner' AND salescall.cur_actor_kind() IS DISTINCT FROM 'admin' THEN
    RAISE EXCEPTION '声の一覧を変えられるのは管理者だけです' USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION '声は消せません（前の通話の記録が指しています。使わないときは印を付ける）' USING ERRCODE = 'check_violation';
  END IF;
  IF TG_OP = 'UPDATE' AND ((NEW.voice_name, NEW.note, NEW.added_at) IS DISTINCT FROM (OLD.voice_name, OLD.note, OLD.added_at)
                           OR OLD.retired_at IS NOT NULL) THEN
    RAISE EXCEPTION '声の一覧で変えてよいのは、使わない印を1回付けることだけです' USING ERRCODE = 'check_violation';
  END IF;
  RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END $$;
CREATE TRIGGER voices_guard BEFORE INSERT OR UPDATE OR DELETE ON salescall.voices
  FOR EACH ROW EXECUTE FUNCTION salescall.voices_guard();
GRANT SELECT, INSERT, UPDATE ON salescall.voices TO salescall_app;

-- ═════════ (2) 録音の版の記録 ═════════
CREATE TABLE salescall.recording_sets (
  set_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id  uuid NOT NULL REFERENCES salescall.tenants (tenant_id),
  unit       text NOT NULL CHECK (unit IN ('contractor', 'business')),
  voice_name text NOT NULL REFERENCES salescall.voices (voice_name),
  client_id  uuid,
  version_id uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  created_by text NOT NULL DEFAULT salescall.cur_actor() CHECK (btrim(created_by) <> ''),
  CHECK (CASE unit WHEN 'business' THEN client_id IS NOT NULL AND version_id IS NOT NULL
                   ELSE client_id IS NULL AND version_id IS NULL END),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, version_id) REFERENCES salescall.business_versions (client_id, version_id),
  UNIQUE (tenant_id, set_id)
);
CREATE INDEX recording_sets_by_voice ON salescall.recording_sets (tenant_id, unit, voice_name, version_id);

CREATE TABLE salescall.recordings (
  tenant_id    uuid NOT NULL,
  client_id    uuid,                   -- 組と同じ（見える範囲を組と同じにするため）
  set_id       uuid NOT NULL,
  part         text NOT NULL CHECK (part ~ '^[A-Za-z0-9_]{1,64}$'),   -- 流す単位の名前（会話の側の名前。例 greeting・closing・Q2）
  spoken_text  text NOT NULL CHECK (btrim(spoken_text) <> ''),         -- 録音の文（読みはひらがなで「」にくくった形）
  audio_ref    text NOT NULL CHECK (btrim(audio_ref) <> ''),           -- 音声のファイルの置き場の参照
  audio_sha256 text NOT NULL CHECK (audio_sha256 ~ '^[0-9a-f]{64}$'),  -- 中身の指紋（置き場の中身が替わっていないか）
  PRIMARY KEY (set_id, part),
  FOREIGN KEY (tenant_id, set_id) REFERENCES salescall.recording_sets (tenant_id, set_id)
);

CREATE TABLE salescall.recording_approvals (
  tenant_id   uuid NOT NULL,
  client_id   uuid,
  set_id      uuid PRIMARY KEY,
  approved_at timestamptz NOT NULL DEFAULT now(),
  approved_by text NOT NULL CHECK (btrim(approved_by) <> ''),
  FOREIGN KEY (tenant_id, set_id) REFERENCES salescall.recording_sets (tenant_id, set_id)
);

-- 組：使わない声では作らない
CREATE FUNCTION salescall.recording_set_before_insert() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM salescall.voices WHERE voice_name = NEW.voice_name AND retired_at IS NOT NULL) THEN
    RAISE EXCEPTION '使わない声（%）の録音は作れません', NEW.voice_name USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER recording_set_before_insert BEFORE INSERT ON salescall.recording_sets
  FOR EACH ROW EXECUTE FUNCTION salescall.recording_set_before_insert();

-- 部分：承認した組には足せない。見える範囲（依頼者）は組と同じ
CREATE FUNCTION salescall.recording_before_insert() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM salescall.recording_approvals WHERE set_id = NEW.set_id) THEN
    RAISE EXCEPTION '承認した録音の組には、部分を足せません（作り直すときは新しい組）' USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.client_id IS DISTINCT FROM (SELECT client_id FROM salescall.recording_sets WHERE set_id = NEW.set_id) THEN
    RAISE EXCEPTION '録音の部分の依頼者は、組と同じです' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER recording_before_insert BEFORE INSERT ON salescall.recordings
  FOR EACH ROW EXECUTE FUNCTION salescall.recording_before_insert();

-- 承認：管理者か契約者が、自分の取引で（人が聞いて選ぶ）。部分の無い組・使わない声の組は承認できない
CREATE FUNCTION salescall.recording_approval_by_person() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF coalesce(salescall.cur_actor_kind()::text, '') NOT IN ('operator', 'admin')
     OR NEW.approved_by IS DISTINCT FROM salescall.cur_actor() THEN
    RAISE EXCEPTION '録音を承認できるのは、管理者か契約者だけです（承認した者は、取引の管理者か契約者）'
      USING ERRCODE = 'insufficient_privilege', CONSTRAINT = 'recording_approval_by_person';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM salescall.recordings WHERE set_id = NEW.set_id) THEN
    RAISE EXCEPTION '部分の無い録音の組は承認できません' USING ERRCODE = 'check_violation';
  END IF;
  IF EXISTS (SELECT 1 FROM salescall.recording_sets s JOIN salescall.voices v USING (voice_name)
             WHERE s.set_id = NEW.set_id AND v.retired_at IS NOT NULL) THEN
    RAISE EXCEPTION '使わない声の録音は承認できません' USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.client_id IS DISTINCT FROM (SELECT client_id FROM salescall.recording_sets WHERE set_id = NEW.set_id) THEN
    RAISE EXCEPTION '承認の依頼者は、組と同じです' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER recording_approval_by_person BEFORE INSERT ON salescall.recording_approvals
  FOR EACH ROW EXECUTE FUNCTION salescall.recording_approval_by_person();

CREATE TRIGGER recording_sets_frozen BEFORE UPDATE OR DELETE ON salescall.recording_sets
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
CREATE TRIGGER recordings_frozen BEFORE UPDATE OR DELETE ON salescall.recordings
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
CREATE TRIGGER recording_approvals_frozen BEFORE UPDATE OR DELETE ON salescall.recording_approvals
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();

-- 見える範囲：契約者の単位は、その契約者の取引（依頼者の取引からも読める）。依頼者の単位は、その依頼者の取引だけ
ALTER TABLE salescall.recording_sets ENABLE ROW LEVEL SECURITY;
ALTER TABLE salescall.recordings ENABLE ROW LEVEL SECURITY;
ALTER TABLE salescall.recording_approvals ENABLE ROW LEVEL SECURITY;
CREATE POLICY recording_scope ON salescall.recording_sets
  USING (tenant_id = salescall.cur_tenant() AND (client_id IS NULL OR client_id = salescall.cur_client()))
  WITH CHECK (tenant_id = salescall.cur_tenant() AND (client_id IS NULL OR client_id = salescall.cur_client()));
CREATE POLICY recording_scope ON salescall.recordings
  USING (tenant_id = salescall.cur_tenant() AND (client_id IS NULL OR client_id = salescall.cur_client()))
  WITH CHECK (tenant_id = salescall.cur_tenant() AND (client_id IS NULL OR client_id = salescall.cur_client()));
CREATE POLICY recording_scope ON salescall.recording_approvals
  USING (tenant_id = salescall.cur_tenant() AND (client_id IS NULL OR client_id = salescall.cur_client()))
  WITH CHECK (tenant_id = salescall.cur_tenant() AND (client_id IS NULL OR client_id = salescall.cur_client()));
GRANT SELECT, INSERT ON salescall.recording_sets, salescall.recordings, salescall.recording_approvals TO salescall_app;

-- ═════════ (4) 声の型の説明から A/B を外す（A/B テストはしない＝2026-10-17 ユーザー：「事前の録音チェックになる方向性」） ═════════
UPDATE salescall.voice_types SET note = v.note FROM (VALUES
  ('S2', '前向き＋会社員（Intent.md 9-8。Sadachbia の初めの声の型）'),
  ('S4', 'にこやかな受付（Intent.md 9-8）'),
  ('L4', '謙虚な会社員（Intent.md 9-8。Leda の初めの声の型）')
) AS v(code, note) WHERE salescall.voice_types.code = v.code;
