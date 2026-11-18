-- 0162 見直し26a（電話の一生を動かして確かめた）の穴を直す。書いた人：CL-2。設計書 7-1c・2章 Q49・15-2 の387〜。
--
-- (1) 確かめのかけ直しの答えを待っているか（salescall.unclear_check_pending の1か所）に、「この行へは、もう確かめのかけ直しがかからない」＝
--     門と同じ「かけない」の判断（salescall.task_excluded_now。強制かけない・かけないで、いまの予定が新しい営業の電話）を足す。⛔ 行を
--     「強制かけない」にすると、確かめのかけ直しは二度とかからないのに「待っている」のまま残り、同じお客様のほかの行が門でも候補でも
--     永久に止まり、だれにも知らされなかった（設計書 Q49「確かめのかけ直しが一度もかからないまま行が終わった通話は、もう待たない」の
--     穴）。印を「強制かけない」にしたときは、行が終わったときと同じく、確定の印の無い結果の版を確かめ直す（core/exclusions.mark）。
CREATE OR REPLACE FUNCTION salescall.unclear_check_pending(p_attempt uuid) RETURNS boolean
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT EXISTS (
    SELECT 1 FROM salescall.unclear_checks u
    WHERE u.attempt_id = p_attempt
      AND NOT EXISTS (SELECT 1 FROM salescall.attempts c
                      WHERE c.task_id = u.task_id AND c.claimed_at > u.decided_at AND salescall.is_dialed(c.state)
                        AND NOT salescall.trouble_ours(c.trouble) AND salescall.is_settled(c.attempt_id))
      AND coalesce((SELECT n.kind::text FROM salescall.current_next_actions n WHERE n.task_id = u.task_id), 'call') <> 'end'
      AND NOT salescall.task_excluded_now(u.task_id))
$$;
