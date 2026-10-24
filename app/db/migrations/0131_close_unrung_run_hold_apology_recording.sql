-- 0131 番号か回線か分からないまま1つの行で止まり続けない・受付の保留を待つ長さ・1日の上限のキーを休ませる長さ・お詫びの電話の文の録音
--      （2026-10-24。書いた人：CL-2）
--
-- (1) 2026-10-24 ユーザー決定：「回線か番号かどちらかわからない場合は、その行に固執するのでなく、1回目から、次の行に進むようにして
--     ください。次の行に進んで、次の行がつながればすぐに回線の問題か番号の問題か判断できますよね。一つの行で止まり続けることがないように
--     してください。」
--     0129 の `unrung_blame` は、同じ回線の次の電話の結果で決める（分からない間は、その行を候補に出さない＝number_unconfirmed）。
--     ⛔ ところが、その回線にかける行がほかに残っていない（一覧の最後の1件）・回線を使わなくした、ときは次の電話が来ないので、行が
--     終わらず、その回の一覧も終わらず、だれにも知らされなかった。
--     ⇒ 回線の「続き」を閉じる印（line_run_closures）を残し、鳴った電話と同じく続きの区切りとして読む（決着は、あとから変わらない）。
--       - no_rows（その回線にかける行が、分からないまま待つ行のほかに残っていない。決めるのは回線ごとの処理の1か所＝supervisor/line.py。
--         夜・時間帯の外のように「今はかけられない」だけの行は残っている行に数える＝閉じない）：続きの前の区切りが鳴った電話なら
--         番号のせい（回線は直前まで元気だった）。前の区切りが閉じた印なら、その印の決め方を引き継ぐ（その間、回線は鳴らせていない）。
--         区切りが無い（その回線は一度も鳴らせていない）なら回線のせい（番号に数えず、回線の異常として知らせる）。
--       - retired（その回線を使わなくした。registry.retire_line）：回線のせい（番号に数えない＝ほかの回線でかけ直して確かめる）。
--     閉じるのは、まだ分からない続き（電話が1つの番号だけで、鳴った電話・閉じた印で閉じていない）があるときだけ（`line_run_pending`）。
-- (2) 設定 call.hold_wait：受付で保留になったときに待つ長さ（業務ごと。初め300秒。CL-1 の連絡帳 4-40 の 7。2026-10-24 ユーザー：
--     「業務ごとに選べる：デフォルト、相手から長く待つ必要がある理由がなければ5分、理由があればそれまで待つ。」）。
-- (3) 設定 stop.ai_quota.rest：1日の上限に当たったキー（のプロジェクト）を休ませる長さ（初め1800秒。errors.ACTIONS の project_quota＝
--     project_rest。⛔ 2026-10-19：その日ずっと使わない形では、2時間ほどで戻ったキーまで止めた＝CL-1 の連絡帳）。
-- (4) お詫びの電話の文を録音にし、契約者が承認するときに聞く（CL-1 の連絡帳 4-40 の 4。ユーザー「OK」）：録音の組の単位に apology を
--     足す（お詫びの案1つに1組。今の録音の組・承認の仕組みをそのまま使う＝2つ目の承認の仕組みを作らない）。部分は2つ：
--     apology_same_day（誤った通話と同じ日に伝える文＝「先ほど」）・apology_later（「先日」）。文はお詫びの案を作るときに決めて案に
--     残し（apology_orders.text_same_day・text_later）、録音の文がそれと違えば組に入れられない。声は誤った通話の声。お詫びの案の承認は、
--     その案の録音の組の承認と同じ取引で行い、承認には組を必ず結ぶ（承認の無い録音・録音の無い承認は無い）。

