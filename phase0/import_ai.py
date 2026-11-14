"""取込のとき AI に1回だけ聞いて決めることを、CL-2 の取込（app の `business_import.preview`・`ask_ai`）の受け口の形で作る1か所。

✅ CL-2 の連絡帳 4-184 の (a)・4-185 の「4-65 (a) の追記」（2026-11-02）：「こちらがすることの言葉を決める関数がまだありません：意味の言葉の
   meanings.by_model／decide と同じ形の関数を作っていただけますか」。⛔ 単位・聞くこと・意味の言葉・曖昧な言葉の意味・事実を伺う質問か、
   も同じく「決め方は CL-1 の1か所」と CL-2 の受け口に書かれているのに、モデル2つに聞いて合わせる所が、どこにも無かった（4-14 では
   取込の側で by_model と decide を呼ぶように頼んだ＝合わせ方を取込の側に書くことになり、決め方が2か所に分かれる）。⇒ 6つとも、ここで作る。
- 決め方そのものは、それぞれの1か所（units.decide・topics.decide・meanings.decide・meanings.decide_replies・facts.decide）。ここは
  「どのモデルに・どの並びで聞き・どう合わせるか」だけ（質問の文の決まりで決まるもの＝units・topics の by_rule は、モデルに聞かない）。
- askers＝[(client, モデルの名前)]（2つ以上）。測った組は 3.8 Flash・3.7 Flash（G-41・G-43・G-48・G-71・G-72）。
  ⛔ 本番では無料の枠を使わない（依頼者のトークスクリプトは依頼者のデータ）＝呼ぶ側が有料のキーの client を渡す。
- 問い合わせが誤りで答えなければ、そのモデルの答えは無いとして決める（決まらない側＝単位・聞くこと・言葉は空、事実を伺う質問か
  は「伺う質問」。会話は止まらない＝4-14）。誤りは on_error(誤り, モデルの名前) に渡す（本番では CL-2 の watch.after_error へ渡す所＝
  CL-2 の連絡帳 4-46 の 1。ここで再試行・キーの切り替えはしない）。
"""
from __future__ import annotations

from typing import Callable

import announces
import facts
import maybes
import meanings
import purposes
import rewords
import since
import target_asks
import topics
import unclear
import units
import ways

Asker = tuple   # (client, モデルの名前)


def _each(askers: list[Asker], ask: Callable, empty, on_error: Callable | None) -> list:
    """モデルごとに1回だけ聞く（誤りなら、そのモデルの答えは empty）。"""
    out = []
    for client, model in askers:
        try:
            out.append(ask(client, model))
        except Exception as exc:   # 答えが無い＝決まらない側に倒す（止めない）
            if on_error is not None:
                on_error(exc, model)
            out.append(empty)
    return out


def decide_units(askers: list[Asker], questions: list[tuple[str, str]], on_error: Callable | None = None) -> dict[str, str]:
    """(業務の目的, 質問の文) の並び → 文ごとの単位（決まらない文は入れない）。⚠️ CL-2 の UnitDecider は文だけを受ける形（目的が無い）。
    G-41 は目的と一緒に聞いて測った＝目的を渡していただく（CL-2 の連絡帳への頼み・帳面 4-69）。"""
    if any(not isinstance(x, (tuple, list)) or len(x) != 2 for x in questions):
        raise TypeError("単位は (業務の目的, 質問の文) の並びで渡す（文だけでは決めない。CL-1 の帳面 4-69）")
    ask = [(p, q) for p, q in questions if not units.by_rule(q)]
    rows = _each(askers, lambda c, m: units.by_model(c, m, ask), [{"unit": ""}] * len(ask), on_error) if ask else []
    at = {q: i for i, (_, q) in enumerate(ask)}
    out = {}
    for _, q in questions:
        u = units.decide(q, [r[at[q]].get("unit", "") for r in rows] if q in at else [])
        if u:
            out[q] = u
    return out


