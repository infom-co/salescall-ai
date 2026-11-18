-- 0060 設定の定義を書き換える移行でも、枠と範囲の決まりが破れないようにする（2026-10-18 CL-2 の見直し。DESIGN.md 8章・15-2 の155）
--
-- 前は「枠は、収める設定より細かく決められない」を、定義を足すとき（INSERT）だけ確かめていた。決められる範囲を変える移行
-- （0045 のように、定義の変更止めを外して書き換える）で破れても、気づかなかった。
-- ⇒ 定義を足す・書き換えるたびに、表全体で確かめる：
--   (1) 枠は、同じ値の形で、収める設定より細かく決められない。
--   (2) どの値も、その設定の決められる範囲より細かい範囲に入っていない。

CREATE FUNCTION salescall.setting_rules_hold() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM salescall.setting_definitions d JOIN salescall.setting_definitions b ON b.key = d.bound_key
             WHERE b.finest_scope > d.finest_scope OR b.value_type <> d.value_type) THEN
    RAISE EXCEPTION '設定の枠が、収める設定より細かく決められるか、値の形が違います' USING ERRCODE = 'check_violation';
  END IF;
  IF EXISTS (SELECT 1 FROM salescall.setting_values v JOIN salescall.setting_definitions d USING (key)
             WHERE v.scope > d.finest_scope) THEN
    RAISE EXCEPTION '決められる範囲より細かい範囲に入った設定の値があります' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NULL;
END $$;
-- 文ごとに確かめる（取引の終わりまで延ばすと、移行が同じ取引の中で変更止めを戻せなくなる＝待っている確かめがあると ALTER TABLE できない）。
-- 書き換えが何段かに分かれるときは、途中で破れない順に書く
CREATE TRIGGER setting_definitions_rules_hold AFTER INSERT OR UPDATE ON salescall.setting_definitions
  FOR EACH STATEMENT EXECUTE FUNCTION salescall.setting_rules_hold();

-- 枠の決まりを確かめるのは上の1か所にする（定義を足すときの確かめ（0045）から、枠の決まりを外す。時間帯の枠の向きの決まりは残す）
CREATE OR REPLACE FUNCTION salescall.setting_definition_before_insert() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.bound_key IS NOT NULL AND (NEW.value_type = 'weekly_hours') <> (NEW.bound_side = 'within') THEN
    RAISE EXCEPTION '時間帯の枠は within、それ以外は upper か lower です' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
