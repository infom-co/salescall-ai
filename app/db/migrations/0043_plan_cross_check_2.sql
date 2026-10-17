-- 0043 計画書との突き合わせで見つかった食い違いを直す（続き。2026-10-17。書いた人：CL-2）
-- （0042 で足した発信禁止の理由 fax_line は、足した取引の中では使えないので、使う決まりはここに書く）
--
-- (1) FAX の回線の発信禁止も、管理番号では止めない（番号違いと同じく、その番号だけ。行のお客様が断ったのではない）。
-- (2) お客様と約束したかけ直しかを、試行に写して持つ（行の回数の上限・1日の回数に数えない＝2026-10-14 ユーザー：「かけ直しの約束を
--     相手側にした場合やかけ直しを依頼された場合は対象外」。前は、かけたあと回数に数えていた）。写すのは門の1か所。声の型と同じく、
--     あとから変えられない。
-- (3) ほかの依頼者がかけた声・発信番号の確かめ（0039）：
--     - 声は、お客様が出た発信だけ（出なかった発信の声は、お客様が聞いていない。前は出なかった発信でも声を替えていた）。
--     - 発信番号は、お客様の電話を鳴らした発信だけ（発信に失敗した発信の番号は、お客様に出ていない。出なかった発信は、着信の
--       履歴に番号が残るので数える）。
-- (4) 検証モードの行の承認は、管理者か契約者（お詫びの承認・発信禁止の取り消しと同じ。管理者と契約者の違いはログインだけ＝
--     Intent.md 1-4）。お詫びの承認の守りが、誰が操作したかが空のとき通してしまう形だったのも直す。
-- (5) 確定した印に、検証の段階か本番かを持たせる（検証の段階の結果は、依頼者へ何も渡さない＝2026-10-16 ユーザー。出力の型 CL-3 が
--     この印で分けられるように）。決めるのは承認の部分の1か所（approval._release）。
-- (6) 法人／個人の持ち場所を1つにする：業務の表の target_kind（作った当初の守り。どこからも読まれていない）を外し、データベースの
--     守り（いまは法人だけ）を、正本である業務設定の中身（target_class）に置く。

-- (1)
ALTER TABLE salescall.dnc_entries DROP CONSTRAINT dnc_entries_ref_not_wrong_party;
ALTER TABLE salescall.dnc_entries
  ADD CONSTRAINT dnc_entries_ref_not_number_only CHECK (client_ref IS NULL OR reason NOT IN ('wrong_party', 'fax_line'));

-- (2)
ALTER TABLE salescall.attempts ADD COLUMN promised boolean NOT NULL DEFAULT false;
CREATE FUNCTION salescall.attempt_record_fixed() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (NEW.promised, NEW.voice_type) IS DISTINCT FROM (OLD.promised, OLD.voice_type) THEN
    RAISE EXCEPTION '試行の、約束したかけ直しかと声の型は変えられません' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER attempt_record_fixed BEFORE UPDATE ON salescall.attempts
  FOR EACH ROW EXECUTE FUNCTION salescall.attempt_record_fixed();

-- (3)
CREATE OR REPLACE FUNCTION salescall.number_last_voice_elsewhere(p_phone text) RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT v.voice_name FROM salescall.attempts a JOIN salescall.voice_types v ON v.code = a.voice_type
  WHERE a.phone_e164 = p_phone AND a.client_id IS DISTINCT FROM salescall.cur_client()
    AND a.state NOT IN ('claimed', 'released') AND salescall.reached_answer(a.attempt_id)
  ORDER BY a.claimed_at DESC LIMIT 1
$$;
CREATE OR REPLACE FUNCTION salescall.line_called_number_elsewhere(p_line uuid, p_phone text) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT EXISTS (SELECT 1 FROM salescall.attempts a
                 WHERE a.line_id = p_line AND a.tenant_id = salescall.cur_tenant() AND a.phone_e164 = p_phone
                   AND a.client_id IS DISTINCT FROM salescall.cur_client() AND a.state NOT IN ('claimed', 'released')
                   AND EXISTS (SELECT 1 FROM salescall.attempt_state_history h
                               WHERE h.attempt_id = a.attempt_id AND h.to_state = 'ringing'))
$$;

-- (4)
CREATE OR REPLACE FUNCTION salescall.task_approval_by_operator() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF coalesce(salescall.cur_actor_kind()::text, '') NOT IN ('operator', 'admin')
     OR NEW.approved_by IS DISTINCT FROM salescall.cur_actor() THEN
    RAISE EXCEPTION '行を承認できるのは管理者か契約者だけです（承認した者は、取引の管理者か契約者）' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'task_approvals_by_operator';
  END IF;
  RETURN NEW;
END $$;
CREATE OR REPLACE FUNCTION salescall.apology_approval_by_person() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF coalesce(salescall.cur_actor_kind()::text, '') NOT IN ('operator', 'admin')
     OR NEW.approved_by IS DISTINCT FROM salescall.cur_actor() THEN
    RAISE EXCEPTION 'お詫びの電話を承認するのは、管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'apology_approval_by_person';
  END IF;
  RETURN NEW;
END $$;

-- (5)
ALTER TABLE salescall.result_releases
  ADD COLUMN stage text NOT NULL DEFAULT 'production' CHECK (stage IN ('verification', 'production'));
ALTER TABLE salescall.result_releases ALTER COLUMN stage DROP DEFAULT;   -- これからは、付ける処理が必ず書く

-- (6)
ALTER TABLE salescall.businesses DROP COLUMN target_kind;
ALTER TABLE salescall.business_contents
  ADD CONSTRAINT business_contents_corporate_only CHECK (content ->> 'target_class' = 'corporate');

-- (5) の続き：送り先への連携（依頼者へ渡す道）は、本番で確定した版にしか作れない（データベースが守る。処理に誤りがあっても、
--     検証の段階の結果を依頼者へ渡さない。2026-10-16 ユーザー：「依頼者に途中作業していることは絶対に知らせてはいけない」）
ALTER TABLE salescall.result_releases ADD CONSTRAINT result_releases_with_stage UNIQUE (client_id, task_id, version_no, stage);
ALTER TABLE salescall.deliveries
  ADD COLUMN release_stage text NOT NULL DEFAULT 'production' CHECK (release_stage = 'production');
ALTER TABLE salescall.deliveries
  ADD CONSTRAINT deliveries_production_release FOREIGN KEY (client_id, task_id, version_no, release_stage)
  REFERENCES salescall.result_releases (client_id, task_id, version_no, stage);
