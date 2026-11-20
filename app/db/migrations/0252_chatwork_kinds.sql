-- 0252 チャットワークの部屋の値の種類と、知らせの送り方「chatwork」（2026-11-19。書いた人：CL-2。DESIGN.md 9-2・15-2 の892。CL-3 の連絡帳 4-1b の 10・11）
-- 種類の値を足すだけ（足した値は、確定したあとでないと使えない＝0253 で使う）。
ALTER TYPE salescall.setting_value_type ADD VALUE 'chatwork_room';
-- ✅ 2026-11-18 管理者：Rakuten Link のショートメールが最優先、それが使えなければチャットワーク。前は sms・dashboard だけだった
ALTER TABLE salescall.notification_deliveries DROP CONSTRAINT notification_deliveries_channel_check;
ALTER TABLE salescall.notification_deliveries ADD CONSTRAINT notification_deliveries_channel_check
  CHECK (channel IN ('sms', 'chatwork', 'dashboard'));
