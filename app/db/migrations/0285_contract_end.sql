-- 0285 依頼者との契約が終わったときのデータ（2026-11-23。書いた人：CL-2。DESIGN_15-2.md の960。計画書 11-4「依頼者との契約が終わったときの、
--      データの返却・削除の手順を決める」の ⏳）
-- ✅ 2026-11-22 管理者：「ハードディスクに残す。消すのはハードディスクで消すだけ。ただし、絶対に不要なデータは、イ（画面で消す）もできるように
--    しておく。」・サーバーは「(ア) HDに写してサーバーから外す」。
-- 流れ：(1) 管理者か契約者が「契約終了」を押す（salescall.client_contract_ends。新しい発信は client_states の使わない印で止まる＝core/contract_end）。
--   (2) 猶予（設定 contract_end.forget_after。7日＝✅ 2026-11-23 管理者）のあいだは取り消せる（client_contract_end_cancels）。(3) 猶予が過ぎ、契約終了のあとに取った
--   AI用の控えが戻せることまで確かめてあり、提出用のファイルがすべてハードディスクへ写してあれば、サーバーから外す（salescall.forget_client の1か所。
--   見守り役の仕事 contract_ends）。外したことは client_forgets に残る。外した依頼者は「使う」に戻せない。ハードディスクの控えは日数で消さない
--   （backup.prune_rules）。
-- 残すもの（ほかの依頼者・契約者の判断に使う＝外すとお客様に害が出る。2026-11-22 の洗い出し）：salescall.forget_kept_tables の1か所。
--   番号ごとにかけた事実（attempts・attempt_state_history：同じ番号への間隔・ほかの契約者の「ずっとかけない」・同じ発信番号でかけない・同じ声を
--   避ける・回線の1日の本数・出てもらえた割合）と、それが指すつながりの約束の親（tasks・task_numbers・contacts・runs・businesses・business_versions）、
--   断られた番号（dnc_entries・dnc_imports）、回線（lines）、依頼者（clients・client_states）、控えのフォルダと記録（client_backup_folders・backup_runs
--   ＝名前の使い回しで控えを上書き・消さない）、契約終了の記録。tasks の中のお客様の中身（名前・読み・役職・部署・依頼者の管理番号・一覧の行）は
--   空にする。音の置き場（recording_audio）は、すべての契約者で使うシステムの名前の録音の音だけ残す。
-- 外すもの：依頼者の欄を持つ表のうち、残す表でないものすべて（表の一覧から求める＝表が増えても外し漏れない。どちらにするかは試験の控えの一覧が
--   見張る）と、依頼者の欄を持たない子の表（知らせを送った記録・停止の確かめ・停止の緊急の道）。消す順は決め打ちにしない（つながりの約束に当たった
--   表は、次の回に回す）。
-- 消してよいかの判断は salescall.forgetting の1か所：外す処理（salescall.forget_client）が付けた印の依頼者の行だけ・残す表は消さない・消すだけ
--   （書き換えは通さない）。「消せない」守り（forbid_change）と、独自の守り5つが、これを呼ぶ。アプリの接続は、そもそも消す権限を持たない。

INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, finest_scope, screen_group, help) VALUES
('contract_end.forget_after', 'override', 'seconds',
 '契約終了を押してから、サーバーからその依頼者のデータを外すまでの猶予（このあいだは取り消せる）',
 'DESIGN.md 3-10・計画書 11-4', 'system', 'retention',
 '契約終了を押してから、サーバーからその依頼者のデータを外すまでの長さです。このあいだは契約終了を取り消せます。外す前に、ハードディスクへ'
 '最後の控えを取り、戻せることを確かめます。外したあとも、ハードディスクの控えは残ります。');
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('contract_end.forget_after', 'system', '604800', 'decided',
 '✅ 2026-11-23 管理者（CL-2 の伺い「猶予は何日にしますか？」）：「7日（おすすめ）」＝誤って契約終了を押したとき、外す前に気づいて取り消せる長さ',
 '依頼者が戻ってくることが多いと分かったとき', 'ユーザー');

