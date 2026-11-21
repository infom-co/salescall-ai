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
                     OFFER_NOT_SAID, AWAY_NO, AWAY_YES, CALL_LATER, DONT_KNOW_LABEL, ELSEWHERE, ELSEWHERE_ALL, ELSEWHERE_NO, HESITATING, KEEP_ON,
                     END_PRESSED, END_REFUSED, LADDER_NEITHER, LADDER_OTHER, LADDER_OTHER_NOT, LADDER_OTHER_OK, LADDER_OTHER_UNSURE, PHONE, PHONE_NONE, PROMPT_REPLY, PROMPT_REPLY_NO, PROMPT_REPLY_WAVER, PROMPT_REPLY_YES, QUERY, QUERY_NO, QUERY_YES,
                     REFUSED_LABEL, REPEAT, HEAR_LOST, SPEAKER, SPEAKER_SAME, TARGET_IS_AWAY, THINKING, VALUE_CHOICE, WAIT_NOW, WHO, WHO_RECEPTION, WHY_NOT_SAID, WRONG,
                     WRONG_NOT_SAID, Question, accepted_line, alternatives_form, aside_form, callback_question, ladder_side_name, pain_question, since_form,
                     NEAR, NEAR_OFF, MISGIVING, MISGIVING_OFF, MISGIVING_UNSAID, UNTIL_WHY, UNTIL_LABEL, UNTIL_WHY_NOT_SAID, CYCLE, CYCLE_LABEL, CYCLE_NOT_SAID)
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
# 「〇時まで」の理由を伺い直した一言のあと（日時の2択の段の時刻の2択に「3時までなら大丈夫です。」＝理由が分からない＝Flow._ask_until の「恐れ入ります。3時までということはそれ以降は
# お電話が難しいということでしょうか？」。伺い63＝判定に見せる質問はこの一言。試験 UntilAskAgainTest と同じ手順。分身 Q の (4)＝n9）
UNTIL_ASK_SCENE = "「〇時まで」の理由を伺い直した一言のあと（時刻の2択に「3時までなら大丈夫です。」）"
# 「〇時まで」の理由の判定の読みが割れ、具体的な理由の向きで伺い直した一言のあと（時刻の2択に「3時までには戻ってます。」・判定2つの読みが「それまでは出られない」と「理由は言っていない」＝
# Flow._ask_until の「恐れ入ります。3時まではお電話が難しいということでしょうか？」。伺い98 ア＝判定に見せる質問はこの一言。試験 UntilLeanTest と同じ手順。分身 AB の (1)）
UNTIL_LEAN_SCENE = "「〇時まで」の理由を向きのある一言で伺い直したあと（時刻の2択に「3時までには戻ってます。」・判定の読みが割れた）"
# 何の日時かの一文（flowctl.asks_about）を判定に見せる日時の2択のうち、正解の表・試しの言葉の表に行が無かった場面（分身 U の (3)＝分身 S の書き置きの (6-2)）：お客様から折り返して
# いただく日の2択（伺い67。答えの段のお客様ご本人の重ねての申し出のあと＝試験 TheirsDayPairTest・CallbackWishTest と同じ手順）・依頼者の日時の質問（伺い62）の2つ目の2択（試験
# PLadderOpeningTest と同じ手順）・顧客獲得型のアポの日時のはじめの2択（試験 _p_to_meeting と同じ手順）。場面は g53_meaning._theirs_pair_scene・_accept_pair_scene・_meeting_pair_scene
THEIRS_PAIR_SCENE = "お客様から折り返していただく日の2択（「平日の4時までには折り返します。」のあとの「本日と明日」）"
ACCEPT_PAIR_SCENE = "依頼者の日時の質問の2つ目の2択（ご来店は平日と答えたあとの曜日の2択）"
MEETING_PAIR_SCENE = "顧客獲得型のアポの日時のはじめの2択（悩みがあると言われ、お時間の文のあとの平日と土日）"
# 並べ直した2回目の2択（分身 W の (5)＝分身 V の書き置き rv184/V/notes.md の (6-5)）：お客様から折り返していただく日の2択に片方だけ合わない（もう一方は不安そう）・2つとも合わないと言われたあとの
# 2択（分身 V の (1)(2)）と、日時の2択の言い直しに日だけ直され、伺えた時刻にその日は電話をかけられないときの、その日の時刻の2択（分身 W の (1)）。場面は g53_meaning._theirs_again_scene・
# _ladder_redo_scene
THEIRS_SIDE_AGAIN_SCENE = "お客様から折り返していただく日の2回目の2択（「本日はちょっと難しいですね。」のあとの「明日と火曜日」）"
THEIRS_NEITHER_AGAIN_SCENE = "お客様から折り返していただく日の2回目の2択（「どちらもちょっと難しいですね。」のあとの「火曜日と水曜日」）"
LADDER_REDO_SCENE = "日時の2択の段の言い直しに日だけ直されたあとの時刻の2択（「いえ、水曜日でお願いします。」・水曜日の4時は空いている予定に無い＝「水曜日でしたら2時と3時」）"
NEW_SCENES = (LADDER_SCENE, WRONG_ASK_SCENE, WAIT_SCENE, PAIN_SCENE, CONCERN_SCENE, CONFIRM_CONCERN_SCENE, LADDER_CLOCK_SCENE, ANSWERED_SCENE, OFFER_FINAL_SCENE,
              WITHDRAW_TIME_SCENE, WITHDRAW_SEND_SCENE, WITHDRAW_WAY_SCENE, ELSEWHERE_POINT_SCENE, UNTIL_ASK_SCENE, THEIRS_PAIR_SCENE, ACCEPT_PAIR_SCENE,
              MEETING_PAIR_SCENE, THEIRS_SIDE_AGAIN_SCENE, THEIRS_NEITHER_AGAIN_SCENE, LADDER_REDO_SCENE, UNTIL_LEAN_SCENE)
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
            "W1@G17": _wish_question("W1@G17"),
            # R14＝お客様に日時を選んでいただく業務の質問（伺い62。試験 ChooseWhenLadderTest の質問と同じ文と聞くこと＝ACCEPT_PAIR_SCENE）・R15＝顧客獲得型のアポの日時（測りの顧客獲得型の
            # 業務の最初の質問 g53_meaning.ACQUIRE_SCENE_BUSINESS の番号だけ替えたもの＝MEETING_PAIR_SCENE。番号は測りの質問の表 g53_meaning.question_map の Q1・Q2 と重ねない）。分身 U の (3)
            # C1T＝お客様から折り返していただく時刻の質問（お言葉に甘えたあとの theirs_later＝本番の1か所 callback_question。番号は C1 のまま＝表の鍵だけ C1T。THEIRS_PAIR_SCENE）
            "C1T": callback_question("theirs_later"),
            "R14": Question("R14", "ご来店の日時はいつがよろしいでしょうか？", "datetime", conditions=("答えた",), required=True, topic="ご来店の日時"),
            "R15": _meeting_question()}


@functools.cache
def _meeting_question() -> Question:
    """顧客獲得型のアポの日時の質問（測りの顧客獲得型の業務の最初の質問の番号だけ R15 に替えたもの＝ここに文を書き写さない）。"""
    import dataclasses
    from g53_meaning import ACQUIRE_SCENE_BUSINESS
    return dataclasses.replace(ACQUIRE_SCENE_BUSINESS().questions[0], id="R15")


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


# 迷っている点を言ったか（flowctl.MISGIVING）の正解の理由（行ごとに答えの正解を読んで当てた。帳面 0-3 の (2)＝一括で既定を付けない。分身 P の (4)＝n2）：
# 答えの正解に「迷っている」が無い行は、項目の説明「答えの選べるものが「迷っている」ではない」から決まる。説明した内容のご承認の質問（R7）の行には付けない（項目を見せない＝
# flowctl.misgiving_fits。迷いの点は N1 を流す前の判定の読み直し Flow._point_ahead が答える。分身 Q の (2)：前に付けていた「迷っていない」の 12 行を1行ずつ外した＝外した行と理由は
# rv183/Q/probe/q2/unset_misgiving_rw.py）
# 答えを伏せた読み手の束 r3（判定が見る文が今の形＝分身 R の (3)(4) のあと。行の正解を当て直した所の注に書く名前＝分身 S の (3)）
READERS_R3 = "答えを伏せた読み手 A・B（束 r3＝rv184/R/readers）"
# 区切りの時刻（「〇時まで」）の境目の1か所の名前（分身 U の (1)＝flowctl.ladder_choice_desc の「（避けた方）だけ合わない」の説明の終わり）と、その説明の文で読み直す束
BOUNDARY_WHY = "「（避けた方）だけ合わない」の説明の見分けの1か所 flowctl.ladder_choice_desc「その時刻まで・その時刻から、と区切りの時刻を言ったなら…「答えた」」＝分身 U の (1)"
READERS_U1 = "⏳ 答えを伏せた読み手 A・B の読み直し待ち（束 u1＝rv184/U/readers）"
MG_OFF_WHY = "答えの正解に「迷っている」が無い＝項目の説明から「迷っていない」（行の答えの正解を読んで当てた）"
# 迷っている・決めかねているとだけ言った行（答えの正解の「迷っている」の読みでは、わけを言っていない。ほかの答えの読みでは「迷っていない」）
MG_UNSAID_WHY = ("迷っている・どれがよいか決めかねている、とだけ言い、決めかねているわけは言っていない＝「迷っているが点は言っていない」。答えの正解のほかの読み"
                 "（考えている途中・決められない）では「迷っていない」")


