"""整理が済んでからの1回の測りに足す試しの言葉と正解（見直し (12) の組 Q。流さない＝料金はかけない。測る前に料金の見込みを出して伺う）。

材料：担当 D の報告の「測りに足す試しの言葉（正解つき）」と、帳面 docs/team/CL-1.md の 1 の (1) の ⏳ にある例（分身 E・G・I・J・N・O1・O2・Q の行）。
- ANSWER_WORDS：答えの段（G-53）。正解の表の行の形（g53_meaning._label の label＝accept・hedged・別のお話か AWAY・こちらへ尋ねた問いか QUERY・
  声かけへのお返事だけか PROMPT_REPLY）。読む口は g53_meaning.set_r（数えは flow_miss の1か所）。
- GROUND_WORDS：中身の判定（G-63）。いま伺っている質問と選択肢を添えた形（通話と同じ make_grounder の ground(…, asking, options)）と、選択肢だけ外した形・
  質問を添えない形の正解。読む口は g63_grounding.touch_cases。
- IMPORT_WORDS：取込の中身の判定（askitems.grounded の same＝元の質問と同じことを伺っているか）。
- TALK_WORDS：会話AIの通話の試験（G-57）の別のお話。会話AIへの決まり（flowctl.TALK_RECEIVE）の例の言葉（「うちは月曜が定休日なんですよ。」）と違う言葉を
  主に使い、支払日の事実の無い業務（KNOW_NO_DAY）でも流す（決まりの例を写しただけで通らないように＝担当 D の D-13）。
- REREAD_ROWS（答えの段）・CALLING_WORDS の印 REREAD の行：判定に送る文が 2026-11-20 に変わった所に足した行（分身 S）。正解は答えを伏せた読み手2人の読み直し待ち。
  場面の印が UNBUILT_SCENES の行は、測りの数えで場面を組む作りがまだ無い＝「場面を組めない」と出す（送らない・数えない）。
"""
from __future__ import annotations

from flowctl import (ABOUT, ABOUT_DEPARTED, ABOUT_NO_SUCH, ABOUT_NONE, ABOUT_WRONG, AWAY, AWAY_NO, AWAY_YES, CALL_LATER, DONT_KNOW_LABEL, HESITATING, KEEP_ON,
                     LADDER_NEITHER, PHONE, PHONE_NONE, PROMPT_REPLY, PROMPT_REPLY_NO, PROMPT_REPLY_WAVER, PROMPT_REPLY_YES, QUERY, QUERY_NO, QUERY_YES,
                     REFUSED_LABEL, SPEAKER, SPEAKER_SAME, TARGET_IS_AWAY, THINKING, VALUE_CHOICE, WAIT_NOW, WHO, WHO_RECEPTION, WHY_NOT_SAID, WRONG,
                     WRONG_NOT_SAID, Question, accepted_line, alternatives_form, aside_form, callback_question, pain_question, since_form)
from grounding import TOUCHED, UNTOUCHED, one_touched
from meaning_judge import UNDECIDED
from phrases import PAIN_ASK

# 正解の読み直し待ちの印（2026-11-20 分身 S＝まとめ役 CL-1 の頼み rv176/S）：判定に送る文が変わった所（11-20 日本時間4時台〜6時台＝帳面 1 の (1) の ⏳）に足した行の正解は、
# 判定に見せる言葉と説明の文だけから分身 S が決めた（境目はどちらも正解）。答えを伏せた読み手2人の読み直し（帳面 0-3）が届くまで、この印を残す（届いたら外す・直す）
REREAD = "⏳ 読み直し待ち（答えを伏せた読み手2人・rv176/S）"
# 場面の印の場面を、測りの数え（g53_meaning._flow_scene）で組む作りがまだ無い行の場面の名前（数えずに「場面を組めない」と出す＝送らない・数えない。組む作りを足すまで）
LADDER_SCENE = "日時の2択の段（平日と答えたあとの曜日の2択）"
WRONG_ASK_SCENE = "番号違いを伺う一言のあと（答えの段・判定の組が割れた）"
WAIT_SCENE = "待っていただく一言のあと（答えを言ったあと、こちらの声が出ていない）"
PAIN_SCENE = "顧客獲得型の悩みの伺い（Y2）"
CONCERN_SCENE = "顧客獲得型の本心を伺う一言（D2）"
UNBUILT_SCENES = (LADDER_SCENE, WRONG_ASK_SCENE, WAIT_SCENE, PAIN_SCENE, CONCERN_SCENE)

PAY = "お支払いはカードと振込のどちらがよろしいでしょうか？"
PAY_REWORDED = "お支払いの方はカードと振込のどちらにされますか？"
PAY_LISTED = "カードになさいますかそれともお振込の方がよろしいでしょうか？"
CARD_PAY = ("カード", "振込")

# 業務の知っていること（中身の判定の材料）。支払日の事実のある業務と無い業務
KNOW = ["お支払い：カード・振込のどちらでも承っております。振込は月曜日以外でもご対応いただけます。カードは曜日を問わずお使いいただけます。",
        "振込手数料はお客様のご負担となります。", "ご予算：お客様ごとに異なりますのでご相談させていただいております。"]
KNOW_NO_DAY = ["お支払い：カード・振込のどちらでも承っております。", "振込手数料はお客様のご負担となります。"]


