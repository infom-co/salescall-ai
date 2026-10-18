-- 0070 業務ごとの1日にかける本数の上限（2026-10-18 ユーザー決定：「業務ごと、デフォルトは上限なし」。Intent.md 5-3 (8)。
-- DESIGN.md 5-2 の順7b・8章・15-2 の166。書いた人：CL-2）
--
-- 依頼者の受け入れの都合などで、業務ごとに、1日（日本時間）にかける本数を絞る。初めは上限なし。
-- 数えるのは、その業務の、営業として新しくかけた電話（お詫びの電話・途中で切れたあとのかけ直し・お客様と約束したかけ直しは数えず、
-- 上限でも止めない：もう話したお客様・お客様が頼んだ電話のため）。判断は門の1か所（gate.claim。理由 business_daily_limit）。

-- 値の形「上限」を確かめに足す（0069 で型に足した値）
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
    -- 1日の中の時刻の範囲（日本時間）。例：["09:00","19:00"]。終わりが始まりより前なら日をまたぐ（["18:00","03:00"]＝翌3時まで）
    WHEN 'daily_range' THEN CASE WHEN jsonb_typeof(v) <> 'array' THEN false
                                 WHEN jsonb_array_length(v) <> 2 THEN false
                                 WHEN jsonb_typeof(v -> 0) <> 'string' OR jsonb_typeof(v -> 1) <> 'string' THEN false
                                 WHEN NOT ((v ->> 0) ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$') THEN false
                                 WHEN NOT ((v ->> 1) ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$') THEN false
                                 ELSE (v ->> 0) <> (v ->> 1) END
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
    -- 上限（0069）：null＝上限なし、または1以上の整数
    WHEN 'limit' THEN jsonb_typeof(v) = 'null' OR salescall.setting_whole_number(v, 1)
  END
$$;

INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, finest_scope) VALUES
('calling.business_daily_dials', 'override', 'limit',
 'その業務で、1日（日本時間）にかけてよい本数の上限（業務ごと。空＝上限なし。お詫びの電話・途中で切れたあとのかけ直し・お客様と約束したかけ直しは数えず、止めない）',
 'DESIGN.md 5-2 の順7b', 'business');
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('calling.business_daily_dials', 'system', 'null', 'decided',
 '2026-10-18 ユーザー決定：「業務ごと、デフォルトは上限なし」', '依頼者の都合で絞りたい業務があるとき（業務ごとに入れる）', 'ユーザー');
