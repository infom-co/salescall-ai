-- 0040 「断られたかが、まだ決まっていない発信」の判断の直し（2026-10-17 の見直し）。
-- 書いた人：CL-2
-- - 結果の版が無い試行（お詫びの電話は結果の版を持たない）で、比べた答えが NULL になっていた。はっきり false/true に
--   する（NULL を「決まった」と読む抜け道を作らない）。
CREATE OR REPLACE FUNCTION salescall.refusal_undecided(p_attempt uuid) RETURNS boolean
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT NOT salescall.is_settled(p_attempt)
      OR coalesce((SELECT r.business_state = 'unknown' FROM salescall.task_results r WHERE r.attempt_id = p_attempt
                   ORDER BY r.version_no DESC LIMIT 1), false)
$$;
