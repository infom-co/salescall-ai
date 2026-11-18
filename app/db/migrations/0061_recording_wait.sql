-- 0061 録音が届かない通話は、決めた時間を過ぎたら「録音なし」で確かめを済ませる（2026-10-18 ユーザー決定。DESIGN.md 4-2・15-2 の158）
--
-- ユーザー（2026-10-18）：通話が終わってから10分たっても録音が届かなければ、システムが「録音なし」と記録して、通話中の報告だけで
-- 結果を決める（「基本的にそれでいい」。前は録音が届くまで確かめが待ちのままで、そのお客様にいつまでもかけられなかった。担当
-- （CL-2）が「録音は戻らない」と決めるのを待つ形は、担当がいつもいるわけではないので成り立たない＝ユーザー「CL-2が契約者の場合や
-- 毎回、すぐに判断できるんですか？」）。録音が残らないこと自体は仕組みの不具合なので、修正依頼は別に出す（その通話は待たない）。
-- (1) 検証の結果の数に「録音なし」を持つ（録音なしなら、確かめた数・言ってはいけないことの数は0）。
-- (2) 待つ時間の設定（10分）。過ぎたかを決めるのは core/attempts.py の finish_without_recording の1か所。

ALTER TABLE salescall.verification_summaries
  ADD COLUMN recording_missing boolean NOT NULL DEFAULT false,
  ADD CONSTRAINT verification_summaries_missing_checks_nothing
    CHECK (NOT recording_missing OR (items_checked = 0 AND forbidden_speech = 0));

INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, finest_scope) VALUES
('verification.recording_wait', 'override', 'seconds',
 '通話が終わってから、録音が届くのを待つ長さ。過ぎても届かなければ、確かめを「録音なし」で済ませ、通話中の報告だけで結果を決める',
 'DESIGN.md 4-2', 'system');
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('verification.recording_wait', 'system', '600', 'decided',
 '2026-10-18 ユーザー決定：通話が終わってから10分たっても録音が届かなければ「録音なし」（録音はふつう1分ほどで届く）',
 '録音の届く時間を本番で測ったとき', 'ユーザー');
