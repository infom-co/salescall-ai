-- 0209 表記の揺れだけの直しは、お呼びする方の替わりにしない（2026-11-08。書いた人：CL-2。DESIGN.md 3-4-1 の【0209】・2章 Q150〜・
--      15-2 の680〜）
--
-- ⛔ 0208 の person_fix_changes は、値を、前後の空白を除いた文字そのもので直前の値と比べた。なので「田中　太郎」（全角の空白）と
--    「田中 太郎」、「ﾀﾅｶ」（半角カナ）と「タナカ」、「第１営業部」と「第1営業部」、空白の有無だけの違いが「違う値」になり、同じお客様なのに
--    担当の期が替わった（引き継ぎを確かめる電話・前の担当の方個人の答えで終えた行の戻し・約束の選び＝お客様に影響する）。
-- ⇒ (1) 比べるための文字の形を1か所（compare_form）にする。決まりは、出典の照らしの1か所 core/source_check.normalize と同じ：
--        文字の形をそろえ（NFKC＝全角の英数字・記号・空白、半角のカタカナなど）、見た目の同じ記号（波の記号・ハイフンやダッシュの類）を
--        そろえ、空白・改行・タブをすべて除く。空になれば NULL（値が無いのと同じ）。値そのものは契約者が入れたまま残す（比べるときだけ使う）。
--        Python と同じ形かは試験が見張る（tests/test_settle.py の CompareForm.test_the_compare_form_is_the_same_as_the_source_check）。
--    (2) その直しでお呼びする方が替わるか（person_fix_changes）は、この形で直前の値と比べる。担当の期（task_person_since）・直しの守り
--        （task_fix_guard）・Python の入口（core/row_fixes._person_fix＝直す入口 fix と画面の入口 old_promise_choice_needed）が読む。
--        前に記録した直しも、この決まりで読み直す（担当の期は記録から毎回求めるので、表記の揺れだけの直しは、前の直しも期を替えない）。

-- ═════════ (1) 比べるための文字の形（1か所） ═════════
-- 空白の文字は、Python の空白（str.isspace）と同じ組を書き並べる（データベースの「\s」は、文字の決まり（locale）によって全角などの空白を
-- 含まないため）。見た目の同じ記号は core/source_check._SAME_MARKS と同じ組（長音「ー」はそろえない＝言葉の一部）
CREATE FUNCTION salescall.compare_form(p text) RETURNS text LANGUAGE sql IMMUTABLE
SET search_path = salescall, pg_temp AS $$
  SELECT nullif(regexp_replace(
           translate(normalize(p, NFKC), U&'\301C\FF5E\2053\223C\2010\2011\2012\2013\2014\2015\2212\FE63\FF0D', '~~~~---------'),
           '[\t\n\v\f\r\x1c-\x1f \u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]', '', 'g'), '')
$$;
REVOKE ALL ON FUNCTION salescall.compare_form(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.compare_form(text) TO salescall_app;

-- ═════════ (2) その直しで、お呼びする方が替わるか（0208 の形から、比べるのを表記をそろえた形にした） ═════════
CREATE OR REPLACE FUNCTION salescall.person_fix_changes(p_task uuid, p_field text, p_value text, p_seq bigint) RETURNS boolean LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT salescall.person_fix_field(p_field)
     AND salescall.compare_form(p_value) IS DISTINCT FROM salescall.compare_form(salescall.task_field_before(p_task, p_field, p_seq))
$$;
