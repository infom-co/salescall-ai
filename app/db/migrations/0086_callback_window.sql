-- 0086 お客様が言ったかけ直しの時間の幅（DESIGN.md 7-2・15-2 の183。2026-10-19 ユーザー決定。書いた人：CL-2）
--
-- ユーザー（2026-10-19）：「「夕方」だけ、「3時まで」のようなパターンだとそれに合わせてかけるべき」「明日なら、いつでも」「13日であれば、
-- そちらのご都合で」・曜日・週末・来週・月末・来月も。幅の中でお客様と話せなかったら、時間の言葉があれば次の日の同じ幅、日だけの
-- 約束なら日が過ぎたら契約者の間隔（案A）。
-- ⇒ 次の予定（かけ直す）に、幅の終わり（window_until。含まない）と、1日の中の幅（window_daily。0時からの秒の [始まり, 終わり)）を
--   持つ。読むのは core/spoken_time（言葉→幅）、書くのは次の動き（core/next_action）、守るのは門（幅の外ではかけない）。
--   約束したかけ直しだけでなく、幅の中で話せなかったあとのふつうのかけ直しも、同じ幅を引き継ぐ（次の日の同じ幅＝案A）。
ALTER TABLE salescall.task_next_actions
  ADD COLUMN window_until timestamptz,
  ADD COLUMN window_daily integer[],
  ADD CONSTRAINT task_next_actions_window CHECK (
    (window_until IS NULL AND window_daily IS NULL)
    OR (kind = 'call'
        AND (window_daily IS NULL
             OR (array_length(window_daily, 1) = 2 AND window_daily[1] >= 0
                 AND window_daily[1] < window_daily[2] AND window_daily[2] <= 86400))));
