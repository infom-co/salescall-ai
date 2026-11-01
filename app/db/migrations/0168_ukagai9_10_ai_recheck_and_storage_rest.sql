-- 0168 2026-11-01 伺い9・伺い10 のご決定。書いた人：CL-2。設計書 15-2 の422〜。
--
-- (1) 設定 decision.undecided_as_decided の説明（画面の i マーク）を、伺い9 の案1 に合わせる（2026-11-01 管理者：「録音を確かめないまま済んだ
--     通話でも、設定が『決まったとみなす』というのはどういうこと？…確かめずに、発信禁止に入れる判断を私がするとは思えません」・「人は
--     甚大な問題以外で介入しない仕組みづくりをするように何度も言っているだろ！」＝案1「OK」）。この設定が効くのは、AI が確かめ直しても
--     決められなかったときだけ。録音が届かなかった・会話の担当が録音で確かめないまま済んだ通話は、契約者に求めず、設定で決まったとみなし
--     もせず、会話の担当の確かめ直しを待つ（core/approval.AI_RECHECK）。⛔ 0166 の文は「3つの理由のすべてに同じく効く」と書いていた
--     （CL-2 が自分の文をご決定と読み違えた）。
-- (2) 設定 stop.trouble.stop_after_rows の説明に、データベースの止まりで休んだ行の戻り方（伺い10。人の操作は要らない）を書き足す。

ALTER TABLE salescall.setting_definitions DISABLE TRIGGER setting_definitions_frozen;
UPDATE salescall.setting_definitions SET meaning =
  'お客様の断り・番号違い・怒っていた（取り消せない決定）を、通話後の AI の確かめ直し（録音から。録音が届かなければ通話中の別の耳の'
  '文字から）でも決められなかったときの決め方です。いいえ（初め）＝契約者に委ねる（契約者が録音とやり取りを確かめて、承認か差し戻しを'
  '押すまで、そのお客様にはかけません）／はい＝決まったとみなす（断り・番号違い・怒っていたとして扱い、そのお客様にはかけません。'
  '契約者と管理者に知らせます）。AI がまだ確かめ直していない通話（録音が届かなかった・会話の担当が録音で確かめないまま済んだ）には'
  '効きません：契約者には求めず、そのお客様にはかけないまま、会話の担当の確かめ直しを待ちます（会話の担当へ頼みます）。言ってはいけない'
  'ことを言った通話で、切れ方が分からない（お客様が何も言わずに切った）ときの「断ったか」も同じです。検証モードの業務は、いつも'
  '契約者の承認です'
WHERE key = 'decision.undecided_as_decided';
UPDATE salescall.setting_definitions SET meaning =
  'こちらのプログラムの不具合（固まった・AI の異常・決まりどおりの会話を進められない・通話を進める部品が落ちた など）で終えた電話が、'
  '同じ理由で、違うこの数の行で続いたら（同じ行の2回目は数えません）、すべての行にかかわる甚大な問題として、その契約者の新しい電話を'
  '止めます（話している電話は切りません。契約者と管理者に知らせ、直す担当に修正依頼を出します。その理由を直したプログラムの版に替わったら'
  '解きます。管理者・契約者も解けます）。数えないもの：声が聞き取れずに終えた電話（途中で切れた電話と同じ扱い＝2026-10-25 夜 案A）・'
  'データベースの一時的な止まりで終えた電話（2026-10-31 伺い5「数えないでOK」）。その不具合なしにお客様と話し終えた電話があれば、'
  '数え直します（続けてこの数の行のときだけ止めます）。その行だけの問題（同じ行で2回続いた）は、その行を休ませて次の行へ進みます'
  '（2026-10-25 ユーザー：「3行同じ問題なら、甚大な問題と判断。」）。休んだ行は、その理由を直したプログラムの版に替わったら戻ります。'
  'データベースの止まりだけで休んだ行は、決まった間隔の仕事が、その行の書き込みが時間内に通ると確かめたら、1回だけ戻して、お詫びと'
  'ともにかけ直します（戻したあとにまた休めば、その行に関わる不具合として、直した版を待ちます。2026-11-01 伺い10）'
WHERE key = 'stop.trouble.stop_after_rows';
ALTER TABLE salescall.setting_definitions ENABLE TRIGGER setting_definitions_frozen;

-- (3) 契約者に確かめてもらう印（decision_asks）の理由は、AI が確かめ直しても決められなかった（recording_undecided）だけ（伺い9。書くのは
--     core/approval._ask_contractor の1か所で、そこも断る。データベースでも守る）。前に付いた印は、そのまま残す（NOT VALID＝新しい印だけ
--     確かめる。答えを求めた事実は消さない）。
ALTER TABLE salescall.decision_asks ADD CONSTRAINT decision_asks_ai_rechecked CHECK (why = 'recording_undecided') NOT VALID;

