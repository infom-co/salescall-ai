-- 0118 見直し16：続けて切れた通話の数え方・断られたかの決まり・発信に失敗が続く番号・赤字では止めない・声の性別（2026-10-23。書いた人：CL-2）
--
-- (1) 途中で切れた通話（お客様の意思と関係なく。cut_off）を数えるか：お詫びしてすぐかけ直す（続けて resume_in_a_row()＝2回まで）
--     うちは数えない。それでも続けて切れた通話からは、お客様に届いた1回（'reached'）として、行の回数・再発信の方針の回数・最小の
--     間隔・業務の1日の上限に数える（2026-10-18 ユーザー 案A：「同じ行で続けて2回まで。それでも切れたら、ふつうのかけ直しの決まり」。
--     ⛔ 見直し16：前は続けて何回切れても数えず、電波の悪いお客様へ、方針の間隔で終わりなくかけ続けた）。決めるのは dial_kind の1か所。
--     すぐかけ直してよいか（resume_allowed）も同じ数（cuts_in_a_row）を読む（Python の next_action は、ここを読む）。
-- (2) 断られたかがまだ決まっていない（refusal_undecided）：判定できない結果でも、途中で切れた通話（cut_off。お客様の意思と関係なく
--     切れた＝黙って切った断りではない）は、通話のあとの確かめ（別の耳で断りの言葉を探す。締めの前に済んでいる）で断りの言葉が
--     見つかっていなければ（見つかれば判定が営業拒否にしている）、断られていない（2026-10-22 ユーザー：「通話のあとの確かめで断りの
--     言葉が見つからなければ、お詫びのかけ直しをします。断り（営業拒否）の言葉が見つかったときは、営業拒否」）。⛔ 見直し16：前は
--     同じお客様のほかの行を、判定し直しが済むまで止め、行が終わったあとも止め続けた。判定し直しが要るか（unknown_pending）は別の問い
--     （判定できない結果を、依頼者へ渡す結果に直すため。core/settle.awaiting_rejudge が読む）。
-- (3) 発信に失敗（回線の部品が番号を受け付けない＝call_failed。お客様の電話は鳴っていない）が、同じ番号で続いた数（failed_in_a_row。
--     依頼者の連絡先ごと・どの行・どの回線でも）。門（core/gate）と次の動き（core/next_action）が、同じ数と同じ設定で決める
--     （core/attempts.number_unusable の1か所）。
-- (4) 発信に失敗のあとのかけ直しの順（2026-10-22 深夜 ユーザー：「同じ番号で続けて当日2回＆翌日1回、さらに2日後1回の計4回受け付け
--     られなかったら、その番号へはかけない」）：設定 dial.failed_retry_days＝続けて失敗した電話を、何日目にかけるかの並び（1＝最初に
--     失敗した日。初めの値 [1, 1, 2, 4]＝1日目に2回・2日目に1回・4日目に1回）。並びの数だけ続けて受け付けられなければ、その番号へは
--     かけない。0117 の dial.failed_same_number_max（続けて何回まで）は、これに置き換える（並びの数が回数）。
-- (5) 0117 で、時刻ちょうどの約束のいまの回に、お客様がその日を言った印（window_day_said）を付け忘れていた（その日はお客様が言った）。
-- (6) 赤字では止めない（2026-10-22 深夜 ユーザー：「赤字で、知らせてから1時間たっても確認ボタンが押されないとき：止めない。知らせる
--     だけでいい」）：止めているあいだに通す電話の表から赤字の行を外す（赤字の停止はもう作らない）。期限までに確認されなければ、
--     もう一度、契約者と管理者へ知らせる（1回。reminded_at）。
-- (8) お客様が言った時間の幅は、すべてお客様の指定（2026-10-23 ユーザー：「『朝』も時間帯が決まっているでしょ」「お客様の日時指定の
--     場合は、『お客様指定の歯止め時間』で見るようにしないといけないんじゃないですか？そして、『営業時間』と『お客様指定の歯止め
--     時間』も業務ごとに選択できるようにした方がいいね」）：依頼者の時間帯・NG時間・依頼者の深夜早朝の歯止めの外でもかける。端の
--     決まっていない側は、新しい設定「お客様指定の歯止め時間」（callee.designated_limit。業務ごと。初め 7:00〜22:00＝ユーザー OK）で
--     埋めて切る（お客様が口にした端は、その外でも切らない＝ユーザー OK）。お客様の営業時間（callee.business_hours）は、午前・午後の
--     読み分けだけ。決まりは core/calling_hours.callee_range の1か所。そのため、0117 の「言った端の回」（window_free）をやめ、幅の
--     始まり・終わりをお客様が口にしたか（window_said）を持つ（⛔ 見直し15 の「言った端の回だけ外でもかけ、言葉の表の端は依頼者の
--     時間で切る」は、「夜なら」を依頼者が18時までなら次の日の昼に回していた）。
-- (7) 声の性別の表示名（Intent.md 9-8「『男性／女性』は画面に出す表示名として設定で持つ」）：ほかの依頼者がこの番号へかけた声と
--     替えるとき、できれば別の性別の声にする（2026-10-17 ユーザー：「同じ男性ならサービスを聞く前に嫌悪感を持たれるので、Ledaが
--     対応するなど」。選ぶのは core/gate.choose_voice の1か所）。

