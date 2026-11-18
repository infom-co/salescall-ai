-- 0038 「断られたかが、まだ決まっていない発信」の判断を1か所に（2026-10-16 の見直しで見つけた穴。DESIGN.md 5-2-2 の7・15-2 の106）。
-- 書いた人：CL-2
-- - 前は、同じお客様のほかの行を守る判断（dnc.unsettled_reach_covers）が「締めが済んだか（is_settled＝結果の版がある）」を見ていた。
--   判定できない（unknown）の結果の版も「済み」なので、会話の報告が失われた通話（中で「もうかけないで」と言われたかもしれない）の
--   あと、同じお客様の別の行が、5分後にかけうる。⇒ 判定できないの版が最新なら、断られたかは「まだ決まっていない」。
-- - 締めの処理が次に締める発信を探すとき・同じ行の次の発信を待つときは、これまでどおり is_settled（判定できない行は、判定待ちで別に止まる）。
CREATE FUNCTION salescall.refusal_undecided(p_attempt uuid) RETURNS boolean
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT NOT salescall.is_settled(p_attempt)
      OR (SELECT r.business_state FROM salescall.task_results r WHERE r.attempt_id = p_attempt
          ORDER BY r.version_no DESC LIMIT 1) = 'unknown'
$$;
GRANT EXECUTE ON FUNCTION salescall.refusal_undecided(uuid) TO salescall_app;
