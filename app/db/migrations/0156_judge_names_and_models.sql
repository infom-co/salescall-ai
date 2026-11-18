-- 0156 判定の顔ぶれの説明を、10-30 夜〜10-31 未明のご決定に合わせる（書いた人：CL-2。CL-1 の連絡帳 4-61・4-62。計画書 12-3・13章）
--
-- 契約者の設定「判定に使うAI」（judge.models）の説明が 10-29 の形（Jev を先に、確かでなければ2つの Sonnet）のままで、10-30 から決まった
-- 顔ぶれ（お呼びする段は d1-ultra と Jev・答えの段と中身の判定は d1-ultra を先に考える Sonnet だけ・Flash Lite は止める側だけ・
-- Anthropic へ直接送れないときだけ緊急の道）と、送り先（Liquid＝米国・緊急の道の Google＝米国）が書かれていなかった（CL-1 の連絡帳 4-61 の
-- 頼み：「画面の説明には『Liquid（米国）にも送ります』を足してください」＝送り先を契約者が選べるようにした 10-18 のご決定のため）。
-- 設定の値の名前は変えない（CL-1 の頼み）。どの組で呼ぶかを決めて呼ぶのは会話（CL-1）。⏳ sonnet の組の答えの段の顔ぶれは CL-1 に確かめ中
-- （CL-2 の連絡帳 4-171）。
ALTER TABLE salescall.setting_definitions DISABLE TRIGGER setting_definitions_frozen;
UPDATE salescall.setting_definitions
SET meaning = '意味の判定に使うAI。jev_and_sonnet（初めの値）＝目的のお客様をお呼びする段の2つの質問は、d1-ultra（Liquid）と Jev 1.14 を'
              '先に同時に出し、どちらも確かでなければ考えない Claude Sonnet 5.5 を出す。答えの意味の判定と、AIが自分の言葉で言う文の中身の判定は、'
              'd1-ultra を先に出し（Jev は d1-ultra が動かないときだけの代わり）、確かでなければ考える Sonnet だけを出す。その場の判定'
              '（3.5 Flash Lite）は、繋ぐのを止める側だけに使う／sonnet＝2つの Sonnet 5.5（考える・考えない）の組（d1-ultra は出さない。Jev は'
              '止まった判定の代わりだけ）。どちらでも、Anthropic へ直接送れないときだけ、同じ Sonnet を緊急の道（OpenRouter を通して Google が'
              '預かる Claude）へ送る。お客様の言葉は、Anthropic（米国）・Google（米国）に加えて、jev_and_sonnet では TypeSafe と Liquid（米国。'
              'OpenRouter 経由）にも送ります。どの組で呼ぶかを決めて呼ぶのは会話（CL-1）'
WHERE key = 'judge.models';
ALTER TABLE salescall.setting_definitions ENABLE TRIGGER setting_definitions_frozen;
