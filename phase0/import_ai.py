"""取込のとき AI に1回だけ聞いて決めることを、CL-2 の取込（app の `business_import.preview`・`ask_ai`）の受け口の形で作る1か所。

✅ CL-2 の連絡帳 4-184 の (a)・4-185 の「4-65 (a) の追記」（2026-11-02）：「こちらがすることの言葉を決める関数がまだありません：意味の言葉の
   meanings.by_model／decide と同じ形の関数を作っていただけますか」。⛔ 単位・聞くこと・意味の言葉・曖昧な言葉の意味・事実を伺う質問か、
   も同じく「決め方は CL-1 の1か所」と CL-2 の受け口に書かれているのに、モデル2つに聞いて合わせる所が、どこにも無かった（4-14 では
   取込の側で by_model と decide を呼ぶように頼んだ＝合わせ方を取込の側に書くことになり、決め方が2か所に分かれる）。⇒ 6つとも、ここで作る。
- 決め方そのものは、それぞれの1か所（units.decide・topics.decide・meanings.decide・meanings.decide_replies・facts.decide）。ここは
  「どのモデルに・どの並びで聞き・どう合わせるか」だけ（質問の文の決まりで決まるもの＝units・topics の by_rule は、モデルに聞かない）。
- askers＝[(client, モデルの名前)]（2つ以上）。本番で聞く組の名前は ASKERS・考える判定は REFEREE（下の1か所）。
  ⛔ 本番では無料の枠を使わない（依頼者のトークスクリプトは依頼者のデータ）＝呼ぶ側が有料のキーの client を渡す。
- どの決め方も同じ1か所 _decide_each を通る。2つの文字のAIがそろわない材料の扱いは、決め方ごとの表 SPLIT の1か所（考える Sonnet で決める／書いてある既定と
  そのよりどころ）。考える Sonnet（referee）は、呼ぶ側（deciders）が1か所から全部の決め方に渡す。
- 問い合わせが誤りで答えなければ、そのモデルの答えは無いとして決める（決まらない側＝単位・聞くこと・言葉は空、事実を伺う質問か
  は「伺う質問」。会話は止まらない＝4-14）。誤りは on_error(誤り, モデルの名前) に渡す（本番では CL-2 の watch.after_error へ渡す所＝
  CL-2 の連絡帳 4-46 の 1。ここで再試行・キーの切り替えはしない）。
"""
from __future__ import annotations

from typing import Callable

import alternatives
import announces
import facts
import maybes
import meanings
import purposes
import rewords
import since
import summaries
import target_asks
import topics
import unclear
import units
import ways

Asker = tuple   # (client, モデルの名前)

# 本番の取込で聞く文字のAI 2つ（測りの表 g23_names.MODELS の表の名前。つなぐ所は、作り手の一覧でこの名前のモデルを引いて askers の client と
# モデルの名前にする）と、そろわない文を聞く考える判定（meaning_judge.OTHER_JUDGES の名前＝referee）。名前を持つのはここ1か所（新しい版の確かめ
# latest_ai.in_use もここを読む＝CL-2 の連絡帳 4-400 の (c)・CL-1.md 4-163 の (c)）。測った組（G-41・G-43・G-48・G-71・G-72）のまま。
# ⚠️ 3.7 Flash を 3.8 Flash の次の版へ替えるかは、ユーザーに伺う前（替えるときは、ここと測りを一緒に直す）
ASKERS = ("3.8 Flash", "3.7 Flash")
REFEREE = "Claude Sonnet 5.5（考える）"


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


def decide_units(askers: list[Asker], questions: list[tuple[str, str]], on_error: Callable | None = None, referee: Asker | None = None) -> dict[str, str]:
    """(業務の目的, 質問の文) の並び → 文ごとの単位（決まらない文は入れない）。⚠️ CL-2 の UnitDecider は文だけを受ける形（目的が無い）。
    G-41 は目的と一緒に聞いて測った＝目的を渡していただく（CL-2 の連絡帳への頼み・帳面 4-69）。質問の文の数える言葉で決まる文（units.by_rule）はモデルに
    聞かない。ほかは units.decide と _decide_each の1か所（そろわない文は考える Sonnet）。"""
    if any(not isinstance(x, (tuple, list)) or len(x) != 2 for x in questions):
        raise TypeError("単位は (業務の目的, 質問の文) の並びで渡す（文だけでは決めない。CL-1 の帳面 4-69）")
    ask = [(p, q) for p, q in questions if not units.by_rule(q)]
    got = dict(zip((q for _, q in ask), _decide_each("decide_units", askers, ask,
                                                     lambda c, m, xs, think=False: [r.get("unit", "") for r in units.by_model(c, m, xs, think=think)],
                                                     lambda p, q, us: units.decide(q, [u or "" for u in us]), on_error, referee)))
    out = {}
    for _, q in questions:
        u = got[q] if q in got else units.decide(q, [])
        if u:
            out[q] = u
    return out


def decide_topics(askers: list[Asker], texts: list[str], on_error: Callable | None = None, referee: Asker | None = None) -> dict[str, str]:
    """質問の文の並び → 文ごとの聞くこと（決まらない文は入れない）。CL-2 の TopicDecider の形。質問の文の決まりで決まる文（topics.by_rule）はモデルに聞かない。
    ほかは用件の名詞などと同じ1か所（_settled）：2つの文字のAIがそろって形に合えばその言葉（そろって空なら空）・そろわない文だけを、まとめて1回、考える
    Sonnet（referee）に聞き、形に合えば（topics.fits）その言葉。referee が無い・答えない・形に合わなければ決まらない（空＝聞き返しは質問の文から作る＝
    flowctl.ask_again_text）。✅ 帳面 0-2「そろわないときは、考える Sonnet で決め」・2026-11-09 日本時間4時36分49秒の残り「聞くことも取込で決める＝
    そろわないときに空にしない」。⛔ 58fb6c7f まで、そろわなければ考える Sonnet に聞かずに空にしていた"""
    ruled = {t: r for t in texts if (r := topics.by_rule(t))}
    got = _settled("decide_topics", askers, [t for t in texts if t not in ruled], topics.by_model, topics.models_decide, on_error, referee)
    return {t: w for t in texts if (w := ruled.get(t) or got.get(t))}


