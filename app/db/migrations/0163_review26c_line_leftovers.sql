-- 0163 見直し26c（見守り役と回線）。書いた人：CL-2。設計書 4-2・5-5・15-2 の389〜。
--
-- その回線に、予約・進行中の試行が残っているか（回線ごとの流れの毎回の頭で、後始末が要るかを見る＝core/leases.recover_left。試行は
-- 依頼者ごとの行の制限で、契約者の取引からは見えないので、持ち主の権限で、その契約者の回線の分だけを見る）。⛔ 見直し26c の 1：終わりの
-- 記録が1回書けずに残った試行で、その回線は門に「使用中」で断られ続け、見守り役を立ち上げ直すまで二度とかけなかった。
CREATE FUNCTION salescall.line_has_leftovers(p_line uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT EXISTS (SELECT 1 FROM salescall.attempts a
                 WHERE a.line_id = p_line AND a.tenant_id = salescall.cur_tenant()
                   AND (a.state = 'claimed' OR salescall.is_active(a.state)))
$$;
REVOKE ALL ON FUNCTION salescall.line_has_leftovers(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.line_has_leftovers(uuid) TO salescall_app;
