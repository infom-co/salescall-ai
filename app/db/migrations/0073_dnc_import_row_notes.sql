-- 0073 発信禁止の一覧の取込：登録した行の注意（DESIGN.md 3-6-2・15-2 の169。2026-10-18 の見直し。書いた人：CL-2）
--
-- 前は、登録した行は理由を持てなかった（理由は、止めなかった行・もう止めていた行だけ）。見直しで2つの穴が見つかった：
-- (1) 番号が読めない行は、正しい管理番号があっても何も止めなかった ⇒ 管理番号で止め、「番号は読めず、管理番号で止めた」と残す。
-- (2) 管理番号に当たる行が無くても「登録した」とだけ出た（表計算ソフトで先頭の 0 が消えた管理番号は、別の管理番号に見える）
--     ⇒ 「この管理番号の行はまだ無い」と残し、プレビューで確かめてもらう。
-- ⇒ 登録した行も、注意（理由）を持てるようにする。止めなかった行・もう止めていた行は、前と同じく理由が必ずある。
--    理由の記号と文は、そろって有るか、そろって無い。

DO $$
DECLARE c text;
BEGIN
  SELECT conname INTO STRICT c FROM pg_constraint
  WHERE conrelid = 'salescall.dnc_import_rows'::regclass AND contype = 'c'
    AND pg_get_constraintdef(oid) LIKE '%stopped%already%reason_code IS NOT NULL%';
  EXECUTE format('ALTER TABLE salescall.dnc_import_rows DROP CONSTRAINT %I', c);
END $$;
ALTER TABLE salescall.dnc_import_rows
  ADD CONSTRAINT dnc_import_rows_reason_when_not_registered
    CHECK (outcome NOT IN ('stopped', 'already') OR reason_code IS NOT NULL),
  ADD CONSTRAINT dnc_import_rows_empty_has_no_reason CHECK (outcome <> 'empty' OR reason_code IS NULL),
  ADD CONSTRAINT dnc_import_rows_reason_pairs CHECK ((reason_code IS NULL) = (reason IS NULL));
