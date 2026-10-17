-- 0041 見直しの直し（2026-10-17。書いた人：CL-2）
-- ユーザー：「ここまでの実装で、あなたの想定との食い違いや、不備、修正漏れなどは起こっていないですか？」に答えるための見直しで
-- 見つかった、修正漏れを直す。
--
-- (1) 会話AIの異常で止める範囲を、全体から「その契約者」にする。
--     2026-10-16 ユーザー決定：会話AIの枠は契約者ごと（契約者が自分のキー・プロジェクトを持つ。Intent.md 1-4）。⚠️ 前は、会話AIの
--     異常（つながらない・設定の誤り・Google の側の誤り・原因の分からない誤り）を全体で数えて、全体を止めていたので、ある契約者の
--     キーやプロジェクトの誤りで、ほかの契約者の発信まで止まった。数えるのも、直った証拠（会話に入った通話）も、解いた時刻も、
--     その契約者のものだけにする。全体の取引では数えない（全体を止める種類のエラーは無くなった）。
-- (2) 停止に数えたエラーを出す関数（画面の「止まった理由」）も、契約者の範囲の停止を出せるようにする。
-- (3) 別の耳の1分の上限の設定 gate.ear_tpm_limit を ai.ear_tpm_limit にし、契約者ごとにする。0037 で会話AIの上限の名前を
--     gate. から ai. に変えたとき（門は読まない）、別の耳の上限を変え漏らしていた。0032 で会話AIの枠を契約者ごとにしたときも
--     変え漏らしていた（別の耳も、契約者のキー・プロジェクトで動く）。
-- (4) 保存の失敗の停止の確かめの設定（stop.storage.*）を外す。2026-10-16 ユーザー：保存の失敗では止めない（管理者に直しを依頼して、
--     ほかの行を続ける）ので、使う所が無い。
-- (5) 設定の意味（i マークで出す説明の元。core/help_texts.py）を、いまの動きに合わせる：費用の上限は止めない（注意だけ）・
--     同時の本数は会話AIの枠で絞らない・品質の段と基準は検証モードだけ・検証モードを決めるのは契約者。言葉も Intent.md 1-4 に
--     そろえる（依頼主 → 依頼者、相手 → お客様、運営者 → 契約者）。

-- ═════════ (1) 続いたエラーの数え方（契約者の範囲・回線の範囲） ═════════
CREATE OR REPLACE FUNCTION salescall.consecutive_errors(p_kinds text[], p_cause text, p_evidence text, p_line uuid DEFAULT NULL)
RETURNS integer
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  WITH allowed AS (     -- 契約者の数は、その契約者の取引だけ・回線の数は、その回線の契約者の取引だけ（全体の取引では数えない）
    SELECT salescall.cur_tenant() IS NOT NULL
           AND (p_line IS NULL OR EXISTS (SELECT 1 FROM salescall.lines l
                                          WHERE l.line_id = p_line AND l.tenant_id = salescall.cur_tenant())) AS ok
  ), since AS (
    SELECT greatest(
      CASE p_evidence
        WHEN 'in_conversation' THEN (SELECT coalesce(max(h.at), '-infinity'::timestamptz)
                                     FROM salescall.attempt_state_history h JOIN salescall.attempts a ON a.attempt_id = h.attempt_id
                                     WHERE h.to_state = 'in_conversation' AND a.tenant_id = salescall.cur_tenant())
        WHEN 'verified' THEN (SELECT coalesce(max(v.recorded_at), '-infinity'::timestamptz) FROM salescall.verification_summaries v
                              WHERE v.tenant_id = salescall.cur_tenant())
        WHEN 'ringing' THEN (SELECT coalesce(max(h.at), '-infinity'::timestamptz)
                             FROM salescall.attempt_state_history h JOIN salescall.attempts a ON a.attempt_id = h.attempt_id
                             WHERE a.line_id = p_line AND h.to_state = 'ringing')
        WHEN 'none' THEN '-infinity'::timestamptz
      END,
      (SELECT coalesce(max(r.at), '-infinity'::timestamptz)
       FROM salescall.stop_events r JOIN salescall.stop_events s ON s.event_id = r.resolves_event_id
       WHERE s.cause::text = p_cause AND s.tenant_id = salescall.cur_tenant()
         AND CASE WHEN p_line IS NULL THEN s.scope = 'tenant' ELSE s.scope = 'line' AND s.line_id = p_line END)) AS t
  )
  SELECT CASE WHEN NOT (SELECT ok FROM allowed) OR p_evidence NOT IN ('in_conversation', 'verified', 'ringing', 'none')
                                     OR (p_evidence = 'ringing' AND p_line IS NULL) THEN NULL
         ELSE (SELECT count(*)::integer FROM salescall.error_events e, since
               WHERE e.kind = ANY(p_kinds) AND e.occurred_at > since.t AND e.tenant_id = salescall.cur_tenant()
                 AND (p_line IS NULL OR e.line_id = p_line)) END
