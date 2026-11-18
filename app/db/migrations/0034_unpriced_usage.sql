-- 0034 単価が入っていないサービスを使っても止めない（2026-10-16 ユーザー：「止める必要ないでしょ！『設定がされていません』と注意表示
-- すればいいだけ」「止めるというのは、このまま進めると、お客様に対して、影響が出てしまうということだけ」）。書いた人：CL-2
-- 利用量は量だけ記録し、単価と金額は空にする（推測で 0円にしない＝空のまま。単価を入れたら、その時刻からの記録に金額が付く）。
-- 単価が無いことは、契約者への注意の知らせで伝える（core/usage.py）。
ALTER TABLE salescall.usage_entries
  ALTER COLUMN price_id DROP NOT NULL,
  ALTER COLUMN amount_yen DROP NOT NULL,
  ADD CONSTRAINT usage_entries_priced_together CHECK ((price_id IS NULL) = (amount_yen IS NULL));
