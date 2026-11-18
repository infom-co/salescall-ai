-- 0148 見直し23 の直し：本番の「同じ原因が1日3件」の名残を外す・止める処理の無い原因で新しく止めるのを断る・決定に合わせた設定の印・
-- コードに直に書いた数を仮の設定へ（2026-10-30。書いた人：CL-2。DESIGN.md 8章・9-1・10-1）
--
-- (3) 本番（検証モードでない業務）の「その日のうちに同じ原因が設定の件数になったら管理者へ知らせる」（設定 quality.production_same_cause_per_day）
--     を外す。2026-10-16 の決定（本番は同じ原因が1日3件で業務を止める）は、2026-10-22 の決定（「本番は止めない（担当への修正依頼と
--     管理者への知らせ）」）で撤回された。いまは締めの処理が、通話ごとに契約者と管理者へ知らせ、直す担当へ修正依頼を出している
--     （core/settle）。この件数の知らせ（知らせの種類 quality_alert＝core/quality_stage の本番の評価）は、その撤回された決定の名残だった。
--     検証モードの2つの設定の説明から、外した設定の名前を消す。
-- (4) 止める処理の無い原因（保存の失敗 storage_failure・会話AIの利用拒否 project_denied・費用の上限 budget・赤字 deficit）で、新しく止めるのを
--     断る（守り stop_events_cause_retired）。どれも、いまはその原因で止めない（保存の失敗＝修正依頼を出して次の行へ＝2026-10-16
--     ユーザー／利用拒否＝そのプロジェクトを休ませ次のキー＝2026-10-17 ユーザー／費用の上限＝注意だけ＝2026-10-16 ユーザー／赤字＝
--     知らせるだけ＝2026-10-22 深夜 ユーザー「止めない。知らせるだけでいい」）。前の記録の値としては残す（種類 stop_cause の値は外さない。
--     前の記録は確かめない＝NOT VALID。停止の記録は書き換えられないので、守りが見るのは新しく書く記録だけ）。Python の側は core/stops.Cause
--     からも外した（RETIRED_CAUSES）。
-- (7) 設定の「仮・確定」の印を、管理者の決定に合わせる（値は変えない）：
--     gate.other_contractor_interval（3日）＝計画書 1-4 の 2026-10-16 ユーザーのご決定「ほかの契約者と同じ番号にかけた場合の間隔＝契約者の
--     設定（「○○時間・○○日・かけない」。決めるまでの初めの値は3日）」／quality.max_forbidden_speech（0件）＝計画書 原則9 の 2026-10-16
--     ユーザーのご決定「品質の門（言ってはいけないこと・品質の基準・判定できないを1件で止める）…は、検証モードだけ」。
-- (9) コードに直に書いた数を、仮の設定へ移す（画面の仮の値の一覧＝core/settings.provisional に出す）。どれも管理者の決定ではない（CL-2 が
--     決めた値）ので「仮」。値は今までと同じ。どれもシステムだけが決める（管理者が変える）：
--     - 回線の応答率の見張り（core/answer_rate）：line.answer_drop_baseline_days（いつもの割合を見る日数 14日）・
--       line.answer_drop_baseline_min_dials（いつもの割合を決めるのに要る前の日々の本数 100本）・line.answer_drop_hour_min_dials（時間帯
--       ごとの割合を使うのに要る本数 20本）・line.answer_drop_significance（偶然では起きにくい下がり方の確率 0.01）
--     - 門（core/gate.promise_guard_seconds）：gate.call_length_days（業務の平均通話時間を測る直近の日数 30日）
--     - 誤判定率の抜き取り（core/measure.draw）：measure.draw_after（週の終わりから抜き取りまであける長さ 1日）
--     - 控え（core/backup）：backup.retry_after（失敗したあと、同じ種類をやり直すまであける長さ 1時間）・backup.overdue_after（1日1回の
--       控えが「取れていない」とみなす長さ 26時間）
--     - 次の動き（core/next_action._no_opening）：retry.no_opening_recheck（これから先、かけてよい時間帯が見つからない行を、もう一度
--       確かめるまでの長さ 1日）

