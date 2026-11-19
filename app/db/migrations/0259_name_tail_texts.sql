-- 0259 名前の録音の台帳の続く言葉を、文で選ぶ（2026-11-19。書いた人：CL-2。DESIGN.md 3-8-3・15-2 の907。CL-1 の連絡帳 4-199 の 4-405 の (3)・CL-2 の連絡帳 4-405 の (3)）
--
-- ✅ 2026-11-15 ユーザー（共通の一言の文が変わったとき）「同じ決まりにする（お勧め）」＝文の違う承認済みの録音は使わない。共通の一言は文で比べて
-- いた（core/recordings._HAS_TEXTS）が、続く言葉（「はいらっしゃいますでしょうか？」など）は鍵（続く言葉の名前）だけで選んでいた（CL-1 の正本に、続く言葉の
-- 文を出す1か所が無かった）。⇒ 会話の担当が文の正本（phrases.NAME_TAILS）を作ったので、写し（core/common_phrases.NAME_TAILS。道具で作る）の文と同じ文の録音
-- だけを流す。選ぶのは今までどおり1か所（salescall.current_name_set）で、続く言葉の文を引数で受ける（役職の部分・名前の部分は文が行ごとに違うので受けない）。
-- 文を渡さない（null）続く言葉は、どの録音も選ばない（文を見ずに選ぶ道を残さない）。

DROP FUNCTION salescall.current_name_set(uuid, text, text, text, text, text);
CREATE FUNCTION salescall.current_name_set(p_business uuid, p_voice text, p_unit text, p_role text, p_reading text, p_tail text,
                                          p_tail_text text)
RETURNS uuid LANGUAGE sql STABLE AS $$
  SELECT s.set_id FROM salescall.recording_sets s
  WHERE s.unit = p_unit AND s.unit IN ('name_role', 'name', 'name_tail') AND s.voice_name = p_voice
    AND s.name_role IS NOT DISTINCT FROM p_role AND s.name_reading IS NOT DISTINCT FROM p_reading AND s.tail IS NOT DISTINCT FROM p_tail
    AND (s.unit <> 'name_tail' OR EXISTS (SELECT 1 FROM salescall.recordings r WHERE r.set_id = s.set_id AND r.spoken_text = p_tail_text))
    AND (s.tenant_id IS NULL OR s.business_id = p_business)
    AND salescall.set_ready(s.set_id)
  ORDER BY (s.tenant_id IS NULL), s.created_at DESC, s.set_id DESC
  LIMIT 1
$$;
GRANT EXECUTE ON FUNCTION salescall.current_name_set(uuid, text, text, text, text, text, text) TO salescall_app;

-- 続く言葉の名前の守りに mobile_confirm（かけた番号が携帯電話の番号のときの「（お名前）様で大丈夫でしょうか？」）・target_transfer（法人の行の
-- 「恐れ入りますが（呼び名）のお取り次ぎをお願いできますでしょうか？」）を足す（CL-1 の連絡帳 4-192 の (b) の (2)・4-197 の (a)。Python の一覧
-- core/recordings.NAME_TAILS＝common_phrases.NAME_TAILS の鍵と同じ並び。守りに無いと、台帳に録音を入れられず、その一言を流せない＝0221 と同じ）
ALTER TABLE salescall.recording_sets DROP CONSTRAINT recording_sets_name_key;
ALTER TABLE salescall.recording_sets
  ADD CONSTRAINT recording_sets_name_key CHECK (CASE unit
    WHEN 'name_role' THEN btrim(coalesce(name_role, '')) <> '' AND name_reading IS NULL AND tail IS NULL
    WHEN 'name' THEN tail IS NULL AND ((name_reading ~ '^[ぁ-ゖー・]+$' AND name_role IS NULL)
                                       OR (name_reading IS NULL AND btrim(coalesce(name_role, '')) <> ''))
    WHEN 'name_tail' THEN tail IN ('target', 'target_individual', 'target_confirm', 'target_reconfirm', 'promise_again',
                                   'target_successor', 'unavailable_confirm', 'carried_target', 'mobile_confirm', 'target_transfer')
                          AND name_role IS NULL AND name_reading IS NULL
    ELSE name_role IS NULL AND name_reading IS NULL AND tail IS NULL END);
