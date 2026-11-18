-- 0221 名前の録音の台帳の続く言葉に carried_target を足す（2026-11-13。書いた人：CL-2。DESIGN.md 3-8-3・2章 Q243・15-2 の778）
--
-- CL-1 の連絡帳 4-151 の伺い11（✅ 2026-11-13 日本時間22時17分01秒 ユーザー「11はイ」）：続きの電話で判断いただける代わりの方に繋がり、前の通話の答えに
-- お呼びした方の答えがあるときは、「恐れ入りますが」＋名前の部分「（〇〇様）」＋続く言葉「のほうからこれまでのお話はお聞き及びでしょうか？」で、お呼びした方から
-- 引き継いでいるかを伺う（会話の側の名乗りの部分 carried_target。名前の無い業務は役職の呼び方「採用のご担当者様」が名前の部分）。続く言葉の名前の正本は
-- CL-1 の instruction.Business.replay_parts、Python の一覧は core/recordings.NAME_TAILS（守りと同じ並び）。守りに無いと、台帳に録音を入れられず、その一言を
-- 流せない（0165 の unavailable_confirm と同じ）。

ALTER TABLE salescall.recording_sets DROP CONSTRAINT recording_sets_name_key;
ALTER TABLE salescall.recording_sets
  ADD CONSTRAINT recording_sets_name_key CHECK (CASE unit
    WHEN 'name_role' THEN btrim(coalesce(name_role, '')) <> '' AND name_reading IS NULL AND tail IS NULL
    WHEN 'name' THEN tail IS NULL AND ((name_reading ~ '^[ぁ-ゖー・]+$' AND name_role IS NULL)
                                       OR (name_reading IS NULL AND btrim(coalesce(name_role, '')) <> ''))
    WHEN 'name_tail' THEN tail IN ('target', 'target_individual', 'target_confirm', 'target_reconfirm', 'promise_again',
                                   'target_successor', 'unavailable_confirm', 'carried_target')
                          AND name_role IS NULL AND name_reading IS NULL
    ELSE name_role IS NULL AND name_reading IS NULL AND tail IS NULL END);
