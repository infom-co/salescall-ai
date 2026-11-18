-- 0214 「守る」を選んだ前の担当の方との約束は約束の決まりいっぱいまで・戻したらやめた約束も戻す（2026-11-08。書いた人：CL-2。
--      DESIGN.md 3-4-1 の【0214】・2章 Q190〜・15-2 の720〜）
--
-- ✅ 2026-11-08 ユーザー＝計画書 7-1 (32)（伺い「守る約束」）：「前の担当の方との約束も守る」を選んだとき、前の担当の方へどこまでかけるか：
--    「約束の幅いっぱい → 鈴木様」（約束の日・幅のあいだは、約束の決まりどおり前の担当の方へかける。それでも話せなければ、行を終えずに新しい
--    担当の方へ引き継ぎを確かめる電話。番号の直しも名前の直しも同じ）。
-- ✅ 同 (31)（伺い「やめた約束」）：「次の担当の方にだけかける」を選んで前の担当の方との約束をやめたあと、前の担当の方に戻したとき：「約束を戻す」。
--    （予定の決め直しは core/next_action の1か所。この移行が変えるのは、選び待ちの元の通話だけ）
--
-- (1) 前の担当の方が新しいかけ直しの約束をした通話（task_old_promise_call。選び待ち task_old_promise_waiting・いまの約束に対する選び
--     task_promise_choice が読む）は、その通話の判定が新しいかけ直しの約束（callback_scheduled）のものだけ。
--     ⛔ 0203〜0213：お客様が出た通話（reached_answer）で見ていた。「守る」約束は前の番号へ1回で新しい担当の方へ移っていたので、前の担当の方の
--     期の約束が残るのは「その方が新しい約束をした」ときだけだったが、0214 から「守る」約束は約束の決まりいっぱいまで前の担当の方へかけるので、
--     受付の方が出て「不在です」と言った約束の電話のあとも約束が前の担当の方の期に残る。お客様が出たかで見ると、その電話を「新しい約束をした通話」と
--     みなし、契約者への知らせも無く選び待ちで止まり、いままでの「守る」の選びも数えなかった（前の担当の方の携帯・名前などの直しの会社の番号へ
--     「守る」で残した約束では、0213 でも同じことが起きえた）。
--     「締めた通話か」（settled_at が NULL でない）は消した：判定の版（call_judgements）がある通話は締めた通話で、判定が保留（取り消せない決定の
--     待ち）のときは、いまの予定が判定待ちになり約束の予定でないので、どの場面でも効かない（壊し試しで、外しても試験が落ちないことを確かめた）。

CREATE OR REPLACE FUNCTION salescall.task_old_promise_call(p_task uuid) RETURNS uuid LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT c.attempt_id
  FROM salescall.current_next_actions n
  JOIN LATERAL (SELECT a.attempt_id FROM salescall.attempts a
                WHERE a.task_id = p_task AND salescall.former_person_call(a.attempt_id)
                ORDER BY a.claimed_at DESC, a.attempt_id DESC LIMIT 1) c ON true
  WHERE n.task_id = p_task AND salescall.promise_call(n.purpose, n.promised)
    AND n.person_since IS DISTINCT FROM salescall.task_person_since(p_task, now())   -- いまの予定が、前の担当の方の期の予定
    AND EXISTS (SELECT 1 FROM salescall.call_judgements j                             -- その通話で、新しいかけ直しの約束をした（締めた）
                WHERE j.attempt_id = c.attempt_id AND j.business_state = 'callback_scheduled')
$$;
