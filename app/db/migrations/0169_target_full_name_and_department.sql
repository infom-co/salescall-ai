-- 0169 2026-11-02 お呼びする方のフルネーム（表記と読み）と部署（CL-1 の連絡帳 4-65 の (d)。書いた人：CL-2。設計書 15-2 の430〜）。
--
-- ✅ 2026-11-01 日本時間23時59分 ユーザー：「名前以外に部署やフルネームを伝えて、取り次いでもらう努力をするべきじゃないでしょうか。
--   そうしないと、もう一度かけ直しても断られるだけですよ。」・11-02 1時22分 (a)「フルネームか部署かどちらかが分かれば、言い直す。
--   どちらもなければ、(イ)」（＝締めてかけ直さず、依頼者へ返す）。
-- - ふだんはお名字（target_name）でお呼びし、受付の方に「お名前だけではどの方か分からない」と言われたときだけ、部署とフルネームの
--   分かるほうでお呼びし直す（言い方は会話の側の1か所 instruction.Target.full_call）。
-- - フルネームは表記と読みを持つ（決め方は core/readings.py の1か所。お名字と同じく、読みが決まらなければ候補とまず言ってみる読み）。
--   部署は、呼ぶ一言の役職（target_role）に入っていないもの（名前のみの指定の行で、一覧に部署があるとき）。
-- - 書くのは取込（core/importer.py）だけ（0028 と同じ。タスクは変えない）。どれも無くてもよい（無い行で分からないと言われたら、
--   お伝えすることが無いので締め、依頼者に埋めていただく欄を返す＝business_settings.who_unclear_blanks）。
ALTER TABLE salescall.tasks
  ADD COLUMN target_full_name                    text CHECK (target_full_name IS NULL OR btrim(target_full_name) <> ''),
  ADD COLUMN target_full_name_reading            text CHECK (target_full_name_reading IS NULL OR btrim(target_full_name_reading) <> ''),
  ADD COLUMN target_full_name_reading_by         text CHECK (target_full_name_reading_by IN ('given', 'ai', 'person')),
  ADD COLUMN target_full_name_reading_candidates text[] NOT NULL DEFAULT '{}',
  ADD COLUMN target_full_name_reading_likely     text,
  ADD COLUMN target_department                   text CHECK (target_department IS NULL OR btrim(target_department) <> ''),
  ADD CONSTRAINT task_full_name_readings CHECK (
    -- 読みと、読みを決めたものは組（0030 のお名字と同じ決まり）
    (target_full_name_reading IS NULL) = (target_full_name_reading_by IS NULL)
    -- フルネームが無ければ、読みも候補も無い
    AND (target_full_name IS NOT NULL OR (target_full_name_reading IS NULL AND target_full_name_reading_candidates = '{}'
                                          AND target_full_name_reading_likely IS NULL))
    -- 決まった読みには候補を付けない
    AND (target_full_name_reading IS NULL OR (target_full_name_reading_candidates = '{}'
                                              AND target_full_name_reading_likely IS NULL))
    -- 読みは、ひらがな（長音・中黒を含む）
    AND (target_full_name_reading IS NULL OR target_full_name_reading ~ '^[ぁ-ゖー・]+$')
    AND (target_full_name_reading_likely IS NULL OR target_full_name_reading_likely ~ '^[ぁ-ゖー・]+$')
    AND array_to_string(target_full_name_reading_candidates, '') ~ '^[ぁ-ゖー・]*$');
