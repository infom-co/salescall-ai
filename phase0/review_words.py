"""整理が済んでからの1回の測りに足す試しの言葉と正解（見直し (12) の組 Q。流さない＝料金はかけない。測る前に料金の見込みを出して伺う）。

材料：担当 D の報告の「測りに足す試しの言葉（正解つき）」と、帳面 docs/team/CL-1.md の 1 の (1) の ⏳ にある例（分身 E・G・I・J・N・O1・O2・Q の行）。
- ANSWER_WORDS：答えの段（G-53）。正解の表の行の形（g53_meaning._label の label＝accept・hedged・別のお話か AWAY・こちらへ尋ねた問いか QUERY・
  声かけへのお返事だけか PROMPT_REPLY）。読む口は g53_meaning.set_r（数えは flow_miss の1か所）。
- GROUND_WORDS：中身の判定（G-63）。いま伺っている質問と選択肢を添えた形（通話と同じ make_grounder の ground(…, asking, options)）と、選択肢だけ外した形・
  質問を添えない形の正解。読む口は g63_grounding.touch_cases。
- IMPORT_WORDS：取込の中身の判定（askitems.grounded の same＝元の質問と同じことを伺っているか）。
- TALK_WORDS：会話AIの通話の試験（G-57）の別のお話。会話AIへの決まり（flowctl.TALK_RECEIVE）の例の言葉（「うちは月曜が定休日なんですよ。」）と違う言葉を
  主に使い、支払日の事実の無い業務（KNOW_NO_DAY）でも流す（決まりの例を写しただけで通らないように＝担当 D の D-13）。
- NEW_SCENE_ROWS（答えの段）：判定に送る文が 2026-11-20 に変わった場面（NEW_SCENES の印）に足した行（分身 S）。正解は答えを伏せた読み手2人（rv176/S/readers の
  束 n）の読みを当てたもの。場面は測りの数え（g53_meaning._flow_scene）が本番の流れの制御で組む。
"""
from __future__ import annotations

import functools

from flowctl import (ABOUT, ABOUT_DEPARTED, ABOUT_NO_SUCH, ABOUT_NONE, ABOUT_WRONG, ABSENT_WHY, ABSENT_WHY_NOT_SAID, AWAY, END, END_NONE, HEAR, HEAR_OK, OFFER,
                     OFFER_NOT_SAID, AWAY_NO, AWAY_YES, CALL_LATER, DONT_KNOW_LABEL, HESITATING, KEEP_ON,
                     END_PRESSED, END_REFUSED, LADDER_NEITHER, LADDER_OTHER, LADDER_OTHER_NOT, LADDER_OTHER_OK, LADDER_OTHER_UNSURE, PHONE, PHONE_NONE, PROMPT_REPLY, PROMPT_REPLY_NO, PROMPT_REPLY_WAVER, PROMPT_REPLY_YES, QUERY, QUERY_NO, QUERY_YES,
                     REFUSED_LABEL, REPEAT, HEAR_LOST, SPEAKER, SPEAKER_SAME, TARGET_IS_AWAY, THINKING, VALUE_CHOICE, WAIT_NOW, WHO, WHO_RECEPTION, WHY_NOT_SAID, WRONG,
                     WRONG_NOT_SAID, Question, accepted_line, alternatives_form, aside_form, callback_question, ladder_side_name, pain_question, since_form)
from grounding import TOUCHED, UNTOUCHED, one_touched
from meaning_judge import UNDECIDED, WITHDREW
from phrases import PAIN_ASK

# 判定に送る文が 2026-11-20 日本時間4時台〜6時台に変わった場面の印（行の印 scene。場面は測りの数えの1か所 g53_meaning._flow_scene が本番の流れの制御で組む＝
# 台本は試験の場面 DateTimePairsTest・WrongSplitAskTest・WaitedReplyHeadTest・PPainAskAgainTest・PConcernAwayTest と同じ手順。分身 S＝まとめ役 CL-1 の頼み rv176/S の続き (4)）
LADDER_SCENE = "日時の2択の段（平日と答えたあとの曜日の2択）"
# 同じ段の時刻の2択（曜日の2択に「火曜日」・午前と午後の2択に「午後」と答えたあとの「2時と4時ではどちらがよろしいでしょうか？」。片方だけ合わないのエは最後の段なので今の言い直しの
# 確かめ＝曜日の2択のエと違う動き。分身 S2）
LADDER_CLOCK_SCENE = "日時の2択の段（火曜日・午後と答えたあとの時刻の2択）"
WRONG_ASK_SCENE = "番号違いを伺う一言のあと（答えの段・判定の組が割れた）"
WAIT_SCENE = "待っていただく一言のあと（答えを言ったあと、こちらの声が出ていない）"
PAIN_SCENE = "顧客獲得型の悩みの伺い（Y2）"
CONCERN_SCENE = "顧客獲得型の本心を伺う一言（D2）"
# 確認の電話の本心を伺う一言 D2 のあと（分身 U2 で判定に送る文が変わった所＝答えの段の「この電話を終えたい・念を押して断った」はここだけ道が替わる＝ご遠慮の一言 R1。分身 S2）
CONFIRM_CONCERN_SCENE = "確認の電話の本心を伺う一言（D2）"
# 尋ねられて答えたあと（見直し (13) の持ち場 C の C2＝ご承認のお知らせのあとのご質問に会話AIが答え、お返事を待っている所。こちらの答えを「質問のあとにこちらが言った一言」として
# 判定に見せる＝帳面 1 の (1) の分身 R13C の段・伺い76 の「お答え」への「もう一度言ってほしい」。分身 S3）
ANSWERED_SCENE = "尋ねられて答えたあと（ご承認のお知らせのあとのご質問に答えた）"
# 申し出の最終の確かめ（迷っている方に言い直しについて尋ねられて答え、相づちだけを返されたあとの「はい、それでは…させていただきましょうか？」＝Flow._final_check の final_do。
# 帳面 1 の (1) の分身 N の段の「申し出の最終の確かめ」の行。分身 S3）
OFFER_FINAL_SCENE = "申し出の最終の確かめ（迷っている方に尋ねられて答えたあと）"
# 一度受けていただいた申し出から始まった質問（Flow._taken_offer＝判定の選べるものに「申し出を取り消した」が出る質問。帳面 1 の (1) の伺い40 の段＝分身 A。分身 S3）：急いでいる方への
# かけ直しの申し出 H1 に「はい」をいただいたあとの時刻の伺い・パンフレットの申し出に「はい」をいただいたあとの送付先（ask_if）
WITHDRAW_TIME_SCENE = "受けていただいたかけ直しの申し出のあとの時刻の伺い（急いでいる方の H1 に「はい」）"
WITHDRAW_SEND_SCENE = "受けていただいた申し出のあとの業務の質問（パンフレットの送付先）"
# 受けていただいた申し出（ご承認いただけなかったあとの詳しい方からのお話の申し出 N4）のあとの方法の伺い N5（伺い40 の方法の質問。分身 S3 の続き）
WITHDRAW_WAY_SCENE = "受けていただいた申し出のあとの方法の伺い（ご承認いただけなかったあとの N4 に「はい」）"
# ほかの会社に頼んでいると言われ（ご案内全体）、困りごとは無いと言われたあとの決め手の伺い E5（伺い47 のイ・本書 9-4 の伺い48 の段の ⏳。分身 S3 の続き）
ELSEWHERE_POINT_SCENE = "決め手を伺う一言のあと（ほかの会社に頼んでいて困りごとは無いと言われた）"
NEW_SCENES = (LADDER_SCENE, WRONG_ASK_SCENE, WAIT_SCENE, PAIN_SCENE, CONCERN_SCENE, CONFIRM_CONCERN_SCENE, LADDER_CLOCK_SCENE, ANSWERED_SCENE, OFFER_FINAL_SCENE,
              WITHDRAW_TIME_SCENE, WITHDRAW_SEND_SCENE, WITHDRAW_WAY_SCENE, ELSEWHERE_POINT_SCENE)
