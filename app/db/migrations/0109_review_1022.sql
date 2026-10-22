-- 0109 2026-10-22 の見直し（main に入れる前の見直し11）。書いた人：CL-2

-- ═════════ (1) 動いている合図は、使わなくなった印を付けた回線でも、通話の途中のあいだは残す（DESIGN.md 5-4） ═════════
-- ⛔ 2026-10-22 の見直し：通話の途中で回線に「使わない」の印を付けると、次の合図でその回線が PC のつなぎから外れ、そのあとは
-- お客様の声も「おわびして切って」も通らなかった（PC はつながったままなので、PC の側のおわびも動かない＝お客様を無言で放置する）。
-- 「使わない」は、新しくかけないこと（門が断る＝core/gate.py）。かけている途中の通話は、終わるまでその回線で続ける（声・切る・
-- おわびが通る）。決めるのは、この合図の1か所。
CREATE OR REPLACE FUNCTION salescall.agent_beat(p_pc uuid, p_lines uuid[]) RETURNS uuid[]
LANGUAGE plpgsql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
DECLARE kept uuid[];
BEGIN
  PERFORM salescall.hub_only();
  UPDATE salescall.pc_agents a SET last_seen_at = now(),
         lines = coalesce((SELECT array_agg(l.line_id ORDER BY l.line_id) FROM salescall.lines l
                           WHERE l.tenant_id = a.tenant_id AND l.line_id = ANY (p_lines)
                             AND (l.retired_at IS NULL
                                  OR EXISTS (SELECT 1 FROM salescall.attempts t
                                              WHERE t.line_id = l.line_id AND salescall.is_active(t.state)))), '{}')
   WHERE a.pc_id = p_pc AND a.revoked_at IS NULL
  RETURNING a.lines INTO kept;
  RETURN kept;
END $$;

-- ═════════ (2) 控えの記録の種類から、使わなくなった種類を外す（負の遺産） ═════════
-- drive（ハードディスクが無い）・key（鍵が無い）は、PC の見守り役が書いていた種類。サーバーと PC の分け方（DESIGN.md 5-5）のあとは、
-- PC が直す人あての文を返し、その控えの種類（system・client_ai・submission・prune）の記録に書く（core/backup.py の PARTS の1か所）。
ALTER TABLE salescall.backup_runs DROP CONSTRAINT backup_runs_kind_check;
ALTER TABLE salescall.backup_runs
  ADD CONSTRAINT backup_runs_kind_check CHECK (kind IN ('system', 'client_ai', 'submission', 'prune'));

-- ═════════ (3) 門の不許可の記録は、その行の理由が変わったときだけ足す（core/gate.py の claim）。その行の最後の記録を速く読む ═════════
CREATE INDEX gate_denials_by_task ON salescall.gate_denials (task_id, denial_id DESC);

-- ═════════ (4) 「お客様のところで鳴った（鳴りえた）発信」の決まりを1か所に（salescall.is_placed） ═════════
-- 発信まで進んだが、回線の側でかけられなかった（call_failed＝電話の部品がかけられなかった・PC の上で緊急停止・PC がつながって
-- いない）発信は、お客様のところで鳴っていない。⛔ 2026-10-22 の見直し：回線の1日の上限・ほかの依頼者がかけた番号の間隔・ほかの
-- 契約者がかけた番号に、それまで数えていた（ほかの依頼者がその番号へ3日かけられなくなり、回線の1日の上限も減った）。
-- core/attempts.py の placed() も、この関数を使う（同じ決まりを2か所に書かない）。
CREATE FUNCTION salescall.is_placed(s salescall.attempt_state) RETURNS boolean LANGUAGE sql IMMUTABLE AS
$$ SELECT salescall.is_dialed(s) AND s <> 'call_failed' $$;
GRANT EXECUTE ON FUNCTION salescall.is_placed(salescall.attempt_state) TO salescall_app;

CREATE OR REPLACE FUNCTION salescall.line_dials_on(p_line uuid, p_day date)
 RETURNS integer LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO 'salescall', 'pg_temp'
AS $function$
  SELECT count(*)::integer FROM salescall.attempts a
  WHERE a.line_id = p_line AND a.tenant_id = salescall.cur_tenant()
    AND salescall.is_placed(a.state)
    AND salescall.jst(a.claimed_at)::date = p_day
