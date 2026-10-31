-- 0159 見直し25（2026-10-31 ユーザー：「ここまでの実装とmainに入れる内容で、食い違いや不備、1本化漏れはないですか？」）で見つけた穴を
-- 直す。書いた人：CL-2。DESIGN.md 9-1・Q58・Q59・15-2 の379〜。
--
-- (1) 解いたあと、どこから数え直すか（1か所＝salescall.errors_counted_since）：前は「解いた時刻」から数えた。⛔ 止まったAIを書く取引
--     （起きた時刻＝その取引の始め）が、回復の確かめが停止を解くのと同時に走ると、解いた時刻より前の出来事として数えられず、止まったAIが
--     消えた（見直し25a の C）。⇒ 確かめで解いた停止は、その確かめを始めた時刻から数える（解いた記録の counted_from。それより前の出来事は、
--     あとで通った確かめが「回復した」と示している）。人が解いた停止は、今までどおり解いた時刻から。確かめの側も、確かめを始めた時刻より
--     あとに止める数に届いた出来事があれば、解かない（core/watch.record_check。見えない出来事は、あとで数えて止め直す＝どちらの順でも漏れない）。
--     数え直しの起点を自分で書いていた3か所（consecutive_errors・緊急の道を出せるか・停止の説明 ai_troubles.for_stop）は、これを読む。
-- (2) 緊急の道で再開できる停止か（1か所＝salescall.emergency_stop）：前は Python（stops.emergency_possible）・守り
--     （stop_emergency_pass_guard）・判定の道（emergency_route）の3か所に同じ条件を書いていた（見直し25b の 4）。
-- (3) 消さない通話（1か所＝salescall.attempt_kept）：前は、録音の片付け（recordings_held）が「保留の表にある、まだ決まっていない通話」
--     だけを返し、やり取りの文字の守りは「まだ決まっていない通話」を自分で書いていた。⛔ 切れ方の確かめのかけ直しを待つ通話（保留の表に
--     無い）は、録音の片付けで消され、やり取りの文字を消す仕事はその1件で契約者ごと毎日失敗した（見直し25a の D・25b の 5）。
--     ⇒ 録音の片付け・やり取りの文字を消す入口・その守りの3つが、同じ関数を読む。
-- (4) やり取りの文字を消すのは、終わった通話だけ（前は、終わりの時刻の無い通話を予約の時刻で数えて消せた＝見直し25a の E）。
-- (5) 契約者の設定「判定に使うAI」（judge.models）の説明を、決まっていない段ごとの顔ぶれを書かない形にする（見直し25c の B1：0156 の説明は、
--     まだ作りに入っていない 10-31 の顔ぶれ・Flash Lite の使い方を、全部の段の決まりとして書いていた）。契約者が選ぶのに要る、組の違いと
--     お客様の言葉の送り先だけを書き、段ごとの顔ぶれは設計書 6-10 の (6)（CL-1 の章）を見る。

-- ═════════ (1) 数え直しの起点 ═════════
ALTER TABLE salescall.stop_events ADD COLUMN counted_from timestamptz;
ALTER TABLE salescall.stop_events ADD CONSTRAINT stop_events_counted_from
  CHECK (counted_from IS NULL OR (action = 'resume' AND actor_kind = 'system' AND counted_from <= at));
-- まだ解かれていない停止の見方（0029）は、足した列も同じ形で返す（停止の記録と同じ形を返す関数 open_stops などが読む）
CREATE OR REPLACE VIEW salescall.open_stop_events WITH (security_invoker = true) AS
  SELECT s.* FROM salescall.stop_events s
  WHERE s.action = 'stop' AND NOT EXISTS (SELECT 1 FROM salescall.stop_events r WHERE r.resolves_event_id = s.event_id);
ALTER TABLE salescall.stop_checks ADD COLUMN begun_at timestamptz;   -- 確かめを始めた時刻（この時刻より前の出来事は、確かめが通れば数えない）
ALTER TABLE salescall.stop_checks ADD CONSTRAINT stop_checks_begun_at CHECK (begun_at IS NULL OR begun_at <= checked_at);

-- その契約者で、その原因の停止を前に解いたあと、どこから数えるか（p_before より前の最後の解き。無ければ -infinity）。契約者の取引で読む
CREATE FUNCTION salescall.errors_counted_since(p_cause text, p_before timestamptz DEFAULT 'infinity') RETURNS timestamptz
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT coalesce(max(coalesce(r.counted_from, r.at)), '-infinity'::timestamptz)
  FROM salescall.stop_events r JOIN salescall.stop_events x ON x.event_id = r.resolves_event_id
  WHERE x.cause::text = p_cause AND x.scope = 'tenant' AND x.tenant_id = salescall.cur_tenant() AND r.at <= p_before
