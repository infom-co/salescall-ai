-- 0154 取り消せない決定の保留と答えを「中身ごと」にする・報告が変わったら保留を決め直す・確定の印の守り・取り消しの守り
-- （見直し24 の a・b・c。2026-10-30 管理者のご決定 (6)(7) の作りの穴。書いた人：CL-2。DESIGN.md 10-2-2・2章 Q53〜Q57）
--
-- 見つかった穴（どれも試験で確かめた）：
-- (1) 「その通話に答えが1つでもあれば、保留は閉じた」を、答えの中身（断り・番号違い・怒っていた）を見ずに8か所で決めていた。発信禁止を
--     取り消すと、取り消していない「怒っていた」の保留まで閉じ、録音でも契約者でも確かめないまま苦情で行を終えた。
-- (2) 保留の中身を、最初の締めの1回だけで決めていた。録音から作り直した報告で中身が変わっても（怒っていたが消えて断りが出た）、承認の
--     一覧は前の中身のまま出て、契約者が差し戻すと、契約者が見ていない断りがその場で営業拒否になった。報告が変わったあとに承認を押すと、
--     答え・結果・誤判定率の正解が食い違った。
-- (3) 保留の版を、結果の承認（approval.approve_result）で確定できた。あとの通話の結果がある行で、前の通話を締め直した版が行の結果として
--     確定した（断ったお客様の行が「open」で確定した）。
-- (4) データベースの守りの抜け：取り消しの答えを、発信禁止を取り消さないまま入れられた（ほかの保留が閉じた）。保留を、その通話と違う行で
--     作れた。やり取りの文字を、通話後の検証が済んだあとにも書き足せた。
--
-- ⇒ (a) 保留は版を重ねる（decision_waits に seq。いちばん新しい版が今の保留。中身が空の版＝もう待たない）。保留を書くのは、締めの処理が
--        判定するたびに呼ぶ core/approval.record_wait の1か所（いまの報告と答えから「待つ中身」を決め、前の版と違えば足す）。
--    (b) 答えは中身ごとに効く（承認した中身は、もう待たない・外した中身は報告から外す＝core/approval.decision_wait）。答えたら締め直し、
--        締め直しが保留の版を決め直す（待たなくなれば中身が空の版）。いまの保留の中身は、データベースの1か所 salescall.decision_open
--        （いちばん新しい版の中身）。「まだ決まっていない」（decision_pending）・契約者に答えを求めているか（decision_asks_contractor）・
--        答えの守り・印の守りは、どれもこれを読む（⛔ 答えのある中身も、ここで引いていた＝閉じ方が2つあった。版の1つにした）。
--    (c) 承認と差し戻しは、画面が見た版（wait_seq）にだけ答えられる（報告が変わって版が替わったあとの古い押し下げは断る）。答えるのは
--        契約者か管理者（ほかの承認＝行の承認・お詫びの電話の案の承認・誤判定率の正解とそろえる。計画書 1-4「管理者と契約者の違いは、
--        ログインのID（できる操作の範囲）だけ」）。
--    (d) 録音の確かめの答え（by_kind recording）は外す：録音から作り直した報告が届いたら締め直し、締め直しが保留の版を決め直す（そろえば
--        待つ中身が無くなる＝空の版）。新しく書くのは断る（前の記録の値としては残す）。
--    (e) 取り消しの答え（by_kind revoke）は、その通話から入れた発信禁止がすべて取り消されているときだけ。
--    (f) 確定の印（result_releases）は、まだ決まっていない通話の版・あとの通話の結果がある前の通話の版（断り・怒っていたで終えた版は除く）
--        には付けられない（salescall.release_blocked の1か所。core/approval の確定の入口も読む）。
--    (g) 保留は、その通話の行でだけ・版の番号は順に。やり取りの文字は、お客様が出た通話で、通話後の検証が済む前だけ。

