-- 0219 1か月以上先のかけ直し：契約者の答えが無いまま約束の時刻が来たら、緊急の知らせをしてかける・お客様の言葉から決まった予定だけに当てはめる
-- （2026-11-12。書いた人：CL-2。DESIGN.md 7-2 の【0219】・9-2・2章 Q230〜・15-2 の760〜）
--
-- ✅ 2026-11-12 ユーザー＝計画書 7-3 (3)：伺い「答えが無い」に「契約者に緊急の通知をして、約束の時間にかける」・伺い「方針の間隔」に「契約者が
-- 意図的に設定したら、かけて良い。」
-- (1) 契約者に伺う予定の印（次の予定の列 month_ahead_ask）：業務の設定 callback.month_ahead が「契約者が認めたらかける」で、お客様の言葉から決まった
--     予定（お客様が言った時刻・幅・日）が1か月以上先で、契約者がまだ認めていないとき、その予定に印を付けて書く。決めるのは決定を書く1か所
--     （core/next_action._write の _month_ahead）。お客様の言葉から決まった予定かは、決定の値で見分ける1か所（next_action.Decision.said_by_callee）。
--     データベースは印を読むだけ（お客様の言葉か・業務の設定を、ここで決め直さない＝同じ判断を2か所に書かない）。
--     契約者が設定した再発信の方針の間隔（例：40日後）で1か月以上先になった予定には付けない（そのままかける）。
-- (2) 伺っていて、まだ答えの無い予定の時刻（salescall.task_month_ahead_asking の1か所。答えを見るのは、ここだけ＝0217 の
--     salescall.month_ahead_unapproved は外した）。ここから：
--     - 答えを待っている＝約束の時刻より前（salescall.task_month_ahead_waiting。画面が「お答え待ち」を出す）。約束の時刻が来たら待たない（かける）。
--     - 答えられる＝まだかけていない（salescall.task_month_ahead_answerable。答えの入口 core/row_fixes.answer_month_ahead と、答えの守りが読む）。
--       約束の時刻のあとでも、まだかけていなければ答えられる（「認めない」なら行を終える）。もうかけた・かけている予定には答えられない。
--     - 答えの無いまま約束の時刻が来てかけるとき、門が通したところで、契約者に緊急の知らせを1回出す（core/gate.claim。行とその時刻ごとに1回）。
-- (3) 知らせの急ぎの印（知らせの台帳の列 urgent）：緊急の知らせかは、知らせの種類ごとに core/notify.URGENT の1か所で決め、入れるときに台帳へ残す
--     （画面・送る処理＝CL-3 が読む）。いまは month_ahead_unanswered だけ。
-- ⛔ 0217 の食い違い：(a) 答えないまま約束の時刻が来ても、門が待ち続けた（行がかけられないまま残る）。(b) 再発信の方針の間隔で1か月以上先になった
-- 予定も、初めの値で行を終え、「契約者が認めたらかける」では答えを待った。(c) 待つかを、データベースが予定ごとに決め直していた（業務の設定を見ずに、
-- 1か月以上先かだけで）ので、0217 の前に書いた1か月以上先の予定は、知らせの無いまま、どの業務でも答えを待った。

-- ═════════ (1) 契約者に伺う予定の印 ═════════
ALTER TABLE salescall.task_next_actions ADD COLUMN month_ahead_ask boolean NOT NULL DEFAULT false;
ALTER TABLE salescall.task_next_actions ADD CONSTRAINT task_next_actions_month_ahead_ask CHECK (NOT month_ahead_ask OR kind = 'call');
-- 列は表の終わりに足すので、見方は同じ形のまま作り直せる（ほかの見方・関数を外さない）
CREATE OR REPLACE VIEW salescall.current_next_actions WITH (security_invoker = true) AS
  SELECT n.* FROM salescall.task_next_actions n
  WHERE n.seq = (SELECT max(m.seq) FROM salescall.task_next_actions m WHERE m.task_id = n.task_id);
-- 0217 で契約者に伺った予定（知らせ month_ahead_approval を出した、その行のその時刻）は、伺ったまま（印を付ける）。伺っていない予定は付けない
-- （0217 の前に書いた予定は、知らせを出していないので伺わない＝いまどおり、その時刻にかける）
ALTER TABLE salescall.task_next_actions DISABLE TRIGGER USER;
UPDATE salescall.task_next_actions n SET month_ahead_ask = true
WHERE n.kind = 'call'
  AND EXISTS (SELECT 1 FROM salescall.notification_events e
              WHERE e.kind = 'month_ahead_approval' AND e.refs ->> 'task_id' = n.task_id::text
                AND (e.refs ->> 'call_at')::timestamptz = n.call_at);
ALTER TABLE salescall.task_next_actions ENABLE TRIGGER USER;

-- ═════════ (2) 伺っていて、まだ答えの無い予定（1か所） ═════════
-- 0217 の「1か月以上先で、まだ認めていない」（決定を書く1か所と、待つかの1か所が読んでいた）：待つかは印を読むようになり、決定を書く1か所は
-- 1か月以上先か（salescall.month_ahead）だけを見る（答えを見るのは下の1か所）ので外す
DROP FUNCTION salescall.month_ahead_unapproved(uuid, timestamptz);
-- いまの予定が「かけ直す」で、契約者に伺う印があり、その時刻への答えがまだ無い（認めた・認めないのどちらも無い。認めなかった予定は、行を終えて
-- いるので「かけ直す」でない）
CREATE FUNCTION salescall.task_month_ahead_asking(p_task uuid) RETURNS timestamptz
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT n.call_at FROM salescall.current_next_actions n
  WHERE n.task_id = p_task AND n.kind = 'call' AND n.month_ahead_ask
    AND NOT EXISTS (SELECT 1 FROM salescall.task_month_ahead_answers k WHERE k.task_id = p_task AND k.call_at = n.call_at)
