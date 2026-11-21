-- 0276 回線の「使えない」（2026-11-21。書いた人：CL-2。DESIGN_15-2.md の936。CL-3 の連絡帳 4-1 のお願い 62 の (2)・CL-2 の連絡帳 4-448 の 58 (2)）
--
-- PC の部品は、合図（hello・beat）の lines に、電話の部品が「使える」と答えた回線（スマホと連携していて、いま発信を受けられる＝link/phone.Phone.usable）だけを入れる。
-- 外れた回線は、発信の出口・門が今の1か所（合図の回線）のまま使わない（回線のせいの数えにも入らない＝電話網の回線が悪いのではない）。
-- 外れたままが PC の途切れと同じ長さ（設定 pc.down_after）続いたら、契約者に知らせる（core/agents.watch_lines の1か所）。そのため、回線ごとに、合図に入っていた
-- いちばん新しい時刻を残す（line_seen。書くのは合図の記録の1か所 salescall.agent_beat だけ）。
CREATE TABLE salescall.line_seen (
  line_id   uuid PRIMARY KEY REFERENCES salescall.lines,
  tenant_id uuid NOT NULL REFERENCES salescall.tenants,
  seen_at   timestamptz NOT NULL
);
ALTER TABLE salescall.line_seen ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON salescall.line_seen USING (tenant_id = salescall.cur_tenant());
GRANT SELECT ON salescall.line_seen TO salescall_app;

CREATE OR REPLACE FUNCTION salescall.agent_beat(p_pc uuid, p_lines uuid[]) RETURNS uuid[]
LANGUAGE plpgsql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
DECLARE kept uuid[];
BEGIN
  PERFORM salescall.hub_only();
  UPDATE salescall.pc_agents a SET last_seen_at = now(),
         lines = coalesce((SELECT array_agg(l.line_id ORDER BY l.line_id) FROM salescall.lines l
                           WHERE l.tenant_id = a.tenant_id AND l.line_id = ANY (p_lines)
                             AND (l.retired_at IS NULL
                                  OR EXISTS (SELECT 1 FROM salescall.attempts t
                                              WHERE t.line_id = l.line_id AND salescall.is_active(t.state)))), '{}')
   WHERE a.pc_id = p_pc AND a.revoked_at IS NULL
  RETURNING a.lines INTO kept;
  INSERT INTO salescall.line_seen (line_id, tenant_id, seen_at)
    SELECT l.line_id, l.tenant_id, now() FROM salescall.lines l WHERE l.line_id = ANY (coalesce(kept, '{}'))
  ON CONFLICT (line_id) DO UPDATE SET seen_at = EXCLUDED.seen_at;
  RETURN kept;
END $$;
