"""電話を終える言葉の試しの正解の表（唯一の場所。2026-11-01 日本時間の夜に作った・⏳ ユーザーにご覧いただく）。

判定の項目「電話を終える言葉か」（flowctl.end_choices）の正解と、その言葉の答えの正解を、ご判断どおりに持つ。表の行ごとに、正解の読みを
流れの制御に当てたとき、お客様に聞こえる言葉を出す（heard。表に手で書かない＝作りから出す。試験は test_offline.EndWordsTest）。
測る道具（答えの段の G-53・お呼びする段の G-59）は、この表から言葉と正解を読む（⏳ 測りは、整理が全部済んでから1回）。

ご判断（そのまま）：
- 11-01 20時11分「1.「いま時間がないので結構です」（折り返しの申し出への返事）： 言っていないし、 急いでいる」「2.「かけてこないでください」
  （折り返しの申し出への返事）：その依頼者の最終目的が、担当者からの折り返しなら、断った＝もうかけないと同じ意味。…担当者からの折り返しは
  サブの目的で、例えば、登録とかが目的なら、営業を拒否したのではなく、担当者からの折り返しを拒否しただけ。」「3.「そういった案内はお断りします」
  （説明会の質問への返事）：2.と同じ」「4.「こういう営業の電話、迷惑なんですけど。」：(ア) 断った」「お急ぎではございませんか？	はい、急いでます：
  …この2つのやり取りしかない場合は急いでいると言っているんじゃないですか？」「訪問自体お断りします…訪問してほしくないと答えを言っているように
  聞こえますが！？」
- 11-01 20時56分「念のためお急ぎではないということでよろしかったでしょうか？	いいえ、かなり急いでおります。…質問の精度が悪いので、これだけでは
  判断できない。」（⇒ この表には入れない。急ぎを伺う質問は、業務の流れの中で何を伺うかが決まってから＝CL-2）
- 11-01 22時31分「「うーん？」は悩んでいる最中なので、少し待って…ただし、「んーん」なら「NO」の意味なので気を付けて！」
- 11-01 23時59分「少し曖昧だが、方向性は決まった場合は、こちら：「念のためパンフレットをお送りして大丈夫だということでよろしかったでしょうか？」よりも、
  「それではパンフレットをお送りさせていただきますね？」の方がいいんじゃないですか？と言いましたよね。何度も修正させるな！」（20時56分のお言葉）
  ・「お名前だけではどの者か分かりかねますので、お繋ぎできかねます。…名前以外に部署やフルネームを伝えて、取り次いでもらう努力をするべきじゃないでしょうか。」
- 11-01 6時05分（事実を伺う質問）「「それでは、ご予約はされているということで登録(記録)させていただいて大丈夫でしょうか？」のような形で…」
"""
from __future__ import annotations

from flowctl import (AWAY, AWAY_NO, AWAY_YES, DONT_KNOW_LABEL, END_ANGRY, END_BRIEF, END_HURRY, END_NONE, END_REASONED, END_REFUSED, END_UNCLEAR, END_UPSET, END_WHO,
                     LATER, QUERY, QUERY_NO, REFUSED_LABEL, THINKING, UNDECIDED, WHY_NOT_SAID)

