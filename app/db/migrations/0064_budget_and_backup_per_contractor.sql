-- 0064 費用の見張りと控えを、契約者ごとにする（2026-10-18 ユーザー：「費用の見張りと控えは、契約者ごとですよ」。DESIGN.md 3-10・5-4・12-1・
-- 15-2 の161。書いた人：CL-2）
--
-- 前は、見守り役の「費用の見張り」と「控え」を、すべての契約者に共通の仕事として管理者の PC だけで動かしていた。
-- 契約者は、自分の会話AIのキーと回線を使い（費用は契約者が払う＝Intent.md 1-4）、自分の依頼者のデータを自分の PC のハードディスクに
-- 置く（控え）。⇒ どちらも、その契約者の PC の見守り役が、その契約者の分だけをする。
--
-- (1) 費用の1日の上限：「全体（すべての契約者の合計）」を「その契約者の合計」にする（budget.system_daily_yen ⇒ budget.tenant_daily_yen。
--     契約者ごとに変えられる。超えたら、その契約者に注意を知らせる。止めない＝2026-10-16 ユーザー「注意喚起のみ」）。
--     すべての契約者の合計は、誰が払う額でもないので見ない。
-- (2) 費用の見張りが契約者の取引で読めるように：今日のその契約者の費用・まだ閉じていない赤字の警告・システムが止めた停止を、
--     その契約者の取引からは、その契約者の分だけ返す（全体の取引からは、これまでどおりすべて）。
-- (3) 控えの記録に契約者を持たせる：依頼者ごとの AI用・提出用・古い控えの片付け・ハードディスク・鍵の記録は、契約者の取引で書き、
--     その契約者（と管理者）が読める。データベース全体の控え（system）は、ほかの契約者のデータも入るので、契約者を持たない
--     （管理者の PC。データベースを契約者ごとに分けたら、その契約者のデータベースの控えになる＝Intent.md 1-4）。
--     鍵が入っていない記録を「データベース全体」の種類で書いていたので、種類「鍵」（key）を足す。
-- (4) ほかの契約者の情報も読める控えの関数のうち、もう使わないもの（提出用の待ち・フォルダの持ち主）を外す（契約者の取引で、
--     自分の依頼者だけを読む）。

-- ═════════ (1) 費用の1日の上限は契約者ごと ═════════
ALTER TABLE salescall.setting_values DROP CONSTRAINT setting_values_key_fkey;
ALTER TABLE salescall.setting_definitions DISABLE TRIGGER setting_definitions_frozen;
ALTER TABLE salescall.setting_values DISABLE TRIGGER setting_values_frozen;
UPDATE salescall.setting_definitions SET key = 'budget.tenant_daily_yen', finest_scope = 'tenant',
  meaning = 'その契約者の1日の費用の上限（円。その契約者のすべての依頼者の合計。超えたら、その契約者に注意を知らせる。発信は止めない＝2026-10-16 ユーザー「注意喚起のみ」）'
WHERE key = 'budget.system_daily_yen';
UPDATE salescall.setting_values SET key = 'budget.tenant_daily_yen',
  rationale = rationale || '（2026-10-18 ユーザー：「費用の見張りと控えは、契約者ごとですよ」⇒ 全体の上限を、契約者ごとの上限にした）'
WHERE key = 'budget.system_daily_yen';
ALTER TABLE salescall.setting_values ENABLE TRIGGER setting_values_frozen;
ALTER TABLE salescall.setting_definitions ENABLE TRIGGER setting_definitions_frozen;
ALTER TABLE salescall.setting_values ADD CONSTRAINT setting_values_key_fkey
  FOREIGN KEY (key) REFERENCES salescall.setting_definitions (key);

-- ═════════ (2) 費用の見張りは契約者の取引で ═════════
DROP FUNCTION salescall.spent_today_all();
-- 今日（日本時間）の、その契約者の費用（その契約者の取引だけ。依頼者の取引・全体の取引では NULL）
CREATE FUNCTION salescall.spent_today_tenant() RETURNS numeric
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT CASE WHEN salescall.cur_tenant() IS NULL OR salescall.cur_client() IS NOT NULL THEN NULL ELSE
    (SELECT coalesce(sum(amount_yen), 0) FROM salescall.usage_entries
     WHERE tenant_id = salescall.cur_tenant() AND salescall.jst(occurred_at)::date = salescall.jst(now())::date) END
$$;
REVOKE ALL ON FUNCTION salescall.spent_today_tenant() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.spent_today_tenant() TO salescall_app;