-- ═════════ (1) ═════════
-- この行で、その試行まで（その試行を含む）続けて、途中で切れた発信の数（その試行が切れていなければ 0）
CREATE FUNCTION salescall.cuts_in_a_row(p_attempt uuid) RETURNS integer
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT count(*)::integer FROM salescall.attempts a JOIN salescall.attempts b ON b.task_id = a.task_id
  WHERE a.attempt_id = p_attempt AND salescall.is_dialed(b.state) AND b.claimed_at <= a.claimed_at AND coalesce(b.cut_off, false)
    AND NOT EXISTS (SELECT 1 FROM salescall.attempts c
                    WHERE c.task_id = a.task_id AND salescall.is_dialed(c.state) AND c.claimed_at > b.claimed_at
                      AND c.claimed_at <= a.claimed_at AND NOT coalesce(c.cut_off, false))
$$;
-- お詫びしてすぐかけ直すのは、同じ行で続けて何回まで（2026-10-18 ユーザー 案A。値はここだけ）
CREATE FUNCTION salescall.resume_in_a_row() RETURNS integer LANGUAGE sql IMMUTABLE AS $$ SELECT 2 $$;
-- その試行（途中で切れた通話）のあと、お詫びしてすぐかけ直してよいか
CREATE FUNCTION salescall.resume_allowed(p_attempt uuid) RETURNS boolean
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT salescall.cuts_in_a_row(p_attempt) BETWEEN 1 AND salescall.resume_in_a_row()
$$;
GRANT EXECUTE ON FUNCTION salescall.cuts_in_a_row(uuid) TO salescall_app;
GRANT EXECUTE ON FUNCTION salescall.resume_in_a_row() TO salescall_app;
GRANT EXECUTE ON FUNCTION salescall.resume_allowed(uuid) TO salescall_app;

CREATE OR REPLACE FUNCTION salescall.dial_kind(s salescall.attempt_state, p_cut_off boolean, p_attempt uuid) RETURNS text
LANGUAGE sql STABLE AS $$
  SELECT CASE WHEN NOT salescall.is_dialed(s) THEN NULL
              WHEN NOT salescall.is_placed(s) THEN 'not_placed'
              WHEN coalesce(p_cut_off, false) AND salescall.cuts_in_a_row(p_attempt) <= salescall.resume_in_a_row() THEN 'cut_off'
              WHEN s = 'technical_error' AND NOT salescall.reached_answer(p_attempt) THEN 'line_failure'
              ELSE 'reached' END
$$;

-- ═════════ (2) ═════════
CREATE OR REPLACE FUNCTION salescall.refusal_undecided(p_attempt uuid) RETURNS boolean
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT NOT salescall.is_settled(p_attempt)
      OR (salescall.unknown_pending(p_attempt)
          AND NOT coalesce((SELECT a.cut_off FROM salescall.attempts a WHERE a.attempt_id = p_attempt), false))
$$;

-- ═════════ (3) ═════════
-- その番号（依頼者の連絡先）への、いちばん新しい発信から続けて、発信に失敗した発信の数
CREATE FUNCTION salescall.failed_in_a_row(p_contact uuid) RETURNS integer
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT coalesce(min(x.n) FILTER (WHERE x.placed) - 1, count(*))::integer FROM (
    SELECT salescall.is_placed(a.state) AS placed, row_number() OVER (ORDER BY a.claimed_at DESC) AS n
    FROM salescall.attempts a WHERE a.contact_id = p_contact AND salescall.is_dialed(a.state)) x
$$;
GRANT EXECUTE ON FUNCTION salescall.failed_in_a_row(uuid) TO salescall_app;