def decide_topics(askers: list[Asker], texts: list[str], on_error: Callable | None = None) -> dict[str, str]:
    """質問の文の並び → 文ごとの聞くこと（決まらない文は入れない）。CL-2 の TopicDecider の形。"""
    ask = [t for t in texts if not topics.by_rule(t)]
    rows = _each(askers, lambda c, m: topics.by_model(c, m, ask), [""] * len(ask), on_error) if ask else []
    at = {t: i for i, t in enumerate(ask)}
    out = {}
    for t in texts:
        got = topics.decide(t, [r[at[t]] for r in rows] if t in at else [])
        if got:
            out[t] = got
    return out


def decide_facts(askers: list[Asker], texts: list[str], on_error: Callable | None = None) -> dict[str, bool]:
    """質問の文の並び → 文ごとの「事実を伺う質問か」（決まらなければ真＝facts.decide）。CL-2 の FactDecider の形。"""
    if not texts:
        return {}
    rows = _each(askers, lambda c, m: facts.by_model(c, m, texts), [None] * len(texts), on_error)
    return {t: facts.decide([r[i] for r in rows]) for i, t in enumerate(texts)}


def _pairs(questions: list[tuple]) -> list[tuple[str, tuple]]:
    return [(q[0], tuple(q[1])) for q in questions]


def decide_meanings(askers: list[Asker], questions: list[tuple[str, tuple]], on_error: Callable | None = None) -> list[dict[str, str]]:
    """(質問の文, 選択肢の並び) の並び → 同じ並びで {選択肢: 意味の言葉}（決まらない選択肢は入れない）。CL-2 の MeaningDecider の形。"""
    pairs = _pairs(questions)
    if not pairs:
        return []
    rows = _each(askers, lambda c, m: meanings.by_model(c, m, pairs, "meaning"), [{}] * len(pairs), on_error)
    return [meanings.decide(t, c, [r[i] for r in rows], "meaning") for i, (t, c) in enumerate(pairs)]


def decide_replies(askers: list[Asker], questions: list[tuple[str, tuple]], on_error: Callable | None = None) -> list[dict[str, str]]:
    """(質問の文, 選択肢の並び) の並び → 同じ並びで {曖昧な言葉: 選択肢}（決まらない言葉は入れない）。CL-2 の ReplyDecider の形。"""
    pairs = _pairs(questions)
    if not pairs:
        return []
    rows = _each(askers, lambda c, m: meanings.by_model_replies(c, m, pairs), [{}] * len(pairs), on_error)
    return [meanings.decide_replies(c, [r[i] for r in rows]) for i, (_, c) in enumerate(pairs)]


def _words_with_meaning(askers: list[Asker], questions: list[tuple[str, tuple, dict]], kind: str, on_error: Callable | None) -> list[dict[str, str]]:
    """選択肢ごとの言葉のうち、意味の言葉と向きをそろえて決める種類（こちらがすること・いまの話の向き）の唯一の決め方：(質問の文, 選択肢の並び,
    その質問の意味の言葉 {選択肢: 意味の言葉}) の並び → 同じ並びで {選択肢: 言葉}（決まらない選択肢は入れない）。言葉の向き（打ち消しか）が意味の言葉の向きと
    そろうときだけ決まる（meanings.decide の meanings。G-72＝2つのモデルがそろって向きを取り違えた）。事実を伺う質問を入れないのは呼ぶ側（CL-2 の取込）。"""
    pairs = _pairs(questions)
    if not pairs:
        return []
    rows = _each(askers, lambda c, m: meanings.by_model(c, m, pairs, kind), [{}] * len(pairs), on_error)
    return [meanings.decide(t, c, [r[i] for r in rows], kind, dict(questions[i][2] or {}) or None) for i, (t, c) in enumerate(pairs)]


def decide_do_words(askers: list[Asker], questions: list[tuple[str, tuple, dict]], on_error: Callable | None = None) -> list[dict[str, str]]:
    """(質問の文, 選択肢の並び, その質問の意味の言葉) の並び → 同じ並びで {選択肢: こちらがすることの言葉}（Question.do_words）。CL-2 の DoWordsDecider の形。"""
    return _words_with_meaning(askers, questions, "do", on_error)


