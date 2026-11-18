-- 0128 見直し18 の直し（2026-10-24。書いた人：CL-2）
--
-- (1) 断られたかがまだ決まっていない（refusal_undecided。同じお客様のほかの行を止める）のは、判定できない原因のうち、断られたかが
--     分からない原因（会話の報告が無い・切れ方の材料が無い）だけ。原因ごとに分かるかを決めるのは判定の1か所（core/judge.REFUSAL_KNOWN）
--     で、判定できない結果の中身に refusal_known として残る。ここはそれを読むだけ（印の無い結果＝前の判定の処理の結果は、分からないと
--     みなす＝止める側）。途中で切れた通話（cut_off）は、今までどおり、通話のあとの確かめで断りの言葉が見つかっていなければ断られて
--     いない（0118）。⛔ 見直し18b の (5)：前は、どの原因の判定できないも止め、かけ直しの時刻・「わからない」の理由・繋がらなかった
--     理由の抜け（拒否の確かめを通ったあとの抜け）で、断られていないお客様のほかの行まで、直して判定し直すまでかからなかった。
CREATE OR REPLACE FUNCTION salescall.refusal_undecided(p_attempt uuid) RETURNS boolean
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT NOT salescall.is_settled(p_attempt)
      OR (salescall.unknown_pending(p_attempt)
          AND NOT coalesce((SELECT a.cut_off FROM salescall.attempts a WHERE a.attempt_id = p_attempt), false)
          AND NOT coalesce((SELECT (r.detail ->> 'refusal_known')::boolean FROM salescall.task_results r
                            WHERE r.attempt_id = p_attempt ORDER BY r.version_no DESC LIMIT 1), false))
$$;

-- (2) 設定 budget.deficit_grace の根拠の欄に、2026-10-22 深夜のユーザー決定（赤字では止めない）を書き足す（値は1時間のまま。
--     値の表は追記だけなので、同じ値で根拠を直した行を足す。⛔ 見直し18c：説明（0120）は直したが、根拠の欄は 10-16 の「止める」の
--     ままだった）。
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('budget.deficit_grace', 'system', '3600', 'decided',
 '2026-10-16 ユーザー決定：「警告喚起後、確認ボタンを1時間押さなければ止める」⇒ 2026-10-22 深夜 ユーザー決定：「赤字で、知らせてから'
 '1時間たっても確認ボタンが押されないとき：止めない。知らせるだけでいい。」（1時間たっても確認されなければ、契約者と管理者へもう一度'
 '知らせる。止めない）',
 '運用の実績を見て', 'ユーザー');