-- ═════════ (1) ═════════
CREATE TABLE salescall.line_run_closures (
  closure_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id  uuid NOT NULL,
  line_id    uuid NOT NULL,
  closed_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
  reason     text NOT NULL CHECK (reason IN ('no_rows', 'retired')),
  verdict    text NOT NULL CHECK (verdict IN ('number', 'line')),
  FOREIGN KEY (tenant_id, line_id) REFERENCES salescall.lines (tenant_id, line_id)
);
CREATE INDEX line_run_closures_by_line ON salescall.line_run_closures (line_id, closed_at);
ALTER TABLE salescall.line_run_closures ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON salescall.line_run_closures
  USING (tenant_id = salescall.cur_tenant()) WITH CHECK (tenant_id = salescall.cur_tenant());
GRANT SELECT ON salescall.line_run_closures TO salescall_app;   -- 書くのは close_unrung_run だけ（持ち主の権限）
CREATE TRIGGER line_run_closures_frozen BEFORE UPDATE OR DELETE ON salescall.line_run_closures
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();

-- 鳴らなかった電話のせい（0129 を作り直す）：続きの区切りは、鳴った電話と閉じた印
CREATE OR REPLACE FUNCTION salescall.unrung_blame(p_attempt uuid) RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  WITH me AS (
    SELECT a.line_id, a.claimed_at FROM salescall.attempts a
    WHERE a.attempt_id = p_attempt AND a.tenant_id = salescall.cur_tenant()
      AND salescall.is_dialed(a.state) AND a.ended_at IS NOT NULL
      AND NOT salescall.rang(a.state, a.cut_off, a.attempt_id)
  ), lo AS (   -- 前のいちばん近い区切り（鳴った電話か閉じた印。無ければ回線の初め）
    SELECT greatest(
      coalesce((SELECT b.claimed_at FROM salescall.attempts b, me
                WHERE b.line_id = me.line_id AND b.claimed_at < me.claimed_at
                  AND salescall.is_dialed(b.state) AND b.ended_at IS NOT NULL
                  AND salescall.rang(b.state, b.cut_off, b.attempt_id)
                ORDER BY b.claimed_at DESC LIMIT 1), '-infinity'::timestamptz),
      coalesce((SELECT max(c.closed_at) FROM salescall.line_run_closures c, me
                WHERE c.line_id = me.line_id AND c.closed_at < me.claimed_at), '-infinity'::timestamptz)) AS t
  ), hi AS (   -- あとのいちばん近い鳴った電話・閉じた印（どちらも無ければ、続きはまだ閉じていない）
    SELECT (SELECT b.claimed_at FROM salescall.attempts b, me
            WHERE b.line_id = me.line_id AND b.claimed_at > me.claimed_at
              AND salescall.is_dialed(b.state) AND b.ended_at IS NOT NULL
              AND salescall.rang(b.state, b.cut_off, b.attempt_id)
            ORDER BY b.claimed_at LIMIT 1) AS rang_at,
           (SELECT c.closed_at FROM salescall.line_run_closures c, me
            WHERE c.line_id = me.line_id AND c.closed_at > me.claimed_at
            ORDER BY c.closed_at LIMIT 1) AS closed_at
  )
  SELECT CASE WHEN (SELECT count(DISTINCT b.phone_e164) FROM salescall.attempts b, me, lo, hi
                    WHERE b.line_id = me.line_id AND b.claimed_at > lo.t
                      AND b.claimed_at < coalesce(least(hi.rang_at, hi.closed_at), 'infinity'::timestamptz)
                      AND salescall.is_dialed(b.state) AND b.ended_at IS NOT NULL) > 1 THEN 'line'
              WHEN hi.rang_at IS NOT NULL AND (hi.closed_at IS NULL OR hi.rang_at < hi.closed_at) THEN 'number'
              WHEN hi.closed_at IS NOT NULL THEN (SELECT c.verdict FROM salescall.line_run_closures c
                                                  WHERE c.line_id = me.line_id AND c.closed_at = hi.closed_at LIMIT 1)
              ELSE 'pending' END
  FROM me, hi
$$;

