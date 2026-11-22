-- 0281 名前の録音の審査で、契約者ごとの「チェック不要」のいまの値を、全部の契約者について読む（2026-11-22。書いた人：CL-2。DESIGN_15-2.md の952。
--      CL-3 の連絡帳 4-1c の 72）
-- ⛔ 前は、いまの値を読めるのが審査を待つ組の一覧（name_recordings_to_review の skipped）だけで、チェック不要にした契約者の録音は一覧に出ないので、
--    その契約者を「チェックする形に戻す」入口を画面が作れなかった。⇒ いまの値の決め方は 0173 の salescall.name_review_skipped の1か所のまま、
--    それを全部の契約者について返す（読み方を画面に写さない）。読めるのは管理者だけ（システムの取引で）＝切り替えと同じ守り name_review_by_admin。
CREATE FUNCTION salescall.name_review_skip_states() RETURNS TABLE (tenant_id uuid, skipped boolean)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
BEGIN
  IF salescall.cur_actor_kind() IS DISTINCT FROM 'admin' OR salescall.cur_tenant() IS NOT NULL THEN
    RAISE EXCEPTION '名前の録音の管理者のチェックが要るかを、全部の契約者について読めるのは、管理者だけです（システムの取引で）'
      USING ERRCODE = 'insufficient_privilege', CONSTRAINT = 'name_review_by_admin';
  END IF;
  RETURN QUERY SELECT t.tenant_id, salescall.name_review_skipped(t.tenant_id) FROM salescall.tenants t ORDER BY t.tenant_id;
END $$;
REVOKE ALL ON FUNCTION salescall.name_review_skip_states() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.name_review_skip_states() TO salescall_app;
