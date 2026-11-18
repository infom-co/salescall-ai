-- 0199 番号を直すとき、担当の方が同じか違うかを契約者が選ぶ（✅ 2026-11-07 ユーザー：伺い「相手の違い」に「番号を直す画面で選ぶ」＝
--     計画書 7-1 (19)。書いた人：CL-2。DESIGN.md 3-4-1・15-2 の512）
--
-- ⛔ 前は「担当の相手が違う」（7-1 (8)「担当の相手が電話番号で違うのであれば、最初から」・(12)）を、お呼びする方の名前・役職・部署を
--    直したかだけで決めていた。番号だけ直すと、新しい番号の先が別の担当の方でも「同じ相手」になり、約束の時刻のまま続け、お詫びもした。
-- ⇒ かける番号の直しに、契約者が選んだ「担当の方は同じか」（same_person）を持つ。読むのは「お呼びする方が替わったか」の1か所
--    （core/next_action._target_changed_since）。前に直した行は、選ぶ形が無かったので「同じ」（前の動きのまま）。
ALTER TABLE salescall.task_dial_fixes ADD COLUMN same_person boolean NOT NULL DEFAULT true;
ALTER TABLE salescall.task_dial_fixes ALTER COLUMN same_person DROP DEFAULT;   -- これからの直しは、契約者が必ず選ぶ