def decide_facts(askers: list[Asker], texts: list[str], on_error: Callable | None = None, referee: Asker | None = None) -> dict[str, bool]:
    """質問の文の並び → 文ごとの「事実を伺う質問か」（決まらなければ真＝facts.decide。書いてある既定＝SPLIT）。CL-2 の FactDecider の形。"""
    return dict(zip(texts, _decide_each("decide_facts", askers, texts, facts.by_model, lambda t, fs: facts.decide(fs), on_error, referee)))


def _pairs(questions: list[tuple]) -> list[tuple[str, tuple]]:
    return [(q[0], tuple(q[1])) for q in questions]


def decide_meanings(askers: list[Asker], questions: list[tuple[str, tuple]], on_error: Callable | None = None,
                    referee: Asker | None = None) -> list[dict[str, str]]:
    """(質問の文, 選択肢の並び) の並び → 同じ並びで {選択肢: 意味の言葉}（決まらない選択肢は入れない）。CL-2 の MeaningDecider の形。そろわない選択肢は
    考える Sonnet（_decide_each）。"""
    return _decide_each("decide_meanings", askers, _pairs(questions),
                        lambda c, m, xs, think=False: meanings.by_model(c, m, xs, "meaning", think=think),
                        lambda t, c, ws: meanings.decide(t, c, [w or {} for w in ws], "meaning"), on_error, referee)


def decide_replies(askers: list[Asker], questions: list[tuple[str, tuple]], on_error: Callable | None = None,
                   referee: Asker | None = None) -> list[dict[str, str]]:
    """(質問の文, 選択肢の並び) の並び → 同じ並びで {曖昧な言葉: 選択肢}（決まらない言葉は入れない）。CL-2 の ReplyDecider の形。そろわない言葉は
    考える Sonnet（_decide_each）。"""
    return _decide_each("decide_replies", askers, _pairs(questions), meanings.by_model_replies,
                        lambda t, c, ws: meanings.decide_replies(c, [w or {} for w in ws]), on_error, referee)


def _words_with_meaning(name: str, askers: list[Asker], questions: list[tuple[str, tuple, dict]], kind: str, on_error: Callable | None,
                        referee: Asker | None) -> list[dict[str, str]]:
    """選択肢ごとの言葉のうち、意味の言葉と向きをそろえて決める種類（こちらがすること・いまの話の向き）の唯一の決め方：(質問の文, 選択肢の並び,
    その質問の意味の言葉 {選択肢: 意味の言葉}) の並び → 同じ並びで {選択肢: 言葉}（決まらない選択肢は入れない）。言葉の向き（打ち消しか）が意味の言葉の向きと
    そろうときだけ決まる（meanings.decide の meanings。G-72＝2つのモデルがそろって向きを取り違えた）。モデルには質問の文と選択肢だけを見せる（意味の言葉は
    決めるときに比べるだけ）。そろわない選択肢は考える Sonnet（_decide_each。その言葉も意味の言葉と向きがそろうときだけ）。事実を伺う質問を入れないのは
    呼ぶ側（CL-2 の取込）。"""
    items = [(q[0], tuple(q[1]), dict(q[2] or {})) for q in questions]
    return _decide_each(name, askers, items, lambda c, m, xs, think=False: meanings.by_model(c, m, [x[:2] for x in xs], kind, think=think),
                        lambda t, c, mn, ws: meanings.decide(t, c, [w or {} for w in ws], kind, mn or None), on_error, referee)


def decide_do_words(askers: list[Asker], questions: list[tuple[str, tuple, dict]], on_error: Callable | None = None,
                    referee: Asker | None = None) -> list[dict[str, str]]:
    """(質問の文, 選択肢の並び, その質問の意味の言葉) の並び → 同じ並びで {選択肢: こちらがすることの言葉}（Question.do_words・締める前の申し出の
    CloseOffer.do＝flowctl.close_offer_do_question の「はい」）。CL-2 の DoWordsDecider の形。✅ 帳面 4-125 の ⏳「そろわないときに考える Sonnet で決める形は、
    取込のほかの決め方と一緒に入れます」"""
    return _words_with_meaning("decide_do_words", askers, questions, "do", on_error, referee)


def decide_doubt_words(askers: list[Asker], questions: list[tuple[str, tuple, dict]], on_error: Callable | None = None,
                       referee: Asker | None = None) -> list[dict[str, str]]:
    """(質問の文, 選択肢の並び, その質問の意味の言葉) の並び → 同じ並びで {選択肢: いまの話の向きを伺う言葉}（Question.doubt_words。「（言葉）ということでしょうか？」。
    ✅ 2026-11-01 日本時間22時31分のご判断＝meanings.DOUBT_PROMPT）。decide_do_words と同じ形（CL-2 の連絡帳 4-199 の (1)。帳面 4-80・4-90）。
    決まらない選択肢は、会話の側が意味の言葉で「念のため（意味の言葉）でよろしかったでしょうか？」と確かめる（今までの形＝安全側）。"""
    return _words_with_meaning("decide_doubt_words", askers, questions, "doubt", on_error, referee)


def decide_away(askers: list[Asker], questions: list[tuple[str, tuple, dict]], on_error: Callable | None = None,
                referee: Asker | None = None) -> list[tuple]:
    """(質問の文, 選択肢の並び, その質問の意味の言葉 {選択肢: 意味の言葉}) の並び → 同じ並びで、目的から遠ざかる向きの選択肢の並び（どちらか1つの
    モデルでも「遠ざかる」と読むか、読めない選択肢＝安全側＝meanings.decide_away。書いてある既定＝SPLIT）。モデルには、選択肢ごとに意味の言葉を添えて読ませる
    （meanings._body。⛔ 2026-11-03 G-72：名前だけで、打ち消しの問い「当日のキャンセルはございませんか？」の「はい」を遠ざかる向きと読んだ）。事実を伺う質問を
    入れないのは呼ぶ側（CL-2 の取込）。✅ 2026-11-03 日本時間8時10分 ユーザー：「今の一言や案は全くダメです。…いい加減な提案をするな！！」＝目的につなげて
    伺う言葉（decide_keep_words）は取り下げ（帳面 4-72）。"""
    items = [(q[0], tuple(q[1]), dict(q[2] or {}) if len(q) > 2 else {}) for q in questions]
    # 答えないモデル（誤り）は、どの選択肢も読めない＝遠ざかる向き（1つのモデルだけで「遠ざからない」と決めない）
    return _decide_each("decide_away", askers, items, lambda c, m, xs, think=False: meanings.by_model(c, m, xs, "away", think=think),
                        lambda t, c, mn, ws: meanings.decide_away(c, [w or {} for w in ws]), on_error, referee)