def _row(qid: str, text: str, accept, away=None, query=QUERY_NO, prompt=None, hedged=False, asked: str | None = None, why: str = "",
         reason=None, wrong=None, scene=None, value: str | None = None, end=None, other=None, elsewhere=None, near=None, misgiving=None, misgiving_why: str = "",
         until=None, until_note: str = "", cycle=None, cycle_note: str = "") -> dict:
    label = {"accept": list(accept), "why": why}
    if hedged:
        label["hedged"] = True
    if reason:
        label["reason"] = list(reason)
    # end＝電話を終える言葉かの正解（無ければ「言っていない」＝g53_meaning.end_accept）・other＝合わないと言わなかったほうで大丈夫そうかの正解（日時の2択の段の行だけ＝
    # g53_meaning.ladder_other_truths。分身 S2）・elsewhere＝ほかの会社に頼んでいる・使っていると言ったかの正解（無ければ「言っていない」＝g53_meaning.elsewhere_truths。
    # 正解の表 g53_labels.json の行と同じ欄。分身 S4）・near＝どの答えもはっきり指していないときのいちばん近い答えの正解（言い直さない質問の最初の答えだけ＝g53_meaning.near_truths。
    # 分身 B（rv179 の束1 の B1））・misgiving＝迷っている点を言ったかの正解（迷っているお客様に応じる質問の行だけ＝g53_meaning.misgiving_truths。misgiving_why＝その理由。
    # 分身 P（CL-1 の頼み rv183/P の (4)＝n2））
    if misgiving is not None:
        label["misgiving_why"] = misgiving_why
    # until＝「〇時まで」の理由の正解・cycle＝月単位の理由の正解（かけ直しの時刻を伺う質問の行だけ＝g53_meaning.until_truths・cycle_truths。until_note・cycle_note＝その理由。
    # 分身 Q（CL-1 の頼み rv183/Q の (4)＝n9））
    if until is not None:
        label["until_why_why"] = until_note
    if cycle is not None:
        label["cycle_why"] = cycle_note
    if end is not None:
        label["end"] = list(end) if isinstance(end, (list, tuple)) else [end]
    for key, got in ((AWAY, away), (QUERY, query), (PROMPT_REPLY, prompt), (WRONG, wrong), (LADDER_OTHER, other), (ELSEWHERE, elsewhere), (NEAR, near),
                     (MISGIVING, misgiving), (UNTIL_WHY, until), (CYCLE, cycle)):
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
        _row("R1", "うちは月曜が定休日なんですよ。", [UNDECIDED], AWAY_YES, why="聞かれたことと関わりのないご事情", reason=WHY_OTHER, misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY),
        _row("R1", "いやあ最近は暑くてねえ。", [UNDECIDED], AWAY_YES, why="世間話", reason=WHY_OTHER, misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY),
        _row("R1", "そういえばこの前の台風、大変だったんですよ。", [UNDECIDED], AWAY_YES, why="世間話", reason=WHY_OTHER, misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY),
        _row("R1", "そうですね。", [UNDECIDED, THINKING], AWAY_NO, reason=WHY_OTHER,
             why="相づちだけ＝答えの話。言いよどみ・言い終えていない言葉＝考えている途中も正解（11-06 11時19分55秒「(1) ア」で足した選べるもの・F5 の 11-15）。"
                 "「わからない」（unknown）は、わからない・まだ決めていない、とは言っていないので入れない（選べるものの説明のとおり）", misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY),
        _row("R1", "振込じゃないほうがいいかなとは思いますけど", ["カード"], AWAY_NO, hedged=True, why="寄った考え（言い切っていない）", misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY),
        _row("R1", "うーん、どっちがいいですかねえ。", [HESITATING, THINKING, UNDECIDED], AWAY_NO, None, reason=WHY_HESITATING,
             why="迷っている・考えている途中・こちらの考えを尋ねた（「〜ですかね」＝11-06 4時11分28秒の2つの意味）の境目（F5 の 11-15）。問いは付けない"
                 "＝どちらの読みも正解（正解の表の「〜のほうがいいですかね」の行と同じ）。「わからない」（答えを持っていない）とは言っていないので外した"
                 "（F4 の (c) の 11-15・2人の読み手がそろった）", misgiving=[MISGIVING_UNSAID, MISGIVING_OFF], misgiving_why=MG_UNSAID_WHY),
        _row("R1", "振込だと手数料はどちら持ちですか？", [UNDECIDED], AWAY_NO, QUERY_YES, why="こちらへ尋ねた問い", reason=WHY_OTHER, misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY),
        # こちらの名前・会社名を頼みの形で聞いた（「こちらへ尋ねた問いがある」の説明に入れた＝まとめ役 CL-1 の頼み rv178/X の6回目 (1)(3)。✅ 2026-11-06 日本時間15時20分30秒
        # 「(1)…OK」＝こちらの名前・会社名を尋ねたのは「もう一度言ってほしい」ではない）。答えは言っていない。別のお話かは付けない（どちらの読みも正解）
        _row("R1", "もう一度お名前をよろしいですか？", [UNDECIDED], None, QUERY_YES, why="頼みの形でこちらの名前を聞いた＝こちらへ尋ねた問い", reason=WHY_OTHER, misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY),
        _row("R1", "すみません、会社名をもう一度教えてもらえますか？", [UNDECIDED], None, QUERY_YES, why="頼みの形でこちらの会社名を聞いた＝こちらへ尋ねた問い",
             reason=WHY_OTHER, misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY),
        _row("R1", "うちは振込だと手間なんですよね。", [UNDECIDED, "カード"], AWAY_YES, hedged=True, reason=WHY_OTHER,
             why="答えの手がかりだけのご事情＝別のお話（✅ 2026-11-15 日本時間21時55分35秒「伺い26：ア」）。振込を避ける向きに寄った考え"
                 "（カード・言い切っていない）とも読める＝どちらの読みも正解（2026-11-16 rv38＝答えを伏せた読み手2人＋CL-1。直す前の値＝"
                 "答え [決められない]・答えの話）", misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY),
        # ほかの形の質問（日にちの質問への答えの手がかりだけのご事情）
        _row("R2", "うちは月曜が定休日なんですよ。", [UNDECIDED], AWAY_YES, reason=WHY_OTHER,
             why="日にちの答えの手がかりだけのご事情＝別のお話（✅ 2026-11-15 日本時間21時55分35秒「伺い26：ア」はこの言葉そのもの。"
                 "2026-11-16 rv38。直す前の値＝答えの話）", misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY),
        _row("R3", "はい、決まりました。", [UNDECIDED], AWAY_NO, why="その後の形に、値の無い「決まりました」", reason=WHY_OTHER, misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY),
        _row("R3", "うちは月曜が定休日なんですよ。", [UNDECIDED], AWAY_YES, why="その後の形への別のお話", reason=WHY_OTHER, misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY),
        _row("R4", "はい、分かりました。", [UNDECIDED], why="事実を伺うその後の形に、値の無い「分かりました」", reason=WHY_OTHER),
        _row("R4", "はい、田中です。", ["答えた"], why="事実を言った"),
        _row("R5", "そうなんですね。", [UNDECIDED], AWAY_NO, why="お話に沿ったひとことのあとの相づちだけ", reason=WHY_OTHER, misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY),
        _row("R5", "じゃあ振込で。", ["振込"], AWAY_NO, misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY),
        _row("R6", "はい。", [UNDECIDED], AWAY_NO, why="並べた文への「はい」はどれも選んでいない", reason=WHY_OTHER, misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY),
        _row("R6", "お振込で。", ["振込"], AWAY_NO, misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY),
        _row("R6", "カードかなあ。", ["カード"], AWAY_NO, hedged=True, misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY),
        # 言い直し・最終の確かめ（分身 O2・N の行）
        # ご承認の質問（はい／いいえ）の最終の確かめは、判定の選べるものが「はい」「いいえ」（⛔ F5 の 11-15 まで、値の言い直しへの返事の名前 affirmed・denied で
        # 書いていた＝判定が選べない名前＝正しい読みも外れと数えた）
        {**_row("R7", "はい、大丈夫です。", ["はい"], asked=accepted, why="最終の確かめへのご了承"), "set": "D"},
        {**_row("R7", "いや、それはちょっと困るんですけど。", ["いいえ"], asked=accepted, why="ご了承いただけない"), "set": "D"},
        {**_row("R7", "はい、大丈夫ですよ。", ["はい"], AWAY_NO, asked=unheard,
                why="聞き取れなかったお知らせの最終の確かめへのご了承（分身 O2）。束 t の #1：読み手2人とも答えは「はい」・別のお話かは「答えの話」＝そろった読みにした"
                    "（直す前の値＝別のお話か付けていない＝どちらの読みも正解。どちらの読みでも流れの制御はお礼の締め＝rv178/S4/probe/split_t.txt）"), "set": "D"},
        # 黙ったあとの一言のあと（分身 N・O2）
        _row("R7", "あ、それとですね、うちは月曜が定休日なんですよ。", [UNDECIDED], (AWAY_YES, AWAY_NO), prompt=PROMPT_REPLY_NO, reason=WHY_OTHER,
             why="声かけへのお返事だけではない別のお話。別のお話かは境目：声かけ（気になる点）へのお返事として話したご事情＝答えの話（CL-1 の 9-3 の案の (別のお話か)）とも、"
                 "どの答えの向きも示さないご事情＝別のお話とも読める（2026-11-20 rv176/S2 の束 n の #1＝答えを伏せた読み手2人が2人とも両方を挙げた・ご判断は無い。直す前の値＝別のお話）"),
        _row("R7", "いえ、大丈夫です。", [UNDECIDED], AWAY_NO, prompt=PROMPT_REPLY_YES, why="声かけへのお返事だけ", reason=WHY_OTHER),
        # 帳面 1 の (1) の「この1回の測りに足す」と書かれた答えの段の言葉（分身 Y2 が入れた。正解は帳面に書かれたとおり＝お言葉・決まった説明の文から決まる）
        # 言い直し・最終の確かめへのお返事の別のお話か（分身 N＝5a7f04ce・見直し (12) の A-9・B-3）：金額の言い直し・ご承認のお知らせの最終の確かめ
        {**_row("R8", "うちは月曜が定休日なんですよ。", [UNDECIDED], AWAY_YES, asked=budget_again, why="言い直しへの別のお話（分身 N の行）", reason=WHY_OTHER, misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY),
         "set": "D"},
        {**_row("R8", "はい、大丈夫です。", ["affirmed"], AWAY_NO, asked=budget_again, why="言い直しを認めた＝答えの話（答えは合っている。分身 N の行）", misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY),
         "set": "D"},
        # 分身 N の行の「うーん、どうでしょうねえ。」（前は答えが決まらず TO_READERS）：束 s の #26＝読み手2人とも答えは「決められない」「考えている途中」「迷っている」・答えの話・
        # こちらへ尋ねた問いは無い＝そろった読みで表に入れた（分身 S3 の続き (3)）
        {**_row("R8", "うーん、どうでしょうねえ。", [UNDECIDED, THINKING, HESITATING], AWAY_NO, asked=budget_again, reason=WHY_HESITATING,
                why="言い直しへの迷い・言いよどみ（rv178/S3/readers の束 s の #26 の読み手2人がそろった）", misgiving=[MISGIVING_UNSAID, MISGIVING_OFF], misgiving_why=MG_UNSAID_WHY), "set": "D"},
        {**_row("R7", "そういえば来週は祝日なんですよ。", [UNDECIDED], AWAY_YES, asked=accepted, why="最終の確かめへの別のお話（分身 N の行）", reason=WHY_OTHER),
         "set": "D"},
        {**_row("R7", "あ、はい、分かりました。", ["はい"], AWAY_NO, asked=accepted, why="最終の確かめへのご了承＝答えの話（分身 N の行）"), "set": "D"},
        # お知らせへの最初のお返事の別のお話か（見直し (13) の持ち場 C の C4）
        _row("R7", "なるほど。", [UNDECIDED], AWAY_NO, why="お知らせへの相づちだけ＝答えの話（決められない。C4）", reason=WHY_OTHER),
        _row("R7", "うちは月曜が定休日なんですよ。", [UNDECIDED], AWAY_YES, why="お知らせへの別のお話（C4）", reason=WHY_OTHER),
        # 別のお話か の答えの話（分身 E＝092f2317：分からない・答えたくない、は答えの話）
        _row("R1", "ちょっと分からないですね。", ["unknown"], AWAY_NO, why="分からないとだけ言った＝答えの話（分身 E の行）",
             reason=(DONT_KNOW_LABEL["unspecified"],), misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY),
        _row("R1", "それは言いたくないです。", ["refused"], AWAY_NO, why="言いたくないとだけ言った＝答えの話（分身 E の行）",
             reason=(REFUSED_LABEL["unwilling"],), misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY),
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
        *PAIR_ROWS,
        *AGAIN_ROWS,
        *ASK_ROWS,
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
         why="会社・お店の電話の番号違いの説明「番号の間違いではないか・そういう会社やお店ではない」をそのまま言った", misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY),
    _row("R1", "うちはお店じゃないですけど、番号をお間違えではないですか？", [UNDECIDED], query=None, wrong=ABOUT_WRONG, reason=WHY_OTHER,
         why="携帯電話の番号にかけた目的のお客様のいない業務（どなたもお呼びしない＝説明は会社・お店の電話の文）：そういうお店ではない・番号の間違いではないか", misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY),
    # 繋いだあとの答えの段・会社の電話
    _row("R1", "え、うちは山田ですけど、番号をお間違えではないですか？", [UNDECIDED], query=None, wrong=ABOUT_WRONG, reason=WHY_OTHER, scene="繋いだあと・会社の電話",
         why="番号の間違いではないか、と伝えた（分身 Z の書き置き rv124/Z の 4 の (b) の例）", misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY),
    _row("R1", "そういう者はおりませんが。", [UNDECIDED], wrong=WRONG_NOT_SAID, reason=WHY_OTHER, scene="繋いだあと・会社の電話",
         why="会社の電話の番号違いの説明は「番号の間違い・そういう会社やお店ではない」だけ（「そういう方はいない」は入れていない＝お呼びする段なら在籍していない）"
             "＝番号違いとは言っていない", misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY),
    # 繋いだあとの答えの段・ご本人の電話
    _row("R1", "え、そういう方はいませんけど。", [UNDECIDED], wrong=ABOUT_WRONG, reason=WHY_OTHER, scene="繋いだあと・ご本人の電話",
         why="ご本人の電話の番号違いの説明「お呼びした方の電話ではない（そういう方はいない・…）」のとおり", misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY),
    _row("R1", "番号をお間違えではないですか？", [UNDECIDED], query=None, wrong=ABOUT_WRONG, reason=WHY_OTHER, scene="繋いだあと・ご本人の電話",
         why="ご本人の電話の番号違いの説明「番号の間違いではないか、と伝えた」", misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY),
    _row("R1", "じゃあ振込でお願いします。", ["振込"], AWAY_NO, wrong=WRONG_NOT_SAID, scene="繋いだあと・ご本人の電話",
         why="答えた（番号違いの説明の「と伝えた」に当たる言葉が無い＝番号違いとは言っていない。番号違いの説明の替わる場面の、ふつうの答えの行）", misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY),
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
             "（分身 U2 の書き置き rv176/U2/notes.md の 8 の (1)）", misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY, until=[UNTIL_WHY_NOT_SAID], until_note="「（時刻）まで」とは言っていない（2つとも難しいとだけ言った）", cycle=[CYCLE_NOT_SAID], cycle_note="月ごとにくり返す都合は言っていない（2つとも難しいとだけ言った）"),
    _row("C1", "うーん、どっちがいいかなあ。", [HESITATING, THINKING], AWAY_NO, scene=LADDER_SCENE, other=LADDER_OTHER_NOT,
         why="2つのあいだで迷っていると言った＝迷っている。言いよどみで、このあと続けて答えそう＝考えている途中、とも読める（正解の表の R1「うーん、どっちがいいですかねえ。」"
             "と同じ境目。2026-11-20 rv176/S の束 n の読み手2人も2人とも同じ2つ）。片方だけ合わないとは言っていない", misgiving=[MISGIVING_UNSAID, MISGIVING_OFF], misgiving_why=MG_UNSAID_WHY, until=[UNTIL_WHY_NOT_SAID], until_note="「（時刻）まで」とは言っていない（迷っているとだけ言った）", cycle=[CYCLE_NOT_SAID], cycle_note="月ごとにくり返す都合は言っていない（迷っているとだけ言った）"),
    _row("C1", "金曜日でお願いします。", [VALUE_CHOICE], AWAY_NO, scene=LADDER_SCENE, value="金曜日", other=LADDER_OTHER_NOT,
         why="並べた2つの1つ（金曜日）を選んだ＝答えた（2026-11-20 rv176/S の束 n の読み手2人がそろった）。片方だけ合わないとは言っていない", misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY, until=[UNTIL_WHY_NOT_SAID], until_note="「（時刻）まで」とは言っていない（曜日を選んだだけ）", cycle=[CYCLE_NOT_SAID], cycle_note="月ごとにくり返す都合は言っていない（曜日を選んだだけ）"),
    _row("C1", "来週の月曜日なら空いてます。", [VALUE_CHOICE], AWAY_NO, scene=LADDER_SCENE, value="来週の月曜日", other=LADDER_OTHER_NOT,
         why="2択の外の日を言った＝答えた（日時の2択の段は2択の外の答えも受け取る＝Flow._ladder_take）。✅ 2026-11-20 日本時間17時台 CL-1 が決まりで決めた（9-3 の案の (2択)：答えた＝"
             "日・時刻を言った・並べた2つの外でも／どちらも合わない＝並べた2つのどちらも合わないとだけ言った＝4時44分42秒の伺い60「2.別の空スケジュールの2つ」の場面）＝"
             "どちらも合わないは外した（直す前の値＝[答えた, どちらも合わない]＝2026-11-20 rv176/S の束 n の読み手2人が、説明が名前のくり返しだった文で2人とも両方を挙げた）", misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY, until=[UNTIL_WHY_NOT_SAID], until_note="「（時刻）まで」とは言っていない（日を言っただけ）", cycle=[CYCLE_NOT_SAID], cycle_note="月ごとにくり返す都合は言っていない（空いている日を言っただけ）"),
    _row("C1", "金曜日はちょっと…。", [ladder_side_name("金曜日")], AWAY_NO, scene=LADDER_SCENE, value="火曜日", other=LADDER_OTHER_UNSURE,
         why="並べた2つの片方（金曜日）だけを避けた＝金曜日だけ合わない（✅ 2026-11-20 日本時間16時13分55秒「他も書いてることばかりだろ！」の伺い94＝この言葉そのものが片方だけ避けた"
             f"場面）。火曜日で大丈夫かは言っていない＝{LADDER_OTHER_UNSURE}（16時25分52秒「伺い94：はもう一方で大丈夫そうなら、エでいいし、もう一方では不安そうならウでしょ」）。"
             "直す前の値＝[決められない, 答えた, 考えている途中]（選べるものに片方だけ合わないが無かった頃の境目＝rv176/S の束 n）", misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY, until=[UNTIL_WHY_NOT_SAID], until_note="「（時刻）まで」とは言っていない（片方の曜日を避けただけ）", cycle=[CYCLE_NOT_SAID], cycle_note="月ごとにくり返す都合は言っていない（片方の曜日を避けただけ）"),
    _row("C1", "金曜日はちょっと…。火曜日は空いてますけど。", [ladder_side_name("金曜日")], AWAY_NO, scene=LADDER_SCENE, value="火曜日", other=LADDER_OTHER_OK,
         why="金曜日だけを避け、火曜日は空いていると言ったが、火曜日にすると、はっきり選んではいない＝金曜日だけ合わない・もう一方で大丈夫そう（伺い94 のエ＝16時25分52秒。"
             "分身 U2 の書き置きの 8 の (1) の案。rv176/S2 の束 n の #8＝読み手2人が2人とも「答えた」とも読んだ＝ご判断（16時25分52秒）で決めて説明の1か所を直した）", misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY, until=[UNTIL_WHY_NOT_SAID], until_note="「（時刻）まで」とは言っていない（曜日を避け、もう一方は空いていると言っただけ）", cycle=[CYCLE_NOT_SAID], cycle_note="月ごとにくり返す都合は言っていない（空いている曜日を言っただけ）"),
    _row("C1", "金曜日はちょっと…。火曜日もどうかなあ。", [ladder_side_name("金曜日")], AWAY_NO, scene=LADDER_SCENE, value="火曜日", other=LADDER_OTHER_UNSURE,
         why=f"金曜日だけを避け、火曜日も不安そうに言った＝金曜日だけ合わない・{LADDER_OTHER_UNSURE}（伺い94 のウ＝16時25分52秒「…もう一方では不安そうならウでしょ」。分身 U2 の書き置きの"
             " 8 の (1) の案。rv176/S2 の束 n の #9＝読み手2人が2人とも「どちらも合わない」とも読んだ＝ご判断で決めて説明の1か所を直した）", misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY, until=[UNTIL_WHY_NOT_SAID], until_note="「（時刻）まで」とは言っていない（曜日を避け、もう一方も不安そうに言っただけ）", cycle=[CYCLE_NOT_SAID], cycle_note="月ごとにくり返す都合は言っていない（曜日の都合だけ）"),
    _row("C1", "金曜日はちょっと…火曜日なら。", [ladder_side_name("金曜日")], AWAY_NO, scene=LADDER_SCENE, value="火曜日", other=LADDER_OTHER_OK,
         why="金曜日を避けて、火曜日なら（大丈夫）と言った＝金曜日だけ合わない・もう一方で大丈夫そう（✅ 2026-11-20 日本時間16時25分52秒「伺い94：はもう一方で大丈夫そうなら、エでいいし…」"
             "＝エ「それでは火曜日でしたら…」）。2026-11-20 rv176/S2 の束 n の #10＝答えを伏せた読み手2人が2人とも「答えた」「金曜日だけ合わない」の両方を挙げ、境目が分かりにくいと書いた"
             "＝ご判断で決めて「（避けた方）だけ合わない」の説明の1か所を直した（直す前の値＝答えた・片方だけ合わないとは言っていない＝分身 U2 の書き置きの 8 の (1) の候補）", misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY, until=[UNTIL_WHY_NOT_SAID], until_note="「（時刻）まで」とは言っていない（曜日を避け、もう一方ならと言っただけ）", cycle=[CYCLE_NOT_SAID], cycle_note="月ごとにくり返す都合は言っていない（曜日の都合だけ）"),
    _row("C1", "火曜日はちょっと難しいですね。", [ladder_side_name("火曜日")], AWAY_NO, scene=LADDER_SCENE, value="金曜日", other=LADDER_OTHER_UNSURE,
         why="並べた2つのうちあとの方（火曜日）だけを難しいと言った＝火曜日だけ合わない・金曜日で大丈夫かは言っていない（伺い94。分身 U2 の書き置きの 8 の (1) の案）", misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY, until=[UNTIL_WHY_NOT_SAID], until_note="「（時刻）まで」とは言っていない（片方の曜日を避けただけ）", cycle=[CYCLE_NOT_SAID], cycle_note="月ごとにくり返す都合は言っていない（片方の曜日を避けただけ）"),
    # 時刻の2択（LADDER_CLOCK_SCENE＝「2時と4時ではどちらがよろしいでしょうか？」）：片方だけ合わないのエは最後の段なので、避けなかった時刻で今の言い直しの確かめ
    _row("C1", "2時はちょっと…。4時なら空いてるかな。", [ladder_side_name("2時")], AWAY_NO, scene=LADDER_CLOCK_SCENE, value="4時", other=LADDER_OTHER_OK,
         why="2時だけを避け、4時なら空いていそうと言った（「かな」＝はっきり選んではいない）＝2時だけ合わない・もう一方で大丈夫そう（伺い94 のエ。分身 U2 の書き置きの 8 の (1) の案。"
             "rv176/S2 の束 n の #12＝読み手2人が2人とも「答えた」とも読んだ＝16時25分52秒のご判断で決めて説明の1か所を直した。答えたでも同じ言い直しの確かめ）", misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY, until=[UNTIL_WHY_NOT_SAID], until_note="「（時刻）まで」とは言っていない（時刻を避け、もう一方なら空いていそうと言っただけ（「なら」は時刻の手前までの意味ではない））", cycle=[CYCLE_NOT_SAID], cycle_note="月ごとにくり返す都合は言っていない（時刻の都合だけ）"),
    # (n9) 「〇時まで」の理由・月単位の理由（分身 Q＝CL-1 の頼み rv183/Q の (4)。答えとは別の項目 flowctl.UNTIL_WHY・CYCLE＝かけ直しの時刻を伺う質問だけ）。言葉は本書 9-4 の「担当者が不在」の
    # 行のご決定（✅ 2026-10-24「いま電話を続けられない理由（別の番号・別の方へ／それまでは出られない／それまでに確認・準備）を記録し、分からなければ時刻を1回だけ伺い直す」・✅ 2026-10-19
    # 「月ぎめの業務の都合や振り込みなどの入金の間隔などの話の場合は、月単位で考えた方がいい」・伺い63「3時までなら大丈夫です。」の伺い直し）に当てて作り、流れの制御に通して場面に合う
    # ことを確かめた（rv183/Q/probe/n9）。理由ごと・言っていない・境目。言葉と説明の文だけで決まらない3行は、答えを伏せた読み手への束 rv183/Q/readers の n9 で読み手 A・B が読み、2人の読みで CL-1 が決めた（2026-11-21＝3行とも境目）
    # 時刻の2択（LADDER_CLOCK_SCENE）への「〇時まで」
    _row("C1", "3時までなら大丈夫です。", [VALUE_CHOICE], AWAY_NO, scene=LADDER_CLOCK_SCENE, value="3時まで", other=LADDER_OTHER_NOT,
         misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY,
         why=f"区切りの時刻を「まで」で言った＝答えた（{BOUNDARY_WHY}）。片方だけ合わないとは言っていない（答えが「答えた」）。⛔ d7d64450 まで [答えた・4時だけ合わない]"
             f"（{READERS_R3}が2人とも両方を挙げた＝区切りの見分けが説明の文に無かった）。{READERS_U1}",
         until=[UNTIL_WHY_NOT_SAID], until_note="その時刻より前なら話せると言っただけで、それより後が難しいかは言っていない＝理由は言っていない（✅ 2026-11-20 日本時間4時44分42秒「伺い63：ア」＝"
                                                 "この言葉で理由が分からないときに伺い直す）",
         cycle=[CYCLE_NOT_SAID], cycle_note="月ごとにくり返す都合は言っていない（時刻だけ）"),
    _row("C1", "4時まで会議なんです。", [VALUE_CHOICE], AWAY_NO, scene=LADDER_CLOCK_SCENE, value="4時まで", other=LADDER_OTHER_NOT,
         misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY,
         why=f"4時まで会議＝区切りの時刻を「まで」で言った＝答えた（{BOUNDARY_WHY}。2時がその区切りで合わなくなるときも）。片方だけ合わないとは言っていない（答えが「答えた」）。"
             f"⛔ d7d64450 まで [2時だけ合わない]（{READERS_R3}が2人とも挙げた。その「不安そう」の読みでは会議中の3時を伺った＝分身 S の (7-5)）・f2ded9f4 まで [答えた]。{READERS_U1}",
         until=[UNTIL_LABEL["unavailable"]], until_note="その時刻までは予定（会議）で話せないと言った＝それまでは出られない（10-23「3時まで出られないなら、3時以降になる」。答えが片方だけ合わないのときは、"
                                                     "流れの制御は「〇時まで」の理由を受け取らない＝flowctl.reason_items）",
         cycle=[CYCLE_NOT_SAID], cycle_note="月ごとにくり返す都合は言っていない（その日の予定）"),
    _row("C1", "4時までは会議なので、それ以降でお願いします。", [VALUE_CHOICE], AWAY_NO, scene=LADDER_CLOCK_SCENE, value="4時以降", other=LADDER_OTHER_NOT, misgiving=[MISGIVING_OFF],
         misgiving_why=MG_OFF_WHY, why="その時刻より後を頼んだ＝答えた（値は「以降」＝流れの制御は「〇時まで」の理由を読まない・記録にも書かない）。片方だけ合わないとは言っていない",
         until=[UNTIL_LABEL["unavailable"]], until_note="その時刻までは会議で話せないと言った＝それまでは出られない",
         cycle=[CYCLE_NOT_SAID], cycle_note="月ごとにくり返す都合は言っていない（その日の予定）"),
    _row("C1", "3時までに別の番号にかけてもらえますか。", [VALUE_CHOICE], AWAY_NO, scene=LADDER_CLOCK_SCENE, value="3時まで", other=LADDER_OTHER_NOT, misgiving=[MISGIVING_OFF],
         misgiving_why=MG_OFF_WHY, why="時刻を「まで」で言った＝答えた（頼みの形＝こちらへ尋ねた問いではない）。片方だけ合わないとは言っていない",
         until=[UNTIL_LABEL["other_party"]], until_note="その時刻までに別の番号へかけてほしいと言った＝別の番号・別の方へ（10-23「3時までに別の番号にかけてなら、すぐでいい」）",
         cycle=[CYCLE_NOT_SAID], cycle_note="月ごとにくり返す都合は言っていない"),
    _row("C1", "3時までに担当に確認しておきます。", [VALUE_CHOICE], AWAY_NO, scene=LADDER_CLOCK_SCENE, value="3時まで", other=LADDER_OTHER_NOT, misgiving=[MISGIVING_OFF],
         misgiving_why=MG_OFF_WHY, why="時刻を「まで」で言った＝答えた。片方だけ合わないとは言っていない",
         until=[UNTIL_LABEL["preparing"]], until_note="その時刻までに担当の方に確かめておくと言った＝それまでに確認・準備（10-23「3時までに担当者へ聞いて折り返してなら、確認後」）",
         cycle=[CYCLE_NOT_SAID], cycle_note="月ごとにくり返す都合は言っていない"),
    _row("C1", "3時以降は外出するので、それまでにお願いします。", [VALUE_CHOICE], AWAY_NO, scene=LADDER_CLOCK_SCENE, value="3時まで", other=LADDER_OTHER_NOT, misgiving=[MISGIVING_OFF],
         misgiving_why=MG_OFF_WHY, why="その時刻より前を頼んだ＝答えた。片方だけ合わないとは言っていない",
         until=[UNTIL_LABEL["available_until"]], until_note="その時刻より後は外出で難しいと言った＝それまでは出られる（それ以降は難しい）（伺い63 の確かめの「はい」と同じ中身）",
         cycle=[CYCLE_NOT_SAID], cycle_note="月ごとにくり返す都合は言っていない"),
    _row("C1", "3時までには戻ってます。", [VALUE_CHOICE], AWAY_NO, scene=LADDER_CLOCK_SCENE, value="3時まで", other=LADDER_OTHER_NOT,
         misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY,
         why=f"戻る時刻＝区切りの時刻を「まで」で言った＝答えた（{BOUNDARY_WHY}。2時がその区切りで合わなくなるときも）。片方だけ合わないとは言っていない（答えが「答えた」）。"
             f"⛔ d7d64450 まで [答えた・2時だけ合わない・決められない]（束 n9 と{READERS_R3}の4人がそろって3つを挙げた＝区切りの見分けが説明の文に無かった）。{READERS_U1}",
         until=[UNTIL_LABEL["unavailable"]],
         until_note="その時刻までには戻る＝それまでは席にいない＝それまでは出られない（説明の文の1か所 flowctl.until_choices の「それまでは出られない」の「その時刻までには戻る（それまでは"
                    "席にいない）と言ったのも」。✅ 2026-10-23「3時まで出られないなら、3時以降になる」・DESIGN.md の表の例「3時までには戻ります」。答えを伏せた読み手 A・B（束 n9＝rv183/Q/readers）が"
                    "2人ともどちらとも取れると読んだ境目を、CL-1 が決まりで決めて説明の文に書いた＝分身 R の (3)。⛔ 20109859 まで「理由は言っていない」も正解）",
         cycle=[CYCLE_NOT_SAID], cycle_note="月ごとにくり返す都合は言っていない"),
    _row("C1", "3時まではこの番号で大丈夫です。", [VALUE_CHOICE], AWAY_NO, scene=LADDER_CLOCK_SCENE, value="3時まで", other=LADDER_OTHER_NOT,
         misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY,
         why=f"区切りの時刻を「まで」で言った＝答えた（{BOUNDARY_WHY}。4時がその区切りで合わなくなるときも）。片方だけ合わないとは言っていない（答えが「答えた」）。"
             f"⛔ d7d64450 まで [答えた・4時だけ合わない・決められない]（束 n9 と{READERS_R3}の4人がそろって3つを挙げた＝区切りの見分けが説明の文に無かった）。{READERS_U1}",
         until=[UNTIL_LABEL["available_until"], UNTIL_WHY_NOT_SAID],
         until_note="境目：答えを伏せた読み手 A・B（束 n9）が2人とも「それまでは出られる（それ以降は難しい）」「理由は言っていない」のどちらとも取れると読んだ（「は」でそれより後はこの番号では難しいとにおわせた、とも、その時刻より前なら話せると言っただけ、とも取れる）＝2つとも正解（2026-11-21 CL-1）",
         cycle=[CYCLE_NOT_SAID], cycle_note="月ごとにくり返す都合は言っていない"),
    _row("C1", "4時でお願いします。", [VALUE_CHOICE], AWAY_NO, scene=LADDER_CLOCK_SCENE, value="4時", other=LADDER_OTHER_NOT, misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY,
         why="並べた2つの1つ（4時）を選んだ＝答えた。片方だけ合わないとは言っていない",
         until=[UNTIL_WHY_NOT_SAID], until_note="「（時刻）まで」とは言っていない（時刻を選んだだけ）", cycle=[CYCLE_NOT_SAID], cycle_note="月ごとにくり返す都合は言っていない（時刻を選んだだけ）"),
    # 「〇時まで」の理由を伺い直した一言（UNTIL_ASK_SCENE＝「恐れ入ります。3時までということはそれ以降はお電話が難しいということでしょうか？」）へのお返事。答えは、時刻を言い直していない＝
    # 答えた・決められないのどちらでも流れの制御は伺い直す前の値で受け取る（Flow._op_record_answer の until_held）＝両方を正解にした
    _row("C1", "はい、3時から会議なんです。", [VALUE_CHOICE, UNDECIDED], AWAY_NO, scene=UNTIL_ASK_SCENE, value="3時まで", misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY,
         why="こちらの確かめにそうだと答え、そのあとの予定も言った（時刻は言い直していない）＝答えた・決められないのどちらでも同じ受け取り",
         until=[UNTIL_LABEL["available_until"]], until_note="その時刻より後は難しいかの確かめにそうだと答えた＝それまでは出られる（それ以降は難しい）（伺い63＝試験 UntilAskAgainTest の言葉）",
         cycle=[CYCLE_NOT_SAID], cycle_note="月ごとにくり返す都合は言っていない"),
    _row("C1", "はい、そうです。", [VALUE_CHOICE, UNDECIDED], AWAY_NO, scene=UNTIL_ASK_SCENE, value="3時まで", misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY,
         why="こちらの確かめにそうだと答えた（時刻は言い直していない）＝答えた・決められないのどちらでも同じ受け取り",
         until=[UNTIL_LABEL["available_until"]], until_note="その時刻より後は難しいかの確かめにそうだと答えた＝それまでは出られる（それ以降は難しい）",
         cycle=[CYCLE_NOT_SAID], cycle_note="月ごとにくり返す都合は言っていない"),
    _row("C1", "いえ、そういうわけではないです。", [VALUE_CHOICE, UNDECIDED], AWAY_NO, scene=UNTIL_ASK_SCENE, value="3時まで", misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY,
         why="こちらの確かめを打ち消しただけ（時刻は言い直していない）＝答えた・決められないのどちらでも同じ受け取り",
         until=[UNTIL_WHY_NOT_SAID], until_note="こちらの確かめを打ち消しただけで、ほかの理由も言っていない＝理由は言っていない（伺い63 の「いいえ」＝試験 UntilAskAgainTest の言葉）",
         cycle=[CYCLE_NOT_SAID], cycle_note="月ごとにくり返す都合は言っていない"),
    _row("C1", "いえ、3時までに社内で確認しておくので、そのあとでお願いします。", [VALUE_CHOICE, UNDECIDED], AWAY_NO, scene=UNTIL_ASK_SCENE, value="3時まで", misgiving=[MISGIVING_OFF],
         misgiving_why=MG_OFF_WHY, why="こちらの確かめを打ち消し、理由を言った（時刻は言い直していない）＝答えた・決められないのどちらでも同じ受け取り",
         until=[UNTIL_LABEL["preparing"]], until_note="その時刻までに社内で確かめておくと言った＝それまでに確認・準備",
         cycle=[CYCLE_NOT_SAID], cycle_note="月ごとにくり返す都合は言っていない"),
    # 曜日の2択（LADDER_SCENE）への月単位の理由
    _row("C1", "振り込みが毎月25日なので、その後にしてもらえますか。", [VALUE_CHOICE], AWAY_NO, scene=LADDER_SCENE, value="25日以降", other=LADDER_OTHER_NOT, misgiving=[MISGIVING_OFF],
         misgiving_why=MG_OFF_WHY, why="2択の外の日（25日の後）を頼んだ＝答えた（頼みの形＝こちらへ尋ねた問いではない）。片方だけ合わないとは言っていない",
         until=[UNTIL_WHY_NOT_SAID], until_note="「（時刻）まで」とは言っていない",
         cycle=[CYCLE_LABEL["month"]], cycle_note="毎月の振り込みの日＝月ごとにくり返す入金の都合で、その後にしてほしいと言った＝月単位の理由（10-19「振り込みなどの入金の間隔」）"),
    _row("C1", "月末は締めで忙しいので、来月の頭にお願いします。", [VALUE_CHOICE], AWAY_NO, scene=LADDER_SCENE, value="来月の頭", other=LADDER_OTHER_NOT, misgiving=[MISGIVING_OFF],
         misgiving_why=MG_OFF_WHY, why="2択の外の頃（来月の頭）を頼んだ＝答えた。片方だけ合わないとは言っていない",
         until=[UNTIL_WHY_NOT_SAID], until_note="「（時刻）まで」とは言っていない",
         cycle=[CYCLE_LABEL["month"]], cycle_note="月末の締め＝月ごとにくり返す業務の都合で、来月の頭にしてほしいと言った＝月単位の理由（10-19「月ぎめの業務の都合」）"),
    _row("C1", "月初は忙しいので、金曜日でお願いします。", [VALUE_CHOICE], AWAY_NO, scene=LADDER_SCENE, value="金曜日", other=LADDER_OTHER_NOT, misgiving=[MISGIVING_OFF],
         misgiving_why=MG_OFF_WHY, why="並べた2つの1つ（金曜日）を選んだ＝答えた。片方だけ合わないとは言っていない",
         until=[UNTIL_WHY_NOT_SAID], until_note="「（時刻）まで」とは言っていない",
         cycle=[CYCLE_LABEL["month"]], cycle_note="月の初めはいつも忙しい＝月ごとにくり返す業務の都合を、その日にしてほしいわけに言った＝月単位の理由"),
    # 2つとも・片方だけ合わない答えに添えた月単位の理由（分身 S の (2)＝CL-1 の頼み rv184/S の (2)：避けた理由も、残った日を選んだ理由＝説明の文の1か所 flowctl.CYCLE_WHY_OF の
    # 「並べた日・頃を避けたわけ」・受け取る項目は flowctl.reason_items。並べた2つは電話の日 11-19 の「金曜日（20日）と火曜日（24日）」）。⏳ 答えを伏せた読み手2人の読み直し待ち（rv184/S/readers）
    _row("C1", "振り込みが毎月20日なので、金曜日はちょっと…。", [ladder_side_name("金曜日")], AWAY_NO, scene=LADDER_SCENE, value="火曜日", other=LADDER_OTHER_UNSURE,
         misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY,
         why=f"並べた2つの片方（金曜日）だけを避けた＝金曜日だけ合わない（「金曜日はちょっと…。」の行と同じ）。火曜日で大丈夫かは言っていない＝{LADDER_OTHER_UNSURE}",
         until=[UNTIL_WHY_NOT_SAID], until_note="「（時刻）まで」とは言っていない（片方の曜日を避けただけ）",
         cycle=[CYCLE_LABEL["month"]], cycle_note="毎月の振り込みの日（並べた金曜日＝20日）＝月ごとにくり返す入金の都合を、並べた日を避けたわけに言った＝月単位の理由（10-19「振り込みなどの入金の間隔」）"),
    _row("C1", "毎月この時期は締めで忙しいので、どちらもちょっと…。", [LADDER_NEITHER], AWAY_NO, scene=LADDER_SCENE, other=LADDER_OTHER_NOT,
         misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY,
         why="並べた2つのどちらも難しいと言った（わけを添えた）＝どちらも合わない（「どちらもちょっと難しいですね。」の行と同じ）。片方だけ合わないとは言っていない",
         until=[UNTIL_WHY_NOT_SAID], until_note="「（時刻）まで」とは言っていない（2つとも難しいと言っただけ）",
         cycle=[CYCLE_LABEL["month"]], cycle_note="毎月この時期の締め＝月ごとにくり返す業務の都合を、並べた2つを避けたわけに言った＝月単位の理由（10-19「月ぎめの業務の都合」）"),
    _row("C1", "来週は出張なので、再来週にお願いします。", [VALUE_CHOICE], AWAY_NO, scene=LADDER_SCENE, value="再来週", other=LADDER_OTHER_NOT, misgiving=[MISGIVING_OFF],
         misgiving_why=MG_OFF_WHY, why="2択の外の頃（再来週）を頼んだ＝答えた。片方だけ合わないとは言っていない",
         until=[UNTIL_WHY_NOT_SAID], until_note="「（時刻）まで」とは言っていない",
         cycle=[CYCLE_NOT_SAID], cycle_note="来週だけの出張＝月ごとにくり返す都合ではない＝月単位の理由は言っていない"),
    _row("C1", "25日以降でお願いします。", [VALUE_CHOICE], AWAY_NO, scene=LADDER_SCENE, value="25日以降", other=LADDER_OTHER_NOT, misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY,
         why="2択の外の日（25日以降）を頼んだ＝答えた。片方だけ合わないとは言っていない",
         until=[UNTIL_WHY_NOT_SAID], until_note="「（時刻）まで」とは言っていない", cycle=[CYCLE_NOT_SAID], cycle_note="日を言っただけ＝月単位の理由は言っていない"),
    _row("C1", "給料日のあとでお願いします。", [VALUE_CHOICE, UNDECIDED], AWAY_NO, scene=LADDER_SCENE, value="給料日のあと", other=LADDER_OTHER_NOT, misgiving=[MISGIVING_OFF],
         misgiving_why=MG_OFF_WHY, reason=WHY_OTHER,
         why=f"境目：2択の外の頃（給料日のあと）を頼んだ＝答えた、とも、給料日がいつかは言っていない＝決められない、とも取れる（{READERS_R3}が2人とも両方を挙げた・束 n9 は2人とも答えた＝"
             "分身 S の (3)）。片方だけ合わないとは言っていない（4人とも）。どちらの読みでも、流れの制御は読めない頃としていま伺った2択で聞き直す（同じ動き＝伺い99 は rv184/R の (7-4)）",
         until=[UNTIL_WHY_NOT_SAID], until_note="「（時刻）まで」とは言っていない",
         cycle=[CYCLE_LABEL["month"], CYCLE_NOT_SAID, UNDECIDED],
         cycle_note="境目：答えを伏せた読み手 A・B（束 n9）が2人とも「月単位の理由」「月単位の理由は言っていない」のどちらとも取れると読んだ（給料日＝月ごとの入金の日をわけに言った、とも、頃を言っただけ、とも取れる）＝2つとも正解（2026-11-21 CL-1）。束 s2（rv184/S/readers）の読み手 A・B は2人とも「決められない」も挙げた（給料日が月ごとかは言葉から決まらない）＝3つとも正解（2026-11-21 CL-1）"),
    # (e) 番号違いを伺う一言のあと（分身 M の伺い72 エ＝meaning_judge.WRONG_ASK_NOTE。「はい」「そうです」は伺ったとおり＝番号違い）。場面は、支払いの質問（R1）への
    # 「うちはそういう会社じゃないですよ。番号をお間違えだと思います。」を判定の組の1つだけが番号違いと読み、こちらが「大変失礼いたしました。番号を間違えてお掛けして
    # しまいましたでしょうか？」と伺ったあと（目的のお客様のいない業務の最初の質問）。別のお話かは、番号違いの行と同じ（どちらの読みも正解＝WRONG_ANSWER_ROWS の注）
    _row("R1", "はい、そうです。", [UNDECIDED], AWAY_NO, wrong=ABOUT_WRONG, scene=WRONG_ASK_SCENE, reason=WHY_OTHER,
         why=f"番号違いかを伺った一言に「はい、そうです」＝伺ったとおり番号違い（WRONG_ASK_NOTE・2026-11-20 rv176/S の束 n の読み手2人がそろった）。支払いの質問には答えていない。"
             "別のお話かは答えの話（質問のあとにこちらが言った一言へのお返事も答えの話＝✅ 2026-11-20 日本時間17時台 CL-1 が決まりで決めた 9-3 の案の (別のお話か)・11-13 日本時間"
             "20時06分19秒。直す前の値＝[答えの話, 別のお話, 決められない]）", misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY),
    _row("R1", "いえ、合ってますよ。", [UNDECIDED], AWAY_NO, wrong=WRONG_NOT_SAID, scene=WRONG_ASK_SCENE, reason=WHY_OTHER,
         why=f"番号は合っていると言った＝番号違いとは言っていない（2026-11-20 rv176/S の束 n の読み手2人がそろった）。支払いの質問には答えていない。別のお話かは答えの話（伺った一言への"
             "お返事＝CL-1 の 9-3 の案の (別のお話か)。直す前の値＝[答えの話, 別のお話, 決められない]＝読み手2人とも別のお話・決められないも挙げた）", misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY),
    # (e) 待っていただく一言のあと（分身 M の伺い76 ウ＝flowctl.PROMPT_REPLY_WAVER・meaning_judge.WAIT_NOTE）。場面は、支払いの質問（R1）に答えたあと（「振込でお願いします。」）、
    # こちらの声が出ていないので「少々お待ちいただいてもよろしいでしょうか？」を流したところ
    _row("R1", "えー、待つんですか、どうしようかなあ。", [UNDECIDED], AWAY_NO, query=(QUERY_YES, QUERY_NO), prompt=PROMPT_REPLY_WAVER,
         scene=WAIT_SCENE, reason=WHY_OTHER,
         why="待つかどうかの迷いだけ（WAIT_NOTE のとおり・✅ 2026-11-20 日本時間3時55分24秒「伺い76：ウ」の伺いの言葉＝待つことに迷われたとき。読み手の1人はお返事だけではない"
             f"とも読んだ＝ご判断で迷いだけ）。こちらへ尋ねた問いかは、2026-11-20 rv176/S の束 n の読み手2人が2人とも2つを挙げた（「待つんですか」は尋ねたとも、ためらいを言っただけとも）。"
             "別のお話かは答えの話（待っていただく一言へのお返事＝CL-1 の 9-3 の案の (別のお話か)。直す前の値＝[別のお話, 決められない]）", misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY),
    _row("R1", "はい、構いませんよ。", [UNDECIDED], AWAY_NO, prompt=PROMPT_REPLY_YES, scene=WAIT_SCENE, reason=WHY_OTHER,
         why=f"待つお願いへのお返事だけ（待ってよい）＝答えは言い直していない（WAIT_NOTE のとおり・2026-11-20 rv176/S の束 n の読み手2人がそろった）。別のお話かは答えの話（待っていただく"
             "一言へのお返事＝CL-1 の 9-3 の案の (別のお話か)。直す前の値＝[答えの話, 決められない, 別のお話]＝読み手2人が割れた）", misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY),
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


