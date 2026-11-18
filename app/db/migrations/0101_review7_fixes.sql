-- 0101 見直し7（2026-10-19。ユーザー：「他にも、ここまでの実装とmainに入れる内容で、あなたの想定との食い違いや、不備、修正漏れなどは
-- 起こっていないですか？」）で見つけた不備を直す（DESIGN.md 15-2 の211・212。書いた人：CL-2）
--
-- ═════════ 名前のいまの読みを、版の読みも含めて1か所にする（0100 の穴。15-2 の211） ═════════
-- 0100 は「その版の名前のいまの読み」（current_name_readings）に、録音を聞いて選んだ読みしか入れなかった。そのため
--   (1) 入口の読みのある名前（担当者名など）の読みを録音に書くと、Python は通すのに、データベースが「選んだ読みと違う」と断った
--   (2) AI が決めた読み・契約者が確かめた読みの名前を話す録音は、管理者か契約者が「選ぶ」まで作れなかった（フルオートでも人の手作業。
--       Intent.md 6-8：「入口の読み・フルオートの AI の読みは、そのまま使う」と食い違い。6-2 で選ぶのは、読みが決まらない名前だけ）
-- ⇒ 版の名前の読みの表（version_name_readings）に、版の中身に書いた決まった読み（入口の読み・契約者が確かめた読み・AI の読み）を
--   番号0で入れ（版の中身を置いたとき、データベースが入れる＝どの書き方で置いても同じ）、録音で選んだ読みは番号1から足す。
--   いまの読みは、いちばん大きい番号（見方 current_name_readings。読みを決めたもの reading_by も返す）。入口の読みのある名前は選べない。
--   業務設定を読む1か所（business_settings.load）・録音の確かめ（recordings）・データベースの確かめ（recording_set_fits）は、
--   どれもこの見方だけを見る（順の決まりを2か所に書かない）。
ALTER TABLE salescall.version_name_readings DROP CONSTRAINT version_name_readings_seq_check;
ALTER TABLE salescall.version_name_readings
  ADD COLUMN reading_by text NOT NULL DEFAULT 'recording' CHECK (reading_by IN ('given', 'person', 'ai', 'recording')),
  ADD CONSTRAINT version_name_readings_seq_check CHECK ((seq = 0) = (reading_by <> 'recording') AND seq >= 0);

CREATE OR REPLACE FUNCTION salescall.version_name_reading_by_person() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.reading_by <> 'recording' THEN   -- 版の中身に書いた読み（版の中身を置いたときだけ。下の business_content_readings）
    RETURN NEW;
  END IF;
  IF coalesce(salescall.cur_actor_kind()::text, '') NOT IN ('operator', 'admin')
     OR NEW.chosen_by IS DISTINCT FROM salescall.cur_actor() THEN
    RAISE EXCEPTION '名前の読みを録音で選べるのは、管理者か契約者だけです（録音を聞いて選ぶ）'
      USING ERRCODE = 'insufficient_privilege', CONSTRAINT = 'version_name_reading_by_person';
  END IF;
  IF EXISTS (SELECT 1 FROM salescall.version_name_readings g WHERE g.version_id = NEW.version_id AND g.name_text = NEW.name_text
             AND g.reading_by = 'given') THEN
    RAISE EXCEPTION '名前「%」の読みは入口で入っています（録音では選びません）', NEW.name_text
      USING ERRCODE = 'check_violation', CONSTRAINT = 'version_name_reading_given';
  END IF;
  RETURN NEW;
END $$;

-- 版の中身を置いたとき、名前の決まった読みを番号0で入れる（表記は business_settings の確かめと同じく前後の空白を除いたもの）
CREATE FUNCTION salescall.business_content_readings() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO salescall.version_name_readings (tenant_id, client_id, version_id, name_text, seq, reading, reading_by, chosen_at, chosen_by)
  SELECT NEW.tenant_id, NEW.client_id, NEW.version_id, btrim(e ->> 'text'), 0, e ->> 'reading', e ->> 'reading_by', NEW.saved_at, NEW.saved_by
  FROM jsonb_array_elements(coalesce(NEW.content -> 'names', '[]'::jsonb)) e
  WHERE e ->> 'reading' IS NOT NULL
  ON CONFLICT DO NOTHING;   -- 同じ表記が2つ（中身の確かめで同じ読み）
  RETURN NEW;
END $$;
CREATE TRIGGER business_content_readings AFTER INSERT ON salescall.business_contents
  FOR EACH ROW EXECUTE FUNCTION salescall.business_content_readings();

-- いまある版の分（0100 で選んだ読みは番号1から。番号0は空いている）
INSERT INTO salescall.version_name_readings (tenant_id, client_id, version_id, name_text, seq, reading, reading_by, chosen_at, chosen_by)
SELECT c.tenant_id, c.client_id, c.version_id, btrim(e ->> 'text'), 0, e ->> 'reading', e ->> 'reading_by', c.saved_at, c.saved_by
FROM salescall.business_contents c, jsonb_array_elements(coalesce(c.content -> 'names', '[]'::jsonb)) e
WHERE e ->> 'reading' IS NOT NULL
ON CONFLICT DO NOTHING;

