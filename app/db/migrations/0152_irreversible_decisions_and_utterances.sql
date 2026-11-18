-- 0152 取り消せない決定の確かめ・発信禁止の取り消しで行を戻す・やり取りの文字（2026-10-30 管理者のご決定 (6)(7)。Intent.md 18-3 の
-- 「【2026-10-30】取り消せない決定の確かめ・発信禁止の取り消し」。書いた人：CL-2。DESIGN.md 3-6・7-1・10章・11-1）
--
-- 管理者 (6)「案A　検証モードでは、人が録音を確認し、承認を押した場合だけ営業拒否と確定。人が録音やその場のやり取りを確認できる状態に
-- する必要があります。」 (7)「案A」（誤って入れた発信禁止を取り消したら、その発信禁止で終えた行を予定に戻し、依頼者へ渡す結果の
-- 「営業拒否」も外す）。
--
-- (1) 取り消せない決定（お客様の断り・番号違い・怒っていた＝会話の報告から決まる、発信禁止や行の終わりになるもの）の保留
--     （表 decision_waits。書くのは core/approval の1か所）。保留にするか・どちらの保留かを決めるのも core/approval の1か所
--     （decision_wait）。どの決定が取り消せない決定で、2つ目の判定とそろったかは core/judge.irreversible の1か所：
--     - recording（録音の確かめ待ち）：会話AIの報告と2つ目の判定（意味の判定）がそろわなかった（食い違った・判定が無い＝会話の報告の
--       irreversible_check。データの形 版 1.07。無い・古い報告は「判定が無い」）。通話後の録音の確かめ（2つの耳。録音から作り直した報告）で
--       決まるまで保留。作り直した報告で、その決定が2つ目の判定とそろえば決まりどおり（confirmed）、その決定が報告から消えていれば
--       元に戻す（withdrawn。その報告のまま、ふつうに締める）。
--     - approval（契約者の承認待ち）：検証モードの業務（原則9。モードを読むのは core/approval の1か所）。そろっていても、契約者が録音と
--       その場のやり取りを確かめて承認を押したときだけ確定（confirmed）。差し戻し（断りではない）を押したら、その決定をその通話の
--       報告から外して、ふつうに締める（withdrawn）。
--     保留のあいだは：発信禁止を登録しない・行を終えない（次の動きは判定待ち hold）・結果を確定しない・同じお客様のほかの行も待たせる。
-- (2) 答え（表 decision_answers。書くのは core/approval の1か所）：承認（approval＝契約者だけ）・録音の確かめ（recording＝システムの処理だけ）・
--     発信禁止の取り消し（revoke＝管理者か契約者。(7)。保留が無くても入る＝その場で決めた決定を、依頼者からの正式な求めで取り消した）。
--     withdrawn の答えの中身（parts）は、締めの処理が、その通話の報告から外す（core/settle._judge_input の1か所）。
-- (3) 「まだ決まっていない」は1つ（salescall.decision_pending）：切れ方が分からない通話の確かめのかけ直しを待っている（0150 の
--     unclear_check_pending）か、上の保留の答えがまだ無い。読むのは salescall.refusal_undecided（同じお客様のほかの行を門で待たせる＝門の
--     理由は related_decision_pending の1つ。⛔ 0150 の related_unclear_cut から名前を替えた）・core/approval.after_judged（結果を確定
--     しない）・salescall.recordings_held（決まるまで、その通話の録音を消さない＝確かめられる状態にしておく）。
-- (4) やり取りの文字（表 call_utterances。計画書 11-1。書くのは会話の側から core/conversation_io の1か所）：お客様と AI の発話の文字と時刻
--     （その通話の録音の上の秒）。依頼者ごとに分け、読める範囲は今の決まり（11章）のまま。書き換えない・消さない。

