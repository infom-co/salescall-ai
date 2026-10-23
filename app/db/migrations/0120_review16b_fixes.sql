-- 0120 見直し16b の直し：残った赤字の停止・続けて切れた数の数え直し・発信に失敗が続いた日の1か所・決まった文面だけの電話・
--      約束が近い番号の関数の引数・設定の説明（2026-10-23。書いた人：CL-2）
--
-- (1) 0118 で「赤字では止めない」（2026-10-22 深夜 ユーザー：「止めない。知らせるだけでいい」）にしたが、0118 より前に開いていた
--     赤字の停止は開いたまま残り、解く処理も通す電話の表の行も無いので、約束・お詫びの電話も含めて、その依頼者の電話がすべて止まった
--     ままになりえた（見直し16b）。⇒ ここで、システムが理由を付けて解く（知らせの台帳にも「解いた」を入れる）。原因の種類 deficit は
--     残す（前の記録が指すため。止める処理の無い原因＝storage_failure・project_denied・budget と同じ扱い。core/stops.CAUSES の説明）。
-- (2) 続けて切れた数（cuts_in_a_row）を0に戻すのは、お客様に届いて途中で切れなかった発信だけ（数え方の種類 dial_kind の 'reached'
--     と同じ決まり）。発信に失敗（鳴っていない）・お客様が出る前の回線の失敗は、はさまっても数え直さない（⛔ 見直し16b：前は、
--     「切れた → 回線の失敗 → 切れた」が交互に続くと、どの回数にも数えないまま、すぐのかけ直しを続けえた）。お客様が出る前の回線の
--     失敗かは、dial_kind と同じ1か所（is_line_failure）。
-- (3) 発信に失敗が続いた番号の、続いた数・続いた最初の日・最後の失敗の終わり（failed_run の1か所）。続いた数だけの関数
--     （failed_in_a_row）は外した（⛔ 見直し16b：前は、次にかける日を決める所が、続いた最初の日を別の問い合わせで求め直していた）。
-- (4) 決まった文面だけの電話（業務の話し方を使わない＝品質の段・目的のお客様を呼ぶ材料を見ない電話）か（fixed_text_kind の1か所。
--     いまはお詫びの電話だけ）。⛔ 見直し16b：前は「品質の停止でも通す電話」の表の行で決めていたので、品質の停止で続きの電話を通す
--     行を足すと、続きの電話も品質の段を見なくなった（2つの別の問いが1つの行につながっていた）。
-- (5) 約束が近い番号（number_promise_soon）の、使われない引数（いつまで＝いつも無限・除く行＝約束でない電話の行は約束の一覧に
--     もともと入らない）を外す（見直し16b の D19：壊しても働きが変わらなかった）。
-- (6) 設定の説明（画面の i マーク）を、0116〜0119 の決まりに合わせる（赤字では止めない・数える電話の種類）。

-- ═════════ (1) ═════════
SELECT set_config('salescall.actor', '表の作り 0120（赤字では止めない）', false),
       set_config('salescall.actor_kind', 'system', false);
WITH resumed AS (
  INSERT INTO salescall.stop_events (scope, tenant_id, client_id, business_id, line_id, action, resolves_event_id, actor,
                                     actor_kind, reason)
  SELECT e.scope, e.tenant_id, e.client_id, e.business_id, e.line_id, 'resume', e.event_id, salescall.cur_actor(),
         'system', '赤字では止めない（2026-10-22 深夜 ユーザー：「止めない。知らせるだけでいい」）。0118 より前に開いていた赤字の停止を解いた'
  FROM salescall.open_stop_events e WHERE e.cause = 'deficit'
  RETURNING event_id, resolves_event_id, scope, tenant_id, client_id)
