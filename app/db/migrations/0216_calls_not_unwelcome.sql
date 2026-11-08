-- 0216 お客様から見て煙たがられないかけ方（2026-11-08。書いた人：CL-2。DESIGN.md 5-2・7-1d・7-2・3-4-1 の【0215】・2章 Q200〜・15-2 の730〜）
--
-- ✅ 2026-11-08 ユーザー＝計画書 7-3「お客様から見て煙たがられないかけ方」(1)〜(4)：
-- (1) 同じ番号へ、ほかの業務・ほかの実施回・ほかの依頼者からかける電話の間は1つの決まり：依頼者ごとの設定 gate.same_number_gap（初めの値は
--     "next_day"＝その日はかけない）。用件の断り・苦情のあとは、その依頼者からはかけない（依頼者ごとの設定 calling.call_after_refusal。初めは
--     「かけない」）。声と発信番号は必ず替える。
--     ⛔ 前は、同じ依頼者なら、同じ番号への最短の間隔5分・1日2回だけ（行をまたいで数えた＝gate._pacing）で、依頼者をまたぐと3日
--     （システムだけの設定 gate.cross_client_interval）。同じ依頼者のほかの業務の電話が、断られた・怒られた10分後に同じ会社名で鳴っていた。
--     ほかの契約者とまたぐ間隔（gate.other_contractor_interval＝計画書 1-4。契約者の設定）は別の決まりのまま（混ぜない）。
-- (2) 言ってはいけないことを言った通話で、お客様がかけ直しを約束していたとき：お詫びは約束の時刻の電話の頭で伝え、そのまま約束の用件を続ける
--     （次の予定のお詫びの理由 forbidden_speech。契約者がお詫びの案を承認するまで、その電話はかけない＝salescall.task_apology_unapproved）。
-- (3) 幅の約束で出ないとき：1日に決めた回数まで（業務ごとの設定 callback.window_daily_max。初め3）。端が言葉で決まっていない側は、お客様の
--     営業時間（callee.business_hours）で切る（決まりは core/calling_hours.callee_range の1か所。設定の意味を直す）。
-- (4) こちらの止めで、時刻ちょうどの約束の時刻にかけられなかったとき：その日のうちにかけられるようになったら、すぐお詫びつきでかける（お客様
--     指定の歯止め時間の中で）。その電話の期限が次の予定の列 window_recover_until（今日の歯止め時間の終わり）。

