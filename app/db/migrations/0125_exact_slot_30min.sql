-- 0125 1回分（callback.exact_slot）を30分にし、時刻ちょうどの約束の1日目の最後の1本を「1回分の終わり」に置く
--      （DESIGN.md 7-1d・7-2-1。2026-10-23。書いた人：CL-2）
--
-- ユーザー（2026-10-23）：「歯止めの外の時刻の『1回分』の長さ：時刻ちょうどの約束も含めて、30分の方がいいんじゃない。」
-- 問い「1回分を30分にすると、時刻ちょうどの約束の4本目（15:00・15:05・15:15・15:35 の 15:35）が30分の外になる。案A：4本目を
-- 30分の終わりに合わせ 15:00・15:05・15:15・15:30 の4本」⇒ 答え「A」。
-- ⇒ (1) callback.exact_slot の初めの値を 3600秒 → 1800秒。時刻ちょうどの約束の回の時間と、歯止めの外の時刻を確かめて「はい」と
--        言われたときの1回分（callee_range の beyond_slot）の両方が、この1つを使う。依頼者ごとに変えられるのは今のまま。
--    (2) callback.exact_retry_gaps（前の電話からの間の並び）の初めの値を [300, 600, 1200] → [300, 600]。並びのあとの最後の1本は、
--        間ではなく「1回分の終わり」に置く（next_action._exact_next_try の1か所）。並びが1回分の終わりを越えるときは、そこで
--        最後の1本へ移る。1回分を変えても、最後の1本は必ず1回分の終わりに来る。
--    (3) 最後の1本を回線のふさがり・見守り役の回りの遅れで少し遅れてもかけられるように、門が見るその回の幅は、1回分の終わりから
--        並びの最初の間（初め5分）だけ長い（next_action._exact_window の1か所。幅の端の決まり＝[始まり, 終わり) は calling_hours の
--        1か所のまま）。
ALTER TABLE salescall.setting_definitions DISABLE TRIGGER setting_definitions_frozen;
UPDATE salescall.setting_definitions
SET meaning = '1回分の長さ（秒）。(1) お客様が時刻ちょうどで約束したかけ直し（「明日の15時」）の、その日にかける時間：約束の時刻から'
              'この長さの中で、callback.exact_retry_gaps の間をあけてかけ直し、最後の1本はこの長さの終わりにかける（初め30分＝15時・'
              '15時05分・15時15分・15時30分）。(2) お客様指定の歯止め時間の外の時刻（「23時以降なら」）を通話の中で確かめて「はい」と'
              '言われたときの、口にした時刻からかける時間（「23時以降」なら23時から30分）'
WHERE key = 'callback.exact_slot';
UPDATE salescall.setting_definitions
SET meaning = 'お客様が時刻ちょうどで約束したかけ直し（「明日の15時」）に出なかったとき、その日の時間（callback.exact_slot）の中で、'
              '前の電話からあける間（秒）を順に並べたもの。並びのあと、最後の1本は callback.exact_slot の終わりにかける（初め：5分後・'
              'さらに10分後・そして30分の終わり）。2日目・3日目は、その時刻に1本だけ'
WHERE key = 'callback.exact_retry_gaps';
ALTER TABLE salescall.setting_definitions ENABLE TRIGGER setting_definitions_frozen;

ALTER TABLE salescall.setting_values DISABLE TRIGGER setting_values_frozen;
UPDATE salescall.setting_values
SET value = '1800',
    rationale = '2026-10-23 ユーザー：「時刻ちょうどの約束も含めて、30分の方がいいんじゃない」（前は 2026-10-19 ユーザー：「15時から'
                '16時に数回かける」＝1時間）'
WHERE key = 'callback.exact_slot' AND scope = 'system';
UPDATE salescall.setting_values
SET value = '[300, 600]',
    rationale = '2026-10-19 ユーザー：「5分後→さらに10分後→さらに20分後くらいがいいんじゃない？」。2026-10-23 ユーザー：1回分を30分にし、'
                '4本目は30分の終わり（案A＝15:00・15:05・15:15・15:30）'
WHERE key = 'callback.exact_retry_gaps' AND scope = 'system';
ALTER TABLE salescall.setting_values ENABLE TRIGGER setting_values_frozen;
