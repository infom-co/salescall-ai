-- 0102 見直し8（2026-10-19。ユーザー：「本当にintent.mdを全部読み込んで、ここまでのやりとりをしっかりと理解できていますか？」で、
-- 計画書の決まりを1つずつ作りと突き合わせて見つけた不備を直す。DESIGN.md 15-2 の214〜。書いた人：CL-2）

-- (1) 控えが取れているかの見張り（Intent.md 14-1「取れなかったら知らせる」）：全体の取引は契約者の表を読めないので、見張りを始めて
--     よい時刻（最初の契約者を作った時刻）だけを返す関数。契約者の中身は返さない。
CREATE FUNCTION salescall.first_tenant_at() RETURNS timestamptz
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT min(created_at) FROM salescall.tenants
$$;
REVOKE ALL ON FUNCTION salescall.first_tenant_at() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.first_tenant_at() TO salescall_app;

-- (2) 意味の判定の費用を、利用量の台帳に書けなかった（Intent.md 12-2：Claude Sonnet 5.5・3.5 Flash Lite・Jev の判定の費用は変動費。
--     ✅ 2026-10-18・10-19 ユーザー承認）。出どころに「意味の判定」（judge）を足す。無料枠・有料枠は会話AI・別の耳と同じく持つ。
ALTER TABLE salescall.price_versions DROP CONSTRAINT price_versions_source_check;
ALTER TABLE salescall.price_versions ADD CONSTRAINT price_versions_source_check CHECK (source IN ('ai', 'ear', 'line', 'judge'));
ALTER TABLE salescall.price_versions DROP CONSTRAINT price_versions_tier;
ALTER TABLE salescall.price_versions ADD CONSTRAINT price_versions_tier CHECK ((source IN ('ai', 'ear', 'judge')) = (tier IS NOT NULL));
ALTER TABLE salescall.usage_entries DROP CONSTRAINT usage_entries_source_check;
ALTER TABLE salescall.usage_entries ADD CONSTRAINT usage_entries_source_check CHECK (source IN ('ai', 'ear', 'line', 'judge'));

-- (3) 依頼者をまたいだ間隔は、2026-10-17 にユーザーが値と範囲を決めた（「依頼者をまたいだ間隔 3日　契約者」）のに、仮のままだった
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('gate.cross_client_interval', 'system', '259200', 'decided',
 '2026-10-17 ユーザー決定：「依頼者をまたいだ間隔 3日　契約者」（初めの値。契約者ごとに変えられる）',
 '依頼者が増えて、この理由で止めた件数を見て', 'CL-2');
