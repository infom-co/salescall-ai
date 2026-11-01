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

from flowctl import END_ANGRY, END_HURRY, END_NONE, END_REASONED, END_REFUSED, END_UNCLEAR, END_WHO, UNDECIDED

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
CHOICES = {OFFER: YES_NO, PAMPHLET: YES_NO, SEMINAR: ("参加する", "参加しない"), RESERVED: YES_NO, DOCS: YES_NO, HURRY_Q: YES_NO,
           VISIT: YES_NO, CONTACT: ("電話", "メール"), PAY: ("カード", "振込")}
# 取込で作る言葉（意味の言葉・いまの話の向きを伺う言葉）。⏳ 本番は取込で文字のAI2つが作る（CL-2）＝ここは試しの仮の言葉
MEANINGS = {PAMPHLET: {"はい": "パンフレットをお送りして大丈夫だということ", "いいえ": "パンフレットはお送りしなくて大丈夫だということ"},
            RESERVED: {"はい": "ご予約はお済みということ", "いいえ": "ご予約はまだということ"}}
DOUBT = {PAMPHLET: {"はい": "パンフレットをお送りした方がいい", "いいえ": "パンフレットはお送りしない方がいい"}}
# こちらがすること（お客様がされること）の言葉（flowctl.Question.do_words）。⏳ 本番は取込で文字のAI2つが作る（CL-2）・CL-1 が全部を見る
DO = {PAMPHLET: {"はい": "パンフレットをお送り", "いいえ": "パンフレットの送付は控え"},   # ✅ 11-02 1時22分「パンフレットの送付は控え」
      PAY: {"カード": "お支払いはカードと", "振込": "お支払いはお振込みと"}}
FACTS = (RESERVED, DOCS)   # お客様に事実を伺う質問（flowctl.Question.asks_fact。本番は取込で文字のモデル2つが決める＝facts.decide）

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
    (CONTACT, "今ちょっと立て込んでるので、手短にお願いできますか。", (END_NONE,), (UNDECIDED,), False, "続けてよい（急ぎの印にしない）"),
    (CONTACT, "あとでかけ直してもらえますか。", (END_NONE,), (UNDECIDED,), False, "かけ直しの申し出（電話を終える言葉ではない）"),
    (PAY, "さっきから何なんですか、失礼じゃないですか。", (END_ANGRY,), (UNDECIDED,), False, "怒っている"),
    (PAMPHLET, "もういいです。", (UNDECIDED,), ("いいえ",), False, "20時56分・22時31分＝いまの話の向きを先に伺う"),
    (PAMPHLET, "んーん。", (END_NONE,), ("いいえ",), False, "22時31分＝「んーん」は「いいえ」"),
    (PAMPHLET, "うーん。", (END_NONE,), (UNDECIDED,), False, "22時31分＝悩んでいる最中（待って伺う）"),
    (PAMPHLET, "うーん、送ってもらおうかな…。", (END_NONE,), ("はい" + HEDGED,), False, "言い切っていないが向きは決まった答え（20時56分・23時59分）"),
    (PAMPHLET, "うーん、今回はいいかなあ…。", (END_NONE,), ("いいえ" + HEDGED,), False, "同じ（送らない向き）"),
    (PAY, "たぶんカードかなと思います。", (END_NONE,), ("カード" + HEDGED,), False, "同じ形を、希望を伺う選ぶ質問にも当てた（⏳ 伺う）"),
    (RESERVED, "たぶん予約してると思います。", (END_NONE,), ("はい" + HEDGED,), False, "事実を伺う質問（6時05分・依頼者がすること＝初めの値の記録）"),
    (RESERVED, "そういうお電話はちょっと……。", (UNDECIDED,), (UNDECIDED,), False, "電話を断っていそうだが言い切っていない・答えでもない"),
    (RESERVED, "結構です。", (UNDECIDED, END_NONE), (UNDECIDED,), False, "10-17「その言葉だけで通話の断りとして締めない」"),
]

