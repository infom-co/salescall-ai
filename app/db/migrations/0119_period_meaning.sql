-- 0119 言葉ごとの幅の設定の意味の文を、今の決まりに直す（2026-10-23。書いた人：CL-2）
--
-- 0088 の callback.period_morning の意味の文（設定の i マークに出る）が「かけてよい時間帯と重なる所だけ使う」のままだった。
-- これは 2026-10-18 に CL-2 が足した決まりで、ユーザーが「OK」としたのは幅の値（朝〜10時・午前中〜12時・午後13時〜・夕方16〜18時・
-- 夜18〜20時）だけだった（見直し16で分かった）。⇒ 2026-10-23 ユーザー決定（「お客様の日時指定の場合は、『お客様指定の歯止め時間』で
-- 見るようにしないといけないんじゃないですか？」）のとおり、言葉の幅はお客様の指定で、依頼者の時間帯・NG時間では切らない。言葉で
-- 端が決まっていない側（「朝」の始まり＝00:00 と書いた所・「午後」の終わり）は、お客様指定の歯止め時間（callee.designated_limit）で
-- 埋めて切る（決めるのは core/calling_hours.callee_range の1か所。0118）。
ALTER TABLE salescall.setting_definitions DISABLE TRIGGER setting_definitions_frozen;
UPDATE salescall.setting_definitions SET meaning =
  'お客様が「朝」「朝一」と言ったかけ直しの幅（1日の中の時刻。お客様の指定なので、依頼者の時間帯・NG時間では切らない。'
  '始まりの 00:00 は「言葉で決まっていない」の印で、お客様指定の歯止め時間 callee.designated_limit で埋める）'
 WHERE key = 'callback.period_morning';
UPDATE salescall.setting_definitions SET meaning =
  'お客様が「午前」「午前中」と言ったかけ直しの幅（始まりの 00:00 は、お客様指定の歯止め時間で埋める）'
 WHERE key = 'callback.period_forenoon';
UPDATE salescall.setting_definitions SET meaning =
  'お客様が「午後」と言ったかけ直しの幅（「午後一」は、その始まりから1時間。終わりの 00:00 は、お客様指定の歯止め時間で埋める）'
 WHERE key = 'callback.period_afternoon';
ALTER TABLE salescall.setting_definitions ENABLE TRIGGER setting_definitions_frozen;
