-- 0137 名前の録音の台帳の切れ目を決め打ちしない・前の版のお詫びの案の残りを知らせる（2026-10-24。書いた人：CL-2）
--
-- (1) 名前の録音の台帳の文の切れ目を、データベースで決め打ちしない（見直し19b の2・19c の1）。
--     2026-10-24 ユーザー決定（原文）：「『営業部部長の〇〇様は』も『営業部部長』『〇〇様』『は』に分けて録音しておいた方が汎用性が
--     あるんじゃないですか？」。CL-1 の連絡帳 4-49・4-50：つなぎ目の聞き比べ（G-64）の切り方に誤りがあり、測り直し（G-65）の結果で
--     切れ目（「の」を役職と名前のどちらに付けるか・「様」と助詞を分けられるか）を決めるまで、「続く言葉は助詞から」と「役職の部分の
--     終わりの『の』」は決め打ちしないでほしい。
--     ⛔ 0134 は CL-2 の未承認の案（助詞を続く言葉の頭に付ける）を守りにして、役職の部分＝「部署と役職＋の」・続く言葉＝その助詞
--     （salescall.name_tail_particle）から始まる・助詞だけは断る、とした。G-65 の結果が違えば、正しい録音をデータベースが断った。
--     ⇒ 文と鍵の突き合わせは「その鍵の言葉が入っているか」だけにする（役職の部分は部署と役職を含む・名前の部分は「「読み」」を含む・
--     役職のみの呼び方は部署と役職を含む・続く言葉は空でない）。部分の名前と、鍵の守り（recording_sets_name_key）は今のまま。
--     助詞を単独の部分にするかは G-65 とユーザーの判断で決め、そのときは続く言葉の名前（recording_sets_name_key の tail の一覧と
--     core/recordings.NAME_TAILS）を足すだけで、組と鍵の作りは作り直さない。助詞の対応の関数 name_tail_particle は外す（切れ目を
--     決め打ちする1か所だったため。負の遺産を残さない）。
-- (2) 前の版のお詫びの案（0131 より前に作った、伝える文の無い案）と録音の無い承認が残っていれば、分かる言葉で止める（見直し19a の8・
--     19b の8 (b)）。お詫びの案は変更不可の表なので、あとから文を埋められない。この表の作りは main に入る前で、本番のデータベースは
--     まだ無い（CL-2 の連絡帳 4-108 と同じ）ので、移し替えは作らない。残っていなければ、0131 の2つの守り（apology_orders_texts・
--     apology_approvals_recorded）を確かめ済みにする＝どの案にも文が2つ・どの承認にも録音の組がある（apology.for_task が文を作り直す・
--     録音を空で渡すことは無い。その代わりの扱いは外した）。
-- (3) 0132 の形の名前の録音（name_form のあった組）が残ったデータベースは、0134 が止める（recording_sets_name_key の確かめ）。0134 は
--     流したあとに書き換えられない（core/migrate）ので、ここでは直せない。本番のデータベースはまだ無いので、試験のデータベース
--     （毎回 0001 から作る）だけの話（見直し19b の3。記録だけ）。

-- ═════════ (1) ═════════
CREATE OR REPLACE FUNCTION salescall.name_recording_check() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE s record;
BEGIN
  SELECT unit, name_role, name_reading, tail INTO s FROM salescall.recording_sets WHERE set_id = NEW.set_id;
  IF s.unit NOT IN ('name_role', 'name', 'name_tail') OR s.unit IS NULL THEN
    RETURN NEW;
  END IF;
  IF NEW.part IS DISTINCT FROM (CASE s.unit WHEN 'name_role' THEN 'role' WHEN 'name' THEN 'name' ELSE s.tail END) THEN
    RAISE EXCEPTION '名前の録音の台帳の組の部分は1つで、名前は役職の部分 role・名前の部分 name・続く言葉はその名前です'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'name_recording_part';
  END IF;
  -- 文と鍵の突き合わせは、鍵の言葉が文に入っているかだけ（切れ目＝助詞・「の」の置き場は決め打ちしない。G-65 とユーザーの判断待ち）
  IF btrim(NEW.spoken_text) = '' OR NOT (CASE s.unit
            WHEN 'name_role' THEN strpos(NEW.spoken_text, s.name_role) > 0
            WHEN 'name' THEN (CASE WHEN s.name_reading IS NOT NULL THEN strpos(NEW.spoken_text, '「' || s.name_reading || '」') > 0
                                   ELSE strpos(NEW.spoken_text, s.name_role) > 0 END)
            ELSE true END) THEN
    RAISE EXCEPTION '台帳の文「%」は、鍵と合いません（役職の部分＝その部署と役職・名前の部分＝「「読み」」か役職を含む・続く言葉＝空でない）',
                    NEW.spoken_text
      USING ERRCODE = 'check_violation', CONSTRAINT = 'name_recording_text';
  END IF;
  RETURN NEW;
END $$;
DROP FUNCTION salescall.name_tail_particle(text);

-- ═════════ (2) ═════════
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM salescall.apology_orders WHERE text_same_day IS NULL OR text_later IS NULL)
     OR EXISTS (SELECT 1 FROM salescall.apology_approvals WHERE set_id IS NULL) THEN
    RAISE EXCEPTION '0131 より前に作った、伝える文の無いお詫びの案か、録音の無いお詫びの承認が残っています（その案の文で録音を作れず、'
                    '流す録音もありません）。その案の通話を確かめ、案を作り直す手順を CL-2 に頼んでください'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'apology_orders_texts';
  END IF;
END $$;
ALTER TABLE salescall.apology_orders VALIDATE CONSTRAINT apology_orders_texts;
ALTER TABLE salescall.apology_approvals VALIDATE CONSTRAINT apology_approvals_recorded;