INSERT INTO salescall.notification_events (tenant_id, client_id, kind, event_key, summary, refs)
SELECT r.tenant_id, r.client_id, 'resumed', 'resume:' || r.event_id,
       '止めていた発信を解きました（' || CASE r.scope WHEN 'system' THEN '全体' WHEN 'tenant' THEN '契約者' WHEN 'client' THEN '依頼者'
                                              WHEN 'business' THEN '業務' ELSE '回線' END || '：赤字）。赤字では止めません（知らせるだけ）。',
       jsonb_build_object('stop_event_id', r.resolves_event_id, 'resume_event_id', r.event_id, 'cause', 'deficit')
FROM resumed r;
SELECT set_config('salescall.actor', '', false), set_config('salescall.actor_kind', '', false);

-- ═════════ (2) ═════════
-- お客様が出る前に、回線・こちらの側で終わった発信か（数え方の種類 dial_kind の 'line_failure' と、続けて切れた数の数え直しの1か所）
CREATE FUNCTION salescall.is_line_failure(s salescall.attempt_state, p_attempt uuid) RETURNS boolean
LANGUAGE sql STABLE AS $$ SELECT s = 'technical_error' AND NOT salescall.reached_answer(p_attempt) $$;
GRANT EXECUTE ON FUNCTION salescall.is_line_failure(salescall.attempt_state, uuid) TO salescall_app;

CREATE OR REPLACE FUNCTION salescall.cuts_in_a_row(p_attempt uuid) RETURNS integer
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT count(*)::integer FROM salescall.attempts a JOIN salescall.attempts b ON b.task_id = a.task_id
  WHERE a.attempt_id = p_attempt AND salescall.is_dialed(b.state) AND b.claimed_at <= a.claimed_at AND coalesce(b.cut_off, false)
    AND NOT EXISTS (SELECT 1 FROM salescall.attempts c
                    WHERE c.task_id = a.task_id AND salescall.is_placed(c.state) AND c.claimed_at > b.claimed_at
                      AND c.claimed_at <= a.claimed_at AND NOT coalesce(c.cut_off, false)
                      AND NOT salescall.is_line_failure(c.state, c.attempt_id))
$$;

CREATE OR REPLACE FUNCTION salescall.dial_kind(s salescall.attempt_state, p_cut_off boolean, p_attempt uuid) RETURNS text
LANGUAGE sql STABLE AS $$
  SELECT CASE WHEN NOT salescall.is_dialed(s) THEN NULL
              WHEN NOT salescall.is_placed(s) THEN 'not_placed'
              WHEN coalesce(p_cut_off, false) AND salescall.cuts_in_a_row(p_attempt) <= salescall.resume_in_a_row() THEN 'cut_off'
              WHEN salescall.is_line_failure(s, p_attempt) THEN 'line_failure'
              ELSE 'reached' END
$$;

-- ═════════ (3) ═════════
CREATE FUNCTION salescall.failed_run(p_contact uuid)
RETURNS TABLE (n integer, first_day date, last_end timestamptz)
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  WITH x AS (SELECT a.claimed_at, coalesce(a.ended_at, a.claimed_at) AS e, salescall.is_placed(a.state) AS placed,
                    row_number() OVER (ORDER BY a.claimed_at DESC) AS rn
             FROM salescall.attempts a WHERE a.contact_id = p_contact AND salescall.is_dialed(a.state)),
       k AS (SELECT coalesce(min(x.rn) FILTER (WHERE x.placed) - 1, count(*))::integer AS n FROM x)
  SELECT k.n, (SELECT salescall.jst(x.claimed_at)::date FROM x WHERE x.rn = k.n),
         (SELECT x.e FROM x WHERE x.rn = 1 AND k.n > 0)
  FROM k
$$;
GRANT EXECUTE ON FUNCTION salescall.failed_run(uuid) TO salescall_app;
DROP FUNCTION salescall.failed_in_a_row(uuid);   -- 続いた数も failed_run から読む（同じことを2つの関数に持たない）

