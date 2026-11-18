-- 0228 回線の確かめのテスト番号に、お客様の番号を使わない（2026-11-16。書いた人：CL-2。DESIGN.md 17章・15-2 の835）
--
-- ✅ 2026-11-16 ユーザー「守りを足す（お勧め）」：テスト番号（設定 line.test_number）は、契約者・管理者が設定で入れる番号で、門・発信禁止・予約を通さずに
--    15分〜4時間おきに鳴らす（core/ring_test.run）。⛔ それまで：番号の形だけを確かめていたので、誤ってお客様の番号（発信禁止のお客様・いま話している
--    お客様）を入れると、そのお客様の電話を鳴らし続けた（2026-11-16 の3回目の見直し＝取り消せない害の確かめ）。
-- ⇒ その番号がお客様の番号か（1か所。設定に入れるとき＝core/settings.put と、鳴らす直前＝core/ring_test.run が読む）：取引の契約者のお客様リストの番号・
--    契約者の発信禁止の台帳（依頼者ごと・契約者の共通）の番号・いま予約から話し終えるまでの発信の番号（どの契約者でも）。答えは理由の名前だけ
--    （お客様の中身は返さない）。

CREATE FUNCTION salescall.test_number_problem(p_e164 text) RETURNS text LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = salescall, pg_temp AS $$
  SELECT CASE
    WHEN EXISTS (SELECT 1 FROM salescall.attempts a WHERE a.phone_e164 = p_e164 AND salescall.is_active(a.state)) THEN 'in_call'
    WHEN EXISTS (SELECT 1 FROM salescall.dnc_entries e WHERE e.tenant_id = salescall.cur_tenant() AND e.phone_e164 = p_e164 AND e.revoked_at IS NULL)
      OR EXISTS (SELECT 1 FROM salescall.contractor_dnc_entries e WHERE e.tenant_id = salescall.cur_tenant() AND e.phone_e164 = p_e164
                   AND e.revoked_at IS NULL) THEN 'do_not_call'
    WHEN EXISTS (SELECT 1 FROM salescall.task_numbers n WHERE n.tenant_id = salescall.cur_tenant() AND n.phone_e164 = p_e164) THEN 'customer'
  END
$$;
REVOKE ALL ON FUNCTION salescall.test_number_problem(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.test_number_problem(text) TO salescall_app;
