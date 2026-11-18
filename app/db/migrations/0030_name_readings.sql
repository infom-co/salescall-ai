-- 0030 名前の読みが決まらない名前も登録し、会話の中で確かめる（2026-10-16 ユーザー決定。CL-1 の連絡帳 4-15・4-16。Intent.md 6-8）。
-- 書いた人：CL-2
-- ユーザー：「日本語の曖昧な漢字、例えばなかじまとなかしまなどは人が見ても判断できません。それは会話の中で失礼のないように語るか、
--   場合によっては聞き出すというような手法がいいでしょう」「人は基本的に介在しない」。
-- - 0028 では、読みの無い名前を持たなかった（取込で止めた）。これを改め、読みが決まらない名前は、読みを空にして、考えられる読み
--   （candidates）と、まず言ってみる読み（likely）を持つ。読みはひらがな（決め方と形をそろえるのは core/readings.py の1か所）。
-- - 会話の中でお客様に確かめた読み（task_confirmed_readings）：追記だけ。次にかけるときは、この読みを決まった読みとして使う
--   （読むのは core/business_settings.py の target_of の1か所。書くのは core/tasks.py の record_confirmed_reading だけ）。

ALTER TABLE salescall.tasks DROP CONSTRAINT task_name_has_reading;
ALTER TABLE salescall.tasks
  ADD COLUMN source_name_reading_candidates text[] NOT NULL DEFAULT '{}',
  ADD COLUMN source_name_reading_likely     text,
  ADD COLUMN target_name_reading_candidates text[] NOT NULL DEFAULT '{}',
  ADD COLUMN target_name_reading_likely     text,
  ADD CONSTRAINT task_name_readings CHECK (
    -- 読みと、読みを決めたものは組
    (source_name_reading IS NULL) = (source_name_reading_by IS NULL)
    AND (target_name_reading IS NULL) = (target_name_reading_by IS NULL)
    -- 名前が無ければ、読みも候補も無い
    AND (source_name IS NOT NULL OR (source_name_reading IS NULL AND source_name_reading_candidates = '{}'
                                     AND source_name_reading_likely IS NULL))
    AND (target_name IS NOT NULL OR (target_name_reading IS NULL AND target_name_reading_candidates = '{}'
                                     AND target_name_reading_likely IS NULL))
    -- 決まった読みには候補を付けない
    AND (source_name_reading IS NULL OR (source_name_reading_candidates = '{}' AND source_name_reading_likely IS NULL))
    AND (target_name_reading IS NULL OR (target_name_reading_candidates = '{}' AND target_name_reading_likely IS NULL))
    -- 読みは、ひらがな（長音・中黒を含む）
    AND (source_name_reading IS NULL OR source_name_reading ~ '^[ぁ-ゖー・]+$')
    AND (target_name_reading IS NULL OR target_name_reading ~ '^[ぁ-ゖー・]+$')
    AND (source_name_reading_likely IS NULL OR source_name_reading_likely ~ '^[ぁ-ゖー・]+$')
    AND (target_name_reading_likely IS NULL OR target_name_reading_likely ~ '^[ぁ-ゖー・]+$')
    AND array_to_string(source_name_reading_candidates, '') ~ '^[ぁ-ゖー・]*$'
    AND array_to_string(target_name_reading_candidates, '') ~ '^[ぁ-ゖー・]*$');

CREATE TABLE salescall.task_confirmed_readings (   -- 会話の中でお客様に確かめた名前の読み（追記だけ。いちばん新しいものが効く）
  tenant_id    uuid NOT NULL,
  client_id    uuid NOT NULL,
  confirm_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  task_id      uuid NOT NULL,
  which        text NOT NULL CHECK (which IN ('source', 'target')),   -- お客様の会社名／目的のお客様の名前
  reading      text NOT NULL CHECK (reading ~ '^[ぁ-ゖー・]+$'),
  attempt_id   uuid NOT NULL,                                          -- 確かめた通話
  confirmed_by text NOT NULL CHECK (btrim(confirmed_by) <> ''),
  confirmed_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, task_id) REFERENCES salescall.tasks (client_id, task_id),
  FOREIGN KEY (client_id, attempt_id) REFERENCES salescall.attempts (client_id, attempt_id)
);
CREATE INDEX task_confirmed_readings_task ON salescall.task_confirmed_readings (task_id, which, confirm_id);
CREATE TRIGGER task_confirmed_readings_frozen BEFORE UPDATE OR DELETE ON salescall.task_confirmed_readings
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
-- 確かめた通話は、その行の通話（ほかの行の通話で確かめた読みを付けない）
CREATE FUNCTION salescall.confirmed_reading_of_task() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM salescall.attempts a WHERE a.attempt_id = NEW.attempt_id AND a.task_id = NEW.task_id) THEN
    RAISE EXCEPTION '確かめた通話が、その行の通話ではありません' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'task_confirmed_readings_attempt_of_task';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER confirmed_reading_of_task BEFORE INSERT ON salescall.task_confirmed_readings
  FOR EACH ROW EXECUTE FUNCTION salescall.confirmed_reading_of_task();
ALTER TABLE salescall.task_confirmed_readings ENABLE ROW LEVEL SECURITY;
CREATE POLICY client_scope ON salescall.task_confirmed_readings
  USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
  WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client());
GRANT SELECT, INSERT ON salescall.task_confirmed_readings TO salescall_app;
