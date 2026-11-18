-- 0237 ファクス番号も行の番号として持つ（断ったお客様のファクス番号も止める）（2026-11-17 の5回目の見直し。書いた人：CL-2。DESIGN.md 3-4-1・3-6・
--      15-2 の848。✅ 2026-11-17 ユーザー：伺い「お客様が『もうかけないで』と断ったとき、ファクス番号も止めるか」に「止める（お勧め）」）
--
-- ⛔ 0175〜0236：ファクス番号は番号ではない連絡先（task_contacts の kind fax）としてだけ持ち、行の番号（task_numbers＝断ったときに止める範囲・ほかの行と
--    番号を共有するか・門が見るほかの番号）に入らなかった。電話とファクスが同じ番号の会社だと、断ったお客様の番号へ、ほかの行からかけえた。
-- ⇒ ファクス番号の連絡先を足すとき（core/customer_contacts.add の1か所）、同じ番号を記録だけの番号（名札 fax。かけない）としても行に足す。止める範囲・止め直し・
--    門は、行の番号の1か所（tasks.numbers・dnc.restop）のまま効く。

ALTER TABLE salescall.task_numbers DROP CONSTRAINT task_numbers_label;
ALTER TABLE salescall.task_numbers ADD CONSTRAINT task_numbers_label CHECK (
  CASE source WHEN 'list' THEN label IN ('company', 'designated')
              WHEN 'record' THEN label IN ('company', 'mobile', 'line', 'said', 'fax')
              ELSE label IS NULL END);
