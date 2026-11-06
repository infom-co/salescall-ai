-- 0186 前の電話が今日（日本時間）か（1か所。2026-11-06 CL-1 の連絡帳 4-99 の頼み。書いた人：CL-2）
--
-- お詫びの電話の文（「先ほど」か「先日」か＝core/apology.for_task）と、途中で切れた電話の続きの予約証の apology_when（同じ日か＝
-- core/apology.when。CL-1 の 4-99：✅ 2026-11-06 9時05分17秒 ユーザー「先ほどは（前回は）、お話の最後に通信が悪く…」）は、どちらも
-- この関数で決める（日の境は日本時間）。

CREATE FUNCTION salescall.same_jst_day(p_at timestamptz) RETURNS boolean LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT salescall.jst(p_at)::date = salescall.jst(now())::date
$$;
REVOKE ALL ON FUNCTION salescall.same_jst_day(timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.same_jst_day(timestamptz) TO salescall_app;