-- ═════════ (3) 本番の「同じ原因が1日3件」の名残を外す ═════════
ALTER TABLE salescall.setting_values DISABLE TRIGGER setting_values_frozen;
ALTER TABLE salescall.setting_definitions DISABLE TRIGGER setting_definitions_frozen;
DELETE FROM salescall.setting_values WHERE key = 'quality.production_same_cause_per_day';
DELETE FROM salescall.setting_definitions WHERE key = 'quality.production_same_cause_per_day';
UPDATE salescall.setting_definitions
SET meaning = '「判定できない」（unknown）を、何件まで許すか（超えたら業務を止める）。検証モードの業務だけ（本番は止めない。1件ごとに'
              '契約者と管理者へ知らせ、直す担当へ修正依頼を出す＝2026-10-22 ユーザー）'
WHERE key = 'quality.max_unknown_judgments';
UPDATE salescall.setting_definitions
SET meaning = '言ってはいけないことの検出（2つの耳で確かめたもの）の件数の上限。検証モードの業務だけ（本番は止めない。1件ごとに契約者と'
              '管理者へ知らせ、直す担当へ修正依頼を出す＝2026-10-22 ユーザー）'
WHERE key = 'quality.max_forbidden_speech';
ALTER TABLE salescall.setting_definitions ENABLE TRIGGER setting_definitions_frozen;
ALTER TABLE salescall.setting_values ENABLE TRIGGER setting_values_frozen;

-- ═════════ (4) 止める処理の無い原因で、新しく止めるのを断る（前の記録の値は残す） ═════════
ALTER TABLE salescall.stop_events ADD CONSTRAINT stop_events_cause_retired
  CHECK (action <> 'stop' OR cause::text NOT IN ('storage_failure', 'project_denied', 'budget', 'deficit')) NOT VALID;

-- ═════════ (7) 決定に合わせた「確定」の印（値は同じ。前の値は記録に残る） ═════════
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('gate.other_contractor_interval', 'system', '{"days": 3}', 'decided',
 '2026-10-16 ユーザーのご決定（計画書 1-4）：「ほかの契約者と同じ番号にかけた場合の間隔＝契約者の設定（「○○時間・○○日・かけない」。'
 '決めるまでの初めの値は3日）」', '管理者が変えるとき', 'CL-2（管理者の決定を写した）'),
('quality.max_forbidden_speech', 'system', '0', 'decided',
 '2026-10-16 ユーザーのご決定（計画書 原則9）：「品質の門（言ってはいけないこと・品質の基準・判定できないを1件で止める）…は、検証モード'
 'だけ」＝検証モードの業務は、言ってはいけないことが1件でも出たら止める（許す件数 0）', '管理者が変えるとき', 'CL-2（管理者の決定を写した）');

-- ═════════ (9) コードに直に書いた数を、仮の設定へ（値は今までと同じ） ═════════
INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, finest_scope) VALUES
('line.answer_drop_baseline_days', 'override', 'integer',
 '回線（番号）の応答率の見張りで、「いつもの割合」を見る前の日数（今日を除く）。今日の出てもらえた割合を、この日数の同じ時間帯の割合と'
 '比べて、大きく下がったら契約者に知らせる（発信は止めない）', 'DESIGN.md 5-2-3・Intent.md 8-6', 'system'),
('line.answer_drop_baseline_min_dials', 'override', 'integer',
 '回線（番号）の応答率の見張りで、「いつもの割合」を決めるのに要る、前の日々のかけた本数。これより少ない回線（新しい番号など）は、'
 'いつもの割合が分からないので、下がったかを判断しない', 'DESIGN.md 5-2-3・Intent.md 8-6', 'system'),
('line.answer_drop_hour_min_dials', 'override', 'integer',
 '回線（番号）の応答率の見張りで、時間帯ごとの「いつもの割合」を使うのに要る、前の日々のその時間帯の本数。足りない時間帯は、前の日々'
 '全体の割合で見込む（昼休み・夕方の出てもらいにくさで、知らせないため）', 'DESIGN.md 5-2-3・Intent.md 8-6', 'system'),
('line.answer_drop_significance', 'override', 'ratio',
 '回線（番号）の応答率の見張りで、知らせる下がり方の確からしさ。いつもの割合どおりなら、この確率でしか起きない下がり方のときだけ'
 '知らせる（たまたまの下がりで知らせない）', 'DESIGN.md 5-2-3・Intent.md 8-6', 'system'),
('gate.call_length_days', 'override', 'integer',
 '業務の平均通話時間を測る、直近の日数。時刻を指定された約束の電話の前に、約束の数だけ回線を空けておく長さ（業務の平均通話時間）を、'
 'この日数の通話から測る（2026-10-19 ユーザー：「業務ごとに平均通話時間を測って、それを利用するといいでしょう」）', 'DESIGN.md 5-2', 'system'),