def decide_target_asks(askers: list[Asker], texts: list[str], on_error: Callable | None = None, referee: Asker | None = None) -> dict[str, bool]:
    """業務の質問の文の並び → 文ごとの「お呼びする方がいらっしゃるかを伺う一文か」（target_asks.decide＝2つのモデルがそろって真のときだけ真。そろわない・答えが
    無いときは偽＝依頼者が書いたとおり業務の質問のまま。取込で止めない。書いてある既定＝SPLIT）。真の文は、取込が業務の質問にせず目的のお客様にする（CL-2 の連絡帳
    4-203 の (1)）。facts.decide と同じ形（文の並び → 真偽）。"""
    return dict(zip(texts, _decide_each("decide_target_asks", askers, texts, target_asks.by_model, lambda t, cs: target_asks.decide(cs), on_error, referee)))


def decide_unclear(askers: list[Asker], questions: list[tuple[str, tuple]], on_error: Callable | None = None, referee: Asker | None = None) -> list[str]:
    """(質問の文, 選択肢の並び) の並び → 同じ並びで、ほかに分からない点が無いかを伺う質問の「無い」にあたる選択肢の名前（伺う質問でない・決まらないなら空＝
    unclear.decide。そろわない質問は考える Sonnet＝_decide_each）。業務の選ぶ質問（はい／いいえ・選択肢）だけを渡すのは呼ぶ側（CL-2 の取込。値を伺う質問は
    入れない）。答えは Question.unclear_none（印があれば「ほかに」で伺う＝本書 9-5 の ✅ 11-07 日本時間4時40分35秒「ここで「続きまして」はおかしいでしょ」）。
    ✅ 2026-11-04 日本時間20時27分 ユーザー（お言葉は unclear.py の頭）"""
    return _decide_each("decide_unclear", askers, _pairs(questions), unclear.by_model, lambda t, c, rows: unclear.decide(c, rows), on_error, referee)


def decide_ways(askers: list[Asker], texts: list[str], on_error: Callable | None = None, referee: Asker | None = None) -> dict[str, bool]:
    """業務の質問の文の並び → 文ごとの「ご案内の方法でご案内してよいかを伺う質問か」（ways.decide＝2つのモデルがそろって真のときだけ真。そろわない文は考える
    Sonnet＝_decide_each・それでも決まらなければ偽＝今までどおり「ほかにご希望などは…」）。答えは Question.asks_way（はい／いいえで伺う最終目的の質問だけに
    効く＝flowctl._wish_text）。facts.decide と同じ形（文の並び → 真偽）。✅ 2026-11-05 日本時間23時19分06秒 ユーザー「案でOK　(2)ア」（お言葉は ways.py の頭）"""
    return dict(zip(texts, _decide_each("decide_ways", askers, texts, ways.by_model, lambda t, ws: ways.decide(ws), on_error, referee)))


def decide_maybes(askers: list[Asker], questions: list[tuple[str, tuple]], on_error: Callable | None = None, referee: Asker | None = None) -> list[tuple]:
    """(質問の文, 選択肢の並び) の並び → 同じ並びで、お客様が「どちらの場合もありうる」と言ったときに確かめる (選択肢, 言葉)（当たらない・決まらないなら空＝
    maybes.decide。そろわない質問は考える Sonnet＝_decide_each）。業務の選ぶ質問（はい／いいえ・選択肢）だけを渡すのは呼ぶ側（CL-2 の取込。値を伺う質問は
    入れない）。答えは Question.maybe。decide_unclear と同じ形。⛔✅ 2026-11-09 日本時間4時36分49秒 ユーザー（お言葉は maybes.py の頭）"""
    return _decide_each("decide_maybes", askers, _pairs(questions), maybes.by_model, lambda t, c, rows: maybes.decide(c, rows), on_error, referee)


def decide_way_words(askers: list[Asker], pairs: list[tuple[str, tuple]], on_error: Callable | None = None,
                     referee: Asker | None = None) -> list[dict[str, str]]:
    """(こちらがすることの言葉, 方法の並び) の並び（業務設定から並べる1か所＝flowctl.way_word_pairs）→ 同じ並びで {方法: その方法を入れた言葉}（2つのモデルの
    言葉がどちらも確かめに通るときだけ、最初のモデルの言葉＝meanings.decide の way。そろわない方法は考える Sonnet＝_decide_each）。答えは業務設定 way_words
    （{言葉: {方法: 言葉}}＝instruction.Business）。決まらない方法は入れない＝会話の側は今の形（「（方法）で」を終わりの動詞の前に）。decide_unclear と同じ形。
    ✅ 2026-11-12 まとめ役の (1)"""
    items = [(d, tuple(w)) for d, w in pairs]
    return _decide_each("decide_way_words", askers, items, lambda c, m, xs, think=False: meanings.by_model(c, m, xs, "way", think=think),
                        lambda d, w, rows: meanings.decide(d, w, [r or {} for r in rows], "way"), on_error, referee)


def _unsettled(value, alone: list) -> bool:
    """2つの文字のAIでは決まらなかった所か（考える Sonnet に聞く所＝_decide_each）：決め方の decide が決まらない（None）と言ったか、どれか1つの文字のAIの答え
    だけなら決まる中身（alone＝その答えを decide に、2つの文字のAIがそろってそう答えたとして通したもの。選択肢ごとの言葉なら、その選択肢）が、2つの答えでは
    決まらなかった（そろわない）。どの文字のAIの答えだけでも決まらないもの（そろって空・答えない）は、決まった空（聞かない）。"""
    if value is None:
        return True
    if isinstance(value, dict):
        return any(k not in value for a in alone for k in a)
    return not value and any(alone)


