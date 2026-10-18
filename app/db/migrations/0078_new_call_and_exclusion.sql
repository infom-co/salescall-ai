-- 0078 営業として新しくかける電話か・「かけない」の例外（DESIGN.md 3-4-2・5-2 の7b・15-2 の175。2026-10-18 ユーザー決定。
-- 書いた人：CL-2）
--
-- ユーザー（2026-10-18）：「かけないことを除外ということなので、その3つは途中でかけないに切り替えてもかけるという意味ですよ」
-- ＝行を途中で「かけない」に変えても、お詫びの電話・途中で切れたあとのかけ直し・お客様と約束したかけ直しは、かける
-- （どれも、もう話したお客様・お客様が頼んだ電話＝営業として新しくかける電話ではない）。
-- 前は、この3つも止めていた（候補にも出さなかった）。また「営業として新しくかける電話か」の決まりが、業務の1日の上限の中
-- （門の Python の一覧と、数える SQL）にしか無かった。
--
-- (1) 営業として新しくかける電話か（`salescall.is_new_call`。決まりはこの1か所）：お詫び（apology）・途中で切れたあとのかけ直し
--     （resume）・約束したかけ直し（promised）は、新しい電話ではない。業務の1日の上限（数える・止める）と「かけない」が読む。
-- (2) 行がいま「かけない」で止まるか（`salescall.task_excluded_now`）：「かけない」の印があり、次の予定が新しい電話のとき。門と候補を
--     出す処理が、同じこれを読む（印そのものは `task_excluded`＝画面の表示・付け外し）。

CREATE FUNCTION salescall.is_new_call(p_purpose text, p_promised boolean) RETURNS boolean LANGUAGE sql IMMUTABLE AS
$$ SELECT p_purpose NOT IN ('apology', 'resume') AND NOT coalesce(p_promised, false) $$;

CREATE FUNCTION salescall.task_excluded_now(p_task uuid) RETURNS boolean LANGUAGE sql STABLE AS $$
  SELECT salescall.task_excluded(p_task)
     AND coalesce((SELECT salescall.is_new_call(n.purpose, n.promised) FROM salescall.task_next_actions n
                   WHERE n.task_id = p_task ORDER BY n.seq DESC LIMIT 1), true)
$$;
GRANT EXECUTE ON FUNCTION salescall.is_new_call(text, boolean), salescall.task_excluded_now(uuid) TO salescall_app;