$$;
GRANT EXECUTE ON FUNCTION salescall.errors_counted_since(text, timestamptz) TO salescall_app;

-- 続いたエラーの数（p_from＝それより前は数えない。確かめを始めた時刻からの数＝core/watch.record_check）
DROP FUNCTION salescall.consecutive_errors(text[], text, text);
CREATE FUNCTION salescall.consecutive_errors(p_kinds text[], p_cause text, p_evidence text, p_from timestamptz DEFAULT NULL)
RETURNS integer
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  WITH since AS (
    SELECT greatest(
      CASE WHEN p_evidence = 'in_conversation' THEN
        (SELECT coalesce(max(h.at), '-infinity'::timestamptz)
         FROM salescall.attempt_state_history h JOIN salescall.attempts a ON a.attempt_id = h.attempt_id
         WHERE h.to_state = 'in_conversation' AND a.tenant_id = salescall.cur_tenant())
      ELSE '-infinity'::timestamptz END,
      salescall.errors_counted_since(p_cause),
      coalesce(p_from, '-infinity'::timestamptz)) AS t
  )
  SELECT CASE WHEN salescall.cur_tenant() IS NULL OR p_evidence IS NULL OR p_evidence NOT IN ('in_conversation', 'none') THEN NULL
         ELSE (SELECT count(*)::integer FROM salescall.error_events e CROSS JOIN since
               WHERE e.kind = ANY(p_kinds) AND e.occurred_at > since.t AND e.tenant_id = salescall.cur_tenant()) END
