-- 0108 目的のお客様に、意味の判定なしで繋いだ通話の確かめ直し（DESIGN.md 6-12 の (e)・7-1。CL-1 の連絡帳 4-25）。書いた人：CL-2
--
-- 目的のお客様に繋ぐのは「ご本人」の読みが2つそろったときだけ（CL-1。DESIGN.md 4-3）。ただし判定が止まっていたときは、確かめへの
-- 「はい」をAIの読みだけで受け取って繋ぐ（止まらない）。その通話は、通話の事実 connect に繋いだ読みを残し（AIだけ＝read が "ai" だけ）、
-- 繋いだあとに届いた判定も足す（late）。⇒ 通話後の検証（Q21。CL-1）が、その発話の録音を別の耳と判定で確かめ直し、ここに記録する。
-- - 確かめ直しが要る通話か：`call_report.CallReport.target_unverified`（判断はその1か所）。要る通話は、記録が無いと通話後の検証を
--   済ませられない（core/attempts.py の `record_verification`。録音が届かず済ませるときは cannot_check を記録する）。
-- - 結果：target＝目的のお客様だった／not_target＝別の方だった／cannot_check＝確かめられなかった（録音が無い など）。
-- - **target 以外なら、その通話で取った答えを全部、前の通話の答え（目的のお客様だと確かめられなかった通話のもの以外）に戻し、
--   無ければ「-」（unresolved）にする**（別の方の答え・確かめていない答えを依頼者へ渡さない。依頼者へ渡るのは、確かめた値か「-」だけ
--   ＝Intent.md 9-5 の7）。するのは、この表に記録したときの1か所（下の仕掛け）。
--   どの答えがその通話のものかは、項目の値に足した attempt_id（必ず入る）で見る（ほかの通話で目的のお客様から取った答えは残す）。
-- - 締めの処理は、target 以外の通話を「目的のお客様と話せた」と数えない（約束の日のお詫びの印を外さない。core/settle.py）。

CREATE TABLE salescall.target_rechecks (    -- 目的のお客様の確かめ直し（確かめ直しが要る通話ごとに1つ。変更不可）
  tenant_id   uuid NOT NULL,
  client_id   uuid NOT NULL,
  attempt_id  uuid PRIMARY KEY,
  outcome     text NOT NULL CHECK (outcome IN ('target', 'not_target', 'cannot_check')),
  evidence    text NOT NULL CHECK (btrim(evidence) <> ''),   -- 何で確かめたか（別の耳の文字・判定の名前・録音の場所など）
  checked_by  text NOT NULL CHECK (btrim(checked_by) <> ''),
  checked_at  timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, attempt_id) REFERENCES salescall.attempts (client_id, attempt_id)
);
CREATE TRIGGER target_rechecks_frozen BEFORE UPDATE OR DELETE ON salescall.target_rechecks
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
ALTER TABLE salescall.target_rechecks ENABLE ROW LEVEL SECURITY;
CREATE POLICY client_scope ON salescall.target_rechecks
  USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
  WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client());
GRANT SELECT, INSERT ON salescall.target_rechecks TO salescall_app;

-- 項目の値を、どの通話で取ったか（必ず入れる。会話の処理が書き込む入口 core/conversation_io.py が入れる。本番には、まだ値が無い）
ALTER TABLE salescall.item_values
  ADD COLUMN attempt_id uuid NOT NULL,
  ADD CONSTRAINT item_values_attempt_of_client FOREIGN KEY (client_id, attempt_id)
    REFERENCES salescall.attempts (client_id, attempt_id);

-- ═════════ 答えの上書きと、前の通話の答えの残し（見直し：同じ項目は1つの値なので、後の通話が上書きする） ═════════
-- (1) 答えを書く・書き換えるのは、そのタスクのいちばん新しい通話の番号で（前の通話の番号のまま上書きすると、確かめ直しで
--     「-」にする答えを取り違える）。(2) 後の通話が上書きするとき、前の通話の答えを残す（item_value_history）。目的のお客様だと
--     確かめられなかった通話の答えは、残した答えのうち、確かめられなかった通話のもの以外でいちばん新しいものに戻す（無ければ「-」）。
CREATE TABLE salescall.item_value_history (   -- 後の通話に上書きされた、前の通話の答え（通話ごとに1つ。変更不可）
  tenant_id   uuid NOT NULL,
  client_id   uuid NOT NULL,
  task_id     uuid NOT NULL,
  question_id text NOT NULL,
  attempt_id  uuid NOT NULL,
  state       salescall.item_state NOT NULL,
  value       jsonb,
  condition   text,
  reason      text,
  hedged      text,
  replaced_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (task_id, question_id, attempt_id),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, task_id) REFERENCES salescall.tasks (client_id, task_id),
  FOREIGN KEY (client_id, attempt_id) REFERENCES salescall.attempts (client_id, attempt_id)
);
CREATE TRIGGER item_value_history_frozen BEFORE UPDATE OR DELETE ON salescall.item_value_history
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
ALTER TABLE salescall.item_value_history ENABLE ROW LEVEL SECURITY;
CREATE POLICY client_scope ON salescall.item_value_history
  USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
  WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client());
