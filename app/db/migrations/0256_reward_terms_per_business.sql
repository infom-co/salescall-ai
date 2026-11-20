-- 0256 報酬の決まりを業務ごとに（2026-11-19。書いた人：CL-2。DESIGN.md 12-1-1・15-2 の897。CL-3 の連絡帳 4-1b の 16）
-- ✅ 2026-11-18 管理者「報酬の決まりは『業務』ごと」。⛔ 前は依頼者ごと（0020）。
-- ⇒ 報酬の決まりは業務ごと・版つき（いつから使うか）・足すだけ（今のまま）。出来事（成果1件・かけた1本・話せた1本・時給の時間）の単価は、その出来事の
--    業務の、その時刻に効いていた決まりで数える（core/reward.month の1か所・時給の電話と電話の間はデータベースの salescall.work_seconds の1か所）。
--    今月の数・費用の割合・赤字の判断は、計画書 12-2 のとおり依頼者ごと（その依頼者の業務を合わせた報酬と、依頼者の費用）。
ALTER TABLE salescall.reward_terms ADD COLUMN business_id uuid NOT NULL;
ALTER TABLE salescall.reward_terms ADD CONSTRAINT reward_terms_business
  FOREIGN KEY (client_id, business_id) REFERENCES salescall.businesses (client_id, business_id);
ALTER TABLE salescall.reward_terms DROP CONSTRAINT reward_terms_client_id_valid_from_key;
ALTER TABLE salescall.reward_terms ADD CONSTRAINT reward_terms_business_valid_from UNIQUE (business_id, valid_from);

-- 試行の業務（1か所。報酬の数え方が読む）
CREATE FUNCTION salescall.attempt_business(p_attempt uuid) RETURNS uuid LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT r.business_id FROM salescall.attempts a JOIN salescall.tasks t ON t.task_id = a.task_id JOIN salescall.runs r ON r.run_id = t.run_id
  WHERE a.attempt_id = p_attempt
$$;
REVOKE ALL ON FUNCTION salescall.attempt_business(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.attempt_business(uuid) TO salescall_app;

-- 時給の時間（電話ごと）に、その電話の業務を足す（時給の単価も、電話と電話の間も、業務の決まりで数える）。返す形が変わるので作り直す
DROP FUNCTION salescall.work_seconds(timestamptz, timestamptz);
CREATE FUNCTION salescall.work_seconds(p_from timestamptz, p_to timestamptz)
RETURNS TABLE (at timestamptz, seconds numeric, business_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT a.claimed_at,
         extract(epoch FROM a.ended_at - greatest(a.claimed_at, p_from))
         + CASE WHEN NOT salescall.counts_per_call(salescall.keeping_kind(a.purpose, a.promised)) THEN 0
                ELSE coalesce((SELECT t.hourly_gap_seconds FROM salescall.reward_terms t
                               WHERE t.business_id = salescall.attempt_business(a.attempt_id) AND t.valid_from <= a.claimed_at
                               ORDER BY t.valid_from DESC LIMIT 1), 0) END,
         salescall.attempt_business(a.attempt_id)
  FROM salescall.attempts a
  WHERE a.client_id = salescall.cur_client() AND salescall.cur_client() IS NOT NULL
    AND a.tenant_id = salescall.cur_tenant()
    AND salescall.is_dialed(a.state) AND a.ended_at IS NOT NULL
    AND salescall.counts_for_reward(salescall.keeping_kind(a.purpose, a.promised))
    AND a.ended_at > p_from AND a.claimed_at <= p_to
$$;
REVOKE ALL ON FUNCTION salescall.work_seconds(timestamptz, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.work_seconds(timestamptz, timestamptz) TO salescall_app;