def questions() -> dict[str, Question]:
    """試しの言葉の質問（番号 → 質問）。形は流れの制御の1か所（aside_form・alternatives_form・since_form・accepted_line）から作る＝本番でお客様が聞く文。"""
    pay = Question("R1", PAY, "choice", conditions=CARD_PAY, required=True, reworded=PAY_REWORDED, alternatives=PAY_LISTED)
    budget = Question("R3", "ご予算はおいくらほどでしょうか？", "amount", conditions=("答えた",), required=True, since="ご予算はおいくらほど")
    person = Question("R4", "ご担当者様のお名前を教えていただけますでしょうか？", "name", conditions=("答えた",), required=True, asks_fact=True,
                      since="ご担当者様のお名前")
    # お知らせの中身を常体にした句（matter_clause）は、聞き取れないまま「-」にしたお知らせの締めの前の確かめ「（句）件のお返事のお声が聞き取れなかったのですが…」に
    # 入る（11-15 日本時間11時59分07秒の (3)。名詞と同じく取込で決まったとした例＝お知らせの文の中身を常体にした句）
    approval = Question("R7", "キャンセルについてご了承いただけますでしょうか？", "yes_no", conditions=("はい", "いいえ"), required=True, approval=True,
                        announce="当日のキャンセルは前日までにご連絡いただく必要がございますのでご了承ください。", matter="キャンセル",
                        matter_clause="当日のキャンセルは前日までにご連絡いただく必要がある")
    # H2＝待つかかけ直すかの伺い（システムの質問・本番の1か所 callback_question）。場面は支払いの質問（R1）のあとに流れの制御が伺う所（g53_meaning._flow_scene）
    return {"R1": pay, "R2": Question("R2", "ご訪問の日にちはいつがよろしいでしょうか？", "date", conditions=("答えた",), required=True),
            "R3": since_form(budget), "R4": since_form(person), "R5": aside_form(pay), "R6": alternatives_form(pay), "R7": approval,
            "H2": callback_question("wait_or_later"),
            # 金額を伺う質問（言い直しへのお返事の試しの言葉＝帳面 1 の (1) の分身 N の行の「金額の言い直し」。言い直しの文は本番の1か所 Flow._ask_confirm）
            "R8": Question("R8", "ご予算はおいくらくらいでしょうか？", "amount", conditions=("答えた",), required=True),
            # 日時の2択の段（分身 N の伺い60）・顧客獲得型の悩みの伺いと本心を伺う一言（分身 P）の質問（どれも本番の1か所で作る形。場面は LADDER_SCENE などの印＝まだ組めない）。
            # 悩みの伺いの悩みは、試験の顧客獲得型の業務の1つ目の悩み（test_offline.ACQUIRE_PAINS）と同じ言葉
            "C1": callback_question("later_time", answering=True),
            "Y2": pain_question("ドライバーさんの人手不足", PAIN_ASK.format(who="御社"), "pain_ask_company"),
            "D2": callback_question("concern")}


# わからない・答えたくない理由の正解（label の reason＝g53_labels.json の行と同じ欄・同じ決め方＝F5 の 11-15）：答えが「決められない」を含む行は、流れの制御が
# 理由の読みを使う（Flow._not_deciding）＝理由も正解を持つ。流れの制御で同じ動きになる理由の名前を並べる（理由を言っていない言葉＝相づち・別のお話・ほかのことを
# 話した・答えた、は「とは言っていない」「理由を言わなかった」「決められない」のどれでも同じ動き。迷っている言葉は「まだ決まっていない」との境目）
WHY_OTHER = (WHY_NOT_SAID, DONT_KNOW_LABEL["unspecified"], UNDECIDED)
WHY_HESITATING = (DONT_KNOW_LABEL["undecided"], *WHY_OTHER)


def _row(qid: str, text: str, accept, away=None, query=QUERY_NO, prompt=None, hedged=False, asked: str | None = None, why: str = "",
         reason=None, wrong=None, scene=None) -> dict:
    label = {"accept": list(accept), "why": why}
    if hedged:
        label["hedged"] = True
    if reason:
        label["reason"] = list(reason)
    for key, value in ((AWAY, away), (QUERY, query), (PROMPT_REPLY, prompt), (WRONG, wrong)):
        if value is not None:
            label[key] = [value]
    # scene＝場面の印（g53_meaning._connected_scene：お呼びした方に繋いだあとの答えの段。無ければ目的のお客様のいない業務の最初の質問＝今までの行）
    return {"set": "R", "maker": "見直し (12)", "qid": qid, "text": text, "asked": asked, "label": label} | ({"scene": scene} if scene else {})


def restated_line(q: Question) -> str:
    """値を伺う質問 q の言い直しの文（本番の1か所 Flow._ask_confirm が流す文＝測りの場面 g53_meaning.restated と同じ。判定に見せる形 Flow._judge_text）。"""
    from flowctl import Flow
    from g53_meaning import _scene_value, restated
    f, _ = restated(q, "答えた", _scene_value(q))
    return Flow._judge_text(f, f.awaiting_say)


