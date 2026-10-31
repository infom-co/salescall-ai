-- 0158 通話後の確かめ（別の耳）が止まったときも、次の電話を止める（2026-10-31 管理者のご決定。書いた人：CL-2。計画書 10-3・
-- DESIGN.md 9-1・2章 Q58）
--
-- ご決定：「通話後の確かめに使う別の耳が止まったとき：通話中は止めません。ただし、次の電話はかけず、確かめが通れば自動で再開します。
-- これまでの決まりの通り、甚大な影響が出ている場合は、お客様にお詫びをし、通話中も終了します。この変更は、AI の判断が、以前よりも
-- 影響が大きくなったからです。」（⛔ 2026-10-17 の「別の耳のエラーで発信は止めない」を改めた）
--
-- 止まったかを決めるのは、通話中と同じく会話の側（CL-1 の通話後の確かめの部品。通話中の Ear.errors と同じ決め方）。受け取る入口も通話中と
-- 同じ1か所（core/ai_troubles.note）で、止めるかは watch.decide_stop の1か所（AIの不具合 ai_connection・その契約者・1回で止める・回復の
-- 確かめで解く）。話している電話は切らない（止めるのは新しい電話だけ）。甚大な影響は、今までどおり会話の側の甚大な問題の入口で
-- お詫びして終える（call.end_for_trouble）。
--
-- (1) 止まったAIの事実に「どこで」（stage：call＝通話の中／verification＝通話後の確かめ）を足す。通話の中の分は、通話の途中の知らせと
--     報告の両方から届くので、同じAI・道・出来事を1つに数える（今までどおり）。通話後の確かめの分は、確かめのたびに別の出来事として
--     書く（⚠️ 重ねないと、再開したあとに同じ通話の確かめがまた止まっても、止め直せない）。
-- (2) 守り：通話後の確かめの分は、終わった通話にだけ書ける。

-- ═════════ (1) ═════════
ALTER TABLE salescall.attempt_ai_troubles
  ADD COLUMN stage text NOT NULL DEFAULT 'call' CHECK (stage IN ('call', 'verification')),
  ADD COLUMN trouble_id bigint GENERATED ALWAYS AS IDENTITY;
ALTER TABLE salescall.attempt_ai_troubles DROP CONSTRAINT attempt_ai_troubles_pkey;
ALTER TABLE salescall.attempt_ai_troubles ADD PRIMARY KEY (trouble_id);
CREATE UNIQUE INDEX attempt_ai_troubles_once_in_call ON salescall.attempt_ai_troubles (attempt_id, ai, route, what)
  WHERE stage = 'call';

-- ═════════ (2) ═════════
CREATE OR REPLACE FUNCTION salescall.attempt_ai_trouble_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF salescall.cur_actor_kind() IS DISTINCT FROM 'system' THEN
    RAISE EXCEPTION '止まったAIを書くのは、システムの処理だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'attempt_ai_trouble_guard';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM salescall.attempts a WHERE a.attempt_id = NEW.attempt_id
                   AND a.tenant_id = NEW.tenant_id AND a.client_id = NEW.client_id) THEN
    RAISE EXCEPTION 'その通話は、この依頼者の通話ではありません' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'attempt_ai_trouble_guard';
  END IF;
  IF NEW.stage = 'verification' AND NOT EXISTS (SELECT 1 FROM salescall.attempts a WHERE a.attempt_id = NEW.attempt_id
                                                  AND a.ended_at IS NOT NULL) THEN
    RAISE EXCEPTION '通話後の確かめで止まったAIは、終わった通話にだけ書けます' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'attempt_ai_trouble_guard';
  END IF;
  RETURN NEW;
END $$;
