-- 0231 答えを通話ごとに持ち、ある担当の方に使う答えを1か所で決める（2026-11-16。書いた人：CL-2。DESIGN.md 3-5-9・15-2 の838。
--      ✅ 2026-11-16 ユーザー「担当の方ごとに持つ（お勧め）」）
--
-- ⛔ 0001・0108 の形：答え（item_values）は行と質問ごとに1つで、あとの通話が上書きし、上書きされた前の通話の答えを別の表（item_value_history）に
--    残した。目的のお客様だと確かめられなかった通話の答えは、仕掛け（target_recheck_blanks）が前の答えに戻すか「-」にした。そのため：
--    (a) 「担当の方は違う」で直したあと新しい担当の方が同じ質問に答えると、前の担当の方の答えが上書きされ、前の担当の方に戻しても戻らなかった
--        （計画書 7-1 (30)「戻すと、前の担当の方のそのときの答えも戻る」を守れない）。
--    (b) 判定の材料（settle._items）は、行のいちばん新しい答えをそのまま読み、新しい担当の方が「引き継いでいない」と答えた前の方の答えも、
--        前の担当の方への通話の判定に新しい方の答えも入った（前の通話の答え conversation_io.earlier とは別の決まり＝2か所）。
--    (c) いまの答え・控えの答え・戻す仕掛けの3か所に、同じ「どの答えを使うか」が分かれていた。
-- ⇒ (1) 答えは通話ごと：鍵を（行・質問・通話）にする。同じ通話の中だけ書き換える（その行のいちばん新しい通話の番号で書く＝0108 の守りのまま）。
--        どの行・質問・通話の答えかは変えない。控えの表と戻す仕掛けは無くす（控えの答えは、その通話の答えとして移す）。
--    (2) ある担当の方（担当の期）に使う答えは、Python の1か所（core/conversation_io.answers・earlier）が、通話を古い順にたどって決める：
--        新しい期が始まったとき、すぐ前の期で使っていた答えを引き継ぎ、その期の通話ごとに、引き継ぎを確かめた答え（通話の事実 carried）で外した
--        答えを外し、その通話の答えを入れる。前の期に戻したら、その期の答え（その期がはじめに引き継いだものを含む）だけを使う。目的のお客様だと
--        確かめられなかった通話（target_rechecks の target 以外）は、何も入れず何も外さない。データベースの中では「どの答えを使うか」を決めない。

ALTER TABLE salescall.item_values DISABLE TRIGGER item_value_guard;
ALTER TABLE salescall.item_values DROP CONSTRAINT item_values_pkey;
ALTER TABLE salescall.item_values ADD PRIMARY KEY (task_id, question_id, attempt_id);
INSERT INTO salescall.item_values (tenant_id, client_id, task_id, version_id, question_id, state, value, condition, reason, hedged,
                                   attempt_id, updated_at)
SELECT h.tenant_id, h.client_id, h.task_id, h.version_id, h.question_id, h.state, h.value, h.condition, h.reason, h.hedged,
       h.attempt_id, h.replaced_at
FROM salescall.item_value_history h
ON CONFLICT DO NOTHING;
ALTER TABLE salescall.item_values ENABLE TRIGGER item_value_guard;

DROP TRIGGER target_recheck_blanks ON salescall.target_rechecks;
DROP FUNCTION salescall.target_recheck_blanks();
DROP TABLE salescall.item_value_history;

-- 答えの守り（1か所）：どの行・質問・通話の答えかは変えない。書く・書き換えるのは、その行のいちばん新しい通話の番号で（前の通話の答えは、
-- その通話の答えのまま残る）
CREATE OR REPLACE FUNCTION salescall.item_value_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = salescall, pg_temp AS $$
BEGIN
  IF TG_OP = 'UPDATE' AND (NEW.tenant_id, NEW.client_id, NEW.task_id, NEW.question_id, NEW.attempt_id)
                          IS DISTINCT FROM (OLD.tenant_id, OLD.client_id, OLD.task_id, OLD.question_id, OLD.attempt_id) THEN
    RAISE EXCEPTION '答えの、お客様の行・質問・通話は変えられません' USING ERRCODE = 'check_violation';
  END IF;
  IF TG_OP = 'UPDATE' AND (NEW.version_id, NEW.state, NEW.value, NEW.condition, NEW.reason, NEW.hedged)
                          IS NOT DISTINCT FROM (OLD.version_id, OLD.state, OLD.value, OLD.condition, OLD.reason, OLD.hedged) THEN
    RETURN NEW;                     -- 中身が変わらない
  END IF;
  IF NEW.attempt_id IS DISTINCT FROM salescall.newest_attempt(NEW.task_id) THEN
    RAISE EXCEPTION '答えは、そのタスクのいちばん新しい通話の番号で書きます（前の通話の答えは書き換えない）'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'item_values_from_newest_call';
  END IF;
  RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION salescall.item_value_guard() FROM PUBLIC, salescall_app;