-- ═════════ (a) 保留の版 ═════════
ALTER TABLE salescall.decision_asks DROP CONSTRAINT decision_asks_attempt_id_fkey;
ALTER TABLE salescall.decision_asks DROP CONSTRAINT decision_asks_pkey;
ALTER TABLE salescall.decision_waits DROP CONSTRAINT decision_waits_pkey;
ALTER TABLE salescall.decision_waits ADD COLUMN seq integer NOT NULL DEFAULT 1 CHECK (seq >= 1);
ALTER TABLE salescall.decision_waits ALTER COLUMN seq DROP DEFAULT;
ALTER TABLE salescall.decision_waits ADD PRIMARY KEY (attempt_id, seq);
-- 判定に使った会話の報告の出どころ（original／rebuild:番号。報告が変わったら決め直す＝core/settle の決まった間隔の仕事が読む）
ALTER TABLE salescall.decision_waits ADD COLUMN report_source text;
DO $$
DECLARE c text;
BEGIN
  SELECT conname INTO STRICT c FROM pg_constraint
  WHERE conrelid = 'salescall.decision_waits'::regclass AND contype = 'c' AND pg_get_constraintdef(oid) LIKE '%cardinality(parts)%';
  EXECUTE format('ALTER TABLE salescall.decision_waits DROP CONSTRAINT %I', c);
END $$;
ALTER TABLE salescall.decision_waits ADD CONSTRAINT decision_waits_parts
  CHECK (parts <@ ARRAY['refusal', 'wrong_party', 'complaint'] AND array_position(parts, NULL) IS NULL);   -- 空＝もう待たない

ALTER TABLE salescall.decision_asks ADD COLUMN seq integer NOT NULL DEFAULT 1;
ALTER TABLE salescall.decision_asks ALTER COLUMN seq DROP DEFAULT;
ALTER TABLE salescall.decision_asks ADD PRIMARY KEY (attempt_id, seq);
ALTER TABLE salescall.decision_asks ADD CONSTRAINT decision_asks_wait
  FOREIGN KEY (attempt_id, seq) REFERENCES salescall.decision_waits (attempt_id, seq);
ALTER TABLE salescall.decision_asks ADD CONSTRAINT decision_asks_attempt
  FOREIGN KEY (client_id, attempt_id) REFERENCES salescall.attempts (client_id, attempt_id);
ALTER TABLE salescall.decision_asks DROP CONSTRAINT decision_asks_why_check;
ALTER TABLE salescall.decision_asks ADD CONSTRAINT decision_asks_why
  CHECK (why IN ('recording_missing', 'recording_undecided', 'recording_unchecked'));   -- 届かなかった／作り直してもそろわない／作り直されない

ALTER TABLE salescall.decision_answers ADD COLUMN wait_seq integer;   -- 答えた保留の版（承認と差し戻し。取り消しは無い）
ALTER TABLE salescall.decision_answers ADD CONSTRAINT decision_answers_wait CHECK ((by_kind = 'approval') = (wait_seq IS NOT NULL));

-- ═════════ (b) 今の保留・いまの保留の中身の1か所 ═════════
CREATE FUNCTION salescall.decision_wait_seq(p_attempt uuid) RETURNS integer
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT max(w.seq) FROM salescall.decision_waits w WHERE w.attempt_id = p_attempt
$$;
CREATE FUNCTION salescall.decision_open(p_attempt uuid) RETURNS text[]
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT coalesce((SELECT w.parts FROM salescall.decision_waits w
                   WHERE w.attempt_id = p_attempt AND w.seq = salescall.decision_wait_seq(p_attempt)), '{}')
$$;
GRANT EXECUTE ON FUNCTION salescall.decision_wait_seq(uuid), salescall.decision_open(uuid) TO salescall_app;

CREATE OR REPLACE FUNCTION salescall.decision_pending(p_attempt uuid) RETURNS boolean
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT salescall.unclear_check_pending(p_attempt) OR cardinality(salescall.decision_open(p_attempt)) > 0
$$;

CREATE OR REPLACE FUNCTION salescall.decision_asks_contractor(p_attempt uuid) RETURNS boolean
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT cardinality(salescall.decision_open(p_attempt)) > 0
     AND EXISTS (SELECT 1 FROM salescall.decision_waits w
                 WHERE w.attempt_id = p_attempt AND w.seq = salescall.decision_wait_seq(p_attempt)
                   AND (w.kind = 'approval'
                        OR EXISTS (SELECT 1 FROM salescall.decision_asks k WHERE k.attempt_id = w.attempt_id AND k.seq = w.seq)))