-- ═════════ (4) やり取りの文字 ═════════
CREATE TABLE salescall.call_utterances (
  tenant_id   uuid NOT NULL,
  client_id   uuid NOT NULL,
  attempt_id  uuid NOT NULL,
  seq         integer NOT NULL CHECK (seq >= 1),                      -- その通話の中の順番（書いた順。書く入口が付ける）
  speaker     text NOT NULL CHECK (speaker IN ('callee', 'ai')),     -- お客様／AI（AI の声で言った文・流した録音の文）
  turn        integer NOT NULL CHECK (turn >= 0),                     -- 相手の発話の番号（会話の側の callee_turns。根拠の発話の turn と同じ物差し）
  said        text NOT NULL CHECK (btrim(said) <> ''),                -- 発話の文字（お客様＝会話AIの聞き取り／AI＝言った文）
  at_s        numeric NOT NULL CHECK (at_s >= 0),                     -- その通話の録音の上の位置（秒）
  saved_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (attempt_id, seq),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, attempt_id) REFERENCES salescall.attempts (client_id, attempt_id)
);
CREATE TRIGGER call_utterances_frozen BEFORE UPDATE OR DELETE ON salescall.call_utterances
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
ALTER TABLE salescall.call_utterances ENABLE ROW LEVEL SECURITY;
CREATE POLICY client_scope ON salescall.call_utterances
  USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
  WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client());
GRANT SELECT, INSERT ON salescall.call_utterances TO salescall_app;

-- ═════════ (1) 取り消せない決定の保留 ═════════
CREATE TABLE salescall.decision_waits (
  tenant_id   uuid NOT NULL,
  client_id   uuid NOT NULL,
  attempt_id  uuid PRIMARY KEY,                                       -- その決定が出た通話（1つの通話に保留は1つ）
  task_id     uuid NOT NULL,
  kind        text NOT NULL CHECK (kind IN ('recording', 'approval')),
  parts       text[] NOT NULL CHECK (cardinality(parts) > 0 AND parts <@ ARRAY['refusal', 'wrong_party', 'complaint']),
  checked     text NOT NULL CHECK (checked IN ('agreed', 'disagreed', 'none')),   -- 会話の報告の、2つ目の判定がそろったか
  version_no  integer,                                                -- 保留にした結果の版（お詫びの電話は業務の結果を書かない＝無い）
  waited_at   timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, attempt_id) REFERENCES salescall.attempts (client_id, attempt_id),
  FOREIGN KEY (client_id, task_id) REFERENCES salescall.tasks (client_id, task_id)
);
CREATE INDEX decision_waits_by_task ON salescall.decision_waits (task_id);
CREATE TRIGGER decision_waits_frozen BEFORE UPDATE OR DELETE ON salescall.decision_waits
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
ALTER TABLE salescall.decision_waits ENABLE ROW LEVEL SECURITY;
CREATE POLICY client_scope ON salescall.decision_waits
  USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
  WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client());
GRANT SELECT, INSERT ON salescall.decision_waits TO salescall_app;

-- ═════════ (2) 答え ═════════
CREATE TABLE salescall.decision_answers (
  tenant_id    uuid NOT NULL,
  client_id    uuid NOT NULL,
  answer_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  attempt_id   uuid NOT NULL,
  outcome      text NOT NULL CHECK (outcome IN ('confirmed', 'withdrawn')),
  parts        text[] NOT NULL CHECK (cardinality(parts) > 0 AND parts <@ ARRAY['refusal', 'wrong_party', 'complaint', 'fax']),
  by_kind      text NOT NULL CHECK (by_kind IN ('recording', 'approval', 'revoke')),
  why          text CHECK (why IS NULL OR btrim(why) <> ''),          -- 取り消しの理由（依頼者からの求め）など
  answered_by  text NOT NULL CHECK (btrim(answered_by) <> ''),
  answered_at  timestamptz NOT NULL DEFAULT now(),
  CHECK (by_kind <> 'revoke' OR outcome = 'withdrawn'),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, attempt_id) REFERENCES salescall.attempts (client_id, attempt_id)
);
CREATE INDEX decision_answers_by_attempt ON salescall.decision_answers (attempt_id);
CREATE TRIGGER decision_answers_frozen BEFORE UPDATE OR DELETE ON salescall.decision_answers
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
ALTER TABLE salescall.decision_answers ENABLE ROW LEVEL SECURITY;
CREATE POLICY client_scope ON salescall.decision_answers
  USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
  WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client());