YES_NO = ("はい", "いいえ")
HEDGED = "（言い切っていない）"   # 答えの正解の印：その答えに寄っているが言い切っていない（判定の firm＝偽）
OFFER = "担当者から詳しいご案内のお電話を差し上げてもよろしいでしょうか？"   # ✅ 22時31分・23時04分：仮（既定）の言い方
PAMPHLET = "パンフレットをお送りしてもよろしいでしょうか？"
SEMINAR = "説明会にはご参加いただけますでしょうか？"
RESERVED = "ご予約はされていますでしょうか？"
DOCS = "資料はお手元に届いていますでしょうか？"
HURRY_Q = "お急ぎではございませんか？"
VISIT = "担当者がご訪問させていただいてもよろしいでしょうか？"
CONTACT = "ご連絡はお電話とメールのどちらがよろしいでしょうか？"
PAY = "お支払いはカードと振込のどちらがよろしいでしょうか？"
# 表の質問と選択肢（取込に渡すもの）。答えの段の測りの質問の表（g53_meaning.question_map）に無い質問は、取込と同じ形で G-72 が意味の言葉・事実を
# 伺う質問かを作る。どの質問も、意味の言葉・事実を伺う質問か・向きを伺う言葉・こちらがすることの言葉は、取込で作ったもの（question）＝手で書かない。
# ⛔ 2026-11-02 まで、ここに手で書いていた（「大丈夫だということ」「ご予約はお済み」「お振込み」＝取込の言葉と違う言葉で表を作っていた）
CHOICES = {OFFER: YES_NO, PAMPHLET: YES_NO, SEMINAR: ("参加する", "参加しない"), RESERVED: YES_NO, DOCS: YES_NO, HURRY_Q: YES_NO,
           VISIT: YES_NO, CONTACT: ("電話", "メール"), PAY: ("カード", "振込")}


def question(qtext: str, goal: bool = False, qid: str = "Q1"):
    """表の質問を、取込で作った言葉のついた流れの制御の質問に（唯一の場所＝答えの段の測りの質問の表 g53_meaning.question_by_text）。goal＝業務の
    最終目的の質問か（断る向き＝2つ目の選択肢）。"""
    import dataclasses
    from g53_meaning import question_by_text
    q = question_by_text(qtext)
    return dataclasses.replace(q, id=qid, final_goal=goal, declines=(q.conditions[1],) if goal else ())