-- (4) データベースの止まりだけで休んだ行を、行ごとに書き込みを確かめて戻す（伺い10）ための、その契約者の休みの行の欄の読み（鍵を取らない＝
--     固まった行の鍵を待って、ほかの行の確かめまで止めない）。決まった間隔の仕事は契約者の範囲の取引（依頼者をまたぐ）なので、依頼者の
--     範囲の読み取りの決まり（client_scope）では見えない＝鍵を取って読む health_take と同じく、データベースの関数で読む。どの行を戻して
--     よいかは、ここでは決めない（状態の表の1か所＝core/health_rules.storage_return）。
CREATE FUNCTION salescall.resting_task_states() RETURNS TABLE (task_id uuid, state jsonb)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT s.task_id, s.state FROM salescall.task_states s
  WHERE s.tenant_id = salescall.cur_tenant() AND s.stage = 'resting' ORDER BY s.task_id
$$;
REVOKE ALL ON FUNCTION salescall.resting_task_states() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.resting_task_states() TO salescall_app;

-- (5) 伺い8（2026-11-01 管理者：「業務ごとに選択できるようにする。デフォルトは案1。ただし、再度、同じよう圏でかけ直したい業務の場合は、
--     非推奨で案2も選べるように。よっぽど変わった業務以外は案1となります。」）：用件・最終目的を断られたお客様（行の終わりが「断られた」）
--     に、同じ業務の次の実施回でかけるか。設定 business.call_declined_again（業務ごと。初め：いいえ＝かけない＝案1／はい＝かける＝案2・非推奨）。
--     そのお客様か（同じ業務の前の実施回の、断られて終わった行の番号のどれかと同じ番号・依頼者が管理番号をお客様を表す変わらない番号と
--     した業務では同じ管理番号）は、データベースの1か所 salescall.declined_in_business。かけないかを決めるのは、取込と門の両方が読む
--     かけない理由の1か所（core/target_policy.block_reason＝declined_in_business）。断りが取り消されて行が予定に戻れば（行のいちばん新しい
--     予定が「断られた」で終わっていなければ）、かける。発信禁止（もうかけないで）とは別のもの（依頼者のほかの業務・ほかの依頼者からは、
--     かかる）。
CREATE FUNCTION salescall.declined_in_business(p_business uuid, p_e164 text, p_client_ref text) RETURNS boolean
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT EXISTS (
    SELECT 1 FROM salescall.tasks t
    JOIN salescall.runs r ON r.client_id = t.client_id AND r.run_id = t.run_id
    WHERE r.business_id = p_business
      AND (SELECT n.kind = 'end' AND n.end_reason = 'declined' FROM salescall.task_next_actions n
           WHERE n.task_id = t.task_id ORDER BY n.seq DESC LIMIT 1)
      AND (EXISTS (SELECT 1 FROM salescall.task_numbers k WHERE k.task_id = t.task_id AND k.phone_e164 = p_e164)
           OR (p_client_ref IS NOT NULL AND t.client_ref = p_client_ref)))
$$;
GRANT EXECUTE ON FUNCTION salescall.declined_in_business(uuid, text, text) TO salescall_app;
INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, finest_scope) VALUES
('business.call_declined_again', 'override', 'boolean',
 '用件や最終目的を断られたお客様（「いりません」「興味はありません」・担当者からの折り返しなどの最終目的を断られた）に、同じ業務を後日'
 'もう一度実施する（新しい実施回として一覧を取り込み直す）とき、またかけるかです。いいえ（初め・おすすめ）＝かけない（そのお客様は、'
 'その業務ではもう電話を受けません。依頼者のほかの業務や、ほかの依頼者からはかかります）／はい（おすすめしません）＝次の実施回では'
 'ふつうにかける（時期を置いて同じ用件で改めてご案内したい、よほど変わった業務だけ）。同じお客様かは、断られた行の番号のどれか'
 '（依頼者が管理番号をお客様を表す変わらない番号とした業務では、管理番号も）で決めます。もうかけないでと言われたお客様（発信禁止）は'
 '設定に関係なくかけません',
 'DESIGN.md 5-2-1', 'business');
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('business.call_declined_again', 'system', 'false', 'decided',
 '2026-11-01 管理者：伺い8「業務ごとに選択できるようにする。デフォルトは案1。ただし、再度、同じよう圏でかけ直したい業務の場合は、非推奨で'
 '案2も選べるように。よっぽど変わった業務以外は案1となります。」', '業務ごとに契約者が選ぶとき', 'ユーザー');