def _filled(value, mine, alone: list):
    """決まらなかった所だけを、考える Sonnet の答え mine（decide に2つの文字のAIがそろってそう答えたとして通したもの＝その決め方の今の確かめ）で埋める：
    選択肢ごとの言葉は、そろわなかった選択肢だけ（そろった選択肢は替えない）・ほかは、mine が決まったときだけ。"""
    if not _unsettled(value, alone):
        return value
    if isinstance(value, dict):
        gaps = {k for a in alone for k in a if k not in value}
        got = {**value, **{k: w for k, w in (mine or {}).items() if k in gaps}}
        return {k: got[k] for k in dict.fromkeys([*(mine or {}), *value]) if k in got}   # 並びは選択肢の順（考える Sonnet の答えの並び）
    return mine if mine else value


def _decide_each(name: str, askers: list[Asker], items: list, by_model: Callable, decide: Callable, on_error: Callable | None,
                 referee: Asker | None, grounded: Callable | None = None, groups: list | None = None) -> list:
    """取込で、材料ごとに答えを決める唯一の形（どの決め方 name もここを通る。言葉を決める _settled・文を作る _drafted は、答えの形を整えるだけ）：2つの文字の
    AIに1回ずつ聞き（by_model）、決め方の decide（材料, 答えの並び）で決める。そろわない材料の扱いは、決め方ごとの表 SPLIT の name の行の1か所から読む：
    - 考える Sonnet で決める（BY_REFEREE）：そろわない材料（_unsettled）だけを、まとめて1回、考える Sonnet（referee＝(client, モデルの名前)。CL-2 の取込に
      deciders が1か所から渡す）に考える形で聞き、その答えを同じ decide に「2つの文字のAIがそろってそう答えた」として通したもの（＝その決め方の今の確かめ：
      形・選択肢にあるか・意味の言葉との向き など）を、決まらなかった所だけに使う（_filled）。通らない・答えない・referee が無ければ今の空。
    - 書いてある既定（WRITTEN。行の2つ目がよりどころ）：考える Sonnet に聞かない＝decide の既定のまま。
    ✅ 帳面 0-2「そろわないときは、考える Sonnet で決め」。items は材料の並び（文 1つ、または材料の組＝decide・grounded にそのまま渡す）。decide を項目ごとの組で
    渡すと、1回の問い合わせで材料ごとに項目をいくつも決める（by_model は材料ごとに項目の組を返す＝お知らせの中身の名詞と句 decide_matters）：項目ごとに決め、
    どれかの項目が決まらない材料だけを考える Sonnet に聞き、決まらなかった項目だけをその答えで埋める（11-15 11時59分07秒の (3)）。
    grounded＝中身の判定（askitems.grounded の grounding の問い＝考える Sonnet）を通ったときだけ使う（取込で作る文＝お知らせの文・言い換え・並べた文・要約）。中身の
    判定がある決め方は、考える Sonnet が無ければ空（中身を確かめられない文は使わない＝2つの文字のAIにも聞かない＝料金。⛔ 0c55c4e7 まで、聞いてから捨てていた＝見直し
    (12) の C-8）。groups＝材料ごとの組の印（同じ質問を並べ方を変えて書かせた文＝decide_alternatives）：同じ印の文は、中身の判定に1回でまとめて読ませる（問い合わせの数
    は組の数＝増えない）。通らなければ組の文を全部使わない・組の初めの文が決まらなければ、組のほかの文も使わない。無ければ材料ごとに1つの組。
    ⛔ ba505007 まで、_settled と _drafted が同じ並べ方を別々に書いていた（見直し (13) の C8）。⛔ 直す前（分身 C の束2）は、考える Sonnet を受ける決め方の一覧
    （REFEREED）と、受けない13の決め方（単位・意味の言葉・曖昧な言葉の意味・事実を伺う質問か・こちらがすることの言葉・遠ざかる向き・お呼びする方の一文・
    ほかに分からない点の無い答え・向きを伺う言葉・ご案内の方法を伺う質問か・どちらの場合もの確かめ・方法を入れた言葉・資料が届いているか）の別々の書き方に
    分かれ、そろわなければ空・既定にしていた（棚卸し rv179/I の束2 B2・7章 L1）。考える Sonnet の答えの確かめも、decide と別の fits を決め方ごとに渡していた。
    考える Sonnet が無いときの扱い（言葉はそろった答えを使う・文は空）は今のまま（揃えるかは伺い中＝rv72/r13c/ask.md の C8）"""
    if not items:
        return []
    many = isinstance(decide, tuple)
    decides = decide if many else (decide,)
    blank = (None,) * len(decides)
    if SPLIT[name][0] != BY_REFEREE:
        referee = None   # 書いてある既定のまま（考える Sonnet に聞かない）
    if grounded is not None and referee is None:
        return [("",) * len(decides) if many else "" for _ in items]
    args = [it if isinstance(it, tuple) else (it,) for it in items]
    ask = by_model if many else (lambda c, m, xs, think=False: [(w,) for w in by_model(c, m, xs, think=think)])
    rows = _each(askers, lambda c, m: ask(c, m, items), [blank] * len(items), on_error)
    said = [[[r[n][k] for r in rows] for k in range(len(decides))] for n in range(len(items))]
    got = [[d(*args[n], said[n][k]) for k, d in enumerate(decides)] for n in range(len(items))]
    if referee is not None:
        alone = [[[d(*args[n], [w, w]) for w in said[n][k]] for k, d in enumerate(decides)] for n in range(len(items))]
        left = [n for n in range(len(items)) if any(_unsettled(got[n][k], alone[n][k]) for k in range(len(decides)))]
        if left:
            drafts = _each([referee], lambda c, m: ask(c, m, [items[n] for n in left], think=True), [blank] * len(left), on_error)[0]
            for n, ws in zip(left, drafts):
                got[n] = [_filled(got[n][k], d(*args[n], [ws[k], ws[k]]), alone[n][k]) for k, d in enumerate(decides)]
    if grounded is None:
        return [tuple("" if w is None else w for w in ws) if many else ("" if ws[0] is None else ws[0]) for ws in got]
    keys = groups if groups is not None else list(range(len(items)))
    out = [""] * len(items)
    for key in dict.fromkeys(keys):
        idx = [n for n, k in enumerate(keys) if k == key]
        if got[idx[0]][0] is None:
            continue
        lines = [(n, got[n][0]) for n in idx if got[n][0] is not None]
        line = "\n".join(x for _, x in lines)
        if _each([referee], lambda c, m: grounded(c, m, *args[idx[0]], line), False, on_error)[0]:
            for n, x in lines:
                out[n] = x
    return out


