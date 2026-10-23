-- 0122 見直し17：回線が「直った」とみなす証拠（2026-10-23。書いた人：CL-2）
--
-- 回線の異常（エラーの種類 line・unknown_line）が続いた数は、「直った」証拠の時刻から数え直す（salescall.consecutive_errors）。
-- 回線の証拠は、前は「その回線でお客様を呼び出せた」（呼び出し中 ringing に移った）だった。ところが、お客様が出る前に回線の側で
-- 終わる電話（結果不明＝数え方の line_failure）は、呼び出し中を通ってから終わるので、失敗したその電話自身の呼び出しが証拠になり、
-- 続いた数が毎回1に戻った。そのため、同じ回線で回線の失敗が続いても、回線は止まらなかった（⛔ 見直し17a：3日で12本かけても停止0件。
-- 2026-10-22 深夜 ユーザー：「数えない分の歯止めは、回線の異常が続いたら回線を止める仕組み：OK」が働いていなかった）。
-- ⇒ 回線の証拠を line_ok にする：その回線で、終わった電話のうち、お客様のところで電話が鳴った（数え方の種類が「お客様に届いた1回」
--    か「途中で切れた」＝回線の部品が番号を受け付けなかった・お客様が出る前に回線の側で終わったではない）もの。時刻は、その電話の
--    終わり。決まりは数え方の1か所（is_placed・is_line_failure＝dial_kind と同じ）。core/errors.py の EVIDENCE が名前で読む。

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
                             WHERE a.line_id = p_line AND a.ended_at IS NOT NULL AND salescall.is_placed(a.state)
                               AND NOT salescall.is_line_failure(a.state, a.attempt_id))
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
