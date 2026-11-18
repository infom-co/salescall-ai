-- 0010 相手が言った番号が「番号違い」だったとき（DESIGN.md 3-5。2026-10-15 ユーザー決定）。書いた人：CL-2
-- ユーザー：「明らかに番号違いだった場合は、最初にかけた相手が番号を伝え忘れたか、こちらの聞き違いの可能性がある。証拠と付け合わせ、
--   確認の上、それでも間違いなら、最初の相手に、お聞きした番号が番号違いだったことを告げ、再度、かけ直し先の番号を伺う」。
-- - 番号の確かめ（number_rechecks）：番号違いが分かった通話の、通話後の検証（Q21）の中で、番号を聞いた通話の証拠（録音・別の耳）と
--   かけた番号を付け合わせた結果。聞き違い（正しい番号つき）／言われたとおり／確かめられない。書くのは core/number_recheck.py だけ。
-- - 発信の目的（purpose）：ふつう／番号の聞き直し（番号を聞いた通話の番号へかけ、番号違いだったことを告げて、もう一度番号を伺う）。
--   決めるのは次の動きだけ（予定に持つ）。門が予定から試行へ写す。会話の処理は試行の目的を読んで話す。

CREATE TABLE salescall.number_rechecks (    -- 番号の確かめ（番号違いが分かった通話ごとに1つ。変更不可）
  tenant_id      uuid NOT NULL,
  client_id      uuid NOT NULL,
  attempt_id     uuid PRIMARY KEY,           -- 番号違いが分かった通話（相手が言った番号へかけた通話）
  outcome        text NOT NULL CHECK (outcome IN ('misheard', 'as_said', 'cannot_check')),
  corrected_e164 text CHECK (corrected_e164 IS NULL OR corrected_e164 ~ '^\+[1-9][0-9]{6,14}$'),  -- 聞き違いのときの正しい番号
  evidence       text NOT NULL CHECK (btrim(evidence) <> ''),   -- 何と付け合わせたか（別の耳の文字・録音の場所など）
  checked_by     text NOT NULL CHECK (btrim(checked_by) <> ''),
  checked_at     timestamptz NOT NULL DEFAULT now(),
  CHECK ((outcome = 'misheard') = (corrected_e164 IS NOT NULL)),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, attempt_id) REFERENCES salescall.attempts (client_id, attempt_id)
);
CREATE TRIGGER number_rechecks_frozen BEFORE UPDATE OR DELETE ON salescall.number_rechecks
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
ALTER TABLE salescall.number_rechecks ENABLE ROW LEVEL SECURITY;
CREATE POLICY client_scope ON salescall.number_rechecks
  USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
  WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client());
GRANT SELECT, INSERT ON salescall.number_rechecks TO salescall_app;

-- ═════════ 発信の目的（予定に持ち、門が試行へ写す） ═════════
ALTER TABLE salescall.task_next_actions
  ADD COLUMN purpose text CHECK (purpose IS NULL OR purpose IN ('normal', 'ask_number_again')),
  ADD COLUMN wrong_contact_id uuid,          -- 番号の聞き直しのとき：番号違いだった番号（その行の番号）
  ADD CONSTRAINT task_next_actions_purpose CHECK ((kind = 'call') = (purpose IS NOT NULL)),
  ADD CONSTRAINT task_next_actions_wrong_number CHECK ((purpose = 'ask_number_again') = (wrong_contact_id IS NOT NULL)),
  ADD CONSTRAINT task_next_actions_wrong_number_of_task
    FOREIGN KEY (client_id, task_id, wrong_contact_id) REFERENCES salescall.task_numbers (client_id, task_id, contact_id);

ALTER TABLE salescall.attempts
  ADD COLUMN purpose text NOT NULL CHECK (purpose IN ('normal', 'ask_number_again')),
  ADD COLUMN wrong_contact_id uuid,
  ADD CONSTRAINT attempts_wrong_number CHECK ((purpose = 'ask_number_again') = (wrong_contact_id IS NOT NULL)),
  ADD CONSTRAINT attempts_wrong_number_of_task
    FOREIGN KEY (client_id, task_id, wrong_contact_id) REFERENCES salescall.task_numbers (client_id, task_id, contact_id);

-- 発信の目的は、予約のときに決まり、あとから変えられない
CREATE FUNCTION salescall.attempt_purpose_fixed() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (NEW.purpose, NEW.wrong_contact_id) IS DISTINCT FROM (OLD.purpose, OLD.wrong_contact_id) THEN
    RAISE EXCEPTION '発信の目的は変えられません' USING ERRCODE = 'check_violation', CONSTRAINT = 'attempts_purpose_fixed';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER attempt_purpose_fixed BEFORE UPDATE ON salescall.attempts
  FOR EACH ROW EXECUTE FUNCTION salescall.attempt_purpose_fixed();
