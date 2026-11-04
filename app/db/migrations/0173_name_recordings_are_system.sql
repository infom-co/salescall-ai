-- 0173 2026-11-04 名前の録音は、契約者が作った段階ではその業務の録音（業務の録音の設定と同じ扱い）。管理者が聞いて認めたものだけを、
-- システムの共通の録音に入れる（設計書 3-8-3 の作る順の (3)・15-2 の470。書いた人：CL-2）。
--
-- ✅ 2026-11-02 管理者 伺い1「案1」：代表的な名前に無い名前も、録音したらシステムの録音に入れ、すべての契約者が使う。
-- ✅ 2026-11-04 管理者：「契約者が作ったシステムの録音は、管理者がすべてを聞き流した上で、システムに残すに値しないと判断したものは、
--   登録から除外できるようにしてください。契約者が作ったものをそのままシステムに移行させるのではなく、管理者がチェックしないと
--   システムに入れられないということです。」・伺い「審査待ちのあいだ」＝「その契約者だけ使う」・伺い「除外のあと」＝「その契約者は
--   使い続ける」・「契約者が作った段階では、業務ごとの録音設定と同じ扱いです。管理者が認めて初めてシステムの共通録音に登録されます。」・
--   「このシステムに依頼者は関係ありません。このシステムに関与するのは管理者と契約者のみです。その下に依頼者というタブはありますが、
--   依頼者がそれに関わるわけではなく、契約者が依頼者ごとにタブを作って管理しやすくしているだけです。さらに依頼者の下に業務が紐づく
--   という形です。」
-- ⛔ 前の形（0134）：代表的な名前はシステム、それに無い名前は契約者ごと（その契約者のどの業務でも使う）。⛔ 11-04 朝の CL-2 の下書き
--   （契約者が作ったら、その契約者か管理者の承認でそのままシステムの録音にする）は、上のお言葉で取り下げた。
--
-- (1) 契約者が作る名前の録音は、その業務の録音（業務の録音と同じく、契約者が作って聞いて承認する。組に業務を持つ＝business_id。守りは
--     下の recording_sets_name_owner）。承認すれば、その業務の電話で使う。管理者が除外しても、その業務では使い続ける。承認は中身に付く
--     （0172。同じ依頼者のタブ・同じ声の同じ中身なら承認済み＝業務の録音と同じ）。
-- (2) 電話で流す録音は1か所（関数 current_name_set）：その業務の録音を先に（契約者が聞いて選んだもの）、無ければシステムの共通の録音。
--     同じ持ち主の中では、いちばん新しく作ったそろった組。⛔ 前の見方 current_name_recordings は業務を見られないので外した。
-- (3) システムの共通の録音に入れるかは、管理者が聞いて決める（1か所＝review_name_recording）。決められるのは管理者だけ（システムの
--     取引で）。入れると決めたら、データベースの仕掛けが、その録音の部分（文・音声の置き場・指紋）をそのまま写したシステムの組を作り、
--     管理者の承認を付ける（システムの録音は、どれも管理者が作って承認したもの＝0134・0171 の守りのまま）。すべての契約者が読む。
-- (4) 管理者が聞くのは、契約者が承認して、その業務の電話で使えるようになった組だけ（まだ承認していない組は、契約者が録り直すかも
--     しれない）。まだ決めていない組は name_recordings_to_review の1か所。
-- (5) 決めた記録（入れた・除外した・だれが・いつ・入れたシステムの組）は name_recording_reviews に1回だけ残す（変えられない）。
--     契約者は自分の分を読める（除外されたか分かる）。ほかの契約者は読めない。
-- (6) システムの組の側には、どの業務の組から写したかを書かない（ほかの契約者に、どの業務がどの名前のお客様に電話しているかを見せない）。
-- (7) 前に作った契約者の行の名前の録音（業務の無い組＝0134 の形）は、もう読まない・新しく作れない（下の守りは NOT VALID）。消さない
--     （変えられない表）。本番のデータベースはまだ無い（15-2 の311 と同じ）。