def answer_words() -> list[dict]:
    """答えの段（G-53）の試しの言葉と正解。"""
    qs = questions()
    accepted = accepted_line(qs["R7"])[1]
    unheard = accepted_line(qs["R7"], heard=False)[1]
    budget_again = restated_line(qs["R8"])
    return [
        # 担当 D の表（基本の質問）
        _row("R1", "うちは月曜が定休日なんですよ。", [UNDECIDED], AWAY_YES, why="聞かれたことと関わりのないご事情", reason=WHY_OTHER),
        _row("R1", "いやあ最近は暑くてねえ。", [UNDECIDED], AWAY_YES, why="世間話", reason=WHY_OTHER),
        _row("R1", "そういえばこの前の台風、大変だったんですよ。", [UNDECIDED], AWAY_YES, why="世間話", reason=WHY_OTHER),
        _row("R1", "そうですね。", [UNDECIDED, THINKING], AWAY_NO, reason=WHY_OTHER,
             why="相づちだけ＝答えの話。言いよどみ・言い終えていない言葉＝考えている途中も正解（11-06 11時19分55秒「(1) ア」で足した選べるもの・F5 の 11-15）。"
                 "「わからない」（unknown）は、わからない・まだ決めていない、とは言っていないので入れない（選べるものの説明のとおり）"),
        _row("R1", "振込じゃないほうがいいかなとは思いますけど", ["カード"], AWAY_NO, hedged=True, why="寄った考え（言い切っていない）"),
        _row("R1", "うーん、どっちがいいですかねえ。", [HESITATING, THINKING, UNDECIDED], AWAY_NO, None, reason=WHY_HESITATING,
             why="迷っている・考えている途中・こちらの考えを尋ねた（「〜ですかね」＝11-06 4時11分28秒の2つの意味）の境目（F5 の 11-15）。問いは付けない"
                 "＝どちらの読みも正解（正解の表の「〜のほうがいいですかね」の行と同じ）。「わからない」（答えを持っていない）とは言っていないので外した"
                 "（F4 の (c) の 11-15・2人の読み手がそろった）"),
        _row("R1", "振込だと手数料はどちら持ちですか？", [UNDECIDED], AWAY_NO, QUERY_YES, why="こちらへ尋ねた問い", reason=WHY_OTHER),
        _row("R1", "うちは振込だと手間なんですよね。", [UNDECIDED, "カード"], AWAY_YES, hedged=True, reason=WHY_OTHER,
             why="答えの手がかりだけのご事情＝別のお話（✅ 2026-11-15 日本時間21時55分35秒「伺い26：ア」）。振込を避ける向きに寄った考え"
                 "（カード・言い切っていない）とも読める＝どちらの読みも正解（2026-11-16 rv38＝答えを伏せた読み手2人＋CL-1。直す前の値＝"
                 "答え [決められない]・答えの話）"),
        # ほかの形の質問（日にちの質問への答えの手がかりだけのご事情）
        _row("R2", "うちは月曜が定休日なんですよ。", [UNDECIDED], AWAY_YES, reason=WHY_OTHER,
             why="日にちの答えの手がかりだけのご事情＝別のお話（✅ 2026-11-15 日本時間21時55分35秒「伺い26：ア」はこの言葉そのもの。"
                 "2026-11-16 rv38。直す前の値＝答えの話）"),
        _row("R3", "はい、決まりました。", [UNDECIDED], AWAY_NO, why="その後の形に、値の無い「決まりました」", reason=WHY_OTHER),
        _row("R3", "うちは月曜が定休日なんですよ。", [UNDECIDED], AWAY_YES, why="その後の形への別のお話", reason=WHY_OTHER),
        _row("R4", "はい、分かりました。", [UNDECIDED], why="事実を伺うその後の形に、値の無い「分かりました」", reason=WHY_OTHER),
        _row("R4", "はい、田中です。", ["答えた"], why="事実を言った"),
        _row("R5", "そうなんですね。", [UNDECIDED], AWAY_NO, why="お話に沿ったひとことのあとの相づちだけ", reason=WHY_OTHER),
        _row("R5", "じゃあ振込で。", ["振込"], AWAY_NO),
        _row("R6", "はい。", [UNDECIDED], AWAY_NO, why="並べた文への「はい」はどれも選んでいない", reason=WHY_OTHER),
        _row("R6", "お振込で。", ["振込"], AWAY_NO),
        _row("R6", "カードかなあ。", ["カード"], AWAY_NO, hedged=True),
        # 言い直し・最終の確かめ（分身 O2・N の行）
        # ご承認の質問（はい／いいえ）の最終の確かめは、判定の選べるものが「はい」「いいえ」（⛔ F5 の 11-15 まで、値の言い直しへの返事の名前 affirmed・denied で
        # 書いていた＝判定が選べない名前＝正しい読みも外れと数えた）
        {**_row("R7", "はい、大丈夫です。", ["はい"], asked=accepted, why="最終の確かめへのご了承"), "set": "D"},
        {**_row("R7", "いや、それはちょっと困るんですけど。", ["いいえ"], asked=accepted, why="ご了承いただけない"), "set": "D"},
        {**_row("R7", "はい、大丈夫ですよ。", ["はい"], asked=unheard, why="聞き取れなかったお知らせの最終の確かめへのご了承（分身 O2）"), "set": "D"},
        # 黙ったあとの一言のあと（分身 N・O2）
        _row("R7", "あ、それとですね、うちは月曜が定休日なんですよ。", [UNDECIDED], AWAY_YES, prompt=PROMPT_REPLY_NO, why="声かけへのお返事だけではない別のお話",
             reason=WHY_OTHER),
        _row("R7", "いえ、大丈夫です。", [UNDECIDED], AWAY_NO, prompt=PROMPT_REPLY_YES, why="声かけへのお返事だけ", reason=WHY_OTHER),
        # 帳面 1 の (1) の「この1回の測りに足す」と書かれた答えの段の言葉（分身 Y2 が入れた。正解は帳面に書かれたとおり＝お言葉・決まった説明の文から決まる）
        # 言い直し・最終の確かめへのお返事の別のお話か（分身 N＝5a7f04ce・見直し (12) の A-9・B-3）：金額の言い直し・ご承認のお知らせの最終の確かめ
        {**_row("R8", "うちは月曜が定休日なんですよ。", [UNDECIDED], AWAY_YES, asked=budget_again, why="言い直しへの別のお話（分身 N の行）", reason=WHY_OTHER),
         "set": "D"},
        {**_row("R8", "はい、大丈夫です。", ["affirmed"], AWAY_NO, asked=budget_again, why="言い直しを認めた＝答えの話（答えは合っている。分身 N の行）"),
         "set": "D"},
        {**_row("R7", "そういえば来週は祝日なんですよ。", [UNDECIDED], AWAY_YES, asked=accepted, why="最終の確かめへの別のお話（分身 N の行）", reason=WHY_OTHER),
         "set": "D"},
        {**_row("R7", "あ、はい、分かりました。", ["はい"], AWAY_NO, asked=accepted, why="最終の確かめへのご了承＝答えの話（分身 N の行）"), "set": "D"},
        # お知らせへの最初のお返事の別のお話か（見直し (13) の持ち場 C の C4）
        _row("R7", "なるほど。", [UNDECIDED], AWAY_NO, why="お知らせへの相づちだけ＝答えの話（決められない。C4）", reason=WHY_OTHER),
        _row("R7", "うちは月曜が定休日なんですよ。", [UNDECIDED], AWAY_YES, why="お知らせへの別のお話（C4）", reason=WHY_OTHER),
        # 別のお話か の答えの話（分身 E＝092f2317：分からない・答えたくない、は答えの話）
        _row("R1", "ちょっと分からないですね。", ["unknown"], AWAY_NO, why="分からないとだけ言った＝答えの話（分身 E の行）",
             reason=(DONT_KNOW_LABEL["unspecified"],)),
        _row("R1", "それは言いたくないです。", ["refused"], AWAY_NO, why="言いたくないとだけ言った＝答えの話（分身 E の行）",
             reason=(REFUSED_LABEL["unwilling"],)),
        # 待つかかけ直すか（H2）を伺っている間の言葉（見直し (13) の D8＝rv70/r13d/words.md の案・分身 Y2 が入れた）。正解は言葉・選べるものの説明（KEEP_ON＝
        # 「待つのも改めておかけ直しするのも要らず、そのままお話を続けた（その前のお話に答えた・もう大丈夫だと言った など）」・WAIT_NOW＝「このまま電話をつないで
        # お待ちしてよい」・CALL_LATER＝「改めておかけ直ししてよい」）・ご決定（本書 9-5 の W1 の段の (3)・11-05 日本時間23時56分48秒「(ア)」）だけから。
        # 別のお話かは付けない（どちらの読みも正解＝away_truths の決まり）。決まらない行は TO_READERS
        _row("H2", "あ、もう大丈夫です。どうぞ続けてください。", [KEEP_ON], why="もう大丈夫と言い、続きを促した（説明の「もう大丈夫だと言った」）"),
        _row("H2", "いえ、今ちょうど終わりましたので、お話どうぞ。", [KEEP_ON], why="用事が終わり、続けてよいと言った（待つのも要らない）"),
        _row("H2", "大丈夫ですよ、そのまま話してください。", [KEEP_ON], why="待つのもかけ直すのも要らず、そのまま話してと言った"),
        _row("H2", "あ、さっきの件ですけど、うちはカードでお願いします。", [KEEP_ON],
             why="伺う前のお話（支払いの質問）に答えた（説明の「その前のお話に答えた」）"),
        _row("H2", "じゃあ、少しだけ待っていてもらえますか。", [WAIT_NOW], why="このまま待ってほしいと言った"),
        _row("H2", "今ちょっと無理なので、また後でかけてください。", [CALL_LATER], why="あとでかけ直してほしいと言った"),
        *WRONG_ANSWER_ROWS,
        *REREAD_ROWS,
    ]