def decide_doubt_words(askers: list[Asker], questions: list[tuple[str, tuple, dict]], on_error: Callable | None = None) -> list[dict[str, str]]:
    """(質問の文, 選択肢の並び, その質問の意味の言葉) の並び → 同じ並びで {選択肢: いまの話の向きを伺う言葉}（Question.doubt_words。「（言葉）ということでしょうか？」。
    ✅ 2026-11-01 日本時間22時31分のご判断＝meanings.DOUBT_PROMPT）。decide_do_words と同じ形（CL-2 の連絡帳 4-199 の (1)。帳面 4-80・4-90）。
    決まらない選択肢は、会話の側が意味の言葉で「念のため（意味の言葉）でよろしかったでしょうか？」と確かめる（今までの形＝安全側）。"""
    return _words_with_meaning(askers, questions, "doubt", on_error)


def decide_away(askers: list[Asker], questions: list[tuple[str, tuple, dict]], on_error: Callable | None = None) -> list[tuple]:
    """(質問の文, 選択肢の並び, その質問の意味の言葉 {選択肢: 意味の言葉}) の並び → 同じ並びで、目的から遠ざかる向きの選択肢の並び（どちらか1つの
    モデルでも「遠ざかる」と読むか、読めない選択肢＝安全側＝meanings.decide_away）。モデルには、選択肢ごとに意味の言葉を添えて読ませる（meanings._body。
    ⛔ 2026-11-03 G-72：名前だけで、打ち消しの問い「当日のキャンセルはございませんか？」の「はい」を遠ざかる向きと読んだ）。事実を伺う質問を入れない
    のは呼ぶ側（CL-2 の取込）。✅ 2026-11-03 日本時間8時10分 ユーザー：「今の一言や案は全くダメです。…いい加減な提案をするな！！」＝目的につなげて
    伺う言葉（decide_keep_words）は取り下げ（帳面 4-72）。"""
    items = [(q[0], tuple(q[1]), dict(q[2] or {}) if len(q) > 2 else {}) for q in questions]
    if not items:
        return []
    # 答えないモデル（誤り）は、どの選択肢も読めない＝遠ざかる向き（1つのモデルだけで「遠ざからない」と決めない）
    rows = _each(askers, lambda c, m: meanings.by_model(c, m, items, "away"), [{}] * len(items), on_error)
    return [meanings.decide_away(c, [r[i] for r in rows]) for i, (_, c, _) in enumerate(items)]


def decide_target_asks(askers: list[Asker], texts: list[str], on_error: Callable | None = None) -> dict[str, bool]:
    """業務の質問の文の並び → 文ごとの「お呼びする方がいらっしゃるかを伺う一文か」（target_asks.decide＝2つのモデルがそろって真のときだけ真。そろわない・答えが
    無いときは偽＝依頼者が書いたとおり業務の質問のまま。取込で止めない）。真の文は、取込が業務の質問にせず目的のお客様にする（CL-2 の連絡帳 4-203 の (1)）。
    facts.decide と同じ形（文の並び → 真偽）。"""
    if not texts:
        return {}
    rows = _each(askers, lambda c, m: target_asks.by_model(c, m, texts), [None] * len(texts), on_error)
    return {t: target_asks.decide([r[i] for r in rows]) for i, t in enumerate(texts)}


def decide_unclear(askers: list[Asker], questions: list[tuple[str, tuple]], on_error: Callable | None = None) -> list[str]:
    """(質問の文, 選択肢の並び) の並び → 同じ並びで、ほかに分からない点が無いかを伺う質問の「無い」にあたる選択肢の名前（伺う質問でない・決まらないなら空＝
    unclear.decide）。業務の選ぶ質問（はい／いいえ・選択肢）だけを渡すのは呼ぶ側（CL-2 の取込。値を伺う質問は入れない）。答えは Question.unclear_none。
    ✅ 2026-11-04 日本時間20時27分 ユーザー（お言葉は unclear.py の頭）"""
    items = [(q[0], tuple(q[1])) for q in questions]
    if not items:
        return []
    rows = _each(askers, lambda c, m: unclear.by_model(c, m, items), [None] * len(items), on_error)
    return [unclear.decide(c, [r[i] for r in rows]) for i, (_, c) in enumerate(items)]


