-- 0117 お客様が言った端だけ依頼者の時間の外でかける・受け付けられない番号へかけ続けない（見直し15。2026-10-22。書いた人：CL-2）
--
-- (1) 約束の幅のうち、依頼者の時間帯・NG時間・深夜早朝の歯止めの外でもかけてよいのは、お客様が実際に言った端の回だけ
--     （window_free。0時からの秒の [始まり, 終わり)。window_daily の内側）。言葉の表から来た端（「朝」の0時・「午後」の24時・
--     「3時まで」の始まり・「4時以降」の終わり）は、依頼者のいつもの時間と重なる所だけ（2026-10-19 ユーザー「言葉ごとの幅は、
--     かけてよい時間帯と重なる所だけ使う」OK と、2026-10-22 ユーザー「お客様が指定した時刻ならNG時間でもかけてよい」の両方）。
--     ⛔ 見直し15：前は幅の全部を依頼者の時間の外でもかけ、「朝なら」の約束に0時、「午後なら」の約束に23時にかけえた。
--     決まりは core/calling_hours.callee_hours の1か所（門と次の動きが同じものを読む）。
-- (2) お客様がその日を言ったか（window_day_said）：言った日（「土曜の夕方」「週末なら」）は依頼者の休みの日でもかける。言っていない
--     （「夕方なら」の次の日の回・「すぐ」）は、依頼者のかける日だけ（休みの日の回は、かけられる時刻の無い回として数えずに飛ばす）。
-- (3) 回線の部品が番号を受け付けない（発信に失敗＝お客様の電話は鳴っていない）電話が、同じ番号で（どの回線でも）続けて
--     dial.failed_same_number_max 回になったら、その番号へはかけない（計画書 10-3「無限に再試行しない」。行の回数には数えないので、
--     前は行が終わらず、5分ごとに同じ番号へかけ続けた）。数えるのは core/next_action の1か所。

-- (1)(2)
ALTER TABLE salescall.task_next_actions
  ADD COLUMN window_free integer[],
  ADD COLUMN window_day_said boolean NOT NULL DEFAULT false,
  ADD CONSTRAINT task_next_actions_window_free_check CHECK (
    window_free IS NULL
    OR (window_daily IS NOT NULL AND array_length(window_free, 1) = 2
        AND window_daily[1] <= window_free[1] AND window_free[1] < window_free[2] AND window_free[2] <= window_daily[2]));
-- これまでの時刻ちょうどの約束（その回の全部をお客様が言った）
UPDATE salescall.task_next_actions SET window_free = window_daily WHERE callee_at IS NOT NULL AND window_daily IS NOT NULL;

CREATE OR REPLACE VIEW salescall.current_next_actions WITH (security_invoker = true) AS
  SELECT n.* FROM salescall.task_next_actions n
  WHERE n.seq = (SELECT max(m.seq) FROM salescall.task_next_actions m WHERE m.task_id = n.task_id);

-- (3)
INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, finest_scope) VALUES
('dial.failed_same_number_max', 'override', 'integer',
 '回線の部品（スマホのアプリ）が番号を受け付けず、電話をかけられなかった（お客様の電話は鳴っていない）ことが、同じ番号で続けてこの回数に'
 'なったら、その番号へはかけません（行の回数には数えません。お客様が言った別の番号があれば、そちらへかけます。無ければ、その行を'
 '終えます）。無限にかけ直さないための歯止めです。',
 'DESIGN.md 7-2', 'client');
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('dial.failed_same_number_max', 'system', '3', 'provisional',
 '計画書 10-3「無限に再試行しない」。回線の一時の不具合なら、ほかの番号も失敗して回線の異常として止まるので、同じ番号だけが続けて'
 '受け付けられないのは番号の側の理由とみなす（3回は仮）', '回線の部品の失敗の記録を見て', 'CL-2');
ALTER TABLE salescall.setting_values ADD CONSTRAINT setting_values_failed_same_number_at_least_one
  CHECK (key <> 'dial.failed_same_number_max' OR (jsonb_typeof(value) = 'number' AND (value #>> '{}')::numeric >= 1));
