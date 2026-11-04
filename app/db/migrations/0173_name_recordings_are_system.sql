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
-- (3) システムの共通の録音に入れるかは、管理者が聞いて決める（まとめて決める1か所＝review_name_recordings。決めるのは管理者だけ＝
--     システムの取引で）。管理者のチェックを要らないとした契約者の組は、決まった間隔の仕事が入れる（admit_unreviewed_name_recordings）。
--     入れる＝データベースの仕掛け（name_review_decide の1か所）が、その録音の部分（文・音声の置き場・指紋）をそのまま写したシステムの組を
--     作り、その中身を承認した中身の台帳に入れる（だれが決めたかは決めた記録）。すべての契約者が読む。
-- (4) 管理者が聞くのは、契約者が承認して、その業務の電話で使えるようになった組だけ（まだ承認していない組は、契約者が録り直すかも
--     しれない）。まだ決めていない組は name_review_pending の1か所（管理者が読むのは name_recordings_to_review。どの契約者・どの依頼者の
--     タブ・どの業務のものかも出す）。
-- (5) 決めた記録（入れた・除外した・だれが・いつ・入れたシステムの組）は name_recording_reviews に1回だけ残す（変えられない）。
--     契約者は自分の分を読める（除外されたか分かる）。ほかの契約者は読めない。
-- (6) システムの組の側には、どの業務の組から写したかを書かない（ほかの契約者に、どの業務がどの名前のお客様に電話しているかを見せない。
--     管理者には見せる＝(4)）。
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

