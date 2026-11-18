-- 0028 発信対象の一覧の、お客様の会社名の読みと、目的のお客様の名前・読み・役職（Intent.md 6-2・6-8・7-1。DESIGN.md 3-4）。
-- 書いた人：CL-2
-- - 名前は表記と読みを持つ（読みの無い名前は持たない＝取込で止める）。読みを決めたもの（入口・AI・契約者）も残す
--   （決め方は core/readings.py の1か所。2026-10-16 ユーザー：「検証中はAIが認識したものを私が確認します」）。
-- - 目的のお客様の名前・役職が要るかは、業務設定の版の「目的のお客様」の種類で決まる（core/business_settings.py の1か所）。
-- - 書くのは取込（core/importer.py）だけ（機械が見張る）。タスクは変えない（直すときは新しい実施回）。
ALTER TABLE salescall.tasks
  ADD COLUMN source_name_reading    text CHECK (source_name_reading IS NULL OR btrim(source_name_reading) <> ''),
  ADD COLUMN source_name_reading_by text CHECK (source_name_reading_by IN ('given', 'ai', 'person')),
  ADD COLUMN target_name            text CHECK (target_name IS NULL OR btrim(target_name) <> ''),
  ADD COLUMN target_name_reading    text CHECK (target_name_reading IS NULL OR btrim(target_name_reading) <> ''),
  ADD COLUMN target_name_reading_by text CHECK (target_name_reading_by IN ('given', 'ai', 'person')),
  ADD COLUMN target_role            text CHECK (target_role IS NULL OR btrim(target_role) <> ''),
  ADD CONSTRAINT task_name_has_reading
    CHECK ((source_name_reading IS NULL) = (source_name_reading_by IS NULL)
           AND (source_name IS NULL) = (source_name_reading IS NULL)
           AND (target_name IS NULL) = (target_name_reading IS NULL)
           AND (target_name_reading IS NULL) = (target_name_reading_by IS NULL));
