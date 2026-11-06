-- 0188 2026-11-06 の見直し（3つに分けた第三者の見直し (A)(C) の直し。書いた人：CL-2）

-- ═════════ (C) 前の電話が今日か：電話が終わった時刻で決める・電話ごとの1か所 ═════════
-- 0186 の same_jst_day に、電話をかけ始めた時刻（claimed_at）を渡していた。23時58分にかけ始めて0時03分に切れた電話のすぐあとの
-- かけ直しで「前回は」と言った（切れたのは今日）。⇒ 電話が終わった時刻（終わっていなければかけ始めた時刻）で決める。お詫びの電話の
-- 文（core/apology._order）と、途中で切れた電話の続きの予約証（core/apology.when）の両方が、この1か所を読む。
CREATE FUNCTION salescall.call_today(p_attempt uuid) RETURNS boolean LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT salescall.same_jst_day(coalesce(a.ended_at, a.claimed_at)) FROM salescall.attempts a WHERE a.attempt_id = p_attempt
$$;
REVOKE ALL ON FUNCTION salescall.call_today(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.call_today(uuid) TO salescall_app;
