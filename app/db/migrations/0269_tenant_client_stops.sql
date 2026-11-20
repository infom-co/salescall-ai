-- 0269 依頼者・業務の範囲のまだ解かれていない停止を、契約者の取引で一度に読む（2026-11-20。書いた人：CL-2。DESIGN_15-2 の925。CL-3 の連絡帳 4-1 の 8・CL-2 の連絡帳 4-452）
--
-- 依頼者・業務の範囲の停止は、その依頼者の取引でしか見えない（0007 の stop_scope）。画面は依頼者ごとの取引を合わせて読んでいて、依頼者が多いと遅い。
-- ⇒ 契約者の取引（依頼者を持たない取引）でだけ、その契約者の依頼者・業務の範囲の停止の番号・原因・範囲・依頼者・業務・時刻を返す（理由の文は返さない。
--    中身は依頼者の取引で読む）。「まだ解かれていない」の決め方は今の1か所（salescall.open_stop_events）。ログインした方は契約者の単位なので、見せてよい範囲は変わらない。
CREATE FUNCTION salescall.tenant_client_stops()
RETURNS TABLE (event_id bigint, scope text, cause text, client_id uuid, business_id uuid, at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT s.event_id, s.scope::text, s.cause::text, s.client_id, s.business_id, s.at FROM salescall.open_stop_events s
  WHERE s.scope IN ('client', 'business') AND salescall.cur_tenant() IS NOT NULL AND salescall.cur_client() IS NULL
    AND s.tenant_id = salescall.cur_tenant()
  ORDER BY s.event_id
$$;
REVOKE ALL ON FUNCTION salescall.tenant_client_stops() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.tenant_client_stops() TO salescall_app;