# 何の日時かの一文を見せる日時の2択のうち、行の無かった3つの場面の試しの言葉（分身 U の (3)＝分身 S の書き置きの (6-2)。CL-1 の頼み rv184/U の (3)）：答えた・片方だけ合わない（もう一方で
# 不安そう・大丈夫そう）・2つとも合わない・迷っている・分からない・別のお話を場面ごとに。言葉は本書のご決定の場面（伺い67「平日の4時までには」・伺い62・本書 9-9 の (9)・伺い60・伺い94・
# 【決まりで決めた】(a)(d)）に当てて作り、流れの制御に通して場面に合うことを確かめた（rv184/U/probe/u3）。正解は言葉と説明の文から行ごとに当てた（帳面 0-3 の (2)）。言葉と説明の文だけで
# 決まらない行は、答えを伏せた読み手への束 u1（rv184/U/readers）に入れた（折り返していただく日の2択への「どちらもちょっと難しいですね。」＝読み手 A・B が2人とも「並べた2つのどちらも
# 合わない」という意味と読み、選べるものに無いので「決められない」に落とすしかないと書いた＝分身 V の (2) で選べるものに「どちらも合わない」を足し、その読みで正解を当てた）。項目は、
# その場面の判定が読む項目だけ（折り返していただく日＝月単位の理由・ほかの2つ＝迷っている点を言ったか。「〇時まで」の理由は、こちらからかけ直す時刻の質問だけ＝flowctl.until_fits）
PAIR_ROWS = [
    # お客様から折り返していただく日の2択（「それでは本日と明日ではどちらになりますでしょうか？」。選べるもの＝答えた・どちらも合わない・本日だけ合わない・明日だけ合わない＝Flow._theirs_begin。
    # 「どちらも合わない」は分身 V の (2)＝Flow._ladder_choices）
    _row("C1T", "明日になります。", [VALUE_CHOICE], AWAY_NO, scene=THEIRS_PAIR_SCENE, value="明日", other=LADDER_OTHER_NOT,
         why="並べた2つの1つ（明日）を選んだ＝答えた（試験 TheirsDayPairTest の言葉）。片方だけ合わないとは言っていない",
         cycle=[CYCLE_NOT_SAID], cycle_note="月ごとにくり返す都合は言っていない（日を選んだだけ）"),
    _row("C1T", "本日はちょっと難しいですね。", [ladder_side_name("本日")], AWAY_NO, scene=THEIRS_PAIR_SCENE, value="明日", other=LADDER_OTHER_UNSURE,
         why="本日だけを難しいと言い、明日のことは言っていない＝本日だけ合わない・もう一方は大丈夫とは言っていない（「（避けた方）だけ合わない」の説明の「何も言っていない」）",
         cycle=[CYCLE_NOT_SAID], cycle_note="月ごとにくり返す都合は言っていない（日の都合だけ）"),
    _row("C1T", "本日は無理ですけど、明日なら大丈夫です。", [ladder_side_name("本日")], AWAY_NO, scene=THEIRS_PAIR_SCENE, value="明日", other=LADDER_OTHER_OK,
         why="本日だけを無理と言い、明日なら大丈夫と言っただけ＝本日だけ合わない・もう一方で大丈夫そう（説明の「そちらなら空いている・大丈夫と言っただけ」・伺い94 のエ）",
         cycle=[CYCLE_NOT_SAID], cycle_note="月ごとにくり返す都合は言っていない（日の都合だけ）"),
    _row("C1T", "どちらもちょっと難しいですね。", [LADDER_NEITHER], AWAY_NO, scene=THEIRS_PAIR_SCENE, other=LADDER_OTHER_NOT,
         why="並べた2つのどちらも難しいとだけ言った＝どちらも合わない（伺い60「2.別の空スケジュールの2つ」の場面・日時の2択の段の同じ言葉の行と同じ。答えを伏せた読み手 A・B が2人とも"
             "この意味と読んだ＝束 u1 の #8。⛔ 分身 V の (2) の前は選べるものに無く「決められない」）",
         cycle=[CYCLE_NOT_SAID], cycle_note="月ごとにくり返す都合は言っていない（2つとも難しいとだけ言った）"),
    _row("C1T", "ちょっと分からないですね。", ["unknown"], AWAY_NO, scene=THEIRS_PAIR_SCENE, other=LADDER_OTHER_NOT, reason=(DONT_KNOW_LABEL["unspecified"],),
         why="分からないとだけ言った＝わからない（試験 TheirsDayPairTest の言葉＝伺えた所のまま言い直す）。片方だけ合わないとは言っていない",
         cycle=[CYCLE_NOT_SAID], cycle_note="月ごとにくり返す都合は言っていない"),
    _row("C1T", "いやあ最近は暑くてねえ。", [UNDECIDED], AWAY_YES, scene=THEIRS_PAIR_SCENE, other=LADDER_OTHER_NOT, reason=WHY_OTHER,
         why="伺った日と関わりの無い世間話＝別のお話・答えは決められない（【決まりで決めた】(d)＝ひとこと応えていまの2択へ戻る）",
         cycle=[CYCLE_NOT_SAID], cycle_note="月ごとにくり返す都合は言っていない（世間話）"),
    # 依頼者の日時の質問の2つ目の2択（「金曜日と火曜日ではどちらがよろしいでしょうか？」＝ご来店の日時。選べるもの＝答えた・どちらも合わない・金曜日だけ合わない・火曜日だけ合わない）
    _row("R14", "火曜日でお願いします。", [VALUE_CHOICE], AWAY_NO, scene=ACCEPT_PAIR_SCENE, value="火曜日", other=LADDER_OTHER_NOT, misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY,
         why="並べた2つの1つ（火曜日）を選んだ＝答えた。片方だけ合わないとは言っていない"),
    _row("R14", "金曜日はちょっと…。", [ladder_side_name("金曜日")], AWAY_NO, scene=ACCEPT_PAIR_SCENE, value="火曜日", other=LADDER_OTHER_UNSURE, misgiving=[MISGIVING_OFF],
         misgiving_why=MG_OFF_WHY, why="金曜日だけを避け、火曜日のことは言っていない＝金曜日だけ合わない・もう一方は大丈夫とは言っていない（日時の2択の段の同じ言葉の行と同じ）"),
    _row("R14", "金曜日はちょっと…火曜日なら。", [ladder_side_name("金曜日")], AWAY_NO, scene=ACCEPT_PAIR_SCENE, value="火曜日", other=LADDER_OTHER_OK, misgiving=[MISGIVING_OFF],
         misgiving_why=MG_OFF_WHY, why="金曜日だけを避け、火曜日なら、と挙げただけ＝金曜日だけ合わない・もう一方で大丈夫そう（日時の2択の段の同じ言葉の行と同じ）"),
    _row("R14", "どちらもちょっと難しいですね。", [LADDER_NEITHER], AWAY_NO, scene=ACCEPT_PAIR_SCENE, other=LADDER_OTHER_NOT, misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY,
         why="並べた2つのどちらも難しいとだけ言った＝どちらも合わない（伺い60「2.別の空スケジュールの2つ」の場面。日時の2択の段の同じ言葉の行と同じ）"),
    _row("R14", "うーん、どっちがいいかなあ。", [HESITATING, THINKING], AWAY_NO, scene=ACCEPT_PAIR_SCENE, other=LADDER_OTHER_NOT,
         misgiving=[MISGIVING_UNSAID, MISGIVING_OFF], misgiving_why=MG_UNSAID_WHY,
         why="2つのあいだで迷っていると言った＝迷っている・言いよどみで、このあと続けて答えそう＝考えている途中、とも読める（日時の2択の段の同じ言葉の行と同じ境目）"),
    _row("R14", "いやあ最近は暑くてねえ。", [UNDECIDED], AWAY_YES, scene=ACCEPT_PAIR_SCENE, other=LADDER_OTHER_NOT, reason=WHY_OTHER, misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY,
         why="伺った日と関わりの無い世間話＝別のお話・答えは決められない（【決まりで決めた】(d)）"),
    # 顧客獲得型のアポの日時のはじめの2択（「それでは平日と土日のどちらがよろしいでしょうか？」。選べるもの＝答えた・どちらも合わない・平日だけ合わない・土日だけ合わない）
    _row("R15", "平日でお願いします。", [VALUE_CHOICE], AWAY_NO, scene=MEETING_PAIR_SCENE, value="平日", other=LADDER_OTHER_NOT, misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY,
         why="並べた2つの1つ（平日）を選んだ＝答えた。片方だけ合わないとは言っていない"),
    _row("R15", "土日はちょっと…。", [ladder_side_name("土日")], AWAY_NO, scene=MEETING_PAIR_SCENE, value="平日", other=LADDER_OTHER_UNSURE, misgiving=[MISGIVING_OFF],
         misgiving_why=MG_OFF_WHY, why="土日だけを避け、平日のことは言っていない＝土日だけ合わない・もう一方は大丈夫とは言っていない"),
    _row("R15", "土日は無理ですけど、平日なら。", [ladder_side_name("土日")], AWAY_NO, scene=MEETING_PAIR_SCENE, value="平日", other=LADDER_OTHER_OK, misgiving=[MISGIVING_OFF],
         misgiving_why=MG_OFF_WHY, why="土日だけを無理と言い、平日なら、と挙げただけ＝土日だけ合わない・もう一方で大丈夫そう（説明の「そちらなら、と挙げただけ」・伺い94 のエ）"),
    _row("R15", "どちらもちょっと難しいですね。", [LADDER_NEITHER], AWAY_NO, scene=MEETING_PAIR_SCENE, other=LADDER_OTHER_NOT, misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY,
         why="並べた2つのどちらも難しいとだけ言った＝どちらも合わない（伺い60「2.別の空スケジュールの2つ」の場面）"),
    _row("R15", "うーん、どっちがいいかなあ。", [HESITATING, THINKING], AWAY_NO, scene=MEETING_PAIR_SCENE, other=LADDER_OTHER_NOT,
         misgiving=[MISGIVING_UNSAID, MISGIVING_OFF], misgiving_why=MG_UNSAID_WHY,
         why="2つのあいだで迷っていると言った＝迷っている・言いよどみで、このあと続けて答えそう＝考えている途中、とも読める（日時の2択の段の同じ言葉の行と同じ境目）"),
    _row("R15", "いやあ最近は暑くてねえ。", [UNDECIDED], AWAY_YES, scene=MEETING_PAIR_SCENE, other=LADDER_OTHER_NOT, reason=WHY_OTHER, misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY,
         why="伺った日と関わりの無い世間話＝別のお話・答えは決められない（【決まりで決めた】(d)）"),
]