def decide_ways(askers: list[Asker], texts: list[str], on_error: Callable | None = None) -> dict[str, bool]:
    """業務の質問の文の並び → 文ごとの「ご案内の方法でご案内してよいかを伺う質問か」（ways.decide＝2つのモデルがそろって真のときだけ真。そろわない・答えが無い
    ときは偽＝今までどおり「ほかにご希望などは…」）。答えは Question.asks_way（はい／いいえで伺う最終目的の質問だけに効く＝flowctl._wish_text）。
    facts.decide と同じ形（文の並び → 真偽）。✅ 2026-11-05 日本時間23時19分06秒 ユーザー「案でOK　(2)ア」（お言葉は ways.py の頭）"""
    if not texts:
        return {}
    rows = _each(askers, lambda c, m: ways.by_model(c, m, texts), [None] * len(texts), on_error)
    return {t: ways.decide([r[i] for r in rows]) for i, t in enumerate(texts)}


def decide_maybes(askers: list[Asker], questions: list[tuple[str, tuple]], on_error: Callable | None = None) -> list[tuple]:
    """(質問の文, 選択肢の並び) の並び → 同じ並びで、お客様が「どちらの場合もありうる」と言ったときに確かめる (選択肢, 言葉)（当たらない・決まらないなら空＝
    maybes.decide）。業務の選ぶ質問（はい／いいえ・選択肢）だけを渡すのは呼ぶ側（CL-2 の取込。値を伺う質問は入れない）。答えは Question.maybe。
    decide_unclear と同じ形。⛔✅ 2026-11-09 日本時間4時36分49秒 ユーザー（お言葉は maybes.py の頭）"""
    items = [(q[0], tuple(q[1])) for q in questions]
    if not items:
        return []
    rows = _each(askers, lambda c, m: maybes.by_model(c, m, items), [None] * len(items), on_error)
    return [maybes.decide(c, [r[i] for r in rows]) for i, (_, c) in enumerate(items)]


# ⚠️ decide_away・decide_unclear は、CL-2 の取込の受け口（business_import.preview）が受けるようになったら、ここに足す（比べる試験 test_shapes_match_cl2_import が
# 名前のずれを見る。いまの CL-2 の受け口は取り下げた decide_keep_words＝帳面 4-72 で替えていただくよう頼んだ）
# 決める処理の唯一の一覧（CL-2 の取込 business_import.preview の decide_* の受け口と同じ名前＝試験で比べる）。decide_away・decide_unclear は、CL-2 の受け口が
# できた（CL-2 の連絡帳 4-212 の (A)。⛔ それまで、この一覧に無く、取込から呼ばれなかった）
def decide_way_words(askers: list[Asker], pairs: list[tuple[str, tuple]], on_error: Callable | None = None) -> list[dict[str, str]]:
    """(こちらがすることの言葉, 方法の並び) の並び（業務設定から並べる1か所＝flowctl.way_word_pairs）→ 同じ並びで {方法: その方法を入れた言葉}（2つのモデルの
    言葉がどちらも確かめに通るときだけ、最初のモデルの言葉＝meanings.decide の way）。答えは業務設定 way_words（{言葉: {方法: 言葉}}＝instruction.Business）。
    決まらない方法は入れない＝会話の側は今の形（「（方法）で」を終わりの動詞の前に）。decide_unclear と同じ形。✅ 2026-11-12 まとめ役の (1)"""
    items = [(d, tuple(w)) for d, w in pairs]
    if not items:
        return []
    rows = _each(askers, lambda c, m: meanings.by_model(c, m, items, "way"), [{}] * len(items), on_error)
    return [meanings.decide(d, w, [r[i] for r in rows], "way") for i, (d, w) in enumerate(items)]


def _settled(askers: list[Asker], texts: list[str], by_model: Callable, decide: Callable, fits: Callable, on_error: Callable | None,
             referee: Asker | None) -> dict[str, str]:
    """文ごとの言葉を決める唯一の形（用件の名詞 decide_purposes・その後の文の中身 decide_since）：2つの文字のAIがそろって形に合えばその言葉・そろって空なら空
    （decide が None を返さない）。そろわない文だけを、まとめて1回、考える Sonnet（referee＝(client, モデルの名前)。CL-2 の取込が deciders に渡す）に聞き、
    形に合えば（fits）その言葉。referee が無い・答えない・形に合わなければ空。✅ 帳面 0-2「そろわないときは、考える Sonnet で決め」"""
    if not texts:
        return {}
    rows = _each(askers, lambda c, m: by_model(c, m, texts), [None] * len(texts), on_error)
    out = {t: decide(t, [r[i] for r in rows]) for i, t in enumerate(texts)}
    left = [t for t in texts if out[t] is None]
    if left and referee is not None:
        got = _each([referee], lambda c, m: by_model(c, m, left, think=True), [None] * len(left), on_error)[0]
        out.update({t: w for t, w in zip(left, got) if fits(t, w)})
    return {t: w or "" for t, w in out.items()}