CREATE OR REPLACE VIEW salescall.current_name_readings WITH (security_invoker = true) AS
  SELECT n.version_id, n.name_text, n.reading, n.reading_by FROM salescall.version_name_readings n
  WHERE n.seq = (SELECT max(m.seq) FROM salescall.version_name_readings m
                 WHERE m.version_id = n.version_id AND m.name_text = n.name_text);

CREATE OR REPLACE FUNCTION salescall.recording_reading_before_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE s record;
BEGIN
  SELECT unit, client_id, version_id INTO s FROM salescall.recording_sets WHERE set_id = NEW.set_id;
  IF s.unit IS DISTINCT FROM 'business' THEN
    RAISE EXCEPTION '録音の名前の読みは、依頼者の単位（業務設定の版）の録音だけに付けます' USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.client_id IS DISTINCT FROM s.client_id THEN
    RAISE EXCEPTION '録音の名前の読みの依頼者は、組と同じです' USING ERRCODE = 'check_violation';
  END IF;
  IF EXISTS (SELECT 1 FROM salescall.recording_approvals WHERE set_id = NEW.set_id) THEN
    RAISE EXCEPTION '承認した録音の組には、読みを足せません（作り直すときは新しい組）' USING ERRCODE = 'check_violation';
  END IF;
  IF (SELECT c.reading FROM salescall.current_name_readings c WHERE c.version_id = s.version_id AND c.name_text = NEW.name_text)
     IS DISTINCT FROM NEW.reading THEN
    RAISE EXCEPTION '名前「%」の読み「%」は、この版のいまの読みと違います（読みが決まらない名前は先に録音を聞いて選ぶ。選び直したら、'
                    'その読みで録り直す）', NEW.name_text, NEW.reading
      USING ERRCODE = 'check_violation', CONSTRAINT = 'recording_reading_as_chosen';
  END IF;
  RETURN NEW;
END $$;

-- ═════════ 権限の穴（15-2 の212） ═════════
-- (1) error_lasting（0100）だけ、誰でも呼べるまま（PUBLIC）だった。払い出し専用の役が、取引の契約者を好きに置いて呼ぶと、ほかの
--     契約者のエラーが続いているかが分かった（ほかの SECURITY DEFINER の関数は、どれも PUBLIC から外している）
REVOKE ALL ON FUNCTION salescall.error_lasting(text, interval) FROM PUBLIC;

-- (2) 門は「これからかける約束」の一覧（promises_waiting）を、依頼者の取引から直に読んでいたので、同じ契約者のほかの依頼者の約束
--     （行・依頼者・時刻）が見えた（行の読める範囲の決まりの迂回）。門が要るのは、いちばん近いお客様の指定の時刻だけ ⇒ それだけを返す
--     関数にし、一覧の2つ（promises_waiting・lines_serving）は、ほかの SECURITY DEFINER の関数の中だけで使う（アプリの役から外す）
CREATE FUNCTION salescall.next_customer_promise_at(p_except uuid) RETURNS timestamptz
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT min(w.callee_at) FROM salescall.promises_waiting() w WHERE w.callee_at > now() AND w.task_id <> p_except
$$;
REVOKE ALL ON FUNCTION salescall.next_customer_promise_at(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.next_customer_promise_at(uuid) TO salescall_app;
REVOKE EXECUTE ON FUNCTION salescall.promises_waiting() FROM salescall_app;
REVOKE EXECUTE ON FUNCTION salescall.lines_serving() FROM salescall_app;

-- (3) ⛔ PC の鍵を作る・止める関数（0084）が、誰でも呼べるままだった。0084 は持ち主を鍵を作る役（salescall_login_maker）へ移した
--     あとで「誰でも」から外したが、移行を流す役は、その役の権限を受け継がない形（INHERIT FALSE）でしか入っていないので、外すのも
--     払い出し専用の役へ渡すのも、データベースが何もせずに通していた。そのため、アプリの役（契約者の PC の鍵もこれを受け継ぐ）から、
--     ほかの契約者の名前の形の鍵を好きな合言葉で作れ（＝その契約者としてログインできる）、ほかの契約者の PC を止められた
--     （見直し7で、使い捨てのデータベースで確かめた）⇒ 持ち主の役になって外し、払い出し専用の役にだけ渡す。
SET ROLE salescall_login_maker;
REVOKE ALL ON FUNCTION salescall.make_pc_login(text, text), salescall.stop_pc_logins(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.make_pc_login(text, text), salescall.stop_pc_logins(text) TO salescall_provisioner;
RESET ROLE;

-- (4) 依頼者が有効か（client_active。0059 で SECURITY DEFINER にしたとき、誰でも呼べるまま）
REVOKE ALL ON FUNCTION salescall.client_active(uuid) FROM PUBLIC;