GRANT SELECT, INSERT ON salescall.decision_answers TO salescall_app;

-- 答えるのは、決まった人と処理だけ：承認と差し戻し＝契約者だけ（管理者のご決定「人が録音を確認し、承認を押した場合だけ」の人＝その業務の
-- 契約者）・録音の確かめ＝システムの処理だけ・発信禁止の取り消し＝管理者か契約者（取り消しそのものと同じ。0029）。承認と録音の確かめは、
-- その種類の保留が、まだ答えの無いまま残っているときだけ（1つの保留に答えは1つ）
CREATE FUNCTION salescall.decision_answer_guard() RETURNS trigger LANGUAGE plpgsql AS $$
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
  IF NEW.by_kind IN ('approval', 'recording') AND NOT EXISTS (
       SELECT 1 FROM salescall.decision_waits w WHERE w.attempt_id = NEW.attempt_id AND w.kind = NEW.by_kind
         AND NOT EXISTS (SELECT 1 FROM salescall.decision_answers x WHERE x.attempt_id = NEW.attempt_id)) THEN
    RAISE EXCEPTION 'その通話に、答えを待っている保留（%）がありません', NEW.by_kind USING ERRCODE = 'check_violation',
      CONSTRAINT = 'decision_answer_guard';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER decision_answer_guard BEFORE INSERT ON salescall.decision_answers
  FOR EACH ROW EXECUTE FUNCTION salescall.decision_answer_guard();

-- ═════════ (3) 「まだ決まっていない」の1か所 ═════════
CREATE FUNCTION salescall.decision_pending(p_attempt uuid) RETURNS boolean
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT salescall.unclear_check_pending(p_attempt)
      OR EXISTS (SELECT 1 FROM salescall.decision_waits w WHERE w.attempt_id = p_attempt
                   AND NOT EXISTS (SELECT 1 FROM salescall.decision_answers x WHERE x.attempt_id = p_attempt))
$$;
GRANT EXECUTE ON FUNCTION salescall.decision_pending(uuid) TO salescall_app;

-- 断られたかがまだ決まっていない（0150 の形の、確かめのかけ直しの答え待ちを、まだ決まっていないの1か所に替える）
CREATE OR REPLACE FUNCTION salescall.refusal_undecided(p_attempt uuid) RETURNS boolean
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT NOT salescall.is_settled(p_attempt)
      OR salescall.decision_pending(p_attempt)
      OR (salescall.unknown_pending(p_attempt)
          AND NOT coalesce((SELECT a.cut_off FROM salescall.attempts a WHERE a.attempt_id = p_attempt), false)
          AND NOT coalesce((SELECT (r.detail ->> 'refusal_known')::boolean FROM salescall.task_results r
                            WHERE r.attempt_id = p_attempt ORDER BY r.version_no DESC LIMIT 1), false))
$$;

-- 決まるまで、その通話の録音を消さない（確かめられる状態にしておく。測り終わるまで消さない録音＝0056・0106 の形に足す）
CREATE OR REPLACE FUNCTION salescall.recordings_held() RETURNS SETOF uuid
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
BEGIN
  PERFORM salescall.only_backup_system();
  IF salescall.cur_tenant() IS NULL THEN
    RAISE EXCEPTION '契約者の取引で呼びます' USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN QUERY SELECT DISTINCT x.attempt_id FROM (
    SELECT h.attempt_id FROM salescall.recording_holds h WHERE h.tenant_id = salescall.cur_tenant() AND h.released_at IS NULL
    UNION
    SELECT w.attempt_id FROM salescall.decision_waits w
     WHERE w.tenant_id = salescall.cur_tenant() AND salescall.decision_pending(w.attempt_id)) x ORDER BY 1;
END $$;
REVOKE ALL ON FUNCTION salescall.recordings_held() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.recordings_held() TO salescall_app;