def decide_purposes(askers: list[Asker], texts: list[str], on_error: Callable | None = None, referee: Asker | None = None) -> dict[str, str]:
    """用件の一文の並び → 文ごとの用件の名詞（purposes.decide＝2つのモデルがそろって、その文の中の言葉のとき。そろわない文は考える Sonnet＝_settled の1か所。
    決まらなければ空＝続きの電話でも今の用件の一文のまま・決まった伝言は「…からお電話があったと」）。答えは instruction.Business.purpose_noun（CL-2 の取込が
    業務設定へ）。✅ 2026-11-09 日本時間9時48分09秒の (4)・考える Sonnet は分身 D の (c)（帳面 0-2）"""
    return _settled(askers, texts, purposes.by_model, purposes.decide, purposes.fits, on_error, referee)


def decide_since(askers: list[Asker], texts: list[str], on_error: Callable | None = None, referee: Asker | None = None) -> dict[str, str]:
    """業務の質問の文の並び（CL-2 がかけ直す「わからない」の質問＝flowctl.Question.unknown_called_back の質問だけを渡すのは呼ぶ側）→ 文ごとの、その後の文
    「その後（中身）かお決まりになりましたでしょうか？」の中身の言葉（答えは flowctl.Question.since）。決め方は since.decide と _settled の1か所（そろわない文は
    考える Sonnet・形に合わなければ空＝今の形で伺い直す）。✅ 2026-11-13 日本時間20時06分19秒 ユーザー「伺い6は…」"""
    return _settled(askers, texts, since.by_model, lambda t, words: since.decide(words), lambda t, w: since.fits(w), on_error, referee)


def _drafted(askers: list[Asker], items: list[tuple], by_model: Callable, decide: Callable, fits: Callable, grounded: Callable,
             on_error: Callable | None, referee: Asker | None) -> list[str]:
    """取込で文を作る唯一の形（お知らせの文 decide_announces・言い換えた質問 decide_rewords）：2つの文字のAIがそろって形に合う（fits）文、そろわない文は
    考える Sonnet（referee）にまとめて1回書かせて形に合う文。どちらも、考える Sonnet の中身の判定（grounded＝askitems.grounded の grounding の問い）を通ったとき
    だけ使う。referee が無ければ空（中身を確かめられない文は使わない）。items は文ごとの材料の組（decide・fits・grounded にそのまま渡す）。"""
    if not items:
        return []
    rows = _each(askers, lambda c, m: by_model(c, m, items), [None] * len(items), on_error)
    got = [decide(*it, [r[n] for r in rows]) for n, it in enumerate(items)]
    if referee is None:
        return [""] * len(items)
    left = [n for n, line in enumerate(got) if line is None]
    if left:
        drafts = _each([referee], lambda c, m: by_model(c, m, [items[n] for n in left], think=True), [None] * len(left), on_error)[0]
        for n, line in zip(left, drafts):
            got[n] = line if fits(*items[n], line) else None
    out = []
    for it, line in zip(items, got):
        ok = line is not None and _each([referee], lambda c, m: grounded(c, m, *it, line), False, on_error)[0]
        out.append(line if ok else "")
    return out


