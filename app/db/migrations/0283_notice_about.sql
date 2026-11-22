-- 0283 知らせの文の頭の名指し（依頼者・業務）を引く1か所（2026-11-22。書いた人：CL-2。DESIGN_15-2.md の954。CL-2 の連絡帳 4-470 の (5) への
--      CL-3 の答え (イ)＝知らせの入口 notify.emit で文に付ける）
-- ⛔ 初めは名指しを取引の見える範囲で引いた（core/notify.about が表を直に読む）。依頼者ごとの表（業務・実施回・お客様の行・通話）は、行ごとの
--    守り client_scope で、その依頼者の取引でしか見えない。契約者の範囲の取引（状態の表の見守り・費用の見張りなど）が出す知らせは、指す業務を
--    引けずに止まった（試験 test_notify で分かった）。⇒ 印から業務・依頼者を引くのは、この関数の1か所にする。
-- 守り：返すのは、取引の契約者の依頼者・業務の名前だけ（依頼者の取引なら、その依頼者だけ）。契約者を持たない取引（全体の取引）は、管理者か
--   システムの処理しか開けない（読み書きの層とデータベースの守り）ので、すべての契約者の名前を引ける。印は、つながりの約束（同じ契約者・同じ依頼者の親を指す）で同じ依頼者の中を通るので、最後に引いた業務・依頼者の
--   範囲だけを確かめればよい。ほかの契約者の印を渡しても、何も返らない（名前の有無から、ほかの契約者の印の有無は分からない）。
-- 引く順（細かい印から）：通話 → お客様の行 → 実施回 → 業務。業務設定の版も業務を指す。差し込む値に業務の印があれば、それが先。
CREATE FUNCTION salescall.notice_about(p_attempt uuid, p_task uuid, p_run uuid, p_business uuid, p_version uuid, p_client uuid)
RETURNS TABLE (pointed boolean, business_id uuid, business_name text, business_client uuid, client_id uuid, client_name text)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
DECLARE
  v_business uuid;
  v_bclient uuid;
  v_bname text;
  v_client uuid;
  v_cname text;
BEGIN
  v_business := COALESCE(p_business,
    (SELECT r.business_id FROM salescall.runs r WHERE r.run_id = COALESCE(p_run,
       (SELECT t.run_id FROM salescall.tasks t WHERE t.task_id = COALESCE(p_task,
          (SELECT a.task_id FROM salescall.attempts a WHERE a.attempt_id = p_attempt))))),
    (SELECT v.business_id FROM salescall.business_versions v WHERE v.version_id = p_version));
  IF v_business IS NOT NULL THEN
    SELECT b.name, b.client_id INTO v_bname, v_bclient FROM salescall.businesses b
     WHERE b.business_id = v_business
       AND (salescall.cur_tenant() IS NULL OR b.tenant_id = salescall.cur_tenant())
       AND (salescall.cur_client() IS NULL OR b.client_id = salescall.cur_client());
  END IF;
  v_client := COALESCE(v_bclient, p_client, salescall.cur_client());
  IF v_client IS NOT NULL THEN
    SELECT c.name INTO v_cname FROM salescall.clients c
     WHERE c.client_id = v_client
       AND (salescall.cur_tenant() IS NULL OR c.tenant_id = salescall.cur_tenant())
       AND (salescall.cur_client() IS NULL OR c.client_id = salescall.cur_client());
  END IF;
  RETURN QUERY SELECT num_nonnulls(p_attempt, p_task, p_run, p_business, p_version) > 0,
                      CASE WHEN v_bname IS NULL THEN NULL ELSE v_business END, v_bname, v_bclient,
                      CASE WHEN v_cname IS NULL THEN NULL ELSE v_client END, v_cname;
END $$;
REVOKE ALL ON FUNCTION salescall.notice_about(uuid, uuid, uuid, uuid, uuid, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.notice_about(uuid, uuid, uuid, uuid, uuid, uuid) TO salescall_app;