# 並べ直した2回目の2択の試しの言葉（分身 W の (5)＝分身 V の書き置き rv184/V/notes.md の (6-5)。測りの前に正解の表まで整える＝帳面 0-3・分身 U の (3) と同じ形）：はじめの2択の行（PAIR_ROWS の
# C1T・NEW_SCENE_ROWS の時刻の2択）と同じ種類の言葉（答えた・片方だけ合わない（もう一方で不安そう・大丈夫そう）・2つとも合わない・迷っている／分からない・別のお話）を、並べ直した2つの日・
# 時刻に替えて場面ごとに。言葉は流れの制御に通して場面に合うことを確かめた（rv184/W/probe/w5）。正解は言葉と説明の文から行ごとに当てた（帳面 0-3 の (2)。はじめの2択の同じ種類の行の正解と
# 同じ理由）。判定が見る文の変わった行（足した行の全部）は、答えを伏せた読み手への束 w1（rv184/W/readers）に入れた（読み手を立てるのは CL-1）。項目は、その場面の判定が読む項目だけ
# （折り返していただく日＝月単位の理由・こちらからかけ直す時刻＝迷っている点を言ったか・「〇時まで」の理由・月単位の理由）
AGAIN_ROWS = [
    # 折り返していただく日の2択で「本日はちょっと難しいですね。」（本日だけ合わない・もう一方は不安そう）のあとの「それでは明日と火曜日ではどちらになりますでしょうか？」（分身 V の (1)）
    _row("C1T", "火曜日になります。", [VALUE_CHOICE], AWAY_NO, scene=THEIRS_SIDE_AGAIN_SCENE, value="火曜日", other=LADDER_OTHER_NOT,
         why="並べ直した2つの1つ（火曜日）を選んだ＝答えた（はじめの2択の「明日になります。」の行と同じ）。片方だけ合わないとは言っていない",
         cycle=[CYCLE_NOT_SAID], cycle_note="月ごとにくり返す都合は言っていない（日を選んだだけ）"),
    _row("C1T", "明日もちょっと難しいですね。", [ladder_side_name("明日")], AWAY_NO, scene=THEIRS_SIDE_AGAIN_SCENE, value="火曜日", other=LADDER_OTHER_UNSURE,
         why="並べ直した2つのうち明日だけを難しいと言い（「も」は前の本日に重ねただけ）、火曜日のことは言っていない＝明日だけ合わない・もう一方は大丈夫とは言っていない"
             "（はじめの2択の「本日はちょっと難しいですね。」の行と同じ）",
         cycle=[CYCLE_NOT_SAID], cycle_note="月ごとにくり返す都合は言っていない（日の都合だけ）"),
    _row("C1T", "明日は無理ですけど、火曜日なら大丈夫です。", [ladder_side_name("明日")], AWAY_NO, scene=THEIRS_SIDE_AGAIN_SCENE, value="火曜日", other=LADDER_OTHER_OK,
         why="明日だけを無理と言い、火曜日なら大丈夫と言っただけ＝明日だけ合わない・もう一方で大丈夫そう（伺い94 のエ・はじめの2択の「本日は無理ですけど、明日なら大丈夫です。」の行と同じ）",
         cycle=[CYCLE_NOT_SAID], cycle_note="月ごとにくり返す都合は言っていない（日の都合だけ）"),
    _row("C1T", "どちらもちょっと難しいですね。", [LADDER_NEITHER], AWAY_NO, scene=THEIRS_SIDE_AGAIN_SCENE, other=LADDER_OTHER_NOT,
         why="並べ直した2つのどちらも難しいとだけ言った＝どちらも合わない（はじめの2択の同じ言葉の行と同じ）",
         cycle=[CYCLE_NOT_SAID], cycle_note="月ごとにくり返す都合は言っていない（2つとも難しいとだけ言った）"),
    _row("C1T", "ちょっと分からないですね。", ["unknown"], AWAY_NO, scene=THEIRS_SIDE_AGAIN_SCENE, other=LADDER_OTHER_NOT, reason=(DONT_KNOW_LABEL["unspecified"],),
         why="分からないとだけ言った＝わからない（はじめの2択の同じ言葉の行と同じ）。片方だけ合わないとは言っていない",
         cycle=[CYCLE_NOT_SAID], cycle_note="月ごとにくり返す都合は言っていない"),
    _row("C1T", "いやあ最近は暑くてねえ。", [UNDECIDED], AWAY_YES, scene=THEIRS_SIDE_AGAIN_SCENE, other=LADDER_OTHER_NOT, reason=WHY_OTHER,
         why="伺った日と関わりの無い世間話＝別のお話・答えは決められない（【決まりで決めた】(d)・はじめの2択の同じ言葉の行と同じ）",
         cycle=[CYCLE_NOT_SAID], cycle_note="月ごとにくり返す都合は言っていない（世間話）"),
    # 折り返していただく日の2択で「どちらもちょっと難しいですね。」（どちらも合わない）のあとの「それでは火曜日と水曜日ではどちらになりますでしょうか？」（分身 V の (2)）
    _row("C1T", "水曜日になります。", [VALUE_CHOICE], AWAY_NO, scene=THEIRS_NEITHER_AGAIN_SCENE, value="水曜日", other=LADDER_OTHER_NOT,
         why="並べ直した2つの1つ（水曜日）を選んだ＝答えた（はじめの2択の「明日になります。」の行と同じ）。片方だけ合わないとは言っていない",
         cycle=[CYCLE_NOT_SAID], cycle_note="月ごとにくり返す都合は言っていない（日を選んだだけ）"),
    _row("C1T", "火曜日はちょっと難しいですね。", [ladder_side_name("火曜日")], AWAY_NO, scene=THEIRS_NEITHER_AGAIN_SCENE, value="水曜日", other=LADDER_OTHER_UNSURE,
         why="並べ直した2つのうち火曜日だけを難しいと言い、水曜日のことは言っていない＝火曜日だけ合わない・もう一方は大丈夫とは言っていない（はじめの2択の「本日はちょっと難しいですね。」の行と同じ）",
         cycle=[CYCLE_NOT_SAID], cycle_note="月ごとにくり返す都合は言っていない（日の都合だけ）"),
    _row("C1T", "火曜日は無理ですけど、水曜日なら大丈夫です。", [ladder_side_name("火曜日")], AWAY_NO, scene=THEIRS_NEITHER_AGAIN_SCENE, value="水曜日", other=LADDER_OTHER_OK,
         why="火曜日だけを無理と言い、水曜日なら大丈夫と言っただけ＝火曜日だけ合わない・もう一方で大丈夫そう（伺い94 のエ・はじめの2択の「本日は無理ですけど、明日なら大丈夫です。」の行と同じ）",
         cycle=[CYCLE_NOT_SAID], cycle_note="月ごとにくり返す都合は言っていない（日の都合だけ）"),
    _row("C1T", "どちらもちょっと難しいですね。", [LADDER_NEITHER], AWAY_NO, scene=THEIRS_NEITHER_AGAIN_SCENE, other=LADDER_OTHER_NOT,
         why="並べ直した2つのどちらも難しいとだけ言った＝どちらも合わない（はじめの2択の同じ言葉の行と同じ）",
         cycle=[CYCLE_NOT_SAID], cycle_note="月ごとにくり返す都合は言っていない（2つとも難しいとだけ言った）"),
    _row("C1T", "ちょっと分からないですね。", ["unknown"], AWAY_NO, scene=THEIRS_NEITHER_AGAIN_SCENE, other=LADDER_OTHER_NOT, reason=(DONT_KNOW_LABEL["unspecified"],),
         why="分からないとだけ言った＝わからない（はじめの2択の同じ言葉の行と同じ）。片方だけ合わないとは言っていない",
         cycle=[CYCLE_NOT_SAID], cycle_note="月ごとにくり返す都合は言っていない"),
    _row("C1T", "いやあ最近は暑くてねえ。", [UNDECIDED], AWAY_YES, scene=THEIRS_NEITHER_AGAIN_SCENE, other=LADDER_OTHER_NOT, reason=WHY_OTHER,
         why="伺った日と関わりの無い世間話＝別のお話・答えは決められない（【決まりで決めた】(d)・はじめの2択の同じ言葉の行と同じ）",
         cycle=[CYCLE_NOT_SAID], cycle_note="月ごとにくり返す都合は言っていない（世間話）"),
    # 日時の2択の言い直しに日だけ直され、水曜日の4時は空いている予定に無いときの「失礼いたしました。それでは水曜日でしたら2時と3時ではどちらがよろしいでしょうか？」（分身 W の (1)）
    _row("C1", "3時でお願いします。", [VALUE_CHOICE], AWAY_NO, scene=LADDER_REDO_SCENE, value="3時", other=LADDER_OTHER_NOT, misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY,
         why="並べた2つの1つ（3時）を選んだ＝答えた（時刻の2択の「4時でお願いします。」の行と同じ）。片方だけ合わないとは言っていない",
         until=[UNTIL_WHY_NOT_SAID], until_note="「（時刻）まで」とは言っていない（時刻を選んだだけ）", cycle=[CYCLE_NOT_SAID], cycle_note="月ごとにくり返す都合は言っていない（時刻を選んだだけ）"),
    _row("C1", "2時はちょっと…。", [ladder_side_name("2時")], AWAY_NO, scene=LADDER_REDO_SCENE, value="3時", other=LADDER_OTHER_UNSURE, misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY,
         why="2時だけを避け、3時のことは言っていない＝2時だけ合わない・もう一方は大丈夫とは言っていない（曜日の2択の「金曜日はちょっと…。」の行と同じ）",
         until=[UNTIL_WHY_NOT_SAID], until_note="「（時刻）まで」とは言っていない（片方の時刻を避けただけ）", cycle=[CYCLE_NOT_SAID], cycle_note="月ごとにくり返す都合は言っていない（時刻の都合だけ）"),
    _row("C1", "2時はちょっと…。3時なら空いてるかな。", [ladder_side_name("2時")], AWAY_NO, scene=LADDER_REDO_SCENE, value="3時", other=LADDER_OTHER_OK, misgiving=[MISGIVING_OFF],
         misgiving_why=MG_OFF_WHY,
         why="2時だけを避け、3時なら空いていそうと言った（「かな」＝はっきり選んではいない）＝2時だけ合わない・もう一方で大丈夫そう（伺い94 のエ・時刻の2択の「2時はちょっと…。4時なら"
             "空いてるかな。」の行と同じ）",
         until=[UNTIL_WHY_NOT_SAID], until_note="「（時刻）まで」とは言っていない（時刻を避け、もう一方なら空いていそうと言っただけ（「なら」は時刻の手前までの意味ではない））",
         cycle=[CYCLE_NOT_SAID], cycle_note="月ごとにくり返す都合は言っていない（時刻の都合だけ）"),
    _row("C1", "どちらもちょっと難しいですね。", [LADDER_NEITHER], AWAY_NO, scene=LADDER_REDO_SCENE, other=LADDER_OTHER_NOT, misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY,
         why="並べた2つのどちらも難しいとだけ言った＝どちらも合わない（曜日の2択の同じ言葉の行と同じ）。片方だけ合わないとは言っていない",
         until=[UNTIL_WHY_NOT_SAID], until_note="「（時刻）まで」とは言っていない（2つとも難しいとだけ言った）", cycle=[CYCLE_NOT_SAID], cycle_note="月ごとにくり返す都合は言っていない（2つとも難しいとだけ言った）"),
    _row("C1", "うーん、どっちがいいかなあ。", [HESITATING, THINKING], AWAY_NO, scene=LADDER_REDO_SCENE, other=LADDER_OTHER_NOT,
         why="2つのあいだで迷っていると言った＝迷っている。言いよどみで、このあと続けて答えそう＝考えている途中、とも読める（曜日の2択の同じ言葉の行と同じ境目）。片方だけ合わないとは言っていない",
         misgiving=[MISGIVING_UNSAID, MISGIVING_OFF], misgiving_why=MG_UNSAID_WHY, until=[UNTIL_WHY_NOT_SAID], until_note="「（時刻）まで」とは言っていない（迷っているとだけ言った）",
         cycle=[CYCLE_NOT_SAID], cycle_note="月ごとにくり返す都合は言っていない（迷っているとだけ言った）"),
    _row("C1", "いやあ最近は暑くてねえ。", [UNDECIDED], AWAY_YES, scene=LADDER_REDO_SCENE, other=LADDER_OTHER_NOT, reason=WHY_OTHER, misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY,
         why="伺った時刻と関わりの無い世間話＝別のお話・答えは決められない（【決まりで決めた】(d)）",
         until=[UNTIL_WHY_NOT_SAID], until_note="「（時刻）まで」とは言っていない（世間話）", cycle=[CYCLE_NOT_SAID], cycle_note="月ごとにくり返す都合は言っていない（世間話）"),
]