$$;

-- ═════════ (g) 保留の守り ═════════
CREATE FUNCTION salescall.decision_wait_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF coalesce(salescall.cur_actor_kind()::text, '') NOT IN ('system', 'operator', 'admin') THEN
    RAISE EXCEPTION '保留を書くのは、締めの処理だけです' USING ERRCODE = 'insufficient_privilege', CONSTRAINT = 'decision_wait_guard';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM salescall.attempts a WHERE a.attempt_id = NEW.attempt_id AND a.task_id = NEW.task_id) THEN
    RAISE EXCEPTION '保留の行は、その通話の行です' USING ERRCODE = 'check_violation', CONSTRAINT = 'decision_wait_guard';
  END IF;
  IF NEW.seq IS DISTINCT FROM coalesce(salescall.decision_wait_seq(NEW.attempt_id), 0) + 1 THEN
    RAISE EXCEPTION '保留の版は、順に足します' USING ERRCODE = 'check_violation', CONSTRAINT = 'decision_wait_guard';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER decision_wait_guard BEFORE INSERT ON salescall.decision_waits
  FOR EACH ROW EXECUTE FUNCTION salescall.decision_wait_guard();

-- ═════════ (c)(d)(e) 答えの守り ═════════
CREATE OR REPLACE FUNCTION salescall.decision_answer_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.answered_by IS DISTINCT FROM salescall.cur_actor() THEN
    RAISE EXCEPTION '答えた者は、取引の管理者か契約者です' USING ERRCODE = 'check_violation', CONSTRAINT = 'decision_answer_guard';
  END IF;
  IF NEW.by_kind = 'recording' THEN
    RAISE EXCEPTION '録音の確かめは、答えでなく、締め直しが保留の版を決め直して残します（0154）' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'decision_answer_guard';
  END IF;
  IF coalesce(salescall.cur_actor_kind()::text, '') NOT IN ('operator', 'admin') THEN
    RAISE EXCEPTION '取り消せない決定に答えられる（承認・差し戻し・取り消し）のは、管理者か契約者だけです'
      USING ERRCODE = 'insufficient_privilege', CONSTRAINT = 'decision_answer_guard';
  END IF;
  IF NEW.by_kind = 'approval' AND NOT (
       salescall.decision_asks_contractor(NEW.attempt_id)
       AND NEW.wait_seq = salescall.decision_wait_seq(NEW.attempt_id)
       AND NEW.parts <@ salescall.decision_open(NEW.attempt_id) AND salescall.decision_open(NEW.attempt_id) <@ NEW.parts) THEN
    RAISE EXCEPTION 'その通話の、いまの保留（承認か差し戻しを求めている版の中身）に答えてください' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'decision_answer_guard';
  END IF;
  IF NEW.by_kind = 'revoke' AND NOT (
       EXISTS (SELECT 1 FROM salescall.dnc_entries e WHERE e.source_attempt_id = NEW.attempt_id AND e.revoked_at IS NOT NULL)
       AND NOT EXISTS (SELECT 1 FROM salescall.dnc_entries e WHERE e.source_attempt_id = NEW.attempt_id AND e.revoked_at IS NULL)) THEN
    RAISE EXCEPTION '取り消しの答えは、その通話から入れた発信禁止をすべて取り消したときだけです' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'decision_answer_guard';
  END IF;
  RETURN NEW;
END $$;