# 答えの段：(質問, お客様の言葉, 電話を終える言葉かの正解, 答えの正解, 業務の最終目的の質問か, 理由)。正解は受け取ってよい読みの組
# （先頭が、表でお客様に聞こえる言葉を出す読み）。答えの「unknown」＝わからない・「refused」＝答えたくない
ANSWER = [
    (OFFER, "いま時間がないので結構です。", (END_HURRY,), ("いいえ", UNDECIDED), False, "20時11分の1.＝急いでいる"),
    (OFFER, "かけてこないでください。", (END_NONE,), ("いいえ",), False, "20時11分の2.＝申し出への答え（最終目的かは業務設定）"),
    (OFFER, "かけてこないでください。", (END_NONE,), ("いいえ",), True, "同じ（この申し出が業務の最終目的のとき）"),
    (SEMINAR, "そういった案内はお断りします。", (END_NONE,), ("参加しない",), False, "20時11分の3.＝2.と同じ"),
    (SEMINAR, "こういう営業の電話、迷惑なんですけど。", (END_REFUSED,), (UNDECIDED,), False, "20時11分の4.＝断った"),
    (DOCS, "そういう確認はお断りします。", (END_NONE,), ("refused",), False, "答えることを断っただけ（答えたくない）"),
    (HURRY_Q, "はい、急いでます。", (END_HURRY,), ("いいえ", UNDECIDED), False, "20時11分＝このやり取りだけなら急いでいる"),
    (VISIT, "訪問自体お断りします。", (END_NONE,), ("いいえ",), False, "20時11分＝訪問してほしくないという答え"),
    (PAMPHLET, "結構です。もうかけてこないでください。", (END_REFUSED,), ("いいえ",), False, "電話を断った（答えは送らない）"),
    (DOCS, "もう電話しないでもらえますか。", (END_REFUSED,), (UNDECIDED,), False, "電話を断った"),
    (RESERVED, "ごめんなさい、今から出かけるので切りますね。", (END_HURRY,), (UNDECIDED,), False, "急いでいる"),
    (RESERVED, "忙しいんで。", (END_HURRY,), (UNDECIDED,), False, "10-13「忙しい」とだけ＝急いでいる"),
    # ✅ 2026-11-16 日本時間0時42分38秒 ユーザー「伺い30：「ありがとうございます。～（営業の要約）」手短にと言っているのに、いきなり、「お電話とメール」を伺うなんて
    # ありえないでしょ。」＝急いでいるが手短になら聞く（電話を終える言葉の答えの段だけの選べるもの END_BRIEF）
    (CONTACT, "今ちょっと立て込んでるので、手短にお願いできますか。", (END_BRIEF,), (UNDECIDED,), False, "伺い30・伺い31＝急いでいるが手短になら聞く（お礼と要約のあと後ほどかけ直してよいかを伺う）"),
    # ⛔ 11-06 3時台に直した：正解を「決められない」だけで持っていた（答えの段に「あとでかけ直す」が無かった頃の正解。表を当てる道具が、言葉に「あとで」が
    #    あればかけ直しの報告で当てる言葉の形の特別扱いで補っていた）＝答えの段の選べるもの（meaning_judge.ANSWER_KINDS）の「あとでかけ直す」
    (CONTACT, "あとでかけ直してもらえますか。", (END_NONE,), (LATER,), False, "あとでかけ直してほしい＝あとでかけ直す（答えではない・電話を終える言葉でもない）"),
    # 急いでいるの説明「何時ならよい・かけ直してほしい、とまでは言っていない」の境目。急いでいると読むと、頼まれたことを「それではまた改めてお掛けしても
    # よろしいでしょうか？」と重ねて伺う（本書 9-3「取得済みのことを重ねて聞かない」）＝急いでいるは正解に入れない（11-06 3時台に足した）
    (CONTACT, "今ちょっと忙しいので、あとでかけ直してもらえますか。", (END_NONE,), (LATER,), False,
     "急いでいる理由を言っても、かけ直しを頼んだ＝あとでかけ直す（急いでいる、ではない）"),
    (PAY, "さっきから何なんですか、失礼じゃないですか。", (END_ANGRY,), (UNDECIDED,), False, "怒っている"),
    (PAMPHLET, "もういいです。", (UNDECIDED,), ("いいえ",), False, "20時56分・22時31分＝いまの話の向きを先に伺う"),
    (PAMPHLET, "んーん。", (END_NONE,), ("いいえ",), False, "22時31分＝「んーん」は「いいえ」"),
    (PAMPHLET, "うーん。", (END_NONE,), (UNDECIDED, THINKING), False,
     "22時31分＝悩んでいる最中（待って伺う）。言い終えていない言いよどみ＝考えている途中も正解（11-06 11時19分55秒「(1) ア」で足した選べるもの・F5 の 11-15）"),
    (PAMPHLET, "うーん、送ってもらおうかな…。", (END_NONE,), ("はい" + HEDGED,), False, "言い切っていないが向きは決まった答え（20時56分・23時59分）"),
    (PAMPHLET, "うーん、今回はいいかなあ…。", (END_NONE,), ("いいえ" + HEDGED,), False, "同じ（送らない向き）"),
    (PAY, "たぶんカードかなと思います。", (END_NONE,), ("カード" + HEDGED,), False, "同じ形を、希望を伺う選ぶ質問にも（11-02 1時22分 (c)）"),
    (RESERVED, "たぶん予約してると思います。", (END_NONE,), ("はい" + HEDGED,), False, "事実を伺う質問（6時05分・依頼者がすること＝初めの値の記録）"),
    (RESERVED, "そういうお電話はちょっと……。", (UNDECIDED,), (UNDECIDED,), False, "電話を断っていそうだが言い切っていない・答えでもない"),
    (RESERVED, "結構です。", (UNDECIDED, END_NONE), (UNDECIDED,), False, "10-17「その言葉だけで通話の断りとして締めない」"),
    # 答えを断る言葉が強いとき（分身 AE の書き置き rv140/AE/notes.md の 2 の (2) 型 (c)＝まとめ役が決まりで決めた・分身 AG）：電話を終える言葉ではない（11-02 日本時間
    # 3時59分・4時16分・11-01 23時59分「(a) OK」）・その質問は答えたくない＝✕で二度と伺わない（10-13）・受け取る返事の頭はお詫び「失礼いたしました。」（10-14）
    (RESERVED, "言う筋合いはないと思います。", (END_UPSET,), ("refused",), False, "答えを断る言葉が強い＝不快な様子だが電話は続けてよい（お詫びから）"),
]

