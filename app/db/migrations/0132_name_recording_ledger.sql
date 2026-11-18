-- 0132 名前の録音の台帳（2026-10-24。書いた人：CL-2）
--
-- お名前の入る一言（呼ぶ一言 target・確かめる一言 target_confirm・聞き返し target_reconfirm・約束の一言 promise_again・後任の方を
-- お呼びする一言 target_successor）は、業務の録音の組に入らない（お客様ごとに名前が違う）。会話は「名前の部分（呼び方＋助詞）」と
-- 「続く言葉」を組み合わせて流す（✅ 2026-10-19 ユーザー：「名前+続く言葉を別に保存して…同じ録音だと永遠に録音し続けなくてはならない
-- ので、実運用的ではありません」・「人は聞く形にしないと今の判定では些細な違いは間違うでしょ？」。CL-1 の DESIGN.md 4-3 の「台帳の形」・
-- CL-1 の連絡帳 4-34 の 4-122・4-39）。⛔ 2026-10-24（CL-1 の 4-39）：前は、約束の一言を業務の録音の組に探し、約束の電話が1本もかからな
-- かった。その確かめは 0130 で外し、台帳ができるまで確かめていなかった。
--
-- (1) 台帳は、今の録音の組・承認の作り（0047）にのせる（承認の仕組みを2つにしない）：組の単位に2つ足す。
--     - name（名前の部分1つ）：鍵＝形（助詞「は・で・と・の」）・部署と役職（法人の呼ぶ一言・後任の方の一言・役職のみの目的のお客様
--       だけ）・名前の読み（ひらがな。役職のみの目的のお客様は無い）・声の名前。部分は1つ（名前 name）。
--     - name_tail（続く言葉1つ）：鍵＝続く言葉の名前（下の (2)）・声の名前。部分は1つ（名前は続く言葉の名前と同じ）。
--     どちらも契約者の単位（取引の契約者の持ち物。依頼者を持たない＝その契約者のどの依頼者の行でも使い回す）。同じ読みなら漢字が違っても
--     同じ録音（鍵に表記を入れない）。承認するのは、今の録音と同じく管理者か契約者が聞いて（0047 の守り）。使うのは、承認した組のうち
--     鍵ごとにいちばん新しいもの（見方 current_name_recordings の1か所）。
--     ⏳ システムの単位（すべての契約者で使い回す・管理者が前もって作る代表的な名字）：DESIGN.md 4-3 は「システムの単位」と書く。
--        契約者の取引をまたいで読む作り（契約者の無い組・管理者だけが書ける守り）が要るので、ユーザーに伺ってから足す。
-- (2) 続く言葉の名前（CL-1 の 4-3 の表。部分の名前と同じ。呼ぶ一言は法人と個人で続く言葉が違うので、個人は target_individual）：
--     target（法人「いらっしゃいますでしょうか？」。役職のみの目的のお客様も）・target_individual（個人「お電話でお間違えないでしょうか？」）・
--     target_confirm・target_reconfirm・promise_again・target_successor。
-- (3) 名前の部分の文の守り：助詞で終わる。読みのある名前の部分は「（部署・役職の）「読み」様＋助詞」の形（録音の文は、読みをひらがなで
--     「」にくくった形＝0047）。役職のみの呼び方（「社長様」「院長先生」など）の言い方は会話の側の一覧（CL-1 の phrases.ROLE_HONORIFIC）
--     なので、ここは助詞で終わることだけを見る。

ALTER TABLE salescall.recording_sets
  ADD COLUMN name_form text,
  ADD COLUMN name_role text,
  ADD COLUMN name_reading text,
  ADD COLUMN tail text;