def _settled(name: str, askers: list[Asker], texts: list[str], by_model: Callable, decide: Callable, on_error: Callable | None,
             referee: Asker | None) -> dict[str, str]:
    """文ごとの言葉を決める（用件の名詞 decide_purposes・その後の文の中身 decide_since・聞くこと・お知らせの中身の名詞と句 decide_matters）：決め方は _decide_each の
    1か所（中身の判定なし）。答えは文ごと（項目の組なら項目ごとの組）。"""
    return dict(zip(texts, _decide_each(name, askers, texts, by_model, decide, on_error, referee)))


def decide_purposes(askers: list[Asker], texts: list[str], on_error: Callable | None = None, referee: Asker | None = None) -> dict[str, str]:
    """用件の一文の並び → 文ごとの用件の名詞（purposes.decide＝2つのモデルがそろって、その文の中の言葉のとき。そろわない文は考える Sonnet＝_settled の1か所。
    決まらなければ空＝続きの電話でも今の用件の一文のまま・決まった伝言は「…からお電話があったと」）。答えは instruction.Business.purpose_noun（CL-2 の取込が
    業務設定へ）。✅ 2026-11-09 日本時間9時48分09秒の (4)・考える Sonnet は分身 D の (c)（帳面 0-2）"""
    return _settled("decide_purposes", askers, texts, purposes.by_model, purposes.decide, on_error, referee)


def decide_since(askers: list[Asker], texts: list[str], on_error: Callable | None = None, referee: Asker | None = None) -> dict[str, str]:
    """業務の質問の文の並び（CL-2 がかけ直す「わからない」の質問＝flowctl.Question.unknown_called_back の質問だけを渡すのは呼ぶ側）→ 文ごとの、その後の文
    「その後（中身）かお決まりになりましたでしょうか？」の中身の言葉（答えは flowctl.Question.since）。決め方は since.decide と _settled の1か所（そろわない文は
    考える Sonnet・形に合わなければ空＝今の形で伺い直す）。✅ 2026-11-13 日本時間20時06分19秒 ユーザー「伺い6は…」"""
    return _settled("decide_since", askers, texts, since.by_model, since.decide, on_error, referee)


def _drafted(name: str, askers: list[Asker], items: list[tuple], by_model: Callable, decide: Callable, grounded: Callable,
             on_error: Callable | None, referee: Asker | None, groups: list | None = None) -> list[str]:
    """取込で文を作る（お知らせの文 decide_announces・言い換えた質問 decide_rewords・選択肢を並べた質問 decide_alternatives・営業の要約 decide_summaries）：決め方は
    _decide_each の1か所（中身の判定 grounded を通ったときだけ使う・考える Sonnet が無ければ空・groups の組で中身の判定をまとめる）。"""
    return _decide_each(name, askers, items, by_model, decide, on_error, referee, grounded=grounded, groups=groups)


def decide_announces(askers: list[Asker], pairs: list[tuple[str, str]], on_error: Callable | None = None, referee: Asker | None = None) -> list[str]:
    """ご承認を伺う質問の (説明＝入る一言, 質問の文) の並び（flowctl.Question.approval の質問だけを渡すのは呼ぶ側）→ 同じ並びで、すらっと伝えるお知らせの文
    （「（説明）…のでご了承ください。」。答えは flowctl.Question.announce。決まらなければ空＝今の形）。決め方は announces の1か所：2つの文字のAIがそろって形に合う
    （announces.fits）文、そろわない文は考える Sonnet（referee）にまとめて1回聞いて形に合う文。どちらも、考える Sonnet の中身の判定（announces.grounded＝grounding の
    問い）を通ったときだけ使う。referee が無ければ空（中身を確かめられない文は使わない）。✅ 2026-11-13 日本時間20時06分19秒 ユーザー「伺い1の(1)はウ…」"""
    pairs = [(str(i or ""), str(t or "")) for i, t in pairs]
    return _drafted("decide_announces", askers, pairs, announces.by_model, announces.decide, announces.grounded, on_error, referee)


def decide_summaries(askers: list[Asker], items: list[tuple[str, tuple]], on_error: Callable | None = None,
                     referee: Asker | None = None) -> list[str]:
    """業務の (用件の一文, 質問の文の並び) の並び → 同じ並びで、急いでいるが手短になら聞くと言われたときに「ありがとうございます。（要約）の件でお電話させていただき
    ましたが…」と伺う営業の要約（「〜の件」の前に置ける名詞の形。答えは instruction.Business.summary＝CL-2 の取込が業務設定へ・依頼主の単位の録音 brief＝
    phrases.brief_line）。決め方はお知らせの文と同じ1か所（_drafted）：2つの文字のAIがそろって形に合う（summaries.fits）言葉、そろわない業務は考える Sonnet
    （referee）にまとめて1回書かせて形に合う言葉。どちらも考える Sonnet の中身の判定（summaries.grounded）を通ったときだけ使う。決まらなければ空＝用件の名詞で同じ
    文。✅ 2026-11-16 日本時間0時42分38秒・12時48分22秒 ユーザー（伺い30・伺い31。お言葉は summaries の頭）"""
    items = [(str(p or ""), tuple(str(t or "") for t in texts)) for p, texts in items]
    return _drafted("decide_summaries", askers, items, summaries.by_model, summaries.decide, summaries.grounded, on_error, referee)