-- (8) 管理者のチェックを要らないとした契約者（✅ 2026-11-04 管理者：「この契約者のチェックは、管理者がチェックしなくてもシステムに入れて
--     OK という設定も設置するようにしてください。信頼や熟練した契約者やスタッフが契約者の場合は、私がチェック不要にするという意味合い
--     です。」）。決めるのは管理者だけ（契約者が自分で変えられる設定の表とは別の記録にする）。いまの値は、その契約者のいちばん新しい行
--     （行は変えられない＝だれが・いつ変えたかが残る）。行の無い契約者は、管理者のチェックが要る。
CREATE TABLE salescall.name_review_skips (
  seq       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES salescall.tenants (tenant_id),
  skip      boolean NOT NULL,                              -- true＝管理者のチェック不要（契約者が承認すれば、システムの共通の録音に入る）
  set_by    text NOT NULL CHECK (btrim(set_by) <> ''),
  set_at    timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE salescall.name_review_skips ENABLE ROW LEVEL SECURITY;
CREATE POLICY skip_scope ON salescall.name_review_skips FOR SELECT
  USING (tenant_id = salescall.cur_tenant()
         OR (salescall.cur_tenant() IS NULL AND salescall.cur_actor_kind() IS NOT DISTINCT FROM 'admin'));
GRANT SELECT ON salescall.name_review_skips TO salescall_app;
CREATE TRIGGER name_review_skips_frozen BEFORE UPDATE OR DELETE ON salescall.name_review_skips
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
CREATE TRIGGER stamp_now BEFORE INSERT ON salescall.name_review_skips
  FOR EACH ROW EXECUTE FUNCTION salescall.stamp_now('set_at');

-- 管理者だけ（システムの取引で）：その契約者のチェックを要らないとする／要るに戻す
CREATE FUNCTION salescall.set_name_review_skip(p_tenant uuid, p_skip boolean) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
BEGIN
  IF salescall.cur_actor_kind() IS DISTINCT FROM 'admin' OR salescall.cur_tenant() IS NOT NULL THEN
    RAISE EXCEPTION '名前の録音の管理者のチェックを要らないとするか決めるのは、管理者だけです（システムの取引で）'
      USING ERRCODE = 'insufficient_privilege', CONSTRAINT = 'name_review_by_admin';
  END IF;
  INSERT INTO salescall.name_review_skips (tenant_id, skip, set_by) VALUES (p_tenant, p_skip, salescall.cur_actor());
END $$;
REVOKE ALL ON FUNCTION salescall.set_name_review_skip(uuid, boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.set_name_review_skip(uuid, boolean) TO salescall_app;

-- その契約者は、管理者のチェックが要らないか（1か所）
CREATE FUNCTION salescall.name_review_skipped(p_tenant uuid) RETURNS boolean LANGUAGE sql STABLE AS $$
  SELECT coalesce((SELECT k.skip FROM salescall.name_review_skips k WHERE k.tenant_id = p_tenant
                   ORDER BY k.set_at DESC, k.seq DESC LIMIT 1), false)
$$;
REVOKE ALL ON FUNCTION salescall.name_review_skipped(uuid) FROM PUBLIC;

-- (3)(4) まだ決めていない組（1か所）：契約者が業務の録音として作り、その業務の電話で使えるようになった（set_ready）名前の録音の組で、
-- 決めた記録の無いもの。管理者に聞いていただくので、どの契約者・どの依頼者のタブ・どの業務のものかも返す（✅ 2026-11-04 管理者：
-- 「信頼度の高い契約者のものはそのまま通す可能性がありますし、信頼度の低い契約者はじっくりとチェックをして、確認する必要がある」）。
-- 中の関数（持ち主の権限の関数からだけ呼ぶ）
CREATE FUNCTION salescall.name_review_pending()
RETURNS TABLE (set_id uuid, tenant_id uuid, tenant_name text, client_name text, business_name text, skipped boolean,
               unit text, voice_name text, name_role text, name_reading text, tail text,
               part text, spoken_text text, audio_ref text, audio_sha256 text, created_at timestamptz)
LANGUAGE sql STABLE AS $$
  SELECT s.set_id, s.tenant_id, t.name, c.name, b.name, salescall.name_review_skipped(s.tenant_id),
         s.unit, s.voice_name, s.name_role, s.name_reading, s.tail,
         r.part, r.spoken_text, r.audio_ref, r.audio_sha256, s.created_at
  FROM salescall.recording_sets s JOIN salescall.recordings r ON r.set_id = s.set_id
  JOIN salescall.tenants t ON t.tenant_id = s.tenant_id
  JOIN salescall.clients c ON c.tenant_id = s.tenant_id AND c.client_id = s.client_id
  JOIN salescall.businesses b ON b.client_id = s.client_id AND b.business_id = s.business_id
  WHERE s.unit IN ('name_role', 'name', 'name_tail') AND s.business_id IS NOT NULL
    AND NOT EXISTS (SELECT 1 FROM salescall.name_recording_reviews v WHERE v.set_id = s.set_id)
    AND salescall.set_ready(s.set_id)
  ORDER BY t.name, s.tenant_id, s.created_at, s.set_id
$$;
REVOKE ALL ON FUNCTION salescall.name_review_pending() FROM PUBLIC;

-- 管理者だけ（システムの取引で）が読む一覧
CREATE FUNCTION salescall.name_recordings_to_review()
RETURNS TABLE (set_id uuid, tenant_id uuid, tenant_name text, client_name text, business_name text, skipped boolean,
               unit text, voice_name text, name_role text, name_reading text, tail text,
               part text, spoken_text text, audio_ref text, audio_sha256 text, created_at timestamptz)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
BEGIN
  IF salescall.cur_actor_kind() IS DISTINCT FROM 'admin' OR salescall.cur_tenant() IS NOT NULL THEN
    RAISE EXCEPTION '名前の録音をシステムに入れるかを決めるのは、管理者だけです（システムの取引で）'
      USING ERRCODE = 'insufficient_privilege', CONSTRAINT = 'name_review_by_admin';
  END IF;
  RETURN QUERY SELECT * FROM salescall.name_review_pending();
END $$;
REVOKE ALL ON FUNCTION salescall.name_recordings_to_review() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.name_recordings_to_review() TO salescall_app;

-- 1つの組を決める（中の関数。1か所）：入れる＝聞いた中身（文・音声の置き場・指紋）をそのまま写したシステムの組を作り、その中身を
-- 承認した中身の台帳に入れる（0172＝この組がそろう。承認したのは決めた記録の decided_by）／除外する。どちらも1回だけ（決めた記録の鍵）。
CREATE FUNCTION salescall.name_review_decide(p_set uuid, p_admit boolean, p_by text) RETURNS uuid
LANGUAGE plpgsql AS $$
DECLARE s record; sys uuid;
BEGIN
  SELECT * INTO s FROM salescall.recording_sets WHERE set_id = p_set FOR UPDATE;
  IF s.set_id IS NULL OR s.unit NOT IN ('name_role', 'name', 'name_tail') OR s.business_id IS NULL
     OR NOT salescall.set_ready(p_set) THEN
    RAISE EXCEPTION '決められるのは、業務の名前の録音で、契約者が承認した組だけです（%）', p_set
      USING ERRCODE = 'check_violation', CONSTRAINT = 'name_review_target';
  END IF;
  IF p_admit THEN
    INSERT INTO salescall.recording_sets (tenant_id, unit, voice_name, name_role, name_reading, tail, created_by)
    VALUES (NULL, s.unit, s.voice_name, s.name_role, s.name_reading, s.tail, p_by) RETURNING set_id INTO sys;
    INSERT INTO salescall.recordings (tenant_id, client_id, set_id, part, spoken_text, audio_ref, audio_sha256)
    SELECT NULL, NULL, sys, r.part, r.spoken_text, r.audio_ref, r.audio_sha256
    FROM salescall.recordings r WHERE r.set_id = p_set;
    INSERT INTO salescall.approved_contents (tenant_id, client_id, voice_name, content, set_id)
    SELECT NULL, NULL, s.voice_name, k.content, sys FROM salescall.part_contents k WHERE k.set_id = sys
    ON CONFLICT DO NOTHING;
  END IF;
  INSERT INTO salescall.name_recording_reviews (set_id, tenant_id, client_id, decision, system_set_id, decided_by)
  VALUES (p_set, s.tenant_id, s.client_id, CASE WHEN p_admit THEN 'admitted' ELSE 'excluded' END, sys, p_by);
  RETURN sys;
END $$;
REVOKE ALL ON FUNCTION salescall.name_review_decide(uuid, boolean, text) FROM PUBLIC;

-- 管理者がまとめて決める（✅ 2026-11-04 管理者：「一個一個チェック登録という形でやっていては時間がかかってしょうがないので、まとめて
-- 流して、私がレ点を入れたものをのみ登録、もしくは除外を入れたもののみ、登録されないという登録方法にしてください。」）：聞いていただいた
-- 組の並び p_sets のうち、p_admit に入れた組を入れ、残りを除外する（どちらの登録方法も、画面の側で入れる組を決めて渡す＝
-- recordings.review_names の1か所）。1つの取引で全部か何も無いか（決められない組が1つでもあれば、どれも決めない）。
CREATE FUNCTION salescall.review_name_recordings(p_sets uuid[], p_admit uuid[])
RETURNS TABLE (set_id uuid, system_set_id uuid)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
DECLARE x uuid;
BEGIN
  IF salescall.cur_actor_kind() IS DISTINCT FROM 'admin' OR salescall.cur_tenant() IS NOT NULL THEN
    RAISE EXCEPTION '名前の録音をシステムに入れるかを決めるのは、管理者だけです（システムの取引で）'
      USING ERRCODE = 'insufficient_privilege', CONSTRAINT = 'name_review_by_admin';
  END IF;
  IF EXISTS (SELECT 1 FROM unnest(p_admit) a WHERE a <> ALL (p_sets)) OR cardinality(p_sets) <> (SELECT count(DISTINCT u) FROM unnest(p_sets) u) THEN
    RAISE EXCEPTION '入れる組は、聞いていただいた組の中から、重ねずに選びます'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'name_review_target';
  END IF;
  FOREACH x IN ARRAY p_sets LOOP
    set_id := x;
    system_set_id := salescall.name_review_decide(x, x = ANY (p_admit), salescall.cur_actor());
    RETURN NEXT;
  END LOOP;
END $$;
REVOKE ALL ON FUNCTION salescall.review_name_recordings(uuid[], uuid[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.review_name_recordings(uuid[], uuid[]) TO salescall_app;

-- 管理者のチェックを要らないとした契約者の組を入れる（決まった間隔の仕事が、システムの取引で呼ぶ。1か所）。入れた組の数を返す
CREATE FUNCTION salescall.admit_unreviewed_name_recordings() RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
DECLARE n integer := 0; x uuid;
BEGIN
  IF salescall.cur_actor_kind() IS NULL OR salescall.cur_actor_kind() = 'operator' OR salescall.cur_tenant() IS NOT NULL THEN
    RAISE EXCEPTION '管理者のチェックが要らない契約者の名前の録音を入れるのは、システムの取引の仕事だけです'
      USING ERRCODE = 'insufficient_privilege', CONSTRAINT = 'name_review_by_admin';
  END IF;
  FOR x IN SELECT p.set_id FROM salescall.name_review_pending() p WHERE p.skipped LOOP
    PERFORM salescall.name_review_decide(x, true, '管理者のチェック不要の契約者（システム）');
    n := n + 1;
  END LOOP;
  RETURN n;
END $$;
REVOKE ALL ON FUNCTION salescall.admit_unreviewed_name_recordings() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.admit_unreviewed_name_recordings() TO salescall_app;

-- システムの組・部分・承認を書けるのは、システムの取引の管理者と、持ち主の権限で動く仕掛け（業務の名前の録音を写す1か所
-- name_review_decide）だけ（0171 の system_recording_check を作り直す。前は管理者だけ）
CREATE OR REPLACE FUNCTION salescall.system_recording_check() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.tenant_id IS NULL AND salescall.cur_actor_kind() IS DISTINCT FROM 'admin' AND current_user = session_user THEN
    RAISE EXCEPTION 'システムの録音（共通の一言・名前の録音）を作れて承認できるのは、管理者だけです（システムの取引で）'
      USING ERRCODE = 'insufficient_privilege', CONSTRAINT = 'system_recording_by_admin';
  END IF;
  RETURN NEW;
END $$;