$$;
REVOKE ALL ON FUNCTION salescall.consecutive_errors(text[], text, text, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.consecutive_errors(text[], text, text, timestamptz) TO salescall_app;

-- ═════════ (2) 緊急の道で再開できる停止か ═════════
-- まだ解かれていない・システムが止めた・この契約者の AIの不具合の停止。緊急の道で補えないAIが止まっていないかは、止める数の設定を読む
-- core/watch.ai_blocker の1か所（入口 stops.emergency_possible が読む。同時に出すのと止まるのとは、取引の直列の守りがぶつけてやり直す）
CREATE FUNCTION salescall.emergency_stop(p_event bigint) RETURNS boolean
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT EXISTS (SELECT 1 FROM salescall.open_stop_events s
                 WHERE s.event_id = p_event AND s.cause = 'ai_connection' AND s.scope = 'tenant' AND s.actor_kind = 'system'
                   AND s.tenant_id = salescall.cur_tenant())
$$;
GRANT EXECUTE ON FUNCTION salescall.emergency_stop(bigint) TO salescall_app;

CREATE OR REPLACE FUNCTION salescall.stop_emergency_pass_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF salescall.cur_actor_kind() NOT IN ('operator', 'admin') OR NEW.passed_by IS DISTINCT FROM salescall.cur_actor()
     OR NEW.actor_kind::text IS DISTINCT FROM salescall.cur_actor_kind()::text THEN
    RAISE EXCEPTION '緊急の道で再開できるのは、契約者か管理者だけです（出した人は取引の人）' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'stop_emergency_pass_guard';
  END IF;
  IF NEW.tenant_id IS DISTINCT FROM salescall.cur_tenant() OR NOT salescall.emergency_stop(NEW.stop_event_id) THEN
    RAISE EXCEPTION '緊急の道で再開できるのは、まだ解かれていない、システムが止めた、その契約者の AIの不具合の停止だけです'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'stop_emergency_pass_guard';
  END IF;
  RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION salescall.emergency_route() RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT EXISTS (SELECT 1 FROM salescall.stop_emergency_passes p
                 WHERE p.tenant_id = salescall.cur_tenant() AND salescall.emergency_stop(p.stop_event_id))
$$;

-- ═════════ (3) 消さない通話 ═════════
-- 測り終わるまで消さない録音の印がある（検証中・誤判定率の抜き取り）か、取り消せない決定がまだ決まっていない（salescall.decision_pending＝
-- 保留の答え待ち・切れ方の確かめのかけ直し待ち。2026-10-30 管理者のご決定 (6)：「人が録音やその場のやり取りを確認できる状態にする」）
CREATE FUNCTION salescall.attempt_kept(p_attempt uuid) RETURNS boolean
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT EXISTS (SELECT 1 FROM salescall.recording_holds h WHERE h.attempt_id = p_attempt AND h.released_at IS NULL)
      OR salescall.decision_pending(p_attempt)
$$;
GRANT EXECUTE ON FUNCTION salescall.attempt_kept(uuid) TO salescall_app;

-- 録音の片付け（PC へ渡す消さない通話）：消さない通話になりうる通話（印・保留・切れ方の確かめ）のうち、いま消さないもの
CREATE OR REPLACE FUNCTION salescall.recordings_held() RETURNS SETOF uuid
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
BEGIN
  PERFORM salescall.only_backup_system();
  IF salescall.cur_tenant() IS NULL THEN
    RAISE EXCEPTION '契約者の取引で呼びます' USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN QUERY SELECT x.attempt_id FROM (
    SELECT h.attempt_id FROM salescall.recording_holds h WHERE h.tenant_id = salescall.cur_tenant()
    UNION SELECT w.attempt_id FROM salescall.decision_waits w WHERE w.tenant_id = salescall.cur_tenant()
    UNION SELECT u.attempt_id FROM salescall.unclear_checks u WHERE u.tenant_id = salescall.cur_tenant()) x
  WHERE salescall.attempt_kept(x.attempt_id) ORDER BY 1;
END $$;

CREATE OR REPLACE FUNCTION salescall.call_utterances_kept() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'UPDATE' THEN
    RAISE EXCEPTION 'やり取りの文字は書き換えられません' USING ERRCODE = 'check_violation', CONSTRAINT = 'call_utterances_kept';
  END IF;
  IF current_setting('salescall.purging_utterances', true) IS DISTINCT FROM 'on' THEN
    RAISE EXCEPTION 'やり取りの文字を消すのは、残す日数を過ぎた分を消す処理だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'call_utterances_kept';
  END IF;
  IF salescall.attempt_kept(OLD.attempt_id) THEN
    RAISE EXCEPTION 'その通話のやり取りの文字は、まだ消せません（測り終わっていない・まだ決まっていない）' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'call_utterances_kept';
  END IF;
  RETURN OLD;
END $$;

-- ═════════ (4) やり取りの文字を消すのは、終わった通話だけ ═════════
CREATE OR REPLACE FUNCTION salescall.purge_utterances(p_days integer) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
DECLARE n integer;
BEGIN
  IF salescall.cur_actor_kind() IS DISTINCT FROM 'system' OR salescall.cur_tenant() IS NULL THEN
    RAISE EXCEPTION 'やり取りの文字を消すのは、契約者の取引のシステムの処理だけです' USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF p_days IS NULL OR p_days < 1 THEN
    RAISE EXCEPTION '残す日数は1日から上です（%）', p_days USING ERRCODE = 'check_violation';
  END IF;
  PERFORM set_config('salescall.purging_utterances', 'on', true);
  DELETE FROM salescall.call_utterances u USING salescall.attempts a
  WHERE a.attempt_id = u.attempt_id AND u.tenant_id = salescall.cur_tenant()
    AND a.ended_at < now() - make_interval(days => p_days)
    AND NOT salescall.attempt_kept(u.attempt_id);
  GET DIAGNOSTICS n = ROW_COUNT;
  PERFORM set_config('salescall.purging_utterances', '', true);
  RETURN n;
END $$;

-- ═════════ (5) 判定に使うAIの説明 ═════════
ALTER TABLE salescall.setting_definitions DISABLE TRIGGER setting_definitions_frozen;
UPDATE salescall.setting_definitions
SET meaning = '意味の判定に使うAI。jev_and_sonnet（初めの値）＝先に安い判定（d1-ultra（Liquid）・Jev 1.14。お呼びする段では、その場の'
              '判定 3.5 Flash Lite も）を出し、確かでなければ Claude Sonnet 5.5 を出す／sonnet＝Claude Sonnet 5.5 で判定する（d1-ultra・Jev は、'
              '止まった判定の代わりのほかは出さない）。段ごとの顔ぶれは設計書 6-10 の (6)。どちらでも、Anthropic へ直接送れないときだけ、同じ'
              ' Sonnet を緊急の道（OpenRouter を通して Google が預かる Claude）へ送る。お客様の言葉は、Anthropic（米国）・Google（米国）に'
              '加えて、jev_and_sonnet では TypeSafe と Liquid（米国。OpenRouter 経由）にも送ります。どの組で呼ぶかを決めて呼ぶのは会話（CL-1）'
WHERE key = 'judge.models';
ALTER TABLE salescall.setting_definitions ENABLE TRIGGER setting_definitions_frozen;