# 申し出の手順で選んでいただく方法（業務設定 handoff_ways。試験 EndJudgedTest.test_an_unlinked_reply_to_an_offer_is_answered_then_the_offer_is_put_again の to_offer と同じ）
HANDOFF_WAYS = ("訪問", "お電話")

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
    # R9＝申し出の質問（パンフレットの申し出＝電話を終える言葉の試しの表の質問と同じ1か所 end_words.question。申し出の最終の確かめの場面 OFFER_FINAL_SCENE。分身 S3）
    from end_words import PAMPHLET, question as offer_question
    return {"R1": pay, "R2": Question("R2", "ご訪問の日にちはいつがよろしいでしょうか？", "date", conditions=("答えた",), required=True),
            "R3": since_form(budget), "R4": since_form(person), "R5": aside_form(pay), "R6": alternatives_form(pay), "R7": approval,
            "H2": callback_question("wait_or_later"),
            # 金額を伺う質問（言い直しへのお返事の試しの言葉＝帳面 1 の (1) の分身 N の行の「金額の言い直し」。言い直しの文は本番の1か所 Flow._ask_confirm）
            "R8": Question("R8", "ご予算はおいくらくらいでしょうか？", "amount", conditions=("答えた",), required=True),
            # 日時の2択の段（分身 N の伺い60）・顧客獲得型の悩みの伺いと本心を伺う一言（分身 P）の質問（どれも本番の1か所で作る形。場面は LADDER_SCENE などの印＝まだ組めない）。
            # 悩みの伺いの悩みは、試験の顧客獲得型の業務の1つ目の悩み（test_offline.ACQUIRE_PAINS）と同じ言葉
            "C1": callback_question("later_time", answering=True),
            "Y2": pain_question("ドライバーさんの人手不足", PAIN_ASK.format(who="御社"), "pain_ask_company"),
            "D2": callback_question("concern"), "R9": offer_question(PAMPHLET, qid="R9"),
            # R10＝パンフレットの申し出（R9）を受ける答えで伺う業務の質問（ask_if＝試験 RecallProposalWithdrawTest の送付先と同じ形。WITHDRAW_SEND_SCENE）
            "R10": Question("R10", "送付先のメールアドレスをお伺いしてもよろしいでしょうか？", "email", conditions=("答えた",), ask_if=("R9", ("はい",))),
            # R12・R13＝ご承認いただけなかったときの手順（N1 → 申し出 N4 → 方法 N5）へ進む業務（同じ試験の to_offer と同じ質問。WITHDRAW_WAY_SCENE と G-57 の方法の提案）
            "R12": Question("R12", "資料はお手元に届いていますでしょうか？", "yes_no", conditions=("はい", "いいえ")),
            "R13": Question("R13", "以上の内容でご承認いただけますでしょうか？", "yes_no", conditions=("はい", "いいえ"), approval=True,
                            intro="こちらのプランは月額3,000円で初月は無料となっております。", final_goal=True, declines=("いいえ",)),
            # システムの質問（本番の1か所 callback_question＝測りの質問の表 g53_meaning.question_map と同じもの）：N5＝申し出の方法・E5＝決め手・N2＝ほかにご納得いただけない点
            "N5": callback_question("handoff_way"), "E5": callback_question("elsewhere_point"), "N2": callback_question("other_point"),
            # ほかのご希望 W1（「どちらも要らない」の G17 のあと＝測りの質問の表の W1@G17 の1か所）
            "W1@G17": _wish_question("W1@G17")}


@functools.cache
def _wish_question(key: str) -> Question:
    """ほかのご希望 W1 の質問（測りの質問の表 g53_meaning.question_map の1か所＝g73_system_words.wish_scenes が元の質問から作る。ここに書き写さない）。"""
    from g53_meaning import question_map
    return question_map()[key]


# わからない・答えたくない理由の正解（label の reason＝g53_labels.json の行と同じ欄・同じ決め方＝F5 の 11-15）：答えが「決められない」を含む行は、流れの制御が
# 理由の読みを使う（Flow._not_deciding）＝理由も正解を持つ。流れの制御で同じ動きになる理由の名前を並べる（理由を言っていない言葉＝相づち・別のお話・ほかのことを
# 話した・答えた、は「とは言っていない」「理由を言わなかった」「決められない」のどれでも同じ動き。迷っている言葉は「まだ決まっていない」との境目）
WHY_OTHER = (WHY_NOT_SAID, DONT_KNOW_LABEL["unspecified"], UNDECIDED)
WHY_HESITATING = (DONT_KNOW_LABEL["undecided"], *WHY_OTHER)


def _row(qid: str, text: str, accept, away=None, query=QUERY_NO, prompt=None, hedged=False, asked: str | None = None, why: str = "",
         reason=None, wrong=None, scene=None, value: str | None = None, end=None, other=None) -> dict:
    label = {"accept": list(accept), "why": why}
    if hedged:
        label["hedged"] = True
    if reason:
        label["reason"] = list(reason)
    # end＝電話を終える言葉かの正解（無ければ「言っていない」＝g53_meaning.end_accept）・other＝合わないと言わなかったほうで大丈夫そうかの正解（日時の2択の段の行だけ＝
    # g53_meaning.ladder_other_truths。分身 S2）
    if end is not None:
        label["end"] = list(end) if isinstance(end, (list, tuple)) else [end]
    for key, got in ((AWAY, away), (QUERY, query), (PROMPT_REPLY, prompt), (WRONG, wrong), (LADDER_OTHER, other)):
        if got is not None:
            label[key] = list(got) if isinstance(got, (list, tuple)) else [got]   # 境目の行は並べた名前のどれも正解
    # scene＝場面の印（g53_meaning._connected_scene：お呼びした方に繋いだあとの答えの段・NEW_SCENES。無ければ目的のお客様のいない業務の最初の質問＝今までの行）。
    # value＝会話AIが答えの報告で渡す値の文字（判定は値の文字を読まない＝測りの中立の報告 g53_meaning._neutral_report が渡す。無ければ質問の型の置き場の値）
    return ({"set": "R", "maker": "見直し (12)", "qid": qid, "text": text, "asked": asked, "label": label} | ({"scene": scene} if scene else {})
            | ({"value": value} if value else {}))


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
        _row("R7", "あ、それとですね、うちは月曜が定休日なんですよ。", [UNDECIDED], (AWAY_YES, AWAY_NO), prompt=PROMPT_REPLY_NO, reason=WHY_OTHER,
             why="声かけへのお返事だけではない別のお話。別のお話かは境目：声かけ（気になる点）へのお返事として話したご事情＝答えの話（CL-1 の 9-3 の案の (別のお話か)）とも、"
                 "どの答えの向きも示さないご事情＝別のお話とも読める（2026-11-20 rv176/S2 の束 n の #1＝答えを伏せた読み手2人が2人とも両方を挙げた・ご判断は無い。直す前の値＝別のお話）"),
        _row("R7", "いえ、大丈夫です。", [UNDECIDED], AWAY_NO, prompt=PROMPT_REPLY_YES, why="声かけへのお返事だけ", reason=WHY_OTHER),
        # 帳面 1 の (1) の「この1回の測りに足す」と書かれた答えの段の言葉（分身 Y2 が入れた。正解は帳面に書かれたとおり＝お言葉・決まった説明の文から決まる）
        # 言い直し・最終の確かめへのお返事の別のお話か（分身 N＝5a7f04ce・見直し (12) の A-9・B-3）：金額の言い直し・ご承認のお知らせの最終の確かめ
        {**_row("R8", "うちは月曜が定休日なんですよ。", [UNDECIDED], AWAY_YES, asked=budget_again, why="言い直しへの別のお話（分身 N の行）", reason=WHY_OTHER),
         "set": "D"},
        {**_row("R8", "はい、大丈夫です。", ["affirmed"], AWAY_NO, asked=budget_again, why="言い直しを認めた＝答えの話（答えは合っている。分身 N の行）"),
         "set": "D"},
        # 分身 N の行の「うーん、どうでしょうねえ。」（前は答えが決まらず TO_READERS）：束 s の #26＝読み手2人とも答えは「決められない」「考えている途中」「迷っている」・答えの話・
        # こちらへ尋ねた問いは無い＝そろった読みで表に入れた（分身 S3 の続き (3)）
        {**_row("R8", "うーん、どうでしょうねえ。", [UNDECIDED, THINKING, HESITATING], AWAY_NO, asked=budget_again, reason=WHY_HESITATING,
                why="言い直しへの迷い・言いよどみ（rv178/S3/readers の束 s の #26 の読み手2人がそろった）"), "set": "D"},
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
        *NEW_SCENE_ROWS,
        *PREP3_ROWS,
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


