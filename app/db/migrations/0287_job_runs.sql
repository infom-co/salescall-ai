-- 0287 中央の見守り役の仕事を、いつ回し終えたか（2026-11-23。書いた人：CL-2。DESIGN_15-2.md の962。CL-3 の連絡帳 4-1c の 78・CL-2 の 4-128）
-- 画面の見張りに「見守り役：最後に仕事を回した時刻」を出し、遅れていれば「気をつけること」に数えるため。仕事を返したとき（return_job）に、回し終えた
-- 時刻を残す（誤りで終えた回も「回した」＝誤りは修正依頼で別に知らせる）。読むのは salescall.job_runs の1か所：管理者の全体の取引で、すべての仕事・
-- 契約者の取引で、その契約者の仕事（名前:契約者）だけ。遅れの決まり（間隔の何倍か）は見守り役の仕事の表の1か所（supervisor/jobs の late_jobs）。
ALTER TABLE salescall.job_leases ADD COLUMN done_at timestamptz;

CREATE OR REPLACE FUNCTION salescall.return_job(p_key text, p_holder text) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
DECLARE got boolean;
BEGIN
  PERFORM salescall.job_key_allowed(p_key);
  UPDATE salescall.job_leases SET expires_at = now(), done_at = now() WHERE job_key = p_key AND holder = p_holder RETURNING true INTO got;
  RETURN got;
END $$;

CREATE FUNCTION salescall.job_runs() RETURNS TABLE (job_key text, done_at timestamptz)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
BEGIN
  IF salescall.cur_tenant() IS NULL THEN
    IF salescall.cur_actor_kind() IS DISTINCT FROM 'admin' THEN
      RAISE EXCEPTION 'すべての見守り役の仕事を読めるのは、管理者だけです（全体の取引で）' USING ERRCODE = 'insufficient_privilege',
        CONSTRAINT = 'job_runs_by_admin';
    END IF;
    RETURN QUERY SELECT l.job_key, l.done_at FROM salescall.job_leases l ORDER BY l.job_key;
  ELSE
    RETURN QUERY SELECT l.job_key, l.done_at FROM salescall.job_leases l
                  WHERE l.job_key LIKE '%:' || salescall.cur_tenant()::text ORDER BY l.job_key;
  END IF;
END $$;
REVOKE ALL ON FUNCTION salescall.job_runs() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.job_runs() TO salescall_app;