-- 契約者に確かめてもらう印：締めの処理だけ（契約者の答え・取り消しのあとの締め直しも、同じ締めの処理）・待っている中身がある、いまの
-- 録音の確かめ待ちの版にだけ
CREATE OR REPLACE FUNCTION salescall.decision_ask_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF coalesce(salescall.cur_actor_kind()::text, '') NOT IN ('system', 'operator', 'admin') THEN
    RAISE EXCEPTION '契約者に確かめてもらう印を付けるのは、締めの処理だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'decision_ask_guard';
  END IF;
  IF NOT (NEW.seq = salescall.decision_wait_seq(NEW.attempt_id) AND cardinality(salescall.decision_open(NEW.attempt_id)) > 0
          AND EXISTS (SELECT 1 FROM salescall.decision_waits w WHERE w.attempt_id = NEW.attempt_id AND w.seq = NEW.seq
                      AND w.kind = 'recording')) THEN
    RAISE EXCEPTION 'その通話に、答えを待っている、いまの録音の確かめ待ちの保留がありません' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'decision_ask_guard';
  END IF;
  RETURN NEW;
END $$;

-- ═════════ (f) 確定の印の守り ═════════
-- 確定の印を付けられない版（1か所）：その版の通話の取り消せない決定がまだ決まっていない・あとの通話の結果がある前の通話の版（行の結果は、
-- あとの通話の結果。ただし、断り・怒っていたで終えた版は行の結果にする＝取り消せない決定が確定した）
CREATE FUNCTION salescall.release_blocked(p_task uuid, p_version integer) RETURNS boolean
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT EXISTS (
    SELECT 1 FROM salescall.task_results r JOIN salescall.attempts a ON a.attempt_id = r.attempt_id
    WHERE r.task_id = p_task AND r.version_no = p_version
      AND (salescall.decision_pending(r.attempt_id)
           OR (r.kind IN ('judged', 'rejudged') AND r.business_state <> 'do_not_call' AND NOT r.complaint
               AND EXISTS (SELECT 1 FROM salescall.attempts b JOIN salescall.call_judgements j ON j.attempt_id = b.attempt_id
                           WHERE b.task_id = a.task_id AND b.claimed_at > a.claimed_at AND j.business_state <> 'unknown'))))
$$;
GRANT EXECUTE ON FUNCTION salescall.release_blocked(uuid, integer) TO salescall_app;
CREATE FUNCTION salescall.result_release_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF salescall.release_blocked(NEW.task_id, NEW.version_no) THEN
    RAISE EXCEPTION 'その版には確定の印を付けられません（取り消せない決定がまだ決まっていない・あとの通話の結果がある）'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'result_release_guard';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER result_release_guard BEFORE INSERT ON salescall.result_releases
  FOR EACH ROW EXECUTE FUNCTION salescall.result_release_guard();

-- ═════════ (g) やり取りの文字の守り ═════════
CREATE FUNCTION salescall.call_utterance_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM salescall.attempts a WHERE a.attempt_id = NEW.attempt_id
                 AND salescall.reached_answer(a.attempt_id) AND a.verification IS DISTINCT FROM 'done') THEN
    RAISE EXCEPTION 'やり取りの文字を書けるのは、お客様が出た通話で、通話後の検証が済む前だけです' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'call_utterance_guard';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER call_utterance_guard BEFORE INSERT ON salescall.call_utterances
  FOR EACH ROW EXECUTE FUNCTION salescall.call_utterance_guard();

-- ═════════ 文の直し（見直し24 c） ═════════
-- テスト番号の設定の説明：再開の確かめはアプリの応答だけでは行わない（2026-10-24 のご決定・0147 の作り。前の文は逆を書いていた。画面に出る）
ALTER TABLE salescall.setting_definitions DISABLE TRIGGER setting_definitions_frozen;
UPDATE salescall.setting_definitions
SET meaning = 'テスト番号：回線で電話が鳴るかを試す相手の番号（お客様ではない電話）。管理者がシステムの初めの値として仮の番号を入れ、'
              '契約者が自分の番号を入れたら、その契約者ではそちらを使う。番号のせいか回線のせいかが分からない電話を決める（鳴れば番号のせい・'
              '鳴らなければ回線のせい）のと、止めた回線を再開してよいかの確かめに使う。鳴ったら出る前に切る。番号が無いあいだは、分からない'
              '電話は決めずに待ち、止めた回線は再開しない（どちらも契約者と管理者に1回知らせる。管理者か契約者は、確かめて手で再開できる）'
WHERE key = 'line.test_number';
ALTER TABLE salescall.setting_definitions ENABLE TRIGGER setting_definitions_frozen;