# 番号違いの試しの言葉（答えの段・目的のお客様のいない業務の最初の質問の段＝G-53。分身 AC＝まとめ役 CL-1 の頼み rv140/AC の (2)）。正解は説明の文と決まりの文の言葉だけ
# から決める：番号違いの説明は1か所（flowctl.wrong_number_choice）＝会社・お店の電話（と、どなたもお呼びしない業務）は「番号の間違いではないか・そういう会社やお店ではない」、
# ご本人の電話（お呼びした方に繋いだあとの個人のお客様・携帯電話の番号）は「お呼びした方の電話ではない（そういう方はいない・番号の間違いではないか）」。番号違いを読む項目は
# 答えとは別の項目 WRONG（答えの段だけ）。答えは、どの行も質問に答えていない＝「決められない」（理由は言っていない）。電話を終える言葉かは付けない（＝「言っていない」＝
# 説明「この電話を続けられない・続けたくない、とは言っていない」のとおり）。別のお話か（AWAY）は付けない（どちらの読みも正解＝番号違いと読めば、別のお話かで動きは変わらない
# ＝締める）。問いの形の言葉の「こちらへ尋ねた問いがあるか」は付けない（番号違いを伝える問いの形＝こちらの答えを求めているかは言葉から決まらない＝どちらの読みも正解）。
# ⚠️ 目的のお客様のいない業務には「ご本人の電話」の場面が無い（お呼びした方がいない＝Flow._own_phone は携帯電話の番号でも偽＝判定に見せる文は会社の電話と同じ）
WRONG_ANSWER_ROWS = [
    # 目的のお客様のいない業務の最初の質問（場面の印なし＝今までの行と同じ場面）
    _row("R1", "うちはそういう会社じゃないですよ。番号をお間違えだと思います。", [UNDECIDED], wrong=ABOUT_WRONG, reason=WHY_OTHER,
         why="会社・お店の電話の番号違いの説明「番号の間違いではないか・そういう会社やお店ではない」をそのまま言った"),
    _row("R1", "うちはお店じゃないですけど、番号をお間違えではないですか？", [UNDECIDED], query=None, wrong=ABOUT_WRONG, reason=WHY_OTHER,
         why="携帯電話の番号にかけた目的のお客様のいない業務（どなたもお呼びしない＝説明は会社・お店の電話の文）：そういうお店ではない・番号の間違いではないか"),
    # 繋いだあとの答えの段・会社の電話
    _row("R1", "え、うちは山田ですけど、番号をお間違えではないですか？", [UNDECIDED], query=None, wrong=ABOUT_WRONG, reason=WHY_OTHER, scene="繋いだあと・会社の電話",
         why="番号の間違いではないか、と伝えた（分身 Z の書き置き rv124/Z の 4 の (b) の例）"),
    _row("R1", "そういう者はおりませんが。", [UNDECIDED], wrong=WRONG_NOT_SAID, reason=WHY_OTHER, scene="繋いだあと・会社の電話",
         why="会社の電話の番号違いの説明は「番号の間違い・そういう会社やお店ではない」だけ（「そういう方はいない」は入れていない＝お呼びする段なら在籍していない）"
             "＝番号違いとは言っていない"),
    # 繋いだあとの答えの段・ご本人の電話
    _row("R1", "え、そういう方はいませんけど。", [UNDECIDED], wrong=ABOUT_WRONG, reason=WHY_OTHER, scene="繋いだあと・ご本人の電話",
         why="ご本人の電話の番号違いの説明「お呼びした方の電話ではない（そういう方はいない・…）」のとおり"),
    _row("R1", "番号をお間違えではないですか？", [UNDECIDED], query=None, wrong=ABOUT_WRONG, reason=WHY_OTHER, scene="繋いだあと・ご本人の電話",
         why="ご本人の電話の番号違いの説明「番号の間違いではないか、と伝えた」"),
    _row("R1", "じゃあ振込でお願いします。", ["振込"], AWAY_NO, wrong=WRONG_NOT_SAID, scene="繋いだあと・ご本人の電話",
         why="答えた（番号違いの説明の「と伝えた」に当たる言葉が無い＝番号違いとは言っていない。番号違いの説明の替わる場面の、ふつうの答えの行）"),
]


