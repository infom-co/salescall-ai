-- 0109 2026-10-22 の見直し（main に入れる前の見直し11）。書いた人：CL-2

-- ═════════ (1) 動いている合図は、使わなくなった印を付けた回線でも、通話の途中のあいだは残す（DESIGN.md 5-4） ═════════
-- ⛔ 2026-10-22 の見直し：通話の途中で回線に「使わない」の印を付けると、次の合図でその回線が PC のつなぎから外れ、そのあとは
-- お客様の声も「おわびして切って」も通らなかった（PC はつながったままなので、PC の側のおわびも動かない＝お客様を無言で放置する）。
-- 「使わない」は、新しくかけないこと（門が断る＝core/gate.py）。かけている途中の通話は、終わるまでその回線で続ける（声・切る・
-- おわびが通る）。決めるのは、この合図の1か所。
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
  RETURN kept;
END $$;

-- ═════════ (2) 控えの記録の種類から、使わなくなった種類を外す（負の遺産） ═════════
-- drive（ハードディスクが無い）・key（鍵が無い）は、PC の見守り役が書いていた種類。サーバーと PC の分け方（DESIGN.md 5-5）のあとは、
-- PC が直す人あての文を返し、その控えの種類（system・client_ai・submission・prune）の記録に書く（core/backup.py の PARTS の1か所）。
ALTER TABLE salescall.backup_runs DROP CONSTRAINT backup_runs_kind_check;
ALTER TABLE salescall.backup_runs
  ADD CONSTRAINT backup_runs_kind_check CHECK (kind IN ('system', 'client_ai', 'submission', 'prune'));