('measure.draw_after', 'override', 'seconds',
 '誤判定率を測る抜き取りを、週の終わり（日本時間の日曜の24時）から、どれだけあけて選ぶか（秒）。その週の通話の締め（判定）が済むのを待つ',
 'DESIGN.md 10-3・Intent.md 16-3', 'system'),
('backup.retry_after', 'override', 'seconds',
 '控えが取れなかった・戻せなかったあと、同じ種類（同じ契約者・同じ依頼者）の控えを、やり直すまであける長さ（秒）。見守り役が数分ごとに'
 '呼んでも、重い書き出しを繰り返さない', 'DESIGN.md 3-10', 'system'),
('backup.overdue_after', 'override', 'seconds',
 '1日1回の控え（データベース全体・依頼者ごとの AI用）が「取れていない」とみなす長さ（秒）。戻せることまで確かめた控えが、この長さの'
 'あいだ1つも無ければ、管理者（依頼者ごとの控えは、その契約者にも）に1日1回知らせる。1日に、取る時刻のずれの余裕を足した長さ',
 'DESIGN.md 3-10・Intent.md 14-1', 'system'),
('retry.no_opening_recheck', 'override', 'seconds',
 'これから先（31日）、かけてよい時間帯が見つからない行（依頼者の時間帯を空にした・NG時間が時間帯を覆う など、設定のその時の形による）を、'
 '終えずに、もう一度確かめるまでの長さ（秒）。設定を戻せば、また、かけられる', 'DESIGN.md 7-2', 'system');
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('line.answer_drop_baseline_days', 'system', '14', 'provisional',
 '曜日の偏りを2回ずつならす長さ（CL-2 が決めた値。前は core/answer_rate の BASELINE_DAYS に直に書いていた）', '本番の最初の1か月の応答率を見て', 'CL-2'),
('line.answer_drop_baseline_min_dials', 'system', '100', 'provisional',
 '割合がぶれにくい本数（CL-2 が決めた値。前は core/answer_rate の BASELINE_MIN_DIALS に直に書いていた）', '本番の最初の1か月の応答率を見て', 'CL-2'),
('line.answer_drop_hour_min_dials', 'system', '20', 'provisional',
 '時間帯ごとの割合がぶれにくい本数（CL-2 が決めた値。前は core/answer_rate の HOUR_MIN_DIALS に直に書いていた）', '本番の最初の1か月の応答率を見て', 'CL-2'),
('line.answer_drop_significance', 'system', '0.01', 'provisional',
 '100回に1回も起きない下がり方だけ知らせる（CL-2 が決めた値。前は core/answer_rate の SIGNIFICANCE に直に書いていた）', '本番の最初の1か月の知らせの数を見て', 'CL-2'),
('gate.call_length_days', 'system', '30', 'provisional',
 '直近1か月の通話で測る（CL-2 が決めた値。前は core/gate の CALL_LENGTH_DAYS に直に書いていた）', '本番の最初の1か月の通話時間を見て', 'CL-2'),
('measure.draw_after', 'system', '86400', 'provisional',
 '週の終わりから1日あければ、その週の通話の締めがほぼ済む（CL-2 が決めた値。前は core/measure の DRAW_AFTER に直に書いていた）', '本番の最初の抜き取りを見て', 'CL-2'),
('backup.retry_after', 'system', '3600', 'provisional',
 '1時間あければ、重い書き出しを繰り返さず、その日のうちに何度かやり直せる（CL-2 が決めた値。前は core/backup の RETRY_AFTER に直に書いていた）',
 '本番の最初の1か月の控えの記録を見て', 'CL-2'),
('backup.overdue_after', 'system', '93600', 'provisional',
 '1日（24時間）に、取る時刻のずれの余裕2時間を足した26時間（CL-2 が決めた値。前は core/backup の OVERDUE に直に書いていた）',
 '本番の最初の1か月の控えの記録を見て', 'CL-2'),
('retry.no_opening_recheck', 'system', '86400', 'provisional',
 '設定の直しを、1日に1回は拾う（CL-2 が決めた値。前は core/next_action の NO_OPENING_RECHECK に直に書いていた。2026-10-16 の見直し G6）',
 '本番の最初の1か月の記録を見て', 'CL-2');