def _reread(*args, **kw) -> dict:
    """読み直し待ちの行（REREAD の印つき。形は _row と同じ）。"""
    return {**_row(*args, **kw), "reread": REREAD}


# 判定に送る文が変わった所（2026-11-20 日本時間4時台〜6時台・帳面 1 の (1) の ⏳）の試しの言葉（分身 S＝まとめ役 CL-1 の頼み rv176/S の (b)(e)(f)）。正解は判定に見せる言葉と
# 説明の文（選べるものの説明・質問のあとにこちらが言った一言の読み方の注意書き）だけから決めた・境目はどちらも正解（理由は行ごと）。⏳ どの行も、答えを伏せた読み手2人の読み直し待ち
# （REREAD）・場面の印の場面は、測りの数え（g53_meaning._flow_scene）で組む作りがまだ無い＝数えずに「場面を組めない」と出す（送らない・数えない。組む作りを足すまで）
REREAD_ROWS = [
    # (b) 日時の2択の段（分身 N の伺い60＝選べるものに「迷っている」「どちらも合わない」＝flowctl.LADDER_NEITHER）。場面は、かけ直しを頼まれ、平日と土日の2択に「平日」と
    # 答えたあとの曜日の2択（「金曜日と火曜日ではどちらがよろしいでしょうか？」＝電話の日 2026-11-19 木曜日 10時・11-23 は祝日。試験 DateTimePairsTest の場面）
    _reread("C1", "どちらもちょっと難しいですね。", [LADDER_NEITHER], AWAY_NO, scene=LADDER_SCENE,
            why="並べた2つのどちらも難しいと言った（「どちらも合わない」の説明のとおり）"),
    _reread("C1", "うーん、どっちがいいかなあ。", [HESITATING, THINKING], AWAY_NO, scene=LADDER_SCENE,
            why="2つのあいだで迷っていると言った＝迷っている。言いよどみで、このあと続けて答えそう＝考えている途中、とも読める（正解の表の R1「うーん、どっちがいいですかねえ。」"
                "と同じ境目。こちらの考えは尋ねていない＝決められないは入れない）"),
    _reread("C1", "金曜日でお願いします。", [VALUE_CHOICE], AWAY_NO, scene=LADDER_SCENE, why="並べた2つの1つ（金曜日）を選んだ＝答えた"),
    _reread("C1", "来週の月曜日なら空いてます。", [VALUE_CHOICE], AWAY_NO, scene=LADDER_SCENE,
            why="2択の外の日を答えた＝答えた（日時の2択の段は2択の外の答えも受け取る＝Flow._ladder_take）。どちらも合わない、とは言っていない"),
    _reread("C1", "金曜日はちょっと…。", [UNDECIDED], AWAY_NO, scene=LADDER_SCENE, reason=WHY_OTHER,
            why="境目：1つ（金曜日）だけを避けた＝もう一方（火曜日）がよいとも、どちらも合わないとも言っていない＝決められない"),
    # (e) 番号違いを伺う一言のあと（分身 M の伺い72 エ＝meaning_judge.WRONG_ASK_NOTE。「はい」「そうです」は伺ったとおり＝番号違い）。場面は、支払いの質問（R1）への
    # 「うちはそういう会社じゃないですよ。番号をお間違えだと思います。」を判定の組の1つだけが番号違いと読み、こちらが「大変失礼いたしました。番号を間違えてお掛けして
# しまいましたでしょうか？」と伺ったあと（目的のお客様のいない業務の最初の質問）
    _reread("R1", "はい、そうです。", [UNDECIDED], AWAY_NO, wrong=ABOUT_WRONG, scene=WRONG_ASK_SCENE, reason=WHY_OTHER,
            why="番号違いかを伺った一言に「はい、そうです。」＝伺ったとおり番号違い（WRONG_ASK_NOTE）。支払いの質問には答えていない"),
    _reread("R1", "いえ、合ってますよ。", [UNDECIDED], AWAY_NO, wrong=WRONG_NOT_SAID, scene=WRONG_ASK_SCENE, reason=WHY_OTHER,
            why="番号は合っていると言った＝番号違いとは言っていない。支払いの質問には答えていない"),
    # (e) 待っていただく一言のあと（分身 M の伺い76 ウ＝flowctl.PROMPT_REPLY_WAVER・meaning_judge.WAIT_NOTE）。場面は、支払いの質問（R1）に答えたあと（「振込でお願いします。」）、
    # こちらの声が出ていないので「少々お待ちいただいてもよろしいでしょうか？」を流したところ
    _reread("R1", "えー、待つんですか、どうしようかなあ。", [UNDECIDED], AWAY_NO, prompt=PROMPT_REPLY_WAVER, scene=WAIT_SCENE, reason=WHY_OTHER,
            why="待つかどうかを迷っているだけ（質問への答えの迷いではない）＝答えは決められない・お返事だけかは「待つかどうかの迷いだけ」（WAIT_NOTE のとおり）"),
    _reread("R1", "はい、構いませんよ。", [UNDECIDED], AWAY_NO, prompt=PROMPT_REPLY_YES, scene=WAIT_SCENE, reason=WHY_OTHER,
            why="待つお願いへのお返事だけ（待ってよい）＝答えは言い直していない（WAIT_NOTE のとおり。このお願いへの「はい」は質問への「はい」ではない）"),
    # (f) 顧客獲得型の悩みの伺い（Y2）・本心を伺う一言（D2）の「質問の答えの話か、別のお話か」（分身 P＝flowctl.AWAY_ROLES に pain・concern）。場面は、試験の顧客獲得型の
    # 業務（PPainAskAgainTest・PConcernAwayTest の場面）
    _reread("Y2", "今日は暑いですねえ。", [UNDECIDED], AWAY_YES, scene=PAIN_SCENE, reason=WHY_OTHER, why="悩みの伺いと関わりのない世間話＝別のお話"),
    _reread("Y2", "うーん、どうでしょうねえ。", [UNDECIDED, THINKING], AWAY_NO, scene=PAIN_SCENE, reason=WHY_OTHER,
            why="悩みがあるとも無いとも言っていない＝決められない・言いよどみで、このあと続けて答えそう＝考えている途中、の境目。悩みの伺いへの返事＝答えの話"),
    _reread("D2", "今日は暑いですね。", [UNDECIDED], AWAY_YES, scene=CONCERN_SCENE, reason=WHY_OTHER, why="本心を伺う一言と関わりのない世間話＝別のお話"),
    _reread("D2", "説明が長くてよく分からないんですよ。", ["問題を言った"], AWAY_NO, scene=CONCERN_SCENE,
            why="こちらの話し方・進め方への気になる点を言った＝問題を言った・答えの話（「よく分からない」は説明のことで、答えを持っていないとは言っていない）"),
]


