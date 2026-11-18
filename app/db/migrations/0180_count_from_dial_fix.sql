-- 0180 行の回数を、かける番号を直したときから数え直す（2026-11-06 ユーザー決定。書いた人：CL-2。DESIGN.md 3-4-1）
--
-- ✅ ユーザー：「終えた行を新しい番号で戻す場合、その行の「かけた回数」…はどう数えますか」⇒「新しい番号で数え直す」。
--    「お客様と約束したかけ直しで終えた行は、「決めた回数（日数）までかけて終えた行」であれば、新しい番号で再度0回からかけ直します。」
-- 行の回数（行の回数の上限・再発信の方針の回数・時刻の決まっていない約束の回数）を数え始める時刻は、この関数の1か所（いちばん新しい
-- かける番号の直しの時刻・直していなければ、はじめから）。数える処理（門の pacing・次の動きの _times・_said・_unspecified_calls）は、
-- どれも、これより後に予約した発信だけを数える。

CREATE FUNCTION salescall.task_count_from(p_task uuid) RETURNS timestamptz LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT coalesce((SELECT max(f.recorded_at) FROM salescall.task_dial_fixes f WHERE f.task_id = p_task), '-infinity'::timestamptz)
$$;
REVOKE ALL ON FUNCTION salescall.task_count_from(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.task_count_from(uuid) TO salescall_app;

-- 0179 のいまかける番号（salescall.task_dial_contact）も、呼んだ役の行ごとの制限の中で読む（制限を越えて読む関数は守りを呼ぶ決まり＝
-- test_foundation。越えて読む要は無い：呼ぶのは依頼者の範囲の取引と、守りの関数＝dial_number_guard の中だけ。守りの関数は持ち主の権限で動く）
ALTER FUNCTION salescall.task_dial_contact(uuid) SECURITY INVOKER;
