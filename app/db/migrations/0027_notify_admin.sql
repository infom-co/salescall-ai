-- 0027 管理者あての知らせ（2026-10-16 ユーザー承認：プログラムの誤りで止まったときは、管理者にも知らせる。直せるのは管理者だけ
-- のため。Intent.md 1-4）。書いた人：CL-2
-- - 知らせの台帳に「管理者あて」（for_admin）を足す。管理者あての行は契約者を持たない行で、読めるのは管理者とシステムの処理
--   （送る処理）だけ（ほかの契約者に、よその契約者の出来事を見せない）。どの契約者・依頼主の出来事かは refs に入れる。
-- - 入れるのは core/notify.py の emit の1か所（admin=True のとき、この関数で写しを入れる）。契約者の取引の中から呼べるように、
--   この関数だけが行ごとの制限を越えて書く。
ALTER TABLE salescall.notification_events
  ADD COLUMN for_admin boolean NOT NULL DEFAULT false,
  ADD CONSTRAINT notification_for_admin_is_system CHECK (NOT for_admin OR (tenant_id IS NULL AND client_id IS NULL));
DROP POLICY notice_scope ON salescall.notification_events;
CREATE POLICY notice_scope ON salescall.notification_events
  USING ((tenant_id IS NULL AND (NOT for_admin OR salescall.cur_actor_kind() IN ('admin', 'system')))
         OR (tenant_id = salescall.cur_tenant()
             AND (client_id IS NULL OR salescall.cur_client() IS NULL OR client_id = salescall.cur_client())))
  WITH CHECK ((tenant_id IS NULL AND salescall.cur_tenant() IS NULL AND NOT for_admin)
              OR (tenant_id = salescall.cur_tenant()
                  AND client_id IS NOT DISTINCT FROM salescall.cur_client()));

CREATE FUNCTION salescall.notify_admin(p_kind text, p_key text, p_summary text, p_refs jsonb) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
DECLARE got bigint;
BEGIN
  INSERT INTO salescall.notification_events (tenant_id, client_id, kind, event_key, summary, refs, for_admin)
  VALUES (NULL, NULL, p_kind, 'admin:' || p_key, p_summary,
          coalesce(p_refs, '{}'::jsonb) || jsonb_build_object('tenant_id', salescall.cur_tenant(), 'client_id', salescall.cur_client()),
          true)
  ON CONFLICT (event_key) DO NOTHING RETURNING event_id INTO got;
  RETURN got IS NOT NULL;
END $$;
REVOKE ALL ON FUNCTION salescall.notify_admin(text, text, text, jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.notify_admin(text, text, text, jsonb) TO salescall_app;