# 判定に送る文が変わった場面（2026-11-20 日本時間4時台〜6時台・帳面 1 の (1) の ⏳）の試しの言葉（分身 S＝まとめ役 CL-1 の頼み rv176/S の (b)(e)(f)）。正解は、判定に見せる言葉と
# 説明の文から分身 S が案を作り、答えを伏せた読み手2人（rv176/S/readers の束 n）の読みを当てた：2人がそろった読みはその読み・割れた所と2人とも「どちらとも取れる」の所は
# 決まりとご判断で決めた（境目はどちらも正解。行ごとの決め方は rv176/S/notes.md の 9）。場面は NEW_SCENES の印（測りの数えの1か所 g53_meaning._flow_scene が組む）
NEW_SCENE_ROWS = [
    # (b) 日時の2択の段（分身 N の伺い60＝選べるものに「迷っている」「どちらも合わない」＝flowctl.LADDER_NEITHER・分身 U2 の伺い94＝並べた2つごとの「（避けた方）だけ合わない」と
    # 項目「合わないと言わなかったほうで大丈夫そうか」＝flowctl.LADDER_OTHER）。場面は、かけ直しを頼まれ、平日と土日の2択に「平日」と答えたあとの曜日の2択（「金曜日と火曜日では
    # どちらがよろしいでしょうか？」＝電話の日 2026-11-19 木曜日 10時・11-23 は祝日。試験 DateTimePairsTest の場面）。答えの3つ（答えた・どちらも合わない・（避けた方）だけ合わない）の
    # 説明は意味で書いた1か所（flowctl.ladder_choice_desc＝CL-1 が 9-3 の案の (2択) で決めた・分身 S2）。もう一方で大丈夫そうか（other）は、2択を伺っている質問の全部の行に付ける
    # （片方だけ合わないの読みでないときは、その項目は流れの制御の動きを変えない）。⏳ 分身 S2 で正解を当て直した行・足した行は、答えを伏せた読み手2人の読み直し待ち（rv176/S2/readers）
    _row("C1", "どちらもちょっと難しいですね。", [LADDER_NEITHER], AWAY_NO, scene=LADDER_SCENE, other=LADDER_OTHER_NOT,
         why="並べた2つのどちらも難しいとだけ言った（「どちらも合わない」の説明のとおり・2026-11-20 rv176/S の束 n の読み手2人がそろった）。片方だけ合わないとは言っていない"
             "（分身 U2 の書き置き rv176/U2/notes.md の 8 の (1)）"),
    _row("C1", "うーん、どっちがいいかなあ。", [HESITATING, THINKING], AWAY_NO, scene=LADDER_SCENE, other=LADDER_OTHER_NOT,
         why="2つのあいだで迷っていると言った＝迷っている。言いよどみで、このあと続けて答えそう＝考えている途中、とも読める（正解の表の R1「うーん、どっちがいいですかねえ。」"
             "と同じ境目。2026-11-20 rv176/S の束 n の読み手2人も2人とも同じ2つ）。片方だけ合わないとは言っていない"),
    _row("C1", "金曜日でお願いします。", [VALUE_CHOICE], AWAY_NO, scene=LADDER_SCENE, value="金曜日", other=LADDER_OTHER_NOT,
         why="並べた2つの1つ（金曜日）を選んだ＝答えた（2026-11-20 rv176/S の束 n の読み手2人がそろった）。片方だけ合わないとは言っていない"),
    _row("C1", "来週の月曜日なら空いてます。", [VALUE_CHOICE], AWAY_NO, scene=LADDER_SCENE, value="来週の月曜日", other=LADDER_OTHER_NOT,
         why="2択の外の日を言った＝答えた（日時の2択の段は2択の外の答えも受け取る＝Flow._ladder_take）。✅ 2026-11-20 日本時間17時台 CL-1 が決まりで決めた（9-3 の案の (2択)：答えた＝"
             "日・時刻を言った・並べた2つの外でも／どちらも合わない＝並べた2つのどちらも合わないとだけ言った＝4時44分42秒の伺い60「2.別の空スケジュールの2つ」の場面）＝"
             "どちらも合わないは外した（直す前の値＝[答えた, どちらも合わない]＝2026-11-20 rv176/S の束 n の読み手2人が、説明が名前のくり返しだった文で2人とも両方を挙げた）"),
    _row("C1", "金曜日はちょっと…。", [ladder_side_name("金曜日")], AWAY_NO, scene=LADDER_SCENE, value="火曜日", other=LADDER_OTHER_UNSURE,
         why="並べた2つの片方（金曜日）だけを避けた＝金曜日だけ合わない（✅ 2026-11-20 日本時間16時13分55秒「他も書いてることばかりだろ！」の伺い94＝この言葉そのものが片方だけ避けた"
             f"場面）。火曜日で大丈夫かは言っていない＝{LADDER_OTHER_UNSURE}（16時25分52秒「伺い94：はもう一方で大丈夫そうなら、エでいいし、もう一方では不安そうならウでしょ」）。"
             "直す前の値＝[決められない, 答えた, 考えている途中]（選べるものに片方だけ合わないが無かった頃の境目＝rv176/S の束 n）"),
    _row("C1", "金曜日はちょっと…。火曜日は空いてますけど。", [ladder_side_name("金曜日")], AWAY_NO, scene=LADDER_SCENE, value="火曜日", other=LADDER_OTHER_OK,
         why="金曜日だけを避け、火曜日は空いていると言ったが、火曜日にすると、はっきり選んではいない＝金曜日だけ合わない・もう一方で大丈夫そう（伺い94 のエ＝16時25分52秒。"
             "分身 U2 の書き置きの 8 の (1) の案。rv176/S2 の束 n の #8＝読み手2人が2人とも「答えた」とも読んだ＝ご判断（16時25分52秒）で決めて説明の1か所を直した）"),
    _row("C1", "金曜日はちょっと…。火曜日もどうかなあ。", [ladder_side_name("金曜日")], AWAY_NO, scene=LADDER_SCENE, value="火曜日", other=LADDER_OTHER_UNSURE,
         why=f"金曜日だけを避け、火曜日も不安そうに言った＝金曜日だけ合わない・{LADDER_OTHER_UNSURE}（伺い94 のウ＝16時25分52秒「…もう一方では不安そうならウでしょ」。分身 U2 の書き置きの"
             " 8 の (1) の案。rv176/S2 の束 n の #9＝読み手2人が2人とも「どちらも合わない」とも読んだ＝ご判断で決めて説明の1か所を直した）"),
    _row("C1", "金曜日はちょっと…火曜日なら。", [ladder_side_name("金曜日")], AWAY_NO, scene=LADDER_SCENE, value="火曜日", other=LADDER_OTHER_OK,
         why="金曜日を避けて、火曜日なら（大丈夫）と言った＝金曜日だけ合わない・もう一方で大丈夫そう（✅ 2026-11-20 日本時間16時25分52秒「伺い94：はもう一方で大丈夫そうなら、エでいいし…」"
             "＝エ「それでは火曜日でしたら…」）。2026-11-20 rv176/S2 の束 n の #10＝答えを伏せた読み手2人が2人とも「答えた」「金曜日だけ合わない」の両方を挙げ、境目が分かりにくいと書いた"
             "＝ご判断で決めて「（避けた方）だけ合わない」の説明の1か所を直した（直す前の値＝答えた・片方だけ合わないとは言っていない＝分身 U2 の書き置きの 8 の (1) の候補）"),
    _row("C1", "火曜日はちょっと難しいですね。", [ladder_side_name("火曜日")], AWAY_NO, scene=LADDER_SCENE, value="金曜日", other=LADDER_OTHER_UNSURE,
         why="並べた2つのうちあとの方（火曜日）だけを難しいと言った＝火曜日だけ合わない・金曜日で大丈夫かは言っていない（伺い94。分身 U2 の書き置きの 8 の (1) の案）"),
    # 時刻の2択（LADDER_CLOCK_SCENE＝「2時と4時ではどちらがよろしいでしょうか？」）：片方だけ合わないのエは最後の段なので、避けなかった時刻で今の言い直しの確かめ
    _row("C1", "2時はちょっと…。4時なら空いてるかな。", [ladder_side_name("2時")], AWAY_NO, scene=LADDER_CLOCK_SCENE, value="4時", other=LADDER_OTHER_OK,
         why="2時だけを避け、4時なら空いていそうと言った（「かな」＝はっきり選んではいない）＝2時だけ合わない・もう一方で大丈夫そう（伺い94 のエ。分身 U2 の書き置きの 8 の (1) の案。"
             "rv176/S2 の束 n の #12＝読み手2人が2人とも「答えた」とも読んだ＝16時25分52秒のご判断で決めて説明の1か所を直した。答えたでも同じ言い直しの確かめ）"),
    # (e) 番号違いを伺う一言のあと（分身 M の伺い72 エ＝meaning_judge.WRONG_ASK_NOTE。「はい」「そうです」は伺ったとおり＝番号違い）。場面は、支払いの質問（R1）への
    # 「うちはそういう会社じゃないですよ。番号をお間違えだと思います。」を判定の組の1つだけが番号違いと読み、こちらが「大変失礼いたしました。番号を間違えてお掛けして
    # しまいましたでしょうか？」と伺ったあと（目的のお客様のいない業務の最初の質問）。別のお話かは、番号違いの行と同じ（どちらの読みも正解＝WRONG_ANSWER_ROWS の注）
    _row("R1", "はい、そうです。", [UNDECIDED], AWAY_NO, wrong=ABOUT_WRONG, scene=WRONG_ASK_SCENE, reason=WHY_OTHER,
         why=f"番号違いかを伺った一言に「はい、そうです」＝伺ったとおり番号違い（WRONG_ASK_NOTE・2026-11-20 rv176/S の束 n の読み手2人がそろった）。支払いの質問には答えていない。"
             "別のお話かは答えの話（質問のあとにこちらが言った一言へのお返事も答えの話＝✅ 2026-11-20 日本時間17時台 CL-1 が決まりで決めた 9-3 の案の (別のお話か)・11-13 日本時間"
             "20時06分19秒。直す前の値＝[答えの話, 別のお話, 決められない]）"),
    _row("R1", "いえ、合ってますよ。", [UNDECIDED], AWAY_NO, wrong=WRONG_NOT_SAID, scene=WRONG_ASK_SCENE, reason=WHY_OTHER,
         why=f"番号は合っていると言った＝番号違いとは言っていない（2026-11-20 rv176/S の束 n の読み手2人がそろった）。支払いの質問には答えていない。別のお話かは答えの話（伺った一言への"
             "お返事＝CL-1 の 9-3 の案の (別のお話か)。直す前の値＝[答えの話, 別のお話, 決められない]＝読み手2人とも別のお話・決められないも挙げた）"),
    # (e) 待っていただく一言のあと（分身 M の伺い76 ウ＝flowctl.PROMPT_REPLY_WAVER・meaning_judge.WAIT_NOTE）。場面は、支払いの質問（R1）に答えたあと（「振込でお願いします。」）、
    # こちらの声が出ていないので「少々お待ちいただいてもよろしいでしょうか？」を流したところ
    _row("R1", "えー、待つんですか、どうしようかなあ。", [UNDECIDED], AWAY_NO, query=(QUERY_YES, QUERY_NO), prompt=PROMPT_REPLY_WAVER,
         scene=WAIT_SCENE, reason=WHY_OTHER,
         why="待つかどうかの迷いだけ（WAIT_NOTE のとおり・✅ 2026-11-20 日本時間3時55分24秒「伺い76：ウ」の伺いの言葉＝待つことに迷われたとき。読み手の1人はお返事だけではない"
             f"とも読んだ＝ご判断で迷いだけ）。こちらへ尋ねた問いかは、2026-11-20 rv176/S の束 n の読み手2人が2人とも2つを挙げた（「待つんですか」は尋ねたとも、ためらいを言っただけとも）。"
             "別のお話かは答えの話（待っていただく一言へのお返事＝CL-1 の 9-3 の案の (別のお話か)。直す前の値＝[別のお話, 決められない]）"),
    _row("R1", "はい、構いませんよ。", [UNDECIDED], AWAY_NO, prompt=PROMPT_REPLY_YES, scene=WAIT_SCENE, reason=WHY_OTHER,
         why=f"待つお願いへのお返事だけ（待ってよい）＝答えは言い直していない（WAIT_NOTE のとおり・2026-11-20 rv176/S の束 n の読み手2人がそろった）。別のお話かは答えの話（待っていただく"
             "一言へのお返事＝CL-1 の 9-3 の案の (別のお話か)。直す前の値＝[答えの話, 決められない, 別のお話]＝読み手2人が割れた）"),
    # (f) 顧客獲得型の悩みの伺い（Y2）・本心を伺う一言（D2）の「質問の答えの話か、別のお話か」（分身 P＝flowctl.AWAY_ROLES に pain・concern）。場面は、試験の顧客獲得型の
    # 業務（PPainAskAgainTest・PConcernAwayTest の場面）
    _row("Y2", "今日は暑いですねえ。", [UNDECIDED], AWAY_YES, scene=PAIN_SCENE, reason=WHY_OTHER, why=f"悩みの伺いと関わりのない世間話＝別のお話（2026-11-20 rv176/S の束 n の読み手2人がそろった）"),
    _row("Y2", "うーん、どうでしょうねえ。", [UNDECIDED, THINKING, "unknown"], AWAY_NO, scene=PAIN_SCENE, reason=WHY_OTHER,
         why="悩みがあるとも無いとも言っていない＝決められない・言いよどみ＝考えている途中・自分でもよく分からない＝わからない、の境目（"
             f"2026-11-20 rv176/S の束 n の読み手2人が2人とも3つを挙げた）。悩みの伺いへの返事＝答えの話"),
    _row("D2", "今日は暑いですね。", [UNDECIDED], AWAY_YES, scene=CONCERN_SCENE, reason=WHY_OTHER, why=f"本心を伺う一言と関わりのない世間話＝別のお話（2026-11-20 rv176/S の束 n の読み手2人がそろった）"),
    _row("D2", "説明が長くてよく分からないんですよ。", ["問題を言った"], AWAY_NO, scene=CONCERN_SCENE,
         why="こちらの話し方・進め方への気になる点を言った＝問題を言った・答えの話（「よく分からない」は説明のことで、答えを持っていないとは言っていない）。電話を終える言葉か"
             "は、読み手の1人が怒っている・苦情とも読んだが、無礼だと責める・強い言葉で言いつける・責任者を求める、のどれでもない＝伺い81 の説明の文で言っていない"),
    # (g) 確認の電話の本心を伺う一言 D2 のあと（分身 U2＝答えの段の「この電話を終えたい・念を押して断った」と「通話・営業を断った」を分けた所。D2 のあとだけ道が替わる＝前者はご遠慮の
    # 一言 R1・後者はその場で締めてもうかけない）。場面はご予約の質問のあとパンフレットの申し出に「もういいです。」と言われて D2 を伺った所（CONFIRM_CONCERN_SCENE）。分身 S2＝分身 U2 の
    # 書き置き rv176/U2/notes.md の 8 の (2) の案
    _row("D2", "もういいって言ってるでしょう。", ["断ろうとしている"], AWAY_NO, scene=CONFIRM_CONCERN_SCENE, end=END_PRESSED,
         why="本心を伺う一言に、まだこの電話を終えたいと念を押した＝この電話を終えたい・念を押して断った（このあとの電話・営業の電話そのものを断るとまでは言っていない）・答えは"
             "断ろうとしている（✅ 2026-11-08 日本時間2時21分15秒「…まだ電話を終えたい（「もういいって言ってるでしょう。」）＝ここでご遠慮の一言（R1）」・分身 U2 の案）"),
    _row("D2", "もうかけてこないでください。", ["断ろうとしている"], AWAY_NO, scene=CONFIRM_CONCERN_SCENE, end=END_REFUSED,
         why="本心を伺う一言に、このあとの電話を断った＝通話・営業を断った（原則14＝その場で発信禁止）・答えは断ろうとしている（分身 U2 の案）"),
]