def decide_announces(askers: list[Asker], pairs: list[tuple[str, str]], on_error: Callable | None = None, referee: Asker | None = None) -> list[str]:
    """ご承認を伺う質問の (説明＝入る一言, 質問の文) の並び（flowctl.Question.approval の質問だけを渡すのは呼ぶ側）→ 同じ並びで、すらっと伝えるお知らせの文
    （「（説明）…のでご了承ください。」。答えは flowctl.Question.announce。決まらなければ空＝今の形）。決め方は announces の1か所：2つの文字のAIがそろって形に合う
    （announces.fits）文、そろわない文は考える Sonnet（referee）にまとめて1回聞いて形に合う文。どちらも、考える Sonnet の中身の判定（announces.grounded＝grounding の
    問い）を通ったときだけ使う。referee が無ければ空（中身を確かめられない文は使わない）。✅ 2026-11-13 日本時間20時06分19秒 ユーザー「伺い1の(1)はウ…」"""
    pairs = [(str(i or ""), str(t or "")) for i, t in pairs]
    return _drafted(askers, pairs, announces.by_model, announces.decide, announces.fits, announces.grounded, on_error, referee)


def decide_matters(askers: list[Asker], pairs: list[tuple[str, str]], on_error: Callable | None = None, referee: Asker | None = None) -> list[str]:
    """ご承認を伺う質問の (説明＝入る一言, 質問の文) の並び（decide_announces と同じ質問だけを渡すのは呼ぶ側）→ 同じ並びで、お知らせの中身を表す名詞（「キャンセル」。
    答えは flowctl.Question.matter。最終の確かめ「それでは（名詞）の件はご了承ということでよろしかったでしょうか？」＝flowctl.accepted_line）。決め方は用件の名詞と
    同じ1か所（_settled：2つの文字のAIがそろって説明と質問の文の中の言葉・そろわない文は考える Sonnet＝referee・文に無い言葉は使わない＝announces.matter_fits）。
    決まらなければ空＝「先ほどの内容」。✅ 2026-11-14 日本時間14時31分38秒 ユーザー「●伺い10 「はい、それでは、キャンセルの件はご了承ということでよろしかった
    でしょうか？」」"""
    pairs = [(str(i or ""), str(t or "")) for i, t in pairs]
    src = {f"{i}／{t}": (i, t) for i, t in pairs}   # 文ごとに決める1か所（_settled）の鍵＝説明と質問の文
    got = _settled(askers, list(src), lambda c, m, keys, think=False: announces.matter_by_model(c, m, [src[k] for k in keys], think),
                   lambda k, words: announces.matter_decide(*src[k], words), lambda k, w: announces.matter_fits(*src[k], w), on_error, referee)
    return [got[f"{i}／{t}"] for i, t in pairs]


def decide_rewords(askers: list[Asker], items: list[tuple[str, tuple, str]], on_error: Callable | None = None, referee: Asker | None = None) -> list[str]:
    """申し出でない業務の質問の (質問の文, 選択肢の並び, 型) の並び（flowctl.aside_fits の質問だけを渡すのは呼ぶ側）→ 同じ並びで、会話が繋がらないお返事のあとに
    「ところで」に続けて伺い直す、言い換えた質問の文（答えは flowctl.Question.reworded。決まらなければ空＝「ところで」＋元の質問の文）。決め方はお知らせの文と
    同じ1か所（_drafted）・形は rewords.fits・中身の判定は rewords.grounded。✅ 2026-11-13 日本時間22時17分01秒 ユーザー「9は…」"""
    items = [(str(t or ""), tuple(c), str(k or "")) for t, c, k in items]
    return _drafted(askers, items, rewords.by_model, rewords.decide, rewords.fits, rewords.grounded, on_error, referee)


def decide_at_hand(askers: list[Asker], questions: list[tuple[str, tuple]], on_error: Callable | None = None) -> list[str]:
    """(質問の文, 選択肢の並び) の並び → 同じ並びで、資料がお手元に届いているかを伺う質問の「届いている」にあたる選択肢の名前（伺う質問でない・決まらないなら空＝
    announces.decide_hand。unclear と同じ形）。業務の選ぶ質問だけを渡すのは呼ぶ側。答えは flowctl.Question.at_hand（お知らせの「ご確認ください。」の文の条件）。
    ✅ 2026-11-13 日本時間20時06分19秒 ユーザー「(3)b」"""
    items = [(q[0], tuple(q[1])) for q in questions]
    if not items:
        return []
    rows = _each(askers, lambda c, m: announces.hand_by_model(c, m, items), [None] * len(items), on_error)
    return [announces.decide_hand(c, [r[i] for r in rows]) for i, (_, c) in enumerate(items)]