$$;

-- ═════════ (2) 停止に数えたエラー（契約者の範囲の停止も） ═════════
CREATE OR REPLACE FUNCTION salescall.errors_for_stop(p_stop bigint, p_kinds text[], p_limit integer)
RETURNS TABLE (error_id bigint, source text, kind text, code integer, message text, occurred_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT e.error_id, e.source, e.kind, e.code, e.message, e.occurred_at
  FROM salescall.stop_events s JOIN salescall.error_events e
    ON e.kind = ANY(p_kinds) AND e.occurred_at <= s.at AND (s.scope <> 'line' OR e.line_id = s.line_id)
       AND (s.scope = 'system' OR e.tenant_id = s.tenant_id)
  WHERE s.event_id = p_stop AND s.action = 'stop'
    AND ((s.scope = 'system' AND salescall.cur_tenant() IS NULL)
         OR (s.scope IN ('tenant', 'line') AND s.tenant_id = salescall.cur_tenant()))
  ORDER BY e.occurred_at DESC, e.error_id DESC LIMIT p_limit
$$;

-- ═════════ (3)〜(5) 設定（定義と値は変えない決まりなので、表の持ち主が、この移行の中でだけ拒みを外して直す） ═════════
ALTER TABLE salescall.setting_values DROP CONSTRAINT setting_values_key_fkey;
ALTER TABLE salescall.setting_definitions DISABLE TRIGGER setting_definitions_frozen;
ALTER TABLE salescall.setting_values DISABLE TRIGGER setting_values_frozen;

-- (3) 別の耳の1分の上限
UPDATE salescall.setting_definitions SET key = 'ai.ear_tpm_limit', merge_kind = 'override',
  meaning = '別の耳（通話後の検証）の1つのプロジェクトの、1分あたりの入力トークン数の上限（契約者ごと。これに近づいたら、キーの選択が次のキーを併せて使う）',
  design_ref = 'DESIGN.md 5-2 の9・6-12（キーの選択 Q15 が使う。門は使わない）'
WHERE key = 'gate.ear_tpm_limit';
UPDATE salescall.setting_values SET key = 'ai.ear_tpm_limit' WHERE key = 'gate.ear_tpm_limit';

-- (4) 保存の失敗の停止の確かめ
DELETE FROM salescall.setting_values WHERE key LIKE 'stop.storage.%';
DELETE FROM salescall.setting_definitions WHERE key LIKE 'stop.storage.%';

-- (5) 意味をいまの動きに合わせる
UPDATE salescall.setting_definitions SET meaning = v.meaning FROM (VALUES
  ('approval.human',
   '検証モードを使うか：発信の前に契約者が行を承認し、結果を送る前に契約者が確かめる。品質の段・割合の基準・1件で止める品質の門も、検証モードのときだけ。契約者が業務ごとに決める'),
  ('budget.client_daily_yen',
   'その依頼者の1日の費用の上限（円。超えたら、契約者に注意を知らせる。発信は止めない＝2026-10-16 ユーザー「注意喚起のみ」）'),
  ('budget.system_daily_yen',
   '全体の1日の費用の上限（円。超えたら、管理者に注意を知らせる。発信は止めない）'),
  ('budget.max_cost_ratio',
   '報酬に対する費用の割合の上限（今月。超えたら知らせるだけ。赤字は、警告して、期限 budget.deficit_grace までに確認されなければ止める）'),
  ('calling.max_concurrent_calls',
   'その依頼者で同時に流す通話の本数の上限（依頼者ごとの設定。会話AIの1分の上限では絞らない＝キーを併せて使う。実際の本数は回線の数で決まる：1回線＝同時1本はデータベースの約束）'),
  ('stop.ai.stop_after_failures',
   '会話AIの異常（つながらない・Google の側の誤り・原因の分からない誤り）が何回続いたら、その契約者の新しい発信を止めるか（直ったら自動で解く）'),
  ('stop.ai.probe_interval',
   '会話AIの異常で止めたあと、その契約者のキーで試しのセッションを開く間隔'),
  ('quality.stage.sizes',
   '段階的な立ち上げの各段の件数（会話に至った通話の数。最後の段のあとは全部）。検証モードの業務だけ（本番は段を使わない）'),
  ('quality.max_unknown_judgments',
   '「判定できない」（unknown）を、何件まで許すか（超えたら業務を止める）。検証モードの業務だけ（本番は quality.production_same_cause_per_day）'),
  ('quality.max_forbidden_speech',
   '言ってはいけないことの検出（2つの耳で確かめたもの）の件数の上限。検証モードの業務だけ（本番は quality.production_same_cause_per_day）'),
  ('quality.max_ear_disagreement', '2つの耳が食い違った項目の割合の上限。検証モードの業務だけ（本番は割合を見ない）'),
  ('quality.max_complaint', '苦情があった通話の割合の上限。検証モードの業務だけ（本番は割合を見ない）'),
  ('quality.max_technical_error', '技術的な失敗の割合の上限。検証モードの業務だけ（本番は割合を見ない）'),
  ('quality.max_verification_failed', '通話後の検証が確かめられなかった割合の上限。検証モードの業務だけ（本番は割合を見ない）')
) AS v(key, meaning) WHERE salescall.setting_definitions.key = v.key;

-- 言葉を Intent.md 1-4 にそろえる（意味だけ。根拠 rationale は、そのときのユーザーの言葉の記録なので変えない）
UPDATE salescall.setting_definitions SET meaning = replace(replace(replace(meaning, '依頼主', '依頼者'), '相手', 'お客様'), '運営者', '契約者')
WHERE meaning ~ '依頼主|相手|運営者';

-- 根拠のうち、いまの動きと食い違う説明（門が会話AIの枠で数える・費用の上限で止める）を直す
UPDATE salescall.setting_values SET rationale = v.rationale FROM (VALUES
  ('calling.max_concurrent_calls',
   '依頼者ごとの上限の初めの値（会話AIの1分の上限では絞らない＝2026-10-17 ユーザー「次のキーと併用して進んだらいい」）。⚠️ 実際の同時の本数は回線の数で決まる：2026-10-13 ユーザー「1PC 1本。Rakuten Link は1つずつしか流せない。増やすときはスマホを足す」⇒ いまは回線1つ＝同時1本'),
  ('gate.denied_retry_wait',
   '待てば変わる理由（回線・進行中・同時の本数）は数分で変わることが多い。5分あける'),
  ('budget.client_daily_yen',
   'いまは会話AIが無料枠・Rakuten Link も無料で、費用は 0円。有料に切り替えたときに気づくための目安として、仮に1日1,000円（止めない。注意だけ）')
) AS v(key, rationale) WHERE salescall.setting_values.key = v.key AND salescall.setting_values.scope = 'system';

ALTER TABLE salescall.setting_values ENABLE TRIGGER setting_values_frozen;
ALTER TABLE salescall.setting_definitions ENABLE TRIGGER setting_definitions_frozen;
ALTER TABLE salescall.setting_values ADD CONSTRAINT setting_values_key_fkey
  FOREIGN KEY (key) REFERENCES salescall.setting_definitions (key);