# 答えとは別に読む項目（こちらへ尋ねた問いか・別のお話か）と、わからない・答えたくない理由の正解（g53_labels.json の行と同じ欄・同じ決め方＝F5 の 11-15：
# 2人の読み手が全部の行を読み、問いがある・別のお話と書かなかった行は「問いは無い」「答えの話」。別のお話かは、その項目を読む質問（flowctl.away_fits）の
# 行だけ。理由は、答えが「決められない」を含む行（流れの制御が理由の読みを使う＝Flow._not_deciding）だけ、流れの制御で同じ動きになる名前を並べる＝理由を
# 言っていない言葉は「とは言っていない」「理由を言わなかった」「決められない」のどれでも同じ動き。頼みの形（「〜もらえますか」）・怒りの言い回しは問いではない）
_WHY_UNSAID = [WHY_NOT_SAID, DONT_KNOW_LABEL["unspecified"], UNDECIDED]
_WHY_REFUSED = [WHY_NOT_SAID, REFUSED_LABEL["unwilling"], UNDECIDED]
_Q, _A = {QUERY: [QUERY_NO]}, {QUERY: [QUERY_NO], AWAY: [AWAY_NO]}
SIDE = {
    (OFFER, "いま時間がないので結構です。"): {**_Q, "reason": _WHY_UNSAID},
    (OFFER, "かけてこないでください。"): _Q,
    (SEMINAR, "こういう営業の電話、迷惑なんですけど。"): {**_Q, "reason": _WHY_UNSAID},
    (SEMINAR, "そういった案内はお断りします。"): _A,
    (DOCS, "そういう確認はお断りします。"): {**_A, "reason": _WHY_REFUSED},
    (DOCS, "もう電話しないでもらえますか。"): {**_A, "reason": _WHY_UNSAID},
    (HURRY_Q, "はい、急いでます。"): {**_A, "reason": _WHY_UNSAID},
    (VISIT, "訪問自体お断りします。"): _Q,
    (CONTACT, "今ちょっと立て込んでるので、手短にお願いできますか。"): {**_Q, "reason": _WHY_UNSAID},
    (CONTACT, "あとでかけ直してもらえますか。"): _A,
    (CONTACT, "今ちょっと忙しいので、あとでかけ直してもらえますか。"): _A,
    (PAY, "さっきから何なんですか、失礼じゃないですか。"): {**_Q, AWAY: [AWAY_YES, AWAY_NO], "reason": _WHY_UNSAID},
    (PAY, "たぶんカードかなと思います。"): _A,
    (RESERVED, "ごめんなさい、今から出かけるので切りますね。"): {**_Q, "reason": _WHY_UNSAID},
    (RESERVED, "忙しいんで。"): {**_Q, "reason": _WHY_UNSAID},
    (RESERVED, "そういうお電話はちょっと……。"): {**_Q, "reason": _WHY_UNSAID},
    (RESERVED, "結構です。"): {**_A, "reason": _WHY_UNSAID},
    (RESERVED, "言う筋合いはないと思います。"): {**_A, "reason": _WHY_REFUSED},
    (RESERVED, "たぶん予約してると思います。"): _A,
    (PAMPHLET, "うーん。"): {**_Q, "reason": _WHY_UNSAID},
    (PAMPHLET, "結構です。もうかけてこないでください。"): _Q,
    (PAMPHLET, "もういいです。"): _Q,
    (PAMPHLET, "んーん。"): _Q,
    (PAMPHLET, "うーん、送ってもらおうかな…。"): _Q,
    (PAMPHLET, "うーん、今回はいいかなあ…。"): _Q,
}