DECIDERS = {"decide_units": decide_units, "decide_topics": decide_topics, "decide_meanings": decide_meanings,
            "decide_replies": decide_replies, "decide_facts": decide_facts, "decide_do_words": decide_do_words,
            "decide_away": decide_away, "decide_unclear": decide_unclear,
            # いまの話の向きを伺う言葉・お呼びする方がいらっしゃるかの一文（CL-2 の連絡帳 4-199 の (1)・4-203 の (1)。⏳ CL-2 の受け口ができるまで CL2_PENDING）
            "decide_doubt_words": decide_doubt_words, "decide_target_asks": decide_target_asks,
            # ご案内の方法を伺う質問か（✅ 2026-11-05 日本時間23時19分06秒「案でOK　(2)ア」。⏳ CL-2 の受け口ができるまで CL2_PENDING＝帳面 4章で頼む）
            "decide_ways": decide_ways,
            # どちらの場合もありうると言われたときに確かめる答えと言葉（⛔✅ 2026-11-09 日本時間4時36分49秒。⏳ CL-2 の受け口ができるまで CL2_PENDING＝帳面 4-123）
            "decide_maybes": decide_maybes,
            # 用件の一文の中の用件の名詞（続きの電話の用件の一文を前の電話とつなげる＝✅ 11-09 日本時間9時48分09秒の (4)。⏳ CL-2 の受け口ができるまで CL2_PENDING＝帳面 4-128）
            "decide_purposes": decide_purposes,
            # 方法を申し出の中身に入れる言い方（✅ 2026-11-12 まとめ役の (1)。⏳ CL-2 の受け口ができるまで CL2_PENDING＝帳面 4-132）
            "decide_way_words": decide_way_words,
            # その後の文の中身（✅ 2026-11-13 日本時間20時06分19秒「伺い6は…」。⏳ CL-2 の受け口ができるまで CL2_PENDING＝帳面 4-149）。考える Sonnet は referee
            "decide_since": decide_since,
            # ご承認のお知らせの文と、資料が届いているかを伺う質問（✅ 2026-11-13 日本時間20時06分19秒「伺い1…」。⏳ CL-2 の受け口ができるまで CL2_PENDING＝帳面 4-150）。
            # お知らせの文は考える Sonnet が referee（書く・中身の判定）
            "decide_announces": decide_announces, "decide_at_hand": decide_at_hand,
            # お知らせの中身の名詞（✅ 2026-11-14 日本時間14時31分38秒「●伺い10」。⏳ CL-2 の受け口ができるまで CL2_PENDING＝帳面 4-154）。考える Sonnet は referee
            "decide_matters": decide_matters,
            # 会話が繋がらないお返事のあとに「ところで」に続けて伺い直す、言い換えた質問（✅ 2026-11-13 日本時間22時17分01秒「9は…」。⏳ CL-2 の受け口ができるまで
            # CL2_PENDING＝帳面 4-152）。考える Sonnet は referee（書く・中身の判定）
            "decide_rewords": decide_rewords}
# 2つの文字のAIがそろわない文を、考える Sonnet（deciders の referee）に聞く決める処理（唯一の一覧）
REFEREED = frozenset({"decide_since", "decide_announces", "decide_purposes", "decide_rewords", "decide_matters"})


def deciders(askers: list[Asker], on_error: Callable | None = None, referee: Asker | None = None) -> dict[str, Callable]:
    """CL-2 の取込（business_import.preview・ask_ai）に渡す決める処理（引数の名前 → 処理）。askers は2つ以上（2つのモデルが合うときだけ
    決める＝1つでは何も決まらない）。referee＝そろわない文だけを聞く考える Sonnet（(client, モデルの名前)。REFEREED の処理だけが使う。無ければ空のまま）。"""
    assert len(askers) >= 2, "取込で決めることは、2つ以上のモデルが合うときだけ決める"
    return {name: ((lambda qs, fn=fn: fn(askers, qs, on_error, referee)) if name in REFEREED else (lambda qs, fn=fn: fn(askers, qs, on_error)))
            for name, fn in DECIDERS.items()}