# 測りの前に0円で済ませる残り（帳面 1 の (1) の ⏳ と本書 9-4 の ⏳ に、言葉と正解の候補が書いてある試しの言葉。分身 S3＝まとめ役 CL-1 の頼み rv178/S3 の (2)）。言葉と正解は ⏳ に
# 書いてあるとおり（書いていない項目は、説明の文と言葉だけから決めた）。場面は測りの数えの1か所（g53_meaning._flow_scene）が本番の流れの制御で組む。⏳ どの行も、答えを伏せた
# 読み手2人の束（rv178/S3/readers）で読み直してから数える（帳面 0-3「試しの正解は…別の目で読み直してから数える」）
_WAIT_TRUTH = "（帳面 1 の (1) の 11-19 日本時間21時06分08秒の段の (c)＝分身 AF の注意書き WAIT_NOTE の試しの言葉の候補）"
_MOSHI_TRUTH = "（本書 9-4 の ✅ 2026-11-19 日本時間15時10分27秒「伺い49：エ」の段の ⏳「「もしもし？」を流したあとの発話…試しの言葉（「はい」「もしもし」「聞こえてます」「あ、すみません、カードで」）を入れる候補」）"
PREP3_ROWS = [
    # (1) 待っていただく一言のあと（WAIT_SCENE＝支払いの質問に「振込でお願いします。」と答えたあと、こちらの声が出ていないので「少々お待ちいただいてもよろしいでしょうか？」を流した所）
    _row("R1", "はい、大丈夫ですよ。", [UNDECIDED], AWAY_NO, prompt=PROMPT_REPLY_YES, scene=WAIT_SCENE, reason=WHY_OTHER,
         why=f"待っていただく一言へのお返事だけ＝候補のとおり{_WAIT_TRUTH}。支払いの質問の答えは言い直していない。別のお話かは答えの話（質問のあとにこちらが言った一言へのお返事も"
             "答えの話＝✅ 2026-11-20 日本時間17時台 CL-1 が決まりで決めた 9-3 の案の (別のお話か)）"),
    _row("R1", "どうぞ。", [UNDECIDED], AWAY_NO, prompt=PROMPT_REPLY_YES, scene=WAIT_SCENE, reason=WHY_OTHER,
         why=f"待っていただく一言へのお返事だけ＝候補のとおり{_WAIT_TRUTH}。別のお話かは同じ（答えの話）"),
    _row("R1", "はい、大丈夫です。あ、それとお支払いはカードで。", ["カード"], AWAY_NO, prompt=PROMPT_REPLY_NO, scene=WAIT_SCENE, value="カード",
         why=f"お返事だけではない＝候補のとおり{_WAIT_TRUTH}。待つことに応えたあと、支払いの答えを「カード」と言った（答え＝カード）"),
    {**_row("R1", "すみません、ちょっと聞こえなかったんですけど。", [REPEAT], (AWAY_NO, UNDECIDED), prompt=PROMPT_REPLY_NO, scene=WAIT_SCENE,
         why="待っていただく一言が聞こえなかったと言った＝もう一度言ってほしい（帳面 1 の (1) の 11-19 日本時間23時台の段＝伺い76 の試しの言葉そのまま・説明「直前にこちらが言ったこと"
             "（…お待ちいただくようにお願いしたことなど）が聞こえなかった…と言ったのも」）。声かけへのお返事だけではない（説明の「お返事だけ＝待ってよい・分かった、と言っただけ」に"
             "当たらない＝聞こえなかったと言った）。束 s の #4：別のお話かは読み手2人とも「答えの話」「決められない」の両方＝境目にした・こちらの声が届いていない様子かは2人とも"
             "「届いていない様子」（聞こえなかったと言った＝10-30 の管理者のご決定の材料）＝替えた（直す前の値＝答えの話／そういう様子は無い）。声かけへのお返事だけかは読み手の1人が"
             "決められないも挙げた＝説明の文で決めたまま"), "hear_truth": HEAR_LOST},
    # (2) 待つかかけ直すかの伺い H2（支払いの質問に「上司に相談してみるので、少し待っていてください。」と言われて伺った所＝g53_meaning._wait_ask_scene）
    _row("H2", "そういえば、うちは月曜が定休日なんですよ。", [UNDECIDED], AWAY_YES, reason=WHY_OTHER,
         why="H2 と関わりのないご事情＝別のお話（帳面 1 の (1) の 11-19 日本時間21時06分08秒の段の (b)＝分身 AG の flowctl.AWAY_ROLES の試しの言葉そのまま）"),
    _row("H2", "うーん、どうしようかな。", ["unknown", UNDECIDED, THINKING], AWAY_NO, reason=WHY_HESITATING,
         why="H2 への迷い＝答えの話（決められない）（同じ (b) の試しの言葉と正解の候補そのまま）。⏳ 「考えている途中」（「はい。」で待つ）との境目は、答えを伏せた読み手2人で"
             "確かめる（前は正解を付けずに TO_READERS＝見直し (13) の D8。11-07 日本時間18時台に業務の質問の「うーん、どうしようかな」へ考えている途中を足したのは H2 の場面ではない）。"
             "束 s の #6：読み手2人とも「わからない」「決められない」「考えている途中」の3つ・理由は「まだ決まっていない」も＝そろった読みにした（直す前の値＝[決められない]）"),
    # (3) 伺ったあと黙られて「もしもし？」を流したあと（場面＝g53_meaning._played_flow の支払いの質問のあと _quiet。正解に「声かけへのお返事だけか」がある行＝prompted_row）
    _row("R1", "はい", [UNDECIDED], AWAY_NO, prompt=PROMPT_REPLY_YES, reason=WHY_OTHER,
         why=f"「もしもし？」へのお返事だけ（電話口にいる）。支払いの質問には答えていない＝決められない{_MOSHI_TRUTH}"),
    _row("R1", "もしもし", [UNDECIDED], AWAY_NO, prompt=PROMPT_REPLY_YES, reason=WHY_OTHER,
         why=f"「もしもし？」に「もしもし」と返しただけ＝お返事だけ。支払いの質問には答えていない＝決められない{_MOSHI_TRUTH}。こちらの声が届いていない様子かは、1回だけの"
             "「もしもし」では、聞こえない・くり返した、とは言っていない＝そういう様子は無い（読み手で確かめる）"),
    _row("R1", "聞こえてます", [UNDECIDED], AWAY_NO, prompt=PROMPT_REPLY_YES, reason=WHY_OTHER,
         why=f"「もしもし？」へ、聞こえていると返しただけ＝お返事だけ。支払いの質問には答えていない＝決められない{_MOSHI_TRUTH}"),
    _row("R1", "あ、すみません、カードで", ["カード"], AWAY_NO, prompt=PROMPT_REPLY_NO,
         why=f"「もしもし？」に応えて支払いの質問に答えた（カード）＝お返事だけではない{_MOSHI_TRUTH}"),
    # (4) 尋ねられて答えたあと（ANSWERED_SCENE＝ご承認のお知らせのあとの「そういえば御社って駐車場ありましたっけ？」に会話AIが答えた所＝お客様に聞こえるのは「はい、店舗の前に駐車場がございます。」）
    _row("R7", "はい、分かりました。", [UNDECIDED], AWAY_NO, prompt=PROMPT_REPLY_YES, scene=ANSWERED_SCENE, reason=WHY_OTHER,
         why="こちらの答えへのお返事だけ（ご了承ではない）＝帳面 1 の (1) の分身 R13C の段の (a) の試しの言葉と正解そのまま（✅ 2026-11-13 日本時間20時06分19秒「「あ、はい、分かりました。」は、"
             "…に対して答えてる可能性がある」）。束 s の #11：答えは読み手2人とも「決められない」だけ（注意書き「お返事だけで、質問への答えをはっきり言っていなければ「決められない」」）"
             "＝そろった読みにした（直す前の値＝[はい, 決められない]）"),
    _row("R7", "分かりました。キャンセルの件も大丈夫です。", ["はい"], AWAY_NO, prompt=PROMPT_REPLY_NO, scene=ANSWERED_SCENE,
         why="こちらの答えに応え、お知らせ（キャンセル）も了承した＝答え「はい」・お返事だけではない（同じ (a) の試しの言葉と正解そのまま）"),
    {**_row("R7", "え、何ておっしゃいました？", [REPEAT], (AWAY_NO, UNDECIDED), prompt=PROMPT_REPLY_NO, scene=ANSWERED_SCENE,
         why="こちらの答えが聞き取れなかった＝もう一度言ってほしい（帳面 1 の (1) の 11-19 日本時間23時台の段＝伺い76 の試しの言葉「お答え「はい、店舗の前に駐車場がございます。」への"
             "「え、何ておっしゃいました？」」そのまま）。こちらへ尋ねた問いは無い（言い直しを頼んだだけ＝正解の表の HEAR の「すみません、今なんて言いました？」と同じ）。声かけへの"
             "お返事だけではない（説明の「お返事だけ＝分かった、と言っただけ」に当たらない・「こちらに尋ねた など」はお返事だけではない側。お返事だけと読むと、流れの制御は答えを"
             "言い直さずに最終の確かめへ進む）。束 s の #13：#4 と同じ（別のお話かは境目・届いていない様子に替えた）"), "hear_truth": HEAR_LOST},
    # (5) 申し出の最終の確かめ（OFFER_FINAL_SCENE＝パンフレットの申し出に「うーん、送ってもらおうかな…。」→ 言い直し → 「送ってもらったほうがいいですかね？」に答えた → 「そうなんですね。」→
    #     「はい、それではパンフレットをお送りさせていただきましょうか？」）
    _row("R9", "そういえば来週は祝日なんですよ。", [UNDECIDED], AWAY_YES, scene=OFFER_FINAL_SCENE, reason=WHY_OTHER,
         why="最終の確かめと関わりのないご事情＝別のお話（帳面 1 の (1) の分身 N の段の申し出の最終の確かめ「それでは資料をお送りさせていただきましょうか？」への試しの言葉と正解そのまま。"
             "申し出は試しの表のパンフレットの申し出＝同じ形の最終の確かめ）"),
    _row("R9", "うーん、どうしようかな。", [THINKING, HESITATING], AWAY_NO, scene=OFFER_FINAL_SCENE, reason=WHY_HESITATING,
         why="最終の確かめへの迷い＝答えの話（帳面の段の候補は「決められない」）。束 s の #15：読み手2人とも「考えている途中」「迷っている」の2つ＝言い終えた迷い（迷っている）と"
             "言いよどみ（考えている途中）の境目（✅ 2026-11-07 日本時間18時03分49秒「(1) ア、(2) イ」＝言い終えた「迷っています。」と言い終えていない「うーん、どうしようかな…。」）"
             "＝お答えで決まる2つを正解にした（直す前の値＝[決められない]）"),
    # (6) 受けていただいた申し出から始まった質問の途中の取り消し（帳面 1 の (1) の伺い40 の段の試しの言葉と正解そのまま。取り消しの言葉は、電話を終える言葉でも答えたくないでもない＝
    #     電話を終える言葉かは「言っていない」・答えは「申し出を取り消した」。時刻の伺いは日時の2択の段＝もう一方で大丈夫そうかは、片方だけ合わないの読みでない行＝言っていない）
    _row("C1", "あ、やっぱりかけ直していただかなくて結構です。", [WITHDREW], AWAY_NO, scene=WITHDRAW_TIME_SCENE, other=LADDER_OTHER_NOT,
         why="受けていただいたかけ直しの申し出を取り消した（伺い40 の段の時刻の質問への試しの言葉そのまま）"),
    _row("C1", "やっぱりいいです。", [WITHDREW], AWAY_NO, scene=WITHDRAW_TIME_SCENE, other=LADDER_OTHER_NOT, end=(END_NONE, UNDECIDED),
         why="受けていただいたかけ直しの申し出を取り消した（同じ）。束 s の #17：電話を終える言葉かは読み手2人とも「言っていない」「決められない」（何をやめるかを言わない短い言葉＝"
             "決められないの説明「断りらしい言葉でも…言葉から決まらないときも」と、言っていないの説明「前の質問でこちらが申し出たこと…を、断った」の2つの決まりが当たる）＝境目にした"),
    _row("C1", "すみません、やっぱりやめておきます。", [WITHDREW], AWAY_NO, scene=WITHDRAW_TIME_SCENE, other=LADDER_OTHER_NOT, end=(END_NONE, UNDECIDED),
         why="受けていただいたかけ直しの申し出を取り消した（同じ）。束 s の #18：#17 と同じ境目"),
    _row("C1", "時間はちょっと分からないです。", ["unknown"], AWAY_NO, scene=WITHDRAW_TIME_SCENE, other=LADDER_OTHER_NOT, reason=(DONT_KNOW_LABEL["unspecified"],),
         why="時刻が分からないとだけ言った＝わからない（取り消しではない＝同じ段の試しの言葉と正解そのまま）"),
    _row("R10", "あ、やっぱりパンフレットはいいです。", [WITHDREW], AWAY_NO, scene=WITHDRAW_SEND_SCENE,
         why="受けていただいたパンフレットの申し出を取り消した（伺い40 の段の送付先の質問への試しの言葉そのまま）"),
    # ── 分身 S3 の続き（CL-1 の頼み rv178/S3 の続き (2)＝棚卸しで「まだ0円」と残した行）
    # (7) 受けていただいた申し出のあとの方法の伺い N5（WITHDRAW_WAY_SCENE）：伺い40 の段の方法の質問への試しの言葉そのまま
    _row("N5", "あ、やっぱり詳しい方からのお話はいいです。", [WITHDREW], scene=WITHDRAW_WAY_SCENE,
         why="受けていただいた詳しい方からのお話の申し出（N4）を取り消した（伺い40 の段の方法の質問への試しの言葉そのまま）"),
    _row("N5", "メールはちょっと…", [UNDECIDED], scene=WITHDRAW_WAY_SCENE, reason=WHY_OTHER,
         why="取り消しではない（同じ段の試しの言葉と正解そのまま）。方法の答えは、並べた方法（訪問・お電話）のどれも選んでいない・メールを避けただけ＝決められない（説明の文と言葉から）"),
    # (8) 決め手を伺う一言 E5 のあと（ELSEWHERE_POINT_SCENE）：本書 9-4 の伺い48 の段の ⏳「E5 のお返事を読む問い合わせ＝…は決め手を言った・…は決め手は無い」の試しの言葉そのまま
    _row("E5", "料金が安いところですかね。", ["決め手を言った"], scene=ELSEWHERE_POINT_SCENE, why="決め手（料金）を言った（本書 9-4 の伺い48 の段の ⏳ の試しの言葉そのまま）"),
    _row("E5", "対応が早いので。", ["決め手を言った"], scene=ELSEWHERE_POINT_SCENE, why="決め手（対応）を言った（同じ）"),
    _row("E5", "特にないですね。", ["決め手は無い"], scene=ELSEWHERE_POINT_SCENE, why="決め手は特に無い（同じ）"),
    _row("E5", "前の担当が決めたので分からないです。", ["決め手は無い"], scene=ELSEWHERE_POINT_SCENE, why="決め手は分からない（同じ。説明の文の「決め手は特に無い・分からない…」）"),
    # (9) ほかのご希望 W1（G17 で「どちらも要らない」と言い切ったあと＝測りの W1 の場面 g73_system_words.wish_scenes）：会話の繋がらない「それで結構です。」＝決められない
    {**_row("W1@G17", "それで結構です。", [UNDECIDED], reason=WHY_OTHER,
            why="「それで」が何を指すか言葉から決まらない＝会話が繋がらない＝決められない（✅ 2026-11-05 日本時間23時19分06秒「「それで結構です。」は会話がつながっていない＝「無い」と"
                "受け取らず意味合いを尋ねる」・✅ 2026-11-08 日本時間16時34分23秒「判定が「電話」と読んだという判定もおかしい」＝本書 9-5 の会話1 の段の ⏳「W1 の試しの言葉に、この言葉と"
                "同じ形の言葉を「決められない」で入れて測る」）"), "set": "W1"},
    # (10) ほかにご納得いただけない点 N2（ご承認いただけなかったときの手順＝g53_meaning._approval_scene の other_point）：本書 9-5 の (5) の ⏳「点を言っていない言葉の試しの言葉を作って」
    #      ＝選べるもの「分からない点・確かめたい点があると言ったが、どの点かは言っていない」の言葉（言葉は説明の文から作った。点を言った側の言葉は正解の表の N2 の9行）
    {**_row("N2", "はい、まだあります。", ["点は言っていない"], why="ほかにもあると言ったが、どの点かは言っていない（選べるものの説明のとおり）"), "set": "N2"},
    {**_row("N2", "ええ、まだちょっとありますね。", ["点は言っていない"], why="同じ（どの点かは言っていない）"), "set": "N2"},
]