ALTER TABLE salescall.recording_sets ADD COLUMN business_id uuid;
ALTER TABLE salescall.recording_sets ADD CONSTRAINT recording_sets_business
  FOREIGN KEY (client_id, business_id) REFERENCES salescall.businesses (client_id, business_id);
-- 名前の録音の持ち主は、システム（契約者も業務も無い）か業務（契約者・依頼者のタブ・業務がある）のどちらか（1か所。新しい行だけ確かめる）。
-- 業務を持つのは名前の録音の組だけ
ALTER TABLE salescall.recording_sets DROP CONSTRAINT recording_sets_owner;
ALTER TABLE salescall.recording_sets ADD CONSTRAINT recording_sets_owner CHECK (CASE unit
    WHEN 'business' THEN client_id IS NOT NULL AND version_id IS NOT NULL AND order_id IS NULL AND business_id IS NULL
    WHEN 'apology' THEN client_id IS NOT NULL AND version_id IS NULL AND order_id IS NOT NULL AND business_id IS NULL
    WHEN 'contractor' THEN client_id IS NULL AND version_id IS NULL AND order_id IS NULL AND business_id IS NULL
    ELSE version_id IS NULL AND order_id IS NULL END);
ALTER TABLE salescall.recording_sets ADD CONSTRAINT recording_sets_name_owner
  CHECK (unit NOT IN ('name_role', 'name', 'name_tail')
         OR (tenant_id IS NULL AND client_id IS NULL AND business_id IS NULL)
         OR (tenant_id IS NOT NULL AND client_id IS NOT NULL AND business_id IS NOT NULL)) NOT VALID;

-- (2) 電話で流す録音（1か所）
DROP VIEW salescall.current_name_recordings;
CREATE FUNCTION salescall.current_name_set(p_business uuid, p_voice text, p_unit text, p_role text, p_reading text, p_tail text)
RETURNS uuid LANGUAGE sql STABLE AS $$
  SELECT s.set_id FROM salescall.recording_sets s
  WHERE s.unit = p_unit AND s.unit IN ('name_role', 'name', 'name_tail') AND s.voice_name = p_voice
    AND s.name_role IS NOT DISTINCT FROM p_role AND s.name_reading IS NOT DISTINCT FROM p_reading AND s.tail IS NOT DISTINCT FROM p_tail
    AND (s.tenant_id IS NULL OR s.business_id = p_business)
    AND salescall.set_ready(s.set_id)
  ORDER BY (s.tenant_id IS NULL), s.created_at DESC, s.set_id DESC
  LIMIT 1
$$;
GRANT EXECUTE ON FUNCTION salescall.current_name_set(uuid, text, text, text, text, text) TO salescall_app;

CREATE TABLE salescall.name_recording_reviews (
  set_id        uuid PRIMARY KEY REFERENCES salescall.recording_sets (set_id),   -- 契約者が業務の録音として作った名前の録音の組
  tenant_id     uuid NOT NULL,                                                     -- その組の契約者と依頼者のタブ（契約者が自分の分を読むため）
  client_id     uuid NOT NULL,
  decision      text NOT NULL CHECK (decision IN ('admitted', 'excluded')),       -- システムに入れた／除外した
  system_set_id uuid REFERENCES salescall.recording_sets (set_id),               -- 入れたときに作ったシステムの組
  decided_by    text NOT NULL CHECK (btrim(decided_by) <> ''),
  decided_at    timestamptz NOT NULL DEFAULT now(),
  CHECK ((decision = 'admitted') = (system_set_id IS NOT NULL)),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id)
);
ALTER TABLE salescall.name_recording_reviews ENABLE ROW LEVEL SECURITY;
CREATE POLICY review_scope ON salescall.name_recording_reviews FOR SELECT
  USING ((tenant_id = salescall.cur_tenant() AND (salescall.cur_client() IS NULL OR client_id = salescall.cur_client()))
         OR (salescall.cur_tenant() IS NULL AND salescall.cur_actor_kind() IS NOT DISTINCT FROM 'admin'));
GRANT SELECT ON salescall.name_recording_reviews TO salescall_app;
CREATE TRIGGER name_recording_reviews_frozen BEFORE UPDATE OR DELETE ON salescall.name_recording_reviews
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();