# 日時の2択の途中で伺い直した一言へのお返事の試しの言葉（分身 AB。測りの前に正解の表まで整える＝帳面 0-3・分身 U の (3)・分身 W の (5) と同じ形）：(1)「〇時まで」の理由の読みが割れ、具体的な
# 理由の向きで伺い直した一言（伺い98 ア）へのお返事＝はじめの伺い直し（UNTIL_ASK_SCENE）の行と同じ種類（はい・はいと向き・いいえ・いいえと理由）。言葉は流れの制御に通して場面に合うことを
# 確かめた（rv186/AB/probe）。正解は言葉と説明の文から行ごとに当てた（帳面 0-3 の (2)。はじめの伺い直しの同じ種類の行の正解と同じ理由）。判定が見る文の変わった行（足した行の全部）は、
# 答えを伏せた読み手への束（rv186/AB/readers）に入れた（読み手を立てるのは CL-1）
ASK_ROWS = [
    # 「恐れ入ります。3時まではお電話が難しいということでしょうか？」へのお返事。答えは、時刻を言い直していない＝答えた・決められないのどちらでも流れの制御は伺い直す前の値で受け取る
    # （Flow._op_record_answer の until_held＝UNTIL_ASK_SCENE の行と同じ）＝両方を正解にした
    _row("C1", "はい、そうです。", [VALUE_CHOICE, UNDECIDED], AWAY_NO, scene=UNTIL_LEAN_SCENE, value="3時まで", misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY,
         why="こちらの確かめにそうだと答えた（時刻は言い直していない）＝答えた・決められないのどちらでも同じ受け取り",
         until=[UNTIL_LABEL["unavailable"]], until_note="その時刻までは電話が難しいかの確かめにそうだと答えた＝それまでは出られない（伺い98 ア。境目：説明の「こちらがそう確かめた問いに、"
                                                         "そうだと答えた」は「それまでは出られる（それ以降は難しい）」の説明にだけある＝読み手に見ていただく）",
         cycle=[CYCLE_NOT_SAID], cycle_note="月ごとにくり返す都合は言っていない"),
    _row("C1", "はい、3時以降なら大丈夫です。", [VALUE_CHOICE, UNDECIDED], AWAY_NO, scene=UNTIL_LEAN_SCENE, value="3時まで", misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY,
         why="こちらの確かめにそうだと答え、その時刻より後なら話せると言った（時刻は言い直していない）＝答えた・決められないのどちらでも同じ受け取り",
         until=[UNTIL_LABEL["unavailable"]], until_note="その時刻までは電話が難しいかの確かめにそうだと答え、その時刻より後なら大丈夫と言った＝それまでは出られない（伺い98 アの伺いの文の「はい」の行）",
         cycle=[CYCLE_NOT_SAID], cycle_note="月ごとにくり返す都合は言っていない"),
    _row("C1", "いえ、そういうわけではないです。", [VALUE_CHOICE, UNDECIDED], AWAY_NO, scene=UNTIL_LEAN_SCENE, value="3時まで", misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY,
         why="こちらの確かめを打ち消しただけ（時刻は言い直していない）＝答えた・決められないのどちらでも同じ受け取り",
         until=[UNTIL_WHY_NOT_SAID], until_note="こちらの確かめを打ち消しただけで、ほかの理由も言っていない＝理由は言っていない（伺い98 アの伺いの文の「いいえ」の行）",
         cycle=[CYCLE_NOT_SAID], cycle_note="月ごとにくり返す都合は言っていない"),
    _row("C1", "いえ、3時までに社内で確認しておくので、そのあとでお願いします。", [VALUE_CHOICE, UNDECIDED], AWAY_NO, scene=UNTIL_LEAN_SCENE, value="3時まで", misgiving=[MISGIVING_OFF],
         misgiving_why=MG_OFF_WHY, why="こちらの確かめを打ち消し、理由を言った（時刻は言い直していない）＝答えた・決められないのどちらでも同じ受け取り",
         until=[UNTIL_LABEL["preparing"]], until_note="その時刻までに社内で確かめておくと言った＝それまでに確認・準備",
         cycle=[CYCLE_NOT_SAID], cycle_note="月ごとにくり返す都合は言っていない"),
]

