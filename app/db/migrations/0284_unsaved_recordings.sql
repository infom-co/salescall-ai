-- 0284 業務ごとに、通話の録音とやり取りの文字を「保存しない」を選べる（2026-11-22。書いた人：CL-2。DESIGN_15-2.md の957。計画書 11-4
--      「録音・文字起こしを保存するかは、業務ごとに決める。要らない場合は保存しない形を選べる」の ⏳）
-- ✅ 2026-11-22 管理者（CL-2 の伺い「保存しない業務の通話はどうしますか？」）：「(ア) 抜き取りまで残す」＝保存しない業務も、その週の誤判定率の
--    抜き取りが済むまで（最長およそ2週間）は残し、選ばれなかった通話はそこで消す。選ばれた通話は正解が付くまで残す。検証モードの間は、
--    保存しない設定でも残す（計画書 11-4 第2版：検証モードの期間と、抜き取りの対象は保存を必須）。
-- 作り：
--   (1) 設定 recording.save（真偽。いちばん細かく決められるのは業務。システムの値は「保存する」）。
--   (2) 保存しない業務の通話の印 recording_unsaved：門がかけるとき（予約）に、その業務の設定で付ける（検証の印と同じく、かけたときの設定で
--       決める＝あとで設定を変えても、前の通話は変わらない）。追記だけ。
--   (3) 日数を待たずに消してよい通話の一覧は salescall.recordings_unsaved_done の1か所：印があり、その通話の週か、それより後の週の抜き取りが
--       その業務で済み（抜き取りの記録 sample_draws の週をそのまま使う＝週の決め方を2か所に持たない）、消さない通話（salescall.attempt_kept＝
--       抜き取りで選ばれた・検証モード・まだ決まっていない取り消せない決定）でないもの。
--       読むのは、やり取りの文字を消す salescall.purge_utterances・ハードディスクの片付けの決まり（backup.prune_rules の drop）・これから作る
--       Fly.io の録音の消し方（録音の置き場を作るとき＝CL-1・電話の部品）。
INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, finest_scope, screen_group, help) VALUES
('recording.save', 'override', 'boolean',
 '業務の通話の録音とやり取りの文字を残すか（偽＝その週の誤判定率の抜き取りが済んだら、日数を待たずに消す。抜き取りで選ばれた通話・検証モードの通話・まだ決まっていない決定の通話は残す）',
 'DESIGN.md 3-8・計画書 11-4', 'business', 'retention',
 '通話の録音とお客様とのやり取りの文字を残すかです。残さない形にすると、誤判定率を測るための抜き取り（毎週、前の週の通話から選びます）が済んだ'
 'ところで、選ばれなかった通話の録音とやり取りを、残す日数を待たずに消します（最長でおよそ2週間残ります）。抜き取りで選ばれた通話は、正解を付ける'
 'まで残します。検証モードの業務と、まだ確かめていない断り・番号違いなどの通話は、この設定に関係なく残します。変えると、そのあとにかける電話から'
 '効きます（前にかけた電話は、かけたときの形のままです）。取れた答えや結果は消しません。');
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('recording.save', 'system', 'true', 'decided',
 '計画書 11-4：録音・文字起こしを保存するかは業務ごとに決める（要らない場合は保存しない形を選べる）。初めは残す（いまの形）。2026-11-22 管理者：'
 '保存しない業務も「(ア) 抜き取りまで残す」',
 '依頼者から録音を残さないよう求められたとき（業務ごとに変える）', 'ユーザー');

CREATE TABLE salescall.recording_unsaved (   -- 保存しない業務の通話（門がかけるときに付ける。追記だけ）
  tenant_id   uuid NOT NULL,
  client_id   uuid NOT NULL,
  attempt_id  uuid PRIMARY KEY,
  marked_at   timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (client_id, attempt_id) REFERENCES salescall.attempts (client_id, attempt_id)
);
CREATE TRIGGER recording_unsaved_frozen BEFORE UPDATE OR DELETE ON salescall.recording_unsaved
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
ALTER TABLE salescall.recording_unsaved ENABLE ROW LEVEL SECURITY;
CREATE POLICY client_scope ON salescall.recording_unsaved
  USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
  WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client());
GRANT SELECT, INSERT ON salescall.recording_unsaved TO salescall_app;

CREATE FUNCTION salescall.recordings_unsaved_done() RETURNS SETOF uuid
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
BEGIN
  PERFORM salescall.only_backup_system();
  IF salescall.cur_tenant() IS NULL THEN
    RAISE EXCEPTION '契約者の取引で呼びます' USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN QUERY
    SELECT u.attempt_id FROM salescall.recording_unsaved u
      JOIN salescall.attempts a ON a.attempt_id = u.attempt_id
      JOIN salescall.tasks t ON t.task_id = a.task_id
      JOIN salescall.runs r ON r.run_id = t.run_id
     WHERE u.tenant_id = salescall.cur_tenant()
       AND EXISTS (SELECT 1 FROM salescall.sample_draws d
                    WHERE d.business_id = r.business_id
                      AND salescall.jst(a.claimed_at) < (d.week_start + 7)::timestamp)   -- その通話の週か、それより後の週の抜き取りが済んだ
       AND NOT salescall.attempt_kept(u.attempt_id)
     ORDER BY 1;
END $$;
REVOKE ALL ON FUNCTION salescall.recordings_unsaved_done() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.recordings_unsaved_done() TO salescall_app;

-- やり取りの文字を消す1か所に、保存しない業務の通話（抜き取りが済んだもの）を足す（消すのは終わった通話だけ・消さない通話は消さない＝0159 のまま）
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
    AND a.ended_at IS NOT NULL
    AND (a.ended_at < now() - make_interval(days => p_days)
         OR u.attempt_id IN (SELECT salescall.recordings_unsaved_done()))
    AND NOT salescall.attempt_kept(u.attempt_id);
  GET DIAGNOSTICS n = ROW_COUNT;
  PERFORM set_config('salescall.purging_utterances', '', true);
  RETURN n;
END $$;
