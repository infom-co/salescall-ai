-- 0217 1か月以上先のかけ直しは、初めの値は「かけない」（2026-11-09。書いた人：CL-2。DESIGN.md 7-2・7-1d の【0217】・8章・2章 Q213〜・15-2 の743〜）
--
-- ✅ 2026-11-08 ユーザー＝計画書 7-3 (3)：「1か月先に電話を入れるなんてことは通常ありえません。1か月以上先の予定がある場合、契約者が認めた場合
-- のみかけます。設定で、1か月以上先の架電はかけないがデフォルト。（選択式）」
-- (1) 1か月以上先＝かけ直しの予定の時刻（お客様が言った時刻・幅の始まり・再発信の方針の間隔で決めた時刻のどれも）の日が、その予定を決めた日の、
--     翌月の同じ日（その月に無ければ月の終わり。例：1月31日 ⇒ 2月28日）以上。日本時間の日で比べる（時刻は見ない）。決めるのは
--     salescall.month_ahead の1か所（次の動きの決定を書く1か所＝core/next_action._write と、門が読む待ちの1か所が、どちらもこれを読む）。
--     「決めた日」は、その行で、その時刻へかける予定を初めて決めた日（番号を直したときの予定の付け替えなど、同じ時刻のまま書き直しても
--     決めた日は変わらない）。まだ無ければ、いま。
-- (2) 業務ごとの設定 callback.month_ahead（選ぶ形）："never"＝かけない（初め。予定を書かずに、終わりの理由 callback_month_ahead で行を終える）／
--     "with_approval"＝契約者が認めたらかける（予定はそのまま書き、契約者に知らせ、認めるまで門がかけない。認めなければ行を終える）。
-- (3) 契約者の答え（認める・認めない）：表 task_month_ahead_answers（その行の、その時刻ごとに1回。管理者か契約者だけ。書くのは画面の入口
--     core/row_fixes.answer_month_ahead の1か所）。答えを待っているかは salescall.task_month_ahead_waiting の1か所（門・候補を出す処理・答えの
--     守りが読む）。
-- ⛔ 前は、何か月先の約束（「再来月の15日」・月ぎめの理由の翌月の回・再発信の方針の長い間隔）でも、そのままかけていた。

-- ═════════ (2) 設定 ═════════
INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, finest_scope, choices) VALUES
('callback.month_ahead', 'override', 'choice',
 'お客様とのかけ直しの予定（お客様が言った時刻・幅の始まり・再発信の方針の間隔で決めた時刻）が、その予定を決めた日から1か月以上先'
 '（翌月の同じ日から）のときに、かけるかです。never＝かけない（初め。その予定は書かずに、行を終えます。終わりの理由は「1か月以上先の'
 'かけ直し」）／with_approval＝契約者が認めたらかける（予定はそのまま残し、ダッシュボードで契約者が認めるまでかけません。認めなければ'
 '行を終えます）。お客様が口にした「来月の15日に」なども同じです（2026-11-08 ユーザー：「1か月先に電話を入れるなんてことは通常'
 'ありえません。1か月以上先の予定がある場合、契約者が認めた場合のみかけます。設定で、1か月以上先の架電はかけないがデフォルト。（選択式）」）',
 'DESIGN.md 7-2', 'business', ARRAY['never', 'with_approval']);
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('callback.month_ahead', 'system', '"never"', 'decided',
 '2026-11-08 ユーザー（計画書 7-3 (3)）：「設定で、1か月以上先の架電はかけないがデフォルト。（選択式）」',
 '業務ごとに契約者が選ぶとき', 'ユーザー');

-- ═════════ (1) 1か月以上先か（1か所） ═════════
CREATE FUNCTION salescall.month_ahead(p_task uuid, p_at timestamptz) RETURNS boolean
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT salescall.jst(p_at)::date
         >= (salescall.jst(coalesce((SELECT min(n.decided_at) FROM salescall.task_next_actions n
                                     WHERE n.task_id = p_task AND n.kind = 'call' AND n.call_at = p_at), now()))::date
             + interval '1 month')::date
