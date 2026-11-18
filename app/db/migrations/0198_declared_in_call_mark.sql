-- 0198 通話の途中の申告の登録に印を付ける（2026-11-07 第三者の見直し(3)の5。書いた人：CL-2。DESIGN.md 3-6・7-1d・15-2 の524）
--
-- ⛔ 「その通話の途中の申告が台帳にあるか」（core/dnc.refused_in）を、理由 refused_in_call と出どころの通話だけで見ていた。その通話の締めが
--    自分で入れた発信禁止（断りの言葉のあとに切れた・判定が届かなかった断りなど。理由も出どころも同じ）も申告と読み、締め直しで判定の中身の
--    断りの名前が cut・unjudged から in_call に変わり、取り消せない決定の控えの unsure から断りが消えた（2つ目の判定を見ないものと扱った）。
-- ⇒ 申告の入口（core/dnc.register_declared）が入れた登録だけに印 in_call を付ける（書くのは dnc._put の1か所）。読むのは core/dnc.declared_in
--    の1か所。申告の入口が入れるのは、その通話の番号1つ・理由 refused_in_call・出どころの通話つき（core/dnc.stop_set と同じ決まりを守る）。
--    前に入った登録は、どれが申告だったか分からないので、印を付けない（偽＝申告と読まない。締め直しで、断りを2つ目の判定とそろったかで
--    見る側になる＝取り消せない決定を確かめる側）。

ALTER TABLE salescall.dnc_entries ADD COLUMN in_call boolean NOT NULL DEFAULT false;
ALTER TABLE salescall.dnc_entries
  ADD CONSTRAINT dnc_entries_in_call_declared
  CHECK (NOT in_call OR (reason = 'refused_in_call' AND source_attempt_id IS NOT NULL AND phone_e164 IS NOT NULL));