# お呼びする段：(電話口の方, お客様の言葉, 電話を終える言葉かの正解, 理由, 項目ごとの正解の字)。項目ごとの正解の字は、お呼びする段の測り（g59_calling_judge
# の F と同じ字＝前と同じ方か・誰か・電話のやり取り・お呼びした方の事情・5つ目は書いた行だけ＝お呼びした方が電話に出られない理由。並べた字のどれでもよい）。正解は、判定に見せる言葉（呼ぶ一言・その前に相手が
# 言ったこと・お客様の言葉）だけから決める：受付の方の行は、呼ぶ一言への最初の言葉（その前の言葉は無い）・ご本人の行は、受付の方の
# 「岡田ですね。少々お待ちください。」（BEFORE_TARGET）のあとに出た方の言葉
BEFORE_TARGET = "岡田ですね。少々お待ちください。"
CALLING = [
    ("受付の方", "うちはそういうのは間に合ってますので。", (END_REFUSED,), "営業のお断り", ("同", "受不", "無", "無")),
    ("受付の方", "営業のお電話でしたらお繋ぎできません。", (END_REFUSED,), "10-22＝営業のお断り（もうかけない）", ("同", "受", "無", "無", "無決")),
    ("受付の方", "お名前だけではどの者か分かりかねますので、お繋ぎできかねます。", (END_WHO,), "23時59分＝部署・フルネームを伝えてお呼びし直す",
     ("同", "受", "無", "無", "無決")),
    ("受付の方", "個人あてのお電話はお繋ぎしておりません。", (END_REASONED,), "10-22＝事情があって取り次げない（日を改めて1回）", ("同", "受", "無", "無", "無決")),
    ("受付の方", "あいにく取り次ぐことはいたしかねます。", (END_UNCLEAR,), "11-01 5時26分＝理由を言わずに取り次げない", ("同", "受", "無", "無")),
    ("受付の方", "岡田は本日終日外出しておりまして。", (END_NONE,), "お呼びした方の事情（電話を終える言葉ではない）", ("同", "受", "無", "在", "長")),
    ("受付の方", "結構です。", (UNDECIDED,), "10-17＝その言葉だけで断りとしない", ("同", "受不", "無", "無")),
    ("受付の方", "申し訳ございません、今ちょっとバタバタしておりまして。", (END_HURRY,), "急いでいる（受付の方でも同じ伺い）", ("同", "受不", "無", "無", "後無決")),
    ("ご本人", "すみません、いま手が離せないので失礼します。", (END_HURRY,), "10-13「忙しい」とだけ＝急いでいる", ("違", "本不", "無", "無", "後無")),
    ("ご本人", "何度も同じ電話してきて、いい加減にしてください！", (END_ANGRY,), "怒っている・苦情", ("違", "本不", "無", "無")),
]


def answer_truths() -> dict:
    """答えの段の行の正解（唯一の場所。答えの段の測り G-53 の組 F が読む＝g53_meaning._label）：(質問の文, お客様の言葉) ⇒ {accept：受け取ってよい
    答え・hedged：寄っているが言い切っていないか・end：電話を終える言葉かの正解・why・こちらへ尋ねた問いか・別のお話か・わからない理由＝SIDE}。
    答えは書いたとおり（⛔ 2026-11-15 F4 の (c) まで「決められない」に「わからない」を足していた＝お客様に聞こえる次の一言が違う2つを同じに数え、
    外れを見落とした。11 行は 2 人の読み手が読み直し、どれも「わからない」とは言っていない）。同じ質問・同じ言葉の行（業務の最終目的かだけが違う）は、正解も同じで
    なければならない（違えば止める）。SIDE に表の行に無い言葉があれば止める（書き違えた足しの正解を黙って捨てない）。"""
    missing = set(SIDE) - {(qtext, words) for qtext, words, *_ in ANSWER}
    if missing:
        raise ValueError(f"足しの正解（SIDE）に、表の行に無い言葉がある：{sorted(missing)}")
    out: dict = {}
    for qtext, words, ends, answers, _, why in ANSWER:
        acc = [a.removesuffix(HEDGED) for a in answers]
        got = {"accept": acc, "hedged": any(a.endswith(HEDGED) for a in answers), "end": list(ends), "why": why, **SIDE.get((qtext, words), {})}
        old = out.setdefault((qtext, words), got)
        if any(old[k] != got[k] for k in ("accept", "hedged", "end")):
            raise ValueError(f"同じ質問・同じ言葉の行で正解が違う：{qtext}「{words}」")
    return out