$function$;
CREATE OR REPLACE FUNCTION salescall.number_last_dial_elsewhere(p_phone text)
 RETURNS TABLE(relation text, last_at timestamp with time zone)
 LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO 'salescall', 'pg_temp'
AS $function$
  SELECT CASE WHEN a.tenant_id = salescall.cur_tenant() THEN 'same_tenant' ELSE 'other_tenant' END, max(a.claimed_at)
  FROM salescall.attempts a
  WHERE a.phone_e164 = p_phone AND a.client_id IS DISTINCT FROM salescall.cur_client()
    AND salescall.is_placed(a.state)
  GROUP BY 1
$function$;
CREATE OR REPLACE FUNCTION salescall.number_dialed_by_other_tenant(p_phone text)
 RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO 'salescall', 'pg_temp'
AS $function$
  SELECT EXISTS (SELECT 1 FROM salescall.attempts a WHERE a.phone_e164 = p_phone AND a.tenant_id <> salescall.cur_tenant()
                 AND salescall.is_placed(a.state))
$function$;

-- ═════════ (5) その通話で取った答えを速く読む（目的のお客様の確かめ直しが要るか＝core/target_recheck.py の needed） ═════════
CREATE INDEX item_values_by_attempt ON salescall.item_values (attempt_id);

-- ═════════ (6) 録音の組の部分・承認は、取引から見える組にだけ（依頼者をまたいで足せない・承認できない） ═════════
-- ⛔ 2026-10-22 の見直し：仕掛けが組の依頼者を行ごとの制限の下で読むので、ほかの依頼者の組は「見えない＝空」と読まれ、部分の空の
-- 依頼者と「同じ」と判断された。依頼者Aの取引から、依頼者Bのまだ承認していない組に部分を足し、承認できた。
CREATE OR REPLACE FUNCTION salescall.recording_before_insert() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM salescall.recording_approvals WHERE set_id = NEW.set_id) THEN
    RAISE EXCEPTION '承認した録音の組には、部分を足せません（作り直すときは新しい組）' USING ERRCODE = 'check_violation';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM salescall.recording_sets s
                 WHERE s.set_id = NEW.set_id AND s.client_id IS NOT DISTINCT FROM NEW.client_id) THEN
    RAISE EXCEPTION '録音の部分は、取引から見える組に、組と同じ依頼者で足します' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION salescall.recording_approval_by_person() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF coalesce(salescall.cur_actor_kind()::text, '') NOT IN ('operator', 'admin')
     OR NEW.approved_by IS DISTINCT FROM salescall.cur_actor() THEN
    RAISE EXCEPTION '録音を承認できるのは、管理者か契約者だけです（承認した者は、取引の管理者か契約者）'
      USING ERRCODE = 'insufficient_privilege', CONSTRAINT = 'recording_approval_by_person';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM salescall.recording_sets s
                 WHERE s.set_id = NEW.set_id AND s.client_id IS NOT DISTINCT FROM NEW.client_id) THEN
    RAISE EXCEPTION '録音の承認は、取引から見える組に、組と同じ依頼者でします' USING ERRCODE = 'check_violation';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM salescall.recordings WHERE set_id = NEW.set_id) THEN
    RAISE EXCEPTION '部分の無い録音の組は承認できません' USING ERRCODE = 'check_violation';
  END IF;
  IF EXISTS (SELECT 1 FROM salescall.recording_sets s JOIN salescall.voices v USING (voice_name)
             WHERE s.set_id = NEW.set_id AND v.retired_at IS NOT NULL) THEN
    RAISE EXCEPTION '使わない声の録音は承認できません' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;

