-- 0273 名前の録音の台帳の続く言葉に、受付の方への一文の「お役に立つお話」の形を足す（2026-11-20。書いた人：CL-2。DESIGN_15-2.md の931。CL-1 の連絡帳 4-217）
--
-- ✅ 2026-11-20 日本時間16時13分06秒 ユーザー「伺い87：受付の方への一文を「大切なお話」がメインだが、「お役に立つお話」も業務や場合により使う」：顧客獲得型の業務設定の
-- 受付の方への言葉（core/business_settings.reception_talk）が useful の業務は、受付の方への一文の続く言葉が reception_purpose_useful（「にとってお役に立つお話がございまして
-- お電話いたしました。」。名前の1か所は core/common_phrases.NAME_TAILS＝CL-1 の正本 phrases.NAME_TAILS の写し）。守りに無いと、台帳に録音を入れられず、その一言を流せない
-- （0221・0259・0260 と同じ）。写しの名前がどれも守りにあることは試験が見る（test_gate の test_every_name_tail_and_name_part_is_reserved）。
ALTER TABLE salescall.recording_sets DROP CONSTRAINT recording_sets_name_key;
ALTER TABLE salescall.recording_sets
  ADD CONSTRAINT recording_sets_name_key CHECK (CASE unit
    WHEN 'name_role' THEN btrim(coalesce(name_role, '')) <> '' AND name_reading IS NULL AND tail IS NULL
    WHEN 'name' THEN tail IS NULL AND ((name_reading ~ '^[ぁ-ゖー・]+$' AND name_role IS NULL)
                                       OR (name_reading IS NULL AND btrim(coalesce(name_role, '')) <> ''))
    WHEN 'name_tail' THEN tail IN ('target', 'target_individual', 'target_confirm', 'target_reconfirm', 'promise_again',
                                   'target_successor', 'unavailable_confirm', 'carried_target', 'mobile_confirm', 'target_transfer',
                                   'yes_received', 'acquire_glad', 'pain_ask', 'listen_why', 'listen_reason', 'listen_ask', 'reception_purpose',
                                   'reception_purpose_useful')
                          AND name_role IS NULL AND name_reading IS NULL
    ELSE name_role IS NULL AND name_reading IS NULL AND tail IS NULL END);
