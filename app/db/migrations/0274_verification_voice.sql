-- 0274 通話後の検証の結果に、会話AIの声の数（黙り・声の途切れ・言葉の抜け・同じ文の2回・英語）を足す（2026-11-20。書いた人：CL-2。DESIGN_15-2.md の934。
-- CL-1 の連絡帳 4-221・CL-2 の連絡帳 4-447・CL-3 のお願い 57）
--
-- 計画書 9-8 の「音声の問題・エラーの率」と、業務の指標（core/metrics）の、会話AIの言葉の抜け・声の途切れ・黙りの率。数えるのは会話の担当の通話後の検証（1か所）で、
-- 通話ごとに、数えるものごとの「確かめたターンの数 checked」と「見つかったターンの数 found」の組を渡す（率＝found÷checked。checked が0なら率を出さない）。
-- 届いていない通話（フェーズ1の前・録音なしで済ませた確かめ）には記録が無い＝指標は「まだ数えていない」（0 と言わない＝原則10）。
-- 書くのは検証を済ませる1か所（core/attempts.record_verification）だけ。検証の結果の数（verification_summaries）の後に書く・録音なしで済ませた確かめには書けない
-- （データベースも守る）。書き換えない。
CREATE TABLE salescall.verification_voice (
  tenant_id   uuid NOT NULL,
  client_id   uuid NOT NULL,
  attempt_id  uuid NOT NULL,
  kind        text NOT NULL CHECK (kind IN ('silent', 'cut_off', 'words_missing', 'too_long', 'wrong_language')),
  checked     integer NOT NULL CHECK (checked >= 0),
  found       integer NOT NULL CHECK (found >= 0),
  CONSTRAINT verification_voice_found_within CHECK (found <= checked),
  PRIMARY KEY (attempt_id, kind),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, attempt_id) REFERENCES salescall.attempts (client_id, attempt_id)
);
CREATE FUNCTION salescall.verification_voice_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM salescall.verification_summaries v WHERE v.attempt_id = NEW.attempt_id AND NOT v.recording_missing) THEN
    RAISE EXCEPTION '会話AIの声の数は、録音で確かめた検証の結果と一緒にだけ書きます' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'verification_voice_after_summary';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER verification_voice_guard BEFORE INSERT ON salescall.verification_voice
  FOR EACH ROW EXECUTE FUNCTION salescall.verification_voice_guard();
CREATE TRIGGER verification_voice_frozen BEFORE UPDATE OR DELETE ON salescall.verification_voice
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
ALTER TABLE salescall.verification_voice ENABLE ROW LEVEL SECURITY;
CREATE POLICY client_scope ON salescall.verification_voice
  USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
  WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client());
GRANT SELECT, INSERT ON salescall.verification_voice TO salescall_app;
