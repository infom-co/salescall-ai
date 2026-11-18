-- 0153 録音の確かめで決められない保留は、契約者に確かめてもらう（2026-10-30 管理者のご決定 (6) の当てはめ。書いた人：CL-2。
-- DESIGN.md 7-1・10章。0152 の作業担当が残した問い (1)）
--
-- 0152 の録音の確かめ待ち（recording）は、通話後の録音の確かめ（録音から作り直した報告）で決まるまで保留にした。ところが、録音の
-- 確かめで決められない場面が2つあり、そのときは、だれにも知らせないまま、そのお客様と行がいつまでも待ち続けた：
--   (a) 録音が届かなかった（通話後の検証が、録音なしで済んだ＝core/attempts.finish_without_recording）。0152 は会話の担当へ「録音から
--       作り直した報告で返して」と修正依頼を出したが、録音が無いので返しようがない。
--   (b) 録音から作り直した報告でも、会話AIの報告と2つ目の判定がそろわない（2つの耳でも決められない）。
-- 管理者のご決定 (6)「人が録音やその場のやり取りを確認できる状態にする必要があります」のとおり、録音で決められないときは、契約者が
-- 録音（あれば）とその場のやり取りの文字（call_utterances）を確かめて、承認か差し戻しを押す（Intent.md 1-4「管理者も契約者も、通話
-- ごと・行ごとの判断に入らない（確認と承認だけをする）」の承認。フルオートの業務でも、この場合だけ）。それまでは今どおり保留
-- （かけない）。
--
-- (1) 契約者に確かめてもらう印（表 decision_asks。書くのは core/approval の1か所＝`ask_contractor`。システムの処理だけ）。
-- (2) 契約者に答えを求めている保留かは、データベースの1か所 salescall.decision_asks_contractor（承認待ち approval か、上の印のある
--     録音の確かめ待ち）。答えの守り（承認と差し戻しは契約者だけ）と、core/approval の承認・差し戻しの入口・承認の一覧が読む。

-- ═════════ (1) 契約者に確かめてもらう印 ═════════
CREATE TABLE salescall.decision_asks (
  tenant_id   uuid NOT NULL,
  client_id   uuid NOT NULL,
  attempt_id  uuid PRIMARY KEY REFERENCES salescall.decision_waits (attempt_id),   -- 録音の確かめ待ちの保留（1つの保留に1つ）
  why         text NOT NULL CHECK (why IN ('recording_missing', 'recording_undecided')),   -- 録音が届かなかった／作り直してもそろわない
  asked_at    timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id)
);
CREATE TRIGGER decision_asks_frozen BEFORE UPDATE OR DELETE ON salescall.decision_asks
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
ALTER TABLE salescall.decision_asks ENABLE ROW LEVEL SECURITY;
CREATE POLICY client_scope ON salescall.decision_asks
  USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
  WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client());
GRANT SELECT, INSERT ON salescall.decision_asks TO salescall_app;

-- 印を付けるのはシステムの処理だけ。答えの無い録音の確かめ待ちにだけ付く
CREATE FUNCTION salescall.decision_ask_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF salescall.cur_actor_kind() IS DISTINCT FROM 'system' THEN
    RAISE EXCEPTION '契約者に確かめてもらう印を付けるのは、システムの処理だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'decision_ask_guard';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM salescall.decision_waits w WHERE w.attempt_id = NEW.attempt_id AND w.kind = 'recording'
                   AND NOT EXISTS (SELECT 1 FROM salescall.decision_answers x WHERE x.attempt_id = w.attempt_id)) THEN
    RAISE EXCEPTION 'その通話に、答えを待っている録音の確かめ待ちの保留がありません' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'decision_ask_guard';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER decision_ask_guard BEFORE INSERT ON salescall.decision_asks
  FOR EACH ROW EXECUTE FUNCTION salescall.decision_ask_guard();

-- ═════════ (2) 契約者に答えを求めている保留の1か所 ═════════
CREATE FUNCTION salescall.decision_asks_contractor(p_attempt uuid) RETURNS boolean
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT EXISTS (SELECT 1 FROM salescall.decision_waits w
                 WHERE w.attempt_id = p_attempt
                   AND NOT EXISTS (SELECT 1 FROM salescall.decision_answers x WHERE x.attempt_id = w.attempt_id)
                   AND (w.kind = 'approval'
                        OR EXISTS (SELECT 1 FROM salescall.decision_asks k WHERE k.attempt_id = w.attempt_id)))
$$;
GRANT EXECUTE ON FUNCTION salescall.decision_asks_contractor(uuid) TO salescall_app;

-- 答えの守り（0152）：承認と差し戻し（approval）は、契約者に答えを求めている保留にだけ。録音の確かめ（recording）は、録音の確かめ
-- 待ちの保留にだけ（契約者に求めたあとでも、録音から作り直した報告で先に決まれば、それが答え）
CREATE OR REPLACE FUNCTION salescall.decision_answer_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.answered_by IS DISTINCT FROM salescall.cur_actor() THEN
    RAISE EXCEPTION '答えた人は、取引の人です' USING ERRCODE = 'check_violation', CONSTRAINT = 'decision_answer_guard';
  END IF;
  IF (NEW.by_kind = 'approval' AND salescall.cur_actor_kind() IS DISTINCT FROM 'operator')
     OR (NEW.by_kind = 'recording' AND salescall.cur_actor_kind() IS DISTINCT FROM 'system')
     OR (NEW.by_kind = 'revoke' AND salescall.cur_actor_kind() NOT IN ('operator', 'admin')) THEN
    RAISE EXCEPTION '取り消せない決定に答えられるのは、承認は契約者・録音の確かめはシステムの処理・取り消しは管理者か契約者だけです'
      USING ERRCODE = 'insufficient_privilege', CONSTRAINT = 'decision_answer_guard';
  END IF;
  IF (NEW.by_kind = 'approval' AND NOT salescall.decision_asks_contractor(NEW.attempt_id))
     OR (NEW.by_kind = 'recording' AND NOT EXISTS (
       SELECT 1 FROM salescall.decision_waits w WHERE w.attempt_id = NEW.attempt_id AND w.kind = 'recording'
         AND NOT EXISTS (SELECT 1 FROM salescall.decision_answers x WHERE x.attempt_id = NEW.attempt_id))) THEN
    RAISE EXCEPTION 'その通話に、答えを待っている保留（%）がありません', NEW.by_kind USING ERRCODE = 'check_violation',
      CONSTRAINT = 'decision_answer_guard';
  END IF;
  RETURN NEW;
END $$;