def decide_matters(askers: list[Asker], items: list[tuple[str, str, str]], on_error: Callable | None = None,
                   referee: Asker | None = None) -> list[tuple[str, str]]:
    """ご承認を伺う質問の (説明＝入る一言, 質問の文, お知らせの文＝同じ質問の decide_announces の答え。決まらなければ空) の並び（decide_announces と同じ質問だけを
    渡すのは呼ぶ側。取込は decide_announces を先に呼ぶ）→ 同じ並びで、(お知らせの中身を表す名詞, 中身を常体にした句) の組（「キャンセル」「キャンセルは前日までに
    ご連絡いただく必要がある」。答えは flowctl.Question.matter と matter_clause＝CL-2 の取込が業務設定の質問の欄 matter・matter_clause へ。お知らせの文は
    decide_announces の答えのまま＝Question.announce）。名詞は最終の確かめ「それでは（名詞）の件はご了承ということでよろしかったでしょうか？」・句は聞き取れないまま
    「-」にしたお知らせの締めの前の確かめ「（句）件のお返事のお声が聞き取れなかったのですが…」（どちらも flowctl.accepted_line）。名詞と句は同じ1回の問い合わせ
    （announces.matter_by_model）。句はお客様が聞いた文（announces.heard＝お知らせの文、無ければ入る一言＋質問の文）から、決まり・規則として言わない形で作る
    （11-16 0時42分38秒のお言葉）。決め方は用件の名詞と同じ1か所（_settled の項目の組：項目ごとに2つの文字のAIがそろって形に合えば（名詞＝説明と質問の文の中の
    言葉＝announces.matter_fits・句＝説明と質問の文とお客様が聞いた文の言葉＝announces.clause_fits）その言葉・どれかの項目がそろわない文は考える Sonnet＝referee に
    まとめて1回・決まった項目は替えない）。決まらなければ空＝名詞は「先ほどの内容」・句は名詞の形。お知らせの文を渡さない並び（(説明, 質問の文) の組）は止める
    （お客様が聞いていない文から句を作らない）。✅ 2026-11-14 日本時間14時31分38秒 ユーザー「●伺い10 「はい、それでは、キャンセルの件はご了承という
    ことでよろしかったでしょうか？」」・11-15 日本時間11時59分07秒「・(3)今：…」・11-16 日本時間0時42分38秒「…決まりになっております。だと相手の印象が悪いので…」"""
    if any(not isinstance(x, (tuple, list)) or len(x) != 3 for x in items):
        raise TypeError("お知らせの中身は (説明, 質問の文, お知らせの文＝decide_announces の答え。決まらなければ空) の並びで渡す（句はお客様が聞いた文から作る）")
    items = [(str(i or ""), str(t or ""), str(a or "")) for i, t, a in items]
    src = {"／".join(x): x for x in items}   # 文ごとに決める1か所（_settled）の鍵＝説明と質問の文とお知らせの文
    got = _settled("decide_matters", askers, list(src),
                   lambda c, m, keys, think=False: announces.matter_by_model(c, m, [src[k] for k in keys], think),
                   (lambda k, words: announces.matter_decide(*src[k][:2], words), lambda k, words: announces.clause_decide(*src[k][:2], words, src[k][2])),
                   on_error, referee)
    return [got["／".join(x)] for x in items]


def decide_rewords(askers: list[Asker], items: list[tuple[str, tuple, str]], on_error: Callable | None = None, referee: Asker | None = None) -> list[str]:
    """申し出でない業務の質問の (質問の文, 選択肢の並び, 型) の並び（flowctl.aside_fits の質問だけを渡すのは呼ぶ側）→ 同じ並びで、会話が繋がらないお返事のあとに
    「ところで」に続けて伺い直す、言い換えた質問の文（答えは flowctl.Question.reworded。決まらなければ空＝「ところで」＋元の質問の文）。決め方はお知らせの文と
    同じ1か所（_drafted）・形は rewords.fits・中身の判定は rewords.grounded。✅ 2026-11-13 日本時間22時17分01秒 ユーザー「9は…」"""
    items = [(str(t or ""), tuple(c), str(k or "")) for t, c, k in items]
    return _drafted("decide_rewords", askers, items, rewords.by_model, rewords.decide, rewords.grounded, on_error, referee)


def decide_alternatives(askers: list[Asker], items: list[tuple], on_error: Callable | None = None,
                        referee: Asker | None = None) -> list[str]:
    """選ぶ質問の (質問の文, 選択肢の並び, 型) の並び（flowctl.alternatives_fits の質問だけを渡すのは呼ぶ側）→ 同じ並びで、お客様の意向がまだ分からないまま戻る
    ときに「はい、」に続けて伺う、選択肢を並べた質問の文の組 (質問の順の文, 先に並べる選択肢ごとの文の並び)（答えは flowctl.Question.alternatives と
    alternatives_led。並びは alternatives.orders の順＝2つ目からの選択肢ごと。決まらなければ空＝質問の順の文が空なら「はい、」＋言い換えた質問・先に並べた文が
    空ならその選択肢に触れたときも質問の順）。items の4つ目（無ければ真）＝先に並べた文を作る質問か（flowctl.leads_fit の1か所＝先に並べる番に来る質問。偽なら
    質問の順の文だけを作り、先に並べた文は空。⛔ 0c55c4e7 まで、申し出の選ぶ質問にも作っていた＝流れない文の料金＝見直し (12) の C-6）。決め方は言い換えた質問と同じ1か所（_drafted）：並べ方ごとの行を同じ問い合わせに足す（2つの文字のAIと考える Sonnet の
    問い合わせの数は同じ）・形は alternatives.fits（並べた順のまま全部・「それとも」）・中身の判定は alternatives.grounded を質問ごとに1回（その質問の文をまとめて）。
    ✅ 2026-11-14 日本時間22時15分15秒 ユーザー（お言葉は alternatives の頭）・23時45分00秒「伺い16はイでOK」"""
    items = [(str(it[0] or ""), tuple(it[1]), str(it[2] or ""), bool(it[3]) if len(it) > 3 else True) for it in items]
    rows = [(n, (t, order, k)) for n, (t, c, k, leads) in enumerate(items) for order in (c, *(alternatives.orders(c) if leads else ()))]
    got = _drafted("decide_alternatives", askers, [r for _, r in rows], alternatives.by_model, alternatives.decide, alternatives.grounded, on_error, referee,
                   groups=[n for n, _ in rows])
    lines = [[x for (n, _), x in zip(rows, got) if n == m] for m in range(len(items))]
    return [(x[0], tuple(x[1:]) or ("",) * len(alternatives.orders(c))) for x, (_, c, _, _) in zip(lines, items)]