GRANT SELECT, INSERT ON salescall.item_value_history TO salescall_app;

-- そのタスクのいちばん新しい通話（答えを書けるのは、この通話だけ）
CREATE FUNCTION salescall.newest_attempt(p_task uuid) RETURNS uuid LANGUAGE sql STABLE AS $$
  SELECT a.attempt_id FROM salescall.attempts a WHERE a.task_id = p_task ORDER BY a.claimed_at DESC, a.attempt_id DESC LIMIT 1
$$;

CREATE FUNCTION salescall.item_value_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF coalesce(current_setting('salescall.item_restore', true), '') <> '' THEN
    RETURN NEW;                     -- 確かめ直しが戻している（下の target_recheck_blanks の中だけ）
  END IF;
  IF TG_OP = 'UPDATE' AND NEW.attempt_id IS NOT DISTINCT FROM OLD.attempt_id
     AND (NEW.state, NEW.value, NEW.condition, NEW.reason, NEW.hedged)
         IS NOT DISTINCT FROM (OLD.state, OLD.value, OLD.condition, OLD.reason, OLD.hedged) THEN
    RETURN NEW;                     -- 中身が変わらない
  END IF;
  IF NEW.attempt_id IS DISTINCT FROM salescall.newest_attempt(NEW.task_id) THEN
    RAISE EXCEPTION '答えは、そのタスクのいちばん新しい通話の番号で書きます（前の通話の番号のまま書き換えない）'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'item_values_from_newest_call';
  END IF;
  IF TG_OP = 'UPDATE' AND NEW.attempt_id IS DISTINCT FROM OLD.attempt_id THEN   -- 後の通話が上書きする：前の通話の答えを残す
    INSERT INTO salescall.item_value_history (tenant_id, client_id, task_id, question_id, attempt_id, state, value, condition,
                                              reason, hedged)
    VALUES (OLD.tenant_id, OLD.client_id, OLD.task_id, OLD.question_id, OLD.attempt_id, OLD.state, OLD.value, OLD.condition,
            OLD.reason, OLD.hedged)
    ON CONFLICT DO NOTHING;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER item_value_guard BEFORE INSERT OR UPDATE ON salescall.item_values
  FOR EACH ROW EXECUTE FUNCTION salescall.item_value_guard();

-- 目的のお客様だと確かめられなかった通話の答えを戻す・「-」にする（ここだけ。記録と同じ取引の中で必ず起きる）
CREATE FUNCTION salescall.target_recheck_blanks() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v record; prev record; t uuid;
BEGIN
  IF NEW.outcome = 'target' THEN
    RETURN NEW;
  END IF;
  t := (SELECT a.task_id FROM salescall.attempts a WHERE a.attempt_id = NEW.attempt_id);
  PERFORM set_config('salescall.item_restore', NEW.attempt_id::text, true);
  FOR v IN SELECT i.question_id FROM salescall.item_values i WHERE i.task_id = t AND i.attempt_id = NEW.attempt_id LOOP
    SELECT h.* INTO prev FROM salescall.item_value_history h
     WHERE h.task_id = t AND h.question_id = v.question_id AND h.attempt_id <> NEW.attempt_id
       AND NOT EXISTS (SELECT 1 FROM salescall.target_rechecks x WHERE x.attempt_id = h.attempt_id AND x.outcome <> 'target')
     ORDER BY h.replaced_at DESC LIMIT 1;
    IF FOUND THEN                   -- 前の通話で取った答え（目的のお客様だと確かめられなかった通話のもの以外）に戻す
      UPDATE salescall.item_values i
         SET state = prev.state, value = prev.value, condition = prev.condition, reason = prev.reason, hedged = prev.hedged,
             attempt_id = prev.attempt_id, updated_at = now()
       WHERE i.task_id = t AND i.question_id = v.question_id;
    ELSE                            -- 前の答えが無い：「-」
      UPDATE salescall.item_values i
         SET state = 'unresolved', value = NULL, condition = NULL, reason = NULL, hedged = NULL, updated_at = now()
       WHERE i.task_id = t AND i.question_id = v.question_id;
    END IF;
  END LOOP;
  PERFORM set_config('salescall.item_restore', '', true);
  RETURN NEW;
END $$;
CREATE TRIGGER target_recheck_blanks AFTER INSERT ON salescall.target_rechecks
  FOR EACH ROW EXECUTE FUNCTION salescall.target_recheck_blanks();