CREATE TABLE salescall.client_contract_ends (       -- 契約終了（押したとき。追記だけ）
  end_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id  uuid NOT NULL,
  client_id  uuid NOT NULL,
  ended_at   timestamptz NOT NULL DEFAULT now(),
  ended_by   text NOT NULL DEFAULT salescall.cur_actor() CHECK (btrim(ended_by) <> ''),
  reason     text NOT NULL CHECK (btrim(reason) <> ''),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  UNIQUE (client_id, end_id)
);
CREATE TABLE salescall.client_contract_end_cancels (   -- 契約終了の取り消し（外す前だけ。追記だけ）
  end_id       bigint PRIMARY KEY REFERENCES salescall.client_contract_ends,
  tenant_id    uuid NOT NULL,
  client_id    uuid NOT NULL,
  cancelled_at timestamptz NOT NULL DEFAULT now(),
  cancelled_by text NOT NULL DEFAULT salescall.cur_actor() CHECK (btrim(cancelled_by) <> ''),
  reason       text NOT NULL CHECK (btrim(reason) <> ''),
  FOREIGN KEY (client_id, end_id) REFERENCES salescall.client_contract_ends (client_id, end_id)
);
CREATE TABLE salescall.client_forgets (              -- サーバーから外した（1回だけ）
  client_id   uuid PRIMARY KEY,
  tenant_id   uuid NOT NULL,
  end_id      bigint NOT NULL,
  backup_run  bigint NOT NULL,                       -- 外す前に戻せることまで確かめた AI用の控え（backup_runs）
  removed     jsonb NOT NULL,                        -- 表ごとに外した行の数
  forgot_at   timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, end_id) REFERENCES salescall.client_contract_ends (client_id, end_id)
);
DO $$ DECLARE t text; BEGIN
  FOREACH t IN ARRAY ARRAY['client_contract_ends', 'client_contract_end_cancels', 'client_forgets'] LOOP
    EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON salescall.%I FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change()',
                   t || '_frozen', t);
    EXECUTE format('ALTER TABLE salescall.%I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY client_scope ON salescall.%I
                      USING (tenant_id = salescall.cur_tenant() AND (salescall.cur_client() IS NULL OR client_id = salescall.cur_client()))
                      WITH CHECK (tenant_id = salescall.cur_tenant() AND (salescall.cur_client() IS NULL OR client_id = salescall.cur_client()))', t);
    EXECUTE format('GRANT SELECT ON salescall.%I TO salescall_app', t);
  END LOOP;
END $$;
GRANT INSERT ON salescall.client_contract_ends, salescall.client_contract_end_cancels TO salescall_app;

-- 押す・取り消すのは管理者か契約者（いま操作している方）だけ。取り消せるのは、外す前・まだ取り消していない最新の契約終了だけ。
CREATE FUNCTION salescall.contract_end_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT salescall.cur_is_person() THEN
    RAISE EXCEPTION '契約終了を押す・取り消すのは、管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'contract_end_by_person';
  END IF;
  IF EXISTS (SELECT 1 FROM salescall.client_forgets f WHERE f.client_id = NEW.client_id) THEN
    RAISE EXCEPTION 'サーバーから外した依頼者です（契約終了は取り消せません）' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'contract_end_forgotten';
  END IF;
  IF TG_TABLE_NAME = 'client_contract_ends' AND salescall.contract_ended(NEW.client_id) IS NOT NULL THEN
    RAISE EXCEPTION 'もう契約終了になっています' USING ERRCODE = 'check_violation', CONSTRAINT = 'contract_end_once';
  END IF;
  IF TG_TABLE_NAME = 'client_contract_end_cancels' AND salescall.contract_ended(NEW.client_id) IS DISTINCT FROM NEW.end_id THEN
    RAISE EXCEPTION '取り消せるのは、いまの契約終了だけです' USING ERRCODE = 'check_violation', CONSTRAINT = 'contract_end_cancel_current';
  END IF;
  RETURN NEW;
END $$;
-- いまの契約終了（取り消していない最新の契約終了の番号。無ければ NULL）。決めるのはこの1か所
CREATE FUNCTION salescall.contract_ended(p_client uuid) RETURNS bigint LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = salescall, pg_temp AS $$
  SELECT e.end_id FROM salescall.client_contract_ends e
   WHERE e.client_id = p_client AND e.tenant_id = salescall.cur_tenant()
     AND NOT EXISTS (SELECT 1 FROM salescall.client_contract_end_cancels c WHERE c.end_id = e.end_id)
   ORDER BY e.end_id DESC LIMIT 1
$$;
REVOKE ALL ON FUNCTION salescall.contract_ended(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.contract_ended(uuid) TO salescall_app;
CREATE TRIGGER client_contract_end_guard BEFORE INSERT ON salescall.client_contract_ends
  FOR EACH ROW EXECUTE FUNCTION salescall.contract_end_guard();
CREATE TRIGGER client_contract_end_cancel_guard BEFORE INSERT ON salescall.client_contract_end_cancels
  FOR EACH ROW EXECUTE FUNCTION salescall.contract_end_guard();

-- 契約終了のあいだ・外した依頼者は「使う」に戻せない（新しい発信・取込をさせない。契約終了のあいだは、取り消してから戻す＝core/contract_end）
CREATE FUNCTION salescall.client_state_not_forgotten() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.active AND EXISTS (SELECT 1 FROM salescall.client_forgets f WHERE f.client_id = NEW.client_id) THEN
    RAISE EXCEPTION 'サーバーから外した依頼者は、使う形に戻せません（新しい依頼者として作ってください）' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'client_forgotten_stays_off';
  END IF;
  IF NEW.active AND salescall.contract_ended(NEW.client_id) IS NOT NULL THEN
    RAISE EXCEPTION '契約終了になっている依頼者です（使う形に戻すには、契約終了を取り消してください）' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'client_ended_stays_off';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER client_state_not_forgotten BEFORE INSERT ON salescall.client_states
  FOR EACH ROW EXECUTE FUNCTION salescall.client_state_not_forgotten();

-- 残す表（1か所）
CREATE FUNCTION salescall.forget_kept_tables() RETURNS text[] LANGUAGE sql IMMUTABLE AS $$
  SELECT ARRAY['attempts', 'attempt_state_history', 'dnc_entries', 'dnc_imports', 'lines', 'clients', 'client_states',
               'client_backup_folders', 'backup_runs', 'task_numbers', 'tasks', 'contacts', 'runs', 'businesses', 'business_versions',
               'recording_audio', 'client_contract_ends', 'client_contract_end_cancels', 'client_forgets']
$$;
GRANT EXECUTE ON FUNCTION salescall.forget_kept_tables() TO salescall_app;

-- 消してよいか（1か所）：外す処理の印の依頼者の行だけ。残す表は消さない（音の置き場は、外す処理が残す音を除いて消す）
CREATE FUNCTION salescall.forgetting(p_row jsonb, p_table text) RETURNS boolean LANGUAGE plpgsql STABLE
SET search_path = salescall, pg_temp AS $$
DECLARE f text := nullif(current_setting('salescall.forgetting_client', true), '');
BEGIN
  IF f IS NULL OR (p_table = ANY (salescall.forget_kept_tables()) AND p_table <> 'recording_audio') THEN
    RETURN false;
  END IF;
  IF p_row ? 'client_id' THEN
    RETURN p_row->>'client_id' = f;
  END IF;
  IF p_table = 'notification_deliveries' THEN
    RETURN EXISTS (SELECT 1 FROM salescall.notification_events e WHERE e.event_id = (p_row->>'event_id')::bigint AND e.client_id::text = f);
  END IF;
  IF p_table IN ('stop_checks', 'stop_emergency_passes') THEN
    RETURN EXISTS (SELECT 1 FROM salescall.stop_events e WHERE e.event_id = (p_row->>'stop_event_id')::bigint AND e.client_id::text = f);
  END IF;
  RETURN false;
END $$;

CREATE OR REPLACE FUNCTION salescall.forbid_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' AND salescall.forgetting(to_jsonb(OLD), TG_TABLE_NAME) THEN
    RETURN OLD;   -- 契約が終わった依頼者をサーバーから外す処理だけ（0285）
  END IF;
  RAISE EXCEPTION '変えてはいけない記録です（%）。書き換え・消しはできません', TG_TABLE_NAME
    USING ERRCODE = 'integrity_constraint_violation';
END $$;

-- 独自の守り5つも、同じ1か所（salescall.forgetting）を読む（ほかの決まりは 0145・0201・0056・0280・0159 のまま）
CREATE OR REPLACE FUNCTION salescall.attempt_blame_once() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    IF salescall.forgetting(to_jsonb(OLD), TG_TABLE_NAME) THEN
      RETURN OLD;
    END IF;
    RAISE EXCEPTION '試行のせいの欄は消せません' USING ERRCODE = 'check_violation', CONSTRAINT = 'attempt_blame_once';
  END IF;
  IF TG_OP = 'INSERT' THEN
    IF NEW.blame <> 'empty' THEN   -- 初めは空（そのあと表が V-1・V-2 で移す）
      RAISE EXCEPTION '試行のせいの欄は空から始めます' USING ERRCODE = 'check_violation', CONSTRAINT = 'attempt_blame_once';
    END IF;
    RETURN NEW;
  END IF;
  IF NEW.attempt_id <> OLD.attempt_id OR NEW.tenant_id <> OLD.tenant_id OR NEW.client_id <> OLD.client_id THEN
    RAISE EXCEPTION '試行のせいの欄の試行は変えられません' USING ERRCODE = 'check_violation', CONSTRAINT = 'attempt_blame_once';
  END IF;
  -- 移ってよいのは表の V-1〜V-4 だけ（空→分からない・空→こちら・分からない→番号・分からない→回線）。番号・回線・こちらは書き直せない（V-5）
  IF NEW.blame IS DISTINCT FROM OLD.blame
     AND NOT (OLD.blame = 'empty' AND NEW.blame IN ('unknown', 'ours'))
     AND NOT (OLD.blame = 'unknown' AND NEW.blame IN ('number', 'line')) THEN
    RAISE EXCEPTION '試行のせいは1回だけ書けます（% から % へは移せません）', OLD.blame, NEW.blame
      USING ERRCODE = 'check_violation', CONSTRAINT = 'attempt_blame_once';
  END IF;
  RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION salescall.deficit_warning_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    IF salescall.forgetting(to_jsonb(OLD), TG_TABLE_NAME) THEN
      RETURN OLD;
    END IF;
    RAISE EXCEPTION '赤字の警告は消せません' USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  IF (NEW.tenant_id, NEW.client_id, NEW.warning_id, NEW.warned_at, NEW.deadline, NEW.figures)
     IS DISTINCT FROM (OLD.tenant_id, OLD.client_id, OLD.warning_id, OLD.warned_at, OLD.deadline, OLD.figures)
     OR (OLD.confirmed_at IS NOT NULL AND (NEW.confirmed_at, NEW.confirmed_by) IS DISTINCT FROM (OLD.confirmed_at, OLD.confirmed_by))
     OR (OLD.closed_at IS NOT NULL AND (NEW.closed_at, NEW.closed_why) IS DISTINCT FROM (OLD.closed_at, OLD.closed_why))
     OR (OLD.reminded_at IS NOT NULL AND NEW.reminded_at IS DISTINCT FROM OLD.reminded_at) THEN
    RAISE EXCEPTION '赤字の警告は、確認（1回）・もう一度の知らせ（1回）・閉じる（1回）しかできません'
      USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  IF OLD.confirmed_at IS NULL AND NEW.confirmed_at IS NOT NULL
     AND NOT salescall.cur_is_person() THEN
    RAISE EXCEPTION '赤字の警告を確認できるのは、管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'deficit_confirmed_by_person';
  END IF;
  RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION salescall.recording_hold_released_once() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' AND salescall.forgetting(to_jsonb(OLD), TG_TABLE_NAME) THEN
    RETURN OLD;
  END IF;
  IF TG_OP = 'DELETE' OR OLD.released_at IS NOT NULL OR NEW.released_at IS NULL
     OR (NEW.tenant_id, NEW.client_id, NEW.attempt_id, NEW.reason, NEW.held_at)
        IS DISTINCT FROM (OLD.tenant_id, OLD.client_id, OLD.attempt_id, OLD.reason, OLD.held_at) THEN
    RAISE EXCEPTION '録音の保留は、測り終わったときに1回だけ外せます' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION salescall.submission_copied_once() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' AND salescall.forgetting(to_jsonb(OLD), TG_TABLE_NAME) THEN
    RETURN OLD;
  END IF;
  IF TG_OP = 'DELETE' OR OLD.copied_at IS NOT NULL
     OR (NEW.submission_id, NEW.tenant_id, NEW.client_id, NEW.delivered_at, NEW.file_name, NEW.sha256, NEW.template_id)
        IS DISTINCT FROM (OLD.submission_id, OLD.tenant_id, OLD.client_id, OLD.delivered_at, OLD.file_name, OLD.sha256, OLD.template_id) THEN
    RAISE EXCEPTION '提出用のファイルの記録は、写したときに1回だけ（中身を消す）変えられます' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION salescall.call_utterances_kept() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' AND salescall.forgetting(to_jsonb(OLD), TG_TABLE_NAME) THEN
    RETURN OLD;
  END IF;
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

-- 外せるか（1か所。外す処理と、見守り役・画面が読む）：理由の文を返す（NULL＝外せる）
CREATE FUNCTION salescall.forget_blocked(p_client uuid, p_after interval) RETURNS text LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = salescall, pg_temp AS $$
DECLARE e salescall.client_contract_ends;
BEGIN
  -- 読めるのは取引の契約者の依頼者だけ（行ごとの守りを越えて、ほかの契約者の依頼者のことを答えない）
  IF salescall.cur_tenant() IS NULL OR salescall.cur_tenant() IS DISTINCT FROM
     (SELECT c.tenant_id FROM salescall.clients c WHERE c.client_id = p_client) THEN
    RAISE EXCEPTION '外せるかを読めるのは、その依頼者の契約者の取引だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'forget_blocked_in_tenant';
  END IF;
  SELECT * INTO e FROM salescall.client_contract_ends WHERE end_id = salescall.contract_ended(p_client);
  IF e.end_id IS NULL THEN
    RETURN '契約終了になっていません';
  END IF;
  IF EXISTS (SELECT 1 FROM salescall.client_forgets f WHERE f.client_id = p_client) THEN
    RETURN 'もうサーバーから外しています';
  END IF;
  IF now() < e.ended_at + p_after THEN
    RETURN '契約終了の猶予のあいだです（取り消せます）';
  END IF;
  IF salescall.client_active(p_client) THEN
    RETURN 'まだ使う形になっています';
  END IF;
  IF EXISTS (SELECT 1 FROM salescall.attempts a WHERE a.client_id = p_client AND salescall.is_active(a.state)) THEN
    RETURN 'かけている電話があります';
  END IF;
  IF EXISTS (SELECT 1 FROM salescall.submission_files s WHERE s.client_id = p_client AND s.copied_at IS NULL) THEN
    RETURN '提出用のファイルを、まだハードディスクへ写していません';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM salescall.backup_runs b WHERE b.client_id = p_client AND b.kind = 'client_ai' AND b.restore_ok
                  AND b.started_at > e.ended_at) THEN
    RETURN '契約終了のあとの控えを、まだハードディスクに取って確かめていません';
  END IF;
  RETURN NULL;
END $$;
REVOKE ALL ON FUNCTION salescall.forget_blocked(uuid, interval) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.forget_blocked(uuid, interval) TO salescall_app;

-- サーバーから外す（1か所。見守り役の仕事 contract_ends が、契約者の取引のシステムの処理で呼ぶ）。外した表ごとの行の数を返す
CREATE FUNCTION salescall.forget_client(p_client uuid, p_after interval) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path = salescall, pg_temp AS $$
DECLARE
  why text;
  run bigint;
  removed jsonb := '{}';
  todo text[];
  left_over text[];
  t text;
  n integer;
BEGIN
  IF salescall.cur_actor_kind() IS DISTINCT FROM 'system' OR salescall.cur_tenant() IS DISTINCT FROM
     (SELECT c.tenant_id FROM salescall.clients c WHERE c.client_id = p_client) THEN
    RAISE EXCEPTION '依頼者をサーバーから外すのは、その契約者の取引のシステムの処理だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'forget_by_system';
  END IF;
  why := salescall.forget_blocked(p_client, p_after);
  IF why IS NOT NULL THEN
    RAISE EXCEPTION 'まだ外せません：%', why USING ERRCODE = 'check_violation', CONSTRAINT = 'forget_not_ready';
  END IF;
  SELECT max(b.run_id) INTO run FROM salescall.backup_runs b
   WHERE b.client_id = p_client AND b.kind = 'client_ai' AND b.restore_ok;
  PERFORM set_config('salescall.forgetting_client', p_client::text, true);
  -- 残すお客様の行の中身を空にする（行は、残す通話の記録が指すので残す）
  UPDATE salescall.tasks SET client_ref = NULL, source_name = NULL, source_phone = NULL, source_row = NULL, import_id = NULL,
         source_name_reading = NULL, source_name_reading_by = NULL, source_name_reading_candidates = '{}', source_name_reading_likely = NULL,
         target_name = NULL, target_name_reading = NULL, target_name_reading_by = NULL, target_name_reading_candidates = '{}',
         target_name_reading_likely = NULL, target_role = NULL, target_full_name = NULL, target_full_name_reading = NULL,
         target_full_name_reading_by = NULL, target_full_name_reading_candidates = '{}', target_full_name_reading_likely = NULL,
         target_department = NULL
   WHERE client_id = p_client;
  GET DIAGNOSTICS n = ROW_COUNT;
  removed := removed || jsonb_build_object('tasks（中身を空に）', n);
  -- 依頼者の欄を持たない子の表
  DELETE FROM salescall.notification_deliveries d USING salescall.notification_events e
   WHERE d.event_id = e.event_id AND e.client_id = p_client;
  GET DIAGNOSTICS n = ROW_COUNT;
  removed := removed || jsonb_build_object('notification_deliveries', n);
  DELETE FROM salescall.stop_checks k USING salescall.stop_events e WHERE k.stop_event_id = e.event_id AND e.client_id = p_client;
  GET DIAGNOSTICS n = ROW_COUNT;
  removed := removed || jsonb_build_object('stop_checks', n);
  DELETE FROM salescall.stop_emergency_passes k USING salescall.stop_events e WHERE k.stop_event_id = e.event_id AND e.client_id = p_client;
  GET DIAGNOSTICS n = ROW_COUNT;
  removed := removed || jsonb_build_object('stop_emergency_passes', n);
  -- 音の置き場：すべての契約者で使うシステムの録音（審査を通った名前の録音）の音は残す
  DELETE FROM salescall.recording_audio a WHERE a.client_id = p_client
     AND NOT EXISTS (SELECT 1 FROM salescall.recordings r JOIN salescall.recording_sets s ON s.set_id = r.set_id
                      WHERE s.tenant_id IS NULL AND r.audio_sha256 = a.audio_sha256);
  GET DIAGNOSTICS n = ROW_COUNT;
  removed := removed || jsonb_build_object('recording_audio', n);
  -- 外す表：依頼者の欄を持つ表のうち、残す表でないもの（表の一覧から）。つながりの約束に当たった表は、次の回に回す
  SELECT array_agg(c.relname::text ORDER BY c.relname) INTO todo
    FROM pg_class c JOIN pg_namespace ns ON ns.oid = c.relnamespace
   WHERE ns.nspname = 'salescall' AND c.relkind IN ('r', 'p') AND NOT (c.relname::text = ANY (salescall.forget_kept_tables()))
     AND EXISTS (SELECT 1 FROM pg_attribute at WHERE at.attrelid = c.oid AND at.attname = 'client_id' AND NOT at.attisdropped);
  LOOP
    left_over := '{}';
    FOREACH t IN ARRAY todo LOOP
      BEGIN
        EXECUTE format('DELETE FROM salescall.%I WHERE client_id = $1', t) USING p_client;
        GET DIAGNOSTICS n = ROW_COUNT;
        removed := removed || jsonb_build_object(t, coalesce((removed->>t)::integer, 0) + n);
      EXCEPTION WHEN foreign_key_violation THEN
        left_over := left_over || t;
      END;
    END LOOP;
    EXIT WHEN cardinality(left_over) = 0;
    IF cardinality(left_over) = cardinality(todo) THEN
      RAISE EXCEPTION '外せない表があります（つながりの約束）：%', left_over USING ERRCODE = 'foreign_key_violation';
    END IF;
    todo := left_over;
  END LOOP;
  PERFORM set_config('salescall.forgetting_client', '', true);
  INSERT INTO salescall.client_forgets (client_id, tenant_id, end_id, backup_run, removed)
  VALUES (p_client, salescall.cur_tenant(), salescall.contract_ended(p_client), run, removed);
  RETURN removed;
END $$;
REVOKE ALL ON FUNCTION salescall.forget_client(uuid, interval) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.forget_client(uuid, interval) TO salescall_app;