-- 全体の取引からはすべて、契約者の取引からはその契約者の分だけ（依頼者の取引からは何も返さない）
CREATE OR REPLACE FUNCTION salescall.open_deficit_warnings() RETURNS TABLE (tenant_id uuid, client_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT w.tenant_id, w.client_id FROM salescall.deficit_warnings w
  WHERE w.closed_at IS NULL AND salescall.cur_client() IS NULL
    AND (salescall.cur_tenant() IS NULL OR w.tenant_id = salescall.cur_tenant())
$$;
CREATE OR REPLACE FUNCTION salescall.open_system_stops(p_cause text) RETURNS TABLE (event_id bigint, scope text, tenant_id uuid,
                                                                                   client_id uuid, stopped_on date)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT s.event_id, s.scope::text, s.tenant_id, s.client_id, salescall.jst(s.at)::date FROM salescall.open_stop_events s
  WHERE s.cause::text = p_cause AND s.actor_kind = 'system' AND salescall.cur_client() IS NULL
    AND (salescall.cur_tenant() IS NULL OR s.tenant_id = salescall.cur_tenant())
$$;

-- ═════════ (3) 控えの記録に契約者 ═════════
ALTER TABLE salescall.backup_runs ADD COLUMN tenant_id uuid REFERENCES salescall.tenants (tenant_id);
ALTER TABLE salescall.backup_runs DISABLE TRIGGER backup_runs_frozen;
UPDATE salescall.backup_runs r SET tenant_id = c.tenant_id FROM salescall.clients c WHERE c.client_id = r.client_id;
ALTER TABLE salescall.backup_runs ENABLE TRIGGER backup_runs_frozen;
ALTER TABLE salescall.backup_runs DROP CONSTRAINT backup_runs_kind_check;
ALTER TABLE salescall.backup_runs
  ADD CONSTRAINT backup_runs_kind_check CHECK (kind IN ('system', 'client_ai', 'submission', 'prune', 'drive', 'key')),
  -- データベース全体は契約者を持たない（ほかの契約者のデータも入る）。依頼者があれば契約者もある
  ADD CONSTRAINT backup_runs_whole_has_no_tenant CHECK (kind <> 'system' OR tenant_id IS NULL),
  ADD CONSTRAINT backup_runs_client_has_tenant CHECK (client_id IS NULL OR tenant_id IS NOT NULL),
  ADD CONSTRAINT backup_runs_client_kinds_have_tenant CHECK (kind NOT IN ('client_ai', 'submission') OR tenant_id IS NOT NULL);
ALTER TABLE salescall.backup_runs ADD CONSTRAINT backup_runs_client_fk
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id);
CREATE INDEX backup_runs_by_part ON salescall.backup_runs (kind, tenant_id, client_id, started_at);

-- 読める：全体の取引（管理者・システムの処理）はすべて。契約者の取引は、その契約者の記録（依頼者の取引は、その依頼者の記録）。
-- 書ける：システムの処理だけ。契約者のある記録は、その契約者の取引で（依頼者のある記録は、その依頼者の取引で）
DROP POLICY backup_scope ON salescall.backup_runs;
CREATE POLICY backup_scope ON salescall.backup_runs
  USING ((salescall.cur_tenant() IS NULL AND salescall.cur_actor_kind() IN ('admin', 'system'))
         OR (tenant_id = salescall.cur_tenant() AND (salescall.cur_client() IS NULL OR client_id = salescall.cur_client())))
  WITH CHECK (salescall.cur_actor_kind() = 'system' AND tenant_id IS NOT DISTINCT FROM salescall.cur_tenant()
              AND client_id IS NOT DISTINCT FROM coalesce(salescall.cur_client(), client_id));

-- ═════════ (4) 使わなくなった関数 ═════════
DROP FUNCTION salescall.submissions_waiting();
DROP FUNCTION salescall.backup_folder_owner(text);

-- ═════════ (5) 知らせの「同じ出来事は1回だけ」は、契約者ごと ═════════
-- 2026-10-18 に見つけた穴：知らせの鍵（event_key）が、すべての契約者で1つの並びだった。鍵に契約者を入れていない知らせ（その日の
-- 費用の上限・単価が入っていない・控えが取れない など）は、2人目の契約者の同じ日の知らせが黙って落ちていた（重なりの行は、
-- ほかの契約者のものなので見えない）。管理者あての写しも、どの契約者の出来事でも同じ鍵なら1つにまとまっていた。
-- ⇒ 同じ出来事かは「鍵・契約者・（管理者あての写しは）出どころの契約者」で決める。決めるのはこの一意の約束の1か所
--   （呼ぶ側の鍵に契約者を入れて回ることはしない）。
ALTER TABLE salescall.notification_events
  ADD COLUMN origin_tenant uuid REFERENCES salescall.tenants (tenant_id),   -- 管理者あての写しの、出どころの契約者（全体の出来事は NULL）
  ADD CONSTRAINT notification_origin_only_for_admin CHECK (origin_tenant IS NULL OR for_admin);
ALTER TABLE salescall.notification_events DISABLE TRIGGER USER;
UPDATE salescall.notification_events SET origin_tenant = (refs ->> 'tenant_id')::uuid WHERE for_admin AND refs ->> 'tenant_id' IS NOT NULL;
ALTER TABLE salescall.notification_events ENABLE TRIGGER USER;
ALTER TABLE salescall.notification_events DROP CONSTRAINT notification_events_event_key_key;
CREATE UNIQUE INDEX notification_events_one ON salescall.notification_events (event_key, tenant_id, origin_tenant) NULLS NOT DISTINCT;

CREATE OR REPLACE FUNCTION salescall.notify_admin(p_kind text, p_key text, p_summary text, p_refs jsonb) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
DECLARE got bigint;
BEGIN
  INSERT INTO salescall.notification_events (tenant_id, client_id, kind, event_key, summary, refs, for_admin, origin_tenant)
  VALUES (NULL, NULL, p_kind, 'admin:' || p_key, p_summary,
          coalesce(p_refs, '{}'::jsonb) || jsonb_build_object('tenant_id', salescall.cur_tenant(), 'client_id', salescall.cur_client()),
          true, salescall.cur_tenant())
  ON CONFLICT (event_key, tenant_id, origin_tenant) DO NOTHING RETURNING event_id INTO got;
  RETURN got IS NOT NULL;
END $$;

-- ═════════ (6) 表の作りの版（契約者の控えの見出しに書く。アプリの役は、移行の記録の表そのものは読めない） ═════════
CREATE FUNCTION salescall.schema_version() RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT max(name) FROM public.salescall_migrations
$$;
REVOKE ALL ON FUNCTION salescall.schema_version() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.schema_version() TO salescall_app;