-- その回線に、まだ分からない続きがあるか（1か所。閉じる処理と、回線ごとの処理の下調べが読む）：いちばん新しい区切りのあとに、発信まで
-- 進んで終わった電話があり、どれも鳴っておらず、番号が1つだけ（2つ以上なら回線のせいと決まっている）
CREATE FUNCTION salescall.line_run_pending(p_line uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  WITH l AS (SELECT line_id FROM salescall.lines WHERE line_id = p_line AND tenant_id = salescall.cur_tenant()),
       lo AS (SELECT greatest(
                coalesce((SELECT max(b.claimed_at) FROM salescall.attempts b, l
                          WHERE b.line_id = l.line_id AND salescall.is_dialed(b.state) AND b.ended_at IS NOT NULL
                            AND salescall.rang(b.state, b.cut_off, b.attempt_id)), '-infinity'::timestamptz),
                coalesce((SELECT max(c.closed_at) FROM salescall.line_run_closures c, l WHERE c.line_id = l.line_id),
                         '-infinity'::timestamptz)) AS t)
  SELECT coalesce((SELECT count(DISTINCT b.phone_e164) = 1 FROM salescall.attempts b, l, lo
                   WHERE b.line_id = l.line_id AND b.claimed_at > lo.t
                     AND salescall.is_dialed(b.state) AND b.ended_at IS NOT NULL), false)
$$;
REVOKE ALL ON FUNCTION salescall.line_run_pending(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.line_run_pending(uuid) TO salescall_app;

-- まだ分からない続きを閉じる（書くのはここだけ。閉じたら true）。p_reason＝no_rows（呼ぶのは回線ごとの処理だけ。その回線にかける行が
-- 残っていないと確かめてから）・retired（回線を使わなくした）。
CREATE FUNCTION salescall.close_unrung_run(p_line uuid, p_reason text) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
DECLARE last_ring timestamptz; last_close record; v text;
BEGIN
  IF p_reason IS NULL OR p_reason NOT IN ('no_rows', 'retired') THEN
    RAISE EXCEPTION '続きを閉じる理由が違います：%', p_reason USING ERRCODE = 'check_violation';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM salescall.lines WHERE line_id = p_line AND tenant_id = salescall.cur_tenant()) THEN
    RAISE EXCEPTION 'その回線はありません（または、取引の範囲の外）' USING ERRCODE = 'check_violation';
  END IF;
  -- 話している電話がある：続きはまだ終わっていない（かける行が無いのではない）。使わなくした回線は、それでも閉じる（あとで終わる
  -- 電話も、閉じた続きに入る＝回線のせい）
  IF p_reason = 'no_rows' AND EXISTS (SELECT 1 FROM salescall.attempts WHERE line_id = p_line AND salescall.is_active(state)) THEN
    RETURN false;
  END IF;
  IF NOT salescall.line_run_pending(p_line) THEN
    RETURN false;
  END IF;
  IF p_reason = 'retired' THEN
    v := 'line';
  ELSE
    SELECT max(b.claimed_at) INTO last_ring FROM salescall.attempts b
    WHERE b.line_id = p_line AND salescall.is_dialed(b.state) AND b.ended_at IS NOT NULL
      AND salescall.rang(b.state, b.cut_off, b.attempt_id);
    SELECT closed_at, verdict INTO last_close FROM salescall.line_run_closures
    WHERE line_id = p_line ORDER BY closed_at DESC LIMIT 1;
    v := CASE WHEN last_ring IS NOT NULL AND (last_close.closed_at IS NULL OR last_ring > last_close.closed_at) THEN 'number'
              WHEN last_close.closed_at IS NOT NULL THEN last_close.verdict
              ELSE 'line' END;
  END IF;
  INSERT INTO salescall.line_run_closures (tenant_id, line_id, reason, verdict)
  VALUES (salescall.cur_tenant(), p_line, p_reason, v);
  RETURN true;
END $$;
REVOKE ALL ON FUNCTION salescall.close_unrung_run(uuid, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.close_unrung_run(uuid, text) TO salescall_app;

-- ═════════ (2)(3) ═════════
INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, finest_scope) VALUES
('call.hold_wait', 'override', 'seconds', '受付で保留になった（「少々お待ちください」のあと保留の音・無音が続く）とき、黙って待つ長さ（秒）。お客様が長く待つ'
 '理由を言ったとき（「会議が終わるまで10分ほど」など）は、その時まで待つ（決めるのは会話の側）。待っても出なければ、戻る時刻を伺う'
 'かけ直しへ進む', 'DESIGN.md 4-3（持ち主 CL-1）', 'business'),
('stop.ai_quota.rest', 'override', 'seconds', '会話AI・判定のキーが1日の上限に当たったとき、そのプロジェクトのキーを休ませる長さ（秒）。明けたら'
 '問い合わせ直して確かめる（その日ずっと使わない、ではない）', 'DESIGN.md 9-1・12-2（キーを選ぶ処理は CL-1）', 'system');
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('call.hold_wait', 'system', '300', 'decided', '2026-10-24 ユーザー決定（CL-1 の連絡帳 4-40 の 7）：「業務ごとに選べる：デフォルト、相手から長く待つ'
 '必要がある理由がなければ5分、理由があればそれまで待つ。」', '模擬通話と最初の実測で、保留の長さを見て', 'ユーザー'),
('stop.ai_quota.rest', 'system', '1800', 'decided', '2026-10-19 CL-1 の実測（1日の上限の答えが出たキーが2時間ほどで戻った）を受け、'
 '30分休ませてから問い合わせ直す（CL-1 の連絡帳。10分では休み明けのたびに上限のキーへ問い合わせ直し、1回の判定に7.9秒かかった）',
 '本番のキーの上限の記録を見て', 'CL-1');

-- ═════════ (4) ═════════
-- お詫びの案に、伝える文（同じ日・あとの日）を残す（決めるのは core/apology.py の text の1か所。案を作るときに1回）。前の案は空のまま
-- （録音を作れない＝作り直しの案で）
ALTER TABLE salescall.apology_orders
  ADD COLUMN text_same_day text CHECK (text_same_day IS NULL OR btrim(text_same_day) <> ''),
  ADD COLUMN text_later text CHECK (text_later IS NULL OR btrim(text_later) <> ''),
  ADD CONSTRAINT apology_orders_texts CHECK (text_same_day IS NOT NULL AND text_later IS NOT NULL) NOT VALID;

-- 録音の組の単位に apology（お詫びの案1つ）を足す
ALTER TABLE salescall.recording_sets ADD COLUMN order_id bigint;
DO $$
DECLARE c text;
BEGIN   -- 単位の一覧と、単位ごとの持ち物の決まり（名前の無い制約）を作り直す
  FOR c IN SELECT conname FROM pg_constraint WHERE conrelid = 'salescall.recording_sets'::regclass AND contype = 'c'
             AND (pg_get_constraintdef(oid) LIKE '%unit%') LOOP
    EXECUTE format('ALTER TABLE salescall.recording_sets DROP CONSTRAINT %I', c);
  END LOOP;
END $$;
ALTER TABLE salescall.recording_sets
  ADD CONSTRAINT recording_sets_unit CHECK (unit IN ('contractor', 'business', 'apology')),
  ADD CONSTRAINT recording_sets_owner CHECK (CASE unit
    WHEN 'business' THEN client_id IS NOT NULL AND version_id IS NOT NULL AND order_id IS NULL
    WHEN 'apology' THEN client_id IS NOT NULL AND version_id IS NULL AND order_id IS NOT NULL
    ELSE client_id IS NULL AND version_id IS NULL AND order_id IS NULL END),
  ADD CONSTRAINT recording_sets_apology_order FOREIGN KEY (client_id, order_id)
    REFERENCES salescall.apology_orders (client_id, order_id);

-- お詫びの録音（1か所）：声は誤った通話の声。部分は2つで、文は案の文と同じ
CREATE FUNCTION salescall.apology_recording_set_check() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.unit = 'apology' AND NEW.voice_name IS DISTINCT FROM (
       SELECT t.voice_name FROM salescall.apology_orders o JOIN salescall.attempts a ON a.attempt_id = o.source_attempt_id
       JOIN salescall.voice_types t ON t.code = a.voice_type WHERE o.order_id = NEW.order_id AND o.client_id = NEW.client_id) THEN
    RAISE EXCEPTION 'お詫びの録音は、誤った通話と同じ声で作ります' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'apology_recording_voice';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER apology_recording_set_check BEFORE INSERT ON salescall.recording_sets
  FOR EACH ROW EXECUTE FUNCTION salescall.apology_recording_set_check();

CREATE FUNCTION salescall.apology_recording_check() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE o record;
BEGIN
  SELECT x.text_same_day, x.text_later INTO o FROM salescall.recording_sets s
  JOIN salescall.apology_orders x ON x.order_id = s.order_id AND x.client_id = s.client_id
  WHERE s.set_id = NEW.set_id AND s.unit = 'apology';
  IF NOT FOUND THEN
    RETURN NEW;   -- お詫びの録音の組ではない
  END IF;
  IF NEW.part NOT IN ('apology_same_day', 'apology_later')
     OR NEW.spoken_text IS DISTINCT FROM (CASE NEW.part WHEN 'apology_same_day' THEN o.text_same_day ELSE o.text_later END) THEN
    RAISE EXCEPTION 'お詫びの録音は、案の文（同じ日＝apology_same_day・あとの日＝apology_later）と同じ文だけです'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'apology_recording_text';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER apology_recording_check BEFORE INSERT ON salescall.recordings
  FOR EACH ROW EXECUTE FUNCTION salescall.apology_recording_check();

CREATE FUNCTION salescall.apology_recording_approval_check() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM salescall.recording_sets WHERE set_id = NEW.set_id AND unit = 'apology')
     AND (SELECT count(*) FROM salescall.recordings WHERE set_id = NEW.set_id
          AND part IN ('apology_same_day', 'apology_later')) <> 2 THEN
    RAISE EXCEPTION 'お詫びの録音の組は、同じ日とあとの日の2つがそろってから承認します' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'apology_recording_complete';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER apology_recording_approval_check BEFORE INSERT ON salescall.recording_approvals
  FOR EACH ROW EXECUTE FUNCTION salescall.apology_recording_approval_check();

