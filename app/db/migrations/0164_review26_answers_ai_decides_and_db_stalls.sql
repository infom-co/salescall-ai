-- 0164 2026-10-31 管理者のご判断（伺い3・伺い4・伺い5）を入れる。書いた人：CL-2。設計書 10-2-2・9-1・7-1d・15-2 の395〜。
--
-- 伺い3（「伺い3は案2だが、確かめるのは人でなくAI、どうしてもAIでは判断できない場合のみ、契約者に判断をゆだねる。AIがどうしても判断
-- できない場合の設定も選択できるようにしておくこと」）：言ってはいけないことを言った通話で、切れ方が分からない（営業拒否の言葉も、声が
-- 届かなかった証拠も無いまま切れた）お客様は、確かめのかけ直しをせず（AI が言ってはいけないことを言ったお客様へ、すぐまた AI がかけない）、
-- 断ったかを取り消せない決定の保留にする（core/judge の断りの候補 unclear_forbidden・Irreversible.undecided）。行はかけずに保留し、同じ
-- お客様のほかの行も待たせる（salescall.decision_pending の1か所）。決めるのは通話後の録音の確かめ（AI。録音から作り直した報告の切れ方）。
-- それでも決まらなければ、設定 decision.undecided_as_decided の決め方：
--   いいえ（初めの値）＝契約者に委ねる（いまの形：契約者が録音とやり取りを確かめて、承認か差し戻しを押すまでかけない）
--   はい＝決まったとみなす（断り・番号違い・怒っていたとして書く＝お客様へはかけない側。契約者と管理者に知らせるだけ）
-- この設定は、録音でも決められない取り消せない決定のすべてに同じく効く（1か所＝core/approval.assumable。2026-10-30 夜 (1) の「保留：
-- 検証モードで方向性を決めます」は、初めの値「いいえ」で、いまの形のまま）。検証モードでかけた通話は、いつも契約者の承認（2026-10-30 (6)）。
-- ⛔ 見直し26a の 2：前は行を終えるだけで、断ったかが分からないお客様を「断っていない」扱いにし、ほかの行から間隔が空けばかけた。
--
-- (1) 保留の版の印 checked に assumed（録音の確かめでも決められず、設定で決まったとみなした版。待つ中身は、その版に残す）を足す。
--     assumed の版は、録音の確かめ待ちだけ・中身が1つ以上。
-- (2) いまの保留の中身（salescall.decision_open の1か所）は、いちばん新しい版が assumed なら空（もう待たない）。
-- (3) 設定 decision.undecided_as_decided を足す（業務ごとまで変えられる。初めはいいえ）。
--
-- 伺い4（案1）・伺い5（数えない）は、Python の1か所ずつ（core/spoken_time._named_day・core/errors.counts_rows と supervisor/line.storage_back）。

-- ═════════ (1) 保留の版の印 ═════════
ALTER TABLE salescall.decision_waits DROP CONSTRAINT decision_waits_checked_check;
ALTER TABLE salescall.decision_waits ADD CONSTRAINT decision_waits_checked_check
  CHECK (checked IN ('agreed', 'disagreed', 'none', 'assumed'));
ALTER TABLE salescall.decision_waits ADD CONSTRAINT decision_waits_assumed
  CHECK (checked <> 'assumed' OR (kind = 'recording' AND cardinality(parts) > 0));

-- ═════════ (2) いまの保留の中身 ═════════
CREATE OR REPLACE FUNCTION salescall.decision_open(p_attempt uuid) RETURNS text[]
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  SELECT coalesce((SELECT w.parts FROM salescall.decision_waits w
                   WHERE w.attempt_id = p_attempt AND w.seq = salescall.decision_wait_seq(p_attempt)
                     AND w.checked <> 'assumed'), '{}')
$$;

-- ═════════ (3) 設定 ═════════
INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, finest_scope) VALUES
('decision.undecided_as_decided', 'override', 'boolean',
 'お客様の断り・番号違い・怒っていた（取り消せない決定）を、通話後の録音の確かめ（AI）でも決められなかったときの決め方です。いいえ（初め）'
 '＝契約者に委ねる（契約者が録音とやり取りを確かめて、承認か差し戻しを押すまで、そのお客様にはかけません）／はい＝決まったとみなす'
 '（断り・番号違い・怒っていたとして扱い、そのお客様にはかけません。契約者と管理者に知らせます）。言ってはいけないことを言った通話で、'
 '切れ方が分からない（お客様が何も言わずに切った）ときの「断ったか」も同じです。検証モードの業務は、いつも契約者の承認です。'
 '録音があるのに確かめないまま済んだときは、AI が判断できなかったのではないので、いつも契約者に委ね、会話の担当へ直しを頼みます',
 'DESIGN.md 10-2-2', 'business');
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('decision.undecided_as_decided', 'system', 'false', 'decided',
 '2026-10-31 管理者：「確かめるのは人でなくAI、どうしてもAIでは判断できない場合のみ、契約者に判断をゆだねる。AIがどうしても判断できない'
 '場合の設定も選択できるようにしておくこと」', '契約者が業務ごとに選ぶとき', 'ユーザー');
