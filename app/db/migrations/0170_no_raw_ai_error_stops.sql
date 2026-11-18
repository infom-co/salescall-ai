-- 0170 2026-11-02 会話AIの生の誤りを数えて止める作りを外す（CL-1 の連絡帳 4-69 の 4-179 の (a)。書いた人：CL-2。設計書 15-2 の452）。
--
-- CL-1：「あなたの『生の誤りを3回数えて止める』は外してください」。電話の前に会話AIのつながりを開けないときは、会話の側の1か所
-- （Flow.on_ai_unreachable）が止まったAI「会話AI・止まった」を送って電話をかけない・通話の途中なら甚大な問題としてお詫びして終える。
-- 止まったかを決めるのは会話の側の1か所（止まったAI＝core/ai_troubles.note の入口）だけにする（✅ 2026-10-30 ユーザー：「基本的に
-- アンソロピックが使えなくなったりその他に使う AI のどれかが止まったら、AI の不具合として回復するまで一時停止してください。」）。
-- 会話AIの生の誤り（つながらない・Google の側の誤り・不明・設定の誤り＝core/errors.CONVO_RAW）は、記録・知らせ・修正依頼だけ。
--
-- (1) 数える関数を、止める種類が同じ原因の前の解き（と p_from）のあとに起きた数だけにする（新しい名前 errors_since。止める種類は、
--     どれも1回で止める＝core/watch._happened）。⛔ 前の consecutive_errors は、種類ごとの「直った証拠」（in_conversation＝その契約者の
--     どこかの通話が会話に入ったら数え直す。会話AIの生の誤りだけが使っていた）を引数に持っていた。
-- (2) 会話AIの生の誤りが何回続いたら止めるかの仮の設定 stop.ai.stop_after_failures（0005 で CL-2 が仮に決めた3。管理者の決定ではない）を
--     外す（契約者・依頼者の値も含めて。0147 の回線の設定と同じ外し方）。
-- まだ解かれていない停止のうち、前に会話AIの生の誤りで止めたもの（根拠「エラーの種類：ai_unreachable」など）は、そのまま回復の確かめで
-- 解ける（確かめで解ける原因は ai_connection のまま。1時間以上続いたときの修正依頼も、根拠の種類の担当＝CL-1 のまま）。

-- ═════════ (1) 止める種類が前の解きのあとに起きた数 ═════════
DROP FUNCTION salescall.consecutive_errors(text[], text, text, timestamptz);
CREATE FUNCTION salescall.errors_since(p_kinds text[], p_cause text, p_from timestamptz DEFAULT NULL) RETURNS integer
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT CASE WHEN salescall.cur_tenant() IS NULL THEN NULL
         ELSE (SELECT count(*)::integer FROM salescall.error_events e
               WHERE e.kind = ANY(p_kinds) AND e.tenant_id = salescall.cur_tenant()
                 AND e.occurred_at > greatest(salescall.errors_counted_since(p_cause), coalesce(p_from, '-infinity'::timestamptz))) END
$$;
REVOKE ALL ON FUNCTION salescall.errors_since(text[], text, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.errors_since(text[], text, timestamptz) TO salescall_app;

-- ═════════ (2) 仮の設定を外す ═════════
ALTER TABLE salescall.setting_values DISABLE TRIGGER setting_values_frozen;
ALTER TABLE salescall.setting_definitions DISABLE TRIGGER setting_definitions_frozen;
DELETE FROM salescall.setting_values WHERE key = 'stop.ai.stop_after_failures';
DELETE FROM salescall.setting_definitions WHERE key = 'stop.ai.stop_after_failures';
ALTER TABLE salescall.setting_definitions ENABLE TRIGGER setting_definitions_frozen;
ALTER TABLE salescall.setting_values ENABLE TRIGGER setting_values_frozen;
