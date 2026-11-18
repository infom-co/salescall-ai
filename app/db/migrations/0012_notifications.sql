-- 0012 知らせの台帳（Q23「運営者へ、何を・いつ知らせるか」。2026-10-14 ユーザー承認）。書いた人：CL-2（データの形）
-- - 出来事（止めた・解いた・品質の段・判定できない・暦を取れない など）を、出来事の鍵で1回だけ入れる（二重に届かない）。
--   入れるのは core/notify.py だけ（停止・品質の段・締めなどの処理は、出来事を入れるだけで、自分で送らない）。
-- - 送るのは CL-3 の出力の処理（output/）。送り方ごとの送信の記録（状態・回数・送れなかった理由）は notification_deliveries。
-- - 知らせを受けるのは運営者（契約者）。依頼主ごとの出来事も、その契約者の取引で一覧できる（依頼主の取引では、その依頼主の分だけ）。
--   全体の出来事（保存の失敗など）は契約者を持たない行（どの取引からも読める）。
-- - 要約と根拠に、相手の電話番号・名前・発話の中身を入れない（画面と知らせに出すため。中身は試行・行のIDで指す。CL-3 の 4-1e の5）。
CREATE TABLE salescall.notification_events (
  event_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id   uuid REFERENCES salescall.tenants,            -- NULL＝全体の出来事
  client_id   uuid,
  kind        text NOT NULL CHECK (kind ~ '^[a-z_]+$'),      -- 種類の名前（一覧の正本は core/notify.py の KINDS）
  event_key   text NOT NULL UNIQUE CHECK (btrim(event_key) <> ''),   -- 同じ出来事は1回だけ
  summary     text NOT NULL CHECK (btrim(summary) <> ''),    -- 日本語の要約（そのまま画面・知らせに出す）
  refs        jsonb NOT NULL DEFAULT '{}'::jsonb,            -- 指すもの（停止の番号・版・試行のID など）
  occurred_at timestamptz NOT NULL DEFAULT now(),
  CHECK (client_id IS NULL OR tenant_id IS NOT NULL),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id)
);
CREATE INDEX notification_events_by_tenant ON salescall.notification_events (tenant_id, event_id);

CREATE TABLE salescall.notification_deliveries (   -- 送り方ごとの送信の記録（書くのは output/。CL-3）
  delivery_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  event_id    bigint NOT NULL REFERENCES salescall.notification_events,
  channel     text NOT NULL CHECK (channel IN ('sms', 'dashboard')),   -- 2026-10-13 ユーザー決定：ショートメールとダッシュボード
  status      text NOT NULL CHECK (status IN ('sent', 'failed', 'gave_up')),
  attempt_no  integer NOT NULL CHECK (attempt_no >= 1),
  error       text CHECK (error IS NULL OR btrim(error) <> ''),
  recorded_at timestamptz NOT NULL DEFAULT now(),
  CHECK ((status = 'sent') = (error IS NULL)),
  UNIQUE (event_id, channel, attempt_no)
);

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['notification_events', 'notification_deliveries'] LOOP
    EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON salescall.%I
                      FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change()', t || '_frozen', t);
    EXECUTE format('ALTER TABLE salescall.%I ENABLE ROW LEVEL SECURITY', t);
  END LOOP;
END $$;
-- 全体の出来事は誰でも読める。契約者の出来事は、その契約者の取引（依頼主の取引なら、その依頼主の分か契約者全体の分）だけ。
CREATE POLICY notice_scope ON salescall.notification_events
  USING (tenant_id IS NULL
         OR (tenant_id = salescall.cur_tenant()
             AND (client_id IS NULL OR salescall.cur_client() IS NULL OR client_id = salescall.cur_client())))
  WITH CHECK ((tenant_id IS NULL AND salescall.cur_tenant() IS NULL)
              OR (tenant_id = salescall.cur_tenant()
                  AND client_id IS NOT DISTINCT FROM salescall.cur_client()));
CREATE POLICY notice_scope ON salescall.notification_deliveries
  USING (EXISTS (SELECT 1 FROM salescall.notification_events e WHERE e.event_id = notification_deliveries.event_id))
  WITH CHECK (EXISTS (SELECT 1 FROM salescall.notification_events e WHERE e.event_id = notification_deliveries.event_id));
GRANT SELECT, INSERT ON salescall.notification_events, salescall.notification_deliveries TO salescall_app;