$$;
REVOKE ALL ON FUNCTION salescall.task_month_ahead_asking(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.task_month_ahead_asking(uuid) TO salescall_app;

-- 契約者の答えを待っている予定の時刻（無ければ NULL）：伺っていて答えが無く、約束の時刻より前（✅ 2026-11-12 ユーザー：答えが無いまま約束の時刻が
-- 来たら、かける）。⛔ 0217：1か月以上先か（salescall.month_ahead_unapproved）を予定ごとに決め直し、時刻が来ても待った
CREATE OR REPLACE FUNCTION salescall.task_month_ahead_waiting(p_task uuid) RETURNS timestamptz
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT a FROM salescall.task_month_ahead_asking(p_task) a WHERE now() < a
$$;

-- 契約者が答えられる予定の時刻（無ければ NULL）：伺っていて答えが無く、まだかけていない＝その時刻から、かけた（発信まで進んだ）電話も、いま生きて
-- いる予約も無い（門は約束の時刻より前の予定にかけないので、その時刻からの電話は、この予定の電話）。約束の時刻のあとでも、まだかけていなければ
-- 答えられる
CREATE FUNCTION salescall.task_month_ahead_answerable(p_task uuid) RETURNS timestamptz
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT a FROM salescall.task_month_ahead_asking(p_task) a
  WHERE NOT EXISTS (SELECT 1 FROM salescall.attempts t
                    WHERE t.task_id = p_task AND t.claimed_at >= a
                      AND (salescall.is_dialed(t.state) OR salescall.is_live(t.state, t.claim_expires_at)))
$$;
REVOKE ALL ON FUNCTION salescall.task_month_ahead_answerable(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.task_month_ahead_answerable(uuid) TO salescall_app;

CREATE OR REPLACE FUNCTION salescall.task_month_ahead_answer_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT salescall.cur_is_person() THEN
    RAISE EXCEPTION '1か月以上先のかけ直しを認めるかを答えられるのは、管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'task_month_ahead_answer_by_person';
  END IF;
  IF NEW.answered_by IS DISTINCT FROM salescall.cur_actor() THEN
    RAISE EXCEPTION '答えた方は、いま操作している方です' USING ERRCODE = 'check_violation', CONSTRAINT = 'task_month_ahead_answer_answered_by';
  END IF;
  IF NEW.call_at IS DISTINCT FROM salescall.task_month_ahead_answerable(NEW.task_id) THEN
    RAISE EXCEPTION '答えられるのは、契約者に伺っている1か月以上先のかけ直しの予定で、まだかけていない行の、その時刻だけです' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'task_month_ahead_answer_waiting';
  END IF;
  RETURN NEW;
END $$;

-- ═════════ (3) 知らせの急ぎの印 ═════════
ALTER TABLE salescall.notification_events ADD COLUMN urgent boolean NOT NULL DEFAULT false;
-- 管理者あての写しも、同じ急ぎの印で入れる（印を決めるのは core/notify.URGENT の1か所。呼ぶのは notify.emit だけ）
DROP FUNCTION salescall.notify_admin(text, text, text, jsonb);
CREATE FUNCTION salescall.notify_admin(p_kind text, p_key text, p_summary text, p_refs jsonb, p_urgent boolean) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
DECLARE got bigint;
BEGIN
  INSERT INTO salescall.notification_events (tenant_id, client_id, kind, event_key, summary, refs, for_admin, origin_tenant, urgent)
  VALUES (NULL, NULL, p_kind, 'admin:' || p_key, p_summary,
          coalesce(p_refs, '{}'::jsonb) || jsonb_build_object('tenant_id', salescall.cur_tenant(), 'client_id', salescall.cur_client()),
          true, salescall.cur_tenant(), p_urgent)
  ON CONFLICT (event_key, tenant_id, origin_tenant) DO NOTHING RETURNING event_id INTO got;
  RETURN got IS NOT NULL;
END $$;
REVOKE ALL ON FUNCTION salescall.notify_admin(text, text, text, jsonb, boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.notify_admin(text, text, text, jsonb, boolean) TO salescall_app;

-- ═════════ (4) 設定の説明 ═════════
ALTER TABLE salescall.setting_definitions DISABLE TRIGGER setting_definitions_frozen;
UPDATE salescall.setting_definitions SET meaning =
 'お客様とのかけ直しの予定のうち、お客様の言葉から決まった予定（お客様が言った時刻・幅・日。「来月の15日に」など）が、その予定を決めた日から'
 '1か月以上先（翌月の同じ日から）のときに、かけるかです。never＝かけない（初め。その予定は書かずに、行を終えます。終わりの理由は「1か月以上先の'
 'かけ直し」）／with_approval＝契約者が認めたらかける（予定はそのまま残し、ダッシュボードで契約者に伺います。認めればその時刻にかけ、認めなければ'
 '行を終えます。お答えが無いまま約束の時刻が来たら、契約者に緊急でお知らせして、お約束どおりかけます）。契約者が決めた再発信の方針の間隔'
 '（例：40日後）で1か月以上先になった電話は、この設定に関係なく、そのままかけます（2026-11-08 ユーザー：「1か月先に電話を入れるなんてことは'
 '通常ありえません。1か月以上先の予定がある場合、契約者が認めた場合のみかけます。設定で、1か月以上先の架電はかけないがデフォルト。（選択式）」。'
 '2026-11-12 ユーザー：「契約者に緊急の通知をして、約束の時間にかける」「契約者が意図的に設定したら、かけて良い。」）'
WHERE key = 'callback.month_ahead';
ALTER TABLE salescall.setting_definitions ENABLE TRIGGER setting_definitions_frozen;