# お呼びする段：(電話口の方, お客様の言葉, 電話を終える言葉かの正解, 理由)
CALLING = [
    ("受付の方", "うちはそういうのは間に合ってますので。", (END_REFUSED,), "営業のお断り"),
    ("受付の方", "営業のお電話でしたらお繋ぎできません。", (END_REFUSED,), "10-22＝営業のお断り（もうかけない）"),
    ("受付の方", "お名前だけではどの者か分かりかねますので、お繋ぎできかねます。", (END_WHO,), "23時59分＝部署・フルネームを伝えてお呼びし直す"),
    ("受付の方", "個人あてのお電話はお繋ぎしておりません。", (END_REASONED,), "10-22＝事情があって取り次げない（日を改めて1回）"),
    ("受付の方", "あいにく取り次ぐことはいたしかねます。", (END_UNCLEAR,), "11-01 5時26分＝理由を言わずに取り次げない"),
    ("受付の方", "岡田は本日終日外出しておりまして。", (END_NONE,), "お呼びした方の事情（電話を終える言葉ではない）"),
    ("受付の方", "結構です。", (UNDECIDED,), "10-17＝その言葉だけで断りとしない"),
    ("受付の方", "申し訳ございません、今ちょっとバタバタしておりまして。", (END_HURRY,), "急いでいる（受付の方でも同じ伺い）"),
    ("ご本人", "すみません、いま手が離せないので失礼します。", (END_HURRY,), "10-13「忙しい」とだけ＝急いでいる"),
    ("ご本人", "何度も同じ電話してきて、いい加減にしてください！", (END_ANGRY,), "怒っている・苦情"),
]


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
    choices = CHOICES[qtext]
    q = Question("Q1", qtext, "choice" if "どちら" in qtext else "yes_no", conditions=choices, meanings=MEANINGS.get(qtext, {}),
                 doubt_words=DOUBT.get(qtext, {}), do_words=DO.get(qtext, {}), asks_fact=qtext in FACTS,
                 final_goal=goal, declines=(choices[1],) if goal else ())
    nxt = Question("Q2", "ご連絡に都合のよい曜日はございますでしょうか？", "yes_no", conditions=YES_NO)
    f = Flow([q, nxt], play_recordings=True)
    f.use_checks("judge")
    f.on_callee(); f.on_heard(words); f.on_check("judge", _reading(answers[0], ends[0]))
    r = f.judged_end()
    if r is None and answers[0].removesuffix(HEDGED) in choices + ("unknown", "refused"):
        r = f.apply("record_answer", {"question_id": "Q1", "condition_id": answers[0].removesuffix(HEDGED)})
    elif r is None and "あとで" in words:
        r = f.apply("request_callback", {"why": "later"})
    return f, _spoken(r), _marks(f)


def calling_flow(row: tuple):
    """お呼びする段の行の言葉を、正解の読みで流れの制御に当てる（会話AIの報告は「取り次いでいただけなかった」＝判定が理由を決める）。
    電話を終える言葉でない行は None（呼んでいる段の今の決まりで、ほかの項目の読みが決める）。"""
    _, words, ends, _ = row
    if ends[0] == END_NONE:
        return None
    import dataclasses
    from flowctl import Flow
    from g22_opening import BUSINESS
    from instruction import Target
    b = dataclasses.replace(BUSINESS, target=Target(name="岡田", reading="おかだ", role="営業部部長", full_name="岡田太郎", full_reading="おかだたろう"))
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


# プログラムが何も足さない行（電話を終える言葉ではなく、答えでもない）：会話AIが受け答えをし（考えている方には「はい。」とだけ）、黙られたら電話接続側が
# 5秒で「どうなさいますか？」・さらに5秒で「いかがなさいましたか？」（phrases.THINKING_PROMPT）
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