ALTER TABLE salescall.recording_sets DROP CONSTRAINT recording_sets_unit, DROP CONSTRAINT recording_sets_owner;
ALTER TABLE salescall.recording_sets
  ADD CONSTRAINT recording_sets_unit CHECK (unit IN ('contractor', 'business', 'apology', 'name', 'name_tail')),
  ADD CONSTRAINT recording_sets_owner CHECK (CASE unit
    WHEN 'business' THEN client_id IS NOT NULL AND version_id IS NOT NULL AND order_id IS NULL
    WHEN 'apology' THEN client_id IS NOT NULL AND version_id IS NULL AND order_id IS NOT NULL
    ELSE client_id IS NULL AND version_id IS NULL AND order_id IS NULL END),
  ADD CONSTRAINT recording_sets_name_key CHECK (CASE unit
    WHEN 'name' THEN name_form IN ('は', 'で', 'と', 'の') AND tail IS NULL
                     AND (name_role IS NULL OR btrim(name_role) <> '')
                     AND (name_reading IS NULL OR name_reading ~ '^[ぁ-ゖー・]+$')
                     AND (name_reading IS NOT NULL OR name_role IS NOT NULL)
    WHEN 'name_tail' THEN tail IN ('target', 'target_individual', 'target_confirm', 'target_reconfirm', 'promise_again',
                                   'target_successor')
                          AND name_form IS NULL AND name_role IS NULL AND name_reading IS NULL
    ELSE name_form IS NULL AND name_role IS NULL AND name_reading IS NULL AND tail IS NULL END);
CREATE INDEX recording_sets_name_key ON salescall.recording_sets (tenant_id, voice_name, name_form, name_reading)
  WHERE unit = 'name';

-- 台帳の部分（1か所）：名前の部分の組は部分「name」1つで、文は鍵に合う形。続く言葉の組は、続く言葉の名前の部分1つ
CREATE FUNCTION salescall.name_recording_check() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE s record;
BEGIN
  SELECT unit, name_form, name_role, name_reading, tail INTO s FROM salescall.recording_sets WHERE set_id = NEW.set_id;
  IF s.unit = 'name' THEN
    IF NEW.part <> 'name' THEN
      RAISE EXCEPTION '名前の録音の台帳の名前の部分は、部分「name」1つです' USING ERRCODE = 'check_violation',
        CONSTRAINT = 'name_recording_part';
    END IF;
    IF NOT (right(NEW.spoken_text, 1) = s.name_form
            AND (s.name_reading IS NULL OR NEW.spoken_text =
                 coalesce(s.name_role || 'の', '') || '「' || s.name_reading || '」様' || s.name_form)) THEN
      RAISE EXCEPTION '名前の部分の文「%」は、鍵（%・%・%）と合いません（読みのある名前は「（部署・役職の）「読み」様＋助詞」）',
                      NEW.spoken_text, s.name_form, coalesce(s.name_role, '-'), coalesce(s.name_reading, '-')
        USING ERRCODE = 'check_violation', CONSTRAINT = 'name_recording_text';
    END IF;
  ELSIF s.unit = 'name_tail' AND NEW.part IS DISTINCT FROM s.tail THEN
    RAISE EXCEPTION '続く言葉の組の部分は、続く言葉の名前（%）1つです', s.tail USING ERRCODE = 'check_violation',
      CONSTRAINT = 'name_recording_part';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER name_recording_check BEFORE INSERT ON salescall.recordings
  FOR EACH ROW EXECUTE FUNCTION salescall.name_recording_check();

-- 台帳のいまの録音（1か所）：承認した組のうち、鍵ごとにいちばん新しいもの。読むのは core/recordings.py の name_missing・name_coverage
CREATE VIEW salescall.current_name_recordings WITH (security_invoker = true) AS
  SELECT DISTINCT ON (s.tenant_id, s.unit, s.voice_name, s.name_form, s.name_role, s.name_reading, s.tail)
         s.set_id, s.tenant_id, s.unit, s.voice_name, s.name_form, s.name_role, s.name_reading, s.tail
  FROM salescall.recording_sets s JOIN salescall.recording_approvals p USING (set_id)
  WHERE s.unit IN ('name', 'name_tail')
  ORDER BY s.tenant_id, s.unit, s.voice_name, s.name_form, s.name_role, s.name_reading, s.tail, p.approved_at DESC,
           s.created_at DESC;
GRANT SELECT ON salescall.current_name_recordings TO salescall_app;