# お呼びする段（目的のお客様の判定＝G-59）の番号違いの試しの言葉（分身 AC＝頼みの (2)）。⛔ G-59 の正解の表（g59_calling_judge.LINES）はこの分身の持ち場ではない＝
# ここに置き、正解の表へ足す案は書き置き（rv140/AC/notes.md）。行＝(相手の言葉, 場面, 正解)。場面＝{"asked": 確かめる一言への返事か, "own_phone": ご本人の電話か}
# （判定の説明の文は flowctl.target_facets(asked, False, own_phone) の1か所）。正解＝{項目の名前: 正しい選べるものの集まり}（書いていない項目は数えない）。
# 正解は説明の文と決まりの文の言葉だけから決める（理由は行ごと）
CALLING_WORDS = [
    ("うちには岡田という者はおりませんが。", {"asked": False, "own_phone": False},
     {SPEAKER: {SPEAKER_SAME}, WHO: {WHO_RECEPTION}, PHONE: {PHONE_NONE}, ABOUT: {ABOUT_NO_SUCH}},
     "会社の電話：「そういう方はいない」の説明そのもの（退職・異動とは言っていない＝2026-11-20 分身 M が在籍していないから分けた選べるもの。伺い69 エ）・番号違いの説明は"
     "番号の間違い・そういう会社やお店ではないだけ。お呼びした方をほかの人として話した"
     "はじめに出た方＝受付・取次ぎの方（まとめ役 CL-1 の 11-19 日本時間19時台の直し。⛔ 分身 Z の案 F(同,受,無,番)）"),
    ("うちには岡田という者はおりませんが。", {"asked": False, "own_phone": True},
     {SPEAKER: {SPEAKER_SAME}, WHO: {WHO_RECEPTION}, PHONE: {PHONE_NONE}, ABOUT: {ABOUT_WRONG}},
     "ご本人の電話（携帯電話の番号）：番号違いの説明「お呼びした方の電話ではない（そういう方はいない・…）」・在籍していないの説明から「そういう方はいない」を外した"),
    ("そちらは番号をお間違えではないですか。うちはそういう会社ではありませんが。", {"asked": False, "own_phone": False},
     {SPEAKER: {SPEAKER_SAME}, PHONE: {PHONE_NONE}, ABOUT: {ABOUT_WRONG}},
     "会社の電話の番号違いの説明そのもの。話している方は誰かは付けない（受付・取次ぎの方の印＝ほかの人として話した・名前を確かめた などが無く、別の方は代わって出た方"
     "だけ＝説明のどれでも決まらない＝「決められない」とも読める。⛔ 分身 Z の案の「別の方」は、代わって出た方の説明）"),
    ("いえ、そういう方はいませんけど。", {"asked": False, "own_phone": True},
     {SPEAKER: {SPEAKER_SAME}, PHONE: {PHONE_NONE}, ABOUT: {ABOUT_WRONG}},
     "ご本人の電話（個人のお客様の呼ぶ一言「（名前）様のお電話でお間違えないでしょうか？」への返事）：そういう方はいない＝番号違い"),
    ("いいえ、違いますけど。", {"asked": True, "own_phone": False},
     {SPEAKER: {SPEAKER_SAME}, WHO: {WHO_RECEPTION}, PHONE: {PHONE_NONE}, ABOUT: {ABOUT_NONE}},
     "確かめる一言への「いいえ」：番号違い・在籍していないのどちらの言葉も無い＝事情は言っていない（分身 Z の書き置きの行）"),
    ("いいえ、違いますけど。", {"asked": True, "own_phone": True},
     {SPEAKER: {SPEAKER_SAME}, WHO: {WHO_RECEPTION}, PHONE: {PHONE_NONE}, ABOUT: {ABOUT_NONE}},
     "ご本人の電話でも、「違います」だけは「そういう方はいない・番号の間違いではないか」とは伝えていない＝事情は言っていない"),
    # 2026-11-20 分身 S（まとめ役 CL-1 の頼み rv176/S の (c)＝分身 M の「そういう方はいない」ABOUT_NO_SUCH・会社・お店の電話のはじめの場面だけ）。正解は言葉と説明の文だけから
    # 決めた（⏳ 読み直し待ち＝理由の頭の REREAD）
    ("そういう者はおりませんが。", {"asked": False, "own_phone": False},
     {SPEAKER: {SPEAKER_SAME}, WHO: {WHO_RECEPTION}, PHONE: {PHONE_NONE}, ABOUT: {ABOUT_NO_SUCH}},
     f"{REREAD}：会社の電話の呼ぶ一言への言葉：退職・異動・担当が替わったとは言わずに、そういう方はいないとだけ伝えた（「そういう方はいない」の説明そのもの）。"
     "お呼びした方を「そういう者」とほかの人のこととして話した＝受付・取次ぎの方（「うちには岡田という者は…」の行と同じ）"),
    ("いえ、そういう者はおりませんが。", {"asked": True, "own_phone": False},
     {SPEAKER: {SPEAKER_SAME}, WHO: {WHO_RECEPTION}, PHONE: {PHONE_NONE}, ABOUT: {ABOUT_NO_SUCH}},
     f"{REREAD}：会社の電話の確かめる一言への言葉：確かめた問いに「いえ」と答え（受付・取次ぎの方の説明の「いいえ」）、そういう方はいないとだけ伝えた"),
    ("岡田はもうおりませんが。", {"asked": False, "own_phone": False},
     {SPEAKER: {SPEAKER_SAME}, WHO: {WHO_RECEPTION}, PHONE: {PHONE_NONE}, ABOUT: {ABOUT_DEPARTED, TARGET_IS_AWAY}},
     f"{REREAD}：境目：「もう」は、退職・異動などでもういない（在籍していない）とも、今日はもう帰った・出た（不在）とも読める。お呼びした方がいたことは言っている＝"
     "「そういう方はいない（その名前の方に心当たりがない）」ではない。お呼びした方をほかの人のこととして話した＝受付・取次ぎの方"),
]


