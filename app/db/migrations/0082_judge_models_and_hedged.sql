-- 0082 選択の設定・判定に使うAI・言い切っていない返事の言葉（DESIGN.md 8章・1-3・7-1b・15-2 の178。2026-10-18。書いた人：CL-2）
--
-- CL-1 の連絡帳 4-21（✅ ユーザー決定）：
-- 「契約者設定で、Jev も使うか、Sonnetだけにするかも選択できるようにした方がいいね」
--   ⇒ 契約者の設定「判定に使うAI」（judge.models。選択：sonnet＝Claude Sonnet 5.5（考える）だけ・既定／jev_and_sonnet＝Jev と
--      Sonnet 5.5（考える）の振り分け。Jev の確からしさ 0.95 以上は Jev。どちらを使うかを決めて呼ぶのは会話＝CL-1）。
-- 「yes,but 曖昧」：言い直しへの返事が、言い直した答えに寄っているが言い切っていなかった項目は、値はそのまま、その返事の言葉を
--   別に残す ⇒ 項目の値の表に hedged（お客様の言葉そのまま。依頼者へは別の欄に出す＝CL-3）。
--
-- (1) 選択の設定：設定の定義が、選べる選択肢（choices）を持つ。値は、その選択肢のどれか（文字）。選択の形の設定だけが選択肢を持つ。

ALTER TABLE salescall.setting_definitions ADD COLUMN choices text[];
ALTER TABLE salescall.setting_definitions ADD CONSTRAINT setting_definitions_choices
  CHECK ((value_type = 'choice') = (choices IS NOT NULL)
         AND (choices IS NULL OR (cardinality(choices) >= 2 AND array_position(choices, NULL) IS NULL)));

CREATE OR REPLACE FUNCTION salescall.setting_value_before_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE d salescall.setting_definitions;
BEGIN
  SELECT * INTO d FROM salescall.setting_definitions WHERE key = NEW.key;
  -- 選択の形は、値の形の確かめ（setting_value_ok）に無い：定義の選択肢のどれかの文字か、をここで確かめる（選択肢は定義が持つため）
  IF (CASE WHEN d.value_type = 'choice'
           THEN NOT (jsonb_typeof(NEW.value) = 'string' AND (NEW.value #>> '{}') = ANY(d.choices))
           ELSE NOT salescall.setting_value_ok(d.value_type, NEW.value) END) THEN
    RAISE EXCEPTION '設定 % の値の形が違います（%）%', NEW.key, d.value_type,
      CASE WHEN d.value_type = 'choice' THEN '。選べるのは ' || array_to_string(d.choices, '・') ELSE '' END
      USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.scope > d.finest_scope THEN
    RAISE EXCEPTION '設定 % は、%までしか決められません（%の値は入れられない）', NEW.key,
      CASE d.finest_scope WHEN 'system' THEN 'システム' WHEN 'tenant' THEN '契約者' WHEN 'client' THEN '依頼者' ELSE '業務' END,
      CASE NEW.scope WHEN 'tenant' THEN '契約者' WHEN 'client' THEN '依頼者' ELSE '業務' END
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;

-- (2) 判定に使うAI（契約者ごと）
INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, finest_scope, choices) VALUES
('judge.models', 'override', 'choice',
 '答えの意味の2つ目の判定に使うAI（sonnet＝Claude Sonnet 5.5（考える）だけ／jev_and_sonnet＝Jev と Sonnet 5.5（考える）の振り分け。Jev の確からしさ 0.95 以上は Jev）。使い分けて呼ぶのは会話（CL-1）',
 'DESIGN.md 1-3・12-2', 'tenant', ARRAY['sonnet', 'jev_and_sonnet']);
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('judge.models', 'system', '"sonnet"', 'decided',
 '2026-10-18 ユーザー決定：「クレジットの使い残しはもったいないので、当面は、Sonnetだけを使いましょう。Jev検証時を除く」「契約者設定で、Jev も使うか、Sonnetだけにするかも選択できるようにした方がいいね」',
 'Jev を確かめたとき', 'ユーザー');

-- (3) 言い切っていない返事の言葉（項目ごと。値は取れた値のまま）
ALTER TABLE salescall.item_values ADD COLUMN hedged text CHECK (hedged IS NULL OR btrim(hedged) <> '');