-- まだ決めていない組（1か所）：契約者が作り、その契約者の電話で使えるようになった（set_ready）名前の録音の組で、決めた記録の無いもの。
-- 管理者だけが読める（ほかの契約者の録音を読むので、持ち主の権限で読む。どの契約者・どの業務のものかは返さない）。
CREATE FUNCTION salescall.name_recordings_to_review()
RETURNS TABLE (set_id uuid, unit text, voice_name text, name_role text, name_reading text, tail text,
               part text, spoken_text text, audio_ref text, audio_sha256 text, created_at timestamptz)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
BEGIN
  IF salescall.cur_actor_kind() IS DISTINCT FROM 'admin' OR salescall.cur_tenant() IS NOT NULL THEN
    RAISE EXCEPTION '名前の録音をシステムに入れるかを決めるのは、管理者だけです（システムの取引で）'
      USING ERRCODE = 'insufficient_privilege', CONSTRAINT = 'name_review_by_admin';
  END IF;
  RETURN QUERY
    SELECT s.set_id, s.unit, s.voice_name, s.name_role, s.name_reading, s.tail,
           r.part, r.spoken_text, r.audio_ref, r.audio_sha256, s.created_at
    FROM salescall.recording_sets s JOIN salescall.recordings r ON r.set_id = s.set_id
    WHERE s.unit IN ('name_role', 'name', 'name_tail') AND s.business_id IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM salescall.name_recording_reviews v WHERE v.set_id = s.set_id)
      AND salescall.set_ready(s.set_id)
    ORDER BY s.created_at, s.set_id;
END $$;
REVOKE ALL ON FUNCTION salescall.name_recordings_to_review() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.name_recordings_to_review() TO salescall_app;

-- 決める（1か所）：入れる＝その組の部分をそのまま写したシステムの組を作り、管理者が承認する／除外する。どちらも1回だけ。
CREATE FUNCTION salescall.review_name_recording(p_set uuid, p_admit boolean) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
DECLARE s record; sys uuid;
BEGIN
  IF salescall.cur_actor_kind() IS DISTINCT FROM 'admin' OR salescall.cur_tenant() IS NOT NULL THEN
    RAISE EXCEPTION '名前の録音をシステムに入れるかを決めるのは、管理者だけです（システムの取引で）'
      USING ERRCODE = 'insufficient_privilege', CONSTRAINT = 'name_review_by_admin';
  END IF;
  SELECT * INTO s FROM salescall.recording_sets WHERE set_id = p_set FOR UPDATE;
  IF s.set_id IS NULL OR s.unit NOT IN ('name_role', 'name', 'name_tail') OR s.business_id IS NULL
     OR NOT salescall.set_ready(p_set) THEN
    RAISE EXCEPTION '決められるのは、業務の名前の録音で、契約者が承認した組だけです（%）', p_set
      USING ERRCODE = 'check_violation', CONSTRAINT = 'name_review_target';
  END IF;
  IF p_admit THEN
    INSERT INTO salescall.recording_sets (tenant_id, unit, voice_name, name_role, name_reading, tail)
    VALUES (NULL, s.unit, s.voice_name, s.name_role, s.name_reading, s.tail) RETURNING set_id INTO sys;
    INSERT INTO salescall.recordings (tenant_id, client_id, set_id, part, spoken_text, audio_ref, audio_sha256)
    SELECT NULL, NULL, sys, r.part, r.spoken_text, r.audio_ref, r.audio_sha256
    FROM salescall.recordings r WHERE r.set_id = p_set;
    INSERT INTO salescall.recording_approvals (tenant_id, client_id, set_id, approved_by)
    VALUES (NULL, NULL, sys, salescall.cur_actor());
  END IF;
  INSERT INTO salescall.name_recording_reviews (set_id, tenant_id, client_id, decision, system_set_id, decided_by)
  VALUES (p_set, s.tenant_id, s.client_id, CASE WHEN p_admit THEN 'admitted' ELSE 'excluded' END, sys, salescall.cur_actor());
  RETURN sys;
END $$;
REVOKE ALL ON FUNCTION salescall.review_name_recording(uuid, boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.review_name_recording(uuid, boolean) TO salescall_app;