# 正解が言葉・説明の文・ご決定から決まらない試しの言葉（正解を付けない＝答えを伏せた読み手へ回す。数えには入れない）。場面は answer_words と同じ形（qid の質問）
TO_READERS = [
    {"set": "D", "maker": "帳面 1 の (1) の分身 N の行", "qid": "R8", "text": "うーん、どうでしょうねえ。", "asked": None,
     "why": "帳面には「答えの話」とだけ書かれ、答えは書かれていない。金額の言い直しへのお返事で、「決められない」（「はい、いかがなさいますか？」）・「考えている途中」"
            "（「はい。」）・「わからない」（「それではまだ確定ではないと承っておきますね。」）で、お客様に聞こえる言葉が分かれる（伺いの候補 (7) のご決定は最初の"
            "答えの場面＝聞き直しの場面）"},
    {"set": "R", "maker": "見直し (13) の D8", "qid": "H2", "text": "うーん、どうしようかな。", "asked": None,
     "why": "どれとも言っていない。「決められない」（「はい、それではこのままお返事を待っております。」）と「考えている途中」（「はい。」）で、お客様に聞こえる"
            "言葉が分かれる（11-07 日本時間18時台に業務の質問の「うーん、どうしようかな」へ考えている途中を足したのは、H2 の場面ではない）"},
]


