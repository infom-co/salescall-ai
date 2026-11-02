-- 0172 2026-11-03 録音の承認を、録音の中身に付ける（設計書 3-8-3 の作る順の (2)・15-2 の457。書いた人：CL-2）。
--
-- ✅ 2026-11-02 管理者 伺い2「案1でOK」（承認は録音の中身に付ける＝版が変わっても、変わった部分だけ録音して聞けばよい）。
-- ⛔ 前の形：承認は組（その版・その声の部分の一覧）ごとだった。版が変わると、変わっていない部分まで録り直して聞き直していた。
--
-- (1) 承認するという操作は、今のまま組ごとに記録する（recording_approvals＝だれが・いつ、その組を聞いて承認したか）。
-- (2) 部分が承認済みかは、中身で決める（1か所＝見方 part_states。承認した中身の台帳 approved_contents を引く）：同じ持ち主（契約者と依頼者。システムは両方とも無い）・同じ声で、
--     文・読み（録音で選んだ名前の読み）・音声の中身の指紋がまったく同じ部分（鍵の作り方は content_of の1か所。部分と読みを書いたときに part_contents に残す）が、承認した組のどれかに
--     あれば承認済み。部分の名前と
--     置き場（audio_ref）は見ない（同じ音を別の質問の番号で流しても、聞いた音は同じ）。
-- (3) 組がそろったかも1か所（set_ready）：部分が1つ以上あり、すべてが承認済みで、その版のいまの読みと合う（0100 の
--     recording_set_fits）。⇒ 全部が承認済みの部分の組は、承認を待たずにそろう。流す組（current_recording_sets）と名前の録音の
--     台帳（current_name_recordings）は、どちらもこれだけを見る（前は、承認の記録との突き合わせを見方ごとに書いていた）。
-- (4) いまの組は、そろった組のうち、いちばん新しく作った組（前は、いちばん新しく承認した組。承認しなくてもそろう組ができたので、
--     承認の時刻では並べられない）。作った時刻は、データベースが決める（書く側が渡した値を使わない＝0109 の (7) と同じ守り）。

-- 作った時刻はデータベースが決める
CREATE TRIGGER stamp_now BEFORE INSERT ON salescall.recording_sets
  FOR EACH ROW EXECUTE FUNCTION salescall.stamp_now('created_at');

-- 部分の中身の鍵（作り方は1か所＝content_of）：文・音声の指紋・録音で選んだ名前の読み（名前の順に並べた文字）をまとめたもの
CREATE FUNCTION salescall.content_of(p_set uuid, p_part text) RETURNS text LANGUAGE sql STABLE AS $$
  SELECT md5(r.spoken_text || chr(31) || r.audio_sha256 || chr(31) ||
             coalesce((SELECT string_agg(x.name_text || '=' || x.reading, '|' ORDER BY x.name_text)
                       FROM salescall.recording_readings x WHERE x.set_id = r.set_id AND x.part = r.part), ''))
  FROM salescall.recordings r WHERE r.set_id = p_set AND r.part = p_part
$$;

-- 部分の中身の鍵の表：部分と読みを書いたときに、データベースの仕掛けが content_of で作って残す（読む側は、その場で作らない。
-- ⛔ その場で作る形では、録音がたまると門の「話してよい声」が遅くなった＝固まった見張りの試験で見つけた）
CREATE TABLE salescall.part_contents (
  tenant_id uuid,
  client_id uuid,
  set_id    uuid NOT NULL,
  part      text NOT NULL,
  content   text NOT NULL,
  PRIMARY KEY (set_id, part),
  FOREIGN KEY (set_id, part) REFERENCES salescall.recordings (set_id, part)
);
ALTER TABLE salescall.part_contents ENABLE ROW LEVEL SECURITY;
CREATE POLICY part_contents_scope ON salescall.part_contents FOR SELECT
  USING (tenant_id IS NULL OR (tenant_id = salescall.cur_tenant() AND (client_id IS NULL OR client_id = salescall.cur_client())));
