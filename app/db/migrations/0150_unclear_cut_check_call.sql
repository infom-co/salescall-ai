-- 0150 切れ方の分からない通話の確かめのかけ直しと、声が届かず切れた通話（2026-10-30 管理者の決定と指摘・取り消せない決定の洗い出し。
-- 書いた人：CL-2。DESIGN.md 7-1c・7-2・5-2-2）
--
-- (1) 切れ方を決められない通話（営業拒否の言葉が無く、声が届かなかった証拠も無い＝judge の切れ方 unclear）は、すぐお客様へ確かめの
--     かけ直しをする（2026-10-30 管理者の決定。初めの値は「かけ直す」）。そのかけ直しが
--     - お客様の電話まで鳴った（状態の表の E1）のに、お客様が出て話さなかった ⇒ 両方の回線は元気＝前の通話はお客様が切った＝営業拒否と
--       みなす（発信禁止の登録は、ほかの営業拒否と同じ1か所＝core/judge.refusal。広げ方も同じ。台帳には、はっきり断られたとき
--       refused_in_call と別の理由 refused_unclear_checked で残す）。お客様が出て話を続けたら、その話の結果どおり。
--     - 鳴らなかった（E2）⇒ 回線のせい（回線は状態の表が確かめる）。テスト番号をあける長さの並び line.test_ring_gaps の1つ目（初め15分。
--       新しい設定は作らない）おいて、お詫びからかけ直す（そのあとは途中で切れた電話のふつうの決まり）。
--     予定を書くのは次の動きの1か所（core/next_action.after_call）。門は予定を読むだけ。同じ行（同じお客様）で確かめのかけ直しは1回だけ＝
--     下の表 unclear_checks（行ごとに1行。書くのは core/next_action だけ）。どの電話が確かめのかけ直しかを読むのも core/next_action の
--     1か所（check_outcome）。
--     確かめで決まるまでは（取り消せない決定の洗い出し）：発信禁止を登録しない・行を終えない・結果を確定しない（approval.after_judged の
--     印を付けない）。同じお客様のほかの行（番号を共有する行・管理番号）は「まだ決まっていない」として門で待たせる（門の理由
--     related_unclear_cut）。決まっていないかは下の salescall.unclear_check_pending の1か所（salescall.refusal_undecided も読む）。
-- (2) 設定 judge.unclear_disconnect_is_refusal（決められない切れ方を営業拒否にするか。初めの値は CL-2 の仮の値「はい」のままで、
--     10-30 のご決定＝初めの値は「かけ直す」と食い違っていた）を外す。決め方がご決定の1つになったので、設定は要らない。
-- (3) 発信禁止の理由 refused_found_after（通話後の検証で見つけた断り）は、書く入口がどこにも無く、判定の分岐が使われない道だった。
--     断りをあとから見つける道は「録音から作り直した報告」（call_report.save_rebuilt）の1本にそろえ、この理由ではもう書かない
--     （前の記録の値としては型に残す。理由の型から値は外せない）。
-- (4) 切れ方の「断った」は、営業拒否の言葉のあとに切れたときだけ（2026-10-30 管理者の指摘：声が届かないお客様が黙って切った通話を営業拒否
--     にしない。⛔ 前は、切れた瞬間に AI が話していた・だれも話していなかっただけで「断った」にしていた）。声が片方に届いていなかった
--     証拠（会話の報告の切れ方の新しい2項目 callee_cannot_hear・callee_silent。無い＝前の形の報告＝いいえ。データの形 版 1.06）があれば
--     「声が届かず切れた」＝「声が聞き取れない」と同じ扱い（こちらの不具合に数えない・途中で切れた電話と同じ・状態の表の E4）。
--     決めるのは core/judge.cut_of の1か所（表の作りは変えない。試行に記録する切れ方は「途中で切れた」interrupted）。

ALTER TYPE salescall.dnc_reason ADD VALUE IF NOT EXISTS 'refused_unclear_checked';

