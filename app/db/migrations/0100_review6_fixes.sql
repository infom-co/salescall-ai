-- 0100 見直し6（2026-10-19。ユーザー：「他にも、ここまでの実装とmainに入れる内容で、あなたの想定との食い違いや、不備、修正漏れなどは
-- 起こっていないですか？」）で見つけた不備を直す（DESIGN.md 15-2 の209・210。書いた人：CL-2）
--
-- ═════════ 一時的なエラーが「1時間以上続いているか」を、契約者のすべての依頼者のエラーで数える（15-2 の209） ═════════
-- 前は、エラーを記録した取引のまま数えた（watch._lasting）。依頼者の取引では、読める範囲の決まり（error_scope）で、その依頼者の
-- エラーしか見えないので、契約者全体で止まっていても（別の耳・連携・意味の判定 など）、1つの依頼者で1時間続かなければ修正依頼が
-- 出なかった。修正依頼の重なりを防ぐ鍵は契約者ごと（error:種類:契約者）なので、数える範囲もそれに合わせる。
CREATE FUNCTION salescall.error_lasting(p_kind text, p_lasting interval) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  WITH e AS (SELECT occurred_at, lag(occurred_at) OVER (ORDER BY occurred_at) AS prev FROM salescall.error_events
             WHERE kind = p_kind AND tenant_id IS NOT DISTINCT FROM salescall.cur_tenant()
               AND occurred_at > now() - interval '7 days')
  SELECT coalesce(now() - max(occurred_at) FILTER (WHERE prev IS NULL OR occurred_at - prev > p_lasting) >= p_lasting, false) FROM e
$$;
GRANT EXECUTE ON FUNCTION salescall.error_lasting(text, interval) TO salescall_app;

-- ═════════ 録音で選んだ読みの作り直し（0099 の穴。DESIGN.md 3-7・15-2 の210） ═════════
-- 0099 は、名前の読み（版で1つの決まり）を、声ごとの録音の組に持たせ、承認のときに声どうしで突き合わせていた。そのため
--   (1) 両方の声で「なかしま」を承認したあと「なかじま」に替えようとすると、どちらの声の新しい組も、もう片方とぶつかって断られた
--   (2) 使わない印の付いた声の古い組が、ずっとほかの声の読みを止めた
--   (3) 読みを書かずに組を作れば、確かめを素通りできた
-- ⇒ その版の名前の読みは、契約者が選んだ1つだけ（version_name_readings。選び直しは記録を足すだけ。いまの読みは見方
--   current_name_readings の1か所）。録音の組は、その読みと合うものだけを承認し、流す・話してよい声を決めるときも、合う組だけを
--   使う（recording_set_fits の1か所。current_recording_sets がこれを見る）。読みを替えたら、合わない声の組は使われず、新しい読みで
--   録り直した声から話す（止まるのは、その声だけ。行き詰まらない）。
CREATE TABLE salescall.version_name_readings (
  tenant_id  uuid NOT NULL,
  client_id  uuid NOT NULL,
  version_id uuid NOT NULL,
  name_text  text NOT NULL CHECK (btrim(name_text) <> ''),
  seq        integer NOT NULL CHECK (seq >= 1),
  reading    text NOT NULL CHECK (reading ~ '^[ぁ-ゖー・]+$'),
  chosen_at  timestamptz NOT NULL DEFAULT now(),
  chosen_by  text NOT NULL DEFAULT salescall.cur_actor() CHECK (btrim(chosen_by) <> ''),
  PRIMARY KEY (version_id, name_text, seq),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, version_id) REFERENCES salescall.business_versions (client_id, version_id)
);
CREATE FUNCTION salescall.version_name_reading_by_person() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF coalesce(salescall.cur_actor_kind()::text, '') NOT IN ('operator', 'admin')
     OR NEW.chosen_by IS DISTINCT FROM salescall.cur_actor() THEN
    RAISE EXCEPTION '名前の読みを録音で選べるのは、管理者か契約者だけです（録音を聞いて選ぶ）'
      USING ERRCODE = 'insufficient_privilege', CONSTRAINT = 'version_name_reading_by_person';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER version_name_reading_by_person BEFORE INSERT ON salescall.version_name_readings
  FOR EACH ROW EXECUTE FUNCTION salescall.version_name_reading_by_person();