GRANT SELECT ON salescall.part_contents TO salescall_app;

CREATE FUNCTION salescall.part_contents_put() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = salescall, pg_temp AS $$
BEGIN
  INSERT INTO salescall.part_contents (tenant_id, client_id, set_id, part, content)
  VALUES (NEW.tenant_id, NEW.client_id, NEW.set_id, NEW.part, salescall.content_of(NEW.set_id, NEW.part))
  ON CONFLICT (set_id, part) DO UPDATE SET content = EXCLUDED.content;
  RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION salescall.part_contents_put() FROM PUBLIC;
CREATE TRIGGER part_contents_put AFTER INSERT ON salescall.recordings
  FOR EACH ROW EXECUTE FUNCTION salescall.part_contents_put();
CREATE TRIGGER part_contents_put AFTER INSERT ON salescall.recording_readings   -- 読みを足したら、その部分の鍵を作り直す
  FOR EACH ROW EXECUTE FUNCTION salescall.part_contents_put();
INSERT INTO salescall.part_contents (tenant_id, client_id, set_id, part, content)
SELECT r.tenant_id, r.client_id, r.set_id, r.part, salescall.content_of(r.set_id, r.part) FROM salescall.recordings r;

-- 承認した中身の台帳：組を承認したときに、その組の部分の中身の鍵を、持ち主（契約者と依頼者）と声ごとに1回だけ書く（データベースの
-- 仕掛けが書く＝書き忘れが起きない。書けるのは仕掛けだけ）
CREATE TABLE salescall.approved_contents (
  tenant_id   uuid,                                   -- 持ち主（システムは契約者も依頼者も無い）
  client_id   uuid,
  voice_name  text NOT NULL REFERENCES salescall.voices (voice_name),
  content     text NOT NULL,                          -- part_contents の content（作り方は content_of）
  set_id      uuid NOT NULL REFERENCES salescall.recording_sets (set_id),   -- はじめに承認した組
  approved_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX approved_contents_one ON salescall.approved_contents (voice_name, content, tenant_id, client_id)
  NULLS NOT DISTINCT;
ALTER TABLE salescall.approved_contents ENABLE ROW LEVEL SECURITY;
CREATE POLICY approved_contents_scope ON salescall.approved_contents FOR SELECT
  USING (tenant_id IS NULL OR (tenant_id = salescall.cur_tenant() AND (client_id IS NULL OR client_id = salescall.cur_client())));
GRANT SELECT ON salescall.approved_contents TO salescall_app;

CREATE FUNCTION salescall.approved_contents_add() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = salescall, pg_temp AS $$
BEGIN
  INSERT INTO salescall.approved_contents (tenant_id, client_id, voice_name, content, set_id)
  SELECT s.tenant_id, s.client_id, s.voice_name, k.content, s.set_id
  FROM salescall.part_contents k JOIN salescall.recording_sets s ON s.set_id = k.set_id WHERE k.set_id = NEW.set_id
  ON CONFLICT DO NOTHING;
  RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION salescall.approved_contents_add() FROM PUBLIC;
CREATE TRIGGER approved_contents_add AFTER INSERT ON salescall.recording_approvals
  FOR EACH ROW EXECUTE FUNCTION salescall.approved_contents_add();
-- 前に承認した組の分も書き写す
INSERT INTO salescall.approved_contents (tenant_id, client_id, voice_name, content, set_id)
SELECT s.tenant_id, s.client_id, s.voice_name, k.content, s.set_id
FROM salescall.recording_approvals p JOIN salescall.recording_sets s ON s.set_id = p.set_id
JOIN salescall.part_contents k ON k.set_id = s.set_id
ORDER BY p.approved_at
ON CONFLICT DO NOTHING;

-- 部分は承認済みか（1か所＝見方 part_states）：同じ持ち主・同じ声で、同じ中身の鍵が、承認した中身の台帳にあるか。見方にしたのは、
-- 1つの問い合わせの中に組み込まれて、部分ごとに問い合わせを組み立て直さないため（⛔ はじめは部分ごとに関数を呼び、録音がたまると
-- 門の「話してよい声」が約50倍遅くなった＝0.011秒 → 0.536秒。固まった見張りの試験で見つけた）
CREATE VIEW salescall.part_states WITH (security_invoker = true) AS
  SELECT k.set_id, k.part,
         EXISTS (SELECT 1 FROM salescall.approved_contents a
                 WHERE a.voice_name = s.voice_name AND a.content = k.content
                   AND a.tenant_id IS NOT DISTINCT FROM s.tenant_id AND a.client_id IS NOT DISTINCT FROM s.client_id) AS approved
  FROM salescall.part_contents k JOIN salescall.recording_sets s ON s.set_id = k.set_id;
GRANT SELECT ON salescall.part_states TO salescall_app;

-- その組はそろったか（1か所）：部分が1つ以上・すべて承認済み・その版のいまの読みと合う
CREATE FUNCTION salescall.set_ready(p_set uuid) RETURNS boolean LANGUAGE sql STABLE AS $$
  SELECT EXISTS (SELECT 1 FROM salescall.recordings WHERE set_id = p_set)
     AND NOT EXISTS (SELECT 1 FROM salescall.part_states WHERE set_id = p_set AND NOT approved)
     AND salescall.recording_set_fits(p_set)
$$;
GRANT EXECUTE ON FUNCTION salescall.set_ready(uuid) TO salescall_app;

-- 流す組（共通の一言・業務の録音）：そろった組のうち、いちばん新しく作った組（0131 を作り直す）。単位・声・版ごとに、新しい順に
-- 調べて、はじめにそろった組で止める（古い組まで、そろったかを調べない。⛔ 全部の組を調べる形では、録音がたまると遅かった）
CREATE INDEX recording_sets_newest ON salescall.recording_sets (voice_name, unit, created_at DESC, set_id DESC);
CREATE OR REPLACE VIEW salescall.current_recording_sets WITH (security_invoker = true) AS
  SELECT c.set_id, g.tenant_id, c.client_id, g.unit, g.voice_name, g.version_id
  FROM (SELECT DISTINCT s.tenant_id, s.unit, s.voice_name, s.version_id FROM salescall.recording_sets s
        WHERE s.unit IN ('contractor', 'business')) g
  CROSS JOIN LATERAL (
    SELECT s.set_id, s.client_id FROM salescall.recording_sets s
    WHERE s.voice_name = g.voice_name AND s.unit = g.unit
      AND s.tenant_id IS NOT DISTINCT FROM g.tenant_id AND s.version_id IS NOT DISTINCT FROM g.version_id
      AND salescall.set_ready(s.set_id)
    ORDER BY s.created_at DESC, s.set_id DESC LIMIT 1) c;

-- 名前の録音の台帳のいまの録音：鍵ごとに1つ。契約者のものを先に（その契約者が聞いて選んだ）、無ければシステムのもの。同じ持ち主の
-- 中では、いちばん新しく作ったそろった組（0134 を作り直す。どの契約者のものが見えるかは、取引の範囲で決まる）
CREATE OR REPLACE VIEW salescall.current_name_recordings WITH (security_invoker = true) AS
  SELECT DISTINCT ON (s.unit, s.voice_name, s.name_role, s.name_reading, s.tail)
         s.set_id, s.tenant_id, s.unit, s.voice_name, s.name_role, s.name_reading, s.tail
  FROM salescall.recording_sets s
  WHERE s.unit IN ('name_role', 'name', 'name_tail') AND salescall.set_ready(s.set_id)
  ORDER BY s.unit, s.voice_name, s.name_role, s.name_reading, s.tail, (s.tenant_id IS NULL), s.created_at DESC, s.set_id DESC;