def decide_at_hand(askers: list[Asker], questions: list[tuple[str, tuple]], on_error: Callable | None = None, referee: Asker | None = None) -> list[str]:
    """(質問の文, 選択肢の並び) の並び → 同じ並びで、資料がお手元に届いているかを伺う質問の「届いている」にあたる選択肢の名前（伺う質問でない・決まらないなら空＝
    announces.decide_hand。そろわない質問は考える Sonnet＝_decide_each。unclear と同じ形）。業務の選ぶ質問だけを渡すのは呼ぶ側。答えは flowctl.Question.at_hand
    （お知らせの「ご確認ください。」の文の条件）。✅ 2026-11-13 日本時間20時06分19秒 ユーザー「(3)b」"""
    return _decide_each("decide_at_hand", askers, _pairs(questions), announces.hand_by_model, lambda t, c, rows: announces.decide_hand(c, rows),
                        on_error, referee)


# 決める処理の唯一の一覧（CL-2 の取込 business_import.preview の decide_* の受け口と同じ名前＝試験 test_shapes_match_cl2_import で比べる）。decide_away・
# decide_unclear は、CL-2 の受け口ができた（CL-2 の連絡帳 4-212 の (A)。⛔ それまで、この一覧に無く、取込から呼ばれなかった）
DECIDERS = {"decide_units": decide_units, "decide_topics": decide_topics, "decide_meanings": decide_meanings,
            "decide_replies": decide_replies, "decide_facts": decide_facts, "decide_do_words": decide_do_words,
            "decide_away": decide_away, "decide_unclear": decide_unclear,
            # いまの話の向きを伺う言葉・お呼びする方がいらっしゃるかの一文（CL-2 の連絡帳 4-199 の (1)・4-203 の (1)。いまの話の向きは 2026-11-18 に CL-2 の受け口が main に入った。⏳ お呼びする方の一文は CL-2 の受け口ができるまで CL2_PENDING）
            "decide_doubt_words": decide_doubt_words, "decide_target_asks": decide_target_asks,
            # ご案内の方法を伺う質問か（✅ 2026-11-05 日本時間23時19分06秒「案でOK　(2)ア」）
            "decide_ways": decide_ways,
            # どちらの場合もありうると言われたときに確かめる答えと言葉（⛔✅ 2026-11-09 日本時間4時36分49秒。帳面 4-123）
            "decide_maybes": decide_maybes,
            # 用件の一文の中の用件の名詞（続きの電話の用件の一文を前の電話とつなげる＝✅ 11-09 日本時間9時48分09秒の (4)。帳面 4-128）
            "decide_purposes": decide_purposes,
            # 方法を申し出の中身に入れる言い方（✅ 2026-11-12 まとめ役の (1)。帳面 4-132）
            "decide_way_words": decide_way_words,
            # その後の文の中身（✅ 2026-11-13 日本時間20時06分19秒「伺い6は…」。帳面 4-149）
            "decide_since": decide_since,
            # ご承認のお知らせの文と、資料が届いているかを伺う質問（✅ 2026-11-13 日本時間20時06分19秒「伺い1…」。帳面 4-150）。
            # お知らせの文は考える Sonnet が書く・中身の判定もする
            "decide_announces": decide_announces, "decide_at_hand": decide_at_hand,
            # 営業の要約（✅ 2026-11-16 日本時間0時42分38秒 ユーザー（伺い30））。考える Sonnet が書く・中身の判定もする
            "decide_summaries": decide_summaries,
            # お知らせの中身の名詞（✅ 2026-11-14 日本時間14時31分38秒「●伺い10」。帳面 4-154）と、中身を常体にした句
            # （同じ問い合わせ・答えは (名詞, 句) の組。✅ 2026-11-15 日本時間11時59分07秒「・(3)」。帳面 4-162）。
            # 渡すのは (説明, 質問の文, お知らせの文＝decide_announces の答え) の並び＝取込は decide_announces を先に呼ぶ（句はお客様が聞いた文から作る＝11-16 0時42分38秒）
            "decide_matters": decide_matters,
            # 会話が繋がらないお返事のあとに「ところで」に続けて伺い直す、言い換えた質問（✅ 2026-11-13 日本時間22時17分01秒「9は…」。帳面 4-152）。考える Sonnet が書く・中身の判定もする
            "decide_rewords": decide_rewords,
            # 意向がまだ分からないまま戻るときの選択肢を並べた質問（✅ 2026-11-14 日本時間22時15分15秒。帳面 4-156）。考える Sonnet が書く・中身の判定もする
            "decide_alternatives": decide_alternatives}

