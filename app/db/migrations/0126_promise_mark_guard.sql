-- 0126 お詫び・すぐのかけ直し・番号の聞き直しの予定には、必ず約束の印を付ける（2026-10-23。書いた人：CL-2）
--
-- 次にかける電話の予定（task_next_actions）の約束の印（promised）は、先に出すか（候補の並べ方）・回線の1日の本数を取り置くか・
-- これからかける約束の一覧・門の間隔の外に置くか、を決める1か所の答え（DESIGN.md 2章 Q38）。お詫びの電話・途中で切れたあとの
-- すぐのかけ直し（続き）・番号の聞き直しの予定は、いまはプログラム（core/next_action）が必ず印を付けて書いている。将来の直しで
-- 付け忘れると、お詫びの電話がふつうの電話の後ろに回され、取り置きもされず、誰も気づかない。⇒ データベースが、その書き込みを断る
-- （2026-10-23 ユーザー：「『約束の印』の守り：OK」）。
--
-- どの目的が印を必ず持つかは salescall.needs_promise_mark の1か所：約束を果たす電話の種類（keeping_kind。お詫び・続き）と、番号の
-- 聞き直し（2026-10-19 ユーザー：お客様に頼まれたかけ直し。停止で通すかの keeping_kind には入れない＝2026-10-22。Q31 と Q38 は別の問い）。
CREATE FUNCTION salescall.needs_promise_mark(p_purpose text) RETURNS boolean LANGUAGE sql IMMUTABLE AS
$$ SELECT salescall.keeping_kind(p_purpose, false) IS NOT NULL OR p_purpose = 'ask_number_again' $$;
GRANT EXECUTE ON FUNCTION salescall.needs_promise_mark(text) TO salescall_app;

-- 予定の表は書き換えられない（task_next_actions_frozen）ので、今ある行は直さない。合わない行が1つでもあれば、この移行は止まる
-- （確かめる＝試験のデータベース・本番に、印の無いお詫び・続き・聞き直しの予定が無いこと）。
ALTER TABLE salescall.task_next_actions
  ADD CONSTRAINT task_next_actions_promise_mark CHECK (NOT salescall.needs_promise_mark(purpose) OR promised);
