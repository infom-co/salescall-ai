-- 0157 やり取りの文字を、録音と同じ決まりで消す（書いた人：CL-2。計画書 11-4：「録音・文字起こしを保存する場合は、保存期間と期限後の
-- 削除を設定で持つ」。DESIGN.md 3-8）
--
-- やり取りの文字（表 call_utterances。0152）は、書き換えも消しもできない形で、いつまでも残った（計画書 11-4 と食い違い）。録音と同じく、
-- Fly.io に残す日数（契約者ごとの設定 recording.keep_days_cloud。初め7日＝2026-10-17 ユーザー：「録音の残し方は、Fly.io 7日分、HDに2か月分
-- （両方、状況に応じて変更できるようにする：契約者ごと）」）を過ぎた通話の分を消す。ハードディスクの側は、控え（AI用）の残す日数の中に
-- 入る（控えの決まりのまま）。
-- 消さないのは、録音の「消さない」と同じ1か所（salescall.recordings_held＝検証中・誤判定率の抜き取りの「測り終わるまで消さない」と、
-- まだ決まっていない取り消せない決定＝契約者が録音とやり取りを確かめるまで。2026-10-30 管理者のご決定 (6)：「人が録音やその場の
-- やり取りを確認できる状態にする必要があります」）。
-- (1) 消すのは、システムの処理だけ（入口 salescall.purge_utterances。見守り役の1日1回の仕事が、契約者の取引で日数を渡して呼ぶ）。
-- (2) 守り：書き換えは今までどおり断る。消すのは (1) の中からだけ、かつ、消さない通話の分は消せない（入口の決まりを守りでも確かめる）。

-- ═════════ (2) 守り ═════════
DROP TRIGGER call_utterances_frozen ON salescall.call_utterances;
CREATE FUNCTION salescall.call_utterances_kept() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'UPDATE' THEN
    RAISE EXCEPTION 'やり取りの文字は書き換えられません' USING ERRCODE = 'check_violation', CONSTRAINT = 'call_utterances_kept';
  END IF;
  IF current_setting('salescall.purging_utterances', true) IS DISTINCT FROM 'on' THEN
    RAISE EXCEPTION 'やり取りの文字を消すのは、残す日数を過ぎた分を消す処理だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'call_utterances_kept';
  END IF;
  IF EXISTS (SELECT 1 FROM salescall.recording_holds h WHERE h.attempt_id = OLD.attempt_id AND h.released_at IS NULL)
     OR salescall.decision_pending(OLD.attempt_id) THEN
    RAISE EXCEPTION 'その通話のやり取りの文字は、まだ消せません（測り終わっていない・まだ決まっていない）' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'call_utterances_kept';
  END IF;
  RETURN OLD;
END $$;
CREATE TRIGGER call_utterances_kept BEFORE UPDATE OR DELETE ON salescall.call_utterances
  FOR EACH ROW EXECUTE FUNCTION salescall.call_utterances_kept();

-- ═════════ (1) 消す入口 ═════════
CREATE FUNCTION salescall.purge_utterances(p_days integer) RETURNS integer
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
    AND coalesce(a.ended_at, a.claimed_at) < now() - make_interval(days => p_days)
    AND u.attempt_id NOT IN (SELECT salescall.recordings_held());
  GET DIAGNOSTICS n = ROW_COUNT;
  PERFORM set_config('salescall.purging_utterances', '', true);
  RETURN n;
END $$;
REVOKE ALL ON FUNCTION salescall.purge_utterances(integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.purge_utterances(integer) TO salescall_app;
