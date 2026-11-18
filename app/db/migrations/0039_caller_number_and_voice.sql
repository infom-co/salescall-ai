-- 0039 依頼者が替わったら、発信番号と声を替える（2026-10-17 ユーザー決定。DESIGN.md 5-2-2 の順8・順10）。
-- 書いた人：CL-2
-- ユーザー：「依頼者が違うと、期間に関係なく、声と発信番号は変えた方がいいでしょう。その提言を受けて、依頼者ごとに発信番号が
--   あることがデフォルトがいいですね。ただし、検証時は発信番号は1つでOK」「契約者が変わると発信番号は確実に別になります（契約者が
--   携帯を用意する）が、声は変えた方がいいですね」。前の電話が男性の声なら Leda、のように（同じ男性ならサービスを聞く前に嫌悪感を持たれる）。
-- (1) 回線を依頼者に割り当てられる（lines.client_id。空＝その契約者のどの依頼者にも使う共有の回線）。
-- (2) 声の型の一覧（voice_types）：声の型の名前 → 声の名前。中身（話し方の指示）は CL-1 の持ち物（Intent.md 9-8）。ここに持つのは、
--     声を替える判断に要る「どの声か」と、声ごとの初めの声の型だけ。
-- (3) 試行に、使う声の型（attempts.voice_type）。決めるのは門の1か所（予約のとき）。会話の処理は、その声の型で話す。
-- (4) ほかの依頼者（ほかの契約者を含む）が、その番号へ最後にかけた声の名前と、その回線がほかの依頼者のためにその番号へかけたか、
--     を返す関数（中身は出さない：声の名前・はい／いいえ だけ）。

ALTER TABLE salescall.lines ADD COLUMN client_id uuid;
ALTER TABLE salescall.lines ADD CONSTRAINT lines_client FOREIGN KEY (tenant_id, client_id)
  REFERENCES salescall.clients (tenant_id, client_id);

CREATE TABLE salescall.voice_types (
  code        text PRIMARY KEY CHECK (code ~ '^[A-Za-z0-9_-]{1,32}$'),
  voice_name  text NOT NULL CHECK (btrim(voice_name) <> ''),      -- 声の名前（Sadachbia・Leda など）
  default_for_voice boolean NOT NULL DEFAULT false,              -- その声の初めの声の型（声を替えるとき、替えた先に使う）
  note        text NOT NULL CHECK (btrim(note) <> '')
);
CREATE UNIQUE INDEX voice_types_one_default ON salescall.voice_types (voice_name) WHERE default_for_voice;
INSERT INTO salescall.voice_types (code, voice_name, default_for_voice, note) VALUES
('S2', 'Sadachbia', true,  '前向き＋会社員（Intent.md 9-8。2026-10-12 ユーザー指定の A/B のグループA）'),
('S4', 'Sadachbia', false, 'にこやかな受付（Intent.md 9-8。A/B のグループB）'),
('L4', 'Leda',      true,  '謙虚な会社員（Intent.md 9-8）');
GRANT SELECT ON salescall.voice_types TO salescall_app;

ALTER TABLE salescall.attempts ADD COLUMN voice_type text REFERENCES salescall.voice_types (code);

-- ほかの依頼者（同じ契約者のほかの依頼者・ほかの契約者）が、その番号へ最後にかけた声の名前（無ければ NULL）
CREATE FUNCTION salescall.number_last_voice_elsewhere(p_phone text) RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT v.voice_name FROM salescall.attempts a JOIN salescall.voice_types v ON v.code = a.voice_type
  WHERE a.phone_e164 = p_phone AND a.client_id IS DISTINCT FROM salescall.cur_client()
    AND a.state NOT IN ('claimed', 'released')
  ORDER BY a.claimed_at DESC LIMIT 1
$$;
-- その回線（取引の契約者の回線）が、ほかの依頼者のために、その番号へかけたことがあるか（発信番号を替える判断）
CREATE FUNCTION salescall.line_called_number_elsewhere(p_line uuid, p_phone text) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT EXISTS (SELECT 1 FROM salescall.attempts a
                 WHERE a.line_id = p_line AND a.tenant_id = salescall.cur_tenant() AND a.phone_e164 = p_phone
                   AND a.client_id IS DISTINCT FROM salescall.cur_client() AND a.state NOT IN ('claimed', 'released'))
$$;
REVOKE ALL ON FUNCTION salescall.number_last_voice_elsewhere(text), salescall.line_called_number_elsewhere(uuid, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.number_last_voice_elsewhere(text), salescall.line_called_number_elsewhere(uuid, text)
  TO salescall_app;
