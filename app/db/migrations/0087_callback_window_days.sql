-- 0087 お客様が言ったかけ直しの日の種類（「平日なら」「土日祝ならいつでも」など。DESIGN.md 7-1d・15-2 の184。2026-10-19 ユーザー：
-- 「週中や再来週、明後日 明々後日、あさって、しあさって、なども大丈夫ですか？他にもあるんじゃないかな！？」。書いた人：CL-2）
--
-- 次の予定の幅（0086）に、かけてよい日の種類（window_days。曜日・祝日の鍵＝calling_hours の mon〜sun・holiday）を足す。
-- 門は、その日の種類の外ではかけない（幅の決まりは calling_hours.in_window の1か所）。
ALTER TABLE salescall.task_next_actions
  ADD COLUMN window_days text[],
  ADD CONSTRAINT task_next_actions_window_days CHECK (
    window_days IS NULL
    OR (kind = 'call' AND cardinality(window_days) >= 1
        AND window_days <@ ARRAY['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun', 'holiday']::text[]));
