-- 0279 設定の定義に、画面のまとまり screen_group と、変えるときの確かめの文 confirm_text を持たせる（2026-11-21。書いた人：CL-2。DESIGN_15-2.md の948。
-- CL-3 の連絡帳のお願い 70 とその追記）
--
-- ⛔ それまで：画面は、設定をまとまり（見出し）ごとに並べるのに、設定の名前の頭の言葉（business.・calling.・retry. など）を使っていた。名前の頭は作った順の
-- 都合で付いたので、中身と合わない設定があり、契約者が探す所と違うまとまりに出た（断られたお客様の扱いが「業務」「かける時間帯」の2つに分かれ、
-- 時間帯の区切り retry.time_band_boundaries が「かけ直しの回数」にあって、かけてよい時間帯と離れていた）。変えると電話のかけ方が大きく変わる設定の
-- 印と文は、画面と出力の側の表（output/setting_confirm.json）にあり、設定の定義と別の所に持っていた。
-- ⇒ どちらも設定の定義の1か所に持つ（設定の名前＝鍵は変えない）。これから足す定義も、まとまりが無いと入れられない（データベースが守る）。
--
-- (1) まとまり screen_group：契約者が業務を組み立てる仕事の順の、決まった種類（salescall.setting_group。型の並びが画面の並び＝並びを2か所に持たない）。
--     中身で分ける（名前の頭では分けない）。種類と、画面の言葉の案（言葉は画面と出力の側の words.SETTING_GROUPS が持つ）：
--   calling＝かける時間帯
--   gate＝かける前の確かめ（本数・間隔・かけてよい番号）
--   refusal＝断られたお客様と発信禁止
--   spoken_time＝お客様が言った時刻の読み方
--   call＝通話の進め方
--   callback＝お約束したかけ直し
--   retry＝出なかったときのかけ直し
--   output＝結果の出力
--   quality＝検証モードと品質
--   line＝回線と PC
--   ai＝AI
--   stop＝止めると再開
--   budget＝費用と予算
--   retention＝録音と控えを残す日数
--   notify＝知らせ
--   login＝ログイン
--   timing＝仕組みの待ち時間（管理者）
--     空のまとまりは作らない（どの種類にも1つ以上の設定がある＝試験が見張る）。
-- (2) 確かめの文 confirm_text：あれば、画面はその設定を変えるとき、この文を見せて確かめの窓を通す（無ければ通さない＝印と文を2つに分けない）。
--     付ける決まり（1か所）：変えると、断られた・苦情のあったお客様、または「もうかけないで」と言われた番号にかけることになる設定と、契約者の確かめを
--     通さずに電話をかける・決めることになる設定。文は、どちらに変えると何が起きるかを、契約者が読む言葉だけで書く（説明 help と同じ見張り）。
--     値の言葉（はい・いいえの言葉や選び肢の言葉＝画面と出力の側が持つ）は写さず、「〜する形にすると」と中身で書く（言葉が変わっても文がずれない）。