-- ═════════ (7) 「最新が正本」の記録の順番は、データベースが決める（渡された値を使わない） ═════════
-- ⛔ 2026-10-22 の見直し：順番の番号・承認の時刻を書く側が渡せたので、番号を最大の値で入れると、そのあとの「最大＋1」で足す書き方が
-- 桁あふれで失敗し続け（依頼者を無効にできなくなった）、承認の時刻を未来にすると、あとの承認が「いまの組」にならなかった。
CREATE FUNCTION salescall.next_seq() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE k1 text := to_jsonb(NEW) ->> TG_ARGV[0]; k2 text := CASE WHEN TG_NARGS > 1 THEN to_jsonb(NEW) ->> TG_ARGV[1] END;
BEGIN
  IF TG_NARGS > 1 THEN
    EXECUTE format('SELECT coalesce(max(seq), 0) + 1 FROM %I.%I WHERE %I::text = $1 AND %I::text = $2',
                   TG_TABLE_SCHEMA, TG_TABLE_NAME, TG_ARGV[0], TG_ARGV[1]) INTO NEW.seq USING k1, k2;
  ELSE
    EXECUTE format('SELECT coalesce(max(seq), 0) + 1 FROM %I.%I WHERE %I::text = $1',
                   TG_TABLE_SCHEMA, TG_TABLE_NAME, TG_ARGV[0]) INTO NEW.seq USING k1;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER next_seq BEFORE INSERT ON salescall.task_next_actions FOR EACH ROW EXECUTE FUNCTION salescall.next_seq('task_id');
CREATE TRIGGER next_seq BEFORE INSERT ON salescall.run_versions FOR EACH ROW EXECUTE FUNCTION salescall.next_seq('run_id');
CREATE TRIGGER next_seq BEFORE INSERT ON salescall.client_states FOR EACH ROW EXECUTE FUNCTION salescall.next_seq('client_id');
-- 名前の読みは、録音を聞いて選んだ読みだけが番号を持つ（版の中身に書いた読みは 0。0101 の version_name_readings_seq_check）。
CREATE TRIGGER next_seq BEFORE INSERT ON salescall.version_name_readings
  FOR EACH ROW WHEN (NEW.reading_by = 'recording') EXECUTE FUNCTION salescall.next_seq('version_id', 'name_text');

CREATE FUNCTION salescall.stamp_now() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW := jsonb_populate_record(NEW, jsonb_build_object(TG_ARGV[0], now()));
  RETURN NEW;
END $$;
CREATE TRIGGER stamp_now BEFORE INSERT ON salescall.recording_approvals
  FOR EACH ROW EXECUTE FUNCTION salescall.stamp_now('approved_at');
CREATE TRIGGER stamp_now BEFORE INSERT ON salescall.apology_approvals
  FOR EACH ROW EXECUTE FUNCTION salescall.stamp_now('approved_at');
CREATE TRIGGER stamp_now BEFORE INSERT ON salescall.task_next_actions
  FOR EACH ROW EXECUTE FUNCTION salescall.stamp_now('decided_at');

-- ═════════ (8) 番号だけの参照で、ほかの依頼者・契約者の行を指せない ═════════
-- ⛔ 2026-10-22 の見直し：お詫びの承認は案の番号（全体の通し番号）だけで参照していたので、契約者Aの取引から契約者Bの案に承認を
-- 入れて、B自身の承認を「重複」で塞げた（番号を入れると、案があるかも分かった）。品質の評価の停止・利用量の単価も同じ形だった。
ALTER TABLE salescall.apology_orders ADD CONSTRAINT apology_orders_client_order UNIQUE (client_id, order_id);
ALTER TABLE salescall.apology_approvals
  ADD CONSTRAINT apology_approvals_order_of_client FOREIGN KEY (client_id, order_id)
    REFERENCES salescall.apology_orders (client_id, order_id);

-- 参照する行が、その取引から見えること（見えない＝ほかの契約者・依頼者の行は指せない。決まりはこの1つの仕掛け）
CREATE FUNCTION salescall.ref_visible() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v text := to_jsonb(NEW) ->> TG_ARGV[0]; seen boolean;
BEGIN
  IF v IS NULL THEN
    RETURN NEW;
  END IF;
  EXECUTE format('SELECT EXISTS (SELECT 1 FROM %I.%I WHERE %I::text = $1)', TG_TABLE_SCHEMA, TG_ARGV[1], TG_ARGV[2])
    INTO seen USING v;
  IF NOT seen THEN
    RAISE EXCEPTION '参照する行（%）が、この取引から見えません（ほかの契約者・依頼者の行は指せません）', TG_ARGV[1]
      USING ERRCODE = 'foreign_key_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER stop_event_visible BEFORE INSERT OR UPDATE ON salescall.quality_evaluations
  FOR EACH ROW EXECUTE FUNCTION salescall.ref_visible('stop_event_id', 'stop_events', 'event_id');
