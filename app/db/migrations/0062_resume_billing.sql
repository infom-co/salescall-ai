-- 0062 途中で切れたあと、お詫びしてすぐかけ直した電話の、時給の数え方（2026-10-18 ユーザー決定。DESIGN.md 12-1・15-2 の159）
--
-- ユーザー（2026-10-18）：「通常、依頼者が支払うのは、回線が切れてもシステムが途切れても、1人のお客さんに対して、50円ですよね」
-- 「時給では1人のお客様への総時間でいいんじゃないでしょうか！」
-- ⇒ 1本あたりの報酬（かけた1本・話せた1本）には、かけ直した電話を数えない（数え方は core/reward.py の1か所）。
--   時給には、かけ直した電話の時間も入れる（1人のお客様への総時間）。ただし「電話と電話の間」（報告を書く・リストを確かめる時間）は
--   1人のお客様に1回なので、かけ直した電話には足さない（すぐかけ直すので、間の作業が無い）。

CREATE OR REPLACE FUNCTION salescall.work_seconds(p_from timestamptz, p_to timestamptz)
RETURNS TABLE (at timestamptz, seconds numeric)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT a.claimed_at,
         extract(epoch FROM a.ended_at - greatest(a.claimed_at, p_from))
         + CASE WHEN a.purpose = 'resume' THEN 0
                ELSE coalesce((SELECT t.hourly_gap_seconds FROM salescall.reward_terms t
                               WHERE t.client_id = a.client_id AND t.valid_from <= a.claimed_at
                               ORDER BY t.valid_from DESC LIMIT 1), 0) END
  FROM salescall.attempts a
  WHERE a.client_id = salescall.cur_client() AND salescall.cur_client() IS NOT NULL
    AND a.state NOT IN ('claimed', 'released') AND a.ended_at IS NOT NULL AND a.purpose <> 'apology'
    AND a.ended_at > p_from AND a.claimed_at <= p_to
$$;