# お呼びする段（目的のお客様の判定＝G-59）の番号違いの試しの言葉（分身 AC＝頼みの (2)）のうち、ご本人の電話の行。行＝(相手の言葉, 場面, 正解)。
# 場面＝{"asked": 確かめる一言への返事か, "own_phone": ご本人の電話か}（判定の説明の文は flowctl.target_facets(asked, False, own_phone) の1か所）。正解＝{項目の名前:
# 正しい選べるものの集まり}（書いていない項目は数えない）。2026-11-20 rv176/S：答えを伏せた読み手2人（rv176/S/readers の束 c）の読みを当てた（2人がそろった読みはその読み・
# 割れた項目は決まりで決めた＝rv176/S/notes.md の 9。お呼びした方が出られない理由は、番号違いでは使わない＝言っていないも決められないも正解＝11-19 日本時間21時台の決め）。
# 会社の電話の行は G-59 の正解の表へ移した（g59_calling_judge.LINES_FULL）。ご本人の電話の行は、表の場面の業務（会社の電話＝g59_flow_check.b）では組めない＝ここに案として残す
# （表へ移すには、ご本人の電話の業務の印を表の行が持つ形が要る）
_SIDE_NONE = {END: {END_NONE}, HEAR: {HEAR_OK}, OFFER: {OFFER_NOT_SAID}}
CALLING_WORDS = [
    ("うちには岡田という者はおりませんが。", {"asked": False, "own_phone": True},
     {SPEAKER: {SPEAKER_SAME}, WHO: {WHO_RECEPTION}, PHONE: {PHONE_NONE}, ABOUT: {ABOUT_WRONG}, **_SIDE_NONE, ABSENT_WHY: {ABSENT_WHY_NOT_SAID, UNDECIDED}},
     "ご本人の電話（携帯電話の番号）：番号違いの説明「お呼びした方の電話ではない（そういう方はいない・…）」・在籍していないの説明から「そういう方はいない」を外した"),
    ("いえ、そういう方はいませんけど。", {"asked": False, "own_phone": True},
     {SPEAKER: {SPEAKER_SAME}, WHO: {WHO_RECEPTION}, PHONE: {PHONE_NONE}, ABOUT: {ABOUT_WRONG}, **_SIDE_NONE, ABSENT_WHY: {ABSENT_WHY_NOT_SAID, UNDECIDED}},
     "ご本人の電話（個人のお客様の呼ぶ一言「（名前）様のお電話でお間違えないでしょうか？」への返事）：そういう方はいない＝番号違い。話している方は読み手2人がそろって"
     "受付・取次ぎの方（お呼びした方をほかの人として話した）"),
    ("いいえ、違いますけど。", {"asked": True, "own_phone": True},
     {SPEAKER: {SPEAKER_SAME}, WHO: {WHO_RECEPTION}, PHONE: {PHONE_NONE}, ABOUT: {ABOUT_NONE}, **_SIDE_NONE, ABSENT_WHY: {ABSENT_WHY_NOT_SAID}},
     "ご本人の電話でも、「違います」だけは「そういう方はいない・番号の間違いではないか」とは伝えていない＝事情は言っていない"),
]