# 中身の判定（G-63）：いま伺っている質問「お支払いの方はカードと振込のどちらにされますか？」・選択肢カード・振込。touch＝選択肢を添えたときの正解・
# touch_plain＝質問だけ添えて選択肢を添えないときの正解（1つの選択肢に触れても「触れている」）・質問を添えない形は通す・止めるだけを数える
def _g(draft: str, want: bool, touch: str, asking: str = PAY_REWORDED, options=CARD_PAY, said: str = "うちは月曜が定休日なんですよ。") -> dict:
    plain = TOUCHED if touch not in (UNTOUCHED, "") else touch
    return {"said": said, "draft": draft, "want": want, "asking": asking, "options": list(options), "touch": touch, "touch_plain": plain}


GROUND_WORDS = [
    _g("さようでございますか。お振込でしたら月曜日以外でもご対応いただけますよ。", True, one_touched("振込")),
    _g("さようでございますか。カードでしたら曜日を問わずお使いいただけますよ。", True, one_touched("カード")),
    _g("さようでございますか。お支払いはカードでもお振込でも月曜日以外で問題ございませんよ。", True, TOUCHED),
    _g("さようでございますか。お支払いは月曜日以外でも問題ございませんよ。", True, UNTOUCHED),
    _g("さようでございますか。月曜日がお休みでいらっしゃるのですね。", True, UNTOUCHED),
    _g("さようでございますか。お振込ですね。", True, one_touched("振込")),
    _g("お振込は月曜日以外でもご対応いただけます。", True, one_touched("振込")),
    _g("本当に暑い日が続いておりますね。", True, UNTOUCHED, said="いやあ最近は暑くてねえ。"),
    _g("振込手数料はお客様のご負担となります。", True, one_touched("振込"), said="振込だと手数料はどちら持ちですか？"),
    _g("お支払いはカードと振込のどちらでも承っております。", True, TOUCHED, said="振込だと手数料はどちら持ちですか？"),
    _g("さようでございますか。ご予算はお客様ごとに異なりますのでご相談させていただいております。", True, TOUCHED,
       asking="ご予算はだいたいおいくらくらいでお考えですか？", options=(), said="うちは月曜が定休日なんですよ。"),
]

# 取込の中身の判定（askitems.grounded の same）：(元の質問, 選択肢, 型, 作った文, 同じことを伺っているか)
IMPORT_WORDS = [
    ("現在のお支払いはカードと振込のどちらでしょうか？", CARD_PAY, "choice", PAY_LISTED, False),
    (PAY, CARD_PAY, "choice", "お支払いの時期はいつ頃がよろしいですか？", False),
    (PAY, CARD_PAY, "choice", PAY_REWORDED, True),
    (PAY, CARD_PAY, "choice", PAY_LISTED, True),
]

# 会話AIの通話の試験（G-57）の別のお話：(業務の知っていること, お客様の言葉, 正解)。正解はどれも「お話の中身を受けたひとこと・知らない事実を言わない
# （中身の判定を通る）・問いかけと約束を入れない」。決まりの例の言葉は、支払日の事実の無い業務でだけ使う
TALK_WORDS = [
    (KNOW, "うちは水曜がお休みなんですよ。", "中身を受けたひとこと・知っていることだけ"),
    (KNOW, "最近ちょっと腰を痛めちゃってね。", "中身を受けたひとこと（お見舞い）・事実を言わない"),
    (KNOW_NO_DAY, "うちは水曜がお休みなんですよ。", "中身を受けたひとこと・支払日の事実を作らない"),
    (KNOW_NO_DAY, "うちは月曜が定休日なんですよ。", "中身を受けたひとこと・支払日の事実を作らない（決まりの例の答えを写さない）"),
]


def counts() -> dict[str, int]:
    """足した試しの言葉の数（表ごと）。G-63 は添える形ごと（選択肢つき・質問だけ・添えない）に流すので、その数も。"""
    return {"G-53": len(answer_words()), "G-53（読み手へ）": len(TO_READERS), "G-63": len(GROUND_WORDS), "G-63（3つの形で流す行）": len(GROUND_WORDS) * 3,
            "取込の中身の判定": len(IMPORT_WORDS), "G-57": len(TALK_WORDS), "G-59（正解の表へ足す案）": len(CALLING_WORDS),
            "G-53（読み直し待ち）": sum(1 for it in answer_words() if it.get("reread")),
            "G-53（場面を組む作りがまだ無い）": sum(1 for it in answer_words() if it.get("scene") in UNBUILT_SCENES),
            "G-59（正解の表へ足す案・読み直し待ち）": sum(1 for *_, why in CALLING_WORDS if why.startswith(REREAD))}
