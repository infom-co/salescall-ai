-- 0258 業務の再発信の方針だけを変えた版を、ダッシュボードから足す（2026-11-19。書いた人：CL-2。DESIGN.md 3-7・15-2 の902。CL-3 の連絡帳 4-1c の 49）
-- 計画書 6-2「再発信の方針（2026-10-13 ユーザー承認『ダッシュボードでは一括登録できるように』）」・6-1 (4)「結果ごとのかけ直し（回数・間隔）は業務ごとの
-- ダッシュボードの設定」。方針は業務設定の版の決まり（版は変えられない）にあるので、いまの版から方針だけを変えた新しい版を足す（足す道は、ほかの
-- きっかけと同じ business_settings.derive の1か所＝確かめと登録も同じ。前の版は残る）。版を足すきっかけに retry を足す。
ALTER TABLE salescall.version_successions DROP CONSTRAINT version_successions_cause_check;
ALTER TABLE salescall.version_successions ADD CONSTRAINT version_successions_cause_check
  CHECK (cause IN ('standard', 'solution', 'override', 'retry'));
