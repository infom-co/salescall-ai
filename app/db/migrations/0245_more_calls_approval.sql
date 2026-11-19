-- 0245 約束・お詫びの電話で繋がらずに終えた行を、契約者の承認で、さらにかける（2026-11-19。書いた人：CL-2。DESIGN.md 3-4-1・15-2 の878）
--      ✅ 2026-11-19 ユーザー：「約束の電話やお詫びの電話は、その行だけ、契約者が承認ボタンを押したら、さらに3回かける。それでも繋がらなければ、
--      再度承認ボタンを出す。繋がるまで承認ボタンは出し続ける。。承認ボタンが押されなければかけない」・「約束+お詫びでかけられなかったものすべて」・
--      「ダッシュボードの取り込んだ行に承認ボタンを出します」
--
-- ⛔ 前は、お客様と約束したかけ直しを決めた日数・回数かけても話せずに終えた行（callback_exact_unreached・callback_unspecified_time）と、言ってはいけない
--    ことのあとのお詫びの電話を3回かけても届かずに終えた行（apology_unreached）は、終えたまま（番号を直したときだけ戻した）。お詫びの印を持つ約束の
--    電話で終えた行は、お詫びが届かないまま、だれにも知らせなかった。
-- ⇒ (1) 承認（more_call_approvals）：その行のいまの予定（終えた予定＝end_seq）ごとに1つ。押せるのは、ログインした管理者か契約者（いま操作している方）。
--        押せるのは、その行のいまの予定が、その終えた予定のときだけ（どの終わりの理由で押せるかは core/next_action.more_calls_offered の1か所）。
--    (2) 回数の数え始め（task_calls_from の1か所）：番号の直し（task_count_from＝0180・0194）か、この承認の、あとのほう。押したら、番号を直して戻した
--        ときと同じく、行の回数・約束の回数・お詫びの電話の回数を0から数え直す（計画書 7-1 (3)(8)(15)。同じお客様への1日の回数と最短の間隔は
--        数え直さない）。番号の直しの数え始め（task_count_from）を読む、サブ・前の番号の決まりは変えない（番号は替わっていない）。

CREATE TABLE salescall.more_call_approvals (
  tenant_id    uuid NOT NULL,
  client_id    uuid NOT NULL,
  seq          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  task_id      uuid NOT NULL,
  end_seq      integer NOT NULL,                                     -- 押したときの、その行のいまの予定（終えた予定）
  approved_by  text NOT NULL CHECK (btrim(approved_by) <> ''),
  approved_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (task_id, end_seq),
  FOREIGN KEY (task_id, end_seq) REFERENCES salescall.task_next_actions (task_id, seq),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id)
);
CREATE TRIGGER stamp_now BEFORE INSERT ON salescall.more_call_approvals FOR EACH ROW EXECUTE FUNCTION salescall.stamp_now('approved_at');
CREATE TRIGGER more_call_approvals_frozen BEFORE UPDATE OR DELETE ON salescall.more_call_approvals
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
ALTER TABLE salescall.more_call_approvals ENABLE ROW LEVEL SECURITY;
CREATE POLICY client_scope ON salescall.more_call_approvals
  USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
  WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client());
GRANT SELECT, INSERT ON salescall.more_call_approvals TO salescall_app;

-- 押せるのは、管理者か契約者（いま操作している方）で、その行のいまの予定が、その終えた予定のときだけ
CREATE FUNCTION salescall.more_call_approval_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT salescall.cur_is_person() THEN
    RAISE EXCEPTION 'さらにかける承認を押せるのは、管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'more_call_approval_by_person';
  END IF;
  IF NEW.approved_by IS DISTINCT FROM salescall.cur_actor() THEN
    RAISE EXCEPTION '承認した方は、いま操作している方です' USING ERRCODE = 'check_violation', CONSTRAINT = 'more_call_approved_by';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM salescall.current_next_actions n
                 WHERE n.task_id = NEW.task_id AND n.seq = NEW.end_seq AND n.kind = 'end') THEN
    RAISE EXCEPTION '承認できるのは、その行のいまの終えた予定だけです' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'more_call_approval_current_end';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER more_call_approval_guard BEFORE INSERT ON salescall.more_call_approvals
  FOR EACH ROW EXECUTE FUNCTION salescall.more_call_approval_guard();

-- 行の回数を数え始める時刻（1か所。core/attempts.count_start が読む）：番号の直しか、さらにかける承認の、あとのほう（無ければ -infinity）
CREATE FUNCTION salescall.task_calls_from(p_task uuid) RETURNS timestamptz LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT greatest(salescall.task_count_from(p_task),
                  coalesce((SELECT max(m.approved_at) FROM salescall.more_call_approvals m WHERE m.task_id = p_task), '-infinity'::timestamptz))
$$;
REVOKE ALL ON FUNCTION salescall.task_calls_from(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.task_calls_from(uuid) TO salescall_app;
