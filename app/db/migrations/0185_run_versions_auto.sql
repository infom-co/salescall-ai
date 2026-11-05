-- 0185 実施回の版の割り当てが、自動（版を足して自動で替える1か所）か、手（管理者か契約者・取込）かの印（2026-11-06 CL-2 の見直し。
-- DESIGN.md 3-8-3 の (5)）
--
-- ⛔ 0183 の穴：契約者が実施回の版を手で前の版に戻しても、決まった間隔の仕事（business_settings.adopt_ready）が、その版の後の版へ
--    また替えていた（契約者の判断を上書きする）。直し：自動で替えてよいのは、その実施回に手で版を割り当てたときより後に作った後の版
--    だけ（決めるのは adopt_ready の1か所。手の割り当ての時刻は、この印の無い割り当ての、いちばん新しい時刻）。

ALTER TABLE salescall.run_versions ADD COLUMN auto boolean NOT NULL DEFAULT false;
