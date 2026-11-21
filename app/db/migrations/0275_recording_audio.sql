-- 0275 業務で流す録音の音の置き場（2026-11-21。書いた人：CL-2。DESIGN_15-2.md の935。設計書 3-8・CL-1 の連絡帳 4-222）
--
-- ✅ 2026-11-21 日本時間1時台 管理者「ア　データベースの中に置く」：業務で流す録音の音（名乗り・業務の録音・名前の録音・お詫び。承認して電話で流す音）は、
-- データベースの中に置く。音を置く・読む・指紋を確かめる・権限を確かめる・誰がいつ聞いたかを記録する、は core/recording_audio の1か所。
-- - 音（recording_audio）：持ち主の範囲（契約者・依頼者。どちらも NULL＝システムの録音）と指紋ごとに1つ。指紋は中身から作った値と同じであること（データベースが守る＝
--   置いた音と指紋が食い違わない）。読める範囲は録音の記録と同じ（その契約者・依頼者の取引から。システムの録音はどの契約者の取引からも読める・書くのは管理者の
--   システムの取引だけ）。書き換えない・消さない（その版が使われている間は消さない決まり。消す決まりは、使われなくなった組を片付けるときに足す）。
-- - 聞いた記録（recording_listens）：承認・審査の画面で、だれが・いつ・どの組のどの部分を聞いたか。書き換えない。
CREATE TABLE salescall.recording_audio (
  seq           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id     uuid DEFAULT salescall.cur_tenant(),
  client_id     uuid DEFAULT salescall.cur_client(),
  audio_sha256  text NOT NULL CHECK (audio_sha256 ~ '^[0-9a-f]{64}$'),
  audio         bytea NOT NULL CHECK (octet_length(audio) BETWEEN 44 AND 10485760),   -- WAV（頭の 44 バイトから・10MB まで）
  stored_by     text NOT NULL DEFAULT salescall.cur_actor(),
  stored_at     timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT recording_audio_fingerprint CHECK (audio_sha256 = encode(sha256(audio), 'hex')),
  CONSTRAINT recording_audio_client_needs_tenant CHECK (client_id IS NULL OR tenant_id IS NOT NULL),
  FOREIGN KEY (tenant_id) REFERENCES salescall.tenants
);
CREATE UNIQUE INDEX recording_audio_one ON salescall.recording_audio (tenant_id, client_id, audio_sha256) NULLS NOT DISTINCT;
CREATE TRIGGER stamp_now BEFORE INSERT ON salescall.recording_audio FOR EACH ROW EXECUTE FUNCTION salescall.stamp_now('stored_at');
CREATE TRIGGER recording_audio_frozen BEFORE UPDATE OR DELETE ON salescall.recording_audio FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
ALTER TABLE salescall.recording_audio ENABLE ROW LEVEL SECURITY;
CREATE POLICY recording_scope ON salescall.recording_audio
  USING (tenant_id = salescall.cur_tenant() AND (client_id IS NULL OR client_id = salescall.cur_client()))
  WITH CHECK (tenant_id = salescall.cur_tenant() AND (client_id IS NULL OR client_id = salescall.cur_client()));
CREATE POLICY system_audio_read ON salescall.recording_audio FOR SELECT USING (tenant_id IS NULL);
CREATE POLICY system_audio_write ON salescall.recording_audio FOR INSERT
  WITH CHECK (tenant_id IS NULL AND client_id IS NULL AND salescall.cur_tenant() IS NULL);
GRANT SELECT, INSERT ON salescall.recording_audio TO salescall_app;

CREATE TABLE salescall.recording_listens (
  seq        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id  uuid DEFAULT salescall.cur_tenant(),
  client_id  uuid DEFAULT salescall.cur_client(),
  set_id     uuid NOT NULL,
  part       text NOT NULL CHECK (part ~ '^[A-Za-z0-9_]{1,64}$'),
  listened_by text NOT NULL DEFAULT salescall.cur_actor(),
  listened_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (set_id, part) REFERENCES salescall.recordings (set_id, part)
);
CREATE TRIGGER stamp_now BEFORE INSERT ON salescall.recording_listens FOR EACH ROW EXECUTE FUNCTION salescall.stamp_now('listened_at');
CREATE TRIGGER recording_listens_frozen BEFORE UPDATE OR DELETE ON salescall.recording_listens FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
ALTER TABLE salescall.recording_listens ENABLE ROW LEVEL SECURITY;
CREATE POLICY listen_scope ON salescall.recording_listens
  USING (tenant_id IS NOT DISTINCT FROM salescall.cur_tenant() AND (client_id IS NULL OR client_id = salescall.cur_client()))
  WITH CHECK (tenant_id IS NOT DISTINCT FROM salescall.cur_tenant() AND (client_id IS NULL OR client_id = salescall.cur_client()));
GRANT SELECT, INSERT ON salescall.recording_listens TO salescall_app;