-- ═════════ (4) ═════════
ALTER TABLE salescall.setting_values DROP CONSTRAINT setting_values_failed_same_number_at_least_one;
ALTER TABLE salescall.setting_values DISABLE TRIGGER setting_values_frozen;              -- 鍵ごと置き換える（値の意味が変わる）
ALTER TABLE salescall.setting_definitions DISABLE TRIGGER setting_definitions_frozen;
DELETE FROM salescall.setting_values WHERE key = 'dial.failed_same_number_max';
DELETE FROM salescall.setting_definitions WHERE key = 'dial.failed_same_number_max';
ALTER TABLE salescall.setting_definitions ENABLE TRIGGER setting_definitions_frozen;
ALTER TABLE salescall.setting_values ENABLE TRIGGER setting_values_frozen;
INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, finest_scope) VALUES
('dial.failed_retry_days', 'override', 'integer_list',
 '回線の部品（スマホのアプリ）が番号を受け付けず、電話をかけられなかった（お客様の電話は鳴っていない）ときに、同じ番号へ何日目に'
 'かけ直すかの並びです（1＝最初にかけられなかった日。例：[1, 1, 2, 4]＝その日にもう1回・次の日に1回・そのさらに2日後に1回）。'
 '並びの数だけ続けて受け付けられなければ、その番号へはかけません（お客様が言った別の番号があれば、そちらへかけます。無ければ、'
 'その行を終えます）。行の回数には数えません。同じ日のかけ直しは、同じお客様への最短の間隔をあけます。',
 'DESIGN.md 7-2', 'client');
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('dial.failed_retry_days', 'system', '[1, 1, 2, 4]', 'decided',
 '2026-10-22 深夜 ユーザー：「同じ番号で続けて当日2回＆翌日1回、さらに2日後1回の計4回受け付けられなかったら、その番号へはかけない」',
 '回線の部品の失敗の記録を見て', 'CL-2');