def _reading(cond: str, end: str):
    from meaning_judge import Reading
    hedged = cond.endswith(HEDGED)
    r = Reading(cond.removesuffix(HEDGED))
    r.end, r.end_lean, r.firm = end, (), not hedged
    return r


def _spoken(reply: dict | None) -> str:
    """返事で、お客様に聞こえる言葉（相づち＋録音・言い方）。"""
    if reply is None:
        return ""
    from phrases import ACK
    head = ACK[reply["ack"]] if reply.get("ack") else ""
    return head + "".join(p["text"] for p in reply.get("play") or []) + (reply.get("say") or "")


def answer_flow(row: tuple):
    """答えの段の行の言葉を、正解の読み（先頭）で流れの制御に当てる：(流れの制御, お客様に聞こえる言葉, 締めた理由・印の要約)。
    会話AIの報告は、正解の答えの報告（電話を終える読みがあれば、判定が先に当てる＝Flow._apply_one）。"""
    from flowctl import Flow, Question
    qtext, words, ends, answers, goal, _ = row
    q = question(qtext, goal)
    choices = q.conditions
    # 次の質問：どの答えのあとに伺っても筋が通るもの（✅ 2026-11-03 ユーザー：「どうして曜日で聞くの？「曜日や日時」ならわかるけど！それに何の目的で連絡するの？
    # それが分からないと警戒されるでしょ。」＝前は「ご連絡に都合のよい曜日はございますでしょうか？」。ご連絡の曜日や日時は、ご連絡してよいと言っていただいた
    # あとの業務の質問＝HedgedSalesTest.TALK の E1c）
    nxt = Question("Q2", "ご不明な点はございますでしょうか？", "yes_no", conditions=YES_NO)
    f = Flow([q, nxt], play_recordings=True)
    f.use_checks("judge")
    f.on_callee(); f.on_heard(words); f.on_check("judge", _reading(answers[0], ends[0]))
    # 判定の読みだけで動く1か所（電話を終える言葉・待つ／かけ直す）が返さなければ、正解の答えの報告で当てる（⛔ 11-06 3時台に外した：言葉に「あとで」が
    # あれば、かけ直しの報告で当てる言葉の形の特別扱い＝正解を「あとでかけ直す」にして、judged_turn が判定の読みで応じる形にした）
    r = f.judged_turn(voiced=False)   # 会話AIは声を出していない（判定の読みだけで動く報告の無いターン）
    if r is None and answers[0].removesuffix(HEDGED) in choices + ("unknown", "refused"):
        r = f.apply("record_answer", {"question_id": "Q1", "condition_id": answers[0].removesuffix(HEDGED)})
    return f, _spoken(r) + _ahead_spoken(f, r), _marks(f)


