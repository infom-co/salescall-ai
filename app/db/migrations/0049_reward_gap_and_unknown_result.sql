-- 0049 時給の「電話と電話の間」と赤字の判断の慎重さを、依頼者ごとの報酬の決まりに持つ・こちらの不具合で切れた通話の扱いを1つにする
-- （2026-10-17 ユーザー決定。書いた人：CL-2）
--
-- (1) 時給の作業時間（ユーザー：「時給換算では、人がやるのと同じスピードが重要なので、電話と電話の間は、報告を書いて、リストを確認
--     して、電話するので、1から2分くらいじゃないですか？ただし、報告をたくさん書かなくてはならない依頼者もあるかもしれないので、
--     依頼者ごとに間を上下チェックで設定できるといいですね（設定画面でなく、時給設定画面）」）
--     ⇒ 作業時間＝電話1本ごとに「かけ始めから終わりまで」＋「電話と電話の間」（依頼者ごとの決まった秒数。人が報告を書き、リストを
--       確かめ、次にかけるまでの時間）。前は、同じ回線で実際に空いた時間を、5分（システムだけの設定 budget.hourly_gap_max）まで
--       足していた（回線が並んで動くと、人の速さと関係ない数になる）。
--     値は報酬の決まり（reward_terms.hourly_gap_seconds。時給の決まりがあるときは必ず要る）。システムの設定は外す。
-- (2) 赤字かの判断の慎重さ（ユーザー：「依頼者ごと（時給（報酬）設定画面）」）⇒ 報酬の決まり（reward_terms.judge_confidence）。
--     いまある決まりには、これまでのシステムの値（0.95）を写す。システムの設定 budget.judge_confidence は外す。
-- (3) こちらの不具合で切れた通話（処理が落ちて、結果が分からない試行 technical_error）（ユーザー：「そもそも結果までの会話に
--     たどり着いていない場合と、聞いたのに、聞き取れなかった場合は違うでしょ。…たどり着いていない場合は、その理由で違うよね」）
--     ⇒ 聞き取れなかったときは、通話の中で聞き返す（会話の決まり。この試行の状態とは別）。結果が分からない試行は、こちらの仕組みが
--       通話の途中で落ちたもので、お客様が切ったのではない。回線が途中で切れた通話（お客様の意思と関係なく切れた）と同じ扱いにする：
--       回数・間隔に数えず、次は理由（取れたもの）ごとの再発信の方針で決める。前は、お客様が出たあとなら1時間（設定
--       gate.unknown_result_min_interval・下限 gate.unknown_result_min_interval_floor）あけていた。「実は断っていたかもしれない」は、
--       通話後の確かめ（録音から断りを探す。済むまで同じお客様にかけない）が受け持つので、時間をあけて守る必要が無い。

-- ═════════ (1)(2) 報酬の決まり ═════════
ALTER TABLE salescall.reward_terms DISABLE TRIGGER reward_terms_frozen;
ALTER TABLE salescall.reward_terms
  ADD COLUMN hourly_gap_seconds integer CHECK (hourly_gap_seconds >= 0),   -- 電話と電話の間（秒。時給の決まりがあるときは必ず）
  ADD COLUMN judge_confidence numeric NOT NULL DEFAULT 0.95 CHECK (judge_confidence > 0.5 AND judge_confidence < 1);
UPDATE salescall.reward_terms SET hourly_gap_seconds = 300 WHERE hourly_yen > 0;   -- これまでの上限の値を写す（あとで直せる）
ALTER TABLE salescall.reward_terms ALTER COLUMN judge_confidence DROP DEFAULT;    -- これからは、決まりを足すときに必ず書く
ALTER TABLE salescall.reward_terms
  ADD CONSTRAINT reward_terms_hourly_needs_gap CHECK (hourly_yen = 0 OR hourly_gap_seconds IS NOT NULL);
ALTER TABLE salescall.reward_terms ENABLE TRIGGER reward_terms_frozen;

-- 作業時間：電話1本ごとに、かけ始め（予約）から終わりまで＋その時刻に効いていた決まりの「電話と電話の間」。お詫びの電話は数えない
DROP FUNCTION salescall.work_seconds(timestamptz, timestamptz, integer);
CREATE FUNCTION salescall.work_seconds(p_from timestamptz, p_to timestamptz)
RETURNS TABLE (at timestamptz, seconds numeric)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT a.claimed_at,
         extract(epoch FROM a.ended_at - greatest(a.claimed_at, p_from))
         + coalesce((SELECT t.hourly_gap_seconds FROM salescall.reward_terms t
                     WHERE t.client_id = a.client_id AND t.valid_from <= a.claimed_at
                     ORDER BY t.valid_from DESC LIMIT 1), 0)
  FROM salescall.attempts a
  WHERE a.client_id = salescall.cur_client() AND salescall.cur_client() IS NOT NULL
    AND a.state NOT IN ('claimed', 'released') AND a.ended_at IS NOT NULL AND a.purpose <> 'apology'
    AND a.ended_at > p_from AND a.claimed_at <= p_to
$$;
REVOKE ALL ON FUNCTION salescall.work_seconds(timestamptz, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.work_seconds(timestamptz, timestamptz) TO salescall_app;

-- ═════════ (3) こちらの不具合で切れた通話は、お客様の意思と関係なく切れた通話と同じ（決まりは、この1か所） ═════════
CREATE OR REPLACE FUNCTION salescall.not_by_callee(s salescall.attempt_state, disconnect text, p_attempt uuid)
RETURNS boolean LANGUAGE sql STABLE AS $$
  SELECT s IN ('call_failed', 'technical_error') OR disconnect IS NOT DISTINCT FROM 'interrupted'
$$;

-- 使わなくなった設定
ALTER TABLE salescall.setting_values DISABLE TRIGGER setting_values_frozen;
ALTER TABLE salescall.setting_definitions DISABLE TRIGGER setting_definitions_frozen;
UPDATE salescall.setting_definitions SET bound_key = NULL, bound_side = NULL WHERE key = 'gate.unknown_result_min_interval';
DELETE FROM salescall.setting_values
WHERE key IN ('budget.hourly_gap_max', 'budget.judge_confidence', 'gate.unknown_result_min_interval',
              'gate.unknown_result_min_interval_floor');
DELETE FROM salescall.setting_definitions
WHERE key IN ('budget.hourly_gap_max', 'budget.judge_confidence', 'gate.unknown_result_min_interval',
              'gate.unknown_result_min_interval_floor');
ALTER TABLE salescall.setting_definitions ENABLE TRIGGER setting_definitions_frozen;
ALTER TABLE salescall.setting_values ENABLE TRIGGER setting_values_frozen;