-- ═════════ (1) 設定の値の形「その日はかけない、または間隔」 ═════════
CREATE OR REPLACE FUNCTION salescall.setting_value_ok(t salescall.setting_value_type, v jsonb) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE t
    WHEN 'boolean' THEN jsonb_typeof(v) = 'boolean'
    WHEN 'integer' THEN salescall.setting_whole_number(v, 0)
    WHEN 'seconds' THEN salescall.setting_whole_number(v, 1)
    WHEN 'ratio'   THEN CASE WHEN jsonb_typeof(v) <> 'number' THEN false
                             ELSE (v #>> '{}')::numeric BETWEEN 0 AND 1 END
    WHEN 'integer_list' THEN CASE WHEN jsonb_typeof(v) <> 'array' THEN false
                                  WHEN jsonb_array_length(v) < 1 THEN false
                                  ELSE NOT EXISTS (SELECT 1 FROM jsonb_array_elements(v) e
                                                   WHERE NOT salescall.setting_whole_number(e, 1)) END
    -- 曜日ごとの時間帯（日本時間）。例：{"mon": [["09:00","18:00"]], ..., "sun": [], "holiday": []}。8つの鍵が全部要る
    WHEN 'weekly_hours' THEN CASE WHEN jsonb_typeof(v) <> 'object' THEN false
                                  WHEN (SELECT count(*) FROM jsonb_object_keys(v)) <> 8 THEN false
                                  WHEN NOT (v ?& ARRAY['mon','tue','wed','thu','fri','sat','sun','holiday']) THEN false
                                  ELSE NOT EXISTS (SELECT 1 FROM jsonb_each(v) d WHERE NOT salescall.setting_hours_ok(d.value)) END
    -- 1日の中の時刻の範囲（日本時間）。例：["09:00","19:00"]・["00:00","24:00"]（1日じゅう）。終わりが始まりより前なら日をまたぐ（["18:00","03:00"]＝翌3時まで）
    WHEN 'daily_range' THEN salescall.setting_daily_range_ok(v)
    -- 1日の中の時刻の範囲の並び（0139）。例：[["00:00","09:00"],["14:00","24:00"]]・[["22:00","02:00"]]・[["00:00","24:00"]]。
    -- 1つ以上・それぞれ daily_range の形・範囲どうしが重ならない（日をまたぐ範囲は、夜の側と朝の側の両方で比べる）
    WHEN 'daily_ranges' THEN CASE WHEN jsonb_typeof(v) <> 'array' THEN false
                                  WHEN jsonb_array_length(v) < 1 THEN false
                                  WHEN EXISTS (SELECT 1 FROM jsonb_array_elements(v) r WHERE NOT salescall.setting_daily_range_ok(r)) THEN false
                                  ELSE NOT EXISTS (SELECT 1 FROM salescall.daily_range_spans(v) x
                                                   JOIN salescall.daily_range_spans(v) y ON x.i < y.i AND x.lo < y.hi AND y.lo < x.hi) END
    -- 1日を分ける区切りの時（例：[12, 15]＝午前・昼過ぎ・夕方）。1〜23 の整数を、小さい順に重ならずに
    WHEN 'hour_list' THEN CASE WHEN jsonb_typeof(v) <> 'array' THEN false
                               WHEN jsonb_array_length(v) < 1 THEN false
                               WHEN EXISTS (SELECT 1 FROM jsonb_array_elements(v) e
                                            WHERE NOT salescall.setting_whole_number(e, 1) OR (e #>> '{}')::numeric > 23) THEN false
                               ELSE NOT EXISTS (SELECT 1 FROM jsonb_array_elements(v) WITH ORDINALITY a(e, i)
                                                JOIN jsonb_array_elements(v) WITH ORDINALITY b(e, i) ON b.i = a.i + 1
                                                WHERE (b.e #>> '{}')::numeric <= (a.e #>> '{}')::numeric) END
    -- 間隔か、かけない（2026-10-16 ユーザー：「○○時間、○○日、かけない」）。例：{"hours": 6}・{"days": 3}・"never"
    WHEN 'gap_or_never' THEN CASE WHEN v = '"never"'::jsonb THEN true
                                  WHEN jsonb_typeof(v) <> 'object' THEN false
                                  WHEN (SELECT count(*) FROM jsonb_object_keys(v)) <> 1 THEN false
                                  WHEN v ? 'hours' THEN salescall.setting_whole_number(v -> 'hours', 1)
                                  WHEN v ? 'days' THEN salescall.setting_whole_number(v -> 'days', 1)
                                  ELSE false END
    -- その日はかけない、または間隔（0215。2026-11-08 ユーザー）。例："next_day"・{"hours": 2}・{"days": 3}
    WHEN 'day_or_gap' THEN CASE WHEN v = '"next_day"'::jsonb THEN true
                                WHEN jsonb_typeof(v) <> 'object' THEN false
                                WHEN (SELECT count(*) FROM jsonb_object_keys(v)) <> 1 THEN false
                                WHEN v ? 'hours' THEN salescall.setting_whole_number(v -> 'hours', 1)
                                WHEN v ? 'days' THEN salescall.setting_whole_number(v -> 'days', 1)
                                ELSE false END
    -- 上限（0069）：null＝上限なし、または1以上の整数
    WHEN 'limit' THEN jsonb_typeof(v) = 'null' OR salescall.setting_whole_number(v, 1)
    -- 電話番号（0133）：null＝まだ無い、またはそろえた番号（E.164。core/phone.normalize の形）
    WHEN 'phone' THEN jsonb_typeof(v) = 'null' OR (jsonb_typeof(v) = 'string' AND (v #>> '{}') ~ '^\+[1-9][0-9]{7,14}$')
  END
$$;

-- ═════════ (1) 設定：同じ番号へ、ほかの行からかける電話の間・断りと苦情のあと ═════════
-- 依頼者をまたぐ間隔（システムだけの3日）は、依頼者ごとの「同じ番号へのほかの電話の間」に入った（読む所が無くなったので消す。値も鍵ごと）
ALTER TABLE salescall.setting_values DISABLE TRIGGER setting_values_frozen;
ALTER TABLE salescall.setting_definitions DISABLE TRIGGER setting_definitions_frozen;
DELETE FROM salescall.setting_values WHERE key = 'gate.cross_client_interval';
DELETE FROM salescall.setting_definitions WHERE key = 'gate.cross_client_interval';
-- お客様の時間の設定の意味：言葉で決まっていない端は営業時間で切る（(3)。決まりは core/calling_hours.callee_range の1か所）
UPDATE salescall.setting_definitions SET meaning =
 'お客様（発信先）の営業時間です。お客様が言った時刻に午前・午後が無いとき、営業時間に入るほうを補います（両方入る・どちらも入らないとき'
 'だけ聞きます）。お客様が言ったかけ直しの幅のうち、言葉で端が決まっていない側（「〜以降」の終わり・「午前中」「朝」の始まり・「午後」の'
 '終わり・「〜まで」の始まり）は、この時間で切ります（2026-11-08 ユーザー：「1日3回・営業時間内」）。この時間で切ると幅が空になるとき'
 '（営業時間が12時からのお店の「朝」・「20時以降」など）だけ、お客様指定の歯止め時間で切ります。日をまたいでよい'
WHERE key = 'callee.business_hours';
UPDATE salescall.setting_definitions SET meaning =
 'お客様が日時を指定したとき（「朝なら」「夜なら」「3時まで」「4時以降」など）に、かけてよい時間の歯止め（毎日の始まり〜終わり）です。'
 'お客様の指定なので、依頼者のかける時間帯・NG時間の外でもかけますが、この時間の外にはかけません。お客様の言葉で端が決まっていない側'
 '（「朝」の始まり・「午後」の終わり・「〜まで」の始まり・「〜以降」の終わり）は、お客様の営業時間で切り（2026-11-08 ユーザー）、営業時間で'
 '切ると空になるときだけ、この時間で埋めます。お客様が時刻そのものを言ったとき（「明日の朝6時に」など）は、この時間の外でもかけます。'
 'こちらの止め（緊急停止・回線の不具合など）で約束の時刻にかけられなかった電話を、その日のうちにかけるときも、この時間の中だけです'
WHERE key = 'callee.designated_limit';
ALTER TABLE salescall.setting_definitions ENABLE TRIGGER setting_definitions_frozen;
ALTER TABLE salescall.setting_values ENABLE TRIGGER setting_values_frozen;

INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, finest_scope) VALUES
('gate.same_number_gap', 'override', 'day_or_gap',
 '同じ番号へ、この依頼者のほかの業務・ほかの実施回の電話や、同じ契約者のほかの依頼者の電話がかかったあと、この依頼者の電話をかけるまでの間'
 'です。"next_day"＝その日はかけない（次の日から。初め）・{"hours": n}＝n 時間あける・{"days": n}＝n 日あける。どの業務・実施回・依頼者からの'
 '電話も同じ決まりで、声と発信番号は必ず替えます。お客様と約束したかけ直し・途中で切れたあとのかけ直し・お詫びの電話は待たせません。'
 'ほかの契約者の電話との間は、契約者の設定（ほかの契約者と同じ番号にかけた場合の間隔）で別に決めます（2026-11-08 ユーザー：「依頼者ごとの'
 '設定でいいが、声と電話番号は必ず変える。依頼者をまたぐ電話も単に3日を空けるから、こちらに合わせる」）',
 'DESIGN.md 5-2 の5', 'client'),
('calling.call_after_refusal', 'override', 'boolean',
 'この依頼者のほかの業務で、用件を断られた（「うちは要らない」など）・苦情があったお客様に、この依頼者のほかの業務・ほかの実施回でかけるか'
 'です。いいえ（初め）＝かけない（その依頼者からは、そのお客様にもうかけません）／はい＝かける（同じ番号へのほかの電話の間の決まりのとおり）。'
 '同じ業務で用件を断られたお客様に次の実施回でかけるかは、業務の設定（断られたお客様に同じ業務でまたかけるか）で決めます。もうかけないでと'
 '言われたお客様（発信禁止）は、設定に関係なくかけません（2026-11-08 ユーザー：「用件の断り・苦情のあとは、その依頼者からはかけない」）',
 'DESIGN.md 5-2-1', 'client'),
('callback.window_daily_max', 'override', 'integer',
 'お客様が言った幅のかけ直しの約束（「3時以降」「明日の午後」「明日の午前中」など）で、お客様が出なかった・目的の方に繋がらなかったとき、'
 '1日にかける回数の上限です（初め3回。1以上）。お客様が時刻ちょうどを言った約束（「明日の15時」）は、この設定でなく、その日の時間の中の'
 '決まった本数です（2026-11-08 ユーザー：「1日3回・営業時間内」）',
 'DESIGN.md 7-2', 'business');
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('gate.same_number_gap', 'system', '"next_day"', 'decided',
 '2026-11-08 ユーザー（計画書 7-3 (1)）：「依頼者ごとの設定（…初めの値はその日はかけない…）でいいが、声と電話番号は必ず変える」',
 '依頼者ごとに、お客様の受け止め方を見て', 'ユーザー'),
('calling.call_after_refusal', 'system', 'false', 'decided',
 '2026-11-08 ユーザー（計画書 7-3 (1)）：「用件の断り・苦情のあとは、その依頼者からはかけない＝契約者が依頼者ごとに変えられる」',
 '依頼者ごとに契約者が選ぶとき', 'ユーザー'),
('callback.window_daily_max', 'system', '3', 'decided',
 '2026-11-08 ユーザー（計画書 7-3 (3)）：「1日3回・営業時間内（幅の約束で出ないときは1日3回まで＝契約者が業務ごとに変えられる）」',
 '業務ごとに契約者が選ぶとき', 'ユーザー');
ALTER TABLE salescall.setting_values ADD CONSTRAINT setting_values_window_daily_max_at_least_one
  CHECK (key <> 'callback.window_daily_max' OR (jsonb_typeof(value) = 'number' AND (value #>> '{}')::numeric >= 1));

-- ═════════ (1) 同じ番号への、ほかの行の電話からの間（間隔の計算はデータベースの中。ほかの依頼者・契約者の発信の時刻は出さない） ═════════
-- p_task＝これからかける行（その行の電話は、行の最短の間隔・1日の回数で数える＝gate._pacing）。p_same_gap＝同じ契約者のほかの行（同じ依頼者の
-- ほかの業務・実施回・ほかの依頼者）からの間（NULL＝その日はかけない＝その電話の日の次の日の始まりまで）。p_other_gap＝ほかの契約者（NULL＝
-- 間隔を置かない）。まだ待つ関係だけを返す。ほかの契約者の待つ時刻は1時間単位に切り上げる（正確な時刻を出さない）。
-- ⛔ 0116〜0215：同じ依頼者のほかの行は見ず（行をまたぐ同じお客様の最短の間隔5分・1日2回だけ）、ほかの依頼者は3日だった
DROP FUNCTION salescall.number_dial_wait_elsewhere(text, interval, interval);
CREATE FUNCTION salescall.number_dial_wait_elsewhere(p_phone text, p_task uuid, p_same_gap interval, p_other_gap interval)
RETURNS TABLE (relation text, wait_until timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT x.relation, CASE WHEN x.relation = 'same_tenant' THEN x.until
                          ELSE date_trunc('hour', x.until) + interval '1 hour' END
  FROM (SELECT CASE WHEN a.tenant_id = salescall.cur_tenant() THEN 'same_tenant' ELSE 'other_tenant' END AS relation,
               max(CASE WHEN a.tenant_id <> salescall.cur_tenant() THEN a.claimed_at + p_other_gap
                        WHEN p_same_gap IS NULL THEN salescall.jst_day_start(salescall.jst(a.claimed_at)::date + 1)
                        ELSE a.claimed_at + p_same_gap END) AS until
        FROM salescall.attempts a
        WHERE a.phone_e164 = p_phone AND a.task_id IS DISTINCT FROM p_task AND salescall.is_placed(a.state)
        GROUP BY 1) x
  WHERE x.until > now()
$$;
REVOKE ALL ON FUNCTION salescall.number_dial_wait_elsewhere(text, uuid, interval, interval) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.number_dial_wait_elsewhere(text, uuid, interval, interval) TO salescall_app;

-- ═════════ (1) 声と発信番号は必ず替える（ほかの行＝同じ依頼者のほかの業務・実施回も） ═════════
-- お客様が前に聞いた声：ほかの行（同じ依頼者のほかの業務・実施回・ほかの依頼者・ほかの契約者）の発信で、お客様が出た通話の声のうち、いちばん新しい
-- もの（行 p_task は、取引の依頼者の行だけ＝ほかの依頼者・契約者の番号を問い合わせる道にしない）。⛔ 0097〜0215：ほかの依頼者の発信だけ見ていた
DROP FUNCTION salescall.number_last_voice_elsewhere(text);
CREATE FUNCTION salescall.number_last_voice_elsewhere(p_phone text, p_task uuid) RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT v.voice_name FROM salescall.attempts a JOIN salescall.voice_types v ON v.code = a.voice_type
  WHERE a.phone_e164 = p_phone AND a.task_id IS DISTINCT FROM p_task
    AND EXISTS (SELECT 1 FROM salescall.tasks t WHERE t.task_id = p_task AND t.client_id = salescall.cur_client())   -- 取引の依頼者の行だけ
    AND salescall.is_dialed(a.state) AND salescall.reached_answer(a.attempt_id)
  ORDER BY a.claimed_at DESC LIMIT 1
$$;
REVOKE ALL ON FUNCTION salescall.number_last_voice_elsewhere(text, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.number_last_voice_elsewhere(text, uuid) TO salescall_app;
-- その回線の発信番号で、この番号へかけたことがあるか（あれば、この回線ではかけない）：ほかの依頼者が、期間に関係なく（2026-10-17 ユーザー）・
-- 同じ依頼者のほかの行（ほかの業務・実施回）の、いちばん新しい電話がこの回線（お客様が前に見た発信番号と替える。2026-11-08 ユーザー）。
-- ⛔ 0123〜0215：同じ依頼者のほかの業務の電話は、同じ発信番号でかけた
DROP FUNCTION salescall.line_called_number_elsewhere(uuid, text);
CREATE FUNCTION salescall.line_called_number_elsewhere(p_line uuid, p_phone text, p_task uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT EXISTS (SELECT 1 FROM salescall.attempts a
                 WHERE a.line_id = p_line AND a.tenant_id = salescall.cur_tenant() AND a.phone_e164 = p_phone
                   AND a.client_id IS DISTINCT FROM salescall.cur_client() AND salescall.is_placed(a.state))
      OR coalesce((SELECT a.line_id = p_line FROM salescall.attempts a
                   WHERE a.tenant_id = salescall.cur_tenant() AND a.client_id = salescall.cur_client() AND a.phone_e164 = p_phone
                     AND a.task_id IS DISTINCT FROM p_task AND salescall.is_placed(a.state)
                   ORDER BY a.claimed_at DESC LIMIT 1), false)
$$;
REVOKE ALL ON FUNCTION salescall.line_called_number_elsewhere(uuid, text, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.line_called_number_elsewhere(uuid, text, uuid) TO salescall_app;

-- ═════════ (1) 用件の断り・苦情のあとは、その依頼者からはかけない ═════════
-- 同じ依頼者の行で、用件・最終目的を断られて終えた（同じ業務の断りは、業務の設定 business.call_declined_again の salescall.declined_in_business が
-- 受け持つので除く）・苦情で終えた行の番号のどれか（依頼者が管理番号をお客様を表す変わらない番号としたときは管理番号も）か
CREATE FUNCTION salescall.declined_in_client(p_client uuid, p_business uuid, p_e164 text, p_client_ref text) RETURNS boolean
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT EXISTS (
    SELECT 1 FROM salescall.tasks t
    JOIN salescall.runs r ON r.client_id = t.client_id AND r.run_id = t.run_id
    WHERE t.client_id = p_client
      AND (SELECT n.kind = 'end' AND (n.end_reason = 'complaint'
                                      OR (n.end_reason = 'declined' AND r.business_id IS DISTINCT FROM p_business))
           FROM salescall.task_next_actions n WHERE n.task_id = t.task_id ORDER BY n.seq DESC LIMIT 1)
      AND (EXISTS (SELECT 1 FROM salescall.task_numbers k WHERE k.task_id = t.task_id AND k.phone_e164 = p_e164)
           OR (p_client_ref IS NOT NULL AND t.client_ref = p_client_ref)))
$$;
GRANT EXECUTE ON FUNCTION salescall.declined_in_client(uuid, uuid, text, text) TO salescall_app;

-- ═════════ (2) 約束の時刻の電話の頭でお詫び ═════════
ALTER TABLE salescall.task_next_actions DROP CONSTRAINT task_next_actions_apology_reasons;
ALTER TABLE salescall.task_next_actions ADD CONSTRAINT task_next_actions_apology_reasons
  CHECK (apology_reasons <@ ARRAY['missed_promise', 'cut_off', 'forbidden_speech']::text[]);

-- ═════════ (4) こちらの止めでかけられなかった時刻ちょうどの約束を、その日のうちに取り戻す電話の期限 ═════════
ALTER TABLE salescall.task_next_actions ADD COLUMN window_recover_until timestamptz;
ALTER TABLE salescall.task_next_actions ADD CONSTRAINT task_next_actions_window_recover
  CHECK (window_recover_until IS NULL OR (kind = 'call' AND window_until IS NOT NULL AND callee_at IS NOT NULL));
DROP VIEW salescall.current_next_actions;
CREATE VIEW salescall.current_next_actions WITH (security_invoker = true) AS
  SELECT n.* FROM salescall.task_next_actions n
  WHERE n.seq = (SELECT max(m.seq) FROM salescall.task_next_actions m WHERE m.task_id = n.task_id);
GRANT SELECT ON salescall.current_next_actions TO salescall_app;

-- ═════════ (2) お詫びの承認を待つか（門と候補を出す処理が読む1か所） ═════════
-- いまの予定がお詫びの理由 forbidden_speech（約束の時刻の電話の頭でお詫び）を持ち、その行のいちばん新しいお詫びの案を、契約者がまだ承認して
-- いない（承認するまで、その約束の電話はかけない＝お詫びの承認を待つ決まりはいままでと同じ＝core/apology.approve）
CREATE FUNCTION salescall.task_apology_unapproved(p_task uuid) RETURNS boolean
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT coalesce((SELECT 'forbidden_speech' = ANY (n.apology_reasons) FROM salescall.current_next_actions n WHERE n.task_id = p_task), false)
     AND NOT EXISTS (SELECT 1 FROM salescall.apology_approvals p
                     WHERE p.order_id = (SELECT max(o.order_id) FROM salescall.apology_orders o WHERE o.task_id = p_task))
$$;
GRANT EXECUTE ON FUNCTION salescall.task_apology_unapproved(uuid) TO salescall_app;