$$;
REVOKE ALL ON FUNCTION salescall.month_ahead(uuid, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.month_ahead(uuid, timestamptz) TO salescall_app;

-- ═════════ (3) 契約者の答え ═════════
CREATE TABLE salescall.task_month_ahead_answers (
  tenant_id    uuid NOT NULL,
  client_id    uuid NOT NULL,
  seq          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  task_id      uuid NOT NULL,
  call_at      timestamptz NOT NULL,       -- 答えた予定の時刻（その行の、その時刻ごとに1回答える）
  approved     boolean NOT NULL,           -- 真＝かけると認めた・偽＝認めない（行を終える）
  answered_by  text NOT NULL CHECK (btrim(answered_by) <> ''),
  answered_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (task_id, call_at),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, task_id) REFERENCES salescall.tasks (client_id, task_id)
);
CREATE TRIGGER stamp_now BEFORE INSERT ON salescall.task_month_ahead_answers
  FOR EACH ROW EXECUTE FUNCTION salescall.stamp_now('answered_at');
CREATE TRIGGER task_month_ahead_answers_frozen BEFORE UPDATE OR DELETE ON salescall.task_month_ahead_answers
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
ALTER TABLE salescall.task_month_ahead_answers ENABLE ROW LEVEL SECURITY;
CREATE POLICY client_scope ON salescall.task_month_ahead_answers
  USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
  WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client());
GRANT SELECT, INSERT ON salescall.task_month_ahead_answers TO salescall_app;

-- 1か月以上先で、契約者がまだ認めていない予定の時刻か（次の動きの決定を書く1か所と、下の待ちの1か所が読む）
CREATE FUNCTION salescall.month_ahead_unapproved(p_task uuid, p_at timestamptz) RETURNS boolean
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT salescall.month_ahead(p_task, p_at)
     AND NOT EXISTS (SELECT 1 FROM salescall.task_month_ahead_answers k WHERE k.task_id = p_task AND k.call_at = p_at AND k.approved)
$$;
REVOKE ALL ON FUNCTION salescall.month_ahead_unapproved(uuid, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.month_ahead_unapproved(uuid, timestamptz) TO salescall_app;

-- 契約者の答えを待っている予定の時刻（無ければ NULL）：いまの予定が「かけ直す」で、1か月以上先で、まだ認めていない（認めなかった予定は、行を
-- 終えているので「かけ直す」でない）
CREATE FUNCTION salescall.task_month_ahead_waiting(p_task uuid) RETURNS timestamptz
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT n.call_at FROM salescall.current_next_actions n
  WHERE n.task_id = p_task AND n.kind = 'call' AND salescall.month_ahead_unapproved(p_task, n.call_at)
$$;
REVOKE ALL ON FUNCTION salescall.task_month_ahead_waiting(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.task_month_ahead_waiting(uuid) TO salescall_app;

CREATE FUNCTION salescall.task_month_ahead_answer_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT salescall.cur_is_person() THEN
    RAISE EXCEPTION '1か月以上先のかけ直しを認めるかを答えられるのは、管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'task_month_ahead_answer_by_person';
  END IF;
  IF NEW.answered_by IS DISTINCT FROM salescall.cur_actor() THEN
    RAISE EXCEPTION '答えた方は、いま操作している方です' USING ERRCODE = 'check_violation', CONSTRAINT = 'task_month_ahead_answer_answered_by';
  END IF;
  IF NEW.call_at IS DISTINCT FROM salescall.task_month_ahead_waiting(NEW.task_id) THEN
    RAISE EXCEPTION '答えられるのは、1か月以上先のかけ直しの予定で、契約者の答えを待っている行の、その時刻だけです' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'task_month_ahead_answer_waiting';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER task_month_ahead_answer_guard BEFORE INSERT ON salescall.task_month_ahead_answers
  FOR EACH ROW EXECUTE FUNCTION salescall.task_month_ahead_answer_guard();
