-- 0124 お客様が出る前に終わる番号も、番号を受け付けない番号と同じ順で数えて、続いたらかけない（2026-10-23。書いた人：CL-2）
--
-- 2026-10-23 ユーザー決定（問い「回線は正常なのに、ある番号だけ毎回お客様が出る前に終わるとき」への答え）：「番号を受け付けない
-- ときと同じ。当日2回・翌日1回・その2日後1回の計4回続いたら、その番号へはかけない」。⛔ 見直し17a：お客様が出る前に回線の側で
-- 終わる電話（数え方の line_failure）は、回線の異常としてだけ数えていたので、回線が元気でほかの番号にかかるあいだは回線も止まらず、
-- その番号へは再発信の方針の間隔で終わりなくかけ続けた。
--
-- (1) お客様のところで電話が鳴ったか（salescall.rang）を1か所にする：数え方の種類 dial_kind が「お客様に届いた1回」か「途中で切れた」。
--     鳴らなかった＝発信に失敗（番号を受け付けない）・お客様が出る前に回線・こちらの側で終わった（会話の部品が呼び出しのあと
--     落ちたものも、応答の前なら同じ line_failure）。読むのは、番号の続き（failed_run）・回線が直った証拠（consecutive_errors の
--     line_ok）・約束の回にかけたか（core/attempts.rang）。前は3か所で別々の条件を書いていた。
-- (2) 番号の続き（failed_run）：その番号へのいちばん新しい発信から続けて、鳴らなかった発信の数・続いた最初の日・最後の終わり。
--     前は「発信に失敗」（is_placed でない）だけを数えていた。続いた数を0に戻すのは、お客様のところで鳴った発信。並び（設定
--     dial.failed_retry_days）と、使えない番号の判断（core/attempts.number_unusable）・次にかける時刻（core/next_action._failed_retry_at）は
--     変えない（同じ1か所を読む）。回線が壊れているときは、回線の異常が先に回線を止める（続けて3回＝stop.line.stop_after_failures）ので、
--     1つの番号が4回続くには日をまたぐ順が要り、回線の故障だけで番号が使えないと決まることはない。
-- (3) 設定 dial.failed_retry_days の説明（i マーク）を、番号を受け付けない・お客様が出る前に終わる、の両方の順と分かる形に直す。

-- ═════════ (1) ═════════
CREATE FUNCTION salescall.rang(s salescall.attempt_state, p_cut_off boolean, p_attempt uuid) RETURNS boolean
LANGUAGE sql STABLE AS $$ SELECT salescall.dial_kind(s, p_cut_off, p_attempt) IN ('reached', 'cut_off') $$;
GRANT EXECUTE ON FUNCTION salescall.rang(salescall.attempt_state, boolean, uuid) TO salescall_app;

-- 回線が直った証拠 line_ok も、鳴ったか（rang）の1か所を読む
CREATE OR REPLACE FUNCTION salescall.consecutive_errors(p_kinds text[], p_cause text, p_evidence text, p_line uuid DEFAULT NULL)
RETURNS integer
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  WITH allowed AS (     -- 契約者の数は、その契約者の取引だけ・回線の数は、その回線の契約者の取引だけ（全体の取引では数えない）
    SELECT salescall.cur_tenant() IS NOT NULL
           AND (p_line IS NULL OR EXISTS (SELECT 1 FROM salescall.lines l
                                          WHERE l.line_id = p_line AND l.tenant_id = salescall.cur_tenant())) AS ok
  ), since AS (
    SELECT greatest(
      CASE p_evidence
        WHEN 'in_conversation' THEN (SELECT coalesce(max(h.at), '-infinity'::timestamptz)
                                     FROM salescall.attempt_state_history h JOIN salescall.attempts a ON a.attempt_id = h.attempt_id
                                     WHERE h.to_state = 'in_conversation' AND a.tenant_id = salescall.cur_tenant())
        WHEN 'verified' THEN (SELECT coalesce(max(v.recorded_at), '-infinity'::timestamptz) FROM salescall.verification_summaries v
                              WHERE v.tenant_id = salescall.cur_tenant())
        WHEN 'line_ok' THEN (SELECT coalesce(max(a.ended_at), '-infinity'::timestamptz) FROM salescall.attempts a
                             WHERE a.line_id = p_line AND a.ended_at IS NOT NULL
                               AND salescall.rang(a.state, a.cut_off, a.attempt_id))
        WHEN 'none' THEN '-infinity'::timestamptz
      END,
      (SELECT coalesce(max(r.at), '-infinity'::timestamptz)
       FROM salescall.stop_events r JOIN salescall.stop_events s ON s.event_id = r.resolves_event_id
       WHERE s.cause::text = p_cause AND s.tenant_id = salescall.cur_tenant()
         AND CASE WHEN p_line IS NULL THEN s.scope = 'tenant' ELSE s.scope = 'line' AND s.line_id = p_line END)) AS t
  )
  SELECT CASE WHEN NOT (SELECT ok FROM allowed) OR p_evidence NOT IN ('in_conversation', 'verified', 'line_ok', 'none')
                                     OR (p_evidence = 'line_ok' AND p_line IS NULL) THEN NULL
         ELSE (SELECT count(*)::integer FROM salescall.error_events e, since
               WHERE e.kind = ANY(p_kinds) AND e.occurred_at > since.t AND e.tenant_id = salescall.cur_tenant()
                 AND (p_line IS NULL OR e.line_id = p_line)) END
$$;


-- ═════════ (2) ═════════
CREATE OR REPLACE FUNCTION salescall.failed_run(p_contact uuid)
RETURNS TABLE (n integer, first_day date, last_end timestamptz)
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  WITH x AS (SELECT a.claimed_at, coalesce(a.ended_at, a.claimed_at) AS e,
                    salescall.rang(a.state, a.cut_off, a.attempt_id) AS rang,
                    row_number() OVER (ORDER BY a.claimed_at DESC) AS rn
             FROM salescall.attempts a WHERE a.contact_id = p_contact AND salescall.is_dialed(a.state)),
       k AS (SELECT coalesce(min(x.rn) FILTER (WHERE x.rang) - 1, count(*))::integer AS n FROM x)
  SELECT k.n, (SELECT salescall.jst(x.claimed_at)::date FROM x WHERE x.rn = k.n),
         (SELECT x.e FROM x WHERE x.rn = 1 AND k.n > 0)
  FROM k
$$;

-- ═════════ (3) ═════════
ALTER TABLE salescall.setting_definitions DISABLE TRIGGER setting_definitions_frozen;
UPDATE salescall.setting_definitions SET meaning =
  '同じ番号へかけて、お客様のところで電話が鳴らなかった（回線の部品＝スマホのアプリが番号を受け付けなかった・お客様が出る前に'
  '回線やこちらの側で終わった）ときに、同じ番号へ何日目にかけ直すかの並びです（1＝最初に鳴らなかった日。例：[1, 1, 2, 4]＝その日に'
  'もう1回・次の日に1回・そのさらに2日後に1回）。並びの数だけ続けて鳴らなければ、その番号へはかけません（お客様が言った別の番号が'
  'あれば、そちらへかけます。無ければ、その行を終えます）。行の回数には数えません。途中でお客様のところで鳴れば、数え直します。'
  '同じ日のかけ直しは、同じお客様への最短の間隔をあけます。'
 WHERE key = 'dial.failed_retry_days';
ALTER TABLE salescall.setting_definitions ENABLE TRIGGER setting_definitions_frozen;
