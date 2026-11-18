-- 0238 落ちた試行の後始末は、こちらの不具合として終える（2026-11-18 の6回目の見直し。書いた人：CL-2。DESIGN.md 5-4・9-1・15-2 の849。
--      原則6：2026-10-25 ユーザー「こちらのプログラムの不具合で終えた電話は、番号にも回線にも行の回数にも数えない」）
--
-- ⛔ 0063〜0237：回線ごとの処理・見守り役が通話を終えないまま止まったとき、後始末（recover_lines）は試行を「結果不明」にするだけで、終えた理由
--    （trouble）を残さなかった。お客様が出る前なら数え方の1か所（dial_kind）が「回線の失敗（鳴らなかった）」と読み、状態の表は E2（鳴らなかった）
--    にして、回線は確かめ中・番号は「まだ分からない」を増やした。次の電話が鳴れば「番号のせい」と決まり、番号の4回に数えた＝こちらが4回落ちれば、
--    正しい番号が「使えない」になりえた。
-- ⇒ 後始末は、進行中の試行を、呼び手が渡す理由で終える（1か所）：
--    - worker_lost（回線ごとの処理・見守り役そのものが止まった。起動したとき・流れが止まったとき・借用の期限が切れたとき＝core/leases.recover）
--    - storage_stuck（回線ごとの処理の回の頭で見つけた、終わりの記録が書けずに残った試行＝core/leases.recover_left。データベースへの書き込みが
--      進まなかった＝0143 の理由と同じ。違う行の数に数えないのも同じ＝伺い5）
--    どちらも「こちらの不具合」（salescall.trouble_ours）なので、番号・回線・行の回数のどれにも数えず、状態の表は E3（同じ行で2回続けば休み・
--    worker_lost は違う3行で契約者の新しい電話を止める＝原則6。止める数えの例外は、管理者が決めたデータベースの一時的な止まりだけ）。

ALTER TABLE salescall.attempts DROP CONSTRAINT attempts_trouble_check;
ALTER TABLE salescall.attempts ADD CONSTRAINT attempts_trouble_check
  CHECK (trouble IS NULL OR trouble IN ('frozen', 'ai_malfunction', 'poor_audio', 'cannot_proceed', 'runner_error', 'storage_stuck',
                                        'worker_lost'));

DROP FUNCTION salescall.recover_lines(text, uuid, text);
-- p_holder を渡したら「その見守り役が借りている回線」（起動したとき＝前に落ちた自分の分。p_line を渡せば、その回線だけ＝回線の流れが
-- 止まったとき）、NULL なら「借用の期限が切れた回線」（決まった間隔で＝前に動いていた見守り役の分。p_except＝呼んだ見守り役の名前。
-- 自分が借りている回線は除く）。p_trouble・p_why＝進行中の試行を終える理由と根拠の文（こちらの不具合の理由だけ）
CREATE FUNCTION salescall.recover_lines(p_holder text, p_line uuid, p_except text, p_trouble text, p_why text)
RETURNS TABLE (line_id uuid, released integer, ended integer)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
DECLARE l record; r integer; e integer;
BEGIN
  IF salescall.cur_tenant() IS NULL THEN
    RAISE EXCEPTION '契約者の取引で呼びます' USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF NOT salescall.trouble_ours(p_trouble) THEN
    RAISE EXCEPTION '後始末で終える理由は、こちらの不具合の理由です：%', p_trouble USING ERRCODE = 'check_violation';
  END IF;
  FOR l IN SELECT x.line_id FROM salescall.lines x
           WHERE x.tenant_id = salescall.cur_tenant() AND x.lease_holder IS NOT NULL
             AND (CASE WHEN p_holder IS NULL
                       THEN x.lease_expires_at < now() AND x.lease_holder IS DISTINCT FROM p_except
                       ELSE x.lease_holder = p_holder END)
             AND (p_line IS NULL OR x.line_id = p_line)
           FOR UPDATE LOOP
    UPDATE salescall.attempts SET state = 'released' WHERE attempts.line_id = l.line_id AND state = 'claimed';
    GET DIAGNOSTICS r = ROW_COUNT;
    UPDATE salescall.attempts SET state = 'technical_error', trouble = p_trouble, trouble_why = p_why
    WHERE attempts.line_id = l.line_id AND salescall.is_active(state);
    GET DIAGNOSTICS e = ROW_COUNT;
    IF p_line IS NULL THEN   -- 回線の流れだけが止まったとき（p_line）は、借用を続けたまま立ち上げ直す
      UPDATE salescall.lines SET lease_holder = NULL, lease_expires_at = NULL WHERE lines.line_id = l.line_id;
    END IF;
    line_id := l.line_id; released := r; ended := e;
    RETURN NEXT;
  END LOOP;
END $$;
REVOKE ALL ON FUNCTION salescall.recover_lines(text, uuid, text, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.recover_lines(text, uuid, text, text, text) TO salescall_app;

-- ═════════ 発信禁止の台帳を、出どころの通話・取り込みで引く索引（2026-11-18 の6回目の見直し） ═════════
-- 番号を足すたびに、その行の通話から入った断り（dnc.stop_added）・同じ出どころで取り消した番号（dnc._put）を、出どころの通話・取り込みで引く。
-- 索引が無いと、番号を1つ足すたび（取り込みでは行ごと）に台帳全体を読んだ
CREATE INDEX dnc_entries_source_attempt ON salescall.dnc_entries (source_attempt_id) WHERE source_attempt_id IS NOT NULL;
CREATE INDEX dnc_entries_import ON salescall.dnc_entries (import_id) WHERE import_id IS NOT NULL;
