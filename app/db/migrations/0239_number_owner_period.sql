-- 0239 行の番号の持ち主の期（1か所）（2026-11-18 の7回目の見直し。書いた人：CL-2。DESIGN.md 3-6・15-2 の856）
--
-- ⚠️ 0238 の頭の説明文の「worker_lost は違う3行で契約者の新しい電話を止める」は、そのあとの ✅ 2026-11-18 ユーザー「案B」で変わった（違う行の数に
--    数えない＝core/errors の call_worker_lost の apart。流した表の作りは指紋で守るので、0238 の文は書き換えない）。
--
-- ⛔ 0238 まで：足した番号を断りで止めるか（core/dnc.stop_added）は、番号の持ち主の期を自前で決め（持ち主の期が書いていなければ「いまの担当の方」）、
--    データベースの持ち主の決まり（言われた通話の期＝task_former_number・task_contact_period）と2か所になっていた。前の担当の方への通話で言われた番号は、
--    その方が前に断っていても止まらなかった。
-- ⇒ 行の番号の持ち主の期を決める1か所：書いてある持ち主の期（owner_period）・言われた通話の担当の期（source_attempt_id）・足した時点の担当の期、の順。
--    連絡先の持ち主の期（task_contact_period）と同じ順。
CREATE FUNCTION salescall.task_number_period(p_task uuid, p_contact uuid) RETURNS timestamptz LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT CASE WHEN n.owner_period IS NOT NULL THEN salescall.back_period(n.owner_period)
              WHEN n.source_attempt_id IS NOT NULL THEN (SELECT a.person_since FROM salescall.attempts a WHERE a.attempt_id = n.source_attempt_id)
              ELSE salescall.task_person_since(n.task_id, n.added_at) END
  FROM salescall.task_numbers n WHERE n.task_id = p_task AND n.contact_id = p_contact
$$;
GRANT EXECUTE ON FUNCTION salescall.task_number_period(uuid, uuid) TO salescall_app;

-- ═════════ 目的のお客様と話せたかの守り：システムが締め直す取引の契約者の版も（2026-11-18 の7回目の見直し。DESIGN.md 15-2 の857） ═════════
-- 直しのある通話を締め直した版は、直しを読んだ契約者の版（kind human。判定の正しさの正解を付ける相手を直しの中身にしない＝6回目の見直し）。
-- ⛔ 0233 の守りは、システムの締めの版（judged・rejudged）を書く取引だけを通したので、録音から作り直した報告などで「目的のお客様と話せたか」が変わる
--    締め直しが、毎回この守りで落ちた。⇒ 契約者の版でも、システムの処理の取引（締め・締め直し）なら通す。人（管理者・契約者）の取引は、今までどおり
--    通さない（0233 の (3)「契約者の版に通話が入っても、守りを緩めない」の意図＝人の直しの取引で書かせない、は変わらない）。
CREATE OR REPLACE FUNCTION salescall.attempt_reached_target_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.reached_target IS DISTINCT FROM OLD.reached_target
     AND NOT EXISTS (SELECT 1 FROM salescall.task_results r WHERE r.attempt_id = NEW.attempt_id AND r.decided_at = now()
                     AND (r.kind IN ('judged', 'rejudged') OR (r.kind = 'human' AND salescall.cur_actor_kind() = 'system')))
     AND NOT EXISTS (SELECT 1 FROM salescall.apology_outcomes o WHERE o.attempt_id = NEW.attempt_id AND o.recorded_at = now()) THEN
    RAISE EXCEPTION '目的のお客様と話せたか（reached_target）を書けるのは、その通話の締めの記録（結果の版・お詫びの電話の結び）を書く取引の中だけです'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'attempt_reached_target_by_settle';
  END IF;
  RETURN NEW;
END $$;

-- ═════════ 1か月以上先の伺いの答えは、その回の答え（2026-11-18 の7回目の見直し。DESIGN.md 15-2 の858） ═════════
-- 伺っている予定が、門の待ちなどで（まだかけないまま）同じ回の中の別の時刻に書き直されても、伺いの印は引き継ぐ（core/next_action._month_ahead_ask）。
-- 契約者の答え（task_month_ahead_answers）は、答えたときの予定の時刻で残るので、その時刻か、同じ回の中の時刻の答えを、その予定への答えとして読む。
-- ⛔ 0219〜0238：答えは予定の時刻ちょうどとだけ比べ、印は時刻が変わると消えた（契約者の答えも約束の時刻の5分前の緊急の知らせも効かないまま、あとの
--    時刻にかけた）。
CREATE OR REPLACE FUNCTION salescall.task_month_ahead_asking(p_task uuid) RETURNS timestamptz
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT n.call_at FROM salescall.current_next_actions n
  WHERE n.task_id = p_task AND n.kind = 'call' AND n.month_ahead_ask
    AND NOT EXISTS (SELECT 1 FROM salescall.task_month_ahead_answers k WHERE k.task_id = p_task
                      AND (k.call_at = n.call_at
                           OR (n.window_from IS NOT NULL AND k.call_at >= n.window_from AND (n.window_until IS NULL OR k.call_at < n.window_until))))
$$;
