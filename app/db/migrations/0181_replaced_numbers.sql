-- 0181 前にかける番号だった番号（番号を直して替えた番号）の1か所と、約束の回数の数え直し（2026-11-06 ユーザー決定。書いた人：CL-2。
-- DESIGN.md 3-4-1）
--
-- ✅ ユーザー：(1)「話せたら、新番号がメイン、旧番号はサブ」(2) 約束の電話が新しい番号へ移ったら、かけた日数（回数）は「数え直す
-- （お客様との約束は変更しない）」。
-- 前にかける番号だった番号＝一覧の番号と、これまでに直した番号のうち、いまかける番号でないもの（決めるのはこの関数の1か所。Python の
-- next_action.numbers_of の superseded も、これを読む）。行の回数（attempts.in_row_count）は、直したときより後の発信のうち、この番号へ
-- かけたもの（約束の電話は、まず前の番号でかける＝その1本）を数えない＝新しい番号で0から数える。

CREATE FUNCTION salescall.task_number_replaced(p_task uuid, p_contact uuid) RETURNS boolean LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT p_contact IS DISTINCT FROM salescall.task_dial_contact(p_task)
     AND (p_contact = (SELECT t.contact_id FROM salescall.tasks t WHERE t.task_id = p_task)
          OR EXISTS (SELECT 1 FROM salescall.task_dial_fixes f WHERE f.task_id = p_task AND f.contact_id = p_contact))
$$;
REVOKE ALL ON FUNCTION salescall.task_number_replaced(uuid, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.task_number_replaced(uuid, uuid) TO salescall_app;