CREATE TRIGGER version_name_readings_frozen BEFORE UPDATE OR DELETE ON salescall.version_name_readings
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
ALTER TABLE salescall.version_name_readings ENABLE ROW LEVEL SECURITY;
CREATE POLICY recording_scope ON salescall.version_name_readings
  USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
  WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client());
GRANT SELECT, INSERT ON salescall.version_name_readings TO salescall_app;

CREATE VIEW salescall.current_name_readings WITH (security_invoker = true) AS
  SELECT n.version_id, n.name_text, n.reading FROM salescall.version_name_readings n
  WHERE n.seq = (SELECT max(m.seq) FROM salescall.version_name_readings m
                 WHERE m.version_id = n.version_id AND m.name_text = n.name_text);
GRANT SELECT ON salescall.current_name_readings TO salescall_app;

-- その組の録音の読みが、その版のいまの読みと合うか（1か所）
CREATE FUNCTION salescall.recording_set_fits(p_set uuid) RETURNS boolean LANGUAGE sql STABLE AS $$
  SELECT NOT EXISTS (
    SELECT 1 FROM salescall.recording_readings r JOIN salescall.recording_sets s ON s.set_id = r.set_id
    LEFT JOIN salescall.current_name_readings c ON c.version_id = s.version_id AND c.name_text = r.name_text
    WHERE r.set_id = p_set AND c.reading IS DISTINCT FROM r.reading)
$$;
GRANT EXECUTE ON FUNCTION salescall.recording_set_fits(uuid) TO salescall_app;

CREATE OR REPLACE VIEW salescall.current_recording_sets WITH (security_invoker = true) AS
  SELECT DISTINCT ON (s.tenant_id, s.unit, s.voice_name, s.version_id)
         s.set_id, s.tenant_id, s.client_id, s.unit, s.voice_name, s.version_id
  FROM salescall.recording_sets s JOIN salescall.recording_approvals p USING (set_id)
  WHERE salescall.recording_set_fits(s.set_id)
  ORDER BY s.tenant_id, s.unit, s.voice_name, s.version_id, p.approved_at DESC, s.created_at DESC;

DROP VIEW salescall.version_recording_readings;
DROP TRIGGER recording_approval_readings ON salescall.recording_approvals;
DROP FUNCTION salescall.recording_approval_readings();

-- 録音の読みは、その版で選んだいまの読みと同じものだけ（組の中での食い違いも、これで起きない）
CREATE OR REPLACE FUNCTION salescall.recording_reading_before_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE s record;
BEGIN
  SELECT unit, client_id, version_id INTO s FROM salescall.recording_sets WHERE set_id = NEW.set_id;
  IF s.unit IS DISTINCT FROM 'business' THEN
    RAISE EXCEPTION '録音で選んだ読みは、依頼者の単位（業務設定の版）の録音だけに付けます' USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.client_id IS DISTINCT FROM s.client_id THEN
    RAISE EXCEPTION '録音で選んだ読みの依頼者は、組と同じです' USING ERRCODE = 'check_violation';
  END IF;
  IF EXISTS (SELECT 1 FROM salescall.recording_approvals WHERE set_id = NEW.set_id) THEN
    RAISE EXCEPTION '承認した録音の組には、読みを足せません（作り直すときは新しい組）' USING ERRCODE = 'check_violation';
  END IF;
  IF (SELECT c.reading FROM salescall.current_name_readings c WHERE c.version_id = s.version_id AND c.name_text = NEW.name_text)
     IS DISTINCT FROM NEW.reading THEN
    RAISE EXCEPTION '名前「%」の読み「%」は、この版で選んだ読みと違います（先に読みを選ぶ。選び直したら、その読みで録り直す）',
                    NEW.name_text, NEW.reading
      USING ERRCODE = 'check_violation', CONSTRAINT = 'recording_reading_as_chosen';
  END IF;
  RETURN NEW;
END $$;

-- 承認：その版のいまの読みと合わない組は承認しない（作ったあとで読みを選び直したとき）
CREATE FUNCTION salescall.recording_approval_fits() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT salescall.recording_set_fits(NEW.set_id) THEN
    RAISE EXCEPTION 'この録音の名前の読みは、この版でいま選んでいる読みと違います（その読みで録り直してください。Intent.md 6-2）'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'recording_approval_fits';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER recording_approval_fits BEFORE INSERT ON salescall.recording_approvals
  FOR EACH ROW EXECUTE FUNCTION salescall.recording_approval_fits();
