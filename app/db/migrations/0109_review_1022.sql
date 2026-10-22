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

-- ═════════ (3) 門の不許可の記録は、その行の理由が変わったときだけ足す（core/gate.py の claim）。その行の最後の記録を速く読む ═════════
CREATE INDEX gate_denials_by_task ON salescall.gate_denials (task_id, denial_id DESC);

-- ═════════ (4) 「お客様のところで鳴った（鳴りえた）発信」の決まりを1か所に（salescall.is_placed） ═════════
-- 発信まで進んだが、回線の側でかけられなかった（call_failed＝電話の部品がかけられなかった・PC の上で緊急停止・PC がつながって
-- いない）発信は、お客様のところで鳴っていない。⛔ 2026-10-22 の見直し：回線の1日の上限・ほかの依頼者がかけた番号の間隔・ほかの
-- 契約者がかけた番号に、それまで数えていた（ほかの依頼者がその番号へ3日かけられなくなり、回線の1日の上限も減った）。
-- core/attempts.py の placed() も、この関数を使う（同じ決まりを2か所に書かない）。
CREATE FUNCTION salescall.is_placed(s salescall.attempt_state) RETURNS boolean LANGUAGE sql IMMUTABLE AS
$$ SELECT salescall.is_dialed(s) AND s <> 'call_failed' $$;
GRANT EXECUTE ON FUNCTION salescall.is_placed(salescall.attempt_state) TO salescall_app;

CREATE OR REPLACE FUNCTION salescall.line_dials_on(p_line uuid, p_day date)
 RETURNS integer LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO 'salescall', 'pg_temp'
AS $function$
  SELECT count(*)::integer FROM salescall.attempts a
  WHERE a.line_id = p_line AND a.tenant_id = salescall.cur_tenant()
    AND salescall.is_placed(a.state)
    AND salescall.jst(a.claimed_at)::date = p_day
$function$;
CREATE OR REPLACE FUNCTION salescall.number_last_dial_elsewhere(p_phone text)
 RETURNS TABLE(relation text, last_at timestamp with time zone)
 LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO 'salescall', 'pg_temp'
AS $function$
  SELECT CASE WHEN a.tenant_id = salescall.cur_tenant() THEN 'same_tenant' ELSE 'other_tenant' END, max(a.claimed_at)
  FROM salescall.attempts a
  WHERE a.phone_e164 = p_phone AND a.client_id IS DISTINCT FROM salescall.cur_client()
    AND salescall.is_placed(a.state)
  GROUP BY 1
$function$;
CREATE OR REPLACE FUNCTION salescall.number_dialed_by_other_tenant(p_phone text)
 RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO 'salescall', 'pg_temp'
AS $function$
  SELECT EXISTS (SELECT 1 FROM salescall.attempts a WHERE a.phone_e164 = p_phone AND a.tenant_id <> salescall.cur_tenant()
                 AND salescall.is_placed(a.state))
$function$;

-- ═════════ (5) その通話で取った答えを速く読む（目的のお客様の確かめ直しが要るか＝core/target_recheck.py の needed） ═════════
CREATE INDEX item_values_by_attempt ON salescall.item_values (attempt_id);
