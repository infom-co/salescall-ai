-- 0149 2026-10-30 の管理者のご決定：赤字の確認のあとの知らせと、お詫びの電話に出ないときの回数（書いた人：CL-2。DESIGN.md 10-1・12-1）
--
-- (1) 赤字（2026-10-30 管理者：「警告して、1時間で赤字の確認ボタンを契約者が押したらもう知らせない。確認ボタンは画面に出したまま。」）：
--     確認ボタンが押されたら、その赤字が続くあいだは知らせ直さない（core/usage._deficit_step の1か所）。押されなければ1時間（設定
--     budget.deficit_grace）で契約者と管理者へもう一度知らせる（今のまま）。黒字に戻ったら警告を閉じ、また赤字になったら新しく警告する。
--     画面が確認ボタンを出し続けるための「いまの赤字の警告（確認を待っている・確認済みで赤字が続いている）」は core/usage.deficit_notice の
--     1か所で読む。⇒ 確認の有効期間の設定 budget.deficit_confirm_valid（CL-2 の仮の値＝1日ごとに知らせ直す）を外す。警告を閉じた理由の
--     「確認が切れた」（confirm_expired）は、もう書かない（前の記録の値としては残す）。
-- (2) 言ってはいけないことのあとのお詫びの電話にお客様が出ないとき（2026-10-30 管理者：「案A（3回）　出ない場合、連続してかけないように
--     すること」）：回数の上限 retry.apology_max は3のまま「確定」に（値は変えない）。かけ直しは続けてかけず、時間帯を変えてかける＝番号の
--     聞き直しと同じ1か所（core/next_action._new_band_retry。まだ試していない時間帯の次の始まり・前のお詫びの電話から同じお客様への
--     最短の間隔のあと＝gate.same_contact_free_at・「その日はかけない」の決まり not_today）を使う（お詫びの電話用の別の決まりは作らない）。
-- (3) お詫びの電話（言ってはいけないことのあと）を、依頼者からの報酬（1本あたり・時給）に数えない（2026-10-30 管理者：今の作りのまま
--     確定）：決まりはデータベースの1か所 salescall.counts_for_reward（0123。1本あたり＝salescall.counts_per_call・時給の時間＝
--     salescall.work_seconds が読む）。ここでは変えない。

-- ═════════ (1) 赤字の確認の有効期間の設定を外す ═════════
ALTER TABLE salescall.setting_values DISABLE TRIGGER setting_values_frozen;
ALTER TABLE salescall.setting_definitions DISABLE TRIGGER setting_definitions_frozen;
DELETE FROM salescall.setting_values WHERE key = 'budget.deficit_confirm_valid';
DELETE FROM salescall.setting_definitions WHERE key = 'budget.deficit_confirm_valid';
ALTER TABLE salescall.setting_definitions ENABLE TRIGGER setting_definitions_frozen;
ALTER TABLE salescall.setting_values ENABLE TRIGGER setting_values_frozen;

-- ═════════ (2) お詫びの電話に出ないときの回数：3回を確定に（値は同じ。前の値は記録に残る） ═════════
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('retry.apology_max', 'system', '3', 'decided',
 '2026-10-30 管理者のご決定：「案A（3回）　出ない場合、連続してかけないようにすること」＝お詫びの電話に出ないときは、時間帯を変えて、'
 '最初の1本を含めて3回まで（続けてはかけない。時間帯を変えるかけ直しは、番号の聞き直しと同じ1か所＝core/next_action._new_band_retry）',
 '管理者が変えるとき', 'CL-2（管理者の決定を写した）');