CREATE TYPE salescall.setting_group AS ENUM ('calling', 'gate', 'refusal', 'spoken_time', 'call', 'callback', 'retry', 'output', 'quality', 'line', 'ai', 'stop', 'budget', 'retention', 'notify', 'login', 'timing');
ALTER TABLE salescall.setting_definitions ADD COLUMN screen_group salescall.setting_group, ADD COLUMN confirm_text text;
ALTER TABLE salescall.setting_definitions DISABLE TRIGGER USER;
DO $$
DECLARE n int; want int;
BEGIN
  WITH v(key, grp) AS (VALUES
  ('calling.hours', 'calling'),
  ('calling.hours_limit', 'calling'),
  ('calling.ng_hours', 'calling'),
  ('retry.time_band_boundaries', 'calling'),
  ('calling.allow_navi_dial', 'gate'),
  ('calling.allow_toll_free', 'gate'),
  ('calling.business_daily_dials', 'gate'),
  ('calling.max_concurrent_calls', 'gate'),
  ('gate.before_other_promise', 'gate'),
  ('gate.max_dials_per_contact_per_day', 'gate'),
  ('gate.other_contractor_interval', 'gate'),
  ('gate.same_contact_min_interval', 'gate'),
  ('gate.same_number_gap', 'gate'),
  ('business.call_declined_again', 'refusal'),
  ('business.declined_wait_days', 'refusal'),
  ('calling.call_after_refusal', 'refusal'),
  ('decision.undecided_as_decided', 'refusal'),
  ('dnc.client_ref_is_stable_id', 'refusal'),
  ('dnc.former_refusal_calls_new_person', 'refusal'),
  ('callback.period_afternoon', 'spoken_time'),
  ('callback.period_evening', 'spoken_time'),
  ('callback.period_forenoon', 'spoken_time'),
  ('callback.period_morning', 'spoken_time'),
  ('callback.period_night', 'spoken_time'),
  ('callback.until_available', 'spoken_time'),
  ('callback.until_other_party', 'spoken_time'),
  ('callback.until_preparing', 'spoken_time'),
  ('callback.until_unavailable', 'spoken_time'),
  ('callee.business_hours', 'spoken_time'),
  ('callee.designated_limit', 'spoken_time'),
  ('call.filler_after', 'call'),
  ('call.frozen_after', 'call'),
  ('call.hangup_after_close_wait', 'call'),
  ('call.hangup_after_decline_wait', 'call'),
  ('call.hold_wait', 'call'),
  ('call.silence_prompt_after', 'call'),
  ('call.wait_ask_after', 'call'),
  ('conversation.ask_back_required', 'call'),
  ('conversation.recheck_required', 'call'),
  ('callback.call_alternate_number', 'callback'),
  ('callback.exact_days', 'callback'),
  ('callback.exact_retry_gaps', 'callback'),
  ('callback.exact_slot', 'callback'),
  ('callback.month_ahead', 'callback'),
  ('callback.window_daily_max', 'callback'),
  ('gate.call_length_days', 'callback'),
  ('gate.exact_promise_guard', 'callback'),
  ('gate.exact_promise_guard_samples', 'callback'),
  ('dial.failed_retry_days', 'retry'),
  ('retry.apology_max', 'retry'),
  ('retry.ask_number_again_max', 'retry'),
  ('retry.max_attempts_per_task', 'retry'),
  ('output.customer_words', 'output'),
  ('approval.human', 'quality'),
  ('measure.draw_after', 'quality'),
  ('measure.sample_per_week', 'quality'),
  ('quality.max_complaint', 'quality'),
  ('quality.max_ear_disagreement', 'quality'),
  ('quality.max_forbidden_speech', 'quality'),
  ('quality.max_technical_error', 'quality'),
  ('quality.max_unknown_judgments', 'quality'),
  ('quality.mock.calls', 'quality'),
  ('quality.mock.min_judgment_match', 'quality'),
  ('quality.mock.min_value_match', 'quality'),
  ('quality.stage.sizes', 'quality'),
  ('line.answer_drop_baseline_days', 'line'),
  ('line.answer_drop_baseline_min_dials', 'line'),
  ('line.answer_drop_hour_min_dials', 'line'),
  ('line.answer_drop_min_dials', 'line'),
  ('line.answer_drop_ratio', 'line'),
  ('line.answer_drop_significance', 'line'),
  ('line.max_dials_per_day', 'line'),
  ('line.test_number', 'line'),
  ('line.test_ring_gaps', 'line'),
  ('line.test_ring_hours', 'line'),
  ('pc.auto_update', 'line'),
  ('pc.down_after', 'line'),
  ('ai.ear_tpm_limit', 'ai'),
  ('ai.plan_conversation', 'ai'),
  ('ai.plan_import', 'ai'),
  ('ai.tier_conversation', 'ai'),
  ('ai.tier_import', 'ai'),
  ('ai.tpm_limit', 'ai'),
  ('ai.tpm_per_call', 'ai'),
  ('ai.tpm_safety', 'ai'),
  ('judge.models', 'ai'),
  ('judge.outage_bypass', 'ai'),
  ('stop.ai.probe_interval', 'stop'),
  ('stop.ai.resume_after_successes', 'stop'),
  ('stop.ai_quota.rest', 'stop'),
  ('stop.ai_rate_limit.backoff', 'stop'),
  ('stop.trouble.stop_after_rows', 'stop'),
  ('budget.client_daily_yen', 'budget'),
  ('budget.deficit_grace', 'budget'),
  ('budget.max_cost_ratio', 'budget'),
  ('budget.tenant_daily_yen', 'budget'),
  ('backup.ai.keep_days_cloud', 'retention'),
  ('backup.ai.keep_days_drive', 'retention'),
  ('backup.overdue_after', 'retention'),
  ('backup.retry_after', 'retention'),
  ('backup.submission.keep_days_drive', 'retention'),
  ('recording.keep_days_cloud', 'retention'),
  ('recording.keep_days_drive', 'retention'),
  ('notify.admin_chatwork_room', 'notify'),
  ('notify.admin_sms_to', 'notify'),
  ('notify.chatwork_room', 'notify'),
  ('notify.max_tries_chatwork', 'notify'),
  ('notify.max_tries_floor', 'notify'),
  ('notify.max_tries_sms', 'notify'),
  ('notify.send_hours', 'notify'),
  ('notify.sms_to', 'notify'),
  ('login.lock_seconds', 'login'),
  ('login.max_failures', 'login'),
  ('login.password_min_length', 'login'),
  ('login.session_seconds', 'login'),
  ('gate.claim_lifetime', 'timing'),
  ('gate.denied_retry_wait', 'timing'),
  ('line.lease', 'timing'),
  ('line.lease_renew', 'timing'),
  ('retry.no_opening_recheck', 'timing'),
  ('verification.recording_wait', 'timing'),
  ('verification.retry_wait', 'timing')
  )
  UPDATE salescall.setting_definitions d SET screen_group = v.grp::salescall.setting_group FROM v WHERE d.key = v.key;
  GET DIAGNOSTICS n = ROW_COUNT;
  SELECT count(*) INTO want FROM salescall.setting_definitions;
  IF n <> want THEN   -- 書いた鍵が定義と1つでもずれていれば止める（黙って割り当て漏らさない）
    RAISE EXCEPTION 'まとまりを割り当てた設定が % で、定義は % です', n, want USING ERRCODE = 'check_violation';
  END IF;
  WITH v(key, txt) AS (VALUES
  ('approval.human', '検証モードを使わない形にすると、承認なしで電話をかけ、判定した結果をそのまま確定します（フルオート）。使う形にすると、発信の前と結果の確定の前に承認が要ります。'),
  ('business.call_declined_again', '断られたお客様にまたかける形にすると、用件や最終目的を断られたお客様にも、新しいお客様リストでもう一度お電話します。時期を置いて同じ用件で改めてご案内したい業務だけにしてください。'),
  ('calling.call_after_refusal', 'かける形にすると、この依頼者のほかの業務で断られた・苦情があったお客様にも、ほかの業務・ほかのお客様リストでお電話します。'),
  ('dnc.former_refusal_calls_new_person', 'かける形にすると、前の担当の方が「もうかけないで」と言った番号でも、担当の替わった方へお電話します。かけない形にすると、別の番号を足すまで、その方にはかけません。'),
  ('decision.undecided_as_decided', '決まったとみなす形にすると、断り・番号違い・怒っていたかを録音の確かめでも決められなかったとき、契約者の承認を待たずに決まったとみなし、そのお客様にはもうかけません。')
  )
  UPDATE salescall.setting_definitions d SET confirm_text = v.txt FROM v WHERE d.key = v.key;
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n <> 5 THEN
    RAISE EXCEPTION '確かめの文を入れた設定が % です（5 のはず）', n USING ERRCODE = 'check_violation';
  END IF;
END $$;
ALTER TABLE salescall.setting_definitions ENABLE TRIGGER USER;
ALTER TABLE salescall.setting_definitions ALTER COLUMN screen_group SET NOT NULL,
  ADD CONSTRAINT setting_definitions_confirm_text CHECK (confirm_text IS NULL OR btrim(confirm_text) <> '');
