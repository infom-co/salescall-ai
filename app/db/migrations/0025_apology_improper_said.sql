-- 0025 お詫びの案に、正しい案内の無いこと（失礼な言葉など）として実際に言ってしまったことを持たせる（2026-10-16 ユーザー：
-- 「何が不適切なのかが相手に伝わらない、お詫びは、お詫びになりません」）。書いた人：CL-2
-- - これまでの improper（言ったか＝はい／いいえ）を、言ってしまったことの並び（["…", …]。空＝言っていない）に替える。
--   文面は「先ほどのお電話で、「…」といった不適切な発言をしてしまいました。…」（core/apology.py の1か所）。
ALTER TABLE salescall.apology_orders DROP CONSTRAINT apology_orders_check;
ALTER TABLE salescall.apology_orders DROP COLUMN improper;
ALTER TABLE salescall.apology_orders
  ADD COLUMN improper jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(improper) = 'array'),
                                                   -- 正しい案内の無いことで、実際に言ってしまったこと（["…", …]）
  ADD CONSTRAINT apology_orders_has_content CHECK (jsonb_array_length(pairs) >= 1 OR jsonb_array_length(improper) >= 1);
