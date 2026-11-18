-- 0011 相手が自分で指定した時刻（2026-10-15 ユーザー決定）。書いた人：CL-2
-- ユーザー：「NG時間は『こちらから突然かけるのは失礼な時間』なので、通常は控えるが、相手が自分で指定した時刻なら、かけてよい」。
-- - 次の予定（かけ直す）に、相手が指定した時刻を持つ（会話で相手が言った折り返しの時刻だけ。約束したかけ直しのときだけ）。
--   決めるのは次の動きだけ。門で止められたあとも引き継ぐ。
-- - 門は、その時刻を含む「その日のNG時間の枠」の中だけ、NG時間でもかけてよいとする（core/calling_hours.allowed の1か所）。
--   日がずれたら、その日のNG時間には、ふつうどおりかけない。かけてよい時間帯（依頼主ごと・深夜早朝の歯止め）は、いつも守る。
ALTER TABLE salescall.task_next_actions
  ADD COLUMN callee_at timestamptz,
  ADD CONSTRAINT task_next_actions_callee_at CHECK (callee_at IS NULL OR (kind = 'call' AND promised));
