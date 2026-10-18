-- 0083 表の作りの指紋（DESIGN.md 5-4-1・15-2 の179。2026-10-18 の見直し。書いた人：CL-2）
--
-- PC のプログラムとデータベースの版を、いちばん新しい表の作りの名前だけで比べていた（0064 の schema_version）。途中の1本を
-- 流し忘れた・同じ番号の別の表の作りがある、では「同じ」に見えた。
-- ⇒ 流した表の作りすべての「名前:中身の指紋」を名前の順に並べた指紋で比べる（プログラムの側は core/migrate.py の fingerprint＝
--    同じ決まり）。アプリの役は、流した記録の表そのものは読めないので、この関数だけを通す。

CREATE FUNCTION salescall.schema_fingerprint() RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT encode(sha256(convert_to(coalesce(string_agg(name || ':' || sha256 || E'\n', '' ORDER BY name), ''), 'UTF8')), 'hex')
  FROM public.salescall_migrations
$$;
REVOKE ALL ON FUNCTION salescall.schema_fingerprint() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.schema_fingerprint() TO salescall_app;