# 2つの文字のAIがそろわない材料の扱い（決め方ごとの唯一の表。_decide_each が1つ目を読む）。✅ 帳面 0-2「人は介在しない」の「そろわないときは、考える Sonnet で
# 決め、それでも決まらなければその場でお客様に1回確かめる」の順＝(1) 2つの文字のAIがそろえばその答え (2) そろわない材料は考える Sonnet で決める（BY_REFEREE）
# (3) それでも決まらなければ空＝会話の側の今の形・通話の中で確かめる（行の2つ目＝最後の段とそのよりどころ。本書 6-2 の「決まらなければ空＝通話の中で確かめる」
# は、そろわないときの既定ではなく、この最後の段＝6-2 は「そろわなければ」ではなく「決まらなければ」と書いてある・まとめ役 11-21 日本時間4時台）。
# 書いてある既定（WRITTEN）は、そろわないときにどちらの誤りが重いかで倒す側を決めた理由のある決め方だけ＝考える Sonnet に聞かず、その既定（値は決め方の
# decide の1か所。ここにはよりどころだけを書く＝写しを作らない）。⛔ 直す前は、考える Sonnet に聞く決め方の一覧（REFEREED・8つ）と、聞かない13の決め方の
# 別々の書き方に分かれていた（棚卸し rv179/I の束2 B2・7章 L1。帳面 4-125 の ⏳「そろわないときに考える Sonnet で決める形は、取込のほかの決め方と一緒に入れます」）
BY_REFEREE = "考える Sonnet で決める"
WRITTEN = "書いてある既定"
SPLIT = {
    "decide_units": (BY_REFEREE, "最後の段＝空＝単位を補わない（設計書 6-2 の数の質問の単位「3. 決まらなければ空＝単位を補わない（…お客様が言い直しで直して"
                                 "くださる）」・設計書 3-7「決まらなければ空」）"),
    "decide_topics": (BY_REFEREE, "最後の段＝空＝伺い直しはその質問の文（本書 9-5「聞くことも取込のほかの決め方と同じ1か所（そろわなければ考える Sonnet）で決め、"
                                  "それでも無ければ伺い直しにその質問の文…を言う」）"),
    "decide_meanings": (BY_REFEREE, "最後の段＝空＝選択肢の名前で言い直す・通話の中で確かめる（本書 6-2「決まらなければ空＝通話の中で確かめる」・設計書 3-7）"),
    "decide_replies": (BY_REFEREE, "最後の段＝入れない＝通話の中で確かめる（本書 6-2「決まらなければ空＝通話の中で確かめる」・設計書 3-7・帳面 2-1）"),
    "decide_facts": (WRITTEN, "既定＝事実を伺う質問（facts.decide。本書 9-5 の「事実を伺う質問」（✅ 2026-11-01 (a)「OK」）「そろわなければ事実を伺う質問」・"
                              "設計書 3-7 の asks_fact「決まらない・答えが無い・形が違うときは真（…誤った値の重いほうを避ける側）」・帳面 2-1「そろわなければ真」）"),
    "decide_do_words": (BY_REFEREE, "最後の段＝入れない＝言い切っていない答えは今の形・締める前の申し出は作らない（設計書 3-7 の close_offer「申し出の形で"
                                    "決まらなければ締める前の申し出を入れない」・帳面 4-125）"),
    "decide_away": (WRITTEN, "既定＝遠ざかる向き（meanings.decide_away。設計書 3-7 の away「どちらか1つの AI でも遠ざかる向きと読むか、読めない選択肢は入れる"
                             "＝安全側」・帳面 4-72）"),
    "decide_unclear": (BY_REFEREE, "最後の段＝空＝伺う（「続きまして」で伺う。本書 9-5 の「取得済みのことを重ねて聞かない」の段「（そろわなければ伺う）」と同じ段の"
                                   "「⚠️ 取込の文字のAI2つがそろわず印が空の質問は「続きまして」のまま」・設計書 6-10「そろわなければ空＝伺う」）"),
    "decide_doubt_words": (BY_REFEREE, "最後の段＝入れない＝「念のため（意味の言葉）…」の確かめ（設計書 6-10「言葉が無ければ曖昧な答えと同じ確かめ」・帳面 4-90）"),
    "decide_target_asks": (WRITTEN, "既定＝業務の質問のまま（target_asks.decide。設計書 6-10 の一本化の残りの (c)「そろわない・答えが無いときは偽＝依頼者が書いた"
                                    "とおり業務の質問のまま（取込で止めて伺わない・推し量って置き換えない＝11-04 日本時間6時44分のご判断と同じ理由）」・帳面 4-90。"
                                    "⏳ 取込の作り直し（CL-2 の 4-216・4-90 の (2)）で外れうる＝作り直し待ち）"),
    "decide_ways": (BY_REFEREE, "最後の段＝偽＝今までどおり「ほかにご希望などは…」（本書・設計書に決まらないときの文は無い。ways の頭）"),
    "decide_maybes": (BY_REFEREE, "最後の段＝空＝この確かめの無い質問の今のまま（本書 9-5 の「事実を伺う質問」の段「この確かめの無い質問は今のまま」）"),
    "decide_purposes": (BY_REFEREE, "最後の段＝入れない＝今の用件の一文のまま（設計書 3-7 の purpose_noun「決まらなければ入れない」）"),
    "decide_way_words": (BY_REFEREE, "最後の段＝入れない＝「（方法）で」の今の形（設計書 3-7 の way_words「無ければ空＝…今の形」・設計書 6-10「そろわなければ今の形"
                                     "「（方法）で」」）"),
    "decide_since": (BY_REFEREE, "最後の段＝空＝今の形（本書 9-5「そろわない文だけを考える Sonnet に1回聞き・形に合わなければ空＝今の形」）"),
    "decide_announces": (BY_REFEREE, "最後の段＝空＝今の形（設計書 6-10「決まらなければ空＝今の形」）"),
    "decide_at_hand": (BY_REFEREE, "最後の段＝空＝伺う質問ではない（帳面 4-150「空＝伺う質問ではない」・設計書 3-7「既定は空＝会話の側は今の形」）"),
    "decide_summaries": (BY_REFEREE, "最後の段＝空＝用件の一文の録音（本書 9-5「無い業務は用件の一文の録音」）"),
    "decide_matters": (BY_REFEREE, "最後の段＝空＝「それでは先ほどの内容は…」（設計書 6-10「無ければ ANNOUNCE_RECHECK」）"),
    "decide_rewords": (BY_REFEREE, "最後の段＝空＝「ところで」＋元の質問の文（設計書 6-10「無ければ言い換えた質問…それも無ければいまの形」）"),
    "decide_alternatives": (BY_REFEREE, "最後の段＝空＝言い換えた質問・それも無ければ今の形（設計書 6-10「選択肢を並べた文…があれば…無ければ言い換えた質問・それも"
                                        "無ければいまの形」）"),
}


def deciders(askers: list[Asker], on_error: Callable | None = None, referee: Asker | None = None) -> dict[str, Callable]:
    """CL-2 の取込（business_import.preview・ask_ai）に渡す決める処理（引数の名前 → 処理）。askers は2つ以上（2つのモデルが合うときだけ
    決める＝1つでは何も決まらない）。referee＝そろわない材料だけを聞く考える Sonnet（(client, モデルの名前)。ここ1か所から全部の決め方に渡し、使うかは
    表 SPLIT の1か所＝_decide_each が読む。無ければ空のまま）。⛔ 直す前は、ここで考える Sonnet を渡す決め方を一覧（REFEREED）で渡し分けていた"""
    assert len(askers) >= 2, "取込で決めることは、2つ以上のモデルが合うときだけ決める"
    return {name: (lambda qs, fn=fn: fn(askers, qs, on_error, referee)) for name, fn in DECIDERS.items()}
