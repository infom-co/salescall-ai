-- 0085 「強制かけない」（DESIGN.md 3-4-2・15-2 の182。2026-10-19 ユーザー決定。書いた人：CL-2）
--
-- ユーザー（2026-10-19）：「「かけない」と「強制かけない」の2つをつくって、強制かけないは、すべての電話をぜったいにかけないにしてください。」
-- ＝行の印は3つ：かける／かけない（営業として新しくかける電話だけ止める。お詫び・途中で切れたあとのかけ直し・約束したかけ直し
--   〔番号の聞き直しを含む〕は例外としてかける＝0078）／強制かけない（例外なしに、その行へは1本もかけない）。
--
-- (1) 印の記録に「強制」を足す（強制は「かけない」の一種なので、かけてよいに戻す記録には付けられない）。
-- (2) 行の印がどれかは、この関数の1か所（`salescall.task_exclusion_level`：call／exclude／force）。「かけない」か
--     （`task_excluded`。画面の表示）と、かけるのを止めるか（`task_excluded_now`。門と候補を出す処理）は、これを読む。

ALTER TABLE salescall.task_exclusions
  ADD COLUMN forced boolean NOT NULL DEFAULT false,
  ADD CONSTRAINT task_exclusions_forced_is_excluded CHECK (excluded OR NOT forced);

CREATE FUNCTION salescall.task_exclusion_level(p_task uuid) RETURNS text LANGUAGE sql STABLE AS $$
  SELECT coalesce((SELECT CASE WHEN NOT e.excluded THEN 'call' WHEN e.forced THEN 'force' ELSE 'exclude' END
                   FROM salescall.task_exclusions e WHERE e.task_id = p_task ORDER BY e.seq DESC LIMIT 1), 'call')
$$;

CREATE OR REPLACE FUNCTION salescall.task_excluded(p_task uuid) RETURNS boolean LANGUAGE sql STABLE AS $$
  SELECT salescall.task_exclusion_level(p_task) <> 'call'
$$;

CREATE OR REPLACE FUNCTION salescall.task_excluded_now(p_task uuid) RETURNS boolean LANGUAGE sql STABLE AS $$
  SELECT CASE salescall.task_exclusion_level(p_task)
           WHEN 'force' THEN true                                   -- 強制かけない：例外なし
           WHEN 'exclude' THEN coalesce((SELECT salescall.is_new_call(n.purpose, n.promised) FROM salescall.task_next_actions n
                                         WHERE n.task_id = p_task ORDER BY n.seq DESC LIMIT 1), true)
           ELSE false END
$$;
GRANT EXECUTE ON FUNCTION salescall.task_exclusion_level(uuid) TO salescall_app;
