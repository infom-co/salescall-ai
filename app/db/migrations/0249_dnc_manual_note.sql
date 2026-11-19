-- 0249 契約者・管理者が手で発信禁止に入れたとき、なぜ入れたかを残す（2026-11-19。書いた人：CL-2。DESIGN.md 3-6・15-2 の884。CL-3 の連絡帳 4-1b の 25）
-- ⛔ 前は、依頼者の発信禁止に手で入れる（理由 manual）とき、なぜ入れたかの文を残せなかった（契約者の共通の発信禁止は残せる＝0032）。
-- ⇒ 手で入れた登録だけ、その文（note）を必ず持つ（ほかの理由の登録は持たない。理由は台帳の reason＝システムが決めた理由）。
ALTER TABLE salescall.dnc_entries ADD COLUMN note text CHECK (note IS NULL OR btrim(note) <> '');
ALTER TABLE salescall.dnc_entries ADD CONSTRAINT dnc_entries_manual_note CHECK ((reason = 'manual') = (note IS NOT NULL));