def _ahead_spoken(f, reply: dict | None) -> str:
    """返事が、かけ直しの時刻・日の質問を伺う番（かけ直しを頼んだお話を、判定が時刻・日の質問について読み直す＝flowctl.Flow._callback_ahead・決まりで直す (3)）なら、
    読み直しのあとにお客様に聞こえる言葉（表の言葉には時刻・日が無い＝判定は「決められない」と読み、会話AIが質問へ戻る＝流れの制御が時刻・日の質問を流す）。
    ほかは空。"""
    from flowctl import AHEAD_CHECKS, CALLBACK_IDS
    if not (reply and reply.get("ahead_offer") and reply.get("question_id") == CALLBACK_IDS["time"]):
        return ""
    f.ahead_context()
    for k in AHEAD_CHECKS:
        if k.split("@")[0] in f.checks_used:
            f.on_check(k, UNDECIDED, f.callee_turns)
    return _spoken(f.apply("replay_opening", {"part": "resume"}))


def calling_flow(row: tuple):
    """お呼びする段の行の言葉を、正解の読みで流れの制御に当てる（会話AIの報告は「取り次いでいただけなかった」＝判定が理由を決める）。
    電話を終える言葉でない行は None（呼んでいる段の今の決まりで、ほかの項目の読みが決める）。"""
    _, words, ends, _, _ = row
    if ends[0] == END_NONE:
        return None
    from flowctl import Flow
    import g59_flow_check as fc
    b = fc.b_full   # 測りの業務（フルネームのある形）の唯一の定義（g59_flow_check.b_full。⛔ d8d03a78 まで、ここにも同じ定義を書いていた＝頼み rv140/AB の (2) の (b)）
    f = Flow(b.questions, play_recordings=True, opening=b.replay_parts(), target=b.replay_parts())
    f.use_checks("judge")
    f.on_callee(); f.on_heard(words); f.on_check("judge", _reading(UNDECIDED, ends[0]))
    r = f.apply("request_end", {"reason": "not_transferred"})
    return f, _spoken(r), _marks(f)


def _marks(f) -> str:
    """締めた理由と、残る印（もうかけない・苦情・その業務はかけない）の短い要約（表の欄）。"""
    out = []
    if f.end_reason:
        out.append(f"締め（{f.end_reason}）")
    for kind, name in (("do_not_call", "もうかけない"), ("complaint", "人に引き継ぐ"), ("goal_refused", "この業務はかけない")):
        if f.facts.get(kind):
            out.append(name)
    return "・".join(out) or "続ける"


# プログラムが何も足さない行（電話を終える言葉ではなく、答えでもない）：会話AIが受け答えをし（考えている方には「はい。」とだけ）、黙られたら流れの制御が
# 場面ごとの一言を流す（考えている方は5秒で「何か疑問や気になる点が…」・さらに5秒で「いかがなさいましたか？」＝flowctl.QUIET_STEPS・Flow.on_silence）
QUIET = "（プログラムは何も足さない。会話AIの受け答えのまま、いまの質問の答えを待つ）"


def table() -> str:
    """ご覧いただく表（markdown）。お客様に聞こえる言葉は、作りから出したもの。"""
    lines = ["| こちら | お客様 | 電話を終える言葉か | 答え | このあとお客様に聞こえること | 結果 |", "|---|---|---|---|---|---|"]
    for row in ANSWER:
        f, heard, marks = answer_flow(row)
        q = row[0] + ("（最終目的）" if row[4] else "")
        lines.append(f"| {q} | {row[1]} | {'／'.join(row[2])} | {'／'.join(row[3])} | {heard or QUIET} | {marks} |")
    lines += ["", "| 電話口の方 | お客様 | 電話を終える言葉か | このあとお客様に聞こえること | 結果 |", "|---|---|---|---|---|"]
    for row in CALLING:
        got = calling_flow(row)
        heard, marks = (got[1], got[2]) if got else ("（電話を終える言葉ではない。今の呼んでいる段の決まりのまま）", "続ける")
        lines.append(f"| {row[0]} | {row[1]} | {'／'.join(row[2])} | {heard} | {marks} |")
    return "\n".join(lines)


if __name__ == "__main__":
    print(table())