# 正解が言葉・説明の文・ご決定から決まらない試しの言葉（正解を付けない＝答えを伏せた読み手へ回す。数えには入れない）。場面は answer_words と同じ形（qid の質問）
TO_READERS = [
    # ⛔ 帳面 1 の (1) の分身 N の行の R8「うーん、どうでしょうねえ。」は、分身 S3 の続き (3) で束 s の #26 の読み手2人の読みを当てて answer_words へ移した
    # ⛔ 見直し (13) の D8 の H2「うーん、どうしようかな。」は、分身 S3 が帳面 1 の (1) の分身 AG の段の候補（答えの話・決められない）で PREP3_ROWS へ移した（同じ言葉を2か所に
    # 持たない。「考えている途中」との境目は、答えを伏せた読み手2人の束 rv178/S3/readers で確かめる）
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


# 会話AIの通話の試験（G-57）の別のお話の続き（分身 S3 の続き＝帳面 1 の (1) の分身 J・分身 G の段）。言葉は ⏳ に書いてあるとおり（書いていない「また別のお話」は表の R1 の世間話の言葉、
# 「聞き取れない」は相手役の台詞の表の聞き取れない台詞 unclear）。相手役の声はまだ作っていない（流すときに作る＝料金がかかる）
TALK_UNHEARD = "（聞き取れない台詞）"   # 相手役の台詞の表の unclear（make_callee_samples.UTTERANCES の1か所＝ここに言葉を書き写さない）
# お話に沿ったひとことのあとの2つ目のお返事：(業務の知っていること, 別のお話, 2つ目のお返事, 会話AIがすること)。会話AIがすること＝「質問に戻る」（replay_opening の resume）・
# 答えの報告（record_answer）・受け答え（もう1回のひとこと）・聞き返し（report_unclear）を選び分けるか（Flow._wait_after_word の知らせ＝分身 J の段）
TALK_FOLLOW_WORDS = [
    (KNOW, "うちは水曜がお休みなんですよ。", "そうなんですね。", "質問に戻る"),
    (KNOW, "うちは水曜がお休みなんですよ。", "じゃあ振込で", "答えの報告"),
    (KNOW, "うちは水曜がお休みなんですよ。", "いやあ最近は暑くてねえ。", "受け答え"),
    (KNOW, "うちは水曜がお休みなんですよ。", TALK_UNHEARD, "聞き返し"),
]
# 質問の種類ごとの別のお話（分身 G の段＝いまの質問に関わるお話「うちは月曜が定休日なんですよ。」・関わらないお話「いやあ最近は暑くてねえ。」を、申し出・方法の提案・ご承認のお知らせ
# それぞれ）：(種類, 業務の知っていること, 別のお話, 正解)。決まりの例の言葉は、支払日の事実の無い業務でだけ使う（D-13）
TALK_KIND_WORDS = [(kind, KNOW_NO_DAY, words, truth) for kind in ("申し出", "方法の提案", "ご承認のお知らせ")
                   for words, truth in (("うちは月曜が定休日なんですよ。", "中身を受けたひとこと・支払日の事実を作らない（いまの質問に関わるお話）"),
                                        ("いやあ最近は暑くてねえ。", "中身を受けたひとこと・事実を言わない（いまの質問に関わらないお話）"))]
# 種類ごとの場面の作り：(業務の質問の番号, その前のお客様の言葉, 別のお話のあとの答えの言葉, 答えを取る質問と答え, 業務設定のほかの欄)。方法の提案＝ご承認いただけなかったときの手順の
# 申し出 N4（試験 EndJudgedTest の to_offer と同じ言葉で N4 まで進む）
TALK_KIND_SETUP = {
    "申し出": (("R9",), (), "はい、お願いします。", ("R9", "はい"), {}),
    "方法の提案": (("R12", "R13"), ("はい、届いています。", "いいえ、承認はできないですね。", "うーん、なんとなくですかね。", "うーん、特にこれというのは…。"),
               "はい、お願いします。", ("N4", "はい"), {"handoff_ways": HANDOFF_WAYS}),
    "ご承認のお知らせ": (("R7",), (), "はい、分かりました。", ("R7", "はい"), {}),
}


def talk_lines() -> list[str]:
    """G-57 の相手役の台詞にする言葉の全部（試しの言葉の表の1か所＝make_callee_samples が名前を付けて台詞の表に入れる）。"""
    out = [w for _, w, _ in TALK_WORDS] + [w for _, w, r, _ in TALK_FOLLOW_WORDS for w in (w, r) if w != TALK_UNHEARD]
    out += [w for _, _, w, _ in TALK_KIND_WORDS] + [w for _, pre, ans, _, _ in TALK_KIND_SETUP.values() for w in (*pre, ans)]
    return list(dict.fromkeys(out))


def counts() -> dict[str, int]:
    """足した試しの言葉の数（表ごと）。G-63 は添える形ごと（選択肢つき・質問だけ・添えない）に流すので、その数も。"""
    return {"G-53": len(answer_words()), "G-53（読み手へ）": len(TO_READERS), "G-63": len(GROUND_WORDS), "G-63（3つの形で流す行）": len(GROUND_WORDS) * 3,
            "取込の中身の判定": len(IMPORT_WORDS), "G-57": len(TALK_WORDS), "G-57（2つ目のお返事）": len(TALK_FOLLOW_WORDS),
            "G-57（質問の種類ごと）": len(TALK_KIND_WORDS), "G-59（正解の表へ足す案）": len(CALLING_WORDS),
            "G-53（判定に送る文が変わった場面の行）": sum(1 for it in answer_words() if it.get("scene") in NEW_SCENES)}
