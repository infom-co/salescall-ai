-- 0058 修正依頼は「同じ不具合は、直したと報告するまで1つ」（2026-10-18 CL-2 の見直し。DESIGN.md 9-1・15-2 の154）
--
-- 前は、呼ぶ側が鍵に時刻（1時間ごと・1日ごと）や通話を入れていたので、同じ不具合が続くと修正依頼が1日24件などに増えた
-- （エラー＝1時間ごと、控え＝1日ごと、判定できない＝通話ごと：決め方が3つに分かれていた）。
-- ⇒ 呼ぶ側は「どの不具合か」（fault_key）だけを渡す。まだ「直した」の報告が無い同じ不具合の依頼があれば、新しく出さない。
--   報告のあとに同じ不具合が起きたら（直っていなかった）、新しい依頼を出す。決めるのはこの関数の1か所。

ALTER TABLE salescall.fix_requests ADD COLUMN fault_key text;
CREATE INDEX fix_requests_by_fault ON salescall.fix_requests (fault_key);

CREATE OR REPLACE FUNCTION salescall.request_fix(p_assignee text, p_kind text, p_key text, p_summary text, p_refs jsonb)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
DECLARE got bigint;
BEGIN
  IF EXISTS (SELECT 1 FROM salescall.fix_requests q
             WHERE q.fault_key = p_key
               AND NOT EXISTS (SELECT 1 FROM salescall.fix_reports r WHERE r.request_id = q.request_id)) THEN
    RETURN false;
  END IF;
  INSERT INTO salescall.fix_requests (assignee, kind, event_key, fault_key, summary, refs)
  VALUES (p_assignee, p_kind,
          p_key || '#' || (SELECT count(*) + 1 FROM salescall.fix_requests WHERE fault_key = p_key), p_key, p_summary,
          coalesce(p_refs, '{}'::jsonb) || jsonb_build_object('tenant_id', salescall.cur_tenant(), 'client_id', salescall.cur_client()))
  ON CONFLICT (event_key) DO NOTHING RETURNING request_id INTO got;   -- 同時に2つ来たら1つだけ
  RETURN got IS NOT NULL;
END $$;