-- 並びは1日目から始まり、小さくならない（同じ日は続けてよい）。決まりは関数1つ（制約の中では問い合わせを書けない）
CREATE FUNCTION salescall.retry_days_ok(v jsonb) RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
  SELECT jsonb_typeof(v) = 'array' AND (v ->> 0) = '1'
     AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements(v) WITH ORDINALITY a(e, i)
                     JOIN jsonb_array_elements(v) WITH ORDINALITY b(e, i) ON b.i = a.i + 1
                     WHERE (b.e #>> '{}')::numeric < (a.e #>> '{}')::numeric)
$$;
ALTER TABLE salescall.setting_values ADD CONSTRAINT setting_values_failed_retry_days_order CHECK (
  key <> 'dial.failed_retry_days' OR salescall.retry_days_ok(value));

-- ═════════ (5) ═════════
ALTER TABLE salescall.task_next_actions DISABLE TRIGGER task_next_actions_frozen;
UPDATE salescall.task_next_actions SET window_day_said = true
WHERE callee_at IS NOT NULL AND window_free IS NOT NULL AND window_free = window_daily AND NOT window_day_said;
ALTER TABLE salescall.task_next_actions ENABLE TRIGGER task_next_actions_frozen;

-- ═════════ (6) ═════════
ALTER TABLE salescall.stop_lets_through DISABLE TRIGGER stop_lets_through_frozen;
DELETE FROM salescall.stop_lets_through WHERE cause = 'deficit';
ALTER TABLE salescall.stop_lets_through ENABLE TRIGGER stop_lets_through_frozen;

-- 赤字の停止を見守りに見せる関数は、使う所が無くなった（赤字の停止はもう作らない）
DROP FUNCTION salescall.open_system_stops(text);
ALTER TABLE salescall.deficit_warnings ADD COLUMN reminded_at timestamptz;   -- 期限までに確認されず、もう一度知らせた（1回）
CREATE OR REPLACE FUNCTION salescall.deficit_warning_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION '赤字の警告は消せません' USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  IF (NEW.tenant_id, NEW.client_id, NEW.warning_id, NEW.warned_at, NEW.deadline, NEW.figures)
     IS DISTINCT FROM (OLD.tenant_id, OLD.client_id, OLD.warning_id, OLD.warned_at, OLD.deadline, OLD.figures)
     OR (OLD.confirmed_at IS NOT NULL AND (NEW.confirmed_at, NEW.confirmed_by) IS DISTINCT FROM (OLD.confirmed_at, OLD.confirmed_by))
     OR (OLD.closed_at IS NOT NULL AND (NEW.closed_at, NEW.closed_why) IS DISTINCT FROM (OLD.closed_at, OLD.closed_why))
     OR (OLD.reminded_at IS NOT NULL AND NEW.reminded_at IS DISTINCT FROM OLD.reminded_at) THEN
    RAISE EXCEPTION '赤字の警告は、確認（1回）・もう一度の知らせ（1回）・閉じる（1回）しかできません'
      USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  IF OLD.confirmed_at IS NULL AND NEW.confirmed_at IS NOT NULL
     AND salescall.cur_actor_kind() IS DISTINCT FROM 'operator' AND salescall.cur_actor_kind() IS DISTINCT FROM 'admin' THEN
    RAISE EXCEPTION '赤字の警告を確認できるのは、管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'deficit_confirmed_by_person';
  END IF;
  RETURN NEW;
END $$;

-- ═════════ (7) ═════════
ALTER TABLE salescall.voices ADD COLUMN display_gender text CHECK (display_gender IN ('男性', '女性'));
UPDATE salescall.voices SET display_gender = '男性' WHERE voice_name = 'Sadachbia';
UPDATE salescall.voices SET display_gender = '女性' WHERE voice_name = 'Leda';
-- 性別の表示名は、声を足すときに決める（あとから変えない＝同じ声の、前の通話の記録の意味が変わらない）
CREATE OR REPLACE FUNCTION salescall.voices_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF current_user <> 'salescall_owner' AND salescall.cur_actor_kind() IS DISTINCT FROM 'admin' THEN
    RAISE EXCEPTION '声の一覧を変えられるのは管理者だけです' USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION '声は消せません（前の通話の記録が指しています。使わないときは印を付ける）' USING ERRCODE = 'check_violation';
  END IF;
  IF TG_OP = 'UPDATE' AND ((NEW.voice_name, NEW.note, NEW.added_at, NEW.display_gender)
                           IS DISTINCT FROM (OLD.voice_name, OLD.note, OLD.added_at, OLD.display_gender)
                           OR OLD.retired_at IS NOT NULL) THEN
    RAISE EXCEPTION '声の一覧で変えてよいのは、使わない印を1回付けることだけです' USING ERRCODE = 'check_violation';
  END IF;
  RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END $$;

-- ═════════ (8) ═════════
ALTER TABLE salescall.task_next_actions ADD COLUMN window_said boolean[];
ALTER TABLE salescall.task_next_actions DISABLE TRIGGER task_next_actions_frozen;
UPDATE salescall.task_next_actions SET window_said = CASE
    WHEN window_free = window_daily THEN ARRAY[true, true]
    WHEN window_free[1] = window_daily[1] THEN ARRAY[true, false]
    WHEN window_free[2] = window_daily[2] THEN ARRAY[false, true] END
WHERE window_free IS NOT NULL;
ALTER TABLE salescall.task_next_actions ENABLE TRIGGER task_next_actions_frozen;
DROP VIEW salescall.current_next_actions;
ALTER TABLE salescall.task_next_actions DROP COLUMN window_free;
ALTER TABLE salescall.task_next_actions ADD CONSTRAINT task_next_actions_window_said_check CHECK (
  window_said IS NULL OR (window_daily IS NOT NULL AND cardinality(window_said) = 2 AND array_position(window_said, NULL) IS NULL));
CREATE VIEW salescall.current_next_actions WITH (security_invoker = true) AS
  SELECT n.* FROM salescall.task_next_actions n
  WHERE n.seq = (SELECT max(m.seq) FROM salescall.task_next_actions m WHERE m.task_id = n.task_id);
GRANT SELECT ON salescall.current_next_actions TO salescall_app;

INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, finest_scope) VALUES
('callee.designated_limit', 'override', 'daily_range',
 'お客様が日時を指定したとき（「朝なら」「夜なら」「3時まで」「4時以降」など）に、かけてよい時間の歯止め（毎日の始まり〜終わり）です。'
 'お客様の指定なので、依頼者のかける時間帯・NG時間の外でもかけますが、お客様の言葉で端が決まっていない側（「朝」の始まり・'
 '「午後」の終わり・「〜まで」の始まり・「〜以降」の終わり）は、この時間で埋め、この時間の外にはかけません。お客様が時刻そのもの'
 'を言ったとき（「明日の朝6時に」など）は、この時間の外でもかけます。お客様の営業時間（午前・午後を読み分けるための時間）とは別です。',
 'DESIGN.md 7-1d・7-2', 'business');
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('callee.designated_limit', 'system', '["07:00", "22:00"]', 'decided',
 '2026-10-23 ユーザー：「お客様の日時指定の場合は、『お客様指定の歯止め時間』で見るようにしないといけないんじゃないですか？そして、'
 '『営業時間』と『お客様指定の歯止め時間』も業務ごとに選択できるようにした方がいいね」。初めの値 7:00〜22:00 は同じ日のユーザー「OK」',
 '業務ごとに、お客様の種類（会社・店舗）を見て', 'ユーザー');
-- 歯止めは1日の中で、始まりが終わりより前（日をまたがない）
ALTER TABLE salescall.setting_values ADD CONSTRAINT setting_values_designated_limit_order CHECK (
  key <> 'callee.designated_limit' OR ((value ->> 0) < (value ->> 1)));
-- お客様の営業時間（callee.business_hours）は、0045 から業務ごとに選べる（変えるものは無い）