-- ═════════ (4) ═════════
-- 決まった文面だけの電話か（電話の種類＝keeping_kind の答えから。1か所）
CREATE FUNCTION salescall.fixed_text_kind(p_kind text) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$ SELECT coalesce(p_kind = 'apology', false) $$;
GRANT EXECUTE ON FUNCTION salescall.fixed_text_kind(text) TO salescall_app;

-- ═════════ (5) ═════════
DROP FUNCTION salescall.number_promise_soon(text, timestamptz, uuid);
CREATE FUNCTION salescall.number_promise_soon(p_phone text) RETURNS timestamptz
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
BEGIN
  PERFORM salescall.server_only();
  RETURN (SELECT min(w.callee_at) FROM salescall.promises_waiting() w WHERE w.phone_e164 = p_phone);
END $$;
REVOKE ALL ON FUNCTION salescall.number_promise_soon(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.number_promise_soon(text) TO salescall_app;

-- ═════════ (6) ═════════
ALTER TABLE salescall.setting_definitions DISABLE TRIGGER setting_definitions_frozen;
UPDATE salescall.setting_definitions SET meaning =
  '赤字の警告から、確認されなければ、契約者と管理者へもう一度知らせるまでの秒数（赤字では止めない＝2026-10-22 深夜 ユーザー'
  '「止めない。知らせるだけでいい」）'
 WHERE key = 'budget.deficit_grace';
UPDATE salescall.setting_definitions SET meaning =
  '報酬に対する費用の割合の上限（今月。超えたら知らせるだけ。赤字でも止めない：警告して、期限 budget.deficit_grace までに'
  '確認されなければ、もう一度知らせる）'
 WHERE key = 'budget.max_cost_ratio';
UPDATE salescall.setting_definitions SET meaning =
  '1つのタスク（行）に発信してよい回数の上限（再発信の方針はこの内側）。数え方は1日の回数と同じ：お客様の電話が鳴っていない'
  '発信（スマホのアプリが番号を受け付けなかった）・お客様が出る前の回線の失敗・お客様と話している途中で意思と関係なく切れた通話'
  '（お詫びしてすぐかけ直す、続けて2回まで。それを超えて続けて切れた通話は数える）は数えない。お客様と約束したかけ直しは止めない'
 WHERE key = 'retry.max_attempts_per_task';
UPDATE salescall.setting_definitions SET meaning =
  '同じ連絡先へ、日本時間の1日に発信してよい回数の上限（実施回をまたいでも数える）。お客様と約束したかけ直しは対象外。'
  'お客様の電話が鳴っていない発信・お客様が出る前の回線の失敗・お客様と話している途中で意思と関係なく切れた通話（お詫びしてすぐ'
  'かけ直す、続けて2回まで。それを超えて続けて切れた通話は数える）は数えない'
 WHERE key = 'gate.max_dials_per_contact_per_day';
UPDATE salescall.setting_definitions SET meaning =
  '同じ連絡先への前の発信が終わってから、次に予約できるまでの最小の間隔（依頼者ごと。次の動きの誤りでかけ続けないための歯止め。'
  'ふつうの間隔は再発信の方針で決める）。お客様と約束したかけ直しには効かない（約束の時刻にかける）。間隔は、お客様に届いた前の'
  '発信（約束の電話も含む）の終わりから数える。お客様の電話が鳴っていない発信・お客様が出る前の回線の失敗・お客様と話している途中で'
  '意思と関係なく切れた通話（お詫びしてすぐかけ直す、続けて2回まで）は、起点にしない'
 WHERE key = 'gate.same_contact_min_interval';
UPDATE salescall.setting_definitions SET meaning =
  '1つの回線（番号）から、日本時間の1日に発信してよい回数の上限（依頼者ごと。番号の評判を守る。数えるのはその回線の発信すべて'
  '（スマホのアプリが番号を受け付けなかった発信は数えない）：共有の回線では、かける依頼者の値で数える。Intent.md 8-6）'
 WHERE key = 'line.max_dials_per_day';
ALTER TABLE salescall.setting_definitions ENABLE TRIGGER setting_definitions_frozen;