CREATE TABLE salescall.unclear_checks (
  tenant_id   uuid NOT NULL,
  client_id   uuid NOT NULL,
  task_id     uuid PRIMARY KEY,   -- 同じ行で1回だけ
  attempt_id  uuid NOT NULL UNIQUE REFERENCES salescall.attempts (attempt_id),   -- 切れ方の分からなかった通話
  decided_at  timestamptz NOT NULL DEFAULT now(),   -- 確かめると決めた時刻（このあと、その行で最初に発信まで進んだ電話が確かめのかけ直し）
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, task_id) REFERENCES salescall.tasks (client_id, task_id)
);
CREATE TRIGGER unclear_checks_frozen BEFORE UPDATE OR DELETE ON salescall.unclear_checks
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
ALTER TABLE salescall.unclear_checks ENABLE ROW LEVEL SECURITY;
CREATE POLICY client_scope ON salescall.unclear_checks
  USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
  WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client());
GRANT SELECT, INSERT ON salescall.unclear_checks TO salescall_app;

-- 確かめのかけ直しの答えを待っているか（1か所）：その通話で確かめのかけ直しをすると決めてあり、そのあと同じ行で発信まで進んだ電話
-- （こちらの不具合で終えた電話＝かけた予定のままかけ直す＝は数えない）が、まだ締められていない。行が終わっていれば待たない（確かめる
-- 電話がもう無い）。読むのは salescall.refusal_undecided（同じお客様のほかの行を門で待たせる）と core/approval.after_judged（結果を確定しない）
CREATE FUNCTION salescall.unclear_check_pending(p_attempt uuid) RETURNS boolean
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT EXISTS (
    SELECT 1 FROM salescall.unclear_checks u
    WHERE u.attempt_id = p_attempt
      AND NOT EXISTS (SELECT 1 FROM salescall.attempts c
                      WHERE c.task_id = u.task_id AND c.claimed_at > u.decided_at AND salescall.is_dialed(c.state)
                        AND NOT salescall.trouble_ours(c.trouble) AND salescall.is_settled(c.attempt_id))
      AND coalesce((SELECT n.kind::text FROM salescall.current_next_actions n WHERE n.task_id = u.task_id), 'call') <> 'end')
$$;
GRANT EXECUTE ON FUNCTION salescall.unclear_check_pending(uuid) TO salescall_app;

-- 断られたかがまだ決まっていない（0118 の形に、確かめのかけ直しの答えを待っている通話を足す）
CREATE OR REPLACE FUNCTION salescall.refusal_undecided(p_attempt uuid) RETURNS boolean
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT NOT salescall.is_settled(p_attempt)
      OR salescall.unclear_check_pending(p_attempt)
      OR (salescall.unknown_pending(p_attempt)
          AND NOT coalesce((SELECT a.cut_off FROM salescall.attempts a WHERE a.attempt_id = p_attempt), false)
          AND NOT coalesce((SELECT (r.detail ->> 'refusal_known')::boolean FROM salescall.task_results r
                            WHERE r.attempt_id = p_attempt ORDER BY r.version_no DESC LIMIT 1), false))
$$;

-- ═════════ (2) 設定 judge.unclear_disconnect_is_refusal を外す ═════════
ALTER TABLE salescall.setting_values DISABLE TRIGGER setting_values_frozen;
ALTER TABLE salescall.setting_definitions DISABLE TRIGGER setting_definitions_frozen;
DELETE FROM salescall.setting_values WHERE key = 'judge.unclear_disconnect_is_refusal';
DELETE FROM salescall.setting_definitions WHERE key = 'judge.unclear_disconnect_is_refusal';
ALTER TABLE salescall.setting_definitions ENABLE TRIGGER setting_definitions_frozen;
ALTER TABLE salescall.setting_values ENABLE TRIGGER setting_values_frozen;

-- ═════════ (3) 通話後の検証で見つけた断りの理由では、もう書かない ═════════
ALTER TABLE salescall.dnc_entries ADD CONSTRAINT dnc_entries_found_after_retired
  CHECK (reason::text <> 'refused_found_after') NOT VALID;
