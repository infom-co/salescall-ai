-- 0151 お客様の番号へかけて、電話網のお知らせの声が返った電話（取り消せない決定の洗い出し。2026-10-30。書いた人：CL-2。DESIGN.md 17章）
--
-- 2026-10-25 夜 管理者のご決定（テスト番号の話し中・電源オフ・圏外は、こちらの回線から発信できた証拠）と同じ当てはめ：
-- - 電源が切れている・圏外のお知らせ（power_off・out_of_range）⇒ こちらの回線は元気（状態の表で回線を確かめ中にしない）・番号のせいにも
--   数えない＝「お客様が出なかった」と同じ（試行は no_answer。ふつうの再発信の方針）。
-- - 「この番号は現在使われておりません」のお知らせ（not_in_use）だけが番号のせい（試行は call_failed。回線は元気・試行のせいは番号＝
--   状態の表の入口 core/health.after_call が、テスト番号を待たずに決める）。
-- - 分からない（お知らせの種類を見分けられない＝None）は今までどおり「発信に失敗」（だれのせいかは証拠で決める）。
-- 決めるのは core/attempts.end_by_notice の1か所（お知らせの名前は link/phone.NOTICES。電話の部品の担当はまだいないので、形だけ）。

ALTER TABLE salescall.attempts ADD COLUMN notice text;
ALTER TABLE salescall.attempts ADD CONSTRAINT attempts_notice
  CHECK (notice IS NULL OR (notice IN ('power_off', 'out_of_range') AND state = 'no_answer')
         OR (notice = 'not_in_use' AND state = 'call_failed'));

-- お知らせは、進行中の試行を終えるときに1回だけ残す
CREATE FUNCTION salescall.attempt_notice_once() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.notice IS DISTINCT FROM OLD.notice
     AND NOT (OLD.notice IS NULL AND salescall.is_active(OLD.state) AND NOT salescall.is_active(NEW.state)) THEN
    RAISE EXCEPTION 'お知らせの種類は、進行中の試行を終えるときに1回だけ残します' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'attempt_notice_once';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER attempt_notice_once BEFORE UPDATE OF notice ON salescall.attempts
  FOR EACH ROW EXECUTE FUNCTION salescall.attempt_notice_once();
