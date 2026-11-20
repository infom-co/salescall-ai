-- 0260 名前の録音の台帳の続く言葉に、顧客獲得型の続く言葉を足す（2026-11-20。書いた人：CL-2。DESIGN_15-2.md の913。CL-1 の連絡帳 4-208・4-211・4-214）
--
-- 顧客獲得型（✅ 2026-11-20 ユーザー「伺い78：…ア」）の行で流す、お名前の入る文の続く言葉（名前の1か所は core/common_phrases.NAME_TAILS＝CL-1 の正本 phrases.NAME_TAILS
-- の写し）：yes_received（「（お名前）様ももう大丈夫でしたよね。」）・acquire_glad・pain_ask・listen_why・listen_reason・listen_ask（本題の段）・reception_purpose（受付の方への
-- 一文）。守りに無いと、台帳に録音を入れられず、その一言を流せない（0221・0259 と同じ）。前に使っていた target_reconfirm は、台帳に残る録音のため受けたまま（新しくは
-- 入れない＝core/recordings.add_name_tail が写しの名前だけを受ける）。写しの名前がどれも守りにあることは試験が見る（test_gate の test_every_name_tail_and_name_part_is_reserved）。
ALTER TABLE salescall.recording_sets DROP CONSTRAINT recording_sets_name_key;
ALTER TABLE salescall.recording_sets
  ADD CONSTRAINT recording_sets_name_key CHECK (CASE unit
    WHEN 'name_role' THEN btrim(coalesce(name_role, '')) <> '' AND name_reading IS NULL AND tail IS NULL
    WHEN 'name' THEN tail IS NULL AND ((name_reading ~ '^[ぁ-ゖー・]+$' AND name_role IS NULL)
                                       OR (name_reading IS NULL AND btrim(coalesce(name_role, '')) <> ''))
    WHEN 'name_tail' THEN tail IN ('target', 'target_individual', 'target_confirm', 'target_reconfirm', 'promise_again',
                                   'target_successor', 'unavailable_confirm', 'carried_target', 'mobile_confirm', 'target_transfer',
                                   'yes_received', 'acquire_glad', 'pain_ask', 'listen_why', 'listen_reason', 'listen_ask', 'reception_purpose')
                          AND name_role IS NULL AND name_reading IS NULL
    ELSE name_role IS NULL AND name_reading IS NULL AND tail IS NULL END);
