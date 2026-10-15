-- 0015 利用量の台帳と予算（Q16「いくら使ったか」。DESIGN.md 12-1・9-1 の budget）。書いた人：CL-2
-- - 単価の表（price_versions。システム共通・変更不可）：出どころ・単位ごとの単価を、いつから使うかつきで足していく。
-- - 利用量の台帳（usage_entries。依頼主ごと・変更不可）：出どころ（会話AI・別の耳・回線）・実測か推定か・量・単位・使った単価・金額。
--   金額は、記録したときの単価で決まる（あとから単価を変えても、過去の金額は変わらない）。書くのは core/usage.py だけ。
--   画面は、実測と推定を分けて出す（台帳を読むだけ。通話時間から料金を計算し直さない）。
-- - 予算：依頼主ごと・全体の「1日の上限」（日本時間の日）。超えたら止め（原因 budget）、日が替わったら見守りが解く。
CREATE TABLE salescall.price_versions (
  price_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source      text NOT NULL CHECK (source IN ('ai', 'ear', 'line')),
  unit        text NOT NULL CHECK (unit IN ('input_tokens', 'output_tokens', 'seconds')),
  yen_per_unit numeric NOT NULL CHECK (yen_per_unit >= 0),
  valid_from  timestamptz NOT NULL,
  note        text NOT NULL CHECK (btrim(note) <> ''),     -- 根拠（料金表の版・無料枠 など）
  set_by      text NOT NULL CHECK (btrim(set_by) <> ''),
  UNIQUE (source, unit, valid_from)
);
CREATE TRIGGER price_versions_frozen BEFORE UPDATE OR DELETE ON salescall.price_versions
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
ALTER TABLE salescall.price_versions ENABLE ROW LEVEL SECURITY;
CREATE POLICY price_read ON salescall.price_versions USING (true) WITH CHECK (salescall.cur_tenant() IS NULL);
GRANT SELECT, INSERT ON salescall.price_versions TO salescall_app;

CREATE TABLE salescall.usage_entries (
  tenant_id   uuid NOT NULL,
  client_id   uuid NOT NULL,
  entry_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  attempt_id  uuid,                                         -- どの通話の分か（模擬通話・試しの確かめなどは無し）
  source      text NOT NULL CHECK (source IN ('ai', 'ear', 'line')),
  kind        text NOT NULL CHECK (kind IN ('measured', 'estimated')),   -- 実測か推定か
  quantity    numeric NOT NULL CHECK (quantity >= 0),
  unit        text NOT NULL CHECK (unit IN ('input_tokens', 'output_tokens', 'seconds')),
  price_id    bigint NOT NULL REFERENCES salescall.price_versions,
  amount_yen  numeric NOT NULL CHECK (amount_yen >= 0),
  occurred_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, attempt_id) REFERENCES salescall.attempts (client_id, attempt_id)
);
CREATE INDEX usage_entries_by_day ON salescall.usage_entries (client_id, occurred_at);
CREATE TRIGGER usage_entries_frozen BEFORE UPDATE OR DELETE ON salescall.usage_entries
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
ALTER TABLE salescall.usage_entries ENABLE ROW LEVEL SECURITY;
CREATE POLICY client_scope ON salescall.usage_entries
  USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
  WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client());
GRANT SELECT, INSERT ON salescall.usage_entries TO salescall_app;

-- 全体の今日（日本時間）の金額。依頼主の範囲の外も足すので、表の持ち主の権限で数え、合計だけを返す
CREATE FUNCTION salescall.spent_today_all() RETURNS numeric
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT coalesce(sum(amount_yen), 0) FROM salescall.usage_entries
  WHERE salescall.jst(occurred_at)::date = salescall.jst(now())::date
$$;
-- まだ解かれていない費用の停止（範囲・契約者・依頼主・止めた日）。日が替わったかを、全体の取引の見守りが確かめるため
CREATE FUNCTION salescall.open_budget_stops() RETURNS TABLE (event_id bigint, scope text, tenant_id uuid, client_id uuid,
                                                            stopped_on date)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT s.event_id, s.scope::text, s.tenant_id, s.client_id, salescall.jst(s.at)::date FROM salescall.stop_events s
  WHERE s.action = 'stop' AND s.cause = 'budget' AND s.actor_kind = 'system'
    AND NOT EXISTS (SELECT 1 FROM salescall.stop_events r WHERE r.resolves_event_id = s.event_id)
$$;
REVOKE ALL ON FUNCTION salescall.spent_today_all(), salescall.open_budget_stops() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.spent_today_all(), salescall.open_budget_stops() TO salescall_app;

-- 最初の単価（2026-10-15）：会話AI・別の耳は無料枠、Rakuten Link の国内通話は無料（ナビダイヤルなど有料の番号へは、初めはかけない）
INSERT INTO salescall.price_versions (source, unit, yen_per_unit, valid_from, note, set_by) VALUES
('ai', 'input_tokens', 0, '2026-01-01', '無料枠（gemini-3.8-live。有料の枠に移るときに単価を足す。Intent.md 12-2）', 'CL-2'),
('ai', 'output_tokens', 0, '2026-01-01', '無料枠（同上）', 'CL-2'),
('ear', 'input_tokens', 0, '2026-01-01', '無料枠（別の耳 gemini-3.5-transcribe-live）', 'CL-2'),
('ear', 'output_tokens', 0, '2026-01-01', '無料枠（同上）', 'CL-2'),
('line', 'seconds', 0, '2026-01-01', 'Rakuten Link の国内通話は無料（2026-10-13 ユーザー）。有料の回線を足すときに単価を足す', 'CL-2');

-- 予算の設定（1日の上限。日本時間の日）
INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, bound_key, bound_side) VALUES
('budget.client_daily_yen', 'override', 'integer', 'その依頼主の1日の費用の上限（円。超えたら、その依頼主を止め、日が替わったら解く）', 'DESIGN.md 12-1・9-1', NULL, NULL),
('budget.system_daily_yen', 'system_only', 'integer', '全体の1日の費用の上限（円。超えたら、全体を止め、日が替わったら解く）', 'DESIGN.md 12-1・9-1', NULL, NULL);
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('budget.client_daily_yen', 'system', '1000', 'provisional', 'いまは会話AIが無料枠・Rakuten Link も無料で、費用は 0円。有料に切り替えたときの歯止めとして、仮に1日1,000円', '有料の枠に切り替える前に、ユーザーに決めていただく', 'CL-2'),
('budget.system_daily_yen', 'system', '3000', 'provisional', '同上。全体は依頼主の3倍を仮の値に', '同上', 'CL-2');