-- お詫びの案の承認は、その案の、承認した録音の組を必ず結ぶ（前の承認は空のまま）
ALTER TABLE salescall.apology_approvals
  ADD COLUMN set_id uuid,
  ADD CONSTRAINT apology_approvals_recorded CHECK (set_id IS NOT NULL) NOT VALID;
CREATE FUNCTION salescall.apology_approval_recording() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM salescall.recording_sets s JOIN salescall.recording_approvals p ON p.set_id = s.set_id
                 WHERE s.set_id = NEW.set_id AND s.unit = 'apology' AND s.order_id = NEW.order_id AND s.client_id = NEW.client_id) THEN
    RAISE EXCEPTION 'お詫びの電話の承認は、その案の録音を聞いて承認した組と一緒にします' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'apology_approval_recording';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER apology_approval_recording BEFORE INSERT ON salescall.apology_approvals
  FOR EACH ROW EXECUTE FUNCTION salescall.apology_approval_recording();

-- いまの組（流す組）は、契約者・依頼者の単位だけ（お詫びの録音は、お詫びの承認が結んだ組を流す）
CREATE OR REPLACE VIEW salescall.current_recording_sets WITH (security_invoker = true) AS
  SELECT DISTINCT ON (s.tenant_id, s.unit, s.voice_name, s.version_id)
         s.set_id, s.tenant_id, s.client_id, s.unit, s.voice_name, s.version_id
  FROM salescall.recording_sets s JOIN salescall.recording_approvals p USING (set_id)
  WHERE salescall.recording_set_fits(s.set_id) AND s.unit IN ('contractor', 'business')
  ORDER BY s.tenant_id, s.unit, s.voice_name, s.version_id, p.approved_at DESC, s.created_at DESC;