# 測りの前に0円で済ませる残り（帳面 1 の (1) の ⏳ と本書 9-4 の ⏳ に、言葉と正解の候補が書いてある試しの言葉。分身 S3＝まとめ役 CL-1 の頼み rv178/S3 の (2)）。言葉と正解は ⏳ に
# 書いてあるとおり（書いていない項目は、説明の文と言葉だけから決めた）。場面は測りの数えの1か所（g53_meaning._flow_scene）が本番の流れの制御で組む。⏳ どの行も、答えを伏せた
# 読み手2人の束（rv178/S3/readers）で読み直してから数える（帳面 0-3「試しの正解は…別の目で読み直してから数える」）
_WAIT_TRUTH = "（帳面 1 の (1) の 11-19 日本時間21時06分08秒の段の (c)＝分身 AF の注意書き WAIT_NOTE の試しの言葉の候補）"
_MOSHI_TRUTH = "（本書 9-4 の ✅ 2026-11-19 日本時間15時10分27秒「伺い49：エ」の段の ⏳「「もしもし？」を流したあとの発話…試しの言葉（「はい」「もしもし」「聞こえてます」「あ、すみません、カードで」）を入れる候補」）"
# 決め手を伺う一言 E5 へのお返事のほかの会社の項目の境目（言っていない・ご案内全体。分身 S4＝束 t の #4・#5・#7。わけは PREP3_ROWS の (8)）
_E5_BORDER = (ELSEWHERE_NO, ELSEWHERE_ALL)
PREP3_ROWS = [
    # (1) 待っていただく一言のあと（WAIT_SCENE＝支払いの質問に「振込でお願いします。」と答えたあと、こちらの声が出ていないので「少々お待ちいただいてもよろしいでしょうか？」を流した所）
    _row("R1", "はい、大丈夫ですよ。", [UNDECIDED], AWAY_NO, prompt=PROMPT_REPLY_YES, scene=WAIT_SCENE, reason=WHY_OTHER,
         why=f"待っていただく一言へのお返事だけ＝候補のとおり{_WAIT_TRUTH}。支払いの質問の答えは言い直していない。別のお話かは答えの話（質問のあとにこちらが言った一言へのお返事も"
             "答えの話＝✅ 2026-11-20 日本時間17時台 CL-1 が決まりで決めた 9-3 の案の (別のお話か)）", misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY),
    _row("R1", "どうぞ。", [UNDECIDED], AWAY_NO, prompt=PROMPT_REPLY_YES, scene=WAIT_SCENE, reason=WHY_OTHER,
         why=f"待っていただく一言へのお返事だけ＝候補のとおり{_WAIT_TRUTH}。別のお話かは同じ（答えの話）", misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY),
    _row("R1", "はい、大丈夫です。あ、それとお支払いはカードで。", ["カード"], AWAY_NO, prompt=PROMPT_REPLY_NO, scene=WAIT_SCENE, value="カード",
         why=f"お返事だけではない＝候補のとおり{_WAIT_TRUTH}。待つことに応えたあと、支払いの答えを「カード」と言った（答え＝カード）", misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY),
    {**_row("R1", "すみません、ちょっと聞こえなかったんですけど。", [REPEAT], (AWAY_NO, UNDECIDED), prompt=PROMPT_REPLY_NO, scene=WAIT_SCENE,
         why="待っていただく一言が聞こえなかったと言った＝もう一度言ってほしい（帳面 1 の (1) の 11-19 日本時間23時台の段＝伺い76 の試しの言葉そのまま・説明「直前にこちらが言ったこと"
             "（…お待ちいただくようにお願いしたことなど）が聞こえなかった…と言ったのも」）。声かけへのお返事だけではない（説明の「お返事だけ＝待ってよい・分かった、と言っただけ」に"
             "当たらない＝聞こえなかったと言った）。束 s の #4：別のお話かは読み手2人とも「答えの話」「決められない」の両方＝境目にした・こちらの声が届いていない様子かは2人とも"
             "「届いていない様子」（聞こえなかったと言った＝10-30 の管理者のご決定の材料）＝替えた（直す前の値＝答えの話／そういう様子は無い）。声かけへのお返事だけかは読み手の1人が"
             "決められないも挙げた＝説明の文で決めたまま", misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY), "hear_truth": HEAR_LOST},
    # (2) 待つかかけ直すかの伺い H2（支払いの質問に「上司に相談してみるので、少し待っていてください。」と言われて伺った所＝g53_meaning._wait_ask_scene）
    _row("H2", "そういえば、うちは月曜が定休日なんですよ。", [UNDECIDED], AWAY_YES, reason=WHY_OTHER,
         why="H2 と関わりのないご事情＝別のお話（帳面 1 の (1) の 11-19 日本時間21時06分08秒の段の (b)＝分身 AG の flowctl.AWAY_ROLES の試しの言葉そのまま）"),
    _row("H2", "うーん、どうしようかな。", ["unknown", UNDECIDED, THINKING], AWAY_NO, reason=WHY_HESITATING,
         why="H2 への迷い＝答えの話（決められない）（同じ (b) の試しの言葉と正解の候補そのまま）。⏳ 「考えている途中」（「はい。」で待つ）との境目は、答えを伏せた読み手2人で"
             "確かめる（前は正解を付けずに TO_READERS＝見直し (13) の D8。11-07 日本時間18時台に業務の質問の「うーん、どうしようかな」へ考えている途中を足したのは H2 の場面ではない）。"
             "束 s の #6：読み手2人とも「わからない」「決められない」「考えている途中」の3つ・理由は「まだ決まっていない」も＝そろった読みにした（直す前の値＝[決められない]）"),
    # (3) 伺ったあと黙られて「もしもし？」を流したあと（場面＝g53_meaning._played_flow の支払いの質問のあと _quiet。正解に「声かけへのお返事だけか」がある行＝prompted_row）
    _row("R1", "はい", [UNDECIDED], AWAY_NO, prompt=PROMPT_REPLY_YES, reason=WHY_OTHER,
         why=f"「もしもし？」へのお返事だけ（電話口にいる）。支払いの質問には答えていない＝決められない{_MOSHI_TRUTH}", misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY),
    _row("R1", "もしもし", [UNDECIDED], AWAY_NO, prompt=PROMPT_REPLY_YES, reason=WHY_OTHER,
         why=f"「もしもし？」に「もしもし」と返しただけ＝お返事だけ。支払いの質問には答えていない＝決められない{_MOSHI_TRUTH}。こちらの声が届いていない様子かは、1回だけの"
             "「もしもし」では、聞こえない・くり返した、とは言っていない＝そういう様子は無い（読み手で確かめる）", misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY),
    _row("R1", "聞こえてます", [UNDECIDED], AWAY_NO, prompt=PROMPT_REPLY_YES, reason=WHY_OTHER,
         why=f"「もしもし？」へ、聞こえていると返しただけ＝お返事だけ。支払いの質問には答えていない＝決められない{_MOSHI_TRUTH}", misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY),
    _row("R1", "あ、すみません、カードで", ["カード"], AWAY_NO, prompt=PROMPT_REPLY_NO,
         why=f"「もしもし？」に応えて支払いの質問に答えた（カード）＝お返事だけではない{_MOSHI_TRUTH}", misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY),
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
             "申し出は試しの表のパンフレットの申し出＝同じ形の最終の確かめ）", misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY),
    _row("R9", "うーん、どうしようかな。", [THINKING, HESITATING], AWAY_NO, scene=OFFER_FINAL_SCENE, reason=WHY_HESITATING,
         why="最終の確かめへの迷い＝答えの話（帳面の段の候補は「決められない」）。束 s の #15：読み手2人とも「考えている途中」「迷っている」の2つ＝言い終えた迷い（迷っている）と"
             "言いよどみ（考えている途中）の境目（✅ 2026-11-07 日本時間18時03分49秒「(1) ア、(2) イ」＝言い終えた「迷っています。」と言い終えていない「うーん、どうしようかな…。」）"
             "＝お答えで決まる2つを正解にした（直す前の値＝[決められない]）", misgiving=[MISGIVING_UNSAID, MISGIVING_OFF], misgiving_why=MG_UNSAID_WHY),
    # (6) 受けていただいた申し出から始まった質問の途中の取り消し（帳面 1 の (1) の伺い40 の段の試しの言葉と正解そのまま。取り消しの言葉は、電話を終える言葉でも答えたくないでもない＝
    #     電話を終える言葉かは「言っていない」・答えは「申し出を取り消した」。時刻の伺いは日時の2択の段＝もう一方で大丈夫そうかは、片方だけ合わないの読みでない行＝言っていない）
    _row("C1", "あ、やっぱりかけ直していただかなくて結構です。", [WITHDREW], AWAY_NO, scene=WITHDRAW_TIME_SCENE, other=LADDER_OTHER_NOT,
         why="受けていただいたかけ直しの申し出を取り消した（伺い40 の段の時刻の質問への試しの言葉そのまま）", misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY, cycle=[CYCLE_NOT_SAID], cycle_note="月ごとにくり返す都合は言っていない（かけ直しの申し出を取り消しただけ）"),
    _row("C1", "やっぱりいいです。", [WITHDREW], AWAY_NO, scene=WITHDRAW_TIME_SCENE, other=LADDER_OTHER_NOT, end=(END_NONE, UNDECIDED),
         why="受けていただいたかけ直しの申し出を取り消した（同じ）。束 s の #17：電話を終える言葉かは読み手2人とも「言っていない」「決められない」（何をやめるかを言わない短い言葉＝"
             "決められないの説明「断りらしい言葉でも…言葉から決まらないときも」と、言っていないの説明「前の質問でこちらが申し出たこと…を、断った」の2つの決まりが当たる）＝境目にした", misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY, cycle=[CYCLE_NOT_SAID], cycle_note="月ごとにくり返す都合は言っていない（申し出を取り消しただけ）"),
    _row("C1", "すみません、やっぱりやめておきます。", [WITHDREW], AWAY_NO, scene=WITHDRAW_TIME_SCENE, other=LADDER_OTHER_NOT, end=(END_NONE, UNDECIDED),
         why="受けていただいたかけ直しの申し出を取り消した（同じ）。束 s の #18：#17 と同じ境目", misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY, cycle=[CYCLE_NOT_SAID], cycle_note="月ごとにくり返す都合は言っていない（申し出を取り消しただけ）"),
    _row("C1", "時間はちょっと分からないです。", ["unknown"], AWAY_NO, scene=WITHDRAW_TIME_SCENE, other=LADDER_OTHER_NOT, reason=(DONT_KNOW_LABEL["unspecified"],),
         why="時刻が分からないとだけ言った＝わからない（取り消しではない＝同じ段の試しの言葉と正解そのまま）", misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY, cycle=[CYCLE_NOT_SAID], cycle_note="月ごとにくり返す都合は言っていない（時間が分からないとだけ言った）"),
    _row("R10", "あ、やっぱりパンフレットはいいです。", [WITHDREW], AWAY_NO, scene=WITHDRAW_SEND_SCENE,
         why="受けていただいたパンフレットの申し出を取り消した（伺い40 の段の送付先の質問への試しの言葉そのまま）", misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY),
    # ── 分身 S3 の続き（CL-1 の頼み rv178/S3 の続き (2)＝棚卸しで「まだ0円」と残した行）
    # (7) 受けていただいた申し出のあとの方法の伺い N5（WITHDRAW_WAY_SCENE）：伺い40 の段の方法の質問への試しの言葉そのまま
    _row("N5", "あ、やっぱり詳しい方からのお話はいいです。", [WITHDREW], scene=WITHDRAW_WAY_SCENE,
         why="受けていただいた詳しい方からのお話の申し出（N4）を取り消した（伺い40 の段の方法の質問への試しの言葉そのまま）", misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY),
    _row("N5", "メールはちょっと…", [UNDECIDED], scene=WITHDRAW_WAY_SCENE, reason=WHY_OTHER,
         why="取り消しではない（同じ段の試しの言葉と正解そのまま）。方法の答えは、並べた方法（訪問・お電話）のどれも選んでいない・メールを避けただけ＝決められない（説明の文と言葉から）", misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY),
    # (8) 決め手を伺う一言 E5 のあと（ELSEWHERE_POINT_SCENE）：本書 9-4 の伺い48 の段の ⏳「E5 のお返事を読む問い合わせ＝…は決め手を言った・…は決め手は無い」の試しの言葉そのまま
    #     ほかの会社の項目（分身 S4＝束 t の #4・#5・#7）：決め手・決めた経緯を言った3行は、説明の文「いまの言葉で、…ほかの会社に頼んでいる・…使っている、と言った」（言葉は会社に
    #     触れていない＝言っていない）と、いまの言葉を質問「そちらの会社をお選びになった決め手は」への返事として読む決まり（その会社を選んだ＝頼むと決めた、を認めた＝ご案内全体）の
    #     2つが当たる＝読み手2人とも両方を挙げた＝境目（どちらの読みも正解）。E5 では、ほかの会社のことはもう伺った（E1＝1通話1回）＝どちらの読みでも、判定の組がそろっても割れても、
    #     流れの制御の返事・中の状態は同じで、取り消せない印は付かない（rv178/S4/probe/split_t.txt）
    _row("E5", "料金が安いところですかね。", ["決め手を言った"], scene=ELSEWHERE_POINT_SCENE, elsewhere=_E5_BORDER,
         why="決め手（料金）を言った（本書 9-4 の伺い48 の段の ⏳ の試しの言葉そのまま）。束 t の #4：答えは読み手2人がそろった・ほかの会社の項目は境目（直す前の値＝言っていない）"),
    _row("E5", "対応が早いので。", ["決め手を言った"], scene=ELSEWHERE_POINT_SCENE, elsewhere=_E5_BORDER,
         why="決め手（対応）を言った（同じ）。束 t の #5：#4 と同じ"),
    _row("E5", "特にないですね。", ["決め手は無い"], scene=ELSEWHERE_POINT_SCENE,
         why="決め手は特に無い（同じ）。束 t の #6：ほかの会社の項目は読み手2人とも「言っていない」＝そろった読みのまま（言葉は決め手も会社も挙げていない）"),
    _row("E5", "前の担当が決めたので分からないです。", ["決め手は無い"], scene=ELSEWHERE_POINT_SCENE, elsewhere=_E5_BORDER,
         why="決め手は分からない（同じ。説明の文の「決め手は特に無い・分からない…」）。束 t の #7：答えは読み手2人がそろった（「わからない」の説明「言ったことそのものが選べるものに"
             "あれば、そちら」）・ほかの会社の項目は #4 と同じ境目（「前の担当が決めた」＝その会社に頼むと決めた経緯）。理由は読み手2人とも「担当ではない」「決められない」だが数えない"
             "（答えが「決め手は無い」＝流れの制御は理由の読みを使わない＝この表の理由の決まり。どの理由の読みでも動きは同じ＝rv178/S4/probe/flow_t.txt）"),
    # (9) ほかのご希望 W1（G17 で「どちらも要らない」と言い切ったあと＝測りの W1 の場面 g73_system_words.wish_scenes）：会話の繋がらない「それで結構です。」＝決められない
    {**_row("W1@G17", "それで結構です。", [UNDECIDED], reason=WHY_OTHER, near=["電話", "メール"],
            why="「それで」が何を指すか言葉から決まらない＝会話が繋がらない＝決められない（✅ 2026-11-05 日本時間23時19分06秒「「それで結構です。」は会話がつながっていない＝「無い」と"
                "受け取らず意味合いを尋ねる」・✅ 2026-11-08 日本時間16時34分23秒「判定が「電話」と読んだという判定もおかしい」＝本書 9-5 の会話1 の段の ⏳「W1 の試しの言葉に、この言葉と"
                "同じ形の言葉を「決められない」で入れて測る」）。束 t の #8：読み手2人とも「ほかに希望は無い」と「決められない」の両方を挙げた（会話まで読むと「無い」）＝お答え"
                "（11-05 の (2)「「ほかの希望は無い」とは受け取らず」・11-08＝この言葉の正解は会話が繋がらない＝決められない）で決めた＝決められないだけのまま（境目にしない）。"
                "2026-11-21 分身 B（rv179 の束1 の B1）：いちばん近い答えは「電話」か「メール」（✅ 2026-11-05 日本時間23時19分06秒「「結構ですとおっしゃったのは、電話で結構です"
                "(大丈夫です)という意味合いでしょうか？」のような尋ねの一言」＝「それで」は直前に並べた電話とメールを指す＝どちらも正解。「無い」は 11-05 の (2) のとおり当てない）", misgiving=[MISGIVING_OFF], misgiving_why=MG_OFF_WHY), "set": "W1"},
    # (10) ほかにご納得いただけない点 N2（ご承認いただけなかったときの手順＝g53_meaning._approval_scene の other_point）：本書 9-5 の (5) の ⏳「点を言っていない言葉の試しの言葉を作って」
    #      ＝選べるもの「分からない点・確かめたい点があると言ったが、どの点かは言っていない」の言葉（言葉は説明の文から作った。点を言った側の言葉は正解の表の N2 の9行）
    {**_row("N2", "はい、まだあります。", ["点は言っていない"], near=[NEAR_OFF],
            why="ほかにもあると言ったが、どの点かは言っていない（選べるものの説明のとおり）。いちばん近い答えは「答えの読みが決められないではない」（答えの正解から決まる・B1）"), "set": "N2"},
    {**_row("N2", "ええ、まだちょっとありますね。", ["点は言っていない"], near=[NEAR_OFF], why="同じ（どの点かは言っていない・いちばん近い答えも同じ）"), "set": "N2"},
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
