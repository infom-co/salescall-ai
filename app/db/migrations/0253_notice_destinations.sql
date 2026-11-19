-- 0253 知らせの送り先と回数の設定（2026-11-19。書いた人：CL-2。DESIGN.md 9-2・15-2 の892。CL-3 の連絡帳 4-1b の 11）
-- ✅ 2026-11-18 管理者：Rakuten Link のショートメールが最優先、それが使えなければチャットワーク。送る順・文・送り直し・記録は CL-3 の
-- output/notices の1か所。ここは、どこへ（契約者の番号・部屋、管理者の番号・部屋）と、送り方ごとに何回まで、を決める値だけ。
-- 番号・部屋の値は git に書かない（画面で入れる。初めは「まだ無い」＝null）。チャットワークの鍵は、サーバーの秘密（設定の値には入れない）。
-- 管理者あての送り先は、契約者の送り先と別の名前（同じ名前にすると、契約者が値を入れていないとき、全体の値＝管理者の番号を受け継いでしまう）。
-- 送ってよい時間（notify.send_hours。CL-3 の連絡帳 4-1c の 52）：✅ 2026-11-19 管理者「(1) (2)はすぐ　そのほかはそれでいい」・「知らせを送って
-- よい時間の初めの値：『毎日8〜22時（祝日も同じ）』…OK」。契約者ごと（いちばん細かいのは契約者）・値の形はかける時間帯と同じ曜日ごとの幅。
-- 管理者あての知らせは全体の値。いつ送るか（すぐ・時間の内・毎朝）の決め方は CL-3 の output/notices.send_time の1か所。
-- 回数は1より小さくできない（0 にすると、どの知らせも送れず、送る処理が毎回断られる）：枠 notify.max_tries_floor（全体だけ・1）。

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
    -- チャットワークの部屋の番号（0253）：null＝まだ無い、または数字だけ
    WHEN 'chatwork_room' THEN jsonb_typeof(v) = 'null' OR (jsonb_typeof(v) = 'string' AND (v #>> '{}') ~ '^[0-9]{1,20}$')
  END
$$;

INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, finest_scope) VALUES
('notify.max_tries_floor', 'override', 'integer', '知らせを送ってみる回数の下の枠（これより少なくできない）', 'DESIGN.md 9-2', 'system');
INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, finest_scope, bound_key, bound_side) VALUES
('notify.max_tries_sms', 'override', 'integer', 'ショートメールで、1つの知らせを送ってみる回数の上限（使い切ったら次の送り方へ）', 'DESIGN.md 9-2', 'tenant',
 'notify.max_tries_floor', 'lower'),
('notify.max_tries_chatwork', 'override', 'integer', 'チャットワークで、1つの知らせを送ってみる回数の上限（使い切ったら送れていないと画面に出す）', 'DESIGN.md 9-2', 'tenant',
 'notify.max_tries_floor', 'lower');
INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, finest_scope) VALUES
('notify.send_hours', 'override', 'weekly_hours', '知らせ（ショートメール・チャットワーク）を送ってよい曜日と時間帯（日本時間。祝日は holiday）。停止・再開・緊急と、契約者に決めていただくことはいつでもすぐ送り、そのほかは、この時間の外に起きた分を時間が開いたときにまとめて送る（画面の知らせには、いつでもすぐ出る）', 'DESIGN.md 9-2', 'tenant'),
('notify.sms_to', 'override', 'phone', '契約者あての知らせを送る、契約者のショートメールの番号（Rakuten Link で送る）', 'DESIGN.md 9-2', 'tenant'),
('notify.chatwork_room', 'override', 'chatwork_room', '契約者あての知らせを送る、契約者のチャットワークの部屋（ショートメールで送れないとき）', 'DESIGN.md 9-2', 'tenant'),
('notify.admin_sms_to', 'override', 'phone', '管理者あての知らせを送る、管理者のショートメールの番号', 'DESIGN.md 9-2', 'system'),
('notify.admin_chatwork_room', 'override', 'chatwork_room', '管理者あての知らせを送る、管理者のチャットワークの部屋', 'DESIGN.md 9-2', 'system');
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('notify.sms_to', 'system', 'null', 'decided', '契約者ごとに画面で入れる（全体の値は無い）', '契約者が入れたとき', 'CL-2'),
('notify.chatwork_room', 'system', 'null', 'decided', '契約者ごとに画面で入れる（全体の値は無い）', '契約者が入れたとき', 'CL-2'),
('notify.admin_sms_to', 'system', 'null', 'decided', '管理者が画面で入れる', '管理者が入れたとき', 'CL-2'),
('notify.admin_chatwork_room', 'system', 'null', 'decided', '管理者が画面で入れる', '管理者が入れたとき', 'CL-2'),
('notify.max_tries_floor', 'system', '1', 'decided', '1回も送らない回数は、送らないのと同じ（知らせを取りこぼす）', '変えない', 'CL-2'),
('notify.send_hours', 'system', '{"mon": [["08:00","22:00"]], "tue": [["08:00","22:00"]], "wed": [["08:00","22:00"]], "thu": [["08:00","22:00"]], "fri": [["08:00","22:00"]], "sat": [["08:00","22:00"]], "sun": [["08:00","22:00"]], "holiday": [["08:00","22:00"]]}', 'decided', '✅ 2026-11-19 管理者「知らせを送ってよい時間の初めの値：毎日8〜22時（祝日も同じ）…OK」。契約者が設定で変えられる', '契約者が変えたとき', 'CL-2'),
('notify.max_tries_sms', 'system', '3', 'provisional', '電波の一時的な切れを越えられる回数（CL-2 の初めの値）', '送れなかった記録を見て', 'CL-2'),
('notify.max_tries_chatwork', 'system', '3', 'provisional', 'ショートメールと同じ回数（CL-2 の初めの値）', '送れなかった記録を見て', 'CL-2');