CREATE TRIGGER price_visible BEFORE INSERT OR UPDATE ON salescall.usage_entries
  FOR EACH ROW EXECUTE FUNCTION salescall.ref_visible('price_id', 'price_versions', 'price_id');
-- 停止を解く確かめ（stop_checks）は、行ごとの制限 check_scope（0029。停止が見えて、その停止の範囲の取引だけ）が、もっと厳しく
-- 同じことを守っているので、ここでは足さない（同じ判断を2か所に置かない）。

-- ═════════ (9) 仕掛けだけが書く記録には、サーバーの役が直接書けない ═════════
-- ⛔ 2026-10-22 の見直し：状態の移り変わり（attempt_state_history）・後の通話に上書きされた前の答え（item_value_history）・名前の
-- 変更（name_changes）に、サーバーの役が INSERT でき、予約のままの試行に「つながった」行を足すと「お客様が出た」になり（報酬・
-- つながった数の元）、取っていない値を「前の答え」として足すと、確かめ直しが「確かめた答え」として戻した。名前の変更の記録は、
-- ほかの依頼者の分も足せた。⇒ 書くのは仕掛け（持ち主の権限で動く・search_path を固定）だけにし、サーバーの役の INSERT を外す。
ALTER FUNCTION salescall.attempt_after_write() SECURITY DEFINER SET search_path = salescall, pg_temp;
ALTER FUNCTION salescall.only_name_changes() SECURITY DEFINER SET search_path = salescall, pg_temp;
ALTER FUNCTION salescall.line_before_update() SECURITY DEFINER SET search_path = salescall, pg_temp;
ALTER FUNCTION salescall.item_value_guard() SECURITY DEFINER SET search_path = salescall, pg_temp;
REVOKE EXECUTE ON FUNCTION salescall.attempt_after_write(), salescall.only_name_changes(), salescall.line_before_update(),
  salescall.item_value_guard() FROM PUBLIC, salescall_app;   -- 仕掛けとして動くだけ（直接は呼べない。仕掛けが動くのに実行の権限は要らない）
REVOKE INSERT ON salescall.attempt_state_history, salescall.item_value_history, salescall.name_changes FROM salescall_app;

-- ═════════ (10) 設定の意味を、いまの動きにそろえる ═════════
-- ⛔ 2026-10-22 の見直し：検証のやり直しの待ちの意味に「録音が無い」も済むまでやり直すと書いてあり、録音が届かない通話は
-- 10分（verification.recording_wait）で「録音なし」として済ませる決まり（0061。2026-10-18 ユーザー決定）と食い違っていた。
ALTER TABLE salescall.setting_definitions DISABLE TRIGGER setting_definitions_frozen;
UPDATE salescall.setting_definitions
SET meaning = '通話後の検証を、仕組みの不具合（別の耳につながらない・誤り）のあとにやり直すまでの待ち（上限は無い：済むまでやり直す。'
              '録音が届かない通話は、通話の終わりから verification.recording_wait を過ぎたら「録音なし」で済ませる）'
WHERE key = 'verification.retry_wait';
ALTER TABLE salescall.setting_definitions ENABLE TRIGGER setting_definitions_frozen;

-- ═════════ (11) 連携の台帳は、送った状態だけを書き換えられる ═════════
-- ⛔ 2026-10-22 の見直し：サーバーの役が連携の台帳（deliveries）のどの列も書き換えられたので、「送った」記録を、送っていない別の
-- お客様の行（タスク）へ付け替えられた（その行は送ったことになり、依頼者へ届かない）。⇒ 書き換えてよいのは、送った状態・回数・
-- 誤り・相手の番号・時刻だけ（どの行・どの送り先・どの版かは、作ったときのまま）。
REVOKE UPDATE ON salescall.deliveries FROM salescall_app;
GRANT UPDATE (state, send_count, last_error, remote_ref, updated_at) ON salescall.deliveries TO salescall_app;
